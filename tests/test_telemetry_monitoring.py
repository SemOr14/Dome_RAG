from llm_agent.monitoring import NumericMonitor
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


def test_legacy_model_response_reads_as_unknown_impact():
    legacy = AgentDecision(situation='legacy response')
    assert legacy.impact_kind == 'unknown' and legacy.authenticity == 'unknown' and legacy.causal_chain == []
