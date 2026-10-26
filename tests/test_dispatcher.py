"""Диспетчер вызовов (rag/AGENT_TRIGGERS.md): поводы, объединение, паузы, проверка результата. LLM — заглушка."""

import copy
import json
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from llm_agent import DomeLLMAgent, Settings  # noqa: E402
from llm_agent.dispatcher import Dispatcher  # noqa: E402
from llm_agent.llm_client import LLMResult  # noqa: E402
from llm_agent.storage.file_store import FileCatalogRepo, FileDecisionJournalRepo  # noqa: E402

SCENARIO = json.loads((ROOT / "data" / "samples" / "scenarios" / "co2_false_alarm.json").read_text(encoding="utf-8"))
DECISION = {
    "situation": "Ложный CO2 на основном датчике", "cause": "дрейф climate_sensor_01", "severity": "warning",
    "confidence": 0.8, "false_alarm": True,
    "actions": [{"node_id": "dome_automation_01", "action": "set_data_trust",
                 "value": {"node": "climate_sensor_01", "param": "co2_ppm", "trust": "UNRELIABLE"},
                 "reason": "датчик врёт", "expected": "IL_CO2 перестаёт срабатывать", "verify_after_s": 8}],
}
QUIET = {"situation": "Штатно", "actions": []}


class FakeLLM:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append(messages[-1]["content"])
        answer = self.answers.pop(0) if self.answers else QUIET
        if answer is None:
            return LLMResult(ok=False, text=None, model="fake", attempts=1, error="timeout")
        return LLMResult(ok=True, text=json.dumps(answer, ensure_ascii=False), model="fake", attempts=1)


class Telemetry:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def latest(self):
        return self.snapshot

    def history(self, *a, **k):
        return []


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(tmp_path, snapshot, *answers, **overrides):
    settings = Settings(deepseek_api_key=None, rag_backend="none", state_dir=tmp_path / "state",
                        trigger_review_s=0, **overrides)
    agent = DomeLLMAgent(settings, Telemetry(snapshot), FileDecisionJournalRepo(tmp_path / "journal.jsonl"),
                         FileCatalogRepo(ROOT / "data" / "catalog_static.json"))
    llm = FakeLLM(*answers)
    agent._llm = llm
    clock = Clock()
    return Dispatcher(agent, clock=clock), llm, clock


def quiet_snapshot(ts=100.0):
    return {"timestamp": ts, "game_time": {"day": 1, "hour": 10.0}, "nodes": {
        "printer_3d_01": {"state": "IDLE", "bed_clear": True, "online": True, "status": "OK"},
        "meeting_01": {"online": True, "last_message_id": 0, "recent_messages": []}}}


def prompt_trigger(prompt: str) -> str:
    return prompt.split("# Повод для анализа\n", 1)[1].split("\n\nЧасы купола", 1)[0]


def test_alarms_coalesced_into_one_call_and_not_repeated(tmp_path):
    d, llm, clock = make(tmp_path, copy.deepcopy(SCENARIO), DECISION)
    fired = d.tick()
    assert len(fired) == 1 and len(llm.calls) == 1
    kinds = {t.kind for t in fired[0].triggers}
    assert kinds == {"alarm", "conflict"}
    assert "IL_CO2" in prompt_trigger(llm.calls[0]) and "расходятся" in prompt_trigger(llm.calls[0])
    assert fired[0].response.accepted_actions  # set_data_trust прошло локальную проверку

    # Та же картина в следующих кадрах — не новый повод
    for i in range(3):
        clock.t += 15
        d.agent.telemetry.snapshot["observed_at"] = f"frame-{i}"  # новый кадр с тем же состоянием
        d.tick()
    assert len(llm.calls) == 2  # второй вызов — плановая проверка результата, не повтор тревоги
    assert "Проверка результата решения" in prompt_trigger(llm.calls[1])
    journal = (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
    assert "Ложный CO2" in journal


def test_state_change_is_new_situation(tmp_path):
    d, llm, clock = make(tmp_path, copy.deepcopy(SCENARIO), QUIET, QUIET)
    d.tick()
    snap = d.agent.telemetry.snapshot
    snap["timestamp"] += 1
    snap["nodes"]["dome_automation_01"]["interlocks"]["IL_CO2"]["state"] = "BLOCKED"
    clock.t += 11
    fired = d.tick()
    assert len(fired) == 1 and "BLOCKED" in fired[0].text


def test_min_interval_between_calls(tmp_path):
    d, llm, clock = make(tmp_path, quiet_snapshot(), QUIET, QUIET)
    msg = {"id": 1, "sender": {"role": "operator"}, "text": "Статус?"}
    d.agent.telemetry.snapshot["nodes"]["meeting_01"]["recent_messages"] = [msg]
    assert len(d.tick()) == 1
    d.agent.telemetry.snapshot["nodes"]["meeting_01"]["recent_messages"].append(
        {"id": 2, "sender": {"role": "operator"}, "text": "Ау?"})
    clock.t += 3
    assert d.tick() == []  # рано: повод ждёт в очереди
    clock.t += 8
    fired = d.tick()
    assert len(fired) == 1 and "Ау?" in fired[0].text and "Статус?" not in fired[0].text


def test_operator_messages_only_from_operator_and_once_across_restarts(tmp_path):
    snap = quiet_snapshot()
    snap["nodes"]["meeting_01"]["recent_messages"] = [
        {"id": 5, "sender": {"role": "team", "team": 1}, "text": "наш агент"},
        {"id": 6, "sender": {"role": "operator"}, "text": "Напечатай gear.gcode"}]
    d, llm, clock = make(tmp_path, snap, QUIET)
    fired = d.tick()
    assert [t.kind for t in fired[0].triggers] == ["operator"]
    assert "gear.gcode" in fired[0].text and "наш агент" not in fired[0].text

    d2, llm2, _ = make(tmp_path, copy.deepcopy(snap))  # перезапуск: last_id из state/
    assert d2.tick() == [] and llm2.calls == []


def test_llm_failure_keeps_trigger_and_retries_with_backoff(tmp_path):
    snap = quiet_snapshot()
    snap["nodes"]["meeting_01"]["recent_messages"] = [{"id": 1, "sender": {"role": "operator"}, "text": "Статус?"}]
    d, llm, clock = make(tmp_path, snap, None, None, QUIET)
    d.tick()
    assert d.pending and d.stats["llm_failures"] == 1
    clock.t += 11
    assert d.tick() == []  # пауза после сбоя 10 * 2 = 20 с
    clock.t += 10
    d.tick()
    assert d.stats["llm_failures"] == 2 and d.pending
    clock.t += 41
    fired = d.tick()
    assert fired[0].response.ok and not d.pending
    assert "LLM недоступна" in (tmp_path / "journal.jsonl").read_text(encoding="utf-8")


def test_no_new_data_means_unverified_not_success(tmp_path):
    d, llm, clock = make(tmp_path, copy.deepcopy(SCENARIO), DECISION, trigger_verify_retries=1)
    d.tick()
    assert len(d.checks) == 1
    clock.t += 8
    assert d.tick() == [] and len(d.checks) == 1  # ждём ещё один период
    clock.t += 8
    fired = d.tick()  # кадр тот же: результат не подтверждён
    assert d.stats["unverified"] == 1 and not d.checks
    texts = " ".join(f.text for f in fired)
    assert "не проверен" in texts or "Новых кадров" in texts
    journal = [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(r.get("kind") == "verification" and r["status"] == "unverified" for r in journal)


def test_stale_frames_trigger_once(tmp_path):
    d, llm, clock = make(tmp_path, quiet_snapshot(), trigger_stale_s=30)
    assert d.tick() == []
    clock.t += 31
    fired = d.tick()
    assert [t.kind for t in fired[0].triggers] == ["stale"]
    clock.t += 31
    assert d.tick() == []


def test_done_but_progress_grows_is_conflict_and_stall_is_detected(tmp_path):
    snap = quiet_snapshot()
    printer = snap["nodes"]["printer_3d_01"]
    printer.update(state="COMPLETED", progress_pct=97.0)
    d, llm, clock = make(tmp_path, snap, trigger_stall_s=60)
    assert d.tick() == []
    printer["progress_pct"] = 98.5
    snap["timestamp"] += 2
    clock.t += 11
    fired = d.tick()
    assert fired[0].triggers[0].kind == "conflict" and "не считать изделие готовым" in fired[0].text.lower()

    printer.update(state="PRINTING", progress_pct=40.0, file_name="gear.gcode")
    for _ in range(4):
        snap["timestamp"] += 20
        clock.t += 20
        fired = d.tick()
    assert any(t.kind == "operation" for f in fired for t in f.triggers)


def test_review_timer_when_quiet(tmp_path):
    d, llm, clock = make(tmp_path, quiet_snapshot())
    d.settings.trigger_review_s = 300
    assert d.tick() == []
    clock.t += 301
    d.agent.telemetry.snapshot["timestamp"] += 301
    fired = d.tick()
    assert [t.kind for t in fired[0].triggers] == ["review"]


def test_dry_run_does_not_call_model(tmp_path):
    d, llm, clock = make(tmp_path, copy.deepcopy(SCENARIO))
    d.dry_run = True
    assert len(d.tick()) == 1 and llm.calls == []


# --- лента событий БД ---

def test_feed_turns_dome_rejection_and_timeout_into_triggers(tmp_path):
    sys.path.insert(0, str(ROOT / "services" / "data_api"))
    testclient = pytest.importorskip("fastapi.testclient")
    from data_api.app import create_app
    from llm_agent.storage.data_api import ApiEventFeed, DataApiClient

    def ev(event_type, payload, direction="inbound", source="dome", channel="websocket", **kw):
        return {"event_id": str(uuid.uuid4()), "run_id": "r", "session_id": "s", "source": source,
                "channel": channel, "direction": direction, "event_type": event_type, "payload": payload, **kw}

    with testclient.TestClient(create_app(tmp_path / "db.sqlite3", token="test-service-secret")) as http:
        client = DataApiClient("http://testserver", "test-service-secret", http=http)
        client.deliver([ev("control_response", {"type": "control_response", "request_id": 1, "success": False,
                                                "error": "старое"}, request_id=1)])
        feed = ApiEventFeed(client, "r")
        assert feed.poll() == []  # история до старта — не повод
        client.deliver([
            ev("control", {"type": "control", "request_id": 7, "node_id": "cnc_01", "action": "resume"},
               direction="outbound", source="agent", request_id=7),
            ev("control_response", {"type": "control_response", "request_id": 7, "success": False,
                                    "error": "interlock IL_CO2 is TRIPPED"}, request_id=7),
            ev("request_timeout", {"type": "control", "request_id": 8, "node_id": "printer_3d_01"},
               direction="internal", source="agent", channel="internal", request_id=8),
            ev("meeting_message", {"type": "meeting_message", "message": {
                "id": 3, "sender": {"role": "operator"}, "text": "Что с фрезером?"}}),
        ])

        snap = quiet_snapshot()
        d, llm, clock = make(tmp_path, snap)
        d.feed = feed
        d.dry_run = True
        fired = d.tick()
        text = fired[0].text
        assert "cnc_01.resume" in text and "IL_CO2" in text
        assert "Нет ответа Купола" in text and "printer_3d_01" in text
        assert "Что с фрезером?" in text
        assert fired[0].triggers[0].kind in ("command", "operator")
        # Сообщение из ленты и то же сообщение в телеметрии — один повод
        snap["nodes"]["meeting_01"]["recent_messages"] = [
            {"id": 3, "sender": {"role": "operator"}, "text": "Что с фрезером?"}]
        clock.t += 11
        assert d.tick() == []
