"""Тесты сверки источников."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kupol.adapter.scenario import load_scenario
from kupol.adapter.simulator import SimulatorAdapter
from kupol.core.config import AppConfig
from kupol.core.crosscheck import CrossChecker, balance_residual
from kupol.core.models import LineState, Verdict
from kupol.core.state import State
from tests.conftest import ROOT, T0, make_telemetry
from datetime import timedelta


def stream(
    config: AppConfig,
    seconds: int,
    pv: Callable[[int], float] = lambda i: 1500.0,
    load: Callable[[int], float] = lambda i: 1500.0,
    soc0: float = 70.0,
    consistent: bool = True,
    **per_tick: Callable[[int], object],
) -> tuple[State, CrossChecker, list[Verdict]]:
    """Прогнать синтетический поток; SoC интегрируется по P_pv−P_load, если ``consistent``."""
    cap = config.energy.battery_capacity_wh
    st, cc, verdicts = State(), CrossChecker(config), []
    soc = soc0
    for i in range(seconds):
        net = pv(i) - load(i)
        if consistent:
            soc = min(100.0, max(0.0, soc + net / 3600.0 / cap * 100.0))
        kw = {k: f(i) for k, f in per_tick.items()}
        st.update(make_telemetry(i, pv_power_w=pv(i), load_total_w=load(i), battery_soc_pct=soc, **kw))
        verdicts += cc.run(st, T0 + timedelta(seconds=i))
    return st, cc, verdicts


def kinds(vs: list[Verdict], rule: str | None = None) -> set[str]:
    return {v.kind for v in vs if rule is None or v.rule == rule}


# --------------------------------------------------------------------------- CO2


def co2(i: int, b: float = 610.0, a: float = 600.0, c: float = 605.0) -> dict[str, float]:
    return {"co2_a": a, "co2_b": b, "co2_c": c}


def test_co2_abrupt_outlier_is_false_signal(config: AppConfig) -> None:
    _, _, vs = stream(config, 200, co2_ppm=lambda i: co2(i, b=5000.0 if i >= 150 else 610.0))
    ours = [v for v in vs if v.rule == "co2_outlier"]
    assert ours and {v.kind for v in ours} == {"false_signal"}
    assert ours[0].fields == ["co2_ppm.co2_b"] and ours[0].confidence >= 0.8
    assert "co2_b" in ours[0].explanation


def test_co2_gradual_outlier_is_sensor_fault(config: AppConfig) -> None:
    _, _, vs = stream(config, 340, co2_ppm=lambda i: co2(i, b=610.0 + i))
    assert "sensor_fault" in kinds(vs, "co2_outlier") and "false_signal" not in kinds(vs, "co2_outlier")


def test_co2_two_sensors_disagree_is_unknown(config: AppConfig) -> None:
    _, _, vs = stream(config, 20, co2_ppm=lambda i: {"co2_a": 600.0, "co2_b": 1600.0})
    assert kinds(vs, "co2_pair_disagreement") == {"unknown"}


def test_co2_consensus_high_is_real(config: AppConfig) -> None:
    _, _, vs = stream(config, 20, co2_ppm=lambda i: co2(i, a=1500.0, b=1520.0, c=1490.0))
    assert kinds(vs) == {"real"} and vs[0].rule == "co2_consensus_high"


def test_co2_two_sensors_agree_high_is_real(config: AppConfig) -> None:
    _, _, vs = stream(config, 5, co2_ppm=lambda i: {"co2_a": 1500.0, "co2_b": 1510.0})
    assert kinds(vs) == {"real"}


def test_co2_normal_gives_no_verdict(config: AppConfig) -> None:
    _, _, vs = stream(config, 30, co2_ppm=lambda i: co2(i))
    assert vs == []


def test_co2_single_sensor_context(config: AppConfig) -> None:
    spike = lambda i: {"co2_a": 5000.0 if i >= 20 else 600.0}  # noqa: E731
    _, _, vs = stream(config, 25, co2_ppm=spike)
    assert kinds(vs, "co2_single_context") == {"false_signal"}
    _, _, vs2 = stream(config, 25, co2_ppm=spike, vent_mode=lambda i: "low")
    assert kinds(vs2, "co2_single_context") == set()  # вытяжка включена — контекст не «спокойный»
    _, _, vs3 = stream(config, 25, co2_ppm=spike, temp_c=lambda i: 35.0)
    assert kinds(vs3, "co2_single_context") == set()


# --------------------------------------------------------------------------- энергобаланс


def test_balance_consistent_is_silent(config: AppConfig) -> None:
    _, _, vs = stream(config, 400, load=lambda i: 2500.0)
    assert kinds(vs, "energy_balance") == set()


def test_balance_mismatch_detected_after_confirmation(config: AppConfig) -> None:
    st, cc, vs = stream(config, 400, load=lambda i: 2500.0, consistent=False)
    ours = [v for v in vs if v.rule == "energy_balance"]
    assert ours and ours[0].kind == "sensor_fault"
    measured, expected = balance_residual(st, config)  # type: ignore[misc]
    assert abs(measured) < 5 and expected == pytest.approx(-1000.0)
    assert "медленнее" in ours[0].explanation  # SoC не падает, хотя P_pv−P_load = −1000 Вт
    # не сразу: нужно balance_window_s данных и balance_confirm_s устойчивости
    first_t = min(v.ts for v in ours)
    assert (first_t - T0).total_seconds() >= config.crosscheck.balance_confirm_s


def test_balance_skipped_when_battery_full(config: AppConfig) -> None:
    _, _, vs = stream(config, 300, soc0=100.0, consistent=False)  # избыток, но АКБ полна
    assert kinds(vs, "energy_balance") == set()


def test_balance_residual_none_without_data(config: AppConfig) -> None:
    assert balance_residual(State(), config) is None
    st = State()
    st.update(make_telemetry(0))
    assert balance_residual(st, config) is None


def test_balance_skipped_during_comms_loss(config: AppConfig) -> None:
    _, _, vs = stream(config, 300, load=lambda i: 2500.0, consistent=False, comms_ok=lambda i: False)
    assert vs == []


# --------------------------------------------------------------------------- устройства


def test_running_without_power_flagged_after_confirm(config: AppConfig) -> None:
    lines = lambda i: {"cnc": LineState(on=True, power_w=2.0)}  # noqa: E731
    _, _, vs = stream(config, 15, cnc_status=lambda i: "running", lines=lines)
    assert vs == []
    _, _, vs = stream(config, 40, cnc_status=lambda i: "running", lines=lines)
    ours = [v for v in vs if v.rule == "device_status_vs_power"]
    assert ours and ours[0].fields == ["cnc_status", "lines.cnc.power_w"]


def test_idle_with_high_power_flagged(config: AppConfig) -> None:
    lines = lambda i: {"cnc": LineState(on=True, power_w=500.0)}  # noqa: E731
    _, _, vs = stream(config, 40, cnc_status=lambda i: "idle", lines=lines)
    assert "работает без статуса" in [v for v in vs if v.rule == "device_status_vs_power"][0].explanation


def test_consistent_device_is_silent(config: AppConfig) -> None:
    lines = lambda i: {"cnc": LineState(on=True, power_w=600.0)}  # noqa: E731
    _, _, vs = stream(config, 60, cnc_status=lambda i: "running", lines=lines)
    assert vs == []


# --------------------------------------------------------------------------- температура АКБ


def test_battery_temp_rise_at_low_current_is_sensor_fault(config: AppConfig) -> None:
    _, _, vs = stream(config, 400, battery_temp_c=lambda i: 25.0 + 0.8 * i / 60.0)
    assert kinds(vs, "battery_temp_plausibility") == {"sensor_fault"}


def test_battery_temp_rise_at_high_current_is_real(config: AppConfig) -> None:
    _, _, vs = stream(
        config, 400, pv=lambda i: 3000.0, load=lambda i: 500.0, battery_temp_c=lambda i: 25.0 + 0.4 * i / 60.0
    )
    assert kinds(vs, "battery_temp_plausibility") == {"real"}


def test_battery_temp_flat_is_silent(config: AppConfig) -> None:
    _, _, vs = stream(config, 400)
    assert vs == []


# --------------------------------------------------------------------------- падение генерации


def test_pv_drop_confirmed_by_balance(config: AppConfig) -> None:
    _, _, vs = stream(config, 500, pv=lambda i: 1800.0 if i < 300 else 500.0)
    assert "real" in kinds(vs, "pv_drop")


def test_pv_drop_unconfirmed_when_balance_broken(config: AppConfig) -> None:
    _, _, vs = stream(config, 500, pv=lambda i: 1800.0 if i < 300 else 500.0, consistent=False)
    assert "unknown" in kinds(vs, "pv_drop")  # пока баланс не подтверждает, вердикт не «real»


def test_small_pv_change_is_silent(config: AppConfig) -> None:
    _, _, vs = stream(config, 500, pv=lambda i: 1800.0 if i < 300 else 1500.0)
    assert kinds(vs, "pv_drop") == set()


# --------------------------------------------------------------------------- общее


def test_no_verdicts_without_data_or_comms(config: AppConfig) -> None:
    cc = CrossChecker(config)
    assert cc.run(State(), T0) == []
    st = State()
    st.update(make_telemetry(0, comms_ok=False, co2_ppm={"co2_a": 600.0, "co2_b": 5000.0, "co2_c": 610.0}))
    assert cc.run(st, T0) == []


def run_scenario(config: AppConfig, name: str) -> list[Verdict]:
    sc = load_scenario(ROOT / "scenarios" / f"{name}.yaml")
    sim, st, cc = SimulatorAdapter(config, sc, seed=42), State(), CrossChecker(config)
    out: list[Verdict] = []
    for _ in range(int(sc.duration_s)):
        sim.advance(1)
        st.update(sim.get_telemetry())
        out += cc.run(st, sim.now())
    return out


def test_quiet_scenario_has_no_verdicts(config: AppConfig) -> None:
    assert run_scenario(config, "quiet") == []


def test_basic_scenario_verdicts(config: AppConfig) -> None:
    vs = run_scenario(config, "basic")
    false = [v for v in vs if v.kind == "false_signal"]
    assert false and 600 <= (false[0].ts - T0).total_seconds() <= 620  # ложный CO2 распознан сразу
    assert "sensor_fault" in kinds(vs, "battery_temp_plausibility")  # дрейф датчика АКБ
    assert "real" in kinds(vs, "pv_drop")
    assert "real" in kinds(vs, "co2_consensus_high")  # а реальный рост CO2 не спутан с ложным
    assert not [v for v in vs if v.rule == "energy_balance"]
