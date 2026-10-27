"""Offline regression cases for historical hazards; no real equipment or LLM calls."""
import asyncio
import copy

import pytest
from kupol.dome.checks import Ev, assess_co2, independent_sources
from kupol.dome.control import ControlPolicy
from kupol.dome.evidence import measurement_usable
from kupol.dome.expects import expect_for
from kupol.dome.gate import Expect
from kupol.dome.incidents import IncidentCenter
from kupol.dome.playbooks.base import Intent, Step
from kupol.dome.playbooks.energy import EnergyPlaybook
from kupol.dome.runner import Runner

from tests.dome_helpers import make_ctx, set_path, state_from
from tests.test_dome_gate import CFG, Rig


def frame(rig):
    nodes = copy.deepcopy(rig.state.nodes)
    for body in nodes.values():
        if isinstance(body.get("updated_at"), (int, float)):
            body["updated_at"] += 1
    rig.state.update({"nodes": nodes}, "team", rig.t + 1)


@pytest.mark.parametrize("delay", [None, -1, 900, 1800, True, "0", float("nan"), float("inf")])
def test_bad_delay_does_not_vote(delay):
    st = state_from("quiet")
    st.nodes["climate_sensor_01"]["reading_delay_s"] = delay
    assert not measurement_usable(st, "climate_sensor_01")


@pytest.mark.parametrize("field,value", [("online", False), ("sensor_state", "DEGRADED"), ("status", "UNKNOWN")])
def test_unhealthy_source_does_not_vote(field, value):
    st = state_from("quiet")
    st.nodes["climate_sensor_01"][field] = value
    assert not measurement_usable(st, "climate_sensor_01")


@pytest.mark.parametrize("stamp", [None, True, float("nan")])
def test_unknown_source_time_does_not_vote(stamp):
    st = state_from("quiet")
    st.nodes["climate_sensor_01"]["updated_at"] = stamp
    assert not measurement_usable(st, "climate_sensor_01")


def test_historical_delayed_backups_do_not_disprove_current_high_co2():
    st = state_from("co2_pending")
    a = assess_co2(st, CFG.checks)
    assert a.kind == "unknown" and not a.outliers
    assert set(a.values) == {"climate_sensor_01"}


def test_current_independent_backups_can_detect_outlier():
    st = state_from("co2_pending")
    for n in CFG.checks.co2.sensors:
        st.nodes[n]["reading_delay_s"] = 0  # explicit synthetic simultaneous measurements
        st.nodes[n]["failure_domain"] = f"synthetic-domain:{n}"
    a = assess_co2(st, CFG.checks)
    assert a.kind == "false_sensor" and a.outliers == {"climate_sensor_01"}


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf"), True, "700"])
def test_invalid_numeric_measurement_preserved_but_not_used(number):
    st = state_from("quiet")
    st.nodes["climate_sensor_01"]["co2_ppm"] = number
    assert st.num("climate_sensor_01.co2_ppm") is None
    assert "co2_ppm" in st.nodes["climate_sensor_01"]


@pytest.mark.parametrize("value", [{"fan": -1, "pwm": 50}, {"fan": 6, "pwm": 50},
                                  {"fan": True, "pwm": 50}, {"fan": 1, "pwm": 256},
                                  {"fan": 1, "pwm": 1.5}, {"fan": 1, "pwm": float("nan")}])
def test_nested_actuator_arguments_rejected(tmp_path, value):
    rig = Rig(tmp_path, state_from("quiet"))
    assert rig.go("thermal_insulation_01", "set_fan_pwm", value).status == "rejected"
    assert not rig.sent


def test_last_healthy_sensor_cannot_be_distrusted(tmp_path):
    rig = Rig(tmp_path, state_from("quiet"))
    value = {"node": "climate_sensor_01", "param": "co2_ppm", "trust": "UNRELIABLE"}
    assert "последний" in rig.go("dome_automation_01", "set_data_trust", value).reason


def test_old_frame_and_unchanged_node_clock_cannot_verify(tmp_path):
    rig = Rig(tmp_path, state_from("quiet"))
    rig.go("smart_panel_01", "line_off", 8, expect=Expect.equals("smart_panel_01.lines[7].state", "OFF"))
    set_path(rig.state, "smart_panel_01.lines[7].state", "OFF")
    assert rig.gate.check_pending() == []
    rig.state.update({"nodes": copy.deepcopy(rig.state.nodes)}, "team", rig.t + 1)
    assert rig.gate.check_pending() == []
    frame(rig)
    assert len(rig.gate.check_pending()) == 1
    assert rig.records("verification")[-1]["ok"] is True


def test_rejected_step_prevents_dependent_send(tmp_path):
    rig = Rig(tmp_path, state_from("quiet"))
    runner = object.__new__(Runner)
    runner.cfg, runner.log, runner.gate = CFG, rig.log, rig.gate
    runner.last_decision, runner.advisor = {}, None
    runner.policy, runner.incidents = ControlPolicy('autonomous', True, 'service', 'full'), IncidentCenter()
    intent = Intent(key="unsafe-chain", situation="test", cause="test", confidence=1,
                    steps=[Step("missing", "turn_on"), Step("smart_panel_01", "line_off", 8)])
    asyncio.run(runner.execute(intent, 1000))
    assert not rig.sent


def test_ack_without_effect_defers_dependent_step(tmp_path):
    rig = Rig(tmp_path, state_from("quiet"))
    runner = object.__new__(Runner)
    runner.cfg, runner.log, runner.gate = CFG, rig.log, rig.gate
    runner.last_decision, runner.advisor = {}, None
    runner.policy, runner.incidents = ControlPolicy('autonomous', True, 'service', 'full'), IncidentCenter()
    intent = Intent(key="chain", situation="test", cause="test", confidence=1,
                    steps=[Step("smart_panel_01", "line_off", 8, Expect.equals("smart_panel_01.lines[7].state", "OFF")),
                           Step("smart_panel_01", "line_off", 7)])
    asyncio.run(runner.execute(intent, 1000))
    assert rig.sent == [("smart_panel_01", "line_off", 8)]


def test_persistence_failure_blocks_network(tmp_path):
    rig = Rig(tmp_path, state_from("quiet"))
    rig.gate.checkpoint = lambda: False
    assert rig.go("smart_panel_01", "line_off", 8).status == "rejected"
    assert not rig.sent


@pytest.mark.parametrize("humidity", [None, 50, 100])
def test_e09_never_reset_from_weather_or_timer(tmp_path, humidity):
    st = state_from("quiet")
    st.nodes["solar_inverter_01"].update(error=True, error_code="E09")
    set_path(st, "weather_station_01.humidity_pct", humidity)
    out = EnergyPlaybook().step(make_ctx(st, tmp_path, now=10000, mem={"error_since": 0}))
    assert not any(s.action == "reset_error" for i in out for s in i.steps)
    rig = Rig(tmp_path / "gate", st)
    assert rig.go("solar_inverter_01", "reset_error").status == "rejected"


def test_retry_budget_and_unknown_delivery_survive_restart(tmp_path):
    st = state_from("quiet")
    st.nodes["cnc_01"].update(state="PAUSED", hold_reason="UNDERVOLTAGE", resume_attempts=0)
    st.nodes["smart_panel_01"]["bus_voltage_v"] = 230
    rig = Rig(tmp_path, st)
    rig.go("cnc_01", "resume", expect=expect_for("cnc_01", "resume", None))
    saved = rig.gate.export_runtime()
    other = Rig(tmp_path / "restarted", st)
    other.gate.import_runtime(saved)
    assert other.gate.is_pending("cnc_01")
    assert other.gate.resume_attempts("cnc_01", "UNDERVOLTAGE") == 1
    assert other.go("cnc_01", "resume").status == "rejected"


def test_two_metrics_on_one_board_are_one_independent_source():
    ev = [Ev("co", "false", 1, "", "air_quality_01"), Ev("voc", "false", 1, "", "air_quality_01")]
    assert independent_sources(ev, "false") == 1


def test_unknown_effect_and_null_alarm_never_verify():
    st = state_from("quiet")
    assert not expect_for("edge_compute_01", "unknown_action", None).check(st)
    st.nodes["smoke_detector_01"]["alarm"] = None
    st.nodes["smoke_detector_01"].pop("alarm_state", None)
    assert not expect_for("smoke_detector_01", "reset_alarm", None).check(st)


def test_global_source_clock_freeze_is_detected_even_when_frames_arrive():
    st = state_from("quiet")
    same = copy.deepcopy(st.nodes)
    st.update({"nodes": same}, "team", st.wall + 40)
    assert st.is_frozen("climate_sensor_01")


def test_resume_verifies_same_job_and_progress_not_just_running(tmp_path):
    st = state_from("quiet")
    st.nodes["cnc_01"].update(state="PAUSED", hold_reason="UNDERVOLTAGE", progress_pct=20, homed=True)
    st.nodes["smart_panel_01"]["bus_voltage_v"] = 230
    rig = Rig(tmp_path, st)
    rig.go("cnc_01", "resume", expect=expect_for("cnc_01", "resume", None))
    st.nodes["cnc_01"].update(state="RUNNING", hold_reason=None)
    frame(rig)
    assert not rig.gate.check_pending()  # running without progress is not completion
    assert rig.gate.resume_attempts("cnc_01", "UNDERVOLTAGE") == 1
    st.nodes["cnc_01"]["progress_pct"] = 21
    file_name = st.nodes["cnc_01"]["file_name"]
    st.nodes["cnc_01"]["file_name"] = "different.nc"
    frame(rig)
    assert not rig.gate.check_pending()
    st.nodes["cnc_01"]["file_name"] = file_name
    frame(rig)
    assert rig.gate.check_pending()[0].verified is True


@pytest.mark.parametrize("kind", ["IL_CO2", "IL_SMOKE", "IL_OVERCURRENT"])
def test_missing_or_unproven_cause_cannot_reset_protection(tmp_path, kind):
    st = state_from("quiet")
    if kind == "IL_SMOKE":
        st.nodes.pop("smoke_detector_01")
    rig = Rig(tmp_path, st)
    assert rig.go("dome_automation_01", "reset_interlock", kind).status == "rejected"


def test_unknown_bed_temperature_cannot_unlock_box(tmp_path):
    st = state_from("quiet")
    st.nodes["printer_3d_01"]["bed_temp_c"] = None
    rig = Rig(tmp_path, st)
    assert rig.go("thermal_insulation_01", "set_lock", {"lock": 0, "locked": False}).status == "rejected"


def test_smoke_ack_cannot_cancel_unbound_event(tmp_path):
    st = state_from("quiet")
    st.nodes["dome_automation_01"]["events"] = [{"event_id": "alarm-current", "interlock": "IL_SMOKE", "type": "TRIP"}]
    rig = Rig(tmp_path, st)
    assert rig.go("dome_automation_01", "acknowledge", {"event_id": "alarm-current"}).status == "rejected"
    assert not rig.sent


def test_model_cannot_grant_itself_confirmed_manual_mode():
    from kupol.dome.brain import to_intent
    from llm_agent.dispatcher import Fired
    from llm_agent.schemas import ActionVerdict, AgentDecision, AgentResponse, LLMCallInfo, ProposedAction
    action = ProposedAction.model_validate({"node_id": "solar_panels_01", "action": "disable_all_mppt", "confirmed": True})
    response = AgentResponse(ok=True, decision=AgentDecision(situation="test", actions=[action]),
                             llm_call=LLMCallInfo(model="offline", ok=True),
                             verdicts=[ActionVerdict(action=action, accepted=True)])
    intent = to_intent(response, Fired([], ""))
    assert not hasattr(intent.steps[0], "confirmed")


@pytest.mark.parametrize("failure", ["no_effect", "session_change"])
def test_plan_does_not_continue_after_missing_effect_or_session_change(tmp_path, failure):
    from tests.dome_fake.world import World
    from tests.test_dome_scenarios import Harness, run
    async def scenario():
        world = World()
        async with Harness(tmp_path, world) as harness:
            harness.runner.playbooks = []
            await harness.step()  # admit the first authenticated telemetry frame
            if failure == "no_effect":
                harness.dome.on_control = lambda msg: {"success": True}  # ACK only, no physical effect
            intent = Intent(key="plan-test", situation="test", cause="test", confidence=1,
                            steps=[Step("smart_panel_01", "line_off", 8, Expect.equals("smart_panel_01.lines[7].state", "OFF")),
                                   Step("smart_panel_01", "line_off", 6, Expect.equals("smart_panel_01.lines[5].state", "OFF"))])
            await harness.runner.execute(intent, world.clock())
            if failure == "session_change":
                harness.runner.client.session_id = "different-session"
            await harness.step(15)
            assert [(m["action"], m.get("value")) for m in harness.sent()] == [("line_off", 8)]
    run(scenario())
