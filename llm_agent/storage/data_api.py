"""Хранилища поверх сервиса БД/API (services/data_api): телеметрия, каталог, журнал решений, запись событий.

Конверт события — contracts/event.schema.json; что пишет агент — services/data_api/AGENT_INTERFACE.md.
Подключение к Куполу и отправка команд остаются у агентной системы (agent/), здесь только HTTP к БД.
"""

from __future__ import annotations

import json
import sys
import uuid
from collections import deque
from pathlib import Path

import httpx

JOURNAL_TYPES = ("decision", "control", "control_response", "verification", "meeting_message", "action",
                 # копия лога агентной системы (agent/src/kupol/dome/datalog.py)
                 "agent_decision", "agent_action", "agent_verification", "agent_message")


def envelope(run_id: str, event_type: str, payload, *, session_id: str | None = None, source: str = "agent",
             channel: str = "internal", direction: str = "internal", request_id=None,
             node_id: str | None = None, metadata: dict | None = None) -> dict:
    return {"schema_version": "1.0", "event_id": str(uuid.uuid4()), "run_id": run_id, "session_id": session_id,
            "source": source, "channel": channel, "direction": direction, "event_type": event_type,
            "request_id": request_id, "node_id": node_id, "payload": payload, "metadata": metadata or {}}


class DataApiClient:
    """HTTP-клиент сервиса БД. Недоставленные события копятся в локальной очереди и уходят с прежними event_id."""

    def __init__(self, base_url: str, token: str | None = None, timeout_s: float = 5.0,
                 state_dir: Path | None = None, http: httpx.Client | None = None):
        self.http = http or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_s)
        if token:
            self.http.headers["Authorization"] = f"Bearer {token}"
        self.outbox = state_dir / "data_api_outbox.jsonl" if state_dir else None
        self.rejected = state_dir / "data_api_rejected.jsonl" if state_dir else None

    def get(self, path: str, **params):
        resp = self.http.get(path, params={k: v for k, v in params.items() if v is not None})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def pages(self, path: str, cursor: str, next_key: str, start=0, **params):
        """Все записи постранично: курсор исключающий, читаем до пустой страницы."""
        while True:
            page = self.get(path, **{cursor: start}, **params)
            items = page["items"] if page else []
            if not items:
                return
            yield from items
            start = page[next_key]

    def post(self, path: str, body: dict) -> dict:
        resp = self.http.post(path, json=body)
        resp.raise_for_status()
        return resp.json()

    def deliver(self, events: list[dict]) -> bool:
        """Пишет события вместе с ранее не доставленными. False — всё осталось в локальной очереди."""
        pending = self._read(self.outbox) + events
        if not pending:
            return True
        try:
            for i in range(0, len(pending), 1000):
                self._post_batch(pending[i:i + 1000])
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if self.outbox is None:
                raise
            self._write(self.outbox, pending)
            print(f"БД недоступна ({type(exc).__name__}), событий в очереди: {len(pending)}", file=sys.stderr)
            return False
        self._write(self.outbox, [])
        return True

    def _post_batch(self, batch: list[dict]) -> None:
        resp = self.http.post("/v1/events/batch", json={"events": batch})
        if resp.status_code not in (409, 422):
            resp.raise_for_status()
            return
        # Пакет откатывается целиком: пишем по одному, чтобы плохое событие не держало остальные
        for event in batch:
            one = self.http.post("/v1/events", json=event)
            if one.status_code in (409, 422):
                if self.rejected is None:
                    one.raise_for_status()
                self._append(self.rejected, {"status": one.status_code, "detail": one.text[:2000], "event": event})
            else:
                one.raise_for_status()

    @staticmethod
    def _read(path: Path | None) -> list[dict]:
        if path is None or not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    @staticmethod
    def _write(path: Path | None, events: list[dict]) -> None:
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")

    @staticmethod
    def _append(path: Path, record: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def latest_run_id(self) -> str | None:
        runs = list(self.pages("/v1/runs", "after_run_id", "next_after_run_id", start=""))
        return max(runs, key=lambda r: r["last_ingested_at"])["run_id"] if runs else None

    def last_event(self, run_id: str, event_type: str, **filters) -> dict | None:
        last = None
        for last in self.pages("/v1/events", "after_id", "next_after_id", run_id=run_id, event_type=event_type,
                               limit=1000, **filters):
            pass
        return last


class ApiTelemetryRepo:
    """Последнее состояние каждого узла из node_snapshots; время и game_time — из кадра telemetry."""

    def __init__(self, client: DataApiClient, run_id: str):
        self.client = client
        self.run_id = run_id
        self.event_ids: list[str] = []  # кадры, из которых собран последний снимок (observation_event_ids)

    def latest(self) -> dict:
        items = list(self.client.pages("/v1/telemetry/latest", "after_node_id", "next_after_node_id", start="",
                                       run_id=self.run_id))
        self.event_ids = sorted({i["event_id"] for i in items})
        snapshot = {"nodes": {i["node_id"]: i["payload"] for i in items}, "game_time": None, "timestamp": None}
        if not items:
            return snapshot
        frame = self.client.get(f"/v1/events/{max(items, key=lambda i: i['id'])['event_id']}") or {}
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        snapshot["timestamp"] = payload.get("timestamp")
        snapshot["game_time"] = data.get("game_time")
        snapshot["observed_at"] = (frame.get("metadata") or {}).get("observed_at")
        return snapshot

    def history(self, node_id: str, param: str, limit: int = 20) -> list[dict]:
        return []  # тренды агент пока не использует; при необходимости — /v1/events?node_id=...


class ApiCatalogRepo:
    """Каталог прав — последний успешный describe_nodes_response запуска; иначе запасной статический."""

    def __init__(self, client: DataApiClient, run_id: str, fallback=None):
        self.client = client
        self.run_id = run_id
        self.fallback = fallback

    def nodes(self) -> list[dict]:
        last = None
        for ev in self.client.pages("/v1/events", "after_id", "next_after_id", run_id=self.run_id,
                                    event_type="describe_nodes_response", limit=1000):
            if isinstance(ev["payload"], dict) and ev["payload"].get("success") is True:
                last = ev
        if last and isinstance(last["payload"].get("nodes"), list):
            return last["payload"]["nodes"]
        return self.fallback.nodes() if self.fallback else []


def impact_text(kind, authenticity, chain, issues=None) -> str:
    """Разметка воздействия в журнале промпта: модель видит свою прошлую оценку эпизода и замечания к ней."""
    text = f"разметка: {kind or 'unknown'}/{authenticity or 'unknown'}"
    if chain:
        text += f" ({' → '.join(chain)})"
    if issues:
        text += f", замечания: {'; '.join(issues)}"
    return text


def journal_record(ev: dict) -> dict:
    """Событие БД → запись журнала в формате DecisionJournalRepo.recent()."""
    p = ev["payload"] if isinstance(ev["payload"], dict) else {}
    meta = ev.get("metadata") or {}
    kind = ev["event_type"]
    rec = {"ts": ev.get("ingested_at"), "dome_time": meta.get("dome_time") or p.get("dome_time"),
           "actor": p.get("actor") if ev["source"] == "import" else ev["source"], "kind": kind}
    if kind == "agent_decision":
        rec["text"] = (f"{p.get('situation')}; причина: {p.get('cause')}; "
                       f"{impact_text(p.get('impact_kind'), p.get('authenticity'), p.get('causal_chain'), p.get('classification_issues'))}; "
                       f"план: {p.get('plan') or 'наблюдать'}")
        src = p.get("source")
        rec["actor"] = "operator" if src == "operator" else f"agent:{src}" if src else rec["actor"]
    elif kind == "agent_action":
        req = p.get("request") if isinstance(p.get("request"), dict) else {}
        resp = p.get("response") if isinstance(p.get("response"), dict) else {}
        rec.update(node_id=req.get("node_id"), action=req.get("action"), value=req.get("value"),
                   success=resp.get("success"), text=resp.get("error") or resp.get("skipped"))
    elif kind == "agent_verification":
        rec.update(text=p.get("expected"), outcome=f"{'подтверждено' if p.get('ok') else 'НЕ подтверждено'}: "
                                                   f"{json.dumps(p.get('observed'), ensure_ascii=False, default=str)}")
    elif kind == "agent_message":  # переписка с Оператором из лога агентной системы
        rec["actor"] = "operator" if p.get("direction") == "in" else "agent"
        rec["text"] = p.get("text")
    elif kind == "meeting_message":
        msg = p.get("message") if isinstance(p.get("message"), dict) else p
        sender = msg.get("sender") if isinstance(msg.get("sender"), dict) else {}
        rec["actor"] = sender.get("role") or rec["actor"]
        rec["text"] = msg.get("text")
    elif kind in ("control", "action", "control_response"):
        rec.update(node_id=p.get("node_id") or ev.get("node_id"), action=p.get("action"), value=p.get("value"))
        if p.get("success") is not None:
            rec["success"] = p["success"]
        if p.get("error"):
            rec["text"] = p["error"]
    else:  # decision, verification
        rec["text"] = p.get("text") or p.get("summary")
        rec["outcome"] = p.get("outcome")
    return {k: v for k, v in rec.items() if v is not None}


class ApiDecisionJournalRepo:
    """Журнал из событий БД: решения, команды и ответы Купола, проверки, чат. Читает только новые события."""

    def __init__(self, client: DataApiClient, run_id: str, sink: ApiEventSink, keep: int = 200):
        self.client = client
        self.run_id = run_id
        self.sink = sink
        self._cursor = {t: 0 for t in JOURNAL_TYPES}
        self._events: deque[dict] = deque(maxlen=keep)

    def recent(self, limit: int = 15) -> list[dict]:
        try:
            for kind in JOURNAL_TYPES:
                for ev in self.client.pages("/v1/events", "after_id", "next_after_id", start=self._cursor[kind],
                                            run_id=self.run_id, event_type=kind, limit=1000):
                    self._cursor[kind] = ev["id"]
                    self._events.append(ev)
        except httpx.HTTPError as exc:  # без свежего журнала агент работает на том, что уже прочитал
            print(f"журнал из БД не обновлён ({type(exc).__name__})", file=sys.stderr)
        ordered = sorted(self._events, key=lambda e: e["id"])[-limit:]
        return [journal_record(e) for e in ordered]

    def append(self, record: dict) -> None:
        if record.get("decision_event_id"):
            return  # решение уже записано событием decision при вызове decide()
        self.sink.emit("decision", record)


class ApiEventSink:
    def __init__(self, client: DataApiClient, run_id: str, session_id: str | None = None):
        self.client = client
        self.run_id = run_id
        self.session_id = session_id

    def emit(self, event_type: str, payload, **kwargs) -> str:
        event = envelope(self.run_id, event_type, payload, session_id=self.session_id, **kwargs)
        self.client.deliver([event])
        return event["event_id"]


class ApiEventFeed:
    """Новые события запуска для диспетчера: ответы Купола, таймауты, чат, проверки агентной системы.

    Без сохранённых курсоров начинает с конца истории: старые события не повод вызывать модель.
    """

    TYPES = ("control_response", "request_timeout", "meeting_message", "agent_verification",
             "change", "state_change", "node_change", "automation")

    def __init__(self, client: DataApiClient, run_id: str, cursors: dict[str, int] | None = None):
        self.client = client
        self.run_id = run_id
        self.cursors = dict(cursors or {})
        for kind in self.TYPES:
            if kind not in self.cursors:
                last = client.last_event(run_id, kind)
                self.cursors[kind] = last["id"] if last else 0

    def poll(self) -> list[dict]:
        events = []
        for kind in self.TYPES:
            for ev in self.client.pages("/v1/events", "after_id", "next_after_id", start=self.cursors[kind],
                                        run_id=self.run_id, event_type=kind, limit=1000):
                self.cursors[kind] = ev["id"]
                if kind == "control_response" and isinstance(ev["payload"], dict) \
                        and ev["payload"].get("success") is False:
                    ev["request"] = self._request(ev)
                events.append(ev)
        return sorted(events, key=lambda e: e["id"])

    def _request(self, ev: dict) -> dict:
        """Какую команду отклонил Купол: в ответе нет node_id/action, берём их из пары запрос-ответ."""
        if not ev.get("session_id") or ev.get("request_id") is None:
            return {}
        try:
            pair = self.client.get("/v1/exchanges", run_id=self.run_id, session_id=ev["session_id"],
                                   request_id=json.dumps(ev["request_id"]))
        except httpx.HTTPError:
            return {}
        requests = (pair or {}).get("requests") or []
        return requests[-1]["payload"] if requests and isinstance(requests[-1].get("payload"), dict) else {}


class ApiControlChecker:
    """POST /v1/controls/validate: control_intent агента, проверка сервера по каталогу и состоянию сессии.

    request_id намерения должна использовать агентная система при отправке команды в Купол.
    """

    def __init__(self, client: DataApiClient, run_id: str, session_id: str):
        self.client = client
        self.run_id = run_id
        self.session_id = session_id

    def check(self, node_id: str, action: str, value, metadata: dict) -> dict:
        request_id = f"llm-{uuid.uuid4().hex[:12]}"
        payload = {"type": "control", "request_id": request_id, "node_id": node_id, "action": action}
        if value is not None:
            payload["value"] = value
        event = envelope(self.run_id, "control_intent", payload, session_id=self.session_id, channel="websocket",
                         request_id=request_id, node_id=node_id, metadata=metadata)
        try:
            result = self.client.post("/v1/controls/validate", event)
        except httpx.HTTPError as exc:
            return {"status": "unavailable", "reasons": [f"{type(exc).__name__}"], "request_id": request_id,
                    "intent_event_id": event["event_id"]}
        validation = result.get("validation") or {}
        return {"status": validation.get("status", "unknown"), "reasons": validation.get("reasons", []),
                "request_id": request_id, "intent_event_id": event["event_id"]}
