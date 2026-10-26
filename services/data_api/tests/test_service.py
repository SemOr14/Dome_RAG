import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from data_api.app import create_app
from data_api.codec import InvalidEvent, dumps
from data_api.import_jsonl import import_file
from data_api.redaction import Redactor
from data_api.store import Conflict, Store, utcnow


def event(payload=None, **kwargs):
    return {
        "event_id": str(uuid4()),
        "run_id": "r",
        "session_id": "s",
        "source": "dome",
        "channel": "websocket",
        "direction": "inbound",
        "event_type": "record",
        "payload": payload,
        **kwargs,
    }


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "db.sqlite3")


@pytest.fixture
def client(tmp_path):
    with TestClient(
        create_app(tmp_path / "http.sqlite3", token="test-service-secret"),
        headers={"Authorization": "Bearer test-service-secret"},
    ) as client:
        yield client


def context(store, node="smart_panel_01", actions=None, state=None, age=0, operators=None):
    now = utcnow()
    store.append(
        [
            event(
                {"type": "auth_response", "success": True, "role": "team"},
                event_type="auth_response",
            ),
            event(
                {
                    "type": "describe_nodes_response",
                    "success": True,
                    "nodes": [
                        {
                            "node_id": node,
                            "controls": actions or ["line_on", "line_off"],
                            "operator_controls": operators or [],
                            "control_aliases": {"line_on": ["ON"]},
                        }
                    ],
                },
                event_type="describe_nodes_response",
            ),
            event(
                {
                    "type": "telemetry",
                    "timestamp": 1000,
                    "data": {
                        "nodes": {
                            node: {
                                "online": True,
                                "status": "OK",
                                "updated_at": 1000,
                                "lines": [
                                    {
                                        "line": 1,
                                        "protection_state": "ARMED",
                                        "lockout_remaining_s": 0,
                                    }
                                ],
                                **(state or {}),
                            }
                        }
                    },
                },
                event_type="telemetry",
                metadata={"observed_at": (now - timedelta(seconds=age)).isoformat()},
            ),
        ]
    )


def command(
    *,
    node="smart_panel_01",
    action="line_on",
    value=1,
    request_id=1,
    source="agent",
    direction="outbound",
    **kwargs,
):
    return event(
        {
            "type": "control",
            "request_id": request_id,
            "node_id": node,
            "action": action,
            "value": value,
        },
        source=source,
        direction=direction,
        event_type="control_intent" if direction == "internal" else "control",
        request_id=request_id,
        node_id=node,
        **kwargs,
    )


def receipt(request_id=1, success=True, **kwargs):
    return event(
        {"type": "control_response", "request_id": request_id, "success": success},
        event_type="control_response",
        request_id=request_id,
        **kwargs,
    )


def test_lossless_payload_and_immutable_tables(store):
    payload = {"unknown": [None, False, 1, 1.0, "1", {"a": 2**62}], "data": "raw"}
    e = event(payload, source_timestamp="2026-10-14T01:02:03", metadata={"unknown": None})
    item = store.append([e])[0]
    read = store.event(e["event_id"])
    assert read["payload"] == payload
    assert type(read["payload"]["unknown"][2]) is int
    assert type(read["payload"]["unknown"][3]) is float
    assert read["source_timestamp"] == e["source_timestamp"]
    assert store.append([e])[0] == {**item, "created": False}
    with store.connection() as conn:
        for sql in ("UPDATE events SET source='agent'", "DELETE FROM events"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)


def test_conflict_and_batch_rollback(store):
    original = event({"x": 1})
    store.append([original])
    fresh = event({"x": None})
    with pytest.raises(Conflict):
        store.append([fresh, {**original, "payload": {"x": 1.0}}])
    assert store.event(fresh["event_id"]) is None
    with pytest.raises(InvalidEvent):
        store.append([fresh, {**event({}), "unknown_envelope_field": 1}])
    assert store.events()["items"][0]["payload"] == {"x": 1}


def test_concurrent_idempotency_and_restart(store):
    e = event({"x": [None]})
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: store.append([e])[0], range(12)))
    assert sum(r["created"] for r in results) == 1
    assert len(Store(store.path).events()["items"]) == 1


def test_snapshots_all_versions_and_latest_partial(store):
    first = event({"data": {"nodes": {"a": {"x": 1}, "b": None}}}, event_type="telemetry")
    second = event({"nodes": {"a": {"x": None, "future": True}}}, event_type="telemetry")
    store.append(
        [first, second, event({"nodes": {"a": 77}}, run_id="other", event_type="telemetry")]
    )
    items = store.latest_telemetry("r")["items"]
    assert [i["node_id"] for i in items] == ["a", "b"]
    assert items[0]["payload"] == {"x": None, "future": True}
    assert items[1]["payload"] is None
    assert len(store.events(node_id="a", run_id="r")["items"]) == 2
    with store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM node_snapshots").fetchone()[0] == 4


@pytest.mark.parametrize("request_id", [7, "7", 7.0])
def test_typed_correlation_and_sessions(store, request_id):
    for rid in (7, "7", 7.0):
        store.append(
            [
                event({"type": "query"}, request_id=rid, direction="outbound"),
                event({"type": "query_response"}, request_id=rid),
            ]
        )
    store.append([receipt(request_id, False, session_id="new")])
    pair = store.exchanges("r", "s", request_id)
    assert pair["state"] == "complete"
    assert len(pair["requests"]) == len(pair["responses"]) == 1
    assert type(pair["responses"][0]["request_id"]) is type(request_id)
    assert store.exchanges("r", "missing", request_id)["state"] == "not_found"


@pytest.mark.parametrize("source", ["agent", "operator"])
def test_control_check_and_upstream_success_are_separate(store, source):
    context(store)
    cmd = command(source=source)
    store.append([cmd])
    check = store.control(cmd["event_id"])
    assert check["validation"]["accepted"] is True, check
    assert check["upstream_status"] == "pending"
    store.append([receipt(1, False)])
    check = store.control(cmd["event_id"])
    assert check["validation"]["accepted"] is True
    assert check["upstream_status"] == "rejected"
    assert check["effect_verified"] is False


@pytest.mark.parametrize("value", [0, 9, True, "1", None, {"line": 1, "unknown": 3}])
def test_invalid_control_values_retained(store, value):
    context(store)
    cmd = command(value=value)
    store.append([cmd])
    check = store.control(cmd["event_id"])
    assert check["validation"]["accepted"] is False
    assert "invalid_value" in check["validation"]["reasons"]
    assert check["event"]["payload"]["value"] == value


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"age": 60}, "state_stale:smart_panel_01"),
        ({"state": {"online": False}}, "state_unavailable:smart_panel_01"),
        ({"state": {"status": "UNKNOWN"}}, "state_unavailable:smart_panel_01"),
        ({"state": {"updated_at": 1}}, "measurement_stale:smart_panel_01"),
        ({"actions": ["line_off"]}, "action_not_permitted"),
    ],
)
def test_control_context_rejections(store, change, reason):
    context(store, **change)
    cmd = command()
    store.append([cmd])
    assert reason in store.control(cmd["event_id"])["validation"]["reasons"]


def test_dome_rounded_updated_at_is_fresh(store):
    # Купол: updated_at округлён до мс и может быть на доли секунды «позже» timestamp кадра
    context(store, state={"updated_at": 1000.0005})
    cmd = command()
    store.append([cmd])
    assert store.control(cmd["event_id"])["validation"]["accepted"] is True


def test_chat_node_has_no_status_but_is_available(store):
    context(store, node="meeting_01", actions=["post_message"], state={"status": None, "updated_at": None})
    cmd = command(node="meeting_01", action="post_message", value={"text": "Проверка"})
    store.append([cmd])
    check = store.control(cmd["event_id"])["validation"]
    assert check["accepted"] is True, check["reasons"]


def test_revoked_session_and_physical_operator_permission(store):
    context(store, operators=["physical_action"])
    cmd = command(action="physical_action")
    store.append([cmd])
    assert "action_not_permitted" in store.control(cmd["event_id"])["validation"]["reasons"]
    operator = command(action="physical_action", source="operator", request_id=2)
    store.append([operator])
    assert (
        "parameter_rule_unavailable" in store.control(operator["event_id"])["validation"]["reasons"]
    )
    store.append([event({"type": "session_revoked"}, event_type="session_revoked")])
    cmd = command(request_id=3)
    store.append([cmd])
    assert "session_not_authorized" in store.control(cmd["event_id"])["validation"]["reasons"]


def test_response_before_request_conflicting_receipts_and_duplicate_request_id(store):
    store.append([receipt(1, True)])
    cmd = command()
    store.append([cmd])
    assert store.control(cmd["event_id"])["upstream_status"] == "accepted"
    store.append([receipt(1, False)])
    assert store.control(cmd["event_id"])["upstream_status"] == "ambiguous"


def test_machine_occupied_bed_and_unknown_context_fail_closed(store):
    context(
        store,
        node="printer_3d_01",
        actions=["start_job"],
        state={"state": "IDLE", "bed_clear": False, "files": ["part.gcode"]},
    )
    cmd = command(node="printer_3d_01", action="start_job", value="part.gcode")
    store.append([cmd])
    assert "printer_bed_not_clear" in store.control(cmd["event_id"])["validation"]["reasons"]


def test_http_all_routes_errors_body_and_secrets(client):
    e = event(
        {
            "key": "dome-secret",
            "future": {"API-Key": "api-secret"},
            "raw": '{"token":"llm-secret"}',
            "a": [None, 1.0],
        }
    )
    r = client.post("/v1/events", json=e)
    assert r.status_code == 201, r.text
    assert client.post("/v1/events", json=e).status_code == 200
    assert client.post("/v1/events", json={**e, "payload": None}).status_code == 409
    assert client.get("/v1/events?limit=0").status_code == 422
    assert client.get("/does-not-exist").status_code == 404
    assert client.get("/health", headers={"Authorization": "Bearer BAD"}).status_code == 401
    bad = client.post("/v1/events?token=query-secret", content='{"password":"broken-secret",')
    assert bad.status_code == 422
    audit = client.get("/v1/http-exchanges/" + r.headers["x-audit-id"]).json()
    assert audit["response"]["status"] == 201
    serialized = dumps(audit)
    for secret in ("dome-secret", "api-secret", "llm-secret", "test-service-secret"):
        assert secret not in serialized
    body = client.get("/v1/events").json()["items"][0]["payload"]
    assert body["a"] == [None, 1.0]
    audits = client.app.state.store.http_exchanges()["items"]
    assert {401, 404, 409, 422} <= {a["status"] for a in audits}
    for row in audits:
        text = dumps(client.app.state.store.http_exchange(row["id"]))
        assert "query-secret" not in text and "broken-secret" not in text


def test_api_batch_paging_filters_preflight_and_openapi(client):
    es = [event(i, source="agent", event_type="decision") for i in range(3)]
    assert client.post("/v1/events/batch", json={"events": es}).status_code == 200
    page = client.get("/v1/events", params={"limit": 2, "source": "agent"}).json()
    assert len(page["items"]) == 2
    assert (
        len(client.get("/v1/events", params={"after_id": page["next_after_id"]}).json()["items"])
        == 1
    )
    assert client.get("/v1/runs").json()["items"][0]["event_count"] == 3
    invalid = client.post("/v1/controls/validate", json=command(direction="internal"))
    assert invalid.status_code == 201
    assert invalid.json()["validation"]["status"] == "rejected"
    assert client.get("/openapi.json").status_code == 200
    assert (
        client.get(
            "/v1/exchanges", params={"run_id": "r", "session_id": "s", "request_id": "true"}
        ).status_code
        == 422
    )


def test_audit_failure_returns_503_without_positive_ack(client, monkeypatch):
    def unavailable(_):
        raise sqlite3.OperationalError("secret error")

    monkeypatch.setattr(client.app.state.store, "audit", unavailable)
    r = client.post("/v1/events", json=event({}))
    assert r.status_code == 503
    assert "secret error" not in r.text


def test_unhandled_500_is_audited(client):
    @client.app.get("/boom")
    def boom():
        raise RuntimeError("private")

    response = client.get("/boom")
    assert response.status_code == 500
    audit = client.app.state.store.http_exchange(int(response.headers["x-audit-id"]))
    assert audit["error_type"] == "RuntimeError"
    assert "private" not in dumps(audit)


@pytest.mark.parametrize("content", ['{"x":NaN}', '{"x":1,"x":2}', '{"x":Infinity}'])
def test_non_json_and_duplicate_keys_not_silently_coerced(client, content):
    assert client.post("/v1/events", content=content).status_code == 422


def test_import_lossless_deterministic_and_atomic(store, tmp_path):
    path = tmp_path / "telemetry.jsonl"
    lines = [
        dumps({"ts": "2026-10-14T12:00:00", "kind": "telemetry", "nodes": {"n": None}}),
        dumps(
            {
                "kind": "action",
                "actor": "operator",
                "node_id": "n",
                "action": "x",
                "value": {"unknown": [False, 1.0]},
                "success": False,
            }
        ),
        "broken key=secret",
    ]
    path.write_text("\n".join(lines) + "\n")
    args = {"run_id": "historical/day1", "origin": "historical", "input_id": "Logs/day1"}
    result = import_file(store, path, **args)
    assert result == {"records": 3, "created": 3, "duplicates": 0, "import_errors": 1}
    assert import_file(store, path, **args)["created"] == 0
    assert store.latest_telemetry("historical/day1")["items"][0]["payload"] is None
    assert store.controls()["items"][0]["upstream_status"] == "rejected"
    before = len(store.events()["items"])
    path.write_text(dumps({"unknown": 3}) + "\ninvalid\n")
    with pytest.raises(InvalidEvent):
        import_file(store, path, **{**args, "input_id": "different"}, strict=True)
    assert len(store.events()["items"]) == before


def test_envelope_import_conflict_rolls_back_whole_file(store, tmp_path):
    old = event({"x": 1})
    store.append([old])
    path = tmp_path / "envelopes.jsonl"
    path.write_text(dumps(event({})) + "\n" + dumps({**old, "payload": 3}) + "\n")
    with pytest.raises(Conflict):
        import_file(store, path, mode="envelope")
    assert len(store.events()["items"]) == 1


def test_redaction_embedded_text_and_known_values():
    r = Redactor(["known-secret"])
    text = dumps(
        r.value(
            {
                "payload": {"null": None, "f": False, "v": 1.0},
                "raw_text": 'auth key="abc" Bearer token123 known-secret',
                "nested": '{"Authorization":"Basic abcdef"}',
            }
        )
    )
    assert all(x not in text for x in ("abc", "token123", "known-secret", "abcdef"))
    assert json.loads(text)["payload"] == {"null": None, "f": False, "v": 1.0}


def test_response_alias_and_line_debounce(store):
    context(store)
    cmd = command(action="ON")
    store.append([cmd])
    assert store.control(cmd["event_id"])["validation"]["accepted"]
    response = receipt()
    response["payload"].update(node_id="smart_panel_01", action="line_on")
    store.append([response])
    assert store.control(cmd["event_id"])["upstream_status"] == "accepted"
    for n in range(2, 5):
        item = command(request_id=n)
        store.append([item])
    assert "line_switch_rate_limit" in store.control(item["event_id"])["validation"]["reasons"]


def test_batch_control_projection_rollback(store):
    context(store)
    old = event({})
    store.append([old])
    cmd = command()
    with pytest.raises(Conflict):
        store.append([cmd, {**old, "payload": True}])
    assert store.control(cmd["event_id"]) is None
    assert not store.controls()["items"]


def test_historical_unknown_actor_json_type_is_retained(store, tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(dumps({"kind": "action", "actor": [None], "success": True}) + "\n")
    import_file(store, path, run_id="historical/test", origin="historical")
    check = store.controls()["items"][0]
    assert check["event"]["payload"]["actor"] == [None]
    assert check["upstream_status"] == "accepted"
    assert check["validation"]["accepted"] is False
