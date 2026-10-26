"""Сценарные тесты: весь агент (клиент → плейбуки → Gate → лог) против эмулятора купола.

Время моделируется шагами по 2 с: тест толкает телеметрию и ждёт, пока агент закончит такт.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from kupol.dome.agentlog import AgentLog
from kupol.dome.config import load_dome_config
from kupol.dome.env import Settings
from kupol.dome.runner import Runner
from tests.dome_fake.server import FakeDome
from tests.dome_fake.world import World

ROOT = Path(__file__).resolve().parents[1]


class Harness:
    """Запущенный агент + эмулятор в пошаговом режиме."""

    def __init__(self, tmp_path: Path, world: World, can_control: bool = True, dry_run: bool = False, role: str = "team") -> None:
        self.world = world
        self.tmp = tmp_path
        self.dome = FakeDome(period_s=None, telemetry_fn=world.telemetry, on_control=world.control, role=role)
        self.can_control, self.dry_run = can_control, dry_run
        self.runner: Runner | None = None
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Harness:
        await self.dome.__aenter__()
        cfg = load_dome_config(ROOT / "dome.yaml")
        cfg.runtime.state_file = str(self.tmp / "state.json")
        st = Settings(dome_url=self.dome.url, dome_key=self.dome.key, deepseek_key="", dry_run=self.dry_run, allow_control=self.can_control)
        self.log = AgentLog(self.tmp / "logs", secrets=[self.dome.key], run_id="scenario")
        self.runner = Runner(st, cfg, self.log, clock=self.world.clock)
        self._task = asyncio.create_task(self.runner.run())
        await self.until(lambda: self.runner is not None and self.runner.client.connected and bool(self.runner.client.catalog_message))
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self.runner is not None and self._task is not None
        self.runner.stop()
        await asyncio.wait_for(self._task, 10)
        await self.dome.__aexit__()

    async def until(self, pred: Callable[[], bool], timeout: float = 10.0) -> None:
        end = time.monotonic() + timeout
        while not pred():
            assert time.monotonic() < end, "условие не выполнено вовремя"
            await asyncio.sleep(0.01)

    async def step(self, n: int = 1) -> None:
        """Продвинуть мир на n шагов по 2 с и дождаться, пока агент обработает каждый."""
        assert self.runner is not None
        for _ in range(n):
            target = self.runner.ticks + 1
            await self.dome.push(self.world.telemetry())
            await self.until(lambda: self.runner is not None and self.runner.ticks >= target)

    async def operator(self, text: str) -> None:
        msg = self.world.operator_says(text)
        await self.dome.broadcast({"type": "meeting_message", "message": msg})

    def records(self, kind: str | None = None) -> list[dict[str, Any]]:
        rows = [json.loads(x) for x in self.log.path.read_text(encoding="utf-8").splitlines()]
        return [r for r in rows if kind is None or r["kind"] == kind]

    def sent(self, action: str | None = None) -> list[dict[str, Any]]:
        return [m for m in self.dome.received if m.get("type") == "control" and m.get("node_id") != "meeting_01" and (action is None or m.get("action") == action)]


def run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=90))


def start_work(w: World) -> None:
    for m, name in (("printer_3d_01", "sensor_case.gcode"), ("cnc_01", "pcb_power_board.nc")):
        w.nodes[m].update({"state": "PRINTING" if m == "printer_3d_01" else "RUNNING", "file_name": name, "progress_pct": 10.0})
        if m == "printer_3d_01":
            w.nodes[m].update({"real_state": "PRINTING", "webhook_state": "PRINTING"})
    w.job_duration_s = 100_000.0  # задания идут весь сценарий


def spike(sensor: str, value: float = 3000.0) -> Callable[[World], None]:
    def fn(w: World) -> None:
        w.nodes[sensor]["co2_ppm"] = value

    return fn


# --------------------------------------------------------------------------- ложный CO2


def test_false_co2_is_handled_before_the_interlock_trips(tmp_path: Path) -> None:
    async def scenario() -> None:
        w = World()
        start_work(w)
        w.at(20, spike("climate_sensor_01"))
        async with Harness(tmp_path, w) as h:
            await h.step(8)  # до выброса
            assert not h.sent()
            await h.step(60)  # 120 с после: интерлоку хватило бы 60 с
            auto = w.nodes["dome_automation_01"]
            assert auto["interlocks"]["IL_CO2"]["state"] == "ARMED" and auto["interlocks"]["IL_CO2"]["trips"] == 0
            assert auto["data_trust"] == {"climate_sensor_01.co2_ppm": "UNRELIABLE"}
            assert auto["primary_co2_sensor"] in ("climate_sensor_02", "climate_sensor_03")
            assert w.nodes["cnc_01"]["state"] == "RUNNING" and w.nodes["printer_3d_01"]["state"] == "PRINTING"  # простоя не было
            assert [m["action"] for m in h.sent()] == ["set_data_trust", "set_primary_sensor"]
            assert len(w.posted) == 1 and "Ложный CO₂" in w.posted[0]
            dec = [r for r in h.records("decision") if "co2:" in r["situation"] or "ложное" in r["cause"]]
            assert dec and dec[0]["confidence"] >= 0.9 and dec[0]["source"] in ("rules", "mixed")
            ver = h.records("verification")
            assert len(ver) == 2 and all(v["ok"] for v in ver)

    run(scenario())


def test_false_co2_already_tripped_is_released_and_machines_resumed(tmp_path: Path) -> None:
    async def scenario() -> None:
        w = World("co2_tripped")
        w.nodes["printer_3d_01"].update({"files": ["a.gcode"], "bed_clear": True, "_prev": "PRINTING"})
        w.nodes["cnc_01"]["_prev"] = "RUNNING"
        w.job_duration_s = 100_000.0
        for m in ("printer_3d_01", "cnc_01"):
            w.nodes[m].update({"progress_pct": 10.0})
        async with Harness(tmp_path, w) as h:
            await h.step(30)
            auto = w.nodes["dome_automation_01"]
            assert auto["interlocks"]["IL_CO2"]["state"] == "ARMED"
            assert w.nodes["cnc_01"]["state"] == "RUNNING" and w.nodes["printer_3d_01"]["state"] == "PRINTING"
            assert w.nodes["cnc_01"]["resume_attempts"] == 0  # ни одной пустой попытки
            order = [m["action"] for m in h.sent()]
            assert order.index("reset_interlock") < order.index("resume")
            assert order.count("resume") == 2

    run(scenario())


def test_real_co2_is_not_reset_and_ventilation_goes_up(tmp_path: Path) -> None:
    async def scenario() -> None:
        w = World()
        start_work(w)
        for s in ("climate_sensor_01", "climate_sensor_02", "climate_sensor_03"):
            w.at(10, spike(s, 2300.0))
        async with Harness(tmp_path, w) as h:
            await h.step(80)
            assert w.nodes["dome_automation_01"]["interlocks"]["IL_CO2"]["state"] == "TRIPPED"
            assert not h.sent("reset_interlock") and not h.sent("resume") and not h.sent("set_data_trust")
            assert {"set_speed", "set_mode"} <= {m["action"] for m in h.sent()}
            assert w.nodes["cnc_01"]["state"] == "PAUSED"  # остановила автоматика, агент не обходит

    run(scenario())


# --------------------------------------------------------------------------- режимы управления


def test_read_only_mode_sends_nothing_but_logs_decisions(tmp_path: Path) -> None:
    async def scenario() -> None:
        w = World()
        start_work(w)
        w.at(10, spike("climate_sensor_01"))
        async with Harness(tmp_path, w, can_control=False) as h:
            await h.step(30)
            assert h.dome.received and not [m for m in h.dome.received if m.get("type") == "control"]
            acts = h.records("action")
            assert acts and all(a.get("dry_run") for a in acts)
            assert h.records("decision")
            assert any(r.get("dry_run") for r in h.records("message"))

    run(scenario())


def test_guest_role_never_controls(tmp_path: Path) -> None:
    async def scenario() -> None:
        w = World()
        w.at(10, spike("climate_sensor_01"))
        async with Harness(tmp_path, w, role="guest") as h:
            await h.step(20)
            assert not [m for m in h.dome.received if m.get("type") == "control"]
            assert not h.records("decision")
            assert h.records("connection")[1]["role"] == "guest"

    run(scenario())


# --------------------------------------------------------------------------- задания Оператора


def test_operator_task_starts_job_and_unlock_waits_for_real_completion(tmp_path: Path) -> None:
    async def scenario() -> None:
        w = World(job_duration_s=200.0)
        async with Harness(tmp_path, w) as h:
            await h.step(3)
            await h.operator("Следующее задание: sensor_case.gcode — корпус датчика.")
            await h.step(3)
            assert [(m["action"], m["value"]) for m in h.sent("start_job")] == [("start_job", "sensor_case.gcode")]
            assert w.nodes["printer_3d_01"]["state"] == "PRINTING"
            assert any("Запустил sensor_case.gcode" in p for p in w.posted)
            w.webhook_lies = True
            await h.step(5)
            await h.operator("Печать закончилась, заберу деталь — разблокируй бокс.")
            await h.step(3)
            assert not h.sent("set_lock") and any("печать ещё идёт" in p for p in w.posted)  # как в журнале 14.10, но без ошибки
            w.nodes["printer_3d_01"]["progress_pct"] = 99.0
            await h.step(10)  # печать закончилась по-настоящему
            await h.step(2)
            assert w.nodes["thermal_insulation_01"]["locks"][0] == "UNLOCKED"
            assert any("готова" in p and "разблокирован" in p for p in w.posted)
            ver = h.records("verification")
            assert ver and all(v["ok"] for v in ver)

    run(scenario())


def test_log_chain_decision_action_verification(tmp_path: Path) -> None:
    async def scenario() -> None:
        w = World()
        start_work(w)
        w.at(6, spike("climate_sensor_01"))
        async with Harness(tmp_path, w) as h:
            await h.step(20)
            rows = h.records()
            by_seq = {r["seq"]: r for r in rows}
            assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1))
            for a in h.records("action"):
                assert by_seq[a["ref"]]["kind"] == "decision"
            for v in h.records("verification"):
                assert by_seq[v["ref"]]["kind"] == "action"
            for m in h.records("message"):
                if m.get("direction") == "out":
                    assert by_seq[m["ref"]]["kind"] == "decision"
            assert rows[0]["kind"] == "start" and "dome_time" in rows[5]
            assert h.dome.key not in h.log.path.read_text(encoding="utf-8")

    run(scenario())


def test_scenario_logs_pass_the_organizers_format_checker(tmp_path: Path) -> None:
    import sys

    sys.path.insert(0, str(ROOT / "tools"))
    from check_agent_log import check  # type: ignore[import-not-found]

    async def scenario() -> Path:
        w = World()
        start_work(w)
        w.at(6, spike("climate_sensor_01"))
        async with Harness(tmp_path, w) as h:
            await h.step(12)
            await h.operator("Привет, я в куполе. Что по системам?")
            await h.step(3)
            return h.log.path

    path = run(scenario())
    assert check(path, secrets=["TESTKEY-123456"]) == []


# --------------------------------------------------------------------------- остальные ситуации


def test_phantom_line_is_switched_off_with_one_request_to_operator(tmp_path: Path) -> None:
    async def scenario() -> None:
        w = World(phantom=True)
        async with Harness(tmp_path, w) as h:
            await h.step(70)  # > 120 с
            assert [(m["action"], m["value"]) for m in h.sent()] == [("line_off", 8)]
            assert w.nodes["smart_panel_01"]["lines"][7]["state"] == "OFF"
            assert len(w.posted) == 1 and "клеммник" in w.posted[0]
            await h.step(60)
            assert len(h.sent()) == 1 and len(w.posted) == 1  # без повторов

    run(scenario())


def test_e09_waits_for_dry_air_then_resets_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        w = World()
        w.nodes["solar_inverter_01"].update({"error": True, "error_code": "E09", "error_text": "ISOLATION_FAULT", "mppt_state": "OFF"})
        w.nodes["weather_station_01"]["humidity_pct"] = 86.0
        async with Harness(tmp_path, w) as h:
            await h.step(400)  # ≈13 минут тумана
            assert not h.sent("reset_error")
            w.nodes["weather_station_01"]["humidity_pct"] = 55.0
            await h.step(10)
            assert len(h.sent("reset_error")) == 1 and w.nodes["solar_inverter_01"]["error"] is False
            assert [v["ok"] for v in h.records("verification")] == [True]

    run(scenario())


def test_false_smoke_is_acknowledged_in_time_real_smoke_is_not(tmp_path: Path) -> None:
    def alarm(w: World) -> None:
        w.nodes["smoke_detector_01"].update({"smoke_detected": True, "alarm_state": "ALARM", "false_alarm_suspected": True})
        w.nodes["dome_automation_01"]["events"].append({"event_id": "AUT-0100", "interlock": "IL_SMOKE", "type": "TRIP", "acknowledged": False})

    async def false_alarm() -> None:
        w = World()
        w.at(6, alarm)
        async with Harness(tmp_path / "f", w) as h:
            await h.step(10)
            assert [m["action"] for m in h.sent()] == ["acknowledge", "reset_alarm"]
            assert w.nodes["dome_automation_01"]["events"][-1]["acknowledged"] is True

    async def real_alarm() -> None:
        w = World()
        w.at(6, alarm)
        w.at(6, lambda w: w.nodes["air_quality_sensor_01"].update({"co_ppm": 45.0}))
        async with Harness(tmp_path / "r", w) as h:
            await h.step(10)
            assert not h.sent() and w.nodes["dome_automation_01"]["events"][-1]["acknowledged"] is False
            assert any("пожаротушение сработает штатно" in p for p in w.posted)

    run(false_alarm())
    run(real_alarm())


def test_frozen_node_gets_one_reconnect(tmp_path: Path) -> None:
    async def scenario() -> None:
        w = World()
        w.at(4, lambda w: w.nodes["cnc_01"].update({"status": "UNKNOWN"}))
        async with Harness(tmp_path, w) as h:
            await h.step(10)
            assert [(m["node_id"], m["action"]) for m in h.sent()] == [("cnc_01", "reconnect")]
            await h.step(20)
            assert len(h.sent("reconnect")) == 1  # не чаще раза в 5 минут

    run(scenario())


def test_restart_restores_state_and_does_not_repeat_decisions(tmp_path: Path) -> None:
    async def first() -> World:
        w = World(phantom=True)
        async with Harness(tmp_path, w) as h:
            await h.step(70)
            assert len(h.sent("line_off")) == 1
        return w

    async def second(w: World) -> None:
        w.nodes["smart_panel_01"]["lines"][7].update({"state": "ON", "power_w": 52.0})  # оператор включил обратно
        h2 = Harness(tmp_path, w)
        h2.log = None  # type: ignore[assignment]
        async with h2 as h:
            assert any("состояние восстановлено" in r.get("summary", "") for r in h.records("observation"))
            await h.step(70)
            # повторное решение подавлено кулдауном, сохранённым в state.json (час)
            assert not h.sent("line_off")

    w = run(first())
    assert (tmp_path / "state.json").is_file()
    run(second(w))
