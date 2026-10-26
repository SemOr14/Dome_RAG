"""Тесты заглушки реального адаптера: REST-шаблон работает, точки адаптации честно не реализованы."""

from __future__ import annotations

import httpx
import pytest

from kupol.adapter.base import AdapterError, AdapterTimeout
from kupol.adapter.real import RealAdapter
from kupol.core.config import RealAdapterConfig
from kupol.core.models import Command


def make(handler, env: dict[str, str] | None = None, **cfg: object) -> RealAdapter:  # type: ignore[no-untyped-def]
    config = RealAdapterConfig(base_url="http://kupol.test", timeout_s=1, retries=2, **cfg)  # type: ignore[arg-type]
    client = httpx.Client(base_url=config.base_url, transport=httpx.MockTransport(handler))
    return RealAdapter(config, env=env or {}, client=client)


def test_mapping_points_are_not_implemented() -> None:
    a = make(lambda r: httpx.Response(200, json={}))
    with pytest.raises(NotImplementedError, match="MORNING_CHECKLIST"):
        a.get_telemetry()
    with pytest.raises(NotImplementedError):
        a.get_history("temp_c", 5)
    with pytest.raises(NotImplementedError):
        a.send_command(Command(name="vent_enable", args={"mode": "low"}))
    with pytest.raises(NotImplementedError):
        a.decode_command_result(Command(name="x"), {})


def test_request_sends_token_and_params() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"ok": True})

    a = make(handler, env={"KUPOL_API_TOKEN": "secret"})
    assert a._request("GET", "/history", params={"field": "temp_c", "minutes": 5}) == {"ok": True}
    assert seen[0].headers["authorization"] == "Bearer secret"
    assert seen[0].url.params["field"] == "temp_c"


def test_no_token_no_header() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={})

    make(handler)._request("GET", "/telemetry")
    assert "authorization" not in seen[0].headers


def test_timeouts_are_retried_then_become_adapter_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kupol.adapter.real.time.sleep", lambda s: None)
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(AdapterTimeout):
        make(handler)._request("GET", "/telemetry")
    assert len(calls) == 3  # первая попытка + 2 повтора


def test_server_error_retried_client_error_not(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kupol.adapter.real.time.sleep", lambda s: None)
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503 if req.url.path == "/telemetry" else 404)

    a = make(handler)
    with pytest.raises(AdapterError):
        a._request("GET", "/telemetry")
    assert len(calls) == 3
    calls.clear()
    with pytest.raises(AdapterError, match="404"):
        a._request("GET", "/nope")
    assert len(calls) == 1


def test_recovers_after_transient_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kupol.adapter.real.time.sleep", lambda s: None)
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("down", request=req)
        return httpx.Response(200, json={"v": 1})

    assert make(handler)._request("GET", "/telemetry") == {"v": 1}


def test_non_json_is_adapter_error() -> None:
    with pytest.raises(AdapterError, match="JSON"):
        make(lambda r: httpx.Response(200, text="<html>"))._request("GET", "/telemetry")


def test_commands_are_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kupol.adapter.real.time.sleep", lambda s: None)
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=req)

    a = make(handler)
    a.encode_command = lambda cmd: {"x": 1}  # type: ignore[method-assign]
    with pytest.raises(AdapterTimeout):
        a.send_command(Command(name="vent_enable", args={"mode": "low"}))
    assert len(calls) == 1


def test_is_connected_never_raises() -> None:
    assert make(lambda r: httpx.Response(200, json={})).is_connected()

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=req)

    assert not make(boom).is_connected()
    make(boom).close()
