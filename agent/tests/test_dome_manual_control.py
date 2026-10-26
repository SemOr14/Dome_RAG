"""Ручная команда Оператора: интерфейс → сервис БД → агент → Gate → Купол, итог — событие control_dispatch."""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest

from kupol.dome.agentlog import AgentLog
from kupol.dome.config import load_dome_config
from kupol.dome.datalog import DataApiRecorder
from kupol.dome.env import Settings
from kupol.dome.runner import Runner
from tests.dome_fake.world import World
from tests.test_dome_scenarios import ROOT, Harness, run, start_work

sys.path.insert(0, str(ROOT.parent / "services" / "data_api"))
testclient = pytest.importorskip("fastapi.testclient")
from data_api.app import create_app  # noqa: E402

TOKEN = "test-service-secret"


class DbHarness(Harness):
    def __init__(self, tmp_path: Path, world: World, http) -> None:
        super().__init__(tmp_path, world)
        self.http = http

    async def __aenter__(self) -> DbHarness:
        await self.dome.__aenter__()
        cfg = load_dome_config(ROOT / "dome.yaml")
        cfg.runtime.state_file = str(self.tmp / "state.json")
        st = Settings(dome_url=self.dome.url, dome_key=self.dome.key, deepseek_key="", allow_control=True)
        self.log = AgentLog(self.tmp / "logs", secrets=[self.dome.key], run_id="manual")
        self.recorder = DataApiRecorder("http://testserver", TOKEN, "run-manual", self.tmp / "state", http=self.http)
        self.runner = Runner(st, cfg, self.log, clock=self.world.clock, recorder=self.recorder)
        self._task = asyncio.create_task(self.runner.run())
        await self.until(lambda: self.runner is not None and self.runner.client.connected and bool(self.runner.client.catalog_message))
        return self

    async def settle(self, n: int = 1) -> None:
        """Шаг мира и доставка событий в БД (опрос очереди идёт в фоне)."""
        for _ in range(n):
            await self.step()
            await asyncio.to_thread(self.recorder.flush, 5)
            await asyncio.sleep(0.05)

    def intent(self, node: str, action: str, value=None) -> dict:
        assert self.runner is not None
        payload = {"type": "control", "request_id": f"ui-{uuid.uuid4().hex[:8]}", "node_id": node, "action": action}
        if value is not None:
            payload["value"] = value
        event = {"event_id": str(uuid.uuid4()), "run_id": "run-manual", "session_id": self.runner.client.session_id,
                 "source": "operator", "channel": "websocket", "direction": "internal", "event_type": "control_intent",
                 "request_id": payload["request_id"], "node_id": node, "payload": payload,
                 "metadata": {"operator": "Оператор", "reason": "проверка вручную"}}
        return self.http.post("/v1/controls/validate", json=event).json()

    def dispatches(self) -> list[dict]:
        return self.http.get("/v1/events", params={"run_id": "run-manual", "event_type": "control_dispatch"}).json()["items"]


def test_operator_command_goes_through_gate_to_dome(tmp_path: Path) -> None:
    async def scenario() -> None:
        w = World()
        start_work(w)
        with testclient.TestClient(create_app(tmp_path / "db.sqlite3", token=TOKEN)) as http:
            http.headers["Authorization"] = f"Bearer {TOKEN}"
            async with DbHarness(tmp_path, w, http) as h:
                await h.settle(3)
                ok = h.intent("dome_automation_01", "set_primary_sensor", "climate_sensor_02")
                assert ok["validation"]["status"] == "accepted", ok["validation"]["reasons"]
                bad = h.intent("supply_ventilation_01", "set_speed", 5)  # сервер пропустит, политика Gate — нет
                await h.settle(4)

                assert [(m["action"], m.get("value")) for m in h.sent()] == [("set_primary_sensor", "climate_sensor_02")]
                assert w.nodes["dome_automation_01"]["primary_co2_sensor"] == "climate_sensor_02"
                by_intent = {d["payload"]["intent_event_id"]: d["payload"] for d in h.dispatches()}
                assert by_intent[ok["event"]["event_id"]]["status"] == "sent"
                if bad["validation"]["status"] == "accepted":
                    assert by_intent[bad["event"]["event_id"]]["status"] == "rejected"
                dec = [r for r in h.records("decision") if r["source"] == "operator"]
                assert dec and dec[0]["intent_event_id"] == ok["event"]["event_id"]
                ver = [v for v in h.records("verification") if v["expected"].startswith("dome_automation_01.primary_co2_sensor")]
                assert ver and ver[0]["ok"]

                # Прямой вызов (панель) после опроса очереди не исполняет намерение повторно
                assert h.runner is not None
                assert await h.runner.manual_command({"event": ok["event"], "validation": ok["validation"]}) is None
                assert len(h.sent()) == 1

    run(scenario())


def test_confirmed_command_bypasses_agent_policy_only_when_confirmed(tmp_path: Path) -> None:
    async def scenario() -> None:
        w = World()
        start_work(w)
        with testclient.TestClient(create_app(tmp_path / "db.sqlite3", token=TOKEN)) as http:
            http.headers["Authorization"] = f"Bearer {TOKEN}"
            async with DbHarness(tmp_path, w, http) as h:
                await h.settle(2)
                assert h.runner is not None

                def item(confirmed: bool) -> dict:
                    payload = {"type": "control", "node_id": "solar_panels_01", "action": "disable_all_mppt"}
                    return {"event": {"event_id": str(uuid.uuid4()), "payload": payload, "metadata": {"confirmed": confirmed}},
                            "validation": {"status": "accepted"}}

                plain = await h.runner.manual_command(item(False))  # нет в политике dome.yaml
                assert plain is not None and plain.status == "rejected" and "политикой" in plain.reason
                confirmed = await h.runner.manual_command(item(True))
                assert confirmed is not None and confirmed.status == "sent", confirmed.reason
                assert [m["action"] for m in h.sent()] == ["disable_all_mppt"]

    run(scenario())
