"""Тесты калибровки: только шум, разность пар и шум невязки; независимость от режима оборудования."""

from __future__ import annotations

import random
from datetime import timedelta

import pytest

from kupol.adapter.scenario import HiddenLoadEvent, Scenario
from kupol.adapter.simulator import SimulatorAdapter
from kupol.core.baseline import (
    Baseline,
    BaselineSnapshot,
    ResidualSeries,
    detrended_sigma,
    pooled_noise,
)
from kupol.core.config import AppConfig
from kupol.core.state import State
from tests.conftest import T0, make_telemetry

CO2 = ("co2_ppm.co2_a", "co2_ppm.co2_b")


def stream(
    seconds: int,
    seed: int = 1,
    sigma_co2: float = 4.0,
    sigma_t: float = 0.03,
    co2_ramp: float = 0.0,
    temp_ramp: float = 0.0,
    offset_b: float = 8.0,
    start: int = 0,
) -> list:  # type: ignore[type-arg]
    """Синтетический ряд с известным шумом и смещением пары; опциональный общий рост (работа оборудования)."""
    rng = random.Random(seed)
    out = []
    for i in range(start, start + seconds):
        base = 800.0 + co2_ramp * i
        temp = 22.0 + temp_ramp * i
        out.append(
            make_telemetry(
                i,
                co2_ppm={"co2_a": base + rng.gauss(0, sigma_co2), "co2_b": base + offset_b + rng.gauss(0, sigma_co2)},
                temp_c=temp + rng.gauss(0, sigma_t),
                temp_c_2=temp + 0.15 + rng.gauss(0, sigma_t),
            )
        )
    return out


def fitted(config: AppConfig, snaps: list) -> Baseline:  # type: ignore[type-arg]
    b = Baseline(config)
    b.fit(snaps)
    return b


# --------------------------------------------------------------------------- вспомогательные оценки


def test_detrended_sigma_removes_linear_trend() -> None:
    rng = random.Random(3)
    t = list(range(200))
    flat = [5 + rng.gauss(0, 2.0) for _ in t]
    ramp = [5 + 0.7 * x + rng.gauss(0, 2.0) for x in t]
    assert detrended_sigma(t, flat) == pytest.approx(2.0, rel=0.15)
    assert detrended_sigma(t, ramp) == pytest.approx(2.0, rel=0.15)  # тренд не раздувает шум
    assert detrended_sigma([1, 2, 3], [1, 2, 3]) is None  # мало точек
    assert detrended_sigma([5.0] * 10, list(range(10))) is None  # нет разброса по времени


def test_pooled_noise_is_median_robust_to_events() -> None:
    rng = random.Random(4)
    pts = [(float(i), 10 + rng.gauss(0, 1.0)) for i in range(300)]
    pts[100:130] = [(float(i), 10 + (i - 100) * 5.0) for i in range(100, 130)]  # событие в одном подокне
    est = pooled_noise(pts, 30.0, 3)
    assert est is not None and est[0] == pytest.approx(1.0, rel=0.25) and est[1] >= 9
    assert pooled_noise(pts[:20], 30.0, 3) is None and pooled_noise([], 30.0, 3) is None


# --------------------------------------------------------------------------- калибровка


def test_fit_estimates_noise_and_pair_offset(config: AppConfig) -> None:
    b = fitted(config, stream(300))
    assert b.fitted
    assert b.noise_level("co2_ppm.co2_a") == pytest.approx(4.0, rel=0.15)
    assert b.noise_level("temp_c") == pytest.approx(0.03, rel=0.15)
    assert b.pair_offset("co2_ppm.co2_a", "co2_ppm.co2_b") == pytest.approx(-8.0, abs=1.0)
    assert b.pair_offset("co2_ppm.co2_b", "co2_ppm.co2_a") == pytest.approx(8.0, abs=1.0)  # знак учитывает порядок
    assert b.pair_offset("temp_c", "temp_c_2") == pytest.approx(-0.15, abs=0.01)
    assert b.noise_level("pv_power_w") is None and b.pair_offset("temp_c", "co2_ppm.co2_a") is None


def test_calibration_robust_to_trend_of_absolute_values(config: AppConfig) -> None:
    """1b: рост абсолютных значений на начальном отрезке (Оператор начал работу) не меняет оценки."""
    calm = fitted(config, stream(300, seed=5))
    busy = fitted(config, stream(300, seed=5, co2_ramp=1.2, temp_ramp=0.004))  # CO2 +360 ppm, T +1.2 °C за отрезок
    for field in ("co2_ppm.co2_a", "co2_ppm.co2_b", "temp_c", "temp_c_2"):
        assert busy.noise_level(field) == pytest.approx(calm.noise_level(field), rel=0.1), field
    assert busy.pair_offset("co2_ppm.co2_a", "co2_ppm.co2_b") == pytest.approx(calm.pair_offset("co2_ppm.co2_a", "co2_ppm.co2_b"), abs=0.8)
    assert busy.pair_offset("temp_c", "temp_c_2") == pytest.approx(calm.pair_offset("temp_c", "temp_c_2"), abs=0.01)


def test_baseline_has_no_absolute_levels(config: AppConfig) -> None:
    """1c: в структуре данных нет среднего, уровня и порогов абсолютных значений."""
    forbidden = ("mean", "level", "threshold", "zscore", "z_score", "avg", "average", "expected", "normal", "limit", "abs_")
    assert set(BaselineSnapshot.model_fields) == {
        "fitted", "fitted_at", "noise", "pair_offset", "balance_sigma", "subwindows", "residuals", "updates", "skipped_in_alert"
    }
    for name in BaselineSnapshot.model_fields:
        assert not any(word in name.lower() for word in forbidden), name
    b = fitted(config, stream(300))
    for name in vars(b):
        assert not any(word in name.lower() for word in forbidden), name
    # значения словарей — только σ шума и смещения пар; ключи — поля/пары, не уровни
    snap = b.snapshot()
    assert all(v < 50 for v in snap.noise.values())  # σ датчиков, а не уровни вроде 800 ppm
    assert all(abs(v) < 50 for v in snap.pair_offset.values())


def test_initial_window_collects_then_fits(config: AppConfig) -> None:
    b = Baseline(config)
    st = State()
    fitted_at = None
    for snap in stream(400):
        st.update(snap)
        if b.observe(st, ()) and fitted_at is None:
            fitted_at = (snap.ts - T0).total_seconds()
    assert b.fitted and fitted_at == pytest.approx(config.baseline.window_s, abs=2)


def test_data_from_alert_period_is_not_stored(config: AppConfig) -> None:
    rng = random.Random(8)
    b, st = Baseline(config), State()
    for i in range(400):
        spike = 120 <= i < 180  # период тревоги: большие выбросы
        tel = make_telemetry(
            i,
            co2_ppm={"co2_a": 800 + rng.gauss(0, 4) + (3000 if spike else 0), "co2_b": 808 + rng.gauss(0, 4)},
            temp_c=22 + rng.gauss(0, 0.03),
            temp_c_2=22.15 + rng.gauss(0, 0.03),
        )
        st.update(tel)
        b.observe(st, {"co2_ppm.co2_a"} if spike else ())
    assert b.snapshot().skipped_in_alert == 60
    assert b.noise_level("co2_ppm.co2_a") == pytest.approx(4.0, rel=0.2)  # выбросы в оценку не попали


def test_slow_update_tracks_slow_change_but_rejects_anomalies(config: AppConfig) -> None:
    b, st = Baseline(config), State()
    for snap in stream(305, seed=2):
        st.update(snap)
        b.observe(st, ())
    assert b.fitted
    base_noise = b.noise_level("co2_ppm.co2_a")
    updates0 = b.snapshot().updates
    # шум вырос в 1.5 раза: допустимое медленное изменение, оценка чуть сдвигается
    for snap in stream(600, seed=3, sigma_co2=6.0, start=305):
        st.update(snap)
        b.observe(st, ())
    assert b.snapshot().updates > updates0
    assert base_noise < b.noise_level("co2_ppm.co2_a") < 1.5 * base_noise  # type: ignore[operator]
    # потеря шума (в 50 раз) — аномалия, а не новая норма: оценка не «съедается»
    kept = b.noise_level("co2_ppm.co2_a")
    for snap in stream(600, seed=4, sigma_co2=0.05, start=905):
        st.update(snap)
        b.observe(st, ())
    assert b.noise_level("co2_ppm.co2_a") == pytest.approx(kept, rel=0.05)


def test_blocked_field_is_not_updated(config: AppConfig) -> None:
    b, st = Baseline(config), State()
    for snap in stream(305):
        st.update(snap)
        b.observe(st, ())
    before = b.snapshot()
    for snap in stream(600, seed=9, sigma_co2=6.0, start=305):
        st.update(snap)
        b.observe(st, {"co2_ppm.co2_a", "co2_ppm.co2_b"})
    after = b.snapshot()
    assert after.noise["co2_ppm.co2_a"] == before.noise["co2_ppm.co2_a"]
    assert after.noise["temp_c"] != before.noise["temp_c"]  # поля без тревоги обновляются
    for snap in stream(120, seed=9, start=905):
        st.update(snap)
        b.observe(st, {"*"})
    assert b.snapshot().skipped_in_alert > before.skipped_in_alert


def test_not_enough_data_stays_unfitted(config: AppConfig) -> None:
    b = Baseline(config)
    b.fit([])
    assert not b.fitted and b.noise_level("temp_c") is None and b.balance_residual_sigma() is None
    b.fit(stream(10))
    assert not b.fitted


def test_comms_loss_ticks_ignored(config: AppConfig) -> None:
    b, st = Baseline(config), State()
    st.update(make_telemetry(0, comms_ok=False))
    assert b.observe(st, ()) is False
    assert b.observe(State(), ()) is False


# --------------------------------------------------------------------------- невязка баланса


def run_residuals(config: AppConfig, seconds: int, **initial: object) -> tuple[list[float], SimulatorAdapter]:
    events = initial.pop("events", [])
    sim = SimulatorAdapter(config, Scenario(name="t", initial=dict(initial), events=events), seed=5)  # type: ignore[arg-type]
    st, rs, out = State(), ResidualSeries(config), []
    for _ in range(seconds):
        st.update(sim.get_telemetry())
        r = rs.update(st)
        if r is not None:
            out.append(r)
        sim.advance(1)
    return out, sim


def test_residual_is_noise_around_zero_without_anomalies(config: AppConfig) -> None:
    res, _ = run_residuals(config, 1800, cloud_cover=0.2)
    mean = sum(res) / len(res)
    sd = (sum((r - mean) ** 2 for r in res) / (len(res) - 1)) ** 0.5
    assert len(res) == pytest.approx(1800 / config.baseline.residual_window_s, abs=2)
    assert abs(mean) < 2 * sd / len(res) ** 0.5 + 5 and 5 < sd < 80


def test_residual_shows_hidden_load(config: AppConfig) -> None:
    res, _ = run_residuals(config, 900, cloud_cover=0.6, events=[HiddenLoadEvent(at=300, watts=400)])
    before, after = res[:4], res[6:]
    assert abs(sum(before) / len(before)) < 60
    assert sum(after) / len(after) == pytest.approx(-400, abs=50)


def test_residual_skipped_when_battery_pegged_or_comms_lost(config: AppConfig) -> None:
    res, _ = run_residuals(config, 600, initial_soc_pct=100.0, cloud_cover=0.0)
    assert res == []  # АКБ полна и заряд упирается в 100 %: невязка не определена
    rs, st = ResidualSeries(config), State()
    for i in range(300):
        st.update(make_telemetry(i, comms_ok=(i % 50 != 0)))
        assert rs.update(st) is None


def test_balance_sigma_has_floor_and_safety(config: AppConfig) -> None:
    b = Baseline(config)
    snaps = stream(300)
    b.fit(snaps, [0.1, -0.2, 0.15, 0.0, -0.1])  # почти нулевой разброс
    assert b.balance_residual_sigma() == config.baseline.balance_sigma_floor_w
    b2 = Baseline(config)
    b2.fit(snaps, [60.0, -60.0, 60.0, -60.0, 60.0])
    assert b2.balance_residual_sigma() > 60 * config.baseline.sigma_safety * 0.9  # type: ignore[operator]
    b3 = Baseline(config)
    b3.fit(snaps, [10.0])
    assert b3.balance_residual_sigma() is None  # мало окон невязки


def test_residual_sigma_slow_update_rejects_outliers(config: AppConfig) -> None:
    b, st = Baseline(config), State()
    snaps = stream(300)
    for s in snaps:
        st.update(s)
    b.fit(snaps, [30.0, -30.0, 30.0, -30.0, 30.0])
    sigma0 = b.balance_residual_sigma()
    st2 = State()
    for s in stream(5, start=300):
        st2.update(s)
    b._fitted = True
    assert b.observe(st2, (), residual=500.0) is False  # выброс отброшен
    assert b.balance_residual_sigma() == sigma0
    assert b.observe(st2, (), residual=20.0) is True and b.balance_residual_sigma() != sigma0
    b.observe(st2, {"energy_balance"}, residual=10.0)
    assert b.snapshot().skipped_in_alert >= 1
