"""Positive evidence and benign counterexamples for the log-gap fixes; no LLM or equipment."""
import copy

import pytest

from llm_agent.context.telemetry import compact_telemetry
from llm_agent.event_watch import EventWatch
from llm_agent.monitoring import NumericMonitor
from llm_agent.triggers import TelemetryWatch


def fresh(nodes, stamp):
    return {k: dict(v, updated_at=stamp, online=True, status='OK') for k, v in nodes.items()}


@pytest.mark.parametrize('node,field,values', [
    ('wind_turbine_01', 'power_w', [0, 900, 40, 1100]),
    ('wind_turbine_01', 'rpm', [0, 18, 0, 100]),
    ('battery_01', 'current_a', [-12, 15, -.3, .3]),
    ('solar_inverter_01', 'pv_power_w', [0, 1000, 800, 0]),
    ('water_pump_01', 'flow_l_min', [0, 20, 0, 25]),
    ('supply_ventilation_01', 'power_w', [0, 100, 40, 200]),
    ('radio_01', 'bitrate_kbps', [10, 512, 32, 1024]),
    ('weather_station_01', 'wind_direction_deg', [355, 359, 1, 5]),
])
def test_normal_cycles_signed_current_and_angle_wrap_do_not_alarm(node, field, values):
    monitor = NumericMonitor()
    for i in range(100):
        assert not monitor.observe(fresh({node: {field: values[i % len(values)]}}, i * 60), i)


def test_heater_warmup_and_cooling_are_quiet_but_overshoot_is_evidence():
    m = NumericMonitor()
    for i in range(24):
        body = {'nozzle_target_c': 210, 'heater_power_pct': 100 if i < 20 else 27,
                'nozzle_temp_c': min(210, 20 + i * 10)}
        assert not m.observe(fresh({'printer_3d_01': body}, i), i)
    body.update(nozzle_temp_c=280, heater_power_pct=27)
    found = m.observe(fresh({'printer_3d_01': body}, 24), 24)
    assert any(a['field'] == 'nozzle_temp_c' and a['kind'] == 'conflict' for a in found)
    body.update(nozzle_target_c=0, heater_power_pct=0)
    for i in range(25, 28):
        found = m.observe(fresh({'printer_3d_01': body}, i), i)
    assert not found


def test_bms_restriction_at_low_soc_but_normal_end_of_charge_is_quiet():
    nodes = {'battery_01': {'soc_pct': 77, 'cell_voltage_delta_mv': 63},
             'solar_inverter_01': {'battery_soc_pct': 77, 'balancing_state': 'BALANCING', 'battery_charge_blocked': True}}
    assert any(a['param'] == 'balancing_state' for a in compact_telemetry({'nodes': fresh(nodes, 1)}, {}).anomalies)
    assert any(a['field'] == 'cell_voltage_delta_mv' for a in NumericMonitor().observe(fresh(nodes, 1), 1))
    nodes['battery_01']['soc_pct'] = 99
    nodes['solar_inverter_01']['battery_soc_pct'] = 99
    assert not compact_telemetry({'nodes': fresh(nodes, 2)}, {}).anomalies
    assert not NumericMonitor().observe(fresh(nodes, 2), 2)


def test_fan_off_is_normal_but_failed_fan_is_immediate():
    nodes = {'solar_inverter_01': {'fan_state': 'OFF', 'fan_current_a': 0, 'inverter_temp_c': 42}}
    watch = TelemetryWatch(30, 180)
    assert not watch.observe({'timestamp': 1, 'nodes': fresh(nodes, 1)}, {}, 1)
    nodes['solar_inverter_01']['fan_state'] = 'FAULT'
    found = watch.observe({'timestamp': 2, 'nodes': fresh(nodes, 2)}, {}, 2)
    assert any(t.kind == 'alarm' and 'fan_state' in t.text for t in found)


def test_frozen_vane_requires_wind_new_source_updates_and_elapsed_time():
    def node(stamp, speed=3):
        return fresh({'weather_station_01': {'wind_speed_ms': speed, 'wind_direction_deg': 31}}, stamp)
    stuck, calm, stale = NumericMonitor(), NumericMonitor(), NumericMonitor()
    for i in range(30):
        a = stuck.observe(node(i * 60), i)
        assert not calm.observe(node(i * 60, speed=0), i)
        assert not stale.observe(node(0), i * 60)
        if i < 20:
            assert not a
    assert any(x['kind'] == 'stuck' for x in a)
    # A gap in collection cannot be used as continuous frozen-value evidence.
    gap = NumericMonitor()
    for i in range(10):
        gap.observe(node(i * 60), i)
    assert not gap.observe(node(7200), 7200)


def test_mass_unknown_is_one_reason_with_edge_or_sfp_evidence():
    for cause in ('edge', 'sfp'):
        nodes = {f'device_{i}': {'status': 'UNKNOWN', 'online': True} for i in range(30)}
        nodes.update(fresh({'edge_compute_01': {'services': {'telemetry_collector': 'FAILED' if cause == 'edge' else 'RUNNING'}},
                            'network_link_01': {'rx_power_dbm': -8 if cause == 'edge' else -14, 'link_flaps': 2}}, 1))
        found = TelemetryWatch(30, 180).observe({'timestamp': 1, 'nodes': nodes}, {}, 1)
        stale = [t for t in found if t.kind == 'stale']
        assert len(stale) == 1 and len(stale[0].meta['affected_nodes']) == 30
        assert ('telemetry_collector=FAILED' if cause == 'edge' else 'SFP') in stale[0].text


def test_clock_skew_is_not_classified_as_an_edge_outage():
    nodes = fresh({f'node_{i}': {} for i in range(10)}, 7201)
    nodes.update(fresh({'edge_compute_01': {'clock_offset_s': 7200}}, 1))
    found = TelemetryWatch(30, 180).observe({'timestamp': 7201, 'nodes': nodes}, {}, 1)
    assert any('clock_offset_s' in t.text for t in found)
    assert not any('Массовый UNKNOWN' in t.text for t in found)


def test_leak_warning_precedes_trip_and_recovery_does_not_warn():
    m = NumericMonitor()
    def frame(leak, stamp):
        return fresh({'smart_panel_01': {'lines': [{'line': 4, 'state': 'ON', 'leakage_ma': leak}]}}, stamp)
    for i in range(20):
        assert not m.observe(frame(.8, i), i)
    assert m.observe(frame(11, 20), 20)[0]['field'] == 'lines[0].leakage_ma'
    for i in range(21, 24):
        found = m.observe(frame(.8, i), i)
    assert not found


def test_threshold_changes_do_not_reissue_a_leak_case_every_frame():
    watch = TelemetryWatch(30, 180)
    for i, leak in enumerate((11, 12, 17, 29)):
        nodes = fresh({'smart_panel_01': {'lines': [{'line': 4, 'state': 'ON', 'leakage_ma': leak}]}}, i)
        found = watch.observe({'timestamp': i, 'nodes': nodes}, {}, i)
        assert bool(found) == (i == 0)


def test_short_protection_trip_survives_return_to_on_and_event_redelivery():
    watch = EventWatch()
    snapshot = {'nodes': {'smart_panel_01': {'lines': [{'line': 1, 'state': 'ON', 'leakage_ma': .8}]}}}
    trip = {'event_id': 'trip', 'event_type': 'change', 'payload': {
        'kind': 'change', 'node_id': 'smart_panel_01', 'param': 'lines[0].state', 'from': 'ON', 'to': 'TRIPPED'}}
    clear = {'event_id': 'clear', 'event_type': 'change', 'payload': {
        'kind': 'change', 'node_id': 'smart_panel_01', 'param': 'lines[0].state', 'from': 'TRIPPED', 'to': 'ON'}}
    found = watch.observe([trip, clear], snapshot)
    assert len(found) == 1 and found[0].event_ids == ['trip']
    assert 'TRIPPED' in found[0].text
    assert not watch.observe([trip, clear], snapshot)


def test_automation_history_and_ack_updates_are_not_new_trips():
    watch = EventWatch()
    event = {'event_id': 'AUT-1', 't': 100, 'type': 'TRIP', 'interlock': 'IL_SMOKE', 'message': 'Дым'}
    first = {'timestamp': 200, 'nodes': {'dome_automation_01': {'events': [event]}}}
    assert not watch.from_snapshot(first)
    first['timestamp'] = 201
    assert not watch.from_snapshot(first)
    current = copy.deepcopy(event); current.update(event_id='AUT-2', t=202)
    first['nodes']['dome_automation_01']['events'].append(current); first['timestamp'] = 203
    assert len(watch.from_snapshot(first)) == 1
    redelivered = {'event_id': 'different-envelope', 'event_type': 'automation',
                   'payload': {'event': dict(current, acknowledged=True), 'updated': True}}
    assert not watch.observe([redelivered], first)


def test_stale_node_does_not_produce_numeric_gaps_or_old_alarms():
    m = NumericMonitor()
    m.observe(fresh({'battery_01': {'temperature_c': 70}}, 1), 1)
    assert m.active
    nodes = fresh({'battery_01': {'temperature_c': None}}, 1)
    nodes.update(fresh({'other': {'pressure': 10}}, 100))
    assert not m.observe(nodes, 100)


def test_raw_websocket_change_is_also_consumed_without_a_db_envelope():
    event = {'type': 'change', 'node_id': 'cnc_01', 'param': 'hold_reason', 'from': None, 'to': 'UNDERVOLTAGE'}
    found = EventWatch().observe([event], {'nodes': {}})
    assert len(found) == 1 and 'просадка напряжения' in found[0].text


@pytest.mark.parametrize('node', ['pump_sector_B', 'custom_machine_72', 'new_device_without_history'])
def test_nested_faults_on_unseen_devices_reach_dispatcher(node):
    nodes = fresh({node: {'modules': [{'driver_state': 'MOTOR_OVERHEAT'}]}}, 100)
    found = TelemetryWatch(30, 180).observe({'timestamp': 100, 'nodes': nodes}, {}, 100)
    assert any(t.kind == 'alarm' and node in t.nodes and 'modules[0].driver_state' in t.text for t in found)


@pytest.mark.parametrize('node,target,actual', [('extruder_X', 185, 260), ('hotplate_Y', 90, 150)])
def test_same_physical_conflict_at_other_ids_and_values(node, target, actual):
    nodes = fresh({node: {'nozzle_temp_c': actual, 'nozzle_target_c': target, 'heater_power_pct': 20}}, 1)
    found = compact_telemetry({'nodes': nodes}, {}).anomalies
    assert any(a['node'] == node and a['param'] == 'nozzle_temp_c' for a in found)


@pytest.mark.parametrize('setpoint', [3000, -1200])
def test_unknown_running_machine_no_response_to_target_has_grace_and_freshness(setpoint):
    watch = TelemetryWatch(30, 180)
    def frame(stamp, actual=0):
        return {'timestamp': stamp, 'nodes': fresh({'new_mixer': {
            'state': 'RUNNING', 'motor_target_rpm': setpoint, 'motor_rpm': actual}}, stamp)}
    assert not watch.observe(frame(1), {}, 0)
    # Repeated polling cannot confirm a stalled actuator from one measurement.
    assert not any(t.key.startswith('tracking|') for t in watch.observe(frame(1), {}, 180))
    found = []
    for i in range(1, 5):
        found += watch.observe(frame(i * 60), {}, i * 60)
    assert sum(t.key.startswith('tracking|new_mixer|') for t in found) == 1
    assert not any(t.key.startswith('tracking|') for t in watch.observe(frame(300, actual=setpoint * .6), {}, 300))


def test_unknown_process_progress_stall_without_printer_or_cnc_id():
    watch = TelemetryWatch(30, 120)
    def snapshot(t, progress):
        return {'timestamp': t, 'nodes': fresh({'custom_conveyor': {'state': 'MOVING', 'progress_pct': progress}}, t)}
    watch.observe(snapshot(1, 42), {}, 0)
    found = watch.observe(snapshot(121, 42), {}, 121)
    assert any(t.kind == 'operation' and 'custom_conveyor' in t.nodes for t in found)
    watch.observe(snapshot(130, 43), {}, 130)
    assert not any(t.kind == 'operation' for t in watch.observe(snapshot(200, 45), {}, 200))


def test_new_same_zone_sensor_frozen_against_varying_peer():
    frozen, healthy = NumericMonitor(), NumericMonitor()
    for i in range(30):
        peer = 40 + (i % 8) * 2
        nodes = fresh({'sensor_new_A': {'zone': 'test_zone', 'temperature_c': 15},
                       'sensor_new_B': {'zone': 'test_zone', 'temperature_c': peer}}, i * 60)
        a = frozen.observe(nodes, i)
        assert not any(x['kind'] == 'stuck' for x in healthy.observe(fresh({
            'stable_controlled_sensor': {'zone': 'other_zone', 'temperature_c': 15}}, i * 60), i))
    assert any(x['node'] == 'sensor_new_A' and x['kind'] == 'stuck' for x in a)


def test_custom_limits_are_configurable_and_do_not_replace_statistical_checks():
    monitor = NumericMonitor(limits={('new_pipe', 'pressure_bar'): (1, 6)})
    assert not monitor.observe(fresh({'new_pipe': {'pressure_bar': 4}}, 1), 1)
    assert monitor.observe(fresh({'new_pipe': {'pressure_bar': 8}}, 2), 2)[0]['kind'] == 'limit'


def test_unseen_failure_reaches_model_without_a_matching_rag_card(tmp_path):
    from test_dispatcher import make
    snapshot = {'timestamp': 1, 'nodes': fresh({'new_cryostat': {'cooling': {'controller_state': 'FAULT'}}}, 1)}
    dispatcher, llm, clock = make(tmp_path, snapshot)
    assert dispatcher.tick() and len(llm.calls) == 1
    assert 'new_cryostat.cooling.controller_state' in llm.calls[0]
    assert 'Новая аномалия может не иметь карточки' in llm.calls[0]


@pytest.mark.parametrize('baseline,spike', [(.04, .4), (2.5, 9), (120, 400)])
def test_unknown_measurement_scales_without_a_universal_one_unit_floor(baseline, spike):
    monitor = NumericMonitor()
    for i in range(15):
        assert not monitor.observe(fresh({'new_sensor_scale': {'pressure_bar': baseline}}, i), i)
    assert monitor.observe(fresh({'new_sensor_scale': {'pressure_bar': spike}}, 15), 15)
