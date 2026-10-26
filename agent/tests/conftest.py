"""Общие фикстуры тестов."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kupol.core.config import AppConfig, load_config
from kupol.core.models import LineState, Telemetry

ROOT = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 10, 27, 10, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def config() -> AppConfig:
    """Боевой `config.yaml` из корня репозитория (проверяет и его валидность)."""
    return load_config(ROOT / "config.yaml")


def make_telemetry(t: float = 0.0, **kw: object) -> Telemetry:
    """Собрать снимок телеметрии: `t` — секунды от `T0`, `kw` переопределяют поля."""
    base: dict[str, object] = dict(
        ts=T0 + timedelta(seconds=t),
        pv_power_w=1500.0,
        battery_soc_pct=80.0,
        battery_voltage_v=52.0,
        battery_temp_c=25.0,
        load_total_w=900.0,
        temp_c=22.0,
        humidity_pct=45.0,
        co2_ppm={"co2_a": 600.0, "co2_b": 610.0},
        lines={
            "cnc": LineState(on=True, power_w=10.0),
            "lighting": LineState(on=True, power_w=100.0),
        },
        vent_mode="off",
        humidifier_mode="off",
    )
    base.update(kw)
    return Telemetry(**base)  # type: ignore[arg-type]
