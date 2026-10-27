"""Frozen probe families plus transfer/benign counterexamples. No equipment or live model."""
import runpy
from pathlib import Path
import pytest
from llm_agent.triggers import TelemetryWatch
from llm_agent.monitoring import NumericMonitor

probe = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'research/generalization-2026-10-27/probe.py'))

@pytest.mark.parametrize('seed', range(5))
def test_frozen_probe_families(seed):
    rows = probe['evaluate'](seed)
    assert all(r['detected'] == r['anomaly'] for r in rows), [r for r in rows if r['detected'] != r['anomaly']]

@pytest.mark.parametrize('target,unit', [(800, 'rpm'), (-600, 'rpm'), (12, 'bar')])
def test_nonzero_tracking_failure_and_recovery_with_other_channels(target, unit):
    watch = TelemetryWatch(30, 180)
    findings = []
    for i in range(8):
        b = {'state':'RUNNING', f'axis_target_{unit}':target, f'axis_actual_{unit}':target*.4,
             'updated_at':i*60, 'online':True, 'status':'OK'}
        findings += watch.observe({'timestamp':i*60,'nodes':{'new_axis_99':b}}, {}, i*60)
    assert any(t.key.startswith('tracking|') for t in findings)
    b[f'axis_actual_{unit}'] = target
    b['updated_at'] = 480
    watch.observe({'timestamp':480, 'nodes':{'new_axis_99':b}}, {}, 480)
    assert not watch.target_active


def test_long_converging_ramp_is_not_a_persistent_failure():
    watch = TelemetryWatch(30, 180)
    findings = []
    for i in range(30):
        b = {'state':'RUNNING','axis_target_rpm':1000,'axis_rpm':min(i*50,1000),'updated_at':i*60}
        findings += watch.observe({'timestamp':i*60,'nodes':{'ramping_axis':b}}, {}, i*60)
    assert not any(t.key.startswith('tracking|') for t in findings)

@pytest.mark.parametrize('code', ['ok', 'none', '0', '', None, 0, False, 'NO_ERROR'])
def test_nested_neutral_codes_are_not_faults(code):
    b = {'modules':[{'controller':{'error_code':code}}], 'updated_at':1}
    assert not TelemetryWatch(30,180).observe({'timestamp':1,'nodes':{'extension':b}}, {}, 1)


def test_peer_offsets_learned_without_declaring_the_low_sensor_bad():
    monitor = NumericMonitor()
    for i in range(60):
        # Same group, known stable spatial offset, all readings move together.
        nodes = {name:{'zone':'test','pressure_bar':base+(i%5)*.005,'updated_at':i*60}
                 for name,base in [('a',2),('b',3),('c',3)]}
        assert not any(a['kind']=='conflict' for a in monitor.observe(nodes,i))


def test_tracking_timeout_cannot_confirm_from_a_frozen_last_sample():
    watch = TelemetryWatch(30, 180)
    for stamp in (0, 30, 60, 90):
        snapshot = {'timestamp':stamp,'nodes':{'axis':{'state':'RUNNING','axis_target_rpm':1000,'axis_rpm':500,'updated_at':stamp}}}
        watch.observe(snapshot, {}, stamp)
    assert not any(t.key.startswith('tracking|') for t in watch.observe(snapshot, {}, 180))


def test_array_elements_are_one_trigger_and_repeats_are_quiet():
    from llm_agent.triggers import REPEAT_QUIET_S, TelemetryWatch

    def frame(t, rpm):
        return {"timestamp": t, "observed_at": t, "nodes": {"fan_box_01": {"online": True, "updated_at": t, "fans_rpm": rpm}}}

    watch = TelemetryWatch(stale_s=30, stall_s=300)
    now, t = 0.0, 1000.0
    for _ in range(15):
        watch.observe(frame(t, [1200.0] * 6), {}, now)
        now, t = now + 60, t + 60
    first = [x for x in watch.observe(frame(t, [None] * 6), {}, now) if x.kind in ("numeric", "conflict")]
    assert len(first) == 1  # шесть элементов массива — один повод
    now, t = now + 60, t + 60
    watch.observe(frame(t, [1200.0] * 6), {}, now)
    for _ in range(4):  # условие снялось и вернулось
        now, t = now + 60, t + 60
        watch.observe(frame(t, [1200.0] * 6), {}, now)
    again = [x for x in watch.observe(frame(t + 60, [None] * 6), {}, now + 60) if x.kind in ("numeric", "conflict")]
    assert again == [] and now < REPEAT_QUIET_S  # повтор в пределах паузы не отдаётся модели
