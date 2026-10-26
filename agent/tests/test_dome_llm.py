"""DeepSeek-советник: список моделей, запись llm_call, бюджет, сбои; интеграция с рантаймом."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from kupol.dome.agentlog import AgentLog
from kupol.dome.config import AdvisorCfg
from kupol.dome.llm import DeepSeek, Diagnosis, ForbiddenModel, LlmAdvisor, Reply
from kupol.dome.rag import Chunk, Rag
from tests.test_dome_scenarios import Harness, World, run, start_work


def make(tmp_path: Path, handler: Any, **cfg: Any) -> tuple[DeepSeek, AgentLog]:
    log = AgentLog(tmp_path, secrets=["sk-secret-123456"], run_id="llm")
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return DeepSeek("sk-secret-123456", "https://api.test", "deepseek-chat", log, AdvisorCfg(**cfg), http=http), log


def ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 120, "completion_tokens": 30}})


def rows(log: AgentLog) -> list[dict[str, Any]]:
    return [json.loads(x) for x in log.path.read_text(encoding="utf-8").splitlines()]


def test_foreign_model_is_forbidden(tmp_path: Path) -> None:
    log = AgentLog(tmp_path, run_id="x")
    with pytest.raises(ForbiddenModel):
        DeepSeek("k", "https://api.test", "gpt-4o", log)


def test_successful_call_is_logged_with_tokens_and_no_key(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append({"auth": req.headers["authorization"], "body": json.loads(req.content)})
        return ok('{"situation": "шум датчиков", "cause": "наводки от освещения", "confidence": 0.7, "plan": ["наблюдать"], "say": null}')

    llm, log = make(tmp_path, handler)
    d = llm.ask("noise", "диагностика", "система", {"x": 1}, Diagnosis, ref=5)
    assert d is not None and d.cause == "наводки от освещения" and d.say is None
    assert seen[0]["body"]["model"] == "deepseek-chat" and seen[0]["body"]["response_format"] == {"type": "json_object"}
    rec = [r for r in rows(log) if r["kind"] == "llm_call"][0]
    assert rec["ok"] is True and rec["prompt_tokens"] == 120 and rec["completion_tokens"] == 30 and rec["ref"] == 5 and rec["model"] == "deepseek-chat"
    assert rec["latency_ms"] >= 0 and "наводки" in rec["summary"]
    assert "sk-secret-123456" not in log.path.read_text(encoding="utf-8")


def test_bad_json_and_http_errors_give_none_and_log_failure(tmp_path: Path) -> None:
    llm, log = make(tmp_path, lambda r: ok("это не json"), min_interval_s=0)
    assert llm.ask("t", "p", "s", {}, Diagnosis) is None
    llm2, log2 = make(tmp_path / "b", lambda r: httpx.Response(500), min_interval_s=0)
    assert llm2.ask("t", "p", "s", {}, Diagnosis) is None
    assert [r["ok"] for r in rows(log) if r["kind"] == "llm_call"] == [False]
    assert "500" in [r for r in rows(log2) if r["kind"] == "llm_call"][0]["error"]


def test_fenced_json_is_accepted(tmp_path: Path) -> None:
    llm, _ = make(tmp_path, lambda r: ok('```json\n{"text": "Всё в норме"}\n```'))
    assert llm.ask("chat", "p", "s", {}, Reply) == Reply(text="Всё в норме")


def test_budget_interval_and_hourly_limit(tmp_path: Path) -> None:
    t = [1000.0]
    log = AgentLog(tmp_path, run_id="b")
    llm = DeepSeek("k", "https://api.test", "deepseek-chat", log, AdvisorCfg(min_interval_s=60, max_calls_per_hour=2), http=httpx.Client(transport=httpx.MockTransport(lambda r: ok('{"text": "x"}'))), clock=lambda: t[0])
    assert llm.allowed("a")
    llm.ask("a", "p", "s", {}, Reply)
    assert not llm.allowed("a") and llm.allowed("b")  # интервал по теме
    t[0] += 61
    assert llm.allowed("a")
    llm.ask("a", "p", "s", {}, Reply)
    assert not llm.allowed("c")  # часовой лимит 2 вызова
    t[0] += 3600
    assert llm.allowed("c")


def test_advisor_without_llm_still_gives_references(tmp_path: Path) -> None:
    adv = LlmAdvisor(Rag([Chunk("k#1", "Ложная тревога CO2: пометить датчик недостоверным")]), None)
    assert adv.references("ложная тревога CO2")[0]["ref"] == "k#1"
    assert adv.diagnose("t", {}) is None and adv.answer("вопрос?", {}) is None


# --------------------------------------------------------------------------- интеграция с рантаймом


class FakeAdvisor:
    def __init__(self) -> None:
        self.diagnosed: list[str] = []

    def references(self, query: str) -> list[dict[str, Any]]:
        return [{"ref": "knowledge/04.md#шум", "text": "шум коррелирует со светом"}]

    def diagnose(self, topic: str, context: dict[str, Any], ref: int | None = None) -> Diagnosis:
        self.diagnosed.append(topic)
        return Diagnosis(situation=topic, cause="неизвестный код", confidence=0.6, plan=["наблюдать"], say="Станок встал по незнакомой причине — нужен осмотр.")

    def answer(self, question: str, context: dict[str, Any], ref: int | None = None) -> str | None:
        return f"По данным: SOC {context['SOC_%']:.0f} %."


def test_free_question_is_answered_by_model_and_unknown_hold_is_diagnosed(tmp_path: Path) -> None:
    async def scenario() -> None:
        w = World()
        start_work(w)
        async with Harness(tmp_path, w) as h:
            assert h.runner is not None
            adv = FakeAdvisor()
            h.runner.advisor = adv
            await h.step(2)
            await h.operator("Почему шумят датчики климата?")
            await h.step(2)
            await h.until(lambda: any("По данным" in p for p in w.posted))
            w.nodes["cnc_01"].update({"state": "PAUSED", "paused": True, "hold_level": "WARNING", "hold_reason": "GCODE_ERR"})
            await h.step(2)
            await h.until(lambda: any("незнакомой причине" in p for p in w.posted))
            dec = [r for r in h.records("decision") if r["source"] in ("llm", "mixed") and "GCODE_ERR" in r["situation"]]
            assert dec and dec[0]["rag_refs"] == ["knowledge/04.md#шум"]
            assert adv.diagnosed == ["cnc_01: удержание GCODE_ERR"]

    run(scenario())


def test_question_without_model_gets_clarification(tmp_path: Path) -> None:
    async def scenario() -> None:
        w = World()
        async with Harness(tmp_path, w) as h:
            await h.step(2)
            await h.operator("Почему шумят датчики климата?")
            await h.step(2)
            assert any("Не понял запрос" in p for p in w.posted)

    run(scenario())
