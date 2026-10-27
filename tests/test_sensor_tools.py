import copy
import json

import httpx
import pytest
from openai import OpenAI

from llm_agent import DomeLLMAgent, Settings
from llm_agent.context.catalog import NodeCatalog
from llm_agent.llm_client import DeepSeekClient, LLMResult
from llm_agent.sensor_tools import SENSOR_TOOL, SensorSession

NODE = 'climate_sensor_01'


def call(index=1, node=NODE, fields=None, name='read_sensor', arguments=None):
    return {'id': f'tool-{index}', 'type': 'function', 'function': {'name': name, 'arguments': arguments if arguments is not None else json.dumps({'node_id': node, 'fields': fields or ['co2_ppm']})}}


def snapshot():
    return {'timestamp': 100, 'observed_at': '1970-01-01T00:01:40+00:00', 'telemetry_age_s': 0,
            'nodes': {NODE: {'online': True, 'status': 'OK', 'updated_at': 100, 'sensor_state': 'OK',
                             'co2_ppm': 800, 'humidity_pct': [40, None], 'auth_key': 'never-send',
                             'api_token': 'never-send', 'files': [{'size': 500}], 'events': [1, 2]}}}


class Telemetry:
    def __init__(self):
        self.data = snapshot()
        self.calls = 0

    def latest(self):
        self.calls += 1
        return copy.deepcopy(self.data)

    def history(self, *args):
        return []


def session():
    telemetry = Telemetry()
    catalog = NodeCatalog([{'node_id': NODE}, {'node_id': 'absent'}, {'node_id': 'meeting_01'}])
    return SensorSession(telemetry, lambda: catalog, clock=lambda: 100), telemetry


def test_selected_fields_are_read_at_call_time_with_original_types_and_nulls():
    reader, telemetry = session()
    telemetry.data['nodes'][NODE]['co2_ppm'] = 920
    result = reader.execute(call(fields=['co2_ppm', 'humidity_pct[1]', 'not_present']))
    assert result['readings'] == {'co2_ppm': 920, 'humidity_pct[1]': None}
    assert result['missing_fields'] == ['not_present']
    assert result['fresh'] is True and result['node_updated_at'] == 100
    assert result['remaining'] == 2
    assert telemetry.calls == 1 and telemetry.data['nodes'][NODE]['updated_at'] == 100


@pytest.mark.parametrize('change', ['old_receipt', 'old_node', 'future_node', 'offline', 'unknown'])
def test_stale_and_unavailable_sources_are_never_reported_fresh(change):
    reader, telemetry = session()
    if change == 'old_receipt': telemetry.data['telemetry_age_s'] = 121
    if change == 'old_node': telemetry.data['nodes'][NODE]['updated_at'] = 20
    if change == 'future_node': telemetry.data['nodes'][NODE]['updated_at'] = 200
    if change == 'offline': telemetry.data['nodes'][NODE]['online'] = False
    if change == 'unknown': telemetry.data['nodes'][NODE]['status'] = 'UNKNOWN'
    assert reader.execute(call())['fresh'] is False


def test_missing_timestamps_mean_unknown_freshness():
    reader, telemetry = session()
    for key in ('telemetry_age_s', 'observed_at', 'timestamp'):
        telemetry.data.pop(key)
    assert reader.execute(call())['fresh'] is None


def test_read_budget_includes_failures_and_cannot_trigger_controls_or_foreign_nodes():
    reader, telemetry = session()
    assert reader.execute(call(name='control'))['error'] == 'unknown_tool'
    assert reader.execute(call(node='unauthorized'))['error'] == 'node_not_allowed'
    assert reader.execute(call(arguments='invalid JSON'))['error'] == 'invalid_arguments'
    assert reader.execute(call())['error'] == 'limit_exceeded'
    assert telemetry.calls == 0 and reader.remaining == 0


def test_args_limits_chat_and_missing_node_are_enforced():
    reader, telemetry = session()
    assert reader.execute(call(fields=['field'] * 17))['error'] == 'invalid_arguments'
    assert reader.execute(call(node='meeting_01'))['error'] == 'node_not_allowed'
    assert reader.execute(call(node='absent'))['error'] == 'node_unavailable'
    assert telemetry.calls == 1


def test_default_and_explicit_fields_cannot_expose_secrets_or_nonfinite_values():
    reader, telemetry = session()
    telemetry.data['nodes'][NODE]['voltage_v'] = float('nan')
    result = reader.execute(call(arguments=json.dumps({'node_id': NODE, 'fields': []})))
    assert 'never-send' not in json.dumps(result)
    assert 'files[0].size' not in result['readings'] and 'events[0]' not in result['readings']
    assert 'voltage_v' not in result['readings']
    result = reader.execute(call(fields=['auth_key', 'api_token', 'files[0].size', 'voltage_v']))
    assert not result['readings'] and len(result['missing_fields']) == 4


class Events:
    def __init__(self): self.rows = []
    def emit(self, kind, payload, **kwargs):
        self.rows.append((kind, copy.deepcopy(payload), kwargs))
        return str(len(self.rows))


class ScriptedLLM:
    def __init__(self, answers): self.answers, self.calls = answers, []
    def chat(self, messages, **kwargs):
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(kwargs)))
        return self.answers.pop(0)


def tool_result(*calls):
    return LLMResult(ok=True, text=None, model='fake', tool_calls=list(calls), attempts=1, prompt_tokens=7, completion_tokens=3)


def final_result(text=None):
    return LLMResult(ok=True, text=text or json.dumps({'situation': 'Датчики проверены', 'actions': []}), model='fake', attempts=1, prompt_tokens=7, completion_tokens=3)


def agent(tmp_path, answers):
    a = DomeLLMAgent.from_files(Settings(deepseek_api_key=None, rag_backend='none', state_dir=tmp_path))
    a.telemetry = Telemetry()
    a._llm = ScriptedLLM(answers)
    a.events = Events()
    return a


def test_four_calls_in_one_batch_execute_only_three_and_are_audited(tmp_path):
    a = agent(tmp_path, [tool_result(*(call(i) for i in range(4))), final_result()])
    response = a.decide('Проверь датчики')
    assert response.ok and not response.accepted_actions
    assert a.telemetry.calls == 5  # prompt + three explicit reads + validation
    assert len(response.sensor_reads) == 4 and response.sensor_reads[-1]['error'] == 'limit_exceeded'
    messages, kwargs = a._llm.calls[1]
    assert kwargs['tools'] is None
    results = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
    assert sum(r['ok'] for r in results) == 3 and results[-1]['error'] == 'limit_exceeded'
    assert len([r for r in a.events.rows if r[0] == 'sensor_read']) == 4
    decision = next(r[1] for r in a.events.rows if r[0] == 'decision')
    assert len(decision['sensor_read_event_ids']) == 4
    assert response.llm_call.prompt_tokens == 14


def test_budget_survives_multiple_tool_rounds_and_json_repair(tmp_path):
    a = agent(tmp_path, [tool_result(call(1)), final_result('bad JSON'), tool_result(call(2)), tool_result(call(3)), final_result()])
    response = a.decide('Проверь три датчика')
    assert response.ok and len(a._llm.calls) == 5
    assert len(response.sensor_reads) == 3
    assert a._llm.calls[-1][1]['tools'] is None
    # A later independent request gets its own fresh budget.
    a._llm.answers.extend([tool_result(call(4)), final_result()])
    assert a.decide('Ещё запрос').sensor_reads[0]['ok'] is True


def test_repeated_calls_after_budget_exhaustion_stop_without_reading_again(tmp_path):
    a = agent(tmp_path, [tool_result(call(1), call(2), call(3)), tool_result(call(4))])
    response = a.decide('Читай')
    assert not response.ok and a.telemetry.calls == 4  # prompt + three reads
    assert len(response.sensor_reads) == 3
    assert 'лимита' in response.llm_call.error


def test_source_exception_returns_tool_error_and_still_allows_final_decision(tmp_path):
    a = agent(tmp_path, [tool_result(call(1)), final_result()])
    original = a.telemetry.latest
    def latest():
        if a.telemetry.calls == 1:
            a.telemetry.calls += 1
            raise RuntimeError('secret internal failure')
        return original()
    a.telemetry.latest = latest
    response = a.decide('Проверь')
    assert response.ok and response.sensor_reads[0]['error'] == 'source_unavailable'
    assert 'secret internal failure' not in json.dumps(a.events.rows)


def test_deepseek_native_function_call_request_and_response_are_preserved():
    requests = []
    def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'id': 'chat-test', 'object': 'chat.completion', 'created': 100, 'model': 'fake',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': None, 'tool_calls': [call()]}, 'finish_reason': 'tool_calls'}],
            'usage': {'prompt_tokens': 11, 'completion_tokens': 4, 'total_tokens': 15}})
    client = DeepSeekClient(Settings(deepseek_api_key='mock-not-a-real-key', max_retries=0))
    client._client.close()
    client._client = OpenAI(api_key='mock-not-a-real-key', base_url='https://local.test/v1', http_client=httpx.Client(transport=httpx.MockTransport(transport)))
    try:
        result = client.chat([{'role': 'user', 'content': 'Проверь датчик'}], tools=[SENSOR_TOOL])
    finally:
        client._client.close()
    assert result.ok and result.text is None and result.tool_calls == [call()]
    assert requests[0]['tools'][0]['function']['name'] == 'read_sensor'
    assert requests[0]['tool_choice'] == 'auto' and result.prompt_tokens == 11


def test_agent_reads_updated_telemetry_after_initial_prompt(tmp_path):
    a = agent(tmp_path, [])
    seen = []
    def chat(messages, **kwargs):
        seen.append(copy.deepcopy(messages))
        if len(seen) == 1:
            assert '800' in messages[1]['content']
            a.telemetry.data['nodes'][NODE]['co2_ppm'] = 970
            a.telemetry.data['nodes'][NODE]['updated_at'] = 101
            a.telemetry.data['timestamp'] = 101
            return tool_result(call())
        result = json.loads(next(m['content'] for m in messages if m['role'] == 'tool'))
        assert result['readings']['co2_ppm'] == 970 and result['node_updated_at'] == 101
        return final_result()
    a._llm.chat = chat
    assert a.decide('Проверь CO₂').ok


def test_nonfinite_metadata_never_breaks_tool_json_serialization():
    reader, telemetry = session()
    telemetry.data['timestamp'] = float('nan')
    telemetry.data['nodes'][NODE]['updated_at'] = float('inf')
    result = reader.execute(call())
    assert result['node_updated_at'] is None and result['timestamp'] is None
    assert result['fresh'] is None
    json.dumps(result, allow_nan=False)


def test_merged_sensor_reads_keep_classification_checks_and_prompt_boundaries(tmp_path):
    decision = {'situation': 'Проверка объединения', 'actions': [], 'impact_kind': 'cascade',
                'authenticity': 'real', 'false_alarm': True, 'causal_chain': []}
    a = agent(tmp_path, [tool_result(call()), final_result(json.dumps(decision))])
    response = a.decide('Проверь датчик')
    assert response.ok and len(response.sensor_reads) == 1
    assert response.classification_issues
    payload = a.decision_payload(response)
    assert payload['classification_issues'] == response.classification_issues
    assert len(payload['sensor_read_event_ids']) == 1
    system = a._llm.calls[0][0][0]['content']
    assert '<untrusted' in system and 'ключи, токены' in system
    assert 'read_sensor' in system and 'ТРЁХ обращений' in system
    assert 'не более 1 команды' in system
