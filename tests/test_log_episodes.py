"""Эпизоды из логов 12–18.10.2026 (ситуации RAG S25–S53) как сценарные проверки агента; модель — заглушка.

Для каждого эпизода через диспетчер прогоняются 30 настоящих кадров телеметрии до него и два после
(rag/data/sources/Logs). Проверяется, что агент заметил эпизод и что в промпт попала его карточка RAG.
Эпизоды, которые агент пока не распознаёт, отмечены xfail с причиной: это список того, что улучшать.
Отдельно — проверки разметки воздействия, которую делает модель, и правила «только свежие данные».
"""

import copy
import json
import re
from pathlib import Path

import pytest

from llm_agent import DomeLLMAgent, Settings
from llm_agent.agent import make_retriever
from llm_agent.dispatcher import Dispatcher
from llm_agent.schemas import AgentDecision
from llm_agent.storage.data_api import journal_record
from llm_agent.storage.file_store import FileCatalogRepo, FileDecisionJournalRepo
from llm_agent.triggers import PRIORITY, TelemetryWatch, Trigger, gate_verification_trigger
from llm_agent.validate import check_classification
from test_dispatcher import QUIET, SCENARIO, Clock, FakeLLM, make, quiet_snapshot

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "rag/data/sources/Logs"
SITUATIONS = {c["id"]: c for c in json.loads(
    (ROOT / "rag/data/knowledge_base/situations.json").read_text(encoding="utf-8"))}
WINDOW = 30
_lines: dict[str, list[str]] = {}


def frames(day, start, end):
    lines = _lines.setdefault(day, (LOGS / f"2026-10-{day}/telemetry.jsonl").read_text(encoding="utf-8").splitlines())
    for n in range(start, end + 1):
        row = json.loads(lines[n - 1])
        stamp = max(b["updated_at"] for b in row["nodes"].values()
                    if isinstance(b, dict) and isinstance(b.get("updated_at"), (int, float)))
        yield n, {"timestamp": stamp, "game_time": {}, "nodes": row["nodes"], "observed_at": row["ts"], "telemetry_age_s": 2}


class Live:
    snapshot: dict = {}

    def latest(self):
        return self.snapshot

    def history(self, *a, **k):
        return []


def replay(tmp_path, day, line):
    """Кадры минута за минутой; возвращает (номер кадра, поводы, карточки RAG в промпте) у самого эпизода."""
    settings = Settings(deepseek_api_key=None, rag_backend="dome", rag_semantic=False, rag_k=5,
                        state_dir=tmp_path / "state", trigger_review_s=0)
    telemetry = Live()
    agent = DomeLLMAgent(settings, telemetry, FileDecisionJournalRepo(tmp_path / "journal.jsonl"),
                         FileCatalogRepo(ROOT / "data" / "catalog_static.json"), retriever=make_retriever(settings))
    llm = FakeLLM(*[QUIET] * (WINDOW + 3))
    agent._llm = llm
    clock = Clock()
    dispatcher = Dispatcher(agent, clock=clock)
    calls = []
    for n, snapshot in frames(day, line - WINDOW, line + 2):
        telemetry.snapshot = snapshot
        clock.t += 60
        for fired in dispatcher.tick():
            if n >= line - 3:
                calls.append((n, fired.triggers, re.findall(r"^## \[(S\d\d|F\d\d)\]", llm.calls[-1], re.M)))
    return calls


def gap(reason):
    return pytest.mark.xfail(strict=True, reason=reason)


NO_TRIGGER = "агент не замечает эпизод: ни одного повода"
WRONG_CARD = "повод есть, но RAG приносит другие карточки"
EPISODES = [
    ("S27", "12", 973), ("S28", "13", 402), ("S31", "13", 632), ("S35", "15", 848), ("S36", "15", 1006),
    ("S38", "15", 1153), ("S41", "16", 875), ("S43", "17", 241), ("S46", "17", 843), ("S47", "17", 1382),
    ("S50", "18", 904), ("S51", "18", 811),
    pytest.param("S26", "12", 833, marks=gap(WRONG_CARD + " (просадка шины между кадрами не видна)")),
    pytest.param("S29", "13", 126, marks=gap(NO_TRIGGER + " (+50 °C без порога для метеостанции)")),
    pytest.param("S32", "13", 867, marks=gap(WRONG_CARD)),
    pytest.param("S33", "13", 981, marks=gap(WRONG_CARD + " (массовый UNKNOWN)")),
    pytest.param("S34", "15", 691, marks=gap(NO_TRIGGER + " (срабатывание короче шага кадров, видно только в событиях)")),
    pytest.param("S37", "15", 1381, marks=gap(NO_TRIGGER + " (застывшее значение не детектируется)")),
    pytest.param("S39", "16", 731, marks=gap(WRONG_CARD)),
    pytest.param("S42", "16", 1179, marks=gap(WRONG_CARD + " (fan_state FAULT не становится срочным поводом)")),
    pytest.param("S44", "17", 593, marks=gap(NO_TRIGGER + " (температура сопла против мощности нагревателя)")),
    pytest.param("S45", "17", 687, marks=gap(WRONG_CARD + " (массовый UNKNOWN)")),
    pytest.param("S48", "18", 482, marks=gap(NO_TRIGGER + " (balancing_state и battery_charge_blocked)")),
    pytest.param("S49", "18", 761, marks=gap(WRONG_CARD)),
]


@pytest.mark.parametrize("sid,day,line", EPISODES)
def test_log_episode_reaches_model_with_its_card(tmp_path, sid, day, line):
    assert SITUATIONS[sid]["evidence_class"] == "historical_episode"
    calls = replay(tmp_path, day, line)
    assert calls, "у эпизода не было вызова модели"
    assert any(sid in ids for _, _, ids in calls), [(n, ids) for n, _, ids in calls]


# --- база знаний

def test_situations_carry_impact_labels_and_no_second_library():
    assert len(SITUATIONS) == 56
    for c in SITUATIONS.values():
        assert c["impact"]["kind"] in ("base", "hidden", "cascade", "false", "unknown")
        assert c["impact"]["authenticity"] in ("real", "suspected_false", "unknown")
        if c["impact"]["kind"] == "cascade" and c["evidence_class"] == "historical_episode":
            assert len(c["impact"]["causal_chain"]) >= 2, c["id"]
    assert not (ROOT / "rag/data/knowledge_base/operational_scenarios.json").exists()
    assert not (ROOT / "llm_agent/scenarios.py").exists()
    week = {r["path"] for r in json.loads((ROOT / "rag/data/sources/register.json").read_text(encoding="utf-8"))}
    assert {f"sources/Logs/2026-10-{d}/{f}.jsonl" for d in range(12, 19) for f in ("telemetry", "events", "logbook")} <= week


@pytest.fixture(scope="module")
def engine():
    from llm_agent.rag.dome import DomeRagRetriever

    return DomeRagRetriever(ROOT / "rag", semantic=False).engine  # устаревший индекс — ошибка


@pytest.mark.parametrize("sid", [s for s in SITUATIONS if s >= "S25" or s == "S08"])
def test_rag_finds_situation_by_its_first_signal(engine, sid):
    hits = engine.search(SITUATIONS[sid]["observation"]["signals"][0], limit=5)
    assert sid in [h["id"] for h in hits]


def test_rag_doc_shows_episode_labels_to_the_model():
    from llm_agent.rag.dome import DomeRagRetriever

    docs = DomeRagRetriever(ROOT / "rag", semantic=False).search("Влажность 99 % после шлюза холодным утром и IL_CONDENSATE", k=3)
    doc = next(d for d in docs if d.id == "S28")
    assert "Разметка: impact_kind=cascade, authenticity=suspected_false" in doc.text
    assert "конденсат на climate_sensor_01 после шлюза → ложные 99 % RH" in doc.text


# --- разметка воздействия делает модель; код только проверяет согласованность

def decision(**kw):
    base = {"situation": "s", "evidence": [{"node": "battery_01", "param": "soc_pct", "value": 20}]}
    return AgentDecision(**{**base, **kw})


def test_classification_consistency_checks():
    ok = decision(impact_kind="cascade", authenticity="suspected_false", false_alarm=True,
                  causal_chain=["скачок оценки SOC", "IL_LOAD_SHED", "станки без питания"])
    assert check_classification(ok, telemetry_age_s=3, stale_s=30) == []
    assert check_classification(decision(impact_kind="false", authenticity="real"), 3, 30) == [
        "impact_kind=false при authenticity=real: подтверждённый сигнал не может быть ложным"]
    assert "без цепочки" in check_classification(decision(impact_kind="cascade", causal_chain=["IL_CO2"]), 3, 30)[0]
    assert "противоречит" in check_classification(decision(authenticity="suspected_false", false_alarm=False), 3, 30)[0]
    assert "без показаний" in check_classification(decision(authenticity="real", evidence=[]), 3, 30)[0]


def test_authenticity_is_not_confirmed_on_stale_telemetry():
    d = decision(impact_kind="base", authenticity="real")
    issues = check_classification(d, telemetry_age_s=95, stale_s=30)
    assert d.authenticity == "unknown" and "95 с давности" in issues[0]


def test_model_labels_are_normalized_not_rejected():
    d = AgentDecision(situation="s", impact_kind="Каскад", authenticity="REAL",
                      causal_chain="конденсат → 99 % RH → IL_CONDENSATE")
    assert d.impact_kind == "unknown" and d.authenticity == "real"
    assert d.causal_chain == ["конденсат", "99 % RH", "IL_CONDENSATE"]


def test_previous_label_and_issues_reach_the_next_prompt(tmp_path):
    snapshot = copy.deepcopy(SCENARIO)
    snapshot["telemetry_age_s"] = 80
    answer = {"situation": "Ложный CO2", "impact_kind": "cascade", "authenticity": "suspected_false",
              "causal_chain": ["ложный CO₂", "IL_CO2", "пауза станков"], "evidence": [{"node": "climate_sensor_01"}]}
    d, llm, clock = make(tmp_path, snapshot, answer)
    fired = d.tick()
    assert fired[0].response.decision.authenticity == "unknown"  # по устаревшему кадру не подтверждается
    journal = (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
    assert "разметка: cascade/unknown (ложный CO₂ → IL_CO2 → пауза станков), замечания: authenticity=suspected_false" in journal
    rec = journal_record({"event_type": "agent_decision", "source": "agent", "payload": {
        "situation": "s", "cause": "c", "impact_kind": "false", "authenticity": "suspected_false", "causal_chain": []}})
    assert "разметка: false/suspected_false" in rec["text"]


def test_prompt_has_classification_procedure_and_no_code_labels(tmp_path):
    d, llm, clock = make(tmp_path, quiet_snapshot())
    prompt = d.agent.build_prompt("повод")
    assert "Разделение воздействий" not in prompt.messages[1]["content"]  # кодом больше не размечается
    system = prompt.messages[0]["content"]
    assert "код его не вычисляет" in system and "`cascade` + `suspected_false`" in system


# --- подтверждение только по свежим данным

def test_numeric_departure_is_low_priority_hypothesis():
    watch = TelemetryWatch(stale_s=30, stall_s=180)
    snapshot = {"timestamp": 100, "nodes": {"battery_01": {"online": True, "status": "OK", "updated_at": 100, "temperature_c": 64}}}
    found = [t for t in watch.observe(snapshot, {}, 1000) if t.key.startswith("numeric|")]
    assert [t.kind for t in found] == ["numeric"] and "гипотеза" in found[0].text
    assert PRIORITY["numeric"] > max(PRIORITY[k] for k in ("operator", "alarm", "command", "conflict", "stale"))


def test_numeric_hypothesis_goes_after_operator_request(tmp_path):
    snapshot = quiet_snapshot()
    snapshot["nodes"]["battery_01"] = {"online": True, "status": "OK", "updated_at": 100, "temperature_c": 64}
    snapshot["nodes"]["meeting_01"]["recent_messages"] = [{"id": 1, "sender": {"role": "operator"}, "text": "Статус?"}]
    d, llm, clock = make(tmp_path, snapshot, QUIET)
    assert [t.kind for t in d.tick()[0].triggers] == ["operator", "numeric"]


def test_result_is_not_checked_on_stale_telemetry(tmp_path):
    snapshot = copy.deepcopy(SCENARIO)
    snapshot["telemetry_age_s"] = 95
    d, llm, clock = make(tmp_path, snapshot, QUIET, QUIET)
    d.tick()
    clock.t += 30
    d.submit(Trigger("verification", "verify|d1", "Проверка результата решения d1"))
    assert all(t.kind != "verification" for f in d.tick() for t in f.triggers)
    assert "verify|d1" in d.pending  # ждёт свежих данных, не отброшена
    snapshot["telemetry_age_s"] = 2
    clock.t += 30
    assert any(t.kind == "verification" for t in d.tick()[0].triggers)


def test_command_without_new_frames_is_unverified_not_no_effect():
    rec = {"run_id": "r", "ref": 5, "ok": False, "no_data": True, "waited_s": 25,
           "expected": "smart_panel_01.lines[2].state = OFF", "observed": "ON"}
    action = {"request": {"node_id": "smart_panel_01", "action": "line_off", "value": {"line": 3}}}
    trig = gate_verification_trigger(rec, action, timed_out=False, cooldown_s=120)
    assert trig.kind == "stale" and "не проверен" in trig.text and "заблокирована" not in trig.text


def test_prompt_warns_when_telemetry_is_stale(tmp_path):
    snapshot = quiet_snapshot()
    snapshot["telemetry_age_s"] = 95
    d, llm, clock = make(tmp_path, snapshot)
    user = d.agent.build_prompt("Оператор: линия 3 уже отключена?").messages[1]["content"]
    assert "Телеметрия устарела: последний кадр 95 с назад" in user
    snapshot["telemetry_age_s"] = 3
    assert "Телеметрия устарела" not in d.agent.build_prompt("повод").messages[1]["content"]
