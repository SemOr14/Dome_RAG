"""LLM-слой поверх сервиса БД/API: in-process FastAPI на временной SQLite, LLM подменена заглушкой."""

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services" / "data_api"))
fastapi_testclient = pytest.importorskip("fastapi.testclient")
from data_api.app import create_app  # noqa: E402

from llm_agent import DomeLLMAgent, Settings  # noqa: E402
from llm_agent.llm_client import LLMResult  # noqa: E402
from llm_agent.storage.data_api import DataApiClient  # noqa: E402

TOKEN = "test-service-secret"
RUN, SESSION = "run-test", "ws-1"


class FakeLLM:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append(messages)
        return LLMResult(ok=True, text=self.answers.pop(0), model="fake", prompt_tokens=10, completion_tokens=5,
                         latency_ms=1, attempts=1)


def event(event_type, payload, **kwargs):
    return {"event_id": str(uuid.uuid4()), "run_id": RUN, "session_id": SESSION, "source": "dome",
            "channel": "websocket", "direction": "inbound", "event_type": event_type, "payload": payload, **kwargs}


@pytest.fixture
def api(tmp_path):
    with fastapi_testclient.TestClient(create_app(tmp_path / "db.sqlite3", token=TOKEN)) as http:
        client = DataApiClient("http://testserver", TOKEN, state_dir=tmp_path / "state", http=http)
        catalog = json.loads((ROOT / "data" / "catalog_static.json").read_text(encoding="utf-8"))["nodes"]
        frame = json.loads((ROOT / "data" / "samples" / "scenarios" / "co2_false_alarm.json").read_text(encoding="utf-8"))
        observed = datetime.now(timezone.utc).isoformat()
        client.deliver([
            event("auth_response", {"type": "auth_response", "success": True, "role": "team", "team": 1}),
            event("describe_nodes_response", {"type": "describe_nodes_response", "request_id": 1, "success": True,
                                              "nodes": catalog}, request_id=1),
            event("telemetry", {"type": "telemetry", "timestamp": frame["timestamp"], "role": "team",
                                "data": {"nodes": frame["nodes"], "game_time": frame["game_time"]}},
                  metadata={"observed_at": observed}),
            event("meeting_message", {"type": "meeting_message", "message": {
                "id": 1, "time": frame["timestamp"], "sender": {"role": "operator", "team": None, "name": "Оператор"},
                "to": None, "text": "Почему станки стоят?"}}),
        ])
        yield client, http


def make_agent(client, llm, tmp_path):
    settings = Settings(deepseek_api_key=None, data_api_url="http://testserver", state_dir=tmp_path / "state",
                        rag_backend="none")
    agent = DomeLLMAgent.from_data_api(settings, client)
    agent._llm = llm
    return agent


DECISION = {
    "situation": "Ложный CO2 на основном датчике", "cause": "дрейф climate_sensor_01", "severity": "warning",
    "confidence": 0.8, "false_alarm": True,
    "actions": [{"node_id": "dome_automation_01", "action": "set_data_trust",
                 "value": {"node": "climate_sensor_01", "param": "co2_ppm", "trust": "UNRELIABLE"},
                 "reason": "датчик врёт", "expected": "IL_CO2 перестаёт срабатывать", "verify_after_s": 5},
                {"node_id": "dome_automation_01", "action": "set_primary_sensor", "value": "climate_sensor_09",
                 "reason": "несуществующий датчик — сервер должен отклонить", "priority": 2}],
}


def test_prompt_from_db_and_full_llm_io_logged(api, tmp_path):
    client, http = api
    llm = FakeLLM("не json", json.dumps(DECISION, ensure_ascii=False))
    agent = make_agent(client, llm, tmp_path)
    assert agent.telemetry.run_id == RUN and agent.control_checker is not None  # run и сессия найдены сами

    prompt = agent.build_prompt("IL_CO2 сработал")
    user = prompt.messages[1]["content"]
    assert "climate_sensor_01" in user and "Почему станки стоят?" in user
    assert "Кадр телеметрии наблюдён" in user
    assert len(agent.catalog.by_id) > 10  # каталог из describe_nodes_response

    response = agent.decide("IL_CO2 сработал")
    assert response.ok and response.decision.false_alarm and len(llm.calls) == 2

    def stored(event_type):
        return http.get("/v1/events", params={"run_id": RUN, "event_type": event_type}).json()["items"]

    requests, responses = stored("llm_request"), stored("llm_response")
    assert len(requests) == 2 and len(responses) == 2
    assert [r["request_id"] for r in requests] == [r["request_id"] for r in responses]
    assert responses[0]["payload"]["error"].startswith("parse_error")
    assert responses[0]["payload"]["raw_response"] == "не json"
    assert responses[1]["payload"]["parsed_output"]["false_alarm"] is True

    [decision] = stored("decision")
    assert decision["event_id"] == response.decision_event_id
    assert decision["payload"]["observation_event_ids"] and decision["payload"]["status"] == "proposed"

    intents = stored("control_intent")
    assert len(intents) == len(response.verdicts)
    assert all(i["metadata"]["decision_event_id"] == response.decision_event_id for i in intents)
    assert all(v.server_check["status"] in ("accepted", "rejected") for v in response.verdicts)
    checks = {v.action.action: v for v in response.verdicts}
    assert checks["set_data_trust"].server_check["reasons"] == []
    wrong = checks["set_primary_sensor"]
    assert not wrong.accepted and "БД/API: invalid_value" in wrong.reasons

    agent.record(response)  # уже записано событием decision — дубля нет
    assert len(stored("decision")) == 1
    assert any(j.get("kind") == "decision" for j in agent.journal.recent())


def test_outbox_keeps_events_while_db_is_down(api, tmp_path):
    import httpx

    client, http = api
    down = DataApiClient("http://127.0.0.1:9", state_dir=tmp_path / "state", timeout_s=0.5)
    ev = event("llm_request", {"messages": []}, source="agent", channel="llm", direction="outbound")
    assert down.deliver([ev]) is False
    assert json.loads(down.outbox.read_text(encoding="utf-8").splitlines()[0])["event_id"] == ev["event_id"]

    client.outbox = down.outbox  # БД снова доступна: очередь уходит с прежним event_id
    assert client.deliver([]) is True
    assert http.get(f"/v1/events/{ev['event_id']}").status_code == 200
    assert down.outbox.read_text(encoding="utf-8") == ""
    assert isinstance(down.http, httpx.Client)


def test_dispatcher_over_db_records_trigger_decision_and_verification(api, tmp_path):
    from llm_agent.dispatcher import Dispatcher
    from llm_agent.storage.data_api import ApiEventFeed

    client, http = api
    agent = make_agent(client, FakeLLM(json.dumps(DECISION, ensure_ascii=False),
                                       json.dumps({"situation": "IL_CO2 снят", "actions": []}, ensure_ascii=False)),
                       tmp_path)
    clock = [1000.0]
    dispatcher = Dispatcher(agent, ApiEventFeed(client, RUN), clock=lambda: clock[0])
    [fired] = dispatcher.tick()  # все тревоги кадра — один вызов
    # Сообщение Оператора в БД раньше старта ленты — история, не повод (в живой телеметрии оно в recent_messages)
    assert {t.kind for t in fired.triggers} == {"alarm", "conflict"}

    def stored(event_type):
        return http.get("/v1/events", params={"run_id": RUN, "event_type": event_type}).json()["items"]

    [trigger] = stored("llm_trigger")
    assert trigger["payload"]["text"] == fired.text
    [decision] = stored("decision")
    assert dispatcher.checks and dispatcher.checks[0].decision_event_id == decision["event_id"]

    # Новый кадр телеметрии → проверка результата вторым вызовом и событие verification со ссылкой на решение
    frame = json.loads((ROOT / "data" / "samples" / "scenarios" / "co2_false_alarm.json").read_text(encoding="utf-8"))
    client.deliver([event("telemetry", {"type": "telemetry", "timestamp": frame["timestamp"] + 10, "role": "team",
                                        "data": {"nodes": frame["nodes"], "game_time": frame["game_time"]}},
                          metadata={"observed_at": datetime.now(timezone.utc).isoformat()})])
    clock[0] += 11
    [check] = dispatcher.tick()
    assert [t.kind for t in check.triggers] == ["verification"]
    [verification] = stored("verification")
    assert verification["payload"]["decision_event_id"] == decision["event_id"]
    assert verification["payload"]["status"] == "reassessed"


def test_native_sensor_reads_use_new_snapshot_and_are_linked_to_decision(api, tmp_path):
    client, http = api
    class ReadingLLM:
        def __init__(self): self.calls = 0
        def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                frame = json.loads((ROOT / 'data/samples/scenarios/co2_false_alarm.json').read_text())
                frame['nodes']['climate_sensor_01']['co2_ppm'] = 1234
                frame['nodes']['climate_sensor_01']['updated_at'] = frame['timestamp'] + 1
                client.deliver([event('telemetry', {'type': 'telemetry', 'timestamp': frame['timestamp'] + 1,
                    'data': {'nodes': frame['nodes'], 'game_time': frame['game_time']}},
                    metadata={'observed_at': datetime.now(timezone.utc).isoformat()})])
                return LLMResult(ok=True, text=None, model='fake', tool_calls=[{
                    'id': 'read-1', 'type': 'function', 'function': {'name': 'read_sensor',
                    'arguments': json.dumps({'node_id': 'climate_sensor_01', 'fields': ['co2_ppm']})}}])
            reading = json.loads(next(m['content'] for m in messages if m['role'] == 'tool'))
            assert reading['readings']['co2_ppm'] == 1234
            return LLMResult(ok=True, text=json.dumps({'situation': 'Показание уточнено', 'actions': []}), model='fake')
    agent = make_agent(client, ReadingLLM(), tmp_path)
    response = agent.decide('Уточни CO₂ сейчас')
    assert response.ok
    reads = http.get('/v1/events', params={'run_id': RUN, 'event_type': 'sensor_read'}).json()['items']
    assert len(reads) == 1 and reads[0]['session_id'] == SESSION
    assert reads[0]['payload']['result']['readings'] == {'co2_ppm': 1234}
    decision = http.get('/v1/events/' + response.decision_event_id).json()
    assert decision['payload']['sensor_read_event_ids'] == [reads[0]['event_id']]
    responses = http.get('/v1/events', params={'run_id': RUN, 'event_type': 'llm_response'}).json()['items']
    assert responses[0]['payload']['tool_calls'][0]['id'] == 'read-1'
