"""Минимальный WebSocket-сервер с API купола: ``auth``, ``ping``, ``describe_nodes``, ``control``, ``query``, телеметрия."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Any

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from tests.dome_helpers import catalog_message

Handler = Callable[[dict[str, Any]], dict[str, Any]]


class FakeDome:
    """Сервер-заглушка. Телеметрию отдаёт ``telemetry_fn()``; ``control`` обрабатывает ``on_control``."""

    def __init__(
        self,
        key: str = "TESTKEY-123456",
        role: str = "team",
        period_s: float | None = 0.05,
        telemetry_fn: Callable[[], dict[str, Any]] | None = None,
        on_control: Handler | None = None,
        on_query: Handler | None = None,
    ) -> None:
        self.key = key
        self.role = role
        self.period_s = period_s
        self.telemetry_fn = telemetry_fn or (lambda: {"nodes": {}, "game_time": {"day": 0, "hour": 0.0}})
        self.on_control = on_control or (lambda msg: {"success": True, "mode": "virtual"})
        self.on_query = on_query or (lambda msg: {"success": True, "result": []})
        self.received: list[dict[str, Any]] = []
        self.connections: list[ServerConnection] = []
        self.auth_count = 0
        self.port = 0
        self._server: Any = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def __aenter__(self) -> FakeDome:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Послать сообщение всем (например, ``meeting_message``)."""
        for conn in list(self.connections):
            with contextlib.suppress(ConnectionClosed):
                await conn.send(json.dumps(message, ensure_ascii=False))

    async def push(self, data: dict[str, Any]) -> None:
        """Послать телеметрию всем подключённым (пошаговый режим, ``period_s=None``)."""
        await self.broadcast({"type": "telemetry", "timestamp": 0.0, "role": self.role, "data": data})

    async def revoke_all(self) -> None:
        """``session_revoked`` + закрытие с кодом 4403."""
        for conn in list(self.connections):
            with contextlib.suppress(ConnectionClosed):
                await conn.send(json.dumps({"type": "session_revoked", "reason": "смена активной команды"}))
                await conn.close(code=4403)

    async def kick_all(self) -> None:
        """Оборвать все соединения."""
        for conn in list(self.connections):
            with contextlib.suppress(ConnectionClosed):
                await conn.close(code=1011)

    async def _handle(self, ws: ServerConnection) -> None:
        try:
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        except (asyncio.TimeoutError, ConnectionClosed, ValueError):
            await ws.close(code=4401)
            return
        if first.get("type") != "auth" or first.get("key") != self.key:
            await ws.send(json.dumps({"type": "auth_response", "success": False, "error": "Неверный ключ доступа"}))
            await ws.close(code=4401)
            return
        self.auth_count += 1
        await ws.send(json.dumps({"type": "auth_response", "success": True, "role": self.role, "team": 1}))
        self.connections.append(ws)
        sender = asyncio.create_task(self._telemetry_loop(ws)) if self.period_s is not None else None
        try:
            async for raw in ws:
                msg = json.loads(raw)
                self.received.append(msg)
                reply = self._reply(msg)
                if reply is not None:
                    await ws.send(json.dumps(reply, ensure_ascii=False))
        except ConnectionClosed:
            pass
        finally:
            if sender is not None:
                sender.cancel()
            if ws in self.connections:
                self.connections.remove(ws)

    def _reply(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        kind = msg.get("type")
        rid = msg.get("request_id")
        if kind == "ping":
            return {"type": "ping_response", "request_id": rid, "success": True}
        if kind == "describe_nodes":
            return {**catalog_message(), "request_id": rid}
        if kind == "control":
            return {"type": "control_response", "request_id": rid, "node_id": msg.get("node_id"), "action": msg.get("action"), **self.on_control(msg)}
        if kind == "query":
            return {"type": "query_response", "request_id": rid, "node_id": msg.get("node_id"), "query": msg.get("query"), **self.on_query(msg)}
        return {"type": f"{kind}_response", "request_id": rid, "success": False, "error": "Неизвестный тип запроса"}

    async def _telemetry_loop(self, ws: ServerConnection) -> None:
        with contextlib.suppress(ConnectionClosed, asyncio.CancelledError):
            while True:
                data = self.telemetry_fn()
                await ws.send(json.dumps({"type": "telemetry", "timestamp": 0.0, "role": self.role, "data": data}, ensure_ascii=False))
                await asyncio.sleep(self.period_s or 1.0)
