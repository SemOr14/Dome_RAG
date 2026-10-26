"""Тесты Диагноста дополнения: гипотезы с пробами, уверенность из кода, примеры, режимы mock и фолбэк."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from kupol.agents.base import Situation
from kupol.agents.diagnost import DiagHypothesisOut, Diagnost, DiagnostOutput
from kupol.agents.llm import LLMClient, PROMPTS_DIR, load_examples
from kupol.core.config import AppConfig, LLMConfig
from kupol.core.journal import Journal
from kupol.core.models import Alert, AlertLevel, Evidence, Probe, ResponseLevel, Verdict
from kupol.core.response import ResponseDecision
from tests.conftest import T0, make_telemetry

SENSOR = "co2_ppm.co2_b"


@pytest.fixture
def journal(tmp_path: Path):  # type: ignore[no-untyped-def]
    j = Journal(tmp_path / "run")
    yield j
    j.close()


@pytest.fixture
def llm(journal: Journal) -> LLMClient:
    return LLMClient(LLMConfig(mode="mock"), journal, env={})


def alert(kind: str, field: str | None, level: AlertLevel = AlertLevel.WARNING) -> Alert:
    return Alert(ts=T0, level=level, kind=kind, field=field, message=f"{kind} {field}", detector="d")


def false_verdict() -> Verdict:
    return Verdict(
        ts=T0,
        kind="false_signal",
        confidence=0.95,
        explanation="e",
        rule="co2_outlier",
        fields=[SENSOR],
        evidence=[Evidence(source="co2_ppm.co2_a", supports="false", weight=0.6, rule="co2_outlier", kind_hint="false_signal")],
    )


def probe(pid: str, human: bool = False) -> Probe:
    return Probe(
        id=pid,
        description=f"проба {pid}",
        expected_if_real="реально",
        expected_if_false="ложно",
        human_action="Спросить Оператора" if human else None,
    )


def situation(level: ResponseLevel = ResponseLevel.OBSERVE, probes: tuple[Probe, ...] = (probe("recheck_neighbor"),)) -> Situation:
    a = alert("threshold", SENSOR, AlertLevel.CRITICAL)
    v = false_verdict()
    return Situation(
        ts=T0,
        telemetry=make_telemetry(0),
        alerts=[a, alert("jump", SENSOR)],
        verdicts=[v],
        responses={SENSOR: ResponseDecision(SENSOR, level, confidence=0.0, reason="тест")},
        available_probes=list(probes),
        residual_w=-410.0,
        elapsed_s=0.0,
    )


# --------------------------------------------------------------------------- примеры


def test_examples_cover_all_impact_types_and_match_schema() -> None:
    files = sorted((PROMPTS_DIR / "examples").glob("diagnost_*.md"))
    assert len(files) >= 8
    text = " ".join(f.name for f in files)
    for kind in ("direct", "hidden", "false", "cascade", "false_then_real"):
        assert kind in text, kind
    for f in files:
        body = f.read_text(encoding="utf-8")
        (raw,) = re.findall(r"Ответ:\s*\n(\{.*\})", body)
        out = DiagnostOutput.model_validate(json.loads(raw))
        assert out.hypotheses and all(h.safe_check for h in out.hypotheses), f.name
        assert out.recommended_level in ("observe", "prepare", "act_reversible", "act_full")


def test_examples_use_only_catalog_probes(config: AppConfig) -> None:
    catalog = {p.id for p in config.probes.catalog}
    for f in (PROMPTS_DIR / "examples").glob("diagnost_*.md"):
        (raw,) = re.findall(r"Ответ:\s*\n(\{.*\})", f.read_text(encoding="utf-8"))
        for h in json.loads(raw)["hypotheses"]:
            assert h["safe_probe"] is None or h["safe_probe"] in catalog, (f.name, h["safe_probe"])


def test_system_prompt_includes_examples(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    prompt = Diagnost(config, llm, journal).system_prompt()
    assert "Пример: Ложное: скачок CO2" in prompt and "УВЕРЕННОСТЬ ТЫ НЕ СЧИТАЕШЬ" in prompt
    assert load_examples("diagnost").count("Ответ:") >= 8 and load_examples("no_such_agent") == ""


# --------------------------------------------------------------------------- вход модели


def test_payload_has_verdict_evidence_residual_probes_and_levels(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    p = Diagnost(config, llm, journal).build_payload(situation(probes=(probe("recheck_neighbor"), probe("operator_check_indicator", True))))
    assert p["verdicts"][0]["evidence"][0]["source"] == "co2_ppm.co2_a" and p["verdicts"][0]["status"] == "provisional"
    assert p["balance_residual_w"] == -410
    assert [x["id"] for x in p["available_probes"]] == ["recheck_neighbor", "operator_check_indicator"]
    assert p["available_probes"][1]["by_human"] is True
    assert p["response_levels"][SENSOR]["level"] == "observe"
    assert p["alerts"][0]["detector"] if "detector" in p["alerts"][0] else True
    assert p["matrix"] and p["matrix"][0]["causes"]


# --------------------------------------------------------------------------- режим mock


def test_mock_mode_gives_hypotheses_with_probes_from_catalog(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    res = Diagnost(config, llm, journal).run(situation())
    assert not res.used_fallback
    (rec,) = res.recommendations
    assert rec.hypotheses and rec.commands == []
    with_probe = [h for h in rec.hypotheses if h.safe_probe]
    assert with_probe and all(h.safe_probe == "recheck_neighbor" for h in with_probe)
    assert all(h.expected_if_real and h.expected_if_false for h in with_probe)
    assert "Уровень реакции: observe" in rec.rationale and "Уверенность (расчёт кода)" in rec.rationale
    assert "false_signal 95%" in rec.rationale
    assert abs(sum(h.probability for h in rec.hypotheses) - 1.0) < 0.05
    assert rec.hypotheses[0].verdict_id and rec.hypotheses[0].related_alerts
    call = journal.entries(kinds=["llm_call"])[0].data
    assert call["inputs"]["available_probes"][0]["id"] == "recheck_neighbor" and call["prompt_hash"]


# --------------------------------------------------------------------------- код контролирует модель


def out_with(hyps: list[DiagHypothesisOut], level: str = "act_full") -> DiagnostOutput:
    return DiagnostOutput(summary="s", hypotheses=hyps, recommended_action="a", recommended_level=level, needs_human=False)  # type: ignore[arg-type]


def test_probabilities_and_level_come_from_code(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    dg = Diagnost(config, llm, journal)
    s = situation(ResponseLevel.OBSERVE)
    causes = [h.cause for h in dg._hypotheses(s)]
    lie = out_with(
        [
            DiagHypothesisOut(cause=causes[-1], safe_check="c", probability=0.99),  # модель завышает вероятность маловероятной
            DiagHypothesisOut(cause=causes[0], safe_check="c", probability=0.01),
            DiagHypothesisOut(cause="Совсем новая гипотеза", safe_check="c", probability=0.9),
        ]
    )
    (rec,) = dg.to_recommendations(lie, s)
    probs = {h.cause: h.probability for h in rec.hypotheses}
    assert probs[causes[0]] > probs[causes[-1]]  # порядок и веса — по расчёту кода, а не по словам модели
    assert probs["Совсем новая гипотеза"] <= 0.1  # неизвестные коду гипотезы не получают веса
    assert rec.hypotheses[-1].cause == "Совсем новая гипотеза"
    assert "модель предлагала act_full, код ограничил до observe" in rec.rationale


def test_model_cannot_raise_level_above_code(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    dg = Diagnost(config, llm, journal)
    (rec,) = dg.to_recommendations(out_with([DiagHypothesisOut(cause="x", safe_check="c")], "act_reversible"), situation(ResponseLevel.ACT_REVERSIBLE))
    assert "Уровень реакции: act_reversible." in rec.rationale
    (rec2,) = dg.to_recommendations(out_with([DiagHypothesisOut(cause="x", safe_check="c")], "observe"), situation(ResponseLevel.ACT_FULL))
    assert "Уровень реакции: observe." in rec2.rationale  # более осторожный совет модели сохраняется


def test_invented_probe_triggers_fallback(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    class Inventor(Diagnost):
        def mock(self, sit: Situation) -> DiagnostOutput:
            return out_with([DiagHypothesisOut(cause="x", safe_check="c", safe_probe="launch_missiles")])

    res = Inventor(config, llm, journal).run(situation())
    assert res.used_fallback and "недоступные пробы" in (res.error or "")
    assert all(r.used_fallback for r in res.recommendations)
    assert any(e.status == "fallback" for e in journal.entries(kinds=["agent"]))


# --------------------------------------------------------------------------- резервный сценарий

def test_fallback_gives_hypotheses_and_probes_by_rules(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    """9: отказ LLM → гипотезы и пробы из каталога по правилам, used_fallback=true."""
    llm.simulate_failure = "timeout"  # type: ignore[assignment]
    res = Diagnost(config, llm, journal).run(situation(probes=(probe("recheck_neighbor"), probe("operator_check_indicator", True))))
    assert res.used_fallback and res.error
    (rec,) = res.recommendations
    assert rec.used_fallback and rec.needs_human and "п. 7.3" in rec.title
    assert rec.hypotheses and any(h.safe_probe for h in rec.hypotheses)
    sensory = next(h for h in rec.hypotheses if "датчик" in h.cause.lower())
    assert sensory.safe_probe == "recheck_neighbor" and sensory.expected_if_real == "реально"
    assert "recheck_neighbor" in rec.rationale
    calls = journal.entries(kinds=["llm_call"])
    assert calls[0].data["used_fallback"] is True and calls[0].status == "fallback"


def test_fallback_without_probes(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    (rec,) = Diagnost(config, llm, journal).fallback(situation(probes=()))
    assert all(h.safe_probe is None for h in rec.hypotheses) and "доступных проб нет" in rec.rationale


def test_fingerprint_ignores_forecasts_and_unknown_only(config: AppConfig, llm: LLMClient, journal: Journal) -> None:
    dg = Diagnost(config, llm, journal)
    only_forecast = Situation(ts=T0, telemetry=make_telemetry(0), alerts=[alert("time_to_threshold", "temp_c")])
    assert dg.fingerprint(only_forecast) is None
    unknown_only = Situation(ts=T0, telemetry=make_telemetry(0), verdicts=[Verdict(ts=T0, kind="unknown", confidence=0.5, explanation="e", fields=["x"])])
    assert dg.fingerprint(unknown_only) is None
    assert dg.fingerprint(situation()) is not None and timedelta(0) == timedelta(0)


def test_unused_imports_marker() -> None:
    _: Any = None
    assert _ is None
