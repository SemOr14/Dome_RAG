"""Плейбуки принтера, энергии, линий и устройств."""

from __future__ import annotations

from pathlib import Path

from kupol.dome.playbooks.devices import AlarmPlaybook, ClockPlaybook, NetworkPlaybook, StalePlaybook
from kupol.dome.playbooks.energy import EnergyPlaybook
from kupol.dome.playbooks.lines import OvercurrentPlaybook, PhantomLinePlaybook
from kupol.dome.playbooks.production import PrinterPlaybook
from tests.dome_helpers import make_ctx, set_path, state_from


def acts(intents: list) -> list[tuple[str, str, object]]:  # type: ignore[type-arg]
    return [(s.node, s.action, s.value) for i in intents for s in i.steps]


# --------------------------------------------------------------------------- принтер


def test_webhook_lie_is_logged_and_box_stays_locked(tmp_path: Path) -> None:
    out = PrinterPlaybook().step(make_ctx(state_from("printer_done"), tmp_path))
    assert [i.key for i in out] == ["printer:webhook_lie"] and not acts(out) and out[0].say is None


def test_unlocked_box_is_relocked_when_printing_starts(tmp_path: Path) -> None:
    st = state_from("printer_done")
    set_path(st, "thermal_insulation_01.locks[0]", "UNLOCKED")
    out = PrinterPlaybook().step(make_ctx(st, tmp_path))
    assert acts(out) == [("thermal_insulation_01", "set_lock", {"lock": 0, "locked": True})]


def test_real_completion_unlocks_box_once_bed_is_cool(tmp_path: Path) -> None:
    st = state_from("quiet")  # принтер COMPLETED, 100 %
    set_path(st, "printer_3d_01.bed_temp_c", 30.0)
    set_path(st, "printer_3d_01.bed_clear", False)
    mem = {"printing_file": "bracket_v2.gcode"}
    out = PrinterPlaybook().step(make_ctx(st, tmp_path, mem=mem))
    assert acts(out) == [("thermal_insulation_01", "set_lock", {"lock": 0, "locked": False})]
    assert "готова" in (out[0].say or "") and mem["announced"] == "bracket_v2.gcode"
    assert PrinterPlaybook().step(make_ctx(st, tmp_path, mem=mem)) == []  # второй раз не повторяем


def test_hot_bed_postpones_unlock(tmp_path: Path) -> None:
    st = state_from("quiet")
    set_path(st, "printer_3d_01.bed_temp_c", 58.0)
    mem = {"printing_file": "bracket_v2.gcode"}
    out = PrinterPlaybook().step(make_ctx(st, tmp_path, mem=mem))
    assert not acts(out) and "announced" not in mem


def test_box_relocked_after_part_removed(tmp_path: Path) -> None:
    st = state_from("quiet")
    set_path(st, "thermal_insulation_01.locks[0]", "UNLOCKED")
    set_path(st, "printer_3d_01.bed_clear", True)
    out = PrinterPlaybook().step(make_ctx(st, tmp_path, mem={"announced": "bracket_v2.gcode", "printing_file": "bracket_v2.gcode"}))
    assert acts(out) == [("thermal_insulation_01", "set_lock", {"lock": 0, "locked": True})]


# --------------------------------------------------------------------------- энергия


def test_low_soc_in_hybrid_switches_to_grid_only(tmp_path: Path) -> None:
    st = state_from("quiet", **{"solar_inverter_01.mode": "HYBRID", "solar_inverter_01.battery_soc_pct": 34.0})
    mem: dict = {}
    out = EnergyPlaybook().step(make_ctx(st, tmp_path, mem=mem))
    assert acts(out) == [("solar_inverter_01", "set_mode_grid_only", None)] and "GRID_ONLY" in (out[0].say or "")
    assert EnergyPlaybook().step(make_ctx(st, tmp_path, now=1100.0, mem=mem)) == []  # не чаще раза в 10 минут


def test_high_soc_with_sun_returns_to_hybrid(tmp_path: Path) -> None:
    st = state_from("quiet", **{"solar_inverter_01.battery_soc_pct": 77.0, "solar_inverter_01.pv_power_w": 900.0})
    out = EnergyPlaybook().step(make_ctx(st, tmp_path))
    assert acts(out) == [("solar_inverter_01", "set_mode_hybrid", None)]
    st2 = state_from("quiet", **{"solar_inverter_01.battery_soc_pct": 77.0, "solar_inverter_01.pv_power_w": 10.0})  # ночью — остаёмся на сети
    assert acts(EnergyPlaybook().step(make_ctx(st2, tmp_path))) == []


def err_state(humidity: float | None, code: str = "E09"):  # type: ignore[no-untyped-def]
    st = state_from("quiet")
    st.nodes["solar_inverter_01"].update({"error": True, "error_code": code, "error_text": "ISOLATION_FAULT", "mppt_state": "OFF"})
    set_path(st, "weather_station_01.humidity_pct", humidity)
    return st


def test_e09_requires_insulation_check_even_after_dry(tmp_path: Path) -> None:
    mem: dict = {}
    fog = err_state(86.0)
    out = EnergyPlaybook().step(make_ctx(fog, tmp_path, now=1000.0, mem=mem))
    assert [i.key for i in out] == ["energy:e09_wait"] and not acts(out)
    still_fog = EnergyPlaybook().step(make_ctx(fog, tmp_path, now=2500.0, mem=mem))
    assert not acts(still_fog)
    dry = err_state(60.0)
    assert not acts(EnergyPlaybook().step(make_ctx(dry, tmp_path, now=1200.0, mem={"error_since": 1100.0})))  # мало ждали
    out = EnergyPlaybook().step(make_ctx(dry, tmp_path, now=2500.0, mem={"error_since": 1000.0}))
    assert not acts(out) and out[0].say


def test_e09_gives_up_after_repeats_and_other_errors_are_not_reset(tmp_path: Path) -> None:
    dry = err_state(50.0)
    mem = {"error_since": 0.0, "resets": [2000.0, 3000.0 - 1000.0, 2500.0]}
    out = EnergyPlaybook().step(make_ctx(dry, tmp_path, now=3600.0, mem=mem))
    assert [i.key for i in out] == ["energy:e09_wait"] and not acts(out)
    other = EnergyPlaybook().step(make_ctx(err_state(50.0, "E07"), tmp_path))
    assert not acts(other) and other[0].say


def test_soc_recovery_does_not_authorize_load_shed_reset(tmp_path: Path) -> None:
    low = state_from("quiet", **{"dome_automation_01.interlocks.IL_LOAD_SHED.state": "TRIPPED", "solar_inverter_01.battery_soc_pct": 22.0})
    assert not acts(EnergyPlaybook().step(make_ctx(low, tmp_path)))
    ok = state_from("quiet", **{"dome_automation_01.interlocks.IL_LOAD_SHED.state": "TRIPPED", "solar_inverter_01.battery_soc_pct": 41.0})
    out = EnergyPlaybook().step(make_ctx(ok, tmp_path))
    assert not acts(out) and out[0].human_escalation["required_action"]


def test_mppt_enabled_in_daylight(tmp_path: Path) -> None:
    st = state_from("quiet", **{"weather_station_01.illuminance_lux": 40000, "solar_panels_01.all_mppt_enabled": False})
    assert acts(EnergyPlaybook().step(make_ctx(st, tmp_path))) == [("solar_panels_01", "enable_all_mppt", None)]


# --------------------------------------------------------------------------- линии


def test_phantom_line_is_switched_off_after_confirmation(tmp_path: Path) -> None:
    st = state_from("quiet")  # линия 8: 52 Вт, ON
    mem: dict = {}
    assert PhantomLinePlaybook().step(make_ctx(st, tmp_path, now=1000.0, mem=mem)) == []
    out = PhantomLinePlaybook().step(make_ctx(st, tmp_path, now=1130.0, mem=mem))
    assert acts(out) == [("smart_panel_01", "line_off", 8)] and "Осмотрите" in (out[0].say or "")


def test_phantom_timer_resets_when_power_drops(tmp_path: Path) -> None:
    st = state_from("quiet")
    mem: dict = {}
    PhantomLinePlaybook().step(make_ctx(st, tmp_path, now=1000.0, mem=mem))
    set_path(st, "smart_panel_01.lines[7].power_w", 1.0)
    PhantomLinePlaybook().step(make_ctx(st, tmp_path, now=1050.0, mem=mem))
    assert "since:8" not in mem


def test_overcurrent_never_reset_by_elapsed_time(tmp_path: Path) -> None:
    st = state_from("quiet")
    st.nodes["dome_automation_01"]["interlocks"]["IL_OVERCURRENT"].update({"state": "TRIPPED", "shed_lines": [1]})
    mem: dict = {}
    assert not acts(OvercurrentPlaybook().step(make_ctx(st, tmp_path, now=1000.0, mem=mem)))
    out = OvercurrentPlaybook().step(make_ctx(st, tmp_path, now=1200.0, mem=mem))
    assert not acts(out) and out[0].human_escalation["required_action"]
    again = OvercurrentPlaybook().step(make_ctx(st, tmp_path, now=1500.0, mem=mem))
    assert not acts(again) and again[0].human_escalation["required_action"]


def test_tripped_line_protection_is_not_reset(tmp_path: Path) -> None:
    st = state_from("quiet", **{"smart_panel_01.lines[3].state": "RCD_TRIP", "smart_panel_01.lines[3].protection_state": "RCD_TRIPPED"})
    out = OvercurrentPlaybook().step(make_ctx(st, tmp_path))
    assert not acts(out) and "Сам не сбрасываю" in (out[0].say or "")


# --------------------------------------------------------------------------- устройства


def test_alarm_requires_inspection_not_reset_or_automatic_restart(tmp_path: Path) -> None:
    st = state_from("quiet", **{"supply_ventilation_01.alarm": True, "supply_ventilation_01.alarm_code": "FAN_MOTOR", "supply_ventilation_01.state": "FAULT"})
    mem: dict = {}
    out = AlarmPlaybook().step(make_ctx(st, tmp_path, now=1000.0, mem=mem))
    assert not acts(out) and out[0].human_escalation["required_action"]
    set_path(st, "supply_ventilation_01.alarm", False)
    set_path(st, "supply_ventilation_01.state", "OFF")
    out2 = AlarmPlaybook().step(make_ctx(st, tmp_path, now=1010.0, mem=mem))
    assert not acts(out2)


def test_fume_motor_alarm_asks_operator_first(tmp_path: Path) -> None:
    st = state_from("quiet", **{"fume_extraction_01.alarm": True, "fume_extraction_01.alarm_code": "MOTOR_OVERHEAT"})
    out = AlarmPlaybook().step(make_ctx(st, tmp_path))
    assert not acts(out) and "фильтр" in out[0].human_escalation["required_action"]


def test_alarm_giveup_after_repeated_resets(tmp_path: Path) -> None:
    st = state_from("quiet", **{"water_pump_01.alarm": True, "water_pump_01.alarm_code": "DRY_RUN"})
    mem = {"water_pump_01": [100.0, 500.0, 900.0]}
    out = AlarmPlaybook().step(make_ctx(st, tmp_path, now=1500.0, mem=mem))
    assert not acts(out) and out[0].human_escalation["node_id"] == "water_pump_01"


def test_stale_node_gets_limited_reconnects(tmp_path: Path) -> None:
    st = state_from("quiet")
    st.nodes["radio_01"]["status"] = "UNKNOWN"
    mem: dict = {}
    out = StalePlaybook().step(make_ctx(st, tmp_path, now=1000.0, mem=mem))
    assert acts(out) == [("radio_01", "reconnect", None)]
    assert StalePlaybook().step(make_ctx(st, tmp_path, now=1100.0, mem=mem)) == []  # пауза 5 минут


def test_clock_and_network(tmp_path: Path) -> None:
    st = state_from("quiet", **{"edge_compute_01.clock_offset_s": 7.5})
    assert acts(ClockPlaybook().step(make_ctx(st, tmp_path))) == [("edge_compute_01", "sync_time", None)]
    net = state_from("quiet", **{"network_link_01.link_state": "DOWN"})
    mem: dict = {}
    assert NetworkPlaybook().step(make_ctx(net, tmp_path, now=1000.0, mem=mem)) == []
    out = NetworkPlaybook().step(make_ctx(net, tmp_path, now=1040.0, mem=mem))
    assert out and not acts(out) and "резерв" in (out[0].say or "")
