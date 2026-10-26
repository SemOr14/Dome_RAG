"""Тесты Gate: запреты, защитные правила, режимы «только чтение»/боевой, проверка эффекта."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from kupol.dome.agentlog import AgentLog
from kupol.dome.client import RequestTimeout
from kupol.dome.config import load_dome_config
from kupol.dome.gate import Cmd, Expect, Gate
from kupol.dome.state import DomeState
from tests.dome_helpers import catalog, set_path, synchronous_state_from as state_from

CFG = load_dome_config(Path(__file__).resolve().parents[1] / "dome.yaml")


class Rig:
    """Gate + поддельный клиент + управляемые часы."""

    def __init__(self, tmp_path: Path, st: DomeState, can_control: bool = True, dry_run: bool = False) -> None:
        self.sent: list[tuple[str, str, Any]] = []
        self.reply: dict[str, Any] | Exception = {"success": True, "mode": "virtual"}
        self.t = 1000.0
        self.log = AgentLog(tmp_path, run_id="t")
        self.state = st
        self.cat = catalog()
        self.gate = Gate(CFG, st, lambda: self.cat, self.log, self._send, can_control=can_control, dry_run=dry_run, clock=lambda: self.t)
        self.decision = self.log.write("decision", situation="тест")

    async def _send(self, node: str, action: str, value: Any) -> dict[str, Any]:
        self.sent.append((node, action, value))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def go(self, node: str, action: str, value: Any = None, **kw: Any) -> Any:
        kw.setdefault("ref", self.decision)
        return asyncio.run(self.gate.submit(Cmd(node, action, value, **kw)))

    def records(self, kind: str) -> list[dict[str, Any]]:
        self.log._fh.flush()
        rows = [json.loads(x) for x in self.log.path.read_text(encoding="utf-8").splitlines()]
        return [r for r in rows if r["kind"] == kind]


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path, state_from("quiet"))


# --------------------------------------------------------------------------- отказы до сети


def test_command_without_decision_ref_is_rejected(rig: Rig) -> None:
    r = rig.go("printer_3d_01", "home", ref=None)
    assert r.status == "rejected" and "ref" in r.reason and not rig.sent


def test_guest_role_cannot_control(rig: Rig) -> None:
    rig.state.role = "guest"
    assert rig.go("printer_3d_01", "home").status == "rejected" and not rig.sent


def test_action_missing_in_catalog_and_operator_only(rig: Rig) -> None:
    assert "недоступно" in rig.go("printer_3d_01", "pause").reason
    assert "Оператора" in rig.go("printer_3d_01", "remove_part").reason
    assert "недоступно" in rig.go("no_such_node", "x").reason


@pytest.mark.parametrize(
    "node,action,value",
    [
        ("dome_automation_01", "block_interlock", {"id": "IL_SMOKE", "minutes": 10}),
        ("fire_suppression_01", "manual_start_zone", "FABLAB"),
        ("fire_suppression_01", "block_automation", None),
        ("access_control_01", "lockdown", True),
        ("access_control_01", "unlock_door", "SERVER_ROOM"),
        ("solar_inverter_01", "start_cell_balancing", None),
        ("printer_3d_01", "upload_file", {"file_name": "x.gcode", "content": "G1"}),
    ],
)
def test_dangerous_actions_never_reach_the_network(rig: Rig, node: str, action: str, value: Any) -> None:
    r = rig.go(node, action, value)
    assert r.status == "rejected" and "не разрешено политикой" in r.reason
    assert not rig.sent


def test_value_validation(rig: Rig) -> None:
    assert "диапазон" in rig.go("supply_ventilation_01", "set_speed", 5).reason
    assert rig.go("supply_ventilation_01", "set_speed", 100).status == "sent"
    assert "номер линии" in rig.go("smart_panel_01", "line_off", 9).reason
    assert "допустимо" in rig.go("solar_inverter_01", "set_fan", "MAX").reason
    assert "не ожидается" in rig.go("printer_3d_01", "home", 1).reason


def test_alias_is_resolved_to_main_name(rig: Rig) -> None:
    rig.state.nodes["printer_3d_01"].update({"state": "IDLE", "files": ["a.gcode"], "bed_clear": True, "hold_level": None})
    assert rig.go("printer_3d_01", "start_print", "a.gcode").status == "sent"
    assert rig.sent[-1] == ("printer_3d_01", "start_job", "a.gcode")


# --------------------------------------------------------------------------- режимы


def test_read_only_mode_logs_dry_run_and_sends_nothing(tmp_path: Path) -> None:
    rig = Rig(tmp_path, state_from("quiet"), can_control=False)
    r = rig.go("smart_panel_01", "line_off", 8)
    assert r.status == "dry_run" and not rig.sent
    act = rig.records("action")[-1]
    assert act["dry_run"] is True and act["ref"] == rig.decision and act["request"]["action"] == "line_off"
    assert rig.gate.stats["dry_run"] == 1


def test_explicit_dry_run_beats_allow_control(tmp_path: Path) -> None:
    rig = Rig(tmp_path, state_from("quiet"), can_control=True, dry_run=True)
    assert rig.go("smart_panel_01", "line_off", 8).status == "dry_run" and not rig.sent


# --------------------------------------------------------------------------- проверка эффекта


def test_verification_ok_and_failure_with_cooldown(rig: Rig) -> None:
    exp = Expect.equals("smart_panel_01.lines[7].state", "OFF")
    r = rig.go("smart_panel_01", "line_off", 8, expect=exp)
    assert r.status == "sent" and rig.gate.is_pending("smart_panel_01", "line_off")
    # такая же команда, пока результат не проверен, не повторяется
    assert "ожидает проверки" in rig.go("smart_panel_01", "line_off", 8).reason
    assert rig.gate.check_pending() == []  # ещё ждём
    rig.t += 25  # время вышло, линия не выключилась
    done = rig.gate.check_pending()
    assert len(done) == 1
    ver = rig.records("verification")[-1]
    assert ver["ok"] is False and ver["ref"] == r.action_seq and "OFF" in ver["expected"]
    assert "не дала эффекта" in rig.go("smart_panel_01", "line_off", 8).reason
    rig.t += 130  # пауза после «нет эффекта» прошла
    assert rig.go("smart_panel_01", "line_off", 8).ok  # запрет повтора снят, окно дребезга тоже прошло


def test_verification_success_when_state_changes(rig: Rig) -> None:
    r = rig.go("smart_panel_01", "line_off", 8, expect=Expect.equals("smart_panel_01.lines[7].state", "OFF"))
    set_path(rig.state, "smart_panel_01.lines[7].state", "OFF")
    nodes = __import__("copy").deepcopy(rig.state.nodes)
    nodes["smart_panel_01"]["updated_at"] += 1
    rig.state.update({"nodes": nodes}, "team", rig.t + 1)
    rig.gate.check_pending()
    ver = rig.records("verification")[-1]
    assert ver["ok"] is True and ver["ref"] == r.action_seq
    assert rig.gate.stats["verified"] == 1


def test_timeout_leaves_command_pending_not_repeated(rig: Rig) -> None:
    rig.reply = RequestTimeout("нет ответа")
    r = rig.go("smart_panel_01", "line_off", 7, expect=Expect.equals("smart_panel_01.lines[6].state", "OFF"))
    assert r.status == "failed" and rig.gate.is_pending("smart_panel_01", "line_off")
    assert "ожидает проверки" in rig.go("smart_panel_01", "line_off", 7).reason


def test_server_error_is_logged_as_failed(rig: Rig) -> None:
    rig.reply = {"success": False, "error": "Нет связи с узлом"}
    r = rig.go("smart_panel_01", "line_on", 8)
    assert r.status == "failed" and rig.records("action")[-1]["response"]["error"] == "Нет связи с узлом"


# --------------------------------------------------------------------------- анти-дребезг


def test_line_chatter_limit(rig: Rig) -> None:
    assert rig.go("smart_panel_01", "line_off", 6).ok
    rig.t += 5
    assert rig.go("smart_panel_01", "line_on", 6).ok
    rig.t += 5
    r = rig.go("smart_panel_01", "line_off", 6)
    assert r.status == "rejected" and "анти-дребезг" in r.reason
    rig.t += 130
    assert rig.go("smart_panel_01", "line_off", 6).ok


def test_locked_line_is_not_switched(rig: Rig) -> None:
    set_path(rig.state, "smart_panel_01.lines[7].lockout_remaining_s", 200.0)
    set_path(rig.state, "smart_panel_01.lines[7].lockout_reason", "CHATTER")
    assert "заблокирована" in rig.go("smart_panel_01", "line_on", 8).reason


# --------------------------------------------------------------------------- resume


def test_resume_refused_while_interlock_tripped_and_allowed_after(tmp_path: Path) -> None:
    rig = Rig(tmp_path, state_from("co2_tripped"))
    assert "IL_CO2" in rig.go("cnc_01", "resume").reason
    set_path(rig.state, "dome_automation_01.interlocks.IL_CO2.state", "ARMED")
    assert rig.go("cnc_01", "resume").status == "sent"
    rig.t += 5
    assert rig.go("printer_3d_01", "resume").ok  # у каждого станка свой счётчик попыток


def test_resume_one_attempt_per_cause(tmp_path: Path) -> None:
    rig = Rig(tmp_path, state_from("co2_tripped"))
    set_path(rig.state, "dome_automation_01.interlocks.IL_CO2.state", "ARMED")
    assert rig.go("cnc_01", "resume").ok
    rig.t += 60
    r = rig.go("cnc_01", "resume")
    assert r.status == "rejected" and "уже пробовали" in r.reason


def test_resume_blocked_for_alarm_operator_causes_and_unknown(tmp_path: Path) -> None:
    rig = Rig(tmp_path, state_from("co2_tripped"))
    set_path(rig.state, "cnc_01.hold_level", "ALARM")
    assert "ALARM" in rig.go("cnc_01", "resume").reason
    set_path(rig.state, "cnc_01.hold_level", "WARNING")
    set_path(rig.state, "cnc_01.hold_reason", "BED_COLLISION")
    assert "Оператором" in rig.go("cnc_01", "resume").reason
    set_path(rig.state, "cnc_01.hold_reason", "WEIRD_CODE")
    assert "не распознана" in rig.go("cnc_01", "resume").reason
    set_path(rig.state, "cnc_01.state", "RUNNING")
    set_path(rig.state, "cnc_01.paused", False)
    assert "не на паузе" in rig.go("cnc_01", "resume").reason


def test_resume_blocked_when_server_counted_two_attempts(tmp_path: Path) -> None:
    rig = Rig(tmp_path, state_from("co2_tripped"))
    set_path(rig.state, "dome_automation_01.interlocks.IL_CO2.state", "ARMED")
    set_path(rig.state, "cnc_01.resume_attempts", 2)
    assert "ALARM на 20 минут" in rig.go("cnc_01", "resume").reason


# --------------------------------------------------------------------------- start_job и бокс


def test_start_job_rules(rig: Rig) -> None:
    printer = rig.state.nodes["printer_3d_01"]
    printer.update({"state": "COMPLETED", "files": ["gear_m1_z24.gcode"], "bed_clear": False, "hold_level": None})
    assert "BED_COLLISION" in rig.go("printer_3d_01", "start_job", "gear_m1_z24.gcode").reason
    printer["bed_clear"] = True
    assert "нет в списке" in rig.go("printer_3d_01", "start_job", "other.gcode").reason
    printer["state"] = "PRINTING"
    assert "занят" in rig.go("printer_3d_01", "start_job", "gear_m1_z24.gcode").reason
    printer["state"] = "COMPLETED"
    assert rig.go("printer_3d_01", "start_job", {"file_name": "gear_m1_z24.gcode"}).status == "sent"


def test_box_not_unlocked_while_printing_or_hot(tmp_path: Path) -> None:
    rig = Rig(tmp_path, state_from("printer_done"))  # webhook врёт COMPLETED, на деле идёт печать
    assert "печатает" in rig.go("thermal_insulation_01", "set_lock", {"lock": 0, "locked": False}).reason
    rig.state.nodes["printer_3d_01"].update({"state": "COMPLETED", "real_state": "COMPLETED", "bed_temp_c": 60.0})
    assert "горячий" in rig.go("thermal_insulation_01", "set_lock", {"lock": 0, "locked": False}).reason
    rig.state.nodes["printer_3d_01"]["bed_temp_c"] = 25.0
    assert rig.go("thermal_insulation_01", "set_lock", {"lock": 0, "locked": False}).ok
    assert rig.go("thermal_insulation_01", "set_lock", {"lock": 0, "locked": True}).ok


# --------------------------------------------------------------------------- доверие к датчикам


def test_last_trusted_co2_sensor_cannot_be_marked(rig: Rig) -> None:
    trust = {"climate_sensor_01.co2_ppm": "UNRELIABLE", "climate_sensor_02.co2_ppm": "UNRELIABLE"}
    set_path(rig.state, "dome_automation_01.data_trust", trust)
    r = rig.go("dome_automation_01", "set_data_trust", {"node": "climate_sensor_03", "param": "co2_ppm", "trust": "UNRELIABLE"})
    assert "последний доверенный" in r.reason
    assert rig.go("dome_automation_01", "set_data_trust", {"node": "climate_sensor_01", "param": "co2_ppm", "trust": "TRUSTED"}).ok


def test_primary_sensor_must_be_trusted(rig: Rig) -> None:
    set_path(rig.state, "dome_automation_01.data_trust", {"climate_sensor_02.co2_ppm": "UNRELIABLE"})
    assert "недостоверным" in rig.go("dome_automation_01", "set_primary_sensor", "climate_sensor_02").reason
    assert rig.go("dome_automation_01", "set_primary_sensor", "climate_sensor_03").ok


# --------------------------------------------------------------------------- чат


def test_chat_rules(rig: Rig) -> None:
    ok = lambda text, **kw: rig.go("meeting_01", "post_message", {"text": text, **kw})  # noqa: E731
    assert "пустое" in ok(" ").reason
    assert "Оператору" in ok("привет", to="team3").reason
    assert "длиннее" in ok("x" * 2000).reason
    for i in range(4):
        assert ok(f"m{i}", to="operator").ok
    assert "лимит чата" in ok("пятое").reason
    rig.t += 11
    assert ok("снова").ok
