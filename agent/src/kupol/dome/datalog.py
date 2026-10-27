"""Запись всего обмена с куполом в сервис БД/API (``services/data_api``).

Каждый кадр WebSocket (вход и выход), события связи и ошибки JSON становятся конвертом
``contracts/event.schema.json``. Запись не блокирует цикл агента: события уходят фоновым потоком
пакетами, при недоступной БД копятся в ``outbox`` и досылаются с прежними ``event_id``.
Ключ ``auth`` маскируется до записи. Выключено, пока не задан ``DATA_API_URL``.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

MASK = "***"
BATCH = 200


def _utc() -> str:
    return datetime.now(UTC).isoformat()


class DataApiRecorder:
    """Неблокирующий журнал событий в БД. Один ``run_id`` на запуск агента, ``session_id`` — на соединение."""

    def __init__(
        self,
        url: str,
        token: str = "",
        run_id: str | None = None,
        state_dir: str | Path = "state",
        timeout_s: float = 5.0,
        http: httpx.Client | None = None,
    ) -> None:
        """Создать журнал.

        Args:
            url: Адрес сервиса БД (``http://127.0.0.1:8080``).
            token: ``DATA_API_TOKEN`` сервиса (в события не попадает).
            run_id: Идентификатор запуска; по умолчанию новый.
            state_dir: Каталог локальной очереди недоставленных и отклонённых событий.
            timeout_s: Таймаут HTTP.
            http: Готовый HTTP-клиент (для тестов).
        """
        self.run_id = run_id or f"agent-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
        self._http = http or httpx.Client(base_url=url.rstrip("/"), timeout=timeout_s)
        if token:
            self._http.headers["Authorization"] = f"Bearer {token}"
        state = Path(state_dir)
        self.outbox = state / "data_api_outbox.jsonl"
        self.rejected = state / "data_api_rejected.jsonl"
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._idle = threading.Event()
        self._idle.set()
        self.delivered = 0
        self.last_error = ""
        self.storage_error = ""
        self._spool_lock = threading.Lock()
        self._thread = threading.Thread(target=self._worker, name="data-api-recorder", daemon=True)
        self._thread.start()
        self._queue.put({})  # deliver durable records from the previous process

    # ------------------------------------------------------------------ запись

    def frame(self, direction: str, message: Any, session_id: str | None, raw: str | None = None) -> str:
        """Кадр WebSocket. ``message=None`` — кадр не разобрался как JSON (сохраняется ``raw``)."""
        msg = message if isinstance(message, dict) else None
        kind = str(msg.get("type") or "unknown") if msg else "invalid_json"
        payload: Any = message
        if msg and kind == "auth" and "key" in msg:
            payload = {**msg, "key": MASK}
            raw = None
        metadata: dict[str, Any] = {"recorded_at": _utc()}
        if direction == "inbound" and kind == "telemetry":
            metadata["observed_at"] = metadata["recorded_at"]  # время приёма кадра агентом
        return self.event(
            kind, payload, source="dome" if direction == "inbound" else "agent", channel="websocket", direction=direction,
            session_id=session_id, request_id=msg.get("request_id") if msg else None,
            node_id=msg.get("node_id") if msg and isinstance(msg.get("node_id"), str) else None,
            source_timestamp=str(msg["timestamp"]) if msg and isinstance(msg.get("timestamp"), (int, float, str)) else None,
            raw_text=raw if msg is None else None, metadata=metadata,
        )  # fmt: skip

    def event(self, event_type: str, payload: Any, *, source: str = "agent", channel: str = "internal",
              direction: str = "internal", session_id: str | None = None, request_id: Any = None, node_id: str | None = None,
              source_timestamp: str | None = None, raw_text: str | None = None, metadata: dict[str, Any] | None = None) -> str:  # fmt: skip
        """Поставить событие в очередь. Возвращает его ``event_id``."""
        event = {
            "schema_version": "1.0", "event_id": str(uuid.uuid4()), "run_id": self.run_id, "session_id": session_id,
            "source": source, "channel": channel, "direction": direction, "event_type": event_type,
            "request_id": request_id, "node_id": node_id, "source_timestamp": source_timestamp,
            "payload": payload, "raw_text": raw_text, "metadata": metadata or {"recorded_at": _utc()},
        }  # fmt: skip
        self._idle.clear()
        try:
            with self._spool_lock:
                self.outbox.parent.mkdir(parents=True, exist_ok=True)
                with self.outbox.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
        except OSError as exc:
            self.storage_error = type(exc).__name__
            raise
        self._queue.put(event)
        return event["event_id"]

    def flush(self, timeout: float = 10.0) -> bool:
        """Дождаться отправки очереди (``False`` — не успели или БД недоступна)."""
        if not self._idle.wait(timeout):
            return False
        try:
            with self._spool_lock:
                return not self._read(self.outbox)
        except (OSError, ValueError, UnicodeError) as exc:
            self.storage_error = type(exc).__name__
            return False

    def close(self, timeout: float = 10.0) -> None:
        """Отправить остаток и остановить поток."""
        self._queue.put(None)
        self._thread.join(timeout)

    def get(self, path: str, **params: Any) -> Any:
        """Чтение из сервиса БД (блокирующее; в асинхронном коде — в потоке)."""
        resp = self._http.get(path, params={k: v for k, v in params.items() if v is not None})
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------ доставка

    def _worker(self) -> None:
        stop = False
        while not stop:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                item = {}
            self._idle.clear()
            batch: list[dict[str, Any]] = []
            if item is None:
                stop = True
            else:
                batch.append(item)
            while len(batch) < BATCH:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    stop = True
                else:
                    batch.append(item)
            try:
                self._deliver(batch)
            except (OSError, ValueError, UnicodeError) as exc:
                # Keep the original spool intact for repair; never silently drop a bad tail.
                self.storage_error = type(exc).__name__
                time.sleep(0.5)
            if self._queue.empty():
                self._idle.set()

    def _deliver(self, events: list[dict[str, Any]]) -> None:
        with self._spool_lock:
            pending = self._read(self.outbox)
        if not pending:
            return
        try:
            for i in range(0, len(pending), 1000):
                self._post(pending[i : i + 1000])
        except (httpx.HTTPError, OSError) as exc:
            self.last_error = exc.__class__.__name__
            time.sleep(0.5)  # не долбить недоступную БД; следующая попытка — со следующим событием
            return
        delivered_ids = {e["event_id"] for e in pending}
        with self._spool_lock:
            self._write(self.outbox, [e for e in self._read(self.outbox) if e["event_id"] not in delivered_ids])
        self.last_error = ""
        self.storage_error = ""
        self.delivered += len(pending)

    def _post(self, batch: list[dict[str, Any]]) -> None:
        resp = self._http.post("/v1/events/batch", json={"events": batch})
        if resp.status_code not in (409, 422):
            resp.raise_for_status()
            return
        for event in batch:  # пакет откатился целиком — находим плохое событие, остальные пишем
            one = self._http.post("/v1/events", json=event)
            if one.status_code in (409, 422):
                self.rejected.parent.mkdir(parents=True, exist_ok=True)
                with self.rejected.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"status": one.status_code, "detail": one.text[:2000], "event": event}, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            else:
                one.raise_for_status()

    @staticmethod
    def _read(path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    @staticmethod
    def _write(path: Path, events: list[dict[str, Any]]) -> None:
        if not events and not path.is_file():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events))
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
