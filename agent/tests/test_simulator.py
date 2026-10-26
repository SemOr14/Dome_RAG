"""Тесты симулятора: энергобаланс, микроклимат, устройства, команды, детерминизм."""

from __future__ import annotations

import pytest

from kupol.adapter.base import AdapterError
from kupol.adapter.scenario import OperatorEvent, Scenario
from kupol.adapter.simulator import SimulatorAdapter
from kupol.core.config import AppConfig
from kupol.core.models import Command, CommandStatus, DeviceStatus


def quiet(config: AppConfig, **initial: object) -> SimulatorAdapter:
    return SimulatorAdapter(config, Scenario(name="t", initial=dict(initial)), seed=1)


def op(at: float, **kw: object) -> OperatorEvent:
    return OperatorEvent(at=at, **kw)  # type: ignore[arg-type]


def test_time_is_virtual_and_utc(config: AppConfig) -> None:
    sim = quiet(config)
    t0 = sim.now()
    sim.advance(90)
    assert (sim.now() - t0).total_seconds() == 90 and sim.now().tzinfo is not None
    sim.advance(0.5)
    assert (sim.now() - t0).total_seconds() == pytest.approx(90.5)


def test_get_telemetry_is_idempotent(config: AppConfig) -> None:
    sim = quiet(config)
    sim.advance(5)
    assert sim.get_telemetry() == sim.get_telemetry()


def test_telemetry_shape(config: AppConfig) -> None:
    tel = quiet(config).get_telemetry()
    assert set(tel.lines) == set(config.equipment.lines)
    assert set(tel.co2_ppm) == {"co2_a", "co2_b", "co2_c"}
    assert tel.comms_ok and tel.cnc_status is DeviceStatus.IDLE


def test_deterministic_for_same_seed(config: AppConfig) -> None:
    a = SimulatorAdapter(config, seed=7)
    b = SimulatorAdapter(config, seed=7)
    c = SimulatorAdapter(config, seed=8)
    for s in (a, b, c):
        s.advance(120)
    assert a.get_telemetry() == b.get_telemetry()
    assert a.get_telemetry() != c.get_telemetry()


def test_reading_does_not_change_randomness(config: AppConfig) -> None:
    a = SimulatorAdapter(config, seed=3)
    b = SimulatorAdapter(config, seed=3)
    for _ in range(60):
        a.advance(1)
        a.get_telemetry()
    b.advance(60)
    assert a.get_telemetry() == b.get_telemetry()


def test_noise_off_is_smooth(config: AppConfig) -> None:
    sim = quiet(config, noise=False)
    sim.advance(10)
    first = sim.get_telemetry().temp_c
    sim.advance(1)
    assert abs(sim.get_telemetry().temp_c - first) < 0.01


def test_energy_balance_matches_soc_change(config: AppConfig) -> None:
    sim = quiet(config, noise=False, initial_soc_pct=60)
    sim.advance(30)
    soc0, t0 = sim.truth()["battery_soc_pct"], sim.t
    p0 = sim.truth()
    sim.advance(600)
    d_soc = sim.truth()["battery_soc_pct"] - soc0
    p1 = sim.truth()
    net = ((p0["pv_power_w"] - p0["load_total_w"]) + (p1["pv_power_w"] - p1["load_total_w"])) / 2
    expected_pct = net * (sim.t - t0) / 3600.0 / config.energy.battery_capacity_wh * 100.0
    assert d_soc == pytest.approx(expected_pct, rel=0.15)


def test_battery_discharges_under_load(config: AppConfig) -> None:
    sim = quiet(config, noise=False, initial_soc_pct=50, cloud_cover=0.9)
    soc0 = sim.truth()["battery_soc_pct"]
    sim.advance(300)
    assert sim.truth()["battery_soc_pct"] < soc0


def test_battery_charges_in_sun(config: AppConfig) -> None:
    sim = quiet(config, noise=False, initial_soc_pct=50, cloud_cover=0.0)
    sim.advance(300)
    assert sim.truth()["battery_soc_pct"] > 50


def test_soc_clamped(config: AppConfig) -> None:
    sim = quiet(config, noise=False, initial_soc_pct=99.99, cloud_cover=0.0)
    sim.advance(3600)
    assert sim.truth()["battery_soc_pct"] <= 100.0


def test_co2_rises_with_people_and_falls_with_vent(config: AppConfig) -> None:
    sim = quiet(config, noise=False, occupants=3)
    sim.advance(900)
    high = sim.truth()["co2_ppm.co2_a"]
    assert high > 1000
    sim._w.vent_mode = "high"
    sim.advance(900)
    assert sim.truth()["co2_ppm.co2_a"] < high - 300


def test_humidifier_raises_humidity(config: AppConfig) -> None:
    sim = quiet(config, noise=False)
    h0 = sim.truth()["humidity_pct"]
    sim._w.humidifier_mode = "high"
    sim.advance(600)
    assert sim.truth()["humidity_pct"] > h0 + 3


def test_running_devices_heat_room_and_draw_power(config: AppConfig) -> None:
    sc = Scenario(
        name="t",
        initial={"noise": False},
        events=[op(1, action="start_device", device="solder", duration_s=600)],
    )
    sim = SimulatorAdapter(config, sc, seed=1)
    t0 = sim.truth()
    sim.advance(120)
    tel = sim.get_telemetry()
    assert tel.solder_status is DeviceStatus.RUNNING
    assert tel.lines["solder"].power_w == pytest.approx(config.equipment.lines["solder"].nominal_w, rel=0.01)
    assert sim.truth()["temp_c"] > t0["temp_c"]


def test_device_finishes_and_returns_to_idle(config: AppConfig) -> None:
    sc = Scenario(name="t", events=[op(0, action="start_device", device="cnc", duration_s=30)])
    sim = SimulatorAdapter(config, sc, seed=1)
    sim.advance(10)
    assert sim.get_telemetry().cnc_status is DeviceStatus.RUNNING
    sim.advance(40)
    assert sim.get_telemetry().cnc_status is DeviceStatus.IDLE


def test_device_power_ramps_up(config: AppConfig) -> None:
    sc = Scenario(name="t", initial={"noise": False}, events=[op(0, action="start_device", device="cnc", duration_s=100)])
    sim = SimulatorAdapter(config, sc, seed=1)
    sim.advance(2)
    early = sim.get_telemetry().lines["cnc"].power_w
    sim.advance(10)
    assert sim.get_telemetry().lines["cnc"].power_w > early


def test_line_off_aborts_running_device(config: AppConfig) -> None:
    sc = Scenario(
        name="t",
        events=[
            op(0, action="start_device", device="printer", duration_s=1000),
            op(20, action="set_line", line="printer", enabled=False),
        ],
    )
    sim = SimulatorAdapter(config, sc, seed=1)
    sim.advance(30)
    tel = sim.get_telemetry()
    assert tel.printer_status is DeviceStatus.IDLE and tel.lines["printer"].power_w == 0.0


def test_command_applied_after_delay(config: AppConfig) -> None:
    sim = quiet(config, actuator_delay_s=3)
    res = sim.send_command(Command(name="vent_enable", args={"mode": "low"}))
    assert res.status is CommandStatus.ACCEPTED
    sim.advance(2)
    assert sim.get_telemetry().vent_mode == "off"
    sim.advance(2)
    assert sim.get_telemetry().vent_mode == "low"


@pytest.mark.parametrize(
    ("cmd", "check"),
    [
        (Command(name="set_humidifier_mode", args={"mode": "low"}), lambda t: t.humidifier_mode == "low"),
        (Command(name="set_line", args={"line": "lighting", "enabled": False}), lambda t: not t.lines["lighting"].on),
        (Command(name="set_power_mode", args={"mode": "eco"}), lambda t: t.power_mode == "eco"),
        (Command(name="set_vent_mode", args={"mode": "high"}), lambda t: t.vent_mode == "high"),
    ],
)
def test_all_command_types_work(config: AppConfig, cmd: Command, check: object) -> None:
    sim = quiet(config)
    sim.send_command(cmd)
    sim.advance(5)
    assert check(sim.get_telemetry())  # type: ignore[operator]


def test_eco_mode_reduces_load(config: AppConfig) -> None:
    sim = quiet(config, noise=False)
    before = sim.truth()["load_total_w"]
    sim.send_command(Command(name="set_power_mode", args={"mode": "eco"}))
    sim.advance(5)
    assert sim.truth()["load_total_w"] < before


@pytest.mark.parametrize(
    "cmd",
    [
        Command(name="format_disk"),
        Command(name="set_vent_mode", args={"mode": "turbo"}),
        Command(name="set_humidifier_mode", args={}),
        Command(name="set_line", args={"line": "ghost", "enabled": True}),
        Command(name="set_line", args={"line": "aux", "enabled": "yes"}),
        Command(name="set_power_mode", args={"mode": "x"}),
    ],
)
def test_protocol_rejects_garbage(config: AppConfig, cmd: Command) -> None:
    with pytest.raises(AdapterError):
        quiet(config).send_command(cmd)


def test_random_actuator_failure_is_deterministic(config: AppConfig) -> None:
    def run() -> str:
        sim = quiet(config, actuator_fail_prob=1.0)
        sim.send_command(Command(name="vent_enable", args={"mode": "low"}))
        sim.advance(10)
        return sim.get_telemetry().vent_mode

    assert run() == "off" == run()


def test_history(config: AppConfig) -> None:
    sim = quiet(config)
    sim.advance(180)
    hist = sim.get_history("temp_c", 1)
    assert 55 <= len(hist) <= 62 and hist == sorted(hist)
    with pytest.raises(AdapterError):
        sim.get_history("nope", 1)


def test_bad_initial_rejected(config: AppConfig) -> None:
    from kupol.adapter.scenario import ScenarioError

    with pytest.raises(ScenarioError, match="initial"):
        quiet(config, cloud_cover=5)
