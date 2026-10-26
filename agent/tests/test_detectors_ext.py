"""Тесты расширенных детекторов (дополнение к ТЗ, разделы 5 и 13) на синтетических рядах."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from kupol.adapter.scenario import HiddenLoadEvent, Scenario
from kupol.adapter.simulator import SimulatorAdapter
from kupol.core.baseline import Baseline, ResidualSeries
from kupol.core.config import AppConfig
from kupol.core.detectors import (
    CusumDetector,
    DetectorSuite,
    FlatlineDetector,
    NoiseLossDetector,
    PairDiffDetector,
    RateLimitDetector,
    TimeMonotonicDetector,
    TimeToThresholdDetector,
    field_key,
    lookup_by_field,
)
from kupol.core.models import AlertLevel
from kupol.core.state import State
from tests.conftest import T0, make_telemetry

SIGMA_CO2, SIGMA_T = 4.0, 0.03


def gen(
    seconds: int,
    seed: int = 1,
    start: int = 0,
    co2_ramp: float = 0.0,
    temp_ramp: float = 0.0,
    drift_b: float = 0.0,  # дрейф co2_b, ppm/с
    drift_t2: float = 0.0,  # дрейф temp_c_2, °C/с
    sigma_co2: float = SIGMA_CO2,
    sigma_t: float = SIGMA_T,
    load_ramp: float = 0.0,
    **fixed: object,
):  # type: ignore[no-untyped-def]
    """Поток снимков: три датчика CO2 и два датчика температуры с известным шумом и смещениями."""
    rng = random.Random(seed)
    for i in range(start, start + seconds):
        k = i - start  # рост отсчитывается от начала куска: поток непрерывен с предыдущим
        base = 850.0 + co2_ramp * k
        temp = 22.0 + temp_ramp * k
        kw = dict(
            co2_ppm={
                "co2_a": base + rng.gauss(0, sigma_co2),
                "co2_b": base + 8 + drift_b * k + rng.gauss(0, sigma_co2),
                "co2_c": base - 6 + rng.gauss(0, sigma_co2),
            },
            temp_c=temp + rng.gauss(0, sigma_t),
            temp_c_2=temp + 0.15 + drift_t2 * k + rng.gauss(0, sigma_t),
            load_total_w=600.0 + load_ramp * k + rng.gauss(0, 3.0),
            humidity_pct=45.0 + rng.gauss(0, 0.15),
            pv_power_w=1500.0 + rng.gauss(0, 8.0),
            battery_soc_pct=80.0 - 0.001 * i + rng.gauss(0, 0.02),
            battery_voltage_v=52.0 + rng.gauss(0, 0.02),
            battery_temp_c=25.0 + rng.gauss(0, 0.05),
        )
        kw.update(fixed)
        yield make_telemetry(i, **kw)


def calibrated(config: AppConfig, seed: int = 1) -> tuple[Baseline, State]:
    base, st = Baseline(config), State()
    for snap in gen(305, seed=seed):
        st.update(snap)
        base.observe(st, ())
    assert base.fitted
    return base, st


def now_of(st: State) -> datetime:
    latest = st.latest()
    assert latest is not None
    return latest.ts


# --------------------------------------------------------------------------- скорость и время


def test_lookup_helpers() -> None:
    assert field_key("co2_ppm.co2_a") == "co2_ppm" and field_key("temp_c") == "temp_c"
    table = {"co2_ppm": 40.0, "temp_c_2": 0.5}
    assert lookup_by_field(table, "co2_ppm.co2_b") == 40.0 and lookup_by_field(table, "temp_c_2") == 0.5
    assert lookup_by_field(table, "humidity_pct") is None


def test_rate_limit_flags_impossible_jump_as_sensor_problem(config: AppConfig) -> None:
    """2: скачок быстрее физического предела — предположение о проблеме датчика, а не среды."""
    det, st = RateLimitDetector(config), State()
    st.update(make_telemetry(0, co2_ppm={"co2_a": 600.0, "co2_b": 610.0}))
    st.update(make_telemetry(1, co2_ppm={"co2_a": 5000.0, "co2_b": 612.0}))
    (a,) = det.detect(st, T0)
    assert a.kind == "rate_limit" and a.field == "co2_ppm.co2_a" and a.detector == "rate_limit"
    assert a.details["hypothesis"] == "sensor_or_channel" and "датчика или канала" in a.message
    assert a.expected == 600.0 and a.value == 5000.0 and a.confidence is not None


def test_rate_limit_silent_for_physical_changes_and_noise(config: AppConfig) -> None:
    base, st = calibrated(config)
    det = RateLimitDetector(config, base)
    for snap in gen(200, start=305, co2_ramp=5.0):  # быстрый, но физически возможный рост 5 ppm/с
        st.update(snap)
        assert det.detect(st, now_of(st)) == []
    assert det.detect(State(), T0) == []


def test_rate_limit_noise_margin_requires_baseline(config: AppConfig) -> None:
    st = State()
    st.update(make_telemetry(0, battery_soc_pct=80.0))
    st.update(make_telemetry(1, battery_soc_pct=80.5))  # 0.5 %/с при пределе 0.3
    assert RateLimitDetector(config).detect(st, T0)


def test_time_monotonic_detects_frozen_and_rolled_back_ts(config: AppConfig) -> None:
    det = TimeMonotonicDetector(config)
    st = State()
    for i in range(5):
        st.update(make_telemetry(i))
    assert det.detect(st, T0) == []
    for _ in range(3):
        st.update(make_telemetry(4))  # метка повторяется
    (a,) = det.detect(st, T0)
    assert a.kind == "time_monotonic" and a.level is AlertLevel.WARNING and a.details["issue"] == "repeat"
    for _ in range(9):
        st.update(make_telemetry(2))  # метка откатилась
    (b,) = det.detect(st, T0)
    assert b.level is AlertLevel.CRITICAL and b.details["issue"] == "rollback" and "откатилась" in b.message
    st.update(make_telemetry(10))  # время снова идёт
    assert det.detect(st, T0) == [] and st.ts_bad_streak == 0 and st.ts_issue_total == 12


def test_suite_reports_time_issue_even_when_data_is_stale(config: AppConfig) -> None:
    suite, st = DetectorSuite(config), State()
    for i in range(10):
        st.update(make_telemetry(i))
    for _ in range(4):
        st.update(make_telemetry(9, co2_ppm={"co2_a": 5000.0, "co2_b": 600.0}))
    kinds = {a.kind for a in suite.run(st, T0 + timedelta(seconds=30))}
    assert kinds == {"stale", "time_monotonic"}  # остальное подавлено, но нарушение времени видно


# --------------------------------------------------------------------------- время до порога


def ramp_state(rate_per_min: float, noise: float, minutes: float = 6, field: str = "battery_temp_c", start: float = 25.0, seed: int = 1) -> State:
    rng = random.Random(seed)
    st = State()
    for i in range(int(minutes * 60) + 1):
        st.update(make_telemetry(i, **{field: start + rate_per_min * i / 60.0 + rng.gauss(0, noise)}))
    return st


def test_time_to_threshold_predicts_crossing(config: AppConfig) -> None:
    st = ramp_state(1.0, 0.05, start=40.0)  # к концу ≈46 °C, порог 55: ≈9 мин
    (a,) = [a for a in TimeToThresholdDetector(config).detect(st, T0) if a.field == "battery_temp_c"]
    assert a.kind == "time_to_threshold" and a.level is AlertLevel.WARNING and a.expected == 55.0
    assert a.details["eta_s"] == pytest.approx(9 * 60, rel=0.15) and a.details["max_response"] == "prepare"
    assert a.confidence is not None and a.confidence > 0.7 and a.window["n"] >= 60


def test_time_to_threshold_ignores_far_away_or_diverging_or_noisy(config: AppConfig) -> None:
    det = TimeToThresholdDetector(config)
    assert [a for a in det.detect(ramp_state(0.1, 0.05), T0) if a.field == "battery_temp_c"] == []  # порог далеко
    assert [a for a in det.detect(ramp_state(-1.0, 0.05, start=40.0), T0) if a.field == "battery_temp_c"] == []  # от порога
    assert [a for a in det.detect(ramp_state(0.5, 5.0, start=45.0), T0) if a.field == "battery_temp_c"] == []  # R² низкий
    assert [a for a in det.detect(ramp_state(1.0, 0.05, start=60.0), T0) if a.field == "battery_temp_c"] == []  # уже за порогом
    assert det.detect(State(), T0) == []


def test_time_to_threshold_low_side(config: AppConfig) -> None:
    st = ramp_state(-0.8, 0.02, field="battery_soc_pct", start=34.0)  # к 25 % ≈ 4 мин после окна
    alerts = [a for a in TimeToThresholdDetector(config).detect(st, T0) if a.field == "battery_soc_pct"]
    assert alerts and alerts[0].expected == 25.0 and alerts[0].details["eta_s"] > 0


# --------------------------------------------------------------------------- разность пар


def test_pair_diff_detects_single_sensor_deviation(config: AppConfig) -> None:
    base, st = calibrated(config)
    det = PairDiffDetector(config, base)
    seen = []
    for snap in gen(120, start=305, drift_b=0.5):  # co2_b уходит на 0.5 ppm/с
        st.update(snap)
        seen += det.detect(st, now_of(st))
    assert seen and seen[0].kind == "pair_diff" and seen[0].field == "co2_ppm.co2_b"
    assert seen[0].details["suspect"] == "co2_ppm.co2_b" and "подозрение" in seen[0].message


@pytest.mark.slow
def test_pair_diff_silent_on_baseline_noise(config: AppConfig) -> None:
    base, st = calibrated(config)
    det = PairDiffDetector(config, base)
    for snap in gen(3000, seed=77, start=305):
        st.update(snap)
        assert det.detect(st, now_of(st)) == []


def test_pair_diff_needs_confirmation_ticks(config: AppConfig) -> None:
    base, st = calibrated(config)
    det = PairDiffDetector(config, base)
    out = []
    for i, snap in enumerate(gen(10, start=305)):
        if i in (3, 4):  # выброс всего два такта подряд
            snap = make_telemetry(snap.ts.timestamp() - T0.timestamp(), co2_ppm={"co2_a": 850, "co2_b": 900, "co2_c": 845})
        st.update(snap)
        out += det.detect(st, now_of(st))
    assert out == []


def test_pair_diff_hard_limit_works_without_baseline(config: AppConfig) -> None:
    det, st = PairDiffDetector(config, None), State()
    out = []
    for i in range(6):
        st.update(make_telemetry(i, co2_ppm={"co2_a": 600.0, "co2_b": 800.0}))  # разность 200 > 150
        out += det.detect(st, now_of(st))
    assert out and "жёсткий предел" in out[0].message and out[0].details["suspect"] is None
    st2 = State()
    for i in range(6):
        st2.update(make_telemetry(i, co2_ppm={"co2_a": 600.0, "co2_b": 650.0}))
        assert det.detect(st2, now_of(st2)) == []


def test_pair_diff_drift_of_difference_even_inside_corridor(config: AppConfig) -> None:
    det, st = PairDiffDetector(config, None), State()
    out = []
    for i in range(400):
        st.update(make_telemetry(i, temp_c=22.0, temp_c_2=22.0 + 0.5 * i / 60.0))  # 0.5 °C/мин > 0.2
        out += det.detect(st, now_of(st))
    assert any("дрейф разности" in a.message for a in out)


def test_regime_shift_of_both_sensors_is_not_a_pair_anomaly(config: AppConfig) -> None:
    """1a: одновременный рост обоих датчиков пары (работа оборудования) не вызывает тревог."""
    base, st = calibrated(config)
    pair, noise, flat, cusum = PairDiffDetector(config, base), NoiseLossDetector(config, base), FlatlineDetector(config, base), CusumDetector(config, base)
    for snap in gen(900, seed=21, start=305, co2_ramp=0.8, temp_ramp=0.01, load_ramp=1.5):  # CO2 +720 ppm, T +9 °C, нагрузка ×2.5
        st.update(snap)
        now = now_of(st)
        for det in (pair, noise, flat):
            assert det.detect(st, now) == [], type(det).__name__
        assert cusum.detect(st, now) == []


# --------------------------------------------------------------------------- CUSUM


def test_cusum_catches_slow_pair_drift_before_hard_limit(config: AppConfig) -> None:
    base, st = calibrated(config)
    cusum, pair = CusumDetector(config, base), PairDiffDetector(config, base)
    cusum_at = pair_at = None
    for i, snap in enumerate(gen(1500, start=305, drift_t2=0.001)):  # 0.06 °C/мин
        st.update(snap)
        now = now_of(st)
        if cusum_at is None and any(a.field == "temp_c|temp_c_2" or "temp_c_2" in str(a.field) for a in cusum.detect(st, now)):
            cusum_at = i
        if pair_at is None and pair.detect(st, now):
            pair_at = i
    assert cusum_at is not None and cusum_at < 240
    assert pair_at is None or cusum_at < pair_at  # CUSUM замечает раньше коридора


def test_cusum_quiet_on_long_stationary_run(config: AppConfig) -> None:
    base, st = calibrated(config, seed=10)
    cusum = CusumDetector(config, base)
    alerts = 0
    for snap in gen(5400, seed=11, start=305):
        st.update(snap)
        alerts += len(cusum.detect(st, now_of(st)))
    assert alerts == 0


def test_cusum_without_baseline_is_inactive(config: AppConfig) -> None:
    st = State()
    for snap in gen(100, drift_t2=0.05):
        st.update(snap)
    assert CusumDetector(config, None).detect(st, T0) == []
    assert CusumDetector(config, Baseline(config)).detect(st, T0) == []


def test_cusum_on_balance_residual_two_sided(config: AppConfig) -> None:
    base = Baseline(config)
    base.fit(list(gen(301)), [20.0, -25.0, 30.0, -20.0, 25.0, -30.0])
    sigma = base.balance_residual_sigma()
    assert sigma is not None
    st = State()
    for snap in gen(5):
        st.update(snap)
    det = CusumDetector(config, base)
    assert det.detect(st, T0, 0.0) == []
    alerts = []
    for r in (-3 * sigma,) * 7:
        alerts = det.detect(st, T0, r)
    (a,) = [x for x in alerts if x.field == "energy_balance"]
    assert a.kind == "cusum" and a.details["direction"] == "negative" and "теряет больше" in a.message
    det2 = CusumDetector(config, base)
    for r in (3 * sigma,) * 7:
        alerts = det2.detect(st, T0, r)
    assert [x for x in alerts if x.field == "energy_balance"][0].details["direction"] == "positive"
    # возврат к норме снимает тревогу
    for _ in range(40):
        alerts = det.detect(st, T0, 0.0)
    assert not [x for x in alerts if x.field == "energy_balance"]


def balance_sim(config: AppConfig, **initial: object) -> tuple[SimulatorAdapter, list]:  # type: ignore[type-arg]
    events = [HiddenLoadEvent(at=900, watts=900)]
    sim = SimulatorAdapter(config, Scenario(name="t", initial=initial, events=events), seed=3)  # type: ignore[arg-type]
    return sim, []


def test_hidden_load_found_before_critical_charge(config: AppConfig) -> None:
    """3: невязка баланса при hidden_load растёт и обнаруживается раньше критического заряда."""
    sim, _ = balance_sim(config, initial_soc_pct=30, cloud_cover=0.8)
    base, st, rs = Baseline(config), State(), ResidualSeries(config)
    cusum = CusumDetector(config, base)
    detected = crit = None
    for _ in range(4000):
        st.update(sim.get_telemetry())
        r = rs.update(st)
        base.observe(st, {"energy_balance"} if detected else (), r)
        t = sim.t
        if detected is None and any(a.field == "energy_balance" for a in cusum.detect(st, sim.now(), r)):
            detected = t
        if crit is None and sim.truth()["battery_soc_pct"] <= config.thresholds["battery_soc_pct"].crit_low:  # type: ignore[operator]
            crit = t
        sim.advance(1)
    assert detected is not None and 900 < detected < 1200
    assert crit is not None and detected < crit - 300  # упреждение положительно и заметно


# --------------------------------------------------------------------------- шум и залипание


def test_noise_loss_detected_but_noise_growth_is_not(config: AppConfig) -> None:
    base, st = calibrated(config)
    det = NoiseLossDetector(config, base)
    out = []
    for snap in gen(200, start=305, sigma_co2=0.05, temp_ramp=0.002, co2_ramp=0.3):  # шум пропал, физика живая
        st.update(snap)
        out += det.detect(st, now_of(st))
    fields = {a.field for a in out}
    assert "co2_ppm.co2_a" in fields and all(a.kind == "noise_loss" for a in out)
    assert out[0].expected is not None and out[0].value < 0.1 * out[0].expected  # type: ignore[operator]
    base2, st2 = calibrated(config)
    det2 = NoiseLossDetector(config, base2)
    for snap in gen(300, start=305, sigma_co2=40.0, sigma_t=0.3):  # шум вырос в 10 раз: не аномалия
        st2.update(snap)
        assert det2.detect(st2, now_of(st2)) == []


def test_noise_loss_inactive_without_baseline_and_for_flat_signal(config: AppConfig) -> None:
    st = State()
    for snap in gen(100, sigma_co2=0.0):
        st.update(snap)
    assert NoiseLossDetector(config, None).detect(st, T0) == []
    base, st2 = calibrated(config)
    det = NoiseLossDetector(config, base)
    for snap in gen(120, start=305, co2_ppm={"co2_a": 850.0, "co2_b": 858.0, "co2_c": 844.0}):
        st2.update(snap)
    assert [a for a in det.detect(st2, now_of(st2)) if a.field.startswith("co2_ppm")] == []  # type: ignore[union-attr]  # это залипание


def test_flatline_detects_stuck_value_within_noise_fraction(config: AppConfig) -> None:
    base, st = calibrated(config)
    det = FlatlineDetector(config, base)
    out = []
    for snap in gen(100, start=305, co2_ppm={"co2_a": 851.0, "co2_b": 858.0, "co2_c": 845.0}):
        st.update(snap)
        out += det.detect(st, now_of(st))
    fields = {a.field for a in out}
    assert {"co2_ppm.co2_a", "co2_ppm.co2_b", "co2_ppm.co2_c"} <= fields
    assert min((a.window["n"] for a in out)) >= 60 and out[0].details["limit_s"] == 60.0


def test_flatline_not_before_window_and_silent_with_noise(config: AppConfig) -> None:
    base, st = calibrated(config)
    det = FlatlineDetector(config, base)
    for snap in gen(50, start=305, co2_ppm={"co2_a": 851.0, "co2_b": 858.0, "co2_c": 845.0}):
        st.update(snap)
        assert det.detect(st, now_of(st)) == []
    base2, st2 = calibrated(config)
    det2 = FlatlineDetector(config, base2)
    for snap in gen(400, seed=5, start=305):
        st2.update(snap)
        assert det2.detect(st2, now_of(st2)) == []


def test_flatline_exact_equality_without_baseline(config: AppConfig) -> None:
    det, st = FlatlineDetector(config, None), State()
    out = []
    for i in range(70):
        st.update(make_telemetry(i, humidity_pct=45.0 + (0.1 if i % 2 else 0.0), co2_ppm={"co2_a": 700.0, "co2_b": 705.0 + (i % 3)}))
        out += det.detect(st, now_of(st))
    assert {a.field for a in out} == {"co2_ppm.co2_a"}  # у остальных значение «дышит»


# --------------------------------------------------------------------------- набор детекторов


@pytest.mark.slow
def test_suite_quiet_on_noise_with_baseline(config: AppConfig) -> None:
    base, st = calibrated(config)
    suite = DetectorSuite(config, base)
    for snap in gen(2000, seed=31, start=305):
        st.update(snap)
        assert suite.run(st, now_of(st)) == []


def test_suite_finds_false_co2_spike_with_rate_pair_and_threshold(config: AppConfig) -> None:
    base, st = calibrated(config)
    suite = DetectorSuite(config, base)
    kinds: set[str] = set()
    for i, snap in enumerate(gen(60, start=305)):
        if 30 <= i < 45:
            snap = make_telemetry(
                snap.ts.timestamp() - T0.timestamp(),
                co2_ppm={"co2_a": 851.0, "co2_b": 5000.0, "co2_c": 845.0},
                temp_c=22.0,
                temp_c_2=22.15,
            )
        st.update(snap)
        kinds |= {f"{a.kind}:{a.field}" for a in suite.run(st, now_of(st))}
    assert {"rate_limit:co2_ppm.co2_b", "pair_diff:co2_ppm.co2_b", "threshold:co2_ppm.co2_b", "jump:co2_ppm.co2_b"} <= kinds


def test_alert_tracker_updates_known_alert_instead_of_duplicating(config: AppConfig) -> None:
    from kupol.core.detectors import AlertTracker
    from kupol.core.models import Alert

    tr = AlertTracker(60)
    a1 = Alert(ts=T0, level=AlertLevel.WARNING, kind="cusum", field="x", message="m1", value=1.0, confidence=0.2)
    a2 = Alert(ts=T0 + timedelta(seconds=5), level=AlertLevel.WARNING, kind="cusum", field="x", message="m2", value=9.0, confidence=0.9)
    assert tr.filter([a1], T0) == [a1]
    assert tr.filter([a2], T0 + timedelta(seconds=5)) == []
    (active,) = tr.active(T0 + timedelta(seconds=6))
    assert active.id == a1.id and active.value == 9.0 and active.message == "m2" and active.confidence == 0.9


_ = UTC
