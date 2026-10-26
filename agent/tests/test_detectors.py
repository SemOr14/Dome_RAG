"""Тесты детекторов на синтетических данных и на симуляторе."""

from __future__ import annotations

from datetime import timedelta

import pytest

from kupol.adapter.scenario import load_scenario
from kupol.adapter.simulator import SimulatorAdapter
from kupol.core.config import AppConfig
from kupol.core.detectors import (
    AlertTracker,
    DetectorSuite,
    FreezeDetector,
    JumpDetector,
    StaleDetector,
    ThresholdDetector,
    TrendDetector,
    device_errors,
    vent_required,
)
from kupol.core.models import Alert, AlertLevel
from kupol.core.state import State
from tests.conftest import ROOT, T0, make_telemetry


def state_of(*snaps: object) -> State:
    st = State()
    for s in snaps:
        st.update(s)  # type: ignore[arg-type]
    return st


def one(config: AppConfig, **kw: object) -> State:
    return state_of(make_telemetry(0, **kw))


# --------------------------------------------------------------------------- порог


@pytest.mark.parametrize(
    ("kw", "field", "level"),
    [
        ({"temp_c": 29.0}, "temp_c", AlertLevel.WARNING),
        ({"temp_c": 34.0}, "temp_c", AlertLevel.CRITICAL),
        ({"temp_c": 14.0}, "temp_c", AlertLevel.WARNING),
        ({"temp_c": 9.0}, "temp_c", AlertLevel.CRITICAL),
        ({"battery_soc_pct": 30.0}, "battery_soc_pct", AlertLevel.WARNING),
        ({"battery_soc_pct": 24.0}, "battery_soc_pct", AlertLevel.CRITICAL),
        ({"co2_ppm": {"co2_a": 1200.0, "co2_b": 600.0}}, "co2_ppm.co2_a", AlertLevel.WARNING),
        ({"co2_ppm": {"co2_a": 600.0, "co2_b": 2500.0}}, "co2_ppm.co2_b", AlertLevel.CRITICAL),
        ({"humidity_pct": 85.0}, "humidity_pct", AlertLevel.CRITICAL),
        ({"battery_temp_c": 41.0}, "battery_temp_c", AlertLevel.WARNING),
    ],
)
def test_threshold_levels(config: AppConfig, kw: dict[str, object], field: str, level: AlertLevel) -> None:
    alerts = ThresholdDetector(config).detect(one(config, **kw), T0)
    assert [(a.field, a.level) for a in alerts] == [(field, level)]
    assert alerts[0].kind == "threshold" and alerts[0].value is not None


def test_threshold_quiet_on_normal(config: AppConfig) -> None:
    assert ThresholdDetector(config).detect(one(config), T0) == []
    assert ThresholdDetector(config).detect(State(), T0) == []


def test_threshold_boundary_is_inclusive_for_critical(config: AppConfig) -> None:
    a = ThresholdDetector(config).detect(one(config, temp_c=33.0), T0)
    assert a[0].level is AlertLevel.CRITICAL


# --------------------------------------------------------------------------- тренд


def ramp(rate_per_min: float, minutes: float = 6, field: str = "battery_temp_c", start: float = 25.0) -> State:
    st = State()
    for i in range(int(minutes * 60) + 1):
        st.update(make_telemetry(i, **{field: start + rate_per_min * i / 60.0}))
    return st


def test_trend_detects_drift(config: AppConfig) -> None:
    alerts = TrendDetector(config).detect(ramp(0.6), T0)
    assert [(a.field, a.kind, a.level) for a in alerts] == [("battery_temp_c", "trend", AlertLevel.WARNING)]
    assert alerts[0].details["slope_per_min"] == pytest.approx(0.6, rel=1e-3)


def test_trend_detects_falling(config: AppConfig) -> None:
    assert TrendDetector(config).detect(ramp(-0.6), T0)


def test_trend_ignores_slow_change(config: AppConfig) -> None:
    assert TrendDetector(config).detect(ramp(0.1), T0) == []


def test_trend_needs_enough_history(config: AppConfig) -> None:
    assert TrendDetector(config).detect(ramp(2.0, minutes=1), T0) == []


def test_trend_fires_before_critical_threshold(config: AppConfig) -> None:
    """Дрейф 0.8 °C/мин должен быть замечен задолго до crit_high=55 °C."""
    st = State()
    detected_at = crit_at = None
    for i in range(0, 3600):
        value = 25.0 + 0.8 * i / 60.0
        st.update(make_telemetry(i, battery_temp_c=value))
        if detected_at is None and TrendDetector(config).detect(st, T0):
            detected_at = i
        if crit_at is None and value >= 55:
            crit_at = i
    assert detected_at is not None and crit_at is not None
    assert detected_at < crit_at - 600  # минимум на 10 минут раньше


# --------------------------------------------------------------------------- скачок


def test_jump_absolute_on_false_co2(config: AppConfig) -> None:
    st = state_of(make_telemetry(0), make_telemetry(1, co2_ppm={"co2_a": 600.0, "co2_b": 5000.0}))
    alerts = JumpDetector(config).detect(st, T0)
    assert [(a.field, a.kind) for a in alerts] == [("co2_ppm.co2_b", "jump")]
    assert alerts[0].details["previous"] == 610.0


def test_jump_relative_on_pv_drop(config: AppConfig) -> None:
    st = state_of(make_telemetry(0, pv_power_w=1800.0), make_telemetry(1, pv_power_w=600.0))
    alerts = JumpDetector(config).detect(st, T0)
    assert [a.field for a in alerts] == ["pv_power_w"]


def test_jump_relative_ignores_small_base(config: AppConfig) -> None:
    st = state_of(make_telemetry(0, pv_power_w=60.0), make_telemetry(1, pv_power_w=10.0))
    assert JumpDetector(config).detect(st, T0) == []


def test_jump_small_changes_ignored(config: AppConfig) -> None:
    st = state_of(make_telemetry(0), make_telemetry(1, co2_ppm={"co2_a": 650.0, "co2_b": 640.0}, pv_power_w=1400.0))
    assert JumpDetector(config).detect(st, T0) == []


def test_jump_needs_two_snapshots(config: AppConfig) -> None:
    assert JumpDetector(config).detect(one(config), T0) == []
    assert JumpDetector(config).detect(State(), T0) == []


# --------------------------------------------------------------------------- замерзание


def frozen_for(seconds: int, **const: object) -> State:
    st = State()
    for i in range(seconds + 1):
        kw: dict[str, object] = {"temp_c": 22.0 + (i % 7) * 0.01, "humidity_pct": 45.0 + (i % 5) * 0.01, "pv_power_w": 1500.0 + i % 11}
        kw["co2_ppm"] = {"co2_a": 600.0 + i % 3, "co2_b": 610.0}  # co2_b замерзает
        kw["battery_voltage_v"] = 52.0 + (i % 4) * 0.01
        kw["battery_temp_c"] = 25.0 + (i % 6) * 0.01
        kw["load_total_w"] = 900.0 + i % 9
        kw["battery_soc_pct"] = 70.0 - i * 0.001
        kw.update(const)
        st.update(make_telemetry(i, **kw))
    return st


def test_freeze_detected(config: AppConfig) -> None:
    alerts = FreezeDetector(config).detect(frozen_for(130), T0)
    assert [a.field for a in alerts] == ["co2_ppm.co2_b"]
    assert alerts[0].kind == "freeze" and alerts[0].details["unchanged_s"] > 120


def test_freeze_not_before_limit(config: AppConfig) -> None:
    assert FreezeDetector(config).detect(frozen_for(100), T0) == []


def test_freeze_ignores_full_battery(config: AppConfig) -> None:
    st = frozen_for(700, battery_soc_pct=100.0, co2_ppm={"co2_a": 600.0, "co2_b": 610.0})
    fields = {a.field for a in FreezeDetector(config).detect(st, T0)}
    assert "battery_soc_pct" not in fields


def test_freeze_flags_stuck_soc_below_full(config: AppConfig) -> None:
    st = frozen_for(700, battery_soc_pct=70.0)
    assert "battery_soc_pct" in {a.field for a in FreezeDetector(config).detect(st, T0)}


# --------------------------------------------------------------------------- устаревание и связь


def test_stale_by_declared_age(config: AppConfig) -> None:
    det = StaleDetector(config.detectors)
    assert det.detect(one(config, data_age_s=1.0), T0) == []
    assert det.detect(one(config, data_age_s=6.0), T0)[0].level is AlertLevel.WARNING
    a = det.detect(one(config, data_age_s=20.0), T0)[0]
    assert a.level is AlertLevel.CRITICAL and a.kind == "stale"


def test_stale_by_snapshot_age(config: AppConfig) -> None:
    det = StaleDetector(config.detectors)
    st = one(config)
    assert det.detect(st, T0 + timedelta(seconds=3)) == []
    assert det.detect(st, T0 + timedelta(seconds=8))[0].level is AlertLevel.WARNING
    assert det.detect(st, T0 + timedelta(seconds=30))[0].level is AlertLevel.CRITICAL


def test_comms_loss_levels(config: AppConfig) -> None:
    det = StaleDetector(config.detectors)
    early = det.detect(one(config, comms_ok=False, data_age_s=2.0), T0)[0]
    late = det.detect(one(config, comms_ok=False, data_age_s=12.0), T0)[0]
    assert early.kind == late.kind == "comms_loss"
    assert early.level is AlertLevel.WARNING and late.level is AlertLevel.CRITICAL


def test_stale_empty_state(config: AppConfig) -> None:
    det = StaleDetector(config.detectors)
    assert det.detect(State(), T0) == []
    assert det.staleness(State(), T0) == float("inf")


# --------------------------------------------------------------------------- безопасность


def test_vent_required_rule() -> None:
    assert vent_required(State(), T0) == []
    assert vent_required(one(None, solder_status="idle"), T0) == []  # type: ignore[arg-type]
    assert vent_required(one(None, solder_status="running", vent_mode="low"), T0) == []  # type: ignore[arg-type]
    a = vent_required(one(None, solder_status="running", vent_mode="off"), T0)  # type: ignore[arg-type]
    assert a[0].kind == "safety_vent_required" and a[0].level is AlertLevel.CRITICAL


def test_device_errors(config: AppConfig) -> None:
    fields = list(config.equipment.device_lines)
    assert device_errors(State(), T0, fields) == []
    assert device_errors(one(config), T0, fields) == []
    alerts = device_errors(one(config, printer_status="error", cnc_status="error"), T0, fields)
    assert sorted(a.field or "" for a in alerts) == ["cnc_status", "printer_status"]
    assert alerts[0].kind == "device_error" and alerts[0].level is AlertLevel.WARNING


def test_suite_reports_device_error(config: AppConfig) -> None:
    kinds = {a.kind for a in DetectorSuite(config).run(one(config, printer_status="error"), T0)}
    assert "device_error" in kinds


# --------------------------------------------------------------------------- набор


def test_suite_quiet_on_normal(config: AppConfig) -> None:
    assert DetectorSuite(config).run(one(config), T0) == []


def test_suite_suppresses_everything_during_comms_loss(config: AppConfig) -> None:
    st = one(config, comms_ok=False, data_age_s=20.0, temp_c=40.0, solder_status="running")
    alerts = DetectorSuite(config).run(st, T0)
    assert [a.kind for a in alerts] == ["comms_loss"]


def test_suite_combines(config: AppConfig) -> None:
    st = state_of(make_telemetry(0), make_telemetry(1, temp_c=35.0, solder_status="running"))
    kinds = {a.kind for a in DetectorSuite(config).run(st, T0)}
    assert {"threshold", "safety_vent_required"} <= kinds


@pytest.mark.slow
def test_suite_on_quiet_scenario_has_no_critical(config: AppConfig) -> None:
    """Критерий приёмки 4 на уровне детекторов: спокойная игра без ложных критических тревог."""
    sc = load_scenario(ROOT / "scenarios" / "quiet.yaml")
    sim = SimulatorAdapter(config, sc, seed=42)
    suite, st = DetectorSuite(config), State()
    seen: list[Alert] = []
    for _ in range(int(sc.duration_s)):
        sim.advance(1)
        st.update(sim.get_telemetry())
        seen.extend(suite.run(st, sim.now()))
    assert not [a for a in seen if a.level is AlertLevel.CRITICAL], seen[:3]
    assert seen == [], f"ожидалась полная тишина, но были: {[(a.kind, a.field) for a in seen[:5]]}"


# --------------------------------------------------------------------------- трекер


def alert(level: AlertLevel = AlertLevel.WARNING, kind: str = "threshold", field: str = "temp_c") -> Alert:
    return Alert(ts=T0, level=level, kind=kind, field=field, message="m")


def test_tracker_dedup_and_escalation() -> None:
    tr = AlertTracker(hold_s=60)
    assert len(tr.filter([alert()], T0)) == 1
    assert tr.filter([alert()], T0 + timedelta(seconds=5)) == []
    assert len(tr.filter([alert(AlertLevel.CRITICAL)], T0 + timedelta(seconds=6))) == 1  # эскалация
    assert tr.filter([alert(AlertLevel.WARNING)], T0 + timedelta(seconds=7)) == []  # понижение — не новая


def test_tracker_expiry_and_reappearance() -> None:
    tr = AlertTracker(hold_s=30)
    tr.filter([alert()], T0)
    assert tr.active(T0 + timedelta(seconds=10))
    assert tr.active(T0 + timedelta(seconds=31)) == []
    assert len(tr.filter([alert()], T0 + timedelta(seconds=40))) == 1


def test_tracker_continuous_alert_stays_active() -> None:
    tr = AlertTracker(hold_s=30)
    for i in range(0, 200, 10):
        tr.filter([alert()], T0 + timedelta(seconds=i))
    assert len(tr.active(T0 + timedelta(seconds=200))) == 1


def test_tracker_active_sorted_and_clear() -> None:
    tr = AlertTracker()
    tr.filter([alert(AlertLevel.WARNING, field="a"), alert(AlertLevel.CRITICAL, field="b")], T0)
    assert [a.field for a in tr.active(T0)] == ["b", "a"]
    tr.clear_key("threshold:b")
    assert [a.field for a in tr.active(T0)] == ["a"]
