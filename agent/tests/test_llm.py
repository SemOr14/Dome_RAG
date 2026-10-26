"""Тесты обёртки LLM: mock, фолбэк, строгий JSON, запрет чужой модели (критерий приёмки 8)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from kupol.agents.llm import (
    ForbiddenModelError,
    LLMClient,
    LLMConfigError,
    load_prompt,
    prompt_hash,
)
from kupol.core.config import LLMConfig
from kupol.core.journal import Journal


class Out(BaseModel):
    answer: str
    score: int


GOOD = json.dumps({"answer": "ок", "score": 3}, ensure_ascii=False)


def cfg(**kw: object) -> LLMConfig:
    base: dict[str, object] = dict(mode="dev", timeout_s=5, base_url="https://api.test")
    base.update(kw)
    return LLMConfig(**base)  # type: ignore[arg-type]


def reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def client_with(
    handler: Callable[[httpx.Request], httpx.Response],
    journal: Journal | None = None,
    **cfg_kw: object,
) -> LLMClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return LLMClient(cfg(**cfg_kw), journal, http_client=http, env={"DEEPSEEK_API_KEY": "sk-secret"})


@pytest.fixture
def journal(tmp_path: Path):  # type: ignore[no-untyped-def]
    j = Journal(tmp_path / "run")
    yield j
    j.close()


def llm_entries(j: Journal):  # type: ignore[no-untyped-def]
    return j.entries(kinds=["llm_call"])


# --------------------------------------------------------------------------- mock


def test_mock_returns_stub_and_logs(journal: Journal) -> None:
    client = LLMClient(LLMConfig(mode="mock"), journal, env={})
    res = client.call("monitor", "промпт", {"a": 1}, Out, mock=lambda: Out(answer="x", score=1), inputs_summary="сводка")
    assert res.parsed == Out(answer="x", score=1) and not res.used_fallback and res.attempts == 0
    e = llm_entries(journal)[0]
    assert e.actor == "monitor" and e.status == "ok"
    assert e.data["mode"] == "mock" and e.data["inputs"] == {"a": 1} and e.data["inputs_summary"] == "сводка"
    assert e.data["prompt_hash"] == prompt_hash("промпт") and e.data["parsed_decision"] == {"answer": "x", "score": 1}


def test_mock_without_stub_falls_back(journal: Journal) -> None:
    res = LLMClient(LLMConfig(mode="mock"), journal, env={}).call("a", "p", {}, Out)
    assert res.used_fallback and res.parsed is None
    assert llm_entries(journal)[0].data["used_fallback"] is True


@pytest.mark.parametrize("failure", ["timeout", "bad_json"])
def test_mock_simulated_failure_falls_back(journal: Journal, failure: str) -> None:
    client = LLMClient(LLMConfig(mode="mock"), journal, env={})
    client.simulate_failure = failure  # type: ignore[assignment]
    res = client.call("diagnost", "p", {}, Out, mock=lambda: Out(answer="x", score=1))
    assert res.used_fallback and res.parsed is None and res.error
    e = llm_entries(journal)[0]
    assert e.status == "fallback" and e.data["used_fallback"] is True and e.data["error"]


def test_mock_needs_no_key() -> None:
    client = LLMClient(LLMConfig(mode="mock"), env={})
    assert client.mode == "mock" and client.model == "deepseek-v4-flash"
    client.call("a", "p", {}, Out, mock=lambda: Out(answer="x", score=1))  # без журнала не падает


# --------------------------------------------------------------------------- запрет чужой модели


def test_competition_rejects_foreign_model_in_constructor() -> None:
    bad = LLMConfig(mode="competition").model_copy(update={"model": "gpt-4o"})  # в обход валидации конфига
    with pytest.raises(ForbiddenModelError, match="п. 10.6"):
        LLMClient(bad, env={"DEEPSEEK_API_KEY": "k"})


def test_competition_rejects_foreign_model_at_call_time() -> None:
    client = LLMClient(LLMConfig(mode="competition"), env={"DEEPSEEK_API_KEY": "k"}, http_client=httpx.Client(transport=httpx.MockTransport(lambda r: reply(GOOD))))
    client._cfg.model = "claude-opus"  # подмена после создания
    with pytest.raises(ForbiddenModelError):
        client.call("a", "p", {}, Out)


def test_competition_allows_deepseek_flash() -> None:
    seen: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return reply(GOOD)

    res = client_with(handler, mode="competition").call("a", "p", {}, Out)
    assert res.parsed is not None and seen[0]["model"] == "deepseek-v4-flash"


def test_dev_mode_allows_other_model_names() -> None:
    res = client_with(lambda r: reply(GOOD), model="some-dev-model").call("a", "p", {}, Out)
    assert res.parsed is not None


# --------------------------------------------------------------------------- ключ


def test_remote_modes_require_key() -> None:
    with pytest.raises(LLMConfigError, match="DEEPSEEK_API_KEY"):
        LLMClient(cfg(), env={})
    with pytest.raises(LLMConfigError):
        LLMClient(cfg(mode="competition"), env={})


# --------------------------------------------------------------------------- удалённый вызов


def test_remote_success_request_shape(journal: Journal) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return reply(GOOD)

    res = client_with(handler, journal).call("monitor", "ПРОМПТ", {"x": [1, 2]}, Out)
    assert res.parsed == Out(answer="ок", score=3) and res.attempts == 1 and not res.used_fallback
    req = seen[0]
    body = json.loads(req.content)
    assert str(req.url) == "https://api.test/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-secret"
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system" and "ПРОМПТ" in body["messages"][0]["content"]
    assert '"score"' in body["messages"][0]["content"]  # схема ответа добавлена к промпту
    assert json.loads(body["messages"][1]["content"]) == {"x": [1, 2]}


def test_api_key_never_reaches_journal(journal: Journal) -> None:
    client_with(lambda r: reply(GOOD), journal).call("a", "p", {}, Out)
    assert "sk-secret" not in journal.path.read_text(encoding="utf-8")


def test_json_in_markdown_fence_is_accepted() -> None:
    res = client_with(lambda r: reply(f"Вот ответ:\n```json\n{GOOD}\n```")).call("a", "p", {}, Out)
    assert res.parsed == Out(answer="ок", score=3)


def test_timeout_then_success_uses_single_retry() -> None:
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=req)
        return reply(GOOD)

    res = client_with(handler).call("a", "p", {}, Out)
    assert res.parsed is not None and res.attempts == 2 and not res.used_fallback


def test_timeout_twice_falls_back(journal: Journal) -> None:
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=req)

    res = client_with(handler, journal).call("energy_planner", "p", {}, Out)
    assert res.used_fallback and res.parsed is None and len(calls) == 2  # ровно один повтор
    assert "таймаут" in (res.error or "")
    e = llm_entries(journal)[0]
    assert e.data["used_fallback"] is True and e.data["attempts"] == 2


def test_http_500_is_retried_then_falls_back() -> None:
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    res = client_with(handler).call("a", "p", {}, Out)
    assert res.used_fallback and len(calls) == 2 and "HTTP 500" in (res.error or "")


def test_network_error_falls_back() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=req)

    res = client_with(handler).call("a", "p", {}, Out)
    assert res.used_fallback and "сеть" in (res.error or "")


@pytest.mark.parametrize("body", [{"nope": 1}, {"choices": []}, {"choices": [{"message": {"content": None}}]}])
def test_unexpected_api_shape_falls_back(body: dict[str, object]) -> None:
    res = client_with(lambda r: httpx.Response(200, json=body)).call("a", "p", {}, Out)
    assert res.used_fallback


def test_non_json_http_body_falls_back() -> None:
    res = client_with(lambda r: httpx.Response(200, text="<html>")).call("a", "p", {}, Out)
    assert res.used_fallback


def test_bad_format_then_fixed_json() -> None:
    bodies: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return reply("не json" if len(bodies) == 1 else GOOD)

    res = client_with(handler).call("a", "p", {}, Out)
    assert res.parsed is not None and res.attempts == 2
    retry_msgs = bodies[1]["messages"]
    assert retry_msgs[2] == {"role": "assistant", "content": "не json"}  # type: ignore[index]
    assert "проверку формата" in retry_msgs[3]["content"]  # type: ignore[index]


def test_bad_format_twice_falls_back(journal: Journal) -> None:
    res = client_with(lambda r: reply('{"answer": "x"}'), journal).call("a", "p", {}, Out)  # нет score
    assert res.used_fallback and res.attempts == 2 and "формата" in (res.error or "")
    assert res.raw == '{"answer": "x"}'
    assert llm_entries(journal)[0].data["parsed_decision"] is None


def test_schema_type_violation_is_bad_format() -> None:
    res = client_with(lambda r: reply('{"answer": "x", "score": "много"}')).call("a", "p", {}, Out)
    assert res.used_fallback


# --------------------------------------------------------------------------- промпты


def test_prompt_hash_stable_and_distinct() -> None:
    assert prompt_hash("a") == prompt_hash("a") != prompt_hash("b") and len(prompt_hash("a")) == 16


def test_load_prompt_missing() -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt("no_such_prompt")
