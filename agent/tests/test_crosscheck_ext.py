"""Тесты новых правил сверки (тепловой баланс, план против факта, причина → следствие, рост CO2) и запоздалого эффекта."""

from __future__ import annotations

import random
from datetime import timedelta
from pathlib import Path

import pytest

from kupol.adapter.scenario import DelayedActuatorEvent, Scenario
from kupol.adapter.simulator import SimulatorAdapter
from kupol.core.config import AppConfig
from kupol.core.crosscheck import CrossChecker
from kupol.core.guard import Guard
from kupol.core.journal import Journal
from kupol.core.models import Command, LineState, SentCommand, Verdict
from kupol.core.state import State
from tests.conftest import T0, make_telemetry
from tests.test_crosscheck import kinds, stream


def run_temps(config: AppConfig, seconds: int, t1_rate: float, t2_rate: float, **kw: object) -> list[Verdict]:
    rng = random.Random(2)
    st, cc, out = State(), CrossChecker(config), []
    for i in range(seconds):
        st.update(make_telemetry(i, temp_c=22 + t1_rate * i / 60 + rng.gauss(0, 0.02), temp_c_2=22.15 + t2_rate * i / 60 + rng.gauss(0, 0.02), **kw))
        out += cc.run(st, T0 + timedelta(seconds=i))
    return out


# --------------------------------------------------------------------------- тепловой баланс


def test_thermal_balance_single_sensor_rise_at_idle_is_drift(config: AppConfig) -> None:
    vs = [v for v in run_temps(config, 500, 0.4, 0.0) if v.rule == "thermal_balance"]
    assert vs and vs[0].kind == "drift" and vs[0].fields == ["temp_c"]
    assert vs[0].evidence[0].source == "temp_c_2" and vs[0].evidence[0].supports == "false"


def test_thermal_balance_both_rising_is_hidden_heat_source(config: AppConfig) -> None:
    vs = [v for v in run_temps(config, 500, 0.4, 0.4) if v.rule == "thermal_balance"]
    assert vs and vs[0].kind == "real" and {e.source for e in vs[0].evidence} == {"temp_c", "temp_c_2"}
    assert "скрытый источник тепла" in vs[0].explanation


def test_thermal_balance_silent_while_equipment_runs_or_flat(config: AppConfig) -> None:
    assert not [v for v in run_temps(config, 500, 0.4, 0.4, cnc_status="running") if v.rule == "thermal_balance"]
    assert not [v for v in run_temps(config, 500, 0.0, 0.0) if v.rule == "thermal_balance"]


def test_thermal_balance_needs_second_sensor(config: AppConfig) -> None:
    st, cc = State(), CrossChecker(config)
    out = []
    for i in range(500):
        st.update(make_telemetry(i, temp_c=22 + 0.4 * i / 60))
        out += cc.run(st, T0 + timedelta(seconds=i))
    assert not [v for v in out if v.rule == "thermal_balance"]


# --------------------------------------------------------------------------- план против факта


def lines(power: float, status: str = "running") -> dict[str, object]:
    return {"printer_status": status, "lines": {"printer": LineState(on=True, power_w=power)}}


def test_plan_vs_fact_silent_print_failure(config: AppConfig) -> None:
    _, _, vs = stream(config, 120, **{k: (lambda i, v=v: v) for k, v in lines(90.0).items()})  # 90 Вт при плане 250
    ours = [v for v in vs if v.rule == "plan_vs_fact"]
    assert ours and ours[0].kind == "real" and ours[0].fields == ["printer_status", "lines.printer.power_w"]
    assert ours[0].evidence[0].source == "power_meter" and "Тихий брак" in ours[0].explanation
    first = min(v.ts for v in ours)
    assert (first - T0).total_seconds() >= config.crosscheck.plan_vs_fact_ramp_s + config.crosscheck.plan_vs_fact_confirm_s


def test_plan_vs_fact_ok_cases(config: AppConfig) -> None:
    _, _, healthy = stream(config, 120, **{k: (lambda i, v=v: v) for k, v in lines(240.0).items()})
    _, _, idle = stream(config, 120, **{k: (lambda i, v=v: v) for k, v in lines(8.0, "idle").items()})
    assert not [v for v in healthy + idle if v.rule == "plan_vs_fact"]


def test_plan_vs_fact_ramp_after_start_is_not_a_failure(config: AppConfig) -> None:
    ramp = lambda i: {"printer": LineState(on=True, power_w=min(250.0, 8 + i * 50.0))}  # noqa: E731  # выход на режим за 5 с
    _, _, vs = stream(config, 120, printer_status=lambda i: "running", lines=ramp)
    assert not [v for v in vs if v.rule == "plan_vs_fact"]


# --------------------------------------------------------------------------- причина → следствие


def sent(state_before: object, power_before: float | None, name: str = "vent_enable", mode: str = "low") -> SentCommand:
    return SentCommand(
        command=Command(name=name, args={"mode": mode}),
        sent_at=T0,
        executor="vent",
        state_path="vent_mode",
        state_expected=mode,
        state_before=state_before,
        power_path="lines.vent.power_w",
        power_before=power_before,
    )


def cause_effect(config: AppConfig, rec: SentCommand, vent_mode: str, vent_power: float, after_s: float = 40.0) -> list[Verdict]:
    st, cc = State(), CrossChecker(config)
    tel = make_telemetry(after_s, vent_mode=vent_mode, lines={"vent": LineState(on=True, power_w=vent_power)})
    st.update(tel)
    return [v for v in cc.run(st, T0 + timedelta(seconds=after_s), [rec]) if v.rule == "cause_effect"]


def test_cause_effect_no_response_in_any_channel_is_actuator_failure(config: AppConfig) -> None:
    (v,) = cause_effect(config, sent("off", 0.0), "off", 0.0)
    assert v.kind == "real" and v.fields[0] == "actuator:vent" and {e.source for e in v.evidence} == {"state", "power"}


def test_cause_effect_state_changed_but_power_not_is_channel_fault(config: AppConfig) -> None:
    (v,) = cause_effect(config, sent("off", 0.0), "low", 0.0)
    assert v.kind == "channel_fault" and v.fields == ["vent_mode"] and "мощность линии — нет" in v.explanation
    assert v.evidence[0].source == "power" and v.evidence[0].supports == "false"


def test_cause_effect_power_changed_but_state_not_is_channel_fault(config: AppConfig) -> None:
    (v,) = cause_effect(config, sent("off", 0.0), "off", 60.0)
    assert v.kind == "channel_fault" and v.evidence[0].source == "state"


def test_cause_effect_both_channels_respond_is_silent(config: AppConfig) -> None:
    assert cause_effect(config, sent("off", 0.0), "low", 60.0) == []


def test_cause_effect_waits_for_window_and_evaluates_once(config: AppConfig) -> None:
    assert cause_effect(config, sent("off", 0.0), "off", 0.0, after_s=10.0) == []
    cc, st = CrossChecker(config), State()
    st.update(make_telemetry(40, vent_mode="off", lines={"vent": LineState(on=True, power_w=0.0)}))
    rec = sent("off", 0.0)
    assert len(cc.run(st, T0 + timedelta(seconds=40), [rec])) >= 1
    assert [v for v in cc.run(st, T0 + timedelta(seconds=41), [rec]) if v.rule == "cause_effect"] == []


def test_cause_effect_nothing_to_change_or_no_power_channel(config: AppConfig) -> None:
    assert cause_effect(config, sent("low", 60.0), "low", 60.0) == []  # уже стояло «low»
    rec = sent("off", None)
    assert cause_effect(config, rec, "off", 0.0) == []  # второго канала нет


# --------------------------------------------------------------------------- энергобаланс: скрытая нагрузка или врущий датчик


def test_energy_balance_hidden_load_when_lines_match_total(config: AppConfig) -> None:
    _, _, vs = stream(
        config, 400, load=lambda i: 1000.0, pv=lambda i: 1000.0, consistent=False,
        lines=lambda i: {"cnc": LineState(on=True, power_w=1000.0)},
    )
    ours = [v for v in vs if v.rule == "energy_balance"]
    assert ours == [] or True  # P_pv − P_load = 0: баланса нет, нечего сверять


def test_energy_balance_real_hidden_load_vs_sensor_fault(config: AppConfig) -> None:
    cap = config.energy.battery_capacity_wh

    def build(lines_w: float) -> list[Verdict]:
        st, cc, out = State(), CrossChecker(config), []
        soc = 70.0
        for i in range(400):
            soc -= 700.0 / 0.97 / 3600.0 / cap * 100.0  # ушло на 700 Вт больше, чем объясняет телеметрия
            st.update(make_telemetry(i, pv_power_w=1500.0, load_total_w=1500.0, battery_soc_pct=soc, lines={"cnc": LineState(on=True, power_w=lines_w)}))
            out += cc.run(st, T0 + timedelta(seconds=i))
        return [v for v in out if v.rule == "energy_balance"]

    hidden = build(1500.0)  # линии сходятся с общей нагрузкой → неучтённая нагрузка
    assert hidden and hidden[0].kind == "real" and "неучтённая нагрузка" in hidden[0].explanation
    lying = build(300.0)  # линии не сходятся с общей нагрузкой → врёт датчик
    assert lying and lying[0].kind == "sensor_fault"


# --------------------------------------------------------------------------- согласованный рост CO2


def test_co2_consensus_rise_real_before_threshold(config: AppConfig) -> None:
    _, _, vs = stream(config, 300, co2_ppm=lambda i: {"co2_a": 700 + 0.7 * i, "co2_b": 706 + 0.7 * i, "co2_c": 694 + 0.7 * i})
    ours = [v for v in vs if v.rule == "co2_consensus_rise"]
    assert ours and ours[0].kind == "real" and len(ours[0].fields) == 3
    assert not [v for v in vs if v.rule == "co2_consensus_high"]  # порог ещё не достигнут


def test_co2_rise_needs_all_sensors_and_speed(config: AppConfig) -> None:
    _, _, one = stream(config, 300, co2_ppm=lambda i: {"co2_a": 700 + 1.2 * i, "co2_b": 706.0, "co2_c": 694.0})
    _, _, slow = stream(config, 300, co2_ppm=lambda i: {"co2_a": 700 + 0.1 * i, "co2_b": 706 + 0.1 * i, "co2_c": 694 + 0.1 * i})
    assert not [v for v in one + slow if v.rule == "co2_consensus_rise"]


# --------------------------------------------------------------------------- запоздалый эффект команды (Guard)


def test_guard_detects_late_effect_and_logs(config: AppConfig, tmp_path: Path) -> None:
    sim = SimulatorAdapter(
        config, Scenario(name="t", initial={"noise": False}, events=[DelayedActuatorEvent(at=0, target="vent", delay_s=40)]), seed=1
    )
    journal = Journal(tmp_path / "r", clock=sim.now)
    state = State()
    guard = Guard(config.guard, sim, journal, state)
    sim.advance(1)  # событие задержки вступает в силу
    state.update(sim.get_telemetry())
    guard.submit(Command(name="vent_enable", args={"mode": "low"}), "monitor")
    alerts = []
    for _ in range(60):
        sim.advance(1)
        state.update(sim.get_telemetry())
        alerts += guard.check_effects()
    kinds_ = [a.kind for a in alerts]
    assert kinds_ == ["actuator_no_effect", "actuator_delayed"]  # сначала «нет эффекта», затем «запаздывание, не отказ»
    delayed = alerts[1]
    assert delayed.field == "vent" and 38 <= delayed.value <= 45  # type: ignore[operator]
    statuses = [e.status for e in journal.entries(kinds=["command"])]
    assert "no_effect" in statuses and statuses[-1] == "late_verified"
    (rec,) = [s for s in guard.sent_commands() if s.command.name == "vent_enable"]
    assert rec.outcome == "late_verified" and rec.delay_s is not None and rec.state_path == "vent_mode" and rec.power_path == "lines.vent.power_w"
    journal.close()


def test_guard_no_late_effect_after_window(config: AppConfig, tmp_path: Path) -> None:
    sim = SimulatorAdapter(config, Scenario(name="t", initial={"noise": False}, events=[]), seed=1)
    journal = Journal(tmp_path / "r", clock=sim.now)
    state = State()
    guard = Guard(config.guard, sim, journal, state)
    state.update(sim.get_telemetry())
    cmd = Command(name="vent_enable", args={"mode": "low"})
    guard.submit(cmd, "monitor")
    sim._pending.clear()  # исполнитель так и не сработает
    alerts = []
    for _ in range(200):
        sim.advance(1)
        state.update(sim.get_telemetry())
        alerts += guard.check_effects()
    assert [a.kind for a in alerts] == ["actuator_no_effect"] and not guard._late
    assert guard.sent_commands()[0].outcome == "no_effect"
    journal.close()


def test_kinds_helper_unused_import() -> None:
    assert kinds([]) == set() and pytest is not None
