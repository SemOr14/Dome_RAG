"""Тесты энергобаланса и планировщика."""

from __future__ import annotations

from datetime import timedelta

import pytest

from kupol.adapter.scenario import load_scenario
from kupol.adapter.simulator import SimulatorAdapter
from kupol.core.config import AppConfig, Operation
from kupol.core.energy import (
    EnergyPlanner,
    EnergyStatus,
    OpStatus,
    ProcessTracker,
    energy_alert,
    energy_status,
    priority_class,
    t_left_minutes,
)
from kupol.core.models import AlertLevel, LineState
from kupol.core.state import State
from tests.conftest import ROOT, T0, make_telemetry


def feed(soc: float, pv: float, load: float, seconds: int = 90, **kw: object) -> State:
    st = State()
    for i in range(seconds):
        st.update(make_telemetry(i, battery_soc_pct=soc, pv_power_w=pv, load_total_w=load, **kw))
    return st


def status_of(config: AppConfig, soc: float, pv: float, load: float, **kw: object) -> tuple[State, EnergyStatus]:
    st = feed(soc, pv, load, **kw)
    result = energy_status(st, config)
    assert result is not None
    return st, result


# --------------------------------------------------------------------------- формула


def test_t_left_formula() -> None:
    assert t_left_minutes(1000.0, 1500.0, 500.0) == pytest.approx(60.0)
    assert t_left_minutes(1000.0, 500.0, 500.0) is None
    assert t_left_minutes(1000.0, 400.0, 500.0) is None
    assert t_left_minutes(0.0, 1500.0, 500.0) == 0.0
    assert t_left_minutes(-5.0, 1500.0, 500.0) == 0.0


def test_energy_status_values(config: AppConfig) -> None:
    # 4000 Вт·ч * (50−20)% = 1200 Вт·ч; дефицит 1000 Вт → 72 мин
    _, s = status_of(config, 50.0, 500.0, 1500.0)
    assert s.available_wh == pytest.approx(1200.0)
    assert s.t_left_min == pytest.approx(72.0)
    assert s.net_w == pytest.approx(-1000.0) and s.level is None


@pytest.mark.parametrize(
    ("soc", "level"),
    [(24.0, AlertLevel.CRITICAL), (30.0, AlertLevel.WARNING), (60.0, None)],  # дефицит 1000 Вт: 9.6 / 24 / 96 мин
)
def test_energy_levels(config: AppConfig, soc: float, level: AlertLevel | None) -> None:
    _, s = status_of(config, soc, 500.0, 1500.0)
    assert s.level is level


def test_energy_status_charging_has_no_t_left(config: AppConfig) -> None:
    _, s = status_of(config, 50.0, 1800.0, 600.0)
    assert s.t_left_min is None and s.level is None and s.net_w > 0


def test_energy_status_below_minimum(config: AppConfig) -> None:
    _, s = status_of(config, 15.0, 100.0, 900.0)
    assert s.available_wh < 0 and s.t_left_min == 0.0 and s.level is AlertLevel.CRITICAL


def test_energy_status_empty_state(config: AppConfig) -> None:
    assert energy_status(State(), config) is None


def test_energy_alert(config: AppConfig) -> None:
    assert energy_alert(None, T0) is None
    _, ok = status_of(config, 80.0, 1500.0, 1000.0)
    assert energy_alert(ok, T0) is None
    _, crit = status_of(config, 25.0, 300.0, 1500.0)
    alert = energy_alert(crit, T0)
    assert alert is not None and alert.kind == "energy_t_left" and alert.level is AlertLevel.CRITICAL
    assert "мин" in alert.message


# --------------------------------------------------------------------------- приоритеты


def test_priority_classes(config: AppConfig) -> None:
    ops = {o.name: o for o in config.process.operations}
    assert priority_class(ops["Пайка компонентов"]) == 0  # вытяжка → безопасность
    assert priority_class(ops["Фрезеровка платы"]) == 1  # неприрываемая
    assert priority_class(ops["Измерения и тесты"]) == 2


# --------------------------------------------------------------------------- отслеживание этапов


def run_tracker(config: AppConfig, plan: list[tuple[int, int, dict[str, object]]]) -> ProcessTracker:
    """plan: (с, до_с, статусы) — применяется на каждом тике интервала."""
    tr = ProcessTracker(config)
    for start, end, kw in plan:
        for t in range(start, end):
            tr.update(make_telemetry(t, **kw))
    return tr


def test_tracker_marks_done_after_full_run(config: AppConfig) -> None:
    tr = run_tracker(config, [(0, 12 * 60 + 1, {"cnc_status": "running"}), (12 * 60 + 1, 12 * 60 + 5, {})])
    assert tr.status["Фрезеровка платы"] is OpStatus.DONE
    assert tr.status["Печать корпуса"] is OpStatus.PENDING


def test_tracker_interrupted_returns_to_pending(config: AppConfig) -> None:
    tr = run_tracker(config, [(0, 120, {"cnc_status": "running"}), (120, 125, {})])
    assert tr.status["Фрезеровка платы"] is OpStatus.PENDING
    assert tr.interrupted["Фрезеровка платы"] == 1


def test_tracker_blocked_on_error_and_recovers(config: AppConfig) -> None:
    tr = run_tracker(config, [(0, 60, {"printer_status": "running"}), (60, 65, {"printer_status": "error"})])
    assert tr.status["Печать корпуса"] is OpStatus.BLOCKED
    tr.update(make_telemetry(70))
    assert tr.status["Печать корпуса"] is OpStatus.PENDING


def test_tracker_ignores_stale_data(config: AppConfig) -> None:
    tr = ProcessTracker(config)
    tr.update(make_telemetry(0, cnc_status="running", comms_ok=False))
    assert tr.status["Фрезеровка платы"] is OpStatus.PENDING


def test_tracker_manual_marks_and_ready(config: AppConfig) -> None:
    tr = ProcessTracker(config)
    assert [o.name for o in tr.ready()] == ["Фрезеровка платы", "Печать корпуса"]
    tr.mark_done("Фрезеровка платы")
    assert "Пайка компонентов" in [o.name for o in tr.ready()]
    tr.mark_pending("Фрезеровка платы")
    assert "Пайка компонентов" not in [o.name for o in tr.ready()]
    with pytest.raises(KeyError):
        tr.mark_done("нет такого")
    with pytest.raises(KeyError):
        tr.mark_pending("нет такого")


def test_tracker_operations_filter(config: AppConfig) -> None:
    tr = ProcessTracker(config)
    tr.mark_done("Сборка в корпус")
    assert [o.name for o in tr.operations(OpStatus.DONE)] == ["Сборка в корпус"]
    assert len(tr.operations(OpStatus.PENDING, OpStatus.DONE)) == len(config.process.operations)


# --------------------------------------------------------------------------- «что если»


def op_named(config: AppConfig, name: str) -> Operation:
    return next(o for o in config.process.operations if o.name == name)


def test_can_start_ok(config: AppConfig) -> None:
    _, s = status_of(config, 80.0, 1800.0, 600.0)
    tel = make_telemetry(0, pv_power_w=1800.0, load_total_w=600.0, battery_soc_pct=80.0)
    ok, reason = EnergyPlanner(config).can_start(op_named(config, "Фрезеровка платы"), tel, s)
    assert ok and "энергии хватит" in reason


def test_can_start_insufficient_energy(config: AppConfig) -> None:
    _, s = status_of(config, 24.0, 100.0, 1000.0)
    tel = make_telemetry(0, pv_power_w=100.0, load_total_w=1000.0, battery_soc_pct=24.0)
    ok, reason = EnergyPlanner(config).can_start(op_named(config, "Фрезеровка платы"), tel, s)
    assert not ok and "не хватит энергии" in reason and "Вт·ч" in reason


def test_can_start_inverter_limit(config: AppConfig) -> None:
    _, s = status_of(config, 90.0, 2000.0, 2700.0)
    tel = make_telemetry(0, pv_power_w=2000.0, load_total_w=2700.0, battery_soc_pct=90.0)
    ok, reason = EnergyPlanner(config).can_start(op_named(config, "Фрезеровка платы"), tel, s)
    assert not ok and "инвертор" in reason


def test_can_start_accounts_for_vent(config: AppConfig) -> None:
    solder = op_named(config, "Пайка компонентов")
    _, s = status_of(config, 80.0, 1500.0, 1000.0)
    tel_off = make_telemetry(0, lines={"vent": LineState(on=True, power_w=0.0)})
    ok, reason = EnergyPlanner(config).can_start(solder, tel_off, s)
    assert ok and "вытяжку +60 Вт" in reason
    tel_on = make_telemetry(0, lines={"vent": LineState(on=True, power_w=60.0)})
    assert "вытяжку" not in EnergyPlanner(config).can_start(solder, tel_on, s)[1]


def test_can_start_refuses_solder_without_working_vent(config: AppConfig) -> None:
    _, s = status_of(config, 80.0, 1500.0, 1000.0)
    ok, reason = EnergyPlanner(config).can_start(
        op_named(config, "Пайка компонентов"), make_telemetry(0), s, vent_ok=False
    )
    assert not ok and "безопасность" in reason
    # а для этапа без вытяжки неисправная вытяжка не мешает
    assert EnergyPlanner(config).can_start(op_named(config, "Фрезеровка платы"), make_telemetry(0), s, vent_ok=False)[0]


def test_can_start_without_comms(config: AppConfig) -> None:
    _, s = status_of(config, 80.0, 1500.0, 1000.0)
    ok, reason = EnergyPlanner(config).can_start(
        op_named(config, "Фрезеровка платы"), make_telemetry(0, comms_ok=False), s
    )
    assert not ok and "достоверных" in reason


# --------------------------------------------------------------------------- порядок


def test_order_puts_ready_and_safety_first(config: AppConfig) -> None:
    _, s = status_of(config, 80.0, 1800.0, 600.0)
    tr = ProcessTracker(config)
    plan = EnergyPlanner(config).order(tr, make_telemetry(0), s)
    names = [p.operation.name for p in plan]
    # готовые: фрезеровка (неприрываемая) и печать (неприрываемая); остальные ждут зависимости
    assert names[:2] == ["Фрезеровка платы", "Печать корпуса"]
    assert all(p.can_start for p in plan[:2])
    waiting = [p for p in plan if not p.can_start]
    assert any("ждёт завершения" in p.reason for p in waiting)
    tr.mark_done("Фрезеровка платы")
    plan2 = EnergyPlanner(config).order(tr, make_telemetry(0), s)
    assert plan2[0].operation.name == "Пайка компонентов" and plan2[0].priority == 0  # безопасность первой среди готовых


# --------------------------------------------------------------------------- что отложить


def shortage(config: AppConfig) -> tuple[EnergyStatus, object]:
    lines = {
        "humidifier": LineState(on=True, power_w=80.0),
        "lighting": LineState(on=True, power_w=100.0),
        "aux": LineState(on=True, power_w=80.0),
        "vent": LineState(on=True, power_w=60.0),
        "cnc": LineState(on=True, power_w=600.0),
    }
    tel_kw = dict(humidifier_mode="high", lines=lines, humidity_pct=50.0, pv_power_w=300.0, load_total_w=1250.0, battery_soc_pct=27.0)
    st = feed(27.0, 300.0, 1250.0, humidifier_mode="high", lines=lines)
    s = energy_status(st, config)
    assert s is not None and s.t_left_min is not None and s.t_left_min < 30
    return s, make_telemetry(0, **tel_kw)


def test_no_advice_when_energy_is_fine(config: AppConfig) -> None:
    _, s = status_of(config, 80.0, 1500.0, 1000.0)
    assert EnergyPlanner(config).shedding_advice(make_telemetry(0), s) == []


def test_shedding_order_and_commands(config: AppConfig) -> None:
    s, tel = shortage(config)
    advice = EnergyPlanner(config).shedding_advice(tel, s, ProcessTracker(config))  # type: ignore[arg-type]
    assert advice, "при нехватке энергии должны быть советы"
    assert advice[0].command is not None and advice[0].command.name == "set_humidifier_mode"
    assert advice[0].command.args == {"mode": "off"}
    assert advice[1].command is not None and advice[1].command.name == "set_power_mode"
    texts = " ".join(a.text for a in advice)
    # безопасность и неприрываемые этапы не трогаем никогда
    for protected in ("Пайка", "Фрезеровка", "Печать"):
        assert protected not in texts
    # запас времени после советов не уменьшается
    afters = [a.t_left_after_min for a in advice if a.t_left_after_min is not None]
    assert afters == sorted(afters)


def test_shedding_operator_instructions_when_commands_not_enough(config: AppConfig) -> None:
    lines = {"humidifier": LineState(on=False, power_w=0.0), "lighting": LineState(on=False, power_w=0.0)}
    st = feed(21.0, 100.0, 1500.0, lines=lines)
    s = energy_status(st, config)
    assert s is not None
    advice = EnergyPlanner(config).shedding_advice(make_telemetry(0, lines=lines), s, ProcessTracker(config))
    assert advice and all(a.command is None for a in advice)
    assert "Оператор" in advice[0].text and "Измерения" in advice[0].text


def test_shedding_stops_once_target_reached(config: AppConfig) -> None:
    lines = {"humidifier": LineState(on=True, power_w=500.0)}
    st = feed(27.0, 300.0, 1000.0, humidifier_mode="high", lines=lines)
    s = energy_status(st, config)
    assert s is not None and s.t_left_min is not None and s.t_left_min < 30
    advice = EnergyPlanner(config).shedding_advice(make_telemetry(0, humidifier_mode="high", lines=lines, humidity_pct=50), s)
    assert len(advice) == 1  # одного шага достаточно, дальше не советуем
    after = advice[0].t_left_after_min
    assert after is None or after > 30


# --------------------------------------------------------------------------- на симуляторе


def test_cascade_heavy_reaches_energy_alert_and_advice(config: AppConfig) -> None:
    sc = load_scenario(ROOT / "scenarios" / "cascade_heavy.yaml")
    sim, st, tr = SimulatorAdapter(config, sc, seed=42), State(), None
    planner = EnergyPlanner(config)
    tr = ProcessTracker(config)
    first_warn = first_crit = None
    advised = False
    for _ in range(int(sc.duration_s)):
        sim.advance(1)
        tel = sim.get_telemetry()
        st.update(tel)
        tr.update(tel)
        s = energy_status(st, config)
        if s is None or s.level is None:
            continue
        if first_warn is None:
            first_warn = sim.t
        if s.level is AlertLevel.CRITICAL and first_crit is None:
            first_crit = sim.t
        advised = advised or bool(planner.shedding_advice(tel, s, tr))
    assert first_warn is not None and first_crit is not None and first_warn < first_crit
    assert advised
    # прошедшее время: тревога не должна приходить мгновенно в начале игры
    assert first_warn > 60 and timedelta(seconds=first_warn) < timedelta(minutes=60)
