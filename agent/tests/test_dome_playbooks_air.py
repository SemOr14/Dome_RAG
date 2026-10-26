"""Плейбуки CO₂, конденсата, продолжения станков и дыма на снимках из журнала 14.10."""

from __future__ import annotations

from pathlib import Path

from kupol.dome.playbooks.air import Co2Playbook, CondensatePlaybook
from kupol.dome.playbooks.machines import ResumePlaybook, SmokePlaybook
from tests.dome_helpers import make_ctx, seq_state, set_path, state_from


def actions(intents: list) -> list[tuple[str, str, object]]:  # type: ignore[type-arg]
    return [(s.node, s.action, s.value) for i in intents for s in i.steps]


# --------------------------------------------------------------------------- CO2


def test_spike_on_primary_is_handled_before_interlock_trips(tmp_path: Path) -> None:
    """Главный выигрыш: IL_CO2 ещё ARMED (идёт отсчёт 56 с) — помечаем датчик и меняем основной, станки не встают."""
    st = seq_state("quiet", "co2_pending")
    ctx = make_ctx(st, tmp_path)
    out = Co2Playbook().step(ctx)
    assert len(out) == 1
    i = out[0]
    assert actions(out) == [
        ("dome_automation_01", "set_data_trust", {"node": "climate_sensor_01", "param": "co2_ppm", "trust": "UNRELIABLE"}),
        ("dome_automation_01", "set_primary_sensor", "climate_sensor_02"),
    ]
    assert i.confidence >= 0.9 and i.priority == 2 and i.say and "Ложный CO₂" in i.say and "не останавливаю" in i.say
    assert any(e.get("node") == "climate_sensor_02" for e in i.evidence)


def test_after_marking_nothing_more_to_do(tmp_path: Path) -> None:
    st = seq_state("co2_after")  # доверие уже снято, основной — 02
    assert Co2Playbook().step(make_ctx(st, tmp_path)) == []


def test_tripped_interlock_with_false_primary_is_reset(tmp_path: Path) -> None:
    st = seq_state("co2_tripped")  # IL_CO2 TRIPPED, основной всё ещё датчик 01, доверие не снято
    out = Co2Playbook().step(make_ctx(st, tmp_path))
    kinds = [a[1] for a in actions(out)]
    assert "set_data_trust" in kinds and "set_primary_sensor" in kinds
    # сброс интерлока ждёт, пока датчик не помечен: 01 всё ещё «живой» и высокий
    assert "reset_interlock" not in kinds
    marked = seq_state("co2_tripped")
    set_path(marked, "dome_automation_01.data_trust", {"climate_sensor_01.co2_ppm": "UNRELIABLE"})
    set_path(marked, "dome_automation_01.primary_co2_sensor", "climate_sensor_02")
    out2 = Co2Playbook().step(make_ctx(marked, tmp_path))
    assert actions(out2) == [("dome_automation_01", "reset_interlock", "IL_CO2")]


def test_real_co2_ventilates_and_does_not_reset(tmp_path: Path) -> None:
    st = state_from("quiet")
    for s, v in (("climate_sensor_01", 2100), ("climate_sensor_02", 1900), ("climate_sensor_03", 1950)):
        set_path(st, f"{s}.co2_ppm", v)
    set_path(st, "dome_automation_01.interlocks.IL_CO2.state", "TRIPPED")
    out = Co2Playbook().step(make_ctx(st, tmp_path))
    acts = actions(out)
    assert ("supply_ventilation_01", "set_speed", 100) in acts and ("supply_ventilation_01", "set_mode", "FRESH_AIR") in acts
    assert not any(a[1] == "reset_interlock" for a in acts)
    assert out[0].priority == 1 and "реально" in (out[0].say or "")


def test_trust_is_restored_only_after_long_agreement(tmp_path: Path) -> None:
    st = state_from("co2_after")
    set_path(st, "climate_sensor_01.co2_ppm", 790)  # датчик пришёл в норму
    mem: dict = {}
    assert Co2Playbook().step(make_ctx(st, tmp_path, now=1000.0, mem=mem)) == []
    assert Co2Playbook().step(make_ctx(st, tmp_path, now=1300.0, mem=mem)) == []
    out = Co2Playbook().step(make_ctx(st, tmp_path, now=1700.0, mem=mem))
    assert actions(out) == [("dome_automation_01", "set_data_trust", {"node": "climate_sensor_01", "param": "co2_ppm", "trust": "TRUSTED"})]
    # снова разошёлся — отсчёт сбрасывается
    set_path(st, "climate_sensor_01.co2_ppm", 3000)
    Co2Playbook().step(make_ctx(st, tmp_path, now=1800.0, mem=mem))
    assert "climate_sensor_01" not in mem["agree_since"]


# --------------------------------------------------------------------------- конденсат


def test_false_condensate_marks_humidity_and_dew_point(tmp_path: Path) -> None:
    st = state_from("quiet", **{"climate_sensor_01.humidity_pct": 99.0})
    out = CondensatePlaybook().step(make_ctx(st, tmp_path))
    params = [a[2]["param"] for a in actions(out)]  # type: ignore[index]
    assert params == ["humidity_pct", "dew_point_c"] and out[0].priority == 2


def test_real_condensate_is_reported_not_reset(tmp_path: Path) -> None:
    st = state_from("quiet")
    for s in ("climate_sensor_01", "climate_sensor_02", "climate_sensor_03"):
        set_path(st, f"{s}.humidity_pct", 95.0)
    set_path(st, "dome_automation_01.interlocks.IL_CONDENSATE.state", "TRIPPED")
    out = CondensatePlaybook().step(make_ctx(st, tmp_path))
    assert not any(a[1] == "reset_interlock" for a in actions(out)) and out and "риск конденсата" in (out[0].say or "")


# --------------------------------------------------------------------------- продолжение станков


def test_resume_machines_after_interlock_is_armed_again(tmp_path: Path) -> None:
    st = seq_state("co2_tripped")
    set_path(st, "dome_automation_01.interlocks.IL_CO2.state", "ARMED")
    set_path(st, "dome_automation_01.interlocks.IL_CO2.pending_s", 0.0)
    set_path(st, "dome_automation_01.data_trust", {"climate_sensor_01.co2_ppm": "UNRELIABLE"})
    mem: dict = {}
    assert ResumePlaybook().step(make_ctx(st, tmp_path, now=1000.0, mem=mem)) == []  # выдерживаем паузу «причина снята»
    out = ResumePlaybook().step(make_ctx(st, tmp_path, now=1010.0, mem=mem))
    assert sorted(actions(out)) == [("cnc_01", "resume", None), ("printer_3d_01", "resume", None)]


def test_no_resume_while_cause_is_real(tmp_path: Path) -> None:
    st = state_from("co2_tripped")
    set_path(st, "dome_automation_01.interlocks.IL_CO2.state", "ARMED")
    set_path(st, "dome_automation_01.interlocks.IL_CO2.pending_s", 0.0)
    for s in ("climate_sensor_01", "climate_sensor_02", "climate_sensor_03"):
        set_path(st, f"{s}.co2_ppm", 1900)  # реальный высокий CO₂
    mem: dict = {}
    ResumePlaybook().step(make_ctx(st, tmp_path, now=1000.0, mem=mem))
    assert ResumePlaybook().step(make_ctx(st, tmp_path, now=1020.0, mem=mem)) == []


def test_alarm_and_operator_causes_only_notify(tmp_path: Path) -> None:
    st = state_from("co2_tripped")
    set_path(st, "cnc_01.hold_level", "ALARM")
    set_path(st, "cnc_01.lockout_remaining_s", 900.0)
    set_path(st, "printer_3d_01.hold_reason", "FILAMENT_RUNOUT")
    out = ResumePlaybook().step(make_ctx(st, tmp_path))
    assert not actions(out) and len(out) == 2 and all(i.say for i in out)


def test_operator_pause_without_reason_is_left_alone(tmp_path: Path) -> None:
    st = state_from("co2_tripped")
    set_path(st, "cnc_01.hold_reason", None)
    set_path(st, "printer_3d_01.hold_reason", None)
    assert ResumePlaybook().step(make_ctx(st, tmp_path)) == []


# --------------------------------------------------------------------------- дым


def smoke_state(**quiet: object):  # type: ignore[no-untyped-def]
    st = state_from("quiet")
    st.nodes["smoke_detector_01"].update({"smoke_detected": True, "alarm_state": "ALARM"})
    st.nodes["dome_automation_01"]["events"] = [
        {"event_id": "AUT-0007", "interlock": "IL_SMOKE", "type": "TRIP", "acknowledged": False}
    ]
    return st


def test_false_smoke_is_acknowledged(tmp_path: Path) -> None:
    out = SmokePlaybook().step(make_ctx(smoke_state(), tmp_path))
    assert actions(out) == [
        ("dome_automation_01", "acknowledge", {"event_id": "AUT-0007"}),
        ("smoke_detector_01", "reset_alarm", None),
    ]
    assert out[0].priority == 1


def test_real_signs_prevent_acknowledge(tmp_path: Path) -> None:
    st = smoke_state()
    set_path(st, "air_quality_sensor_01.co_ppm", 35.0)
    out = SmokePlaybook().step(make_ctx(st, tmp_path))
    assert not actions(out) and "пожаротушение сработает штатно" in (out[0].say or "")


def test_operator_says_false_alarm_counts_as_evidence(tmp_path: Path) -> None:
    st = smoke_state()
    for p in ("smoke_detector_01.obscuration_pct", "air_quality_sensor_01.co_ppm", "air_quality_sensor_01.voc_index"):
        set_path(st, p, None)
    assert not actions(SmokePlaybook().step(make_ctx(st, tmp_path)))
    out = SmokePlaybook().step(make_ctx(st, tmp_path, texts=["Дыма нет, это ложная тревога"]))
    assert ("dome_automation_01", "acknowledge", {"event_id": "AUT-0007"}) in actions(out)


def test_smoke_interlock_reset_after_quiet(tmp_path: Path) -> None:
    st = state_from("quiet")
    set_path(st, "dome_automation_01.interlocks.IL_SMOKE.state", "TRIPPED")
    out = SmokePlaybook().step(make_ctx(st, tmp_path, now=1100.0, mem={"false_confirmed_at": 1000.0}))
    assert actions(out) == [("dome_automation_01", "reset_interlock", "IL_SMOKE")]
    assert SmokePlaybook().step(make_ctx(st, tmp_path, now=2000.0, mem={"false_confirmed_at": 1000.0})) == []
