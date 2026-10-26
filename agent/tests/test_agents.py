"""Тесты агентов: монитор, диагност, энергопланировщик (критерий приёмки 7)."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from kupol.agents.base import Agent, Situation
from kupol.agents.diagnost import Diagnost, DiagnostOutput, checklist_7_3
from kupol.agents.energy_planner import EnergyOutput, EnergyPlannerAgent
from kupol.agents.llm import PROMPTS_DIR, LLMClient, load_prompt
from kupol.agents.monitor import Monitor, MonitorOutput
from kupol.agents.symptoms import load_matrix, match_rows, rank_causes, render_markdown
from kupol.core.config import AppConfig, LLMConfig
from kupol.core.energy import EnergyPlanner, ProcessTracker, energy_status
from kupol.core.journal import Journal
from kupol.core.models import Alert, AlertLevel, Evidence, LineState, Recommendation, Verdict
from kupol.core.state import State
from tests.conftest import ROOT, T0, make_telemetry


@pytest.fixture
def journal(tmp_path: Path):  # type: ignore[no-untyped-def]
    j = Journal(tmp_path / "run")
    yield j
    j.close()


@pytest.fixture
def llm(config: AppConfig, journal: Journal) -> LLMClient:
    return LLMClient(LLMConfig(mode="mock"), journal, env={})


def alert(kind: str, level: AlertLevel = AlertLevel.WARNING, field: str | None = None, t: float = 0.0, msg: str = "") -> Alert:
    return Alert(ts=T0 + timedelta(seconds=t), level=level, kind=kind, field=field, message=msg or f"{kind} {field}")


def verdict(kind: str, rule: str, fields: list[str], explanation: str = "объяснение") -> Verdict:
    return Verdict(ts=T0, kind=kind, confidence=0.9, rule=rule, fields=fields, explanation=explanation)  # type: ignore[arg-type]


def sit(t: float = 0.0, **kw: Any) -> Situation:
    tel = kw.pop("telemetry", None) or make_telemetry(t)
    return Situation(ts=T0 + timedelta(seconds=t), telemetry=tel, elapsed_s=t, **kw)


def energy_situation(config: AppConfig, soc: float, pv: float, load: float, **tel_kw: Any) -> Situation:
    st = State()
    for i in range(90):
        st.update(make_telemetry(i, battery_soc_pct=soc, pv_power_w=pv, load_total_w=load, **tel_kw))
    status = energy_status(st, config)
    tel = st.latest()
    assert status is not None and tel is not None
    planner = EnergyPlanner(config)
    tracker = ProcessTracker(config)
    return Situation(
        ts=tel.ts,
        telemetry=tel,
        energy=status,
        plan=planner.order(tracker, tel, status),
        advice=planner.shedding_advice(tel, status, tracker),
        elapsed_s=89.0,
    )


# --------------------------------------------------------------------------- промпты


def test_all_prompts_exist_and_few_shot_examples_match_schemas() -> None:
    schemas: dict[str, type[BaseModel]] = {
        "monitor": MonitorOutput,
        "diagnost": DiagnostOutput,
        "energy_planner": EnergyOutput,
    }
    for name, schema in schemas.items():
        text = load_prompt(name)
        assert text.strip() and "ТОЛЬКО" in text
        examples = re.findall(r"Ответ:\s*\n(\{.*\})", text)
        assert len(examples) >= 2, name
        for ex in examples:
            schema.model_validate(json.loads(ex))  # few-shot не должен противоречить схеме
    assert {p.stem for p in PROMPTS_DIR.glob("*.md")} == set(schemas)


# --------------------------------------------------------------------------- матрица


def test_matrix_is_valid_and_documented() -> None:
    rows = load_matrix()
    assert 8 <= len(rows) <= 30
    for r in rows:
        assert abs(sum(c.probability for c in r.causes) - 1.0) < 0.01, r.id
        assert all(c.safe_check for c in r.causes)
    doc = (ROOT / "docs" / "SYMPTOM_MATRIX.md").read_text(encoding="utf-8")
    assert doc == render_markdown(), "docs/SYMPTOM_MATRIX.md устарел: python -m kupol.agents.symptoms > docs/SYMPTOM_MATRIX.md"


def test_rank_causes_boosted_by_verdict() -> None:
    row = next(r for r in load_matrix() if r.id == "co2_single_spike")
    plain = rank_causes(row, [])
    boosted = rank_causes(row, [verdict("real", "co2_outlier", ["co2_ppm.co2_b"])])
    real_cause = "Реальный локальный источник CO2 рядом с датчиком"
    p0 = next(h.probability for h in plain if h.cause == real_cause)
    p1 = next(h.probability for h in boosted if h.cause == real_cause)
    assert p1 > p0 and abs(sum(h.probability for h in boosted) - 1.0) < 0.03
    assert [h.probability for h in boosted] == sorted((h.probability for h in boosted), reverse=True)


def test_row_matching_disambiguates_co2_cases() -> None:
    a = alert("threshold", AlertLevel.CRITICAL, "co2_ppm.co2_b")
    spike = verdict("false_signal", "co2_outlier", ["co2_ppm.co2_b"])
    assert [r.id for r in match_rows([a], [spike])] == ["co2_single_spike"]
    real = verdict("real", "co2_consensus_high", ["co2_ppm.co2_a", "co2_ppm.co2_b", "co2_ppm.co2_c"])
    assert [r.id for r in match_rows([a], [real])] == ["co2_high_consensus"]
    assert {r.id for r in match_rows([a], [])} == {"co2_single_spike", "co2_high_consensus"}
    assert match_rows([], []) == []


def test_row_matching_by_verdict_only() -> None:
    v = verdict("sensor_fault", "energy_balance", ["battery_soc_pct"])
    assert [r.id for r in match_rows([], [v])] == ["energy_balance_mismatch"]


# --------------------------------------------------------------------------- монитор: код


def test_monitor_enables_vent_for_soldering(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    mon = Monitor(config, llm, journal)
    tel = make_telemetry(0, solder_status="running", vent_mode="off")
    recs = mon.react(sit(0, telemetry=tel))
    assert len(recs) == 1 and recs[0].priority == 1
    assert [(c.name, c.args) for c in recs[0].commands] == [("vent_enable", {"mode": "low"})]
    assert not recs[0].needs_human
    assert mon.react(sit(10, telemetry=tel)) == []  # пауза между командами
    assert len(mon.react(sit(31, telemetry=tel))) == 1


def test_monitor_quiet_when_vent_already_on_or_no_soldering(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    mon = Monitor(config, llm, journal)
    assert mon.react(sit(0, telemetry=make_telemetry(0, solder_status="running", vent_mode="low"))) == []
    assert mon.react(sit(0, telemetry=make_telemetry(0, solder_status="idle", vent_mode="off"))) == []
    assert mon.react(Situation(ts=T0, telemetry=None)) == []


def test_monitor_escalates_instead_of_spamming_when_vent_dead(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    mon = Monitor(config, llm, journal)
    tel = make_telemetry(0, solder_status="running", vent_mode="off")
    recs = mon.react(sit(0, telemetry=tel, vent_ok=False))
    assert len(recs) == 1 and recs[0].commands == [] and recs[0].needs_human and recs[0].priority == 1
    assert "прекратить пайку" in recs[0].title
    assert mon.react(sit(60, telemetry=tel, vent_ok=False)) == []  # не чаще раза в 2 минуты
    assert len(mon.react(sit(130, telemetry=tel, vent_ok=False))) == 1


def test_monitor_comms_loss_takes_no_decisions(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    mon = Monitor(config, llm, journal)
    tel = make_telemetry(0, comms_ok=False, solder_status="running", vent_mode="off")
    recs = mon.react(sit(0, telemetry=tel))
    assert len(recs) == 1 and recs[0].commands == [] and "Связь потеряна" in recs[0].title
    assert mon.react(sit(5, telemetry=tel)) == []


def test_monitor_never_enables_vent_for_false_co2(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    mon = Monitor(config, llm, journal)
    a = alert("threshold", AlertLevel.CRITICAL, "co2_ppm.co2_b")
    v = verdict("false_signal", "co2_outlier", ["co2_ppm.co2_b"], "co2_b врёт")
    recs = mon.react(sit(0, alerts=[a], verdicts=[v]))
    assert len(recs) == 1 and recs[0].commands == [] and "недостоверно" in recs[0].title
    # то же для сбоя датчика и при любой дальнейшей эскалации
    assert all(not r.commands for r in mon.react(sit(500, alerts=[a], verdicts=[v])))


def test_monitor_real_co2_enables_vent_low_only(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    mon = Monitor(config, llm, journal)
    a = alert("threshold", AlertLevel.WARNING, "co2_ppm.co2_a")
    v = verdict("real", "co2_consensus_high", ["co2_ppm.co2_a", "co2_ppm.co2_b"])
    recs = mon.react(sit(0, alerts=[a], verdicts=[v], telemetry=make_telemetry(0, vent_mode="off")))
    assert [(c.name, c.args["mode"]) for r in recs for c in r.commands] == [("vent_enable", "low")]


def test_monitor_critical_real_co2_asks_human_for_high(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    mon = Monitor(config, llm, journal)
    a = alert("threshold", AlertLevel.CRITICAL, "co2_ppm.co2_a")
    v = verdict("real", "co2_consensus_high", ["co2_ppm.co2_a"])
    v = v.model_copy(update={"evidence": [Evidence(source=s, supports="real", weight=0.5, rule="co2_consensus_high") for s in ("co2_ppm.co2_b", "co2_ppm.co2_c")]})
    recs = mon.react(sit(0, alerts=[a], verdicts=[v], telemetry=make_telemetry(0, vent_mode="low")))
    assert len(recs) == 1 and recs[0].needs_human
    cmd = recs[0].commands[0]
    assert cmd.name == "set_vent_mode" and cmd.args == {"mode": "high"}
    assert cmd.costly and {e.source for e in cmd.evidence} == {"co2_ppm.co2_b", "co2_ppm.co2_c"}  # правило двух выполнено


def test_monitor_critical_real_co2_without_two_sources_does_not_propose_high(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    mon = Monitor(config, llm, journal)
    a = alert("threshold", AlertLevel.CRITICAL, "co2_ppm.co2_a")
    v = verdict("real", "co2_consensus_high", ["co2_ppm.co2_a"])  # уверенность есть, независимых источников нет
    recs = mon.react(sit(0, alerts=[a], verdicts=[v], telemetry=make_telemetry(0, vent_mode="low")))
    assert not [c for r in recs for c in r.commands]


def test_monitor_ignores_co2_without_verdict(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    mon = Monitor(config, llm, journal)
    a = alert("threshold", AlertLevel.CRITICAL, "co2_ppm.co2_a")
    assert mon.react(sit(0, alerts=[a])) == []


# --------------------------------------------------------------------------- монитор: модель


def test_monitor_mock_and_fallback(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    mon = Monitor(config, llm, journal)
    s = sit(0, alerts=[alert("comms_loss", AlertLevel.CRITICAL, msg="Связь потеряна")], verdicts=[])
    res = mon.run(s)
    assert not res.used_fallback and res.recommendations[0].priority == 1
    assert "Оператору" in res.recommendations[0].rationale
    fb = mon.fallback(s)
    assert fb[0].title.startswith("Тревоги без интерпретации") and "Связь потеряна" in fb[0].rationale


def test_monitor_fingerprint_and_claim(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    mon = Monitor(config, llm, journal)
    assert mon.fingerprint(sit(0)) is None and not mon.try_claim(sit(0))
    a = alert("threshold", AlertLevel.WARNING, "temp_c")
    assert mon.try_claim(sit(0, alerts=[a], new_alerts=[a]))
    assert not mon.try_claim(sit(5, alerts=[a]))  # тот же вход
    b = alert("threshold", AlertLevel.WARNING, "humidity_pct")
    assert not mon.try_claim(sit(10, alerts=[a, b], new_alerts=[b]))  # вход изменился, но интервал не вышел
    assert mon.try_claim(sit(40, alerts=[a, b]))  # интервал прошёл
    c = alert("threshold", AlertLevel.CRITICAL, "temp_c")
    assert mon.try_claim(sit(41, alerts=[a, b, c], new_alerts=[c]))  # срочно: новая критическая


# --------------------------------------------------------------------------- диагност


def test_diagnost_prefers_false_signal_when_verdict_says_so(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    dg = Diagnost(config, llm, journal)
    s = sit(
        0,
        alerts=[alert("threshold", AlertLevel.CRITICAL, "co2_ppm.co2_b"), alert("jump", field="co2_ppm.co2_b")],
        verdicts=[verdict("false_signal", "co2_outlier", ["co2_ppm.co2_b"])],
    )
    res = dg.run(s)
    assert not res.used_fallback
    rec = res.recommendations[0]
    assert "Ложный сигнал" in rec.hypotheses[0].cause and rec.hypotheses[0].probability > 0.5
    assert all(h.safe_check for h in rec.hypotheses)
    assert rec.commands == []  # Диагност команд не подаёт
    probs = [h.probability for h in rec.hypotheses]
    assert probs == sorted(probs, reverse=True) and abs(sum(probs) - 1.0) < 0.05


def test_diagnost_real_co2_flips_ranking(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    dg = Diagnost(config, llm, journal)
    s = sit(
        0,
        alerts=[alert("threshold", AlertLevel.WARNING, "co2_ppm.co2_a")],
        verdicts=[verdict("real", "co2_consensus_high", ["co2_ppm.co2_a", "co2_ppm.co2_b"])],
    )
    top = dg.run(s).recommendations[0].hypotheses[0]
    assert "Реальное накопление CO2" in top.cause


def test_diagnost_unknown_symptom(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    dg = Diagnost(config, llm, journal)
    s = sit(0, alerts=[alert("something_new", AlertLevel.WARNING, "x")])
    rec = dg.run(s).recommendations[0]
    assert len(rec.hypotheses) == 1 and "не определена" in rec.hypotheses[0].cause


def test_diagnost_fallback_is_checklist_7_3(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    dg = Diagnost(config, llm, journal)
    s = sit(0, alerts=[alert("actuator_no_effect", field="vent")])
    rec = dg.fallback(s)[0]
    assert rec.needs_human and "п. 7.3" in rec.title
    steps = rec.rationale.splitlines()
    assert steps[0].startswith("1.") and steps[6].startswith("7.") and all(s[0] == str(i + 1) for i, s in enumerate(steps[:7]))
    assert "Уровень реакции (код)" in rec.rationale and "Доступные безопасные пробы" in rec.rationale
    assert rec.hypotheses and "Отказ исполнителя" in rec.hypotheses[0].cause
    assert len(checklist_7_3([], [])) == 7


def test_diagnost_ignores_pure_energy_alerts_in_fingerprint(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    dg = Diagnost(config, llm, journal)
    assert dg.fingerprint(sit(0, alerts=[alert("energy_t_left", AlertLevel.CRITICAL)])) is None


# --------------------------------------------------------------------------- энергопланировщик


def test_energy_planner_applies_candidates_from_code(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    lines = {"humidifier": LineState(on=True, power_w=80.0), "lighting": LineState(on=True, power_w=100.0)}
    s = energy_situation(config, 27.0, 300.0, 1250.0, humidifier_mode="high", lines=lines, humidity_pct=50.0)
    assert s.advice, "для проверки нужны кандидаты"
    res = EnergyPlannerAgent(config, llm, journal).run(s)
    rec = res.recommendations[0]
    assert not res.used_fallback and "мин до минимального заряда" in rec.title
    allowed = {(a.command.name, json.dumps(a.command.args, sort_keys=True)) for a in s.advice if a.command}
    assert rec.commands and {(c.name, json.dumps(c.args, sort_keys=True)) for c in rec.commands} <= allowed
    assert "Порядок этапов" in rec.rationale and rec.priority <= 2


def test_energy_planner_defers_only_low_priority(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    s = energy_situation(config, 24.0, 100.0, 1000.0)
    rec = EnergyPlannerAgent(config, llm, journal).run(s).recommendations[0]
    assert "Отложить" in rec.rationale
    deferred = rec.rationale.split("Отложить (указание Оператору): ")[1].splitlines()[0]
    for protected in ("Фрезеровка", "Печать", "Пайка"):
        assert protected not in deferred


def test_energy_planner_ok_state(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    s = energy_situation(config, 80.0, 1800.0, 600.0)
    rec = EnergyPlannerAgent(config, llm, journal).run(s).recommendations[0]
    assert "Энергия в норме" in rec.title and rec.commands == [] and not rec.needs_human


def test_energy_planner_rejects_invented_names_and_indexes(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    s = energy_situation(config, 24.0, 100.0, 1000.0)
    ag = EnergyPlannerAgent(config, llm, journal)
    with pytest.raises(ValueError, match="неизвестные этапы"):
        ag.to_recommendations(EnergyOutput(summary="s", order=["Выдуманный этап"], rationale="r"), s)
    with pytest.raises(ValueError, match="кандидатов"):
        ag.to_recommendations(EnergyOutput(summary="s", order=[], apply_advice=[99], rationale="r"), s)


def test_energy_planner_falls_back_when_model_output_contradicts_code(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    s = energy_situation(config, 24.0, 100.0, 1000.0)

    class Liar(EnergyPlannerAgent):
        def mock(self, sit: Situation) -> EnergyOutput:
            return EnergyOutput(summary="s", order=["Выдуманный этап"], rationale="r")

    res = Liar(config, llm, journal).run(s)
    assert res.used_fallback and all(r.used_fallback for r in res.recommendations)
    assert any(e.status == "fallback" for e in journal.entries(kinds=["agent"]))


def test_energy_planner_fingerprint_periodic(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    ag = EnergyPlannerAgent(config, llm, journal)
    s = energy_situation(config, 80.0, 1800.0, 600.0)
    assert ag.try_claim(s) and not ag.try_claim(s)
    later = energy_situation(config, 80.0, 1800.0, 600.0)
    later.ts = s.ts + timedelta(seconds=400)
    later.elapsed_s = 400.0  # следующая «корзина» планового обновления
    assert ag.try_claim(later)
    assert ag.fingerprint(Situation(ts=T0, telemetry=None)) is None


# --------------------------------------------------------------------------- критерий приёмки 7


@pytest.mark.parametrize("failure", ["timeout", "bad_json"])
def test_all_agents_fall_back_when_llm_unavailable(config: AppConfig, journal: Journal, failure: str) -> None:
    llm = LLMClient(LLMConfig(mode="mock"), journal, env={})
    llm.simulate_failure = failure  # type: ignore[assignment]
    energy = energy_situation(config, 24.0, 100.0, 1000.0)
    tail = Situation(
        ts=energy.ts,
        telemetry=energy.telemetry,
        alerts=[alert("threshold", AlertLevel.CRITICAL, "co2_ppm.co2_b"), alert("energy_t_left", AlertLevel.CRITICAL)],
        verdicts=[verdict("false_signal", "co2_outlier", ["co2_ppm.co2_b"])],
        energy=energy.energy,
        plan=energy.plan,
        advice=energy.advice,
        elapsed_s=energy.elapsed_s,
    )
    agents: list[Agent] = [Monitor(config, llm, journal), Diagnost(config, llm, journal), EnergyPlannerAgent(config, llm, journal)]
    for agent in agents:
        res = agent.run(tail)
        assert res.used_fallback, agent.name
        assert res.recommendations and all(r.used_fallback for r in res.recommendations), agent.name
    calls = journal.entries(kinds=["llm_call"])
    assert [c.actor for c in calls] == ["monitor", "diagnost", "energy_planner"]
    assert all(c.data["used_fallback"] is True for c in calls)
    assert len(journal.entries(kinds=["agent"])) == 3


def test_every_llm_call_logs_inputs(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    Monitor(config, llm, journal).run(sit(0, alerts=[alert("threshold", AlertLevel.WARNING, "temp_c")]))
    data = journal.entries(kinds=["llm_call"])[0].data
    assert data["inputs"]["alerts"][0]["field"] == "temp_c" and data["parsed_decision"]["priority"] == 2
    assert data["inputs_summary"]["alerts"] == ["warning:threshold:temp_c"]


def test_recommendation_defaults_used_fallback_false(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    rec = Monitor(config, llm, journal).run(sit(0, alerts=[alert("threshold", AlertLevel.WARNING, "temp_c")])).recommendations[0]
    assert isinstance(rec, Recommendation) and rec.used_fallback is False
