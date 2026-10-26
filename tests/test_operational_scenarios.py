"""Оперативные сценарии RAG Купола (OS-*, rag/data/knowledge_base/operational_scenarios.json) как проверки кода.

Карточка — гипотеза для модели, а не готовый ответ, поэтому здесь проверяется не диагноз, а то, что для
сценария делает код: поиск RAG находит карточку, классификация воздействий, приоритет повода и правило
«результат подтверждается только свежими данными».
"""

import copy
import json
from pathlib import Path

import pytest

from llm_agent.monitoring import assess_impacts
from llm_agent.triggers import PRIORITY, TelemetryWatch, Trigger, gate_verification_trigger
from test_dispatcher import QUIET, SCENARIO, make, quiet_snapshot

ROOT = Path(__file__).resolve().parent.parent
CARDS = {c["id"]: c for c in json.loads(
    (ROOT / "rag/data/knowledge_base/operational_scenarios.json").read_text(encoding="utf-8"))}


def card(cid):
    assert cid in CARDS, f"сценария {cid} нет в RAG"
    return CARDS[cid]


def co2_frame(values, interlock=None, held=()):
    nodes = {f"climate_sensor_0{i}": {"online": True, "status": "OK", "updated_at": 100, "co2_ppm": v, "zone": "MAIN"}
             for i, v in enumerate(values, 1)}
    nodes["dome_automation_01"] = {"interlocks": {interlock: {"state": "TRIPPED"}} if interlock else {}}
    for node in held:
        nodes[node] = {"hold_reason": interlock}
    return {"timestamp": 100, "nodes": nodes}


def kinds(snapshot):
    return {(a["kind"], a["authenticity"]) for a in assess_impacts(snapshot)}


# --- библиотека в RAG

def test_scenarios_live_only_in_rag_and_are_indexed():
    assert len(CARDS) == 24
    assert {c["impact_kind"] for c in CARDS.values()} == {"base", "hidden", "cascade", "false"}
    for c in CARDS.values():
        assert c["origin"] in ("logs", "docs", "synthetic") and c["reference"]
        assert c["decision"]["proposed_steps"] and c["verification"]["expected_and_checks"]
        assert c["decision"]["executable"] is False and c["current_state_claim"] is False
    records = json.loads((ROOT / "rag/index/records.json").read_text(encoding="utf-8"))
    assert set(CARDS) <= {r["id"] for r in records}
    assert not (ROOT / "data/operational_scenarios.yaml").exists()  # второй библиотеки и поиска нет
    assert not (ROOT / "llm_agent/scenarios.py").exists()


@pytest.fixture(scope="module")
def engine():
    from llm_agent.rag.dome import DomeRagRetriever

    return DomeRagRetriever(ROOT / "rag", semantic=False).engine  # индекс сверяется с базой: устаревший — ошибка


@pytest.mark.parametrize("cid", sorted(CARDS))
def test_rag_finds_scenario_by_its_signals(engine, cid):
    hits = engine.search(card(cid)["observation"]["signals"][0], limit=5)
    assert cid in [h["id"] for h in hits]


def test_agent_prompt_gets_scenario_through_rag_with_origin():
    from llm_agent.rag.dome import DomeRagRetriever

    docs = DomeRagRetriever(ROOT / "rag", semantic=False).search(card("OS-F04")["observation"]["signals"][0], k=5)
    doc = next(d for d in docs if d.id == "OS-F04")
    assert "основание: logs" in doc.text and "прошлые значения не являются текущей телеметрией" in doc.text


# --- классификация воздействий

def test_os_f01_single_false_co2():
    card("OS-F01")
    assert kinds(co2_frame((3000, 800, 810))) == {("false", "suspected_false")}


def test_os_c01_false_signal_still_causes_real_cascade():
    card("OS-C01")
    snapshot = co2_frame((3000, 800, 810), "IL_CO2", ("printer_3d_01", "cnc_01"))
    assert kinds(snapshot) == {("false", "suspected_false"), ("cascade", "unknown")}
    cascade = next(a for a in assess_impacts(snapshot) if a["kind"] == "cascade")
    assert cascade["evidence"]["held"] == ["printer_3d_01", "cnc_01"]


def test_os_b01_confirmed_co2_rise_is_real():
    card("OS-B01")
    assert kinds(co2_frame((1900, 1850, 1700))) == {("base", "real")}


def test_os_f06_webhook_completed_while_printing_is_hidden():
    card("OS-F06")
    snapshot = {"timestamp": 100, "nodes": {"printer_3d_01": {"webhook_state": "COMPLETED", "real_state": "PRINTING",
                                                              "progress_pct": 33.1}}}
    assert kinds(snapshot) == {("hidden", "unknown")}


def test_os_c06_lost_link_cannot_prove_a_false_signal():
    card("OS-C06")
    snapshot = co2_frame((3000, 800, 810))
    snapshot["telemetry_age_s"] = 121
    assert ("false", "suspected_false") not in kinds(snapshot)


# --- приоритет числовых гипотез

def battery_frame(temperature, ts):
    return {"timestamp": ts, "nodes": {"battery_01": {"online": True, "status": "OK", "updated_at": ts,
                                                      "temperature_c": temperature}}}


def test_os_c03_numeric_departure_is_low_priority_hypothesis():
    card("OS-C03")
    watch = TelemetryWatch(stale_s=30, stall_s=180)
    found = [t for t in watch.observe(battery_frame(64, 100), {}, 1000) if t.key.startswith("numeric|")]
    assert [t.kind for t in found] == ["numeric"] and "гипотеза" in found[0].text
    assert PRIORITY["numeric"] > max(PRIORITY[k] for k in ("operator", "alarm", "command", "conflict", "stale"))


def test_numeric_hypothesis_goes_after_operator_request(tmp_path):
    snapshot = quiet_snapshot()
    snapshot["nodes"]["battery_01"] = battery_frame(64, 100)["nodes"]["battery_01"]
    snapshot["nodes"]["meeting_01"]["recent_messages"] = [{"id": 1, "sender": {"role": "operator"}, "text": "Статус?"}]
    d, llm, clock = make(tmp_path, snapshot, QUIET)
    fired = d.tick()
    assert [t.kind for t in fired[0].triggers] == ["operator", "numeric"]


# --- подтверждение только по свежим данным

def test_os_h05_result_is_not_checked_on_stale_telemetry(tmp_path):
    card("OS-H05")
    snapshot = copy.deepcopy(SCENARIO)
    snapshot["telemetry_age_s"] = 95
    d, llm, clock = make(tmp_path, snapshot, QUIET, QUIET)
    d.tick()  # первичные поводы сценария
    clock.t += 30
    d.submit(Trigger("verification", "verify|d1", "Проверка результата решения d1"))
    assert all(t.kind != "verification" for f in d.tick() for t in f.triggers)
    assert "verify|d1" in d.pending  # ждёт свежих данных, не отброшена
    snapshot["telemetry_age_s"] = 2
    clock.t += 30
    fired = d.tick()
    assert any(t.kind == "verification" for t in fired[0].triggers)


def test_os_h06_command_without_new_frames_is_unverified_not_no_effect():
    card("OS-H06")
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
