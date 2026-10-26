from llm_agent.monitoring import NumericMonitor, assess_impacts
from llm_agent.schemas import AgentDecision


def test_unknown_nested_numeric_field_jump_null_and_recovery():
    monitor = NumericMonitor()
    for i in range(12):
        assert not monitor.observe({'new_device': {'updated_at': i, 'channels': [{'pressure': 10}]}}, i)
    assert monitor.observe({'new_device': {'updated_at': 12, 'channels': [{'pressure': 80}]}}, 12)[0]['field'] == 'channels[0].pressure'
    # Polling the same source update must neither train nor clear the warning.
    before = monitor.values_seen
    for i in range(10):
        monitor.observe({'new_device': {'updated_at': 12, 'channels': [{'pressure': 10}]}}, 20+i)
    assert monitor.values_seen == before and monitor.active
    for i in range(3):
        monitor.observe({'new_device': {'updated_at': 30+i, 'channels': [{'pressure': 10}]}}, 30+i)
    assert not monitor.active
    assert monitor.observe({'new_device': {'updated_at': 33, 'channels': [{'pressure': None}]}}, 33)
    assert monitor.observe({'new_device': {'updated_at': 34}}, 34)  # a missing known measurement is a gap too


def test_immediate_limits_and_nonfinite_measurement_do_not_train():
    monitor = NumericMonitor()
    assert monitor.observe({'battery_01': {'soc_pct': 20, 'voltage_v': float('nan')}}, 1)
    assert len(monitor.active) == 2
    assert not monitor.baselines['battery_01', 'voltage_v']
    assert not monitor.baselines['battery_01', 'soc_pct']


def snapshot():
    return {'timestamp': 100, 'nodes': {
        **{f'climate_sensor_0{i}': {'online': True, 'status': 'OK', 'updated_at': 100, 'co2_ppm': v}
           for i, v in enumerate((3000, 800, 810), 1)},
        'dome_automation_01': {'interlocks': {'IL_CO2': {'state': 'TRIPPED'}}},
        'cnc_01': {'hold_reason': 'IL_CO2'},
    }}


def test_false_signal_and_cascade_are_independent_and_stale_peers_cannot_disprove_alarm():
    frame = snapshot()
    assessments = assess_impacts(frame)
    assert {a['kind'] for a in assessments} == {'false', 'cascade'}
    frame['nodes']['climate_sensor_02']['updated_at'] = 10
    assert 'false' not in {a['kind'] for a in assess_impacts(frame)}
    frame = snapshot()
    for i in (1, 2, 3):
        frame['nodes'][f'climate_sensor_0{i}']['co2_ppm'] = 2100
    assert 'base' in {a['kind'] for a in assess_impacts(frame)}


def test_legacy_model_response_reads_as_unknown_impact():
    legacy = AgentDecision(situation='legacy response')
    assert legacy.impact_kind == 'unknown' and legacy.authenticity == 'unknown' and legacy.causal_chain == []


def test_prompt_includes_classification_without_external_rag(tmp_path):
    from llm_agent import DomeLLMAgent, Settings
    agent = DomeLLMAgent.from_files(Settings(deepseek_api_key=None, rag_backend='none', state_dir=tmp_path))
    prompt = agent.build_prompt('IL_CO2 hold_reason: сравни датчики и каскад')
    assert '# Разделение воздействий' in prompt.messages[1]['content']
    assert '# Подходящие сценарии' not in prompt.messages[1]['content']  # сценарии — только через RAG
    assert 'impact_kind' in prompt.messages[0]['content']
    assert 'suspected_false' in prompt.messages[0]['content']
    frame = snapshot()
    frame['telemetry_age_s'] = 121
    assert 'false' not in {a['kind'] for a in assess_impacts(frame)}


def test_untrusted_or_different_zone_peers_cannot_disprove_real_co2():
    frame = snapshot()
    frame['nodes']['dome_automation_01']['data_trust'] = {'climate_sensor_02.co2_ppm': 'UNRELIABLE'}
    assert 'false' not in {a['kind'] for a in assess_impacts(frame)}
    frame = snapshot()
    frame['nodes']['climate_sensor_02']['zone'] = 'OTHER_ROOM'
    assert 'false' not in {a['kind'] for a in assess_impacts(frame)}
