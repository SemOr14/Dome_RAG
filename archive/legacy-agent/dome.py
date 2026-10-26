"""WebSocket-клиент купола: авторизация, запросы, телеметрия, переподключение."""
from __future__ import annotations

import asyncio
import itertools
import json
import time
from collections import deque
from typing import Any, Awaitable, Callable

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from agent_log import AgentLog
from config import Config


class DomeError(Exception):
    """Ошибка связи с куполом."""


class DomeClient:
    def __init__(
        self,
        cfg: Config,
        log: AgentLog,
        on_telemetry: Callable[[dict], Awaitable[None]] | None = None,
        on_message: Callable[[dict], Awaitable[None]] | None = None,
        on_role: Callable[[str, int | None], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.log = log
        self.on_telemetry = on_telemetry
        self.on_message = on_message
        self.on_role = on_role

        self.ws = None
        self.role: str | None = None
        self.team: int | None = None
        self.data: dict = {"nodes": {}, "game_time": {}}
        self.data_ts: float = 0.0
        self.connected = asyncio.Event()

        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future] = {}
        self._stop = asyncio.Event()
        self._send_times: deque[float] = deque(maxlen=16)
        self._chat_times: deque[float] = deque(maxlen=16)
        self._tasks: set[asyncio.Task] = set()
        self.last_action_seq: int = -1
        self.stats = {"sent": 0, "errors": 0, "reconnects": 0}

    # ---------------------------------------------------------------- lifecycle
    async def run(self) -> None:
        """Держит соединение, переподключается с паузой."""
        backoff = 1.0
        attempts = 0
        while not self._stop.is_set():
            try:
                await self._session()
                backoff = 1.0
                attempts = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — агент не должен падать
                attempts += 1
                # не засоряем лог: первая ошибка и далее каждая 20-я
                if attempts == 1 or attempts % 20 == 0:
                    self.log.write("error", where="dome.session",
                                   error=f"{type(exc).__name__}: {exc}", attempts=attempts)
            self.connected.clear()
            self.ws = None
            self._fail_pending(DomeError("соединение закрыто"))
            if self._stop.is_set():
                break
            self.stats["reconnects"] += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def stop(self) -> None:
        self._stop.set()
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:  # noqa: BLE001
                pass

    async def _session(self) -> None:
        async with connect(
            self.cfg.dome_url,
            max_size=50_000_000,
            open_timeout=10,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            self.ws = ws
            self.log.write("connection", event="connected", details=self.cfg.dome_url)
            await ws.send(json.dumps({"type": "auth", "key": self.cfg.dome_key}))
            raw = await asyncio.wait_for(ws.recv(), timeout=12)
            auth = json.loads(raw)
            if auth.get("type") != "auth_response":
                # сервер мог прислать телеметрию раньше — читаем до ответа
                auth = await self._wait_auth(ws, raw)
            if not auth.get("success"):
                self.log.write("connection", event="auth", role=None, details=auth.get("error"))
                raise DomeError(f"авторизация отклонена: {auth.get('error')}")
            self.role = auth.get("role")
            self.team = auth.get("team")
            self.log.write("connection", event="auth", role=self.role, team=self.team)
            if self.on_role:
                self.on_role(self.role or "", self.team)
            self.connected.set()
            try:
                await self._reader(ws)
            except ConnectionClosed as exc:
                self.log.write("connection", event="disconnected", details=f"code={exc.code} reason={exc.reason}")
                if exc.code == 4403:
                    self.log.write("connection", event="revoked", role=self.role, team=self.team,
                                   details=str(exc.reason))
            finally:
                self.connected.clear()

    async def _wait_auth(self, ws, first_raw: str) -> dict:
        try:
            msg = json.loads(first_raw)
            if msg.get("type") == "auth_response":
                return msg
        except json.JSONDecodeError:
            pass
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            if msg.get("type") == "auth_response":
                return msg
        raise DomeError("нет auth_response")

    async def _reader(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("type")
            if kind == "telemetry":
                self.data = msg.get("data") or {"nodes": {}}
                self.data_ts = time.time()
                if self.role:
                    self.data["role"] = self.role
                if self.on_telemetry:
                    task = asyncio.create_task(self.on_telemetry(self.data))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                continue
            if kind == "meeting_message":
                if self.on_message:
                    task = asyncio.create_task(self.on_message(msg.get("message") or {}))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                continue
            if kind == "meeting_cleared":
                continue
            if kind == "session_revoked":
                self.log.write("connection", event="revoked", role=self.role, team=self.team,
                               details=msg.get("reason"))
                raise DomeError("session_revoked")
            rid = msg.get("request_id")
            if rid is not None and str(rid) in self._pending:
                fut = self._pending.pop(str(rid))
                if not fut.done():
                    fut.set_result(msg)

    def _fail_pending(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    # ------------------------------------------------------------------ sending
    async def _throttle(self) -> None:
        now = time.monotonic()
        while len(self._send_times) >= self.cfg.max_cmds_per_s and now - self._send_times[0] < 1.0:
            await asyncio.sleep(0.05)
            now = time.monotonic()
        self._send_times.append(time.monotonic())

    async def request(self, payload: dict, timeout: float = 30.0) -> dict:
        if self.ws is None or not self.connected.is_set():
            raise DomeError("нет соединения с куполом")
        await self._throttle()
        rid = str(next(self._ids))
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self.ws.send(json.dumps({**payload, "request_id": rid}, ensure_ascii=False))
            self.stats["sent"] += 1
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(rid, None)
            self.stats["errors"] += 1
            raise DomeError(f"таймаут ответа на {payload.get('type')}") from exc
        except ConnectionClosed as exc:
            self._pending.pop(rid, None)
            self.stats["errors"] += 1
            raise DomeError("соединение закрыто") from exc

    async def control(
        self,
        node_id: str,
        action: str,
        value: Any = None,
        ref: int | None = None,
        timeout: float = 30.0,
    ) -> dict:
        payload: dict = {"type": "control", "node_id": node_id, "action": action}
        if value is not None:
            payload["value"] = value
        resp = await self.request(payload, timeout=timeout)
        ok = bool(resp.get("success"))
        self.log.write(
            "action",
            request=payload,
            response={"success": ok, "error": resp.get("error"), "mode": resp.get("mode")},
            ref=ref,
        )
        self.last_action_seq = self.log.seq
        return resp

    async def query(self, node_id: str, query: str, params: dict | None = None, ref: int | None = None) -> dict:
        payload: dict = {"type": "query", "node_id": node_id, "query": query}
        if params:
            payload["params"] = params
        resp = await self.request(payload, timeout=30.0)
        self.log.write(
            "action",
            request=payload,
            response={"success": bool(resp.get("success")), "error": resp.get("error")},
            ref=ref,
        )
        self.last_action_seq = self.log.seq
        return resp

    async def describe(self) -> dict:
        resp = await self.request({"type": "describe_nodes"}, timeout=30.0)
        return resp

    async def ping(self) -> bool:
        try:
            resp = await self.request({"type": "ping"}, timeout=10.0)
            return bool(resp.get("success"))
        except DomeError:
            return False

    async def say(self, text: str, to: str = "operator", ref: int | None = None) -> dict:
        """Сообщение в песочницу (не чаще 10 за 10 секунд)."""
        now = time.monotonic()
        while len(self._chat_times) >= 10 and now - self._chat_times[0] < 10.0:
            await asyncio.sleep(0.2)
            now = time.monotonic()
        self._chat_times.append(now)
        value = {"text": text[:4000], "to": to, "name": self.cfg.operator_name}
        resp = await self.request(
            {"type": "control", "node_id": "meeting_01", "action": "post_message", "value": value},
            timeout=20.0,
        )
        self.log.write(
            "message",
            direction="out",
            to=to,
            text=text[:4000],
            response={"success": bool(resp.get("success")), "error": resp.get("error")},
            ref=ref,
        )
        return resp

    async def read_messages(self, since_id: int | None = None, limit: int = 50) -> list:
        params: dict = {"limit": limit}
        if since_id is not None:
            params["since_id"] = since_id
        resp = await self.query("meeting_01", "read_messages", params)
        return resp.get("result") or []
