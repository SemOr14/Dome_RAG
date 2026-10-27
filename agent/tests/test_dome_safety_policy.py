"""Safety admission regressions. Synthetic telemetry, local Gate, no live model/control."""
import copy

import pytest

from kupol.dome.checks import assess_co2, co2_recovery_safe, condensate_recovery_safe
from kupol.dome.evidence import independent_group
from kupol.dome.gate import Expect
from kupol.dome.incidents import IncidentCenter
from kupol.dome.playbooks.air import Co2Playbook, CondensatePlaybook
from kupol.dome.playbooks.devices import AlarmPlaybook
from kupol.dome.playbooks.energy import EnergyPlaybook
from kupol.dome.playbooks.lines import OvercurrentPlaybook
from kupol.dome.playbooks.machines import SmokePlaybook, cause_cleared
from tests.dome_helpers import make_ctx, state_from, synchronous_state_from
from tests.test_dome_gate import Rig


def commands(out):
    return [(s.node, s.action) for i in out for s in i.steps]


@pytest.mark.parametrize('node', ['supply_ventilation_01','fume_extraction_01','equipment_cooling_01','water_pump_01'])
def test_unknown_device_fault_never_reset_or_reenabled(tmp_path, node):
    st = synchronous_state_from()
    st.nodes[node].update(alarm=True, alarm_code='MOTOR_OVERHEAT')
    mem = {node: [1000]}
    for now in (1000, 1400, 6000):
        out = AlarmPlaybook().step(make_ctx(st, tmp_path, now=now, mem=mem, texts=['всё нормально']))
        assert out and not commands(out) and out[0].human_escalation['required_action']
    st.nodes[node]['alarm'] = False
    assert not commands(AlarmPlaybook().step(make_ctx(st, tmp_path, now=1001, mem=mem)))


@pytest.mark.parametrize('now', [1000, 1200, 10000])
def test_overcurrent_wait_never_substitutes_for_cause_clearance(tmp_path, now):
    st = synchronous_state_from()
    st.nodes['dome_automation_01']['interlocks']['IL_OVERCURRENT']['state'] = 'TRIPPED'
    out = OvercurrentPlaybook().step(make_ctx(st, tmp_path, now=now, mem={'tripped_since': 0}))
    assert not commands(out) and out[0].human_escalation


def test_soc_recovery_does_not_prove_safe_restored_load(tmp_path):
    st = synchronous_state_from()
    st.nodes['solar_inverter_01'].update(battery_soc_pct=90)
    st.nodes['dome_automation_01']['interlocks']['IL_LOAD_SHED']['state'] = 'TRIPPED'
    out = EnergyPlaybook()._load_shed(make_ctx(st, tmp_path))
    assert not commands(out) and out[0].human_escalation
    assert 'пусковую' in out[0].human_escalation['required_action']


@pytest.mark.parametrize('actor', ['algorithm', 'agent'])
@pytest.mark.parametrize('node,action,value', [
    ('fume_extraction_01','reset_alarm',None),
    ('supply_ventilation_01','reset_alarm',None),
    ('solar_inverter_01','reset_error',None),
    ('smart_panel_01','reset_protection',3),
    ('smart_panel_01','line_off',7),
    ('dome_automation_01','reset_interlock','IL_OVERCURRENT'),
    ('dome_automation_01','reset_interlock','IL_LOAD_SHED'),
    ('dome_automation_01','reset_interlock','IL_SMOKE'),
    ('dome_automation_01','reset_interlock','IL_STORM'),
])
def test_model_and_algorithms_have_identical_critical_admission(tmp_path, actor, node, action, value):
    rig = Rig(tmp_path, synchronous_state_from())
    result = rig.go(node, action, value, actor=actor)
    assert result.status == 'rejected' and not rig.sent


@pytest.mark.parametrize('topology', ['missing','shared','other_zone','delayed','untrusted'])
def test_no_critical_source_exclusion_without_independent_current_comparison(tmp_path, topology):
    st = synchronous_state_from('co2_pending')
    if topology == 'missing':
        for n in ('climate_sensor_01','climate_sensor_02','climate_sensor_03'):
            st.nodes[n].pop('failure_domain')
    elif topology == 'shared':
        st.nodes['climate_sensor_03']['failure_domain'] = st.nodes['climate_sensor_02']['failure_domain']
    elif topology == 'other_zone':
        st.nodes['climate_sensor_03']['zone'] = 'another_room'
    elif topology == 'delayed':
        st.nodes['climate_sensor_03']['reading_delay_s'] = 900
    else:
        st.nodes['dome_automation_01']['data_trust']['climate_sensor_03.co2_ppm'] = 'UNRELIABLE'
    assert assess_co2(st, make_ctx(st, tmp_path).cfg.checks).kind != 'false_sensor'
    assert not commands(Co2Playbook().step(make_ctx(st, tmp_path)))
    rig = Rig(tmp_path / 'gate', st)
    assert rig.go('dome_automation_01','set_data_trust',
        {'node':'climate_sensor_01','param':'co2_ppm','trust':'UNRELIABLE'}).status == 'rejected'


def test_positive_independent_co2_exclusion_still_requires_separate_recovery(tmp_path):
    st = synchronous_state_from('co2_pending')
    cfg = make_ctx(st, tmp_path).cfg.checks
    assert assess_co2(st, cfg).kind == 'false_sensor'
    assert not co2_recovery_safe(st, cfg)  # current primary remains suspect
    out = Co2Playbook().step(make_ctx(st, tmp_path))
    assert commands(out) and all(a != 'reset_interlock' for _,a in commands(out))
    rig = Rig(tmp_path / 'gate', st)
    assert rig.go('dome_automation_01', 'set_data_trust',
        {'node':'climate_sensor_01','param':'co2_ppm','trust':'UNRELIABLE'}).status == 'sent'


def test_critical_recovery_rejects_missing_dew_point_and_unverified_domains(tmp_path):
    st = synchronous_state_from()
    cfg = make_ctx(st, tmp_path).cfg.checks
    assert co2_recovery_safe(st, cfg) and condensate_recovery_safe(st, cfg)
    for n in ('climate_sensor_01','climate_sensor_02','climate_sensor_03'):
        st.nodes[n].pop('dew_point_c')
    assert not condensate_recovery_safe(st, cfg)
    st.nodes['dome_automation_01']['interlocks']['IL_CONDENSATE']['state'] = 'TRIPPED'
    assert not commands(CondensatePlaybook().step(make_ctx(st, tmp_path)))
    rig = Rig(tmp_path / 'gate', st)
    assert rig.go('dome_automation_01','reset_interlock','IL_CONDENSATE').status == 'rejected'
    raw = state_from()
    assert not independent_group(raw, ['climate_sensor_01','climate_sensor_02'])
    assert not co2_recovery_safe(raw, cfg)


def test_general_chat_and_quiet_sensors_do_not_cancel_fire_protection(tmp_path):
    st = synchronous_state_from()
    st.nodes['smoke_detector_01'].update(smoke_detected=True, alarm_state='ALARM')
    out = SmokePlaybook().step(make_ctx(st,tmp_path,texts=['всё нормально, ложная тревога']))
    assert not commands(out) and out[0].human_escalation['severity'] == 'red'
    ctx = make_ctx(st,tmp_path,mem={'smoke_clear_since':0},now=10000)
    st.nodes['dome_automation_01']['interlocks']['IL_SMOKE']['state'] = 'ARMED'
    assert not cause_cleared(ctx,'IL_SMOKE')[0]


@pytest.mark.parametrize('new_data', [True, False])
def test_failed_effect_cannot_be_retried_after_cooldown_or_restart(tmp_path, new_data):
    rig = Rig(tmp_path, synchronous_state_from())
    effect = Expect.equals('smart_panel_01.lines[7].state','OFF')
    assert rig.go('smart_panel_01','line_off',8,expect=effect).status == 'sent'
    rig.t += 100
    if new_data:
        nodes = copy.deepcopy(rig.state.nodes)
        for b in nodes.values():
            b['updated_at'] += 1
        rig.state.update({'nodes':nodes}, 'team', rig.t)
    assert rig.gate.check_pending()[0].verified is False
    saved = rig.gate.export_runtime()
    rig.t += 10000
    assert rig.go('smart_panel_01','line_off',8,expect=effect).status == 'rejected'
    restored = Rig(tmp_path / 'restored',rig.state)
    restored.gate.import_runtime(saved)
    assert restored.go('smart_panel_01','line_off',8,expect=effect).status == 'rejected'
    assert not restored.sent


def test_symptom_recovery_and_ack_do_not_close_physical_incident():
    events=[]
    center=IncidentCenter(lambda kind,payload,**kw:events.append({'payload':copy.deepcopy(payload)}))
    item=center.open('physical','red','pump','overheat','inspect')
    center.acknowledge(item.incident_id)
    center.observe_recovery('physical')
    assert item.status == 'acknowledged' and item.facts['recovery_observed']
    assert not center.resolve('physical')
    replay=IncidentCenter(events=events)
    assert 'physical' in replay.active
    review=dict(state_verified=True,dependencies_verified=True,cause_resolved=True,
        evidence=[{'inspection':'defect repaired and operation verified'}],residual_risks=[],
        follow_up=['inspect filter next shift'],reviewed_by='operator')
    assert center.resolve('physical',review=review) and item.status == 'resolved'


def test_line_on_cannot_bypass_tripped_electrical_protection(tmp_path):
    st=synchronous_state_from()
    st.line(3)['state']='TRIPPED'
    rig=Rig(tmp_path,st)
    assert rig.go('smart_panel_01','line_on',3).status=='rejected' and not rig.sent


def test_box_opening_requires_fresh_dependent_printer(tmp_path):
    st=synchronous_state_from()
    st.nodes['printer_3d_01']['status']='UNKNOWN'
    rig=Rig(tmp_path,st)
    assert rig.go('thermal_insulation_01','set_lock',{'lock':0,'locked':False}).status=='rejected'
    assert not rig.sent
