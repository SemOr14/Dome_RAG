"""WebSocket-клиент купола: авторизация, запросы по ``request_id``, переподключение, лимит запросов.

Протокол — ``Dome_RAG/01_API.md``. Клиент ничего не решает: он поставляет телеметрию и чат,
отправляет запросы и сообщает о событиях связи. Управляющие команды отсюда напрямую не уходят —
только через ``gate.Gate``.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from kupol.dome.config import RuntimeCfg
from kupol.dome.control import SEND_GUARD, COMMAND_ACTOR
from kupol.dome.datalog import DataApiRecorder

AUTH_TIMEOUT_S = 10.0
CLOSE_UNAUTHORIZED = 4401
CLOSE_REVOKED = 4403

EventHook = Callable[[str, dict[str, Any]], None]


class DomeError(Exception):
    """Ошибка работы с куполом."""


class NotConnected(DomeError):
    """Нет активного соединения."""


class RequestTimeout(DomeError):
    """Ответ на запрос не пришёл вовремя."""


class PolicyRejected(DomeError):
    """Cancelled before send; unlike an in-flight timeout, delivery is known not to occur."""


class RateLimiter:
    """Не чаще ``rate`` отправок в секунду (равномерные интервалы между отправками)."""

    def __init__(self, rate_per_s: float) -> None:
        self._gap = 1.0 / min(rate_per_s, 5.0)
        self._next = 0.0
        self._lock = asyncio.Lock()
        self._queue = []
        self._condition = asyncio.Condition()
        self._ticket = 0

    async def wait(self, urgent=False) -> None:
        async with self._condition:
            self._ticket += 1
            ticket = (0 if urgent else 1, self._ticket)
            self._queue.append(ticket)
            try:
                while True:
                    delay = self._next - time.monotonic()
                    if ticket == min(self._queue) and delay <= 0:
                        self._queue.remove(ticket)
                        # Ordinary requests leave capacity for emergency traffic.
                        self._next = time.monotonic() + max(self._gap, .2 if urgent else .25)
                        self._condition.notify_all()
                        return
                    try:
                        await asyncio.wait_for(self._condition.wait(), max(.01, delay))
                    except asyncio.TimeoutError:
                        pass
            finally:
                if ticket in self._queue:
                    self._queue.remove(ticket)
                self._condition.notify_all()


class DomeClient:
    """Клиент с автоматическим переподключением."""

    def __init__(
        self, url: str, key: str, cfg: RuntimeCfg, on_event: EventHook | None = None, recorder: DataApiRecorder | None = None
    ) -> None:
        """Создать клиент.

        Args:
            url: Адрес ``ws://host:port``.
            key: Ключ команды (в логи не попадает).
            cfg: Раздел ``runtime`` конфигурации.
            on_event: Хук событий связи: ``connected``, ``auth``, ``disconnected``, ``revoked`` и т. д.
            recorder: Журнал всего входа/выхода в БД/API (``None`` — не писать).
        """
        self._url = url
        self._key = key
        self._cfg = cfg
        self._on_event = on_event or (lambda _e, _d: None)
        self.recorder = recorder
        self.session_id: str | None = None  # своё на каждое соединение — для корреляции request_id в БД
        self._ws: Any = None
        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._limiter = RateLimiter(cfg.request_rate_per_s)
        self._stop = asyncio.Event()
        self.role: str = ""
        self.team: int | None = None
        self.connected = False
        self.telemetry: dict[str, Any] | None = None  # последнее сообщение telemetry целиком
        self.telemetry_seq = 0
        self.telemetry_event = asyncio.Event()
        self.chat: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.source_events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.catalog_message: dict[str, Any] | None = None
        self.connect_count = 0
        self._auth_error = ""
        self.policy = None

    # ------------------------------------------------------------------ управление жизнью

    def stop(self) -> None:
        """Остановить цикл подключения и закрыть соединение."""
        self._stop.set()
        self.telemetry_event.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    async def run(self) -> None:
        """Подключаться, читать и переподключаться, пока не вызван ``stop``."""
        delay = self._cfg.reconnect_min_s
        while not self._stop.is_set():
            authed = False
            reason = "соединение закрыто"
            try:
                authed = await self._session()
                if not authed:
                    reason = self._auth_error or "авторизация отклонена"
            except asyncio.CancelledError:
                self._drop_connection("остановка")
                raise
            except (OSError, TimeoutError, InvalidHandshake, ConnectionClosed) as exc:
                reason = f"{exc.__class__.__name__}: {exc}"
            except Exception as exc:  # noqa: BLE001 - клиент обязан жить дальше
                reason = f"неожиданная ошибка {exc.__class__.__name__}: {exc}"
            self._drop_connection(reason)
            if self._stop.is_set():
                break
            delay = self._cfg.reconnect_min_s if authed else min(delay * 2, self._cfg.reconnect_max_s)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)

    async def _session(self) -> bool:
        """Одно соединение. Возвращает ``True``, если авторизация прошла."""
        async with websockets.connect(self._url, max_size=50_000_000, open_timeout=10, ping_interval=20, ping_timeout=20) as ws:
            self._ws = ws
            self.session_id = str(uuid.uuid4())
            self.connect_count += 1
            self._emit("connected", url=self._url)
            auth_msg = {"type": "auth", "key": self._key}
            self._record("outbound", auth_msg)
            await ws.send(json.dumps(auth_msg))
            raw = await asyncio.wait_for(ws.recv(), timeout=AUTH_TIMEOUT_S)
            try:
                auth = json.loads(raw)
            except ValueError:
                self._record("inbound", None, raw)
                raise
            self._record("inbound", auth, raw)
            if not auth.get("success"):
                self._auth_error = f"авторизация отклонена: {auth.get('error', 'без пояснения')}"
                return False
            self.role = str(auth.get("role", ""))
            self.team = auth.get("team")
            self.connected = True
            self._emit("auth", role=self.role, team=self.team)
            reader = asyncio.create_task(self._reader(ws))
            try:
                with contextlib.suppress(DomeError):
                    resp = await self.request("describe_nodes")
                    self.catalog_message = resp
                    self._emit("catalog", success=resp.get("success"), nodes=len(resp.get("nodes", []) or []))
                stopper = asyncio.create_task(self._stop.wait())
                done, _ = await asyncio.wait({reader, stopper}, return_when=asyncio.FIRST_COMPLETED)
                stopper.cancel()
                if reader in done:
                    reader.result()
            finally:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reader
            return True

    async def _reader(self, ws: Any) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                self._record("inbound", None, raw)
                continue
            self._record("inbound", msg, raw)
            if not isinstance(msg, dict):
                continue
            kind = msg.get("type")
            rid = msg.get("request_id")
            if rid is not None and str(rid) in self._pending:
                fut = self._pending.pop(str(rid))
                if not fut.done():
                    fut.set_result(msg)
            elif kind == "telemetry":
                self.telemetry = msg
                self.telemetry_seq += 1
                self.telemetry_event.set()
            elif kind == "meeting_message":
                self.chat.put_nowait(msg)
            elif kind in ("change", "state_change", "node_change", "automation"):
                self.source_events.put_nowait(msg)
            elif kind == "meeting_cleared":
                self._emit("meeting_cleared")
            elif kind == "session_revoked":
                self._emit("revoked", reason=msg.get("reason", ""))
                await ws.close(code=1000)
                return

    def _drop_connection(self, reason: str) -> None:
        self.connected = False
        self._ws = None
        self.role = ''
        self.catalog_message = None
        self.telemetry = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(NotConnected("соединение закрыто"))
        self._pending.clear()
        self._emit("disconnected", reason=reason)

    def _record(self, direction: str, msg: Any, raw: Any = None) -> None:
        if self.recorder is None:
            return
        try:
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            self.recorder.frame(direction, msg, self.session_id, text)
        except Exception as exc:  # noqa: BLE001
            if direction == "outbound" and isinstance(msg, dict) and msg.get("type") == "control":
                raise DomeError("команда не сохранена в журнал; отправка запрещена") from exc

    def _emit(self, event: str, **details: Any) -> None:
        if self.recorder is not None:
            try:
                self.recorder.event("connection", {"event": event, **details}, session_id=self.session_id)
            except Exception:  # noqa: BLE001
                pass
        try:
            self._on_event(event, details)
        except Exception:  # noqa: BLE001 - хук журнала не должен ронять связь
            pass

    # ------------------------------------------------------------------ запросы

    async def request(self, type_: str, timeout: float | None = None, *, request_id: str | None = None,
                      **fields: Any) -> dict[str, Any]:
        """Отправить запрос и дождаться ответа с тем же ``request_id``.

        Raises:
            NotConnected: Нет соединения (или оно оборвалось по пути).
            RequestTimeout: Ответ не пришёл за ``timeout``.
        """
        ws = self._ws
        if ws is None:
            raise NotConnected("нет соединения с куполом")
        rid = request_id if request_id is not None else str(next(self._ids))
        if rid in self._pending:
            raise DomeError("request_id уже ожидает ответ в этой сессии")
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._limiter.wait(urgent=COMMAND_ACTOR.get() == 'safety')
            if self._stop.is_set() or (self.policy and self.policy.operating_state == 'shutting_down'):
                raise PolicyRejected('Приложение останавливается')
            guard = SEND_GUARD.get()
            if type_ == 'control' and self.policy is not None and guard is None:
                raise PolicyRejected('Команды разрешены только через Gate')
            if guard and (bad := guard()):
                raise PolicyRejected(bad)
            msg = {"type": type_, "request_id": rid, **fields}
            self._record("outbound", msg)
            await ws.send(json.dumps(msg, ensure_ascii=False))
            return await asyncio.wait_for(fut, timeout=timeout or self._cfg.request_timeout_s)
        except asyncio.TimeoutError as exc:
            if self.recorder is not None:
                self.recorder.event("request_timeout", {"type": type_, "request_id": rid, "node_id": fields.get("node_id")},
                                    session_id=self.session_id, request_id=rid)  # fmt: skip
            raise RequestTimeout(f"{type_}: нет ответа за {timeout or self._cfg.request_timeout_s:g} с") from exc
        except ConnectionClosed as exc:
            raise NotConnected("соединение закрыто при отправке") from exc
        finally:
            self._pending.pop(rid, None)

    async def control(self, node_id: str, action: str, value: Any = None, *,
                      request_id: str | None = None) -> dict[str, Any]:
        """Отправить ``control``. Вызывается только из ``Gate``."""
        fields: dict[str, Any] = {"node_id": node_id, "action": action}
        if value is not None:
            fields["value"] = value
        return await self.request("control", request_id=request_id, **fields)

    async def query(self, node_id: str, query: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Запрос данных узла (чтение)."""
        return await self.request("query", node_id=node_id, query=query, params=params or {})

    async def wait_telemetry(self, timeout: float) -> bool:
        """Дождаться новой телеметрии. ``False`` — таймаут или остановка."""
        self.telemetry_event.clear()
        try:
            await asyncio.wait_for(self.telemetry_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return not self._stop.is_set()


SendHook = Callable[[str, str, Any], Awaitable[dict[str, Any]]]

