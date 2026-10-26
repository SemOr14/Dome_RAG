"""Тесты событий симулятора и YAML-сценариев."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from kupol.adapter.base import AdapterTimeout
from kupol.adapter.scenario import (
    ActuatorFailEvent,
    CascadeEvent,
    CommsLossEvent,
    DeviceFaultEvent,
    DirectEvent,
    FalseSignalEvent,
    HiddenDriftEvent,
    OperatorEvent,
    Scenario,
    ScenarioError,
    load_scenario,
)
from kupol.adapter.simulator import SimulatorAdapter
from kupol.core.config import AppConfig
from kupol.core.models import Command, DeviceStatus
from tests.conftest import ROOT

SCENARIOS = ROOT / "scenarios"


def sim_with(config: AppConfig, *events: object, **initial: object) -> SimulatorAdapter:
    sc = Scenario(name="t", initial={"noise": False, **initial}, events=list(events))  # type: ignore[arg-type]
    return SimulatorAdapter(config, sc, seed=1)


# --------------------------------------------------------------------------- файлы сценариев


@pytest.mark.parametrize("name", ["basic", "cascade_heavy", "quiet"])
def test_shipped_scenarios_load_and_run(config: AppConfig, name: str) -> None:
    sc = load_scenario(SCENARIOS / f"{name}.yaml")
    sim = SimulatorAdapter(config, sc, seed=42)
    sim.advance(sc.duration_s)
    assert sim.get_telemetry().ts == sim.now()


def test_basic_has_every_event_type() -> None:
    sc = load_scenario(SCENARIOS / "basic.yaml")
    kinds = {type(ev) for _, ev, _ in sc.flattened()}
    for needed in (
        DirectEvent,
        HiddenDriftEvent,
        FalseSignalEvent,
        CascadeEvent,
        CommsLossEvent,
        ActuatorFailEvent,
        DeviceFaultEvent,
    ):
        assert needed in kinds, needed.__name__


def test_quiet_has_only_operator_events() -> None:
    sc = load_scenario(SCENARIOS / "quiet.yaml")
    assert all(isinstance(ev, OperatorEvent) for ev in sc.events)


def test_full_run_is_deterministic(config: AppConfig) -> None:
    sc = load_scenario(SCENARIOS / "basic.yaml")
    a, b = SimulatorAdapter(config, sc, seed=42), SimulatorAdapter(config, sc, seed=42)
    a.advance(2000)
    b.advance(2000)
    assert a.get_telemetry() == b.get_telemetry()
    assert a.event_log == b.event_log


# --------------------------------------------------------------------------- события


def test_false_signal_changes_only_reading(config: AppConfig) -> None:
    sim = sim_with(config, FalseSignalEvent(at=10, sensor="co2_ppm.co2_b", value=5000, duration_s=20))
    sim.advance(15)
    tel = sim.get_telemetry()
    assert tel.co2_ppm["co2_b"] == 5000
    assert tel.co2_ppm["co2_a"] < 1000 and tel.co2_ppm["co2_c"] < 1000
    assert sim.truth()["co2_ppm.co2_b"] < 1000  # физически CO2 в норме
    sim.advance(20)
    assert sim.get_telemetry().co2_ppm["co2_b"] < 1000  # ложный сигнал закончился


def test_hidden_drift_changes_only_reading(config: AppConfig) -> None:
    sim = sim_with(config, HiddenDriftEvent(at=0, sensor="battery_temp_c", rate_per_min=1.0, duration_s=600))
    base = sim.truth()["battery_temp_c"]
    sim.advance(300)
    assert sim.get_telemetry().battery_temp_c == pytest.approx(base + 5.0, abs=1.2)
    assert sim.truth()["battery_temp_c"] < base + 2.0
    sim.advance(400)
    assert sim.get_telemetry().battery_temp_c < base + 2.5  # дрейф закончился


def test_direct_pv_drop(config: AppConfig) -> None:
    sim = sim_with(config, DirectEvent(at=10, target="pv", factor=1 / 3))
    sim.advance(9)
    before = sim.truth()["pv_power_w"]
    sim.advance(3)
    assert sim.truth()["pv_power_w"] == pytest.approx(before / 3, rel=0.05)


def test_direct_load_is_temporary(config: AppConfig) -> None:
    sim = sim_with(config, DirectEvent(at=5, target="load", extra_w=500, duration_s=10))
    base = sim.truth()["load_total_w"]
    sim.advance(8)
    assert sim.truth()["load_total_w"] == pytest.approx(base + 500)
    sim.advance(20)
    assert sim.truth()["load_total_w"] == pytest.approx(base)


def test_direct_occupants_real_co2_rise_on_all_sensors(config: AppConfig) -> None:
    sim = sim_with(config, DirectEvent(at=0, target="occupants", delta=6))
    sim.advance(900)
    tel = sim.get_telemetry()
    assert all(v > 1000 for v in tel.co2_ppm.values())


def test_cascade_expands_and_logs(config: AppConfig) -> None:
    sim = sim_with(
        config,
        CascadeEvent(
            at=10,
            note="цепочка",
            steps=[
                OperatorEvent(at=0, action="set_vent", mode="high"),
                OperatorEvent(at=5, action="start_device", device="solder", duration_s=100),
                DirectEvent(at=20, target="load", extra_w=300),
            ],
        ),
    )
    base = sim.truth()["load_total_w"]
    sim.advance(12)
    assert sim.get_telemetry().vent_mode == "high"  # первый шаг каскада уже сработал
    sim.advance(40)
    tel = sim.get_telemetry()
    assert tel.vent_mode == "high" and tel.solder_status is DeviceStatus.RUNNING
    assert sim.truth()["load_total_w"] > base + 300 + 100
    kinds = [e["kind"] for e in sim.event_log]
    assert "cascade" in kinds and kinds.count("operator") == 2 and "direct" in kinds


def test_comms_loss_frozen(config: AppConfig) -> None:
    sim = sim_with(config, CommsLossEvent(at=10, duration_s=20))
    sim.advance(10)
    last_good = sim.get_telemetry()  # последний снимок до потери связи
    sim.advance(9)
    lost = sim.get_telemetry()
    assert not lost.comms_ok and not sim.is_connected()
    assert lost.pv_power_w == last_good.pv_power_w and lost.co2_ppm == last_good.co2_ppm
    assert lost.data_age_s > 5 and lost.ts == sim.now()
    with pytest.raises(AdapterTimeout):
        sim.send_command(Command(name="vent_enable", args={"mode": "low"}))
    sim.advance(20)
    back = sim.get_telemetry()
    assert back.comms_ok and sim.is_connected() and back.data_age_s == 0


def test_comms_loss_timeout_mode(config: AppConfig) -> None:
    sim = sim_with(config, CommsLossEvent(at=1, duration_s=10, mode="timeout"))
    sim.advance(5)
    with pytest.raises(AdapterTimeout):
        sim.get_telemetry()
    with pytest.raises(AdapterTimeout):
        sim.get_history("temp_c", 1)
    sim.advance(10)
    assert sim.get_telemetry().comms_ok


def test_world_keeps_evolving_during_comms_loss(config: AppConfig) -> None:
    sim = sim_with(config, CommsLossEvent(at=1, duration_s=30), DirectEvent(at=2, target="pv", factor=0.1))
    sim.advance(20)
    assert sim.get_telemetry().pv_power_w > 500  # показания застыли
    assert sim.truth()["pv_power_w"] < 300  # а мир изменился


def test_actuator_fail_command_accepted_but_ignored(config: AppConfig) -> None:
    sim = sim_with(config, ActuatorFailEvent(at=0, target="vent", duration_s=60))
    sim.advance(1)
    res = sim.send_command(Command(name="vent_enable", args={"mode": "low"}))
    assert res.status.value == "accepted"
    sim.advance(10)
    assert sim.get_telemetry().vent_mode == "off"
    assert any(e["kind"] == "command_ignored" for e in sim.event_log)
    sim.advance(60)
    sim.send_command(Command(name="vent_enable", args={"mode": "low"}))
    sim.advance(10)
    assert sim.get_telemetry().vent_mode == "low"  # после окончания отказа работает


def test_actuator_fail_other_target_unaffected(config: AppConfig) -> None:
    sim = sim_with(config, ActuatorFailEvent(at=0, target="vent"))
    sim.advance(1)
    sim.send_command(Command(name="set_humidifier_mode", args={"mode": "low"}))
    sim.advance(10)
    assert sim.get_telemetry().humidifier_mode == "low"


def test_actuator_fail_all(config: AppConfig) -> None:
    sim = sim_with(config, ActuatorFailEvent(at=0, target="all"))
    sim.advance(1)
    sim.send_command(Command(name="set_humidifier_mode", args={"mode": "low"}))
    sim.advance(10)
    assert sim.get_telemetry().humidifier_mode == "off"


def test_device_fault_and_recovery(config: AppConfig) -> None:
    sim = sim_with(
        config,
        OperatorEvent(at=0, action="start_device", device="printer", duration_s=1000),
        DeviceFaultEvent(at=20, device="printer", duration_s=30),
    )
    sim.advance(10)
    assert sim.get_telemetry().printer_status is DeviceStatus.RUNNING
    sim.advance(20)
    tel = sim.get_telemetry()
    assert tel.printer_status is DeviceStatus.ERROR and tel.lines["printer"].power_w < 20
    sim.advance(30)
    assert sim.get_telemetry().printer_status is DeviceStatus.IDLE


def test_cannot_start_device_in_error(config: AppConfig) -> None:
    sim = sim_with(
        config,
        DeviceFaultEvent(at=0, device="cnc"),
        OperatorEvent(at=5, action="start_device", device="cnc", duration_s=100),
    )
    sim.advance(20)
    assert sim.get_telemetry().cnc_status is DeviceStatus.ERROR


def test_operator_stop_and_humidifier_and_line(config: AppConfig) -> None:
    sim = sim_with(
        config,
        OperatorEvent(at=0, action="start_device", device="cnc", duration_s=1000),
        OperatorEvent(at=10, action="stop_device", device="cnc"),
        OperatorEvent(at=10, action="set_humidifier", mode="low"),
        OperatorEvent(at=10, action="set_line", line="aux", enabled=False),
    )
    sim.advance(20)
    tel = sim.get_telemetry()
    assert tel.cnc_status is DeviceStatus.IDLE and tel.humidifier_mode == "low" and not tel.lines["aux"].on


# --------------------------------------------------------------------------- ошибки сценариев


def test_unknown_sensor_rejected(config: AppConfig) -> None:
    with pytest.raises(ScenarioError, match="датчик"):
        sim_with(config, FalseSignalEvent(at=1, sensor="nope", value=1, duration_s=1))


def test_unknown_line_and_mode_rejected(config: AppConfig) -> None:
    with pytest.raises(ScenarioError, match="линия"):
        sim_with(config, OperatorEvent(at=1, action="set_line", line="ghost", enabled=True))
    with pytest.raises(ScenarioError, match="вытяжки"):
        sim_with(config, OperatorEvent(at=1, action="set_vent", mode="turbo"))
    with pytest.raises(ScenarioError, match="увлажнителя"):
        sim_with(config, OperatorEvent(at=1, action="set_humidifier", mode="turbo"))


@pytest.mark.parametrize(
    "build",
    [
        lambda: DirectEvent(at=0, target="pv"),
        lambda: DirectEvent(at=0, target="load"),
        lambda: OperatorEvent(at=0, action="start_device"),
        lambda: OperatorEvent(at=0, action="start_device", device="cnc"),
        lambda: OperatorEvent(at=0, action="set_vent"),
        lambda: OperatorEvent(at=0, action="set_line", line="aux"),
    ],
)
def test_event_param_validation(build: object) -> None:
    with pytest.raises(ValueError):
        build()  # type: ignore[operator]


def test_load_scenario_errors(tmp_path: Path) -> None:
    with pytest.raises(ScenarioError, match="не найден"):
        load_scenario(tmp_path / "x.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: [", encoding="utf-8")
    with pytest.raises(ScenarioError, match="YAML"):
        load_scenario(bad)
    lst = tmp_path / "l.yaml"
    lst.write_text("- 1", encoding="utf-8")
    with pytest.raises(ScenarioError, match="словарём"):
        load_scenario(lst)
    wrong = tmp_path / "w.yaml"
    wrong.write_text("name: x\nevents:\n  - {at: 1, type: bogus}\n", encoding="utf-8")
    with pytest.raises(ScenarioError, match="events"):
        load_scenario(wrong)


# --------------------------------------------------------------------------- скрытые и «коварные» события


def hidden_sim(config: AppConfig, *events: object, **initial: object) -> SimulatorAdapter:
    return sim_with(config, *events, **initial)


def test_hidden_load_invisible_in_telemetry_but_drains_battery(config: AppConfig) -> None:
    from kupol.adapter.scenario import HiddenLoadEvent

    base = hidden_sim(config, initial_soc_pct=50, cloud_cover=0.9)
    hid = hidden_sim(config, HiddenLoadEvent(at=0, watts=600), initial_soc_pct=50, cloud_cover=0.9)
    for s in (base, hid):
        s.advance(600)
    assert hid.get_telemetry().load_total_w == pytest.approx(base.get_telemetry().load_total_w, abs=1.0)
    assert sum(t.power_w for t in hid.get_telemetry().lines.values()) == pytest.approx(
        sum(t.power_w for t in base.get_telemetry().lines.values()), abs=1.0
    )
    lost = base.truth()["battery_soc_pct"] - hid.truth()["battery_soc_pct"]
    assert lost == pytest.approx(600 * 600 / 3600 / config.energy.battery_capacity_wh * 100 / 0.97, rel=0.1)


def test_sensor_drift_pair_only_moves_one_sensor(config: AppConfig) -> None:
    from kupol.adapter.scenario import SensorDriftPairEvent

    sim = hidden_sim(config, SensorDriftPairEvent(at=0, sensor="temp_c_2", rate_per_min=0.5))
    sim.advance(600)
    tel = sim.get_telemetry()
    assert tel.temp_c_2 is not None and tel.temp_c_2 - tel.temp_c == pytest.approx(5.0 + 0.15, abs=0.2)
    assert sim.truth()["temp_c_2"] - sim.truth()["temp_c"] == pytest.approx(0.15)


def test_silent_print_fail_keeps_status_but_drops_power(config: AppConfig) -> None:
    from kupol.adapter.scenario import SilentPrintFailEvent

    sim = hidden_sim(
        config,
        OperatorEvent(at=0, action="start_device", device="printer", duration_s=1000),
        SilentPrintFailEvent(at=100, device="printer", power_factor=0.3),
    )
    sim.advance(60)
    healthy = sim.get_telemetry().lines["printer"].power_w
    sim.advance(100)
    tel = sim.get_telemetry()
    assert tel.printer_status is DeviceStatus.RUNNING
    assert tel.lines["printer"].power_w == pytest.approx(healthy * 0.3, rel=0.1)


def test_frozen_sensor_is_constant_and_noise_free(config: AppConfig) -> None:
    from kupol.adapter.scenario import FrozenSensorEvent

    sim = hidden_sim(config, FrozenSensorEvent(at=10, sensor="humidity_pct", duration_s=50), noise=True)
    sim.advance(12)
    values = []
    for _ in range(30):
        sim.advance(1)
        values.append(sim.get_telemetry().humidity_pct)
    assert len(set(values)) == 1
    sim.advance(40)
    assert sim.get_telemetry().humidity_pct != values[0]  # залипание закончилось


def test_noise_loss_reduces_noise_but_follows_physics(config: AppConfig) -> None:
    from statistics import pstdev

    from kupol.adapter.scenario import NoiseLossEvent

    sim = hidden_sim(config, NoiseLossEvent(at=100, sensor="temp_c", noise_factor=0.02), noise=True)
    normal, quiet = [], []
    for i in range(300):
        sim.advance(1)
        (normal if i < 100 else quiet).append(sim.get_telemetry().temp_c)
    assert pstdev(quiet[5:]) < 0.2 * pstdev(normal)
    assert len(set(quiet)) > 5  # величина не константа: просто без шума


def test_stale_timestamp_freeze_and_rollback(config: AppConfig) -> None:
    from kupol.adapter.scenario import StaleTimestampEvent

    sim = hidden_sim(config, StaleTimestampEvent(at=20, mode="freeze", duration_s=30))
    sim.advance(19)
    ts_before = sim.get_telemetry().ts
    sim.advance(25)
    tel = sim.get_telemetry()
    assert tel.ts <= ts_before + timedelta(seconds=2) and tel.ts < sim.now() - timedelta(seconds=20)
    sim.advance(30)
    assert sim.get_telemetry().ts == sim.now()  # после окончания метка снова идёт

    sim2 = hidden_sim(config, StaleTimestampEvent(at=20, mode="rollback", rollback_s=15, duration_s=30))
    sim2.advance(19)
    prev = sim2.get_telemetry().ts
    sim2.advance(2)
    assert sim2.get_telemetry().ts < prev  # метка откатилась


def test_delayed_actuator_executes_late(config: AppConfig) -> None:
    from kupol.adapter.scenario import DelayedActuatorEvent

    sim = hidden_sim(config, DelayedActuatorEvent(at=0, target="vent", delay_s=40, duration_s=100))
    sim.advance(1)
    res = sim.send_command(Command(name="vent_enable", args={"mode": "low"}))
    assert res.status.value == "accepted" and "40" in res.message
    sim.advance(30)
    assert sim.get_telemetry().vent_mode == "off"
    sim.advance(15)
    assert sim.get_telemetry().vent_mode == "low"  # «вдруг сработала»


def test_false_then_real_expands_into_false_and_real_parts(config: AppConfig) -> None:
    from kupol.adapter.scenario import FalseThenRealEvent

    sim = hidden_sim(config, FalseThenRealEvent(at=10, false_duration_s=20, real_after_s=60, real_ppm_s=2.0, real_duration_s=300))
    sim.advance(300)
    cats = [(g["category"], g["type"], g["t_start"]) for g in sim.ground_truth]
    assert ("false", "false_signal", 10.0) in cats and ("real", "direct", 70.0) in cats
    assert sim.truth()["co2_ppm.co2_a"] > 1100  # CO2 действительно вырос
    assert any(e["kind"] == "operator" and "solder" in e["text"] for e in sim.event_log)  # контекст работ


def test_cascade_trap_marks_harmful_actions_in_ground_truth(config: AppConfig) -> None:
    from kupol.adapter.scenario import CascadeTrapEvent

    sim = hidden_sim(config, CascadeTrapEvent(at=5, duration_s=30, window_s=300))
    sim.advance(10)
    trap = [g for g in sim.ground_truth if g["category"] == "trap"][0]
    assert "set_vent_mode:high" in trap["harmful"] and trap["window_s"] == 300 and trap["t_start"] == 5.0
    assert sim.get_telemetry().co2_ppm["co2_b"] == 5000


def test_ground_truth_categories_for_old_events(config: AppConfig) -> None:
    sc = load_scenario(SCENARIOS / "basic.yaml")
    sim = SimulatorAdapter(config, sc, seed=1)
    sim.advance(sc.duration_s)
    by_type = {g["type"]: g["category"] for g in sim.ground_truth}
    assert by_type["false_signal"] == "false" and by_type["hidden_drift"] == "hidden"
    assert by_type["comms_loss"] == "real" and by_type["device_fault"] == "real"
    assert all(g["t_start"] >= 0 and "id" in g for g in sim.ground_truth)


@pytest.mark.parametrize("name", ["hidden", "traps", "mixed"])
def test_new_scenarios_run_and_are_deterministic(config: AppConfig, name: str) -> None:
    sc = load_scenario(SCENARIOS / f"{name}.yaml")
    a, b = SimulatorAdapter(config, sc, seed=11), SimulatorAdapter(config, sc, seed=11)
    a.advance(sc.duration_s)
    b.advance(sc.duration_s)
    assert a.get_telemetry() == b.get_telemetry() and a.ground_truth == b.ground_truth
    assert a.ground_truth, "эталонная разметка не должна быть пустой"


def test_hidden_scenario_contains_each_hidden_type() -> None:
    sc = load_scenario(SCENARIOS / "hidden.yaml")
    types = {type(ev).__name__ for _, ev, _ in sc.flattened()}
    for needed in (
        "HiddenLoadEvent",
        "SensorDriftPairEvent",
        "SilentPrintFailEvent",
        "FrozenSensorEvent",
        "NoiseLossEvent",
        "StaleTimestampEvent",
        "DelayedActuatorEvent",
        "HiddenDriftEvent",
    ):
        assert needed in types


def test_traps_scenario_contains_both_traps() -> None:
    types = {type(ev).__name__ for _, ev, _ in load_scenario(SCENARIOS / "traps.yaml").flattened()}
    assert {"FalseThenRealEvent", "CascadeTrapEvent"} <= types


def test_direct_co2_source_requires_rate() -> None:
    with pytest.raises(ValueError):
        DirectEvent(at=0, target="co2")


def test_below_min_battery_is_logged_once(config: AppConfig) -> None:
    sim = hidden_sim(config, initial_soc_pct=20.5, cloud_cover=1.0)
    sim.advance(1500)
    assert [e["kind"] for e in sim.event_log].count("battery_below_min") == 1
