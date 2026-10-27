"""Synthetic simultaneous-source checks. Historical delays: test_dome_resilience."""

from __future__ import annotations

from pathlib import Path

from kupol.dome.checks import (
    assess_co2,
    assess_condensate,
    assess_energy,
    assess_printer,
    assess_smoke,
    independent_sources,
    p_real,
)
from kupol.dome.config import load_dome_config
from kupol.dome.state import DomeState
from tests.dome_helpers import set_path, synchronous_snapshot as snapshot, synchronous_state_from as state_from

CHECKS = load_dome_config(Path(__file__).resolve().parents[1] / "dome.yaml").checks


def seq(*names: str, dt: float = 2.0) -> DomeState:
    """Состояние после нескольких снимков подряд (даёт историю для скоростей)."""
    st = DomeState()
    for i, n in enumerate(names):
        st.update(snapshot(n)["data"], "team", 1000.0 + i * dt)
    return st


# --------------------------------------------------------------------------- CO2


def test_quiet_co2_is_ok() -> None:
    a = assess_co2(state_from("quiet"), CHECKS)
    assert a.kind == "ok" and not a.suspects and len(a.values) == 3


def test_false_spike_on_primary_sensor_is_detected_at_once() -> None:
    a = assess_co2(seq("quiet", "co2_pending"), CHECKS)  # 725 → 2983 за один отсчёт
    assert a.kind == "false_sensor"
    assert a.spikes == {"climate_sensor_01"} and "climate_sensor_01" in a.outliers
    assert set(a.good()) == {"climate_sensor_02", "climate_sensor_03"}
    assert p_real(a.evidence) < 0.1
    assert independent_sources(a.evidence, "false") == 1  # same physical sensor, two checks


def test_persistent_outlier_without_spike_still_false() -> None:
    a = assess_co2(state_from("co2_after"), CHECKS)  # 3000 держится, два других ~780
    assert a.kind == "false_sensor" and a.outliers == {"climate_sensor_01"}
    assert a.reference is not None and 760 < a.reference < 800


def test_all_three_high_is_real_not_false() -> None:
    st = state_from("quiet")
    for s, v in (("climate_sensor_01", 1850), ("climate_sensor_02", 1700), ("climate_sensor_03", 1650)):
        set_path(st, f"{s}.co2_ppm", v)
    a = assess_co2(st, CHECKS)
    assert a.kind == "real" and not a.suspects
    assert p_real(a.evidence) > 0.9


def test_two_of_three_high_is_treated_as_real_conservatively() -> None:
    st = state_from("quiet")
    for s, v in (("climate_sensor_01", 1900), ("climate_sensor_02", 760), ("climate_sensor_03", 1500)):
        set_path(st, f"{s}.co2_ppm", v)
    a = assess_co2(st, CHECKS)
    assert a.kind == "real" and not a.suspects  # расхождение двух высоких и одного низкого — не «ложный выброс»


def test_null_and_frozen_sensors_are_skipped() -> None:
    st = state_from("co2_after")
    set_path(st, "climate_sensor_02.co2_ppm", None)
    st.nodes["climate_sensor_03"]["status"] = "UNKNOWN"
    a = assess_co2(st, CHECKS)
    assert set(a.values) == {"climate_sensor_01"} and a.kind != "false_sensor"


def test_untrusted_flag_read_from_automation() -> None:
    a = assess_co2(state_from("co2_after"), CHECKS)
    assert a.untrusted == {"climate_sensor_01"}


# --------------------------------------------------------------------------- конденсат


def test_condensate_single_sensor_false() -> None:
    st = state_from("quiet")
    set_path(st, "climate_sensor_01.humidity_pct", 99.0)
    a = assess_condensate(st, CHECKS)
    assert a.kind == "false_sensor" and a.bad == {"climate_sensor_01"}


def test_condensate_two_sensors_is_real() -> None:
    st = state_from("quiet")
    for s in ("climate_sensor_01", "climate_sensor_02"):
        set_path(st, f"{s}.humidity_pct", 96.0)
    assert assess_condensate(st, CHECKS).kind == "real"


def test_condensate_by_dew_point_gap() -> None:
    st = state_from("quiet")
    set_path(st, "climate_sensor_03.dew_point_c", st.num("climate_sensor_03.temperature_c") - 0.3)
    a = assess_condensate(st, CHECKS)
    assert a.kind == "false_sensor" and a.bad == {"climate_sensor_03"}


# --------------------------------------------------------------------------- дым


def smoke_state(**kw: float | bool | str) -> DomeState:
    st = state_from("quiet")
    st.nodes["smoke_detector_01"].update({"smoke_detected": True, "alarm_state": "ALARM", "false_alarm_suspected": False})
    for path, value in kw.items():
        set_path(st, path.replace("__", "."), value)
    return st


def test_smoke_false_needs_two_independent_signs() -> None:
    st = smoke_state()
    a = assess_smoke(st, CHECKS)  # obscuration 0, CO 0.5, VOC 97 — три «ложных» свидетельства
    assert a.alarm and a.kind == "false" and a.false_sources >= 2


def test_any_real_sign_vetoes_false() -> None:
    st = smoke_state(**{"air_quality_sensor_01__co_ppm": 40.0})
    assert assess_smoke(st, CHECKS).kind == "real"
    st2 = smoke_state()
    st2.nodes["smoke_detector_02"].update({"smoke_detected": True, "zone": st2.node("smoke_detector_01")["zone"]})
    assert assess_smoke(st2, CHECKS).kind == "real"


def test_smoke_unknown_when_evidence_thin() -> None:
    st = smoke_state(**{"smoke_detector_01__obscuration_pct": None, "air_quality_sensor_01__co_ppm": None, "air_quality_sensor_01__voc_index": None})
    assert assess_smoke(st, CHECKS).kind == "unknown"


def test_no_alarm_no_assessment() -> None:
    assert not assess_smoke(state_from("quiet"), CHECKS).alarm


# --------------------------------------------------------------------------- принтер


def test_webhook_lie_detected_like_in_the_logbook() -> None:
    p = assess_printer(state_from("printer_done"))
    assert p.webhook_lies and p.printing and not p.done


def test_real_completion() -> None:
    st = state_from("quiet")  # принтер COMPLETED, 100 %
    p = assess_printer(st)
    assert p.done and not p.webhook_lies


# --------------------------------------------------------------------------- энергия


def test_energy_assessment() -> None:
    e = assess_energy(state_from("quiet"), CHECKS)
    assert e.soc is not None and 39 < e.soc < 41 and e.mode == "GRID_ONLY" and e.grid_ok
    assert e.t_left_h is None  # идёт заряд
    st = state_from("quiet", **{"solar_inverter_01.battery_power_w": -500.0})
    e2 = assess_energy(st, CHECKS)
    assert e2.t_left_h is not None and 0.5 < e2.t_left_h < 1.0
