"""SQLite journal with atomic writes, derived projections and type-aware correlation."""

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .codec import dumps, loads, validate
from .control_rules import ALIASES
from .controls import actor, is_control, preflight
from .redaction import Redactor


class Conflict(ValueError):
    pass


def utcnow():
    return datetime.now(UTC)


def decode(row):
    return {**loads(row["body_json"]), "id": row["id"], "ingested_at": row["ingested_at"]}


class Store:
    def __init__(self, path, *, redactor=None, max_state_age=30):
        if str(path) == ":memory:":
            raise ValueError("Use a file-backed SQLite database")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor or Redactor()
        if max_state_age <= 0:
            raise ValueError("max_state_age must be positive")
        self.max_state_age = max_state_age
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            script = (
                Path(__file__).resolve().parents[1] / "migrations/001_initial.sql"
            ).read_text()
            conn.executescript("BEGIN IMMEDIATE;\n" + script + "\nCOMMIT;")
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations VALUES (1, ?)", (utcnow().isoformat(),)
            )

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def append(self, events):
        # Validate the whole batch before entering the transaction. Unknown payload is untouched.
        sanitized = [self.redactor.value(validate(e)) for e in events]
        with self.transaction() as conn:
            return [self._append(conn, e) for e in sanitized]

    def _append(self, conn, event):
        body = dumps(event)
        existing = conn.execute(
            "SELECT id, body_json FROM events WHERE event_id=?", (event["event_id"],)
        ).fetchone()
        if existing:
            if existing["body_json"] != body:
                raise Conflict("event_id already exists with different redacted content")
            return {"id": existing["id"], "event_id": event["event_id"], "created": False}
        now = utcnow()
        cursor = conn.execute(
            "INSERT INTO events(event_id,run_id,session_id,source,channel,direction,event_type,"
            "node_id,request_key,ingested_at,body_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                event["event_id"],
                event["run_id"],
                event.get("session_id"),
                event["source"],
                event["channel"],
                event["direction"],
                event["event_type"],
                event.get("node_id"),
                dumps(event.get("request_id")),
                now.isoformat(),
                body,
            ),
        )
        row_id = cursor.lastrowid
        for node_id, payload in self.snapshots(event).items():
            conn.execute(
                "INSERT INTO node_snapshots VALUES (?,?,?)", (row_id, node_id, dumps(payload))
            )
        if is_control(event):
            try:
                check = preflight(self, conn, event, now)
            except (TypeError, ValueError, KeyError, AttributeError, IndexError):
                # Malformed observed node fields must not prevent recording an attempted command.
                check = {
                    "accepted": False,
                    "status": "rejected",
                    "actor": actor(event),
                    "reasons": ["invalid_validation_context"],
                    "context_event_ids": [],
                    "checked_at": now.isoformat(),
                    "rules_version": "01_API-v1",
                    "scope": "syntax_catalogue_state",
                    "effect_verified": False,
                }
            p = event["payload"]
            success = p.get("success") if isinstance(p, dict) else None
            status = (
                "accepted" if success is True else "rejected" if success is False else "pending"
            )
            conn.execute(
                "INSERT INTO control_checks VALUES (?,?,?,?,?)",
                (row_id, actor(event), int(check["accepted"]), dumps(check), status),
            )
        p = event["payload"]
        if isinstance(p, dict) and (
            p.get("type") == "control_response" or event["event_type"] == "control_response"
        ):
            success = p.get("success")
            conn.execute(
                "INSERT INTO control_receipts VALUES (?,?,?,?)",
                (
                    row_id,
                    int(success) if type(success) is bool else None,
                    p.get("node_id") if isinstance(p.get("node_id"), str) else None,
                    p.get("action") if isinstance(p.get("action"), str) else None,
                ),
            )
        return {"id": row_id, "event_id": event["event_id"], "created": True}

    @staticmethod
    def snapshots(event):
        p = event["payload"]
        if event["event_type"] != "telemetry" or not isinstance(p, dict):
            return {}
        data = p.get("data")
        nodes = data.get("nodes") if isinstance(data, dict) else p.get("nodes")
        if isinstance(nodes, dict):
            return nodes
        if event.get("node_id") is not None:
            return {event["node_id"]: p}
        return {}

    def events(self, *, after_id=0, limit=100, **filters):
        clauses, params = ["e.id > ?"], [after_id]
        allowed = {
            "run_id",
            "session_id",
            "source",
            "channel",
            "direction",
            "event_type",
            "node_id",
        }
        for key, value in filters.items():
            if key == "request_id":
                clauses.append("e.request_key=?")
                params.append(dumps(value))
            elif key == "node_id":
                clauses.append(
                    "(e.node_id=? OR EXISTS (SELECT 1 FROM node_snapshots n "
                    "WHERE n.event_row_id=e.id AND n.node_id=?))"
                )
                params.extend([value, value])
            elif key in allowed:
                clauses.append(f"e.{key} IS ?")
                params.append(value)
            else:
                raise ValueError("Unknown event filter")
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT e.* FROM events e WHERE "
                + " AND ".join(clauses)
                + " ORDER BY e.id LIMIT ?",
                [*params, limit],
            ).fetchall()
        items = [decode(r) for r in rows]
        return {"items": items, "next_after_id": items[-1]["id"] if items else after_id}

    def event(self, event_id):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            return decode(row) if row else None

    def runs(self, *, after_run_id="", limit=100):
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT run_id,count(*) event_count,min(ingested_at) first_ingested_at,"
                "max(ingested_at) last_ingested_at FROM events WHERE run_id>? "
                "GROUP BY run_id ORDER BY run_id LIMIT ?",
                (after_run_id, limit),
            ).fetchall()
        items = [dict(r) for r in rows]
        return {"items": items, "next_after_run_id": items[-1]["run_id"] if items else after_run_id}

    def latest_telemetry(self, run_id, node_id=None, after_node_id="", limit=1000):
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT n.node_id,n.payload_json,e.* FROM node_snapshots n "
                "JOIN events e ON e.id=n.event_row_id WHERE e.run_id=? AND n.node_id>? "
                "AND (? IS NULL OR n.node_id=?) AND NOT EXISTS ("
                "SELECT 1 FROM node_snapshots n2 JOIN events e2 ON e2.id=n2.event_row_id "
                "WHERE e2.run_id=e.run_id AND n2.node_id=n.node_id AND e2.id>e.id) "
                "ORDER BY n.node_id LIMIT ?",
                (run_id, after_node_id, node_id, node_id, limit),
            ).fetchall()
        items = [
            {
                "id": r["id"],
                "event_id": r["event_id"],
                "run_id": r["run_id"],
                "node_id": r["node_id"],
                "ingested_at": r["ingested_at"],
                "payload": loads(r["payload_json"]),
            }
            for r in rows
        ]
        return {
            "items": items,
            "next_after_node_id": items[-1]["node_id"] if items else after_node_id,
        }

    def exchanges(self, run_id, session_id, request_id):
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE run_id=? AND session_id=? AND request_key=? "
                "AND channel='websocket' ORDER BY id",
                (run_id, session_id, dumps(request_id)),
            ).fetchall()
        requests = [decode(r) for r in rows if r["direction"] == "outbound"]
        responses = [decode(r) for r in rows if r["direction"] == "inbound"]
        state = (
            "complete"
            if requests and responses
            else "pending"
            if requests
            else ("orphan_response" if responses else "not_found")
        )
        if len(requests) > 1:
            state = "ambiguous"
        return {"requests": requests, "responses": responses, "state": state}

    @staticmethod
    def latest_message(conn, event, types):
        row = conn.execute(
            "SELECT * FROM events WHERE run_id=? AND session_id IS ? AND source='dome' "
            "AND direction='inbound' AND channel='websocket' AND event_type IN ("
            + ",".join("?" for _ in types)
            + ") ORDER BY id DESC LIMIT 1",
            (event["run_id"], event.get("session_id"), *types),
        ).fetchone()
        return decode(row) if row else None

    @staticmethod
    def latest_snapshot(conn, event, node_id):
        row = conn.execute(
            "SELECT e.*,n.payload_json FROM node_snapshots n JOIN events e ON e.id=n.event_row_id "
            "WHERE e.run_id=? AND e.session_id IS ? AND n.node_id=? AND e.source='dome' "
            "AND e.direction='inbound' AND e.channel='websocket' ORDER BY e.id DESC LIMIT 1",
            (event["run_id"], event.get("session_id"), node_id),
        ).fetchone()
        return (
            {
                "id": row["id"],
                "payload": loads(row["payload_json"]),
                "ingested_at": row["ingested_at"],
                "event": decode(row),
            }
            if row
            else None
        )

    @staticmethod
    def recent_outbound(conn, event, seconds, now):
        rows = conn.execute(
            "SELECT * FROM events WHERE run_id=? AND session_id IS ? AND channel='websocket' "
            "AND direction='outbound' AND ingested_at>=? AND event_id!=? ORDER BY id",
            (
                event["run_id"],
                event.get("session_id"),
                (now - timedelta(seconds=seconds)).isoformat(),
                event["event_id"],
            ),
        ).fetchall()
        return [decode(r) for r in rows if isinstance(loads(r["body_json"])["payload"], dict)]

    def control(self, event_id):
        with self.connection() as conn:
            row = conn.execute(
                "SELECT e.*,c.result_json,c.upstream_status FROM control_checks c "
                "JOIN events e ON e.id=c.event_row_id WHERE e.event_id=?",
                (event_id,),
            ).fetchone()
            if not row:
                return None
            e = decode(row)
            status = row["upstream_status"]
            receipts = []
            if e.get("session_id") and e.get("request_id") is not None:
                candidates = conn.execute(
                    "SELECT e.*,r.success FROM control_receipts r "
                    "JOIN events e ON e.id=r.event_row_id "
                    "WHERE e.run_id=? AND e.session_id=? AND e.request_key=? AND e.source='dome' "
                    "AND e.direction='inbound' AND e.channel='websocket' ORDER BY e.id",
                    (e["run_id"], e["session_id"], dumps(e["request_id"])),
                ).fetchall()
                for receipt in candidates:
                    p = loads(receipt["body_json"])["payload"]
                    cmd = e["payload"] if isinstance(e["payload"], dict) else {}
                    if p.get("request_id") is None or dumps(p["request_id"]) != dumps(
                        e["request_id"]
                    ):
                        continue
                    if p.get("node_id") is not None and p["node_id"] != cmd.get("node_id"):
                        continue
                    if p.get("action") is not None:
                        response_action = p["action"]
                        if not isinstance(response_action, str):
                            continue
                        node = cmd.get("node_id")
                        normalized = ALIASES.get(
                            (node, response_action.lower()), response_action.lower()
                        )
                        expected = loads(row["result_json"]).get("canonical_action")
                        if normalized not in (expected, cmd.get("action")):
                            continue
                    receipts.append(decode(receipt))
                values = {
                    r["payload"].get("success")
                    for r in receipts
                    if type(r["payload"].get("success")) is bool
                }
                if len(values) == 1:
                    received = "accepted" if True in values else "rejected"
                    status = received if status in ("pending", received) else "ambiguous"
                elif len(values) > 1:
                    status = "ambiguous"
                requests = conn.execute(
                    "SELECT count(*) FROM control_checks c JOIN events e ON e.id=c.event_row_id "
                    "WHERE e.run_id=? AND e.session_id=? AND e.request_key=? "
                    "AND e.direction='outbound' AND e.channel='websocket'",
                    (e["run_id"], e["session_id"], dumps(e["request_id"])),
                ).fetchone()[0]
                if requests > 1 and receipts:
                    status = "ambiguous"
            return {
                "event": e,
                "validation": loads(row["result_json"]),
                "upstream_status": status,
                "responses": receipts,
                "effect_verified": False,
            }

    def controls(self, *, after_id=0, limit=100, run_id=None, actor=None, validation=None):
        clauses, params = ["e.id>?"], [after_id]
        for key, value in (("e.run_id", run_id), ("c.actor", actor), ("c.accepted", validation)):
            if value is not None:
                clauses.append(key + "=?")
                params.append(value)
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT e.id,e.event_id FROM control_checks c JOIN events e ON e.id=c.event_row_id "
                "WHERE " + " AND ".join(clauses) + " ORDER BY e.id LIMIT ?",
                [*params, limit],
            ).fetchall()
        return {
            "items": [self.control(r["event_id"]) for r in rows],
            "next_after_id": rows[-1]["id"] if rows else after_id,
        }

    def audit(self, exchange):
        exchange = self.redactor.value(exchange)
        with self.transaction() as conn:
            return conn.execute(
                "INSERT INTO http_exchanges(started_at,finished_at,method,path,status,body_json) "
                "VALUES (?,?,?,?,?,?)",
                (
                    exchange["started_at"],
                    exchange["finished_at"],
                    exchange["request"]["method"],
                    exchange["request"]["path"],
                    exchange["response"]["status"],
                    dumps(exchange),
                ),
            ).lastrowid

    def http_exchanges(self, after_id=0, limit=100):
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT id,started_at,finished_at,method,path,status FROM http_exchanges "
                "WHERE id>? ORDER BY id LIMIT ?",
                (after_id, limit),
            ).fetchall()
        return {
            "items": [dict(r) for r in rows],
            "next_after_id": rows[-1]["id"] if rows else after_id,
        }

    def http_exchange(self, row_id):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM http_exchanges WHERE id=?", (row_id,)).fetchone()
        return {"id": row["id"], **loads(row["body_json"])} if row else None
