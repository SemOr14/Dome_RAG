"""Тесты WS-клиента на локальном эмуляторе: авторизация, запросы, переподключение, лимит запросов."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from kupol.dome.client import DomeClient, NotConnected, RateLimiter, RequestTimeout
from kupol.dome.config import RuntimeCfg
from tests.dome_fake.server import FakeDome

CFG = RuntimeCfg(reconnect_min_s=1.0, reconnect_max_s=2.0, request_timeout_s=2.0, request_rate_per_s=5.0)


def run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=30))


async def until(pred: Any, timeout: float = 8.0) -> None:
    end = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < end, "условие не выполнено вовремя"
        await asyncio.sleep(0.02)


def test_auth_catalog_telemetry_and_control() -> None:
    async def scenario() -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        async with FakeDome() as dome:
            client = DomeClient(dome.url, dome.key, CFG, on_event=lambda e, d: events.append((e, d)))
            task = asyncio.create_task(client.run())
            await until(lambda: client.catalog_message is not None)
            assert client.role == "team" and client.team == 1
            assert await client.wait_telemetry(3)
            assert client.telemetry and client.telemetry["type"] == "telemetry"
            resp = await client.control("printer_3d_01", "home")
            assert resp["success"] and resp["node_id"] == "printer_3d_01"
            assert dome.received[-1] == {"type": "control", "request_id": resp["request_id"], "node_id": "printer_3d_01", "action": "home"}
            client.stop()
            await asyncio.wait_for(task, 5)
        names = [e for e, _ in events]
        assert names[:3] == ["connected", "auth", "catalog"] and names[-1] == "disconnected"

    run(scenario())


def test_wrong_key_is_reported_and_not_hammered() -> None:
    async def scenario() -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        async with FakeDome() as dome:
            client = DomeClient(dome.url, "WRONG-KEY-000", CFG, on_event=lambda e, d: events.append((e, d)))
            task = asyncio.create_task(client.run())
            await asyncio.sleep(2.5)
            client.stop()
            await asyncio.wait_for(task, 5)
            assert dome.auth_count == 0
        reasons = [d["reason"] for e, d in events if e == "disconnected"]
        assert reasons and "Неверный ключ" in reasons[0]
        assert len(reasons) <= 3  # растущая пауза, а не частые попытки

    run(scenario())


def test_reconnects_after_drop_and_after_session_revoked() -> None:
    async def scenario() -> None:
        events: list[str] = []
        async with FakeDome() as dome:
            client = DomeClient(dome.url, dome.key, CFG, on_event=lambda e, d: events.append(e))
            task = asyncio.create_task(client.run())
            await until(lambda: client.connected)
            await dome.kick_all()
            await until(lambda: not client.connected)
            await until(lambda: client.connected and client.connect_count == 2)
            await dome.revoke_all()
            await until(lambda: "revoked" in events)
            await until(lambda: client.connected and client.connect_count == 3)
            client.stop()
            await asyncio.wait_for(task, 5)

    run(scenario())


def test_guest_role_does_not_crash() -> None:
    async def scenario() -> None:
        async with FakeDome(role="guest") as dome:
            client = DomeClient(dome.url, dome.key, CFG)
            task = asyncio.create_task(client.run())
            await until(lambda: client.role == "guest")
            assert await client.wait_telemetry(3)
            client.stop()
            await asyncio.wait_for(task, 5)

    run(scenario())


def test_chat_messages_are_queued() -> None:
    async def scenario() -> None:
        async with FakeDome() as dome:
            client = DomeClient(dome.url, dome.key, CFG)
            task = asyncio.create_task(client.run())
            await until(lambda: client.connected)
            await dome.broadcast({"type": "meeting_message", "message": {"id": 7, "sender": {"role": "operator"}, "text": "привет"}})
            msg = await asyncio.wait_for(client.chat.get(), 3)
            assert msg["message"]["text"] == "привет"
            client.stop()
            await asyncio.wait_for(task, 5)

    run(scenario())


def test_request_without_connection_raises() -> None:
    async def scenario() -> None:
        client = DomeClient("ws://127.0.0.1:1", "k", CFG)
        with pytest.raises(NotConnected):
            await client.request("ping")

    run(scenario())


def test_request_timeout_when_server_is_silent() -> None:
    async def scenario() -> None:
        async with FakeDome() as dome:
            client = DomeClient(dome.url, dome.key, CFG)
            task = asyncio.create_task(client.run())
            await until(lambda: client.connected)
            dome._reply = lambda msg: None  # type: ignore[method-assign]  # сервер молчит
            with pytest.raises(RequestTimeout):
                await client.request("ping", timeout=0.3)
            client.stop()
            await asyncio.wait_for(task, 5)

    run(scenario())


def test_rate_limiter_spaces_requests() -> None:
    async def scenario() -> None:
        limiter = RateLimiter(5.0)  # не чаще 5/с → интервал 0,2 с
        t0 = time.monotonic()
        for _ in range(4):
            await limiter.wait()
        assert time.monotonic() - t0 >= 0.55

    run(scenario())
