"""Плейбуки и модель работают параллельно: плейбук действует первым, модель видит всё и советует.

Модель — заглушка, остальное настоящее: Runner, плейбуки, диспетчер, Gate, эмулятор купола.
"""

from __future__ import annotations

import pytest

pytest.importorskip("jinja2")
pytest.importorskip("openai")

from pathlib import Path  # noqa: E402

from kupol.dome.brain import ALGORITHMS_HEADER  # noqa: E402
from kupol.dome.control import ADVISORY_REASON  # noqa: E402
from llm_agent.prompt_builder import ALGORITHMS_MARK  # noqa: E402
from tests.dome_fake.world import World  # noqa: E402
from tests.test_dome_llm_brain import FALSE_CO2, QUIET, FakeLLM, LlmHarness  # noqa: E402
from tests.test_dome_scenarios import run, spike, start_work  # noqa: E402


def sensor_trust_answer(trigger: str):
    """Модель предлагает то же, что и плейбук: пометить датчик ложным."""
    if "расходятся" not in trigger:
        return QUIET
    return {**FALSE_CO2, "actions": FALSE_CO2["actions"][:1],
            "operator_message": {"needed": False, "text": ""}}


def test_model_sees_nodes_the_playbook_leads_and_its_step_on_them_is_deferred(tmp_path: Path) -> None:
    async def scenario() -> None:
        w = World()
        start_work(w)
        w.at(10, spike("climate_sensor_01"))
        llm = FakeLLM(sensor_trust_answer)
        async with LlmHarness(tmp_path, w, llm, playbooks=None) as h:
            await h.step(30)
            # Плейбук co2 помечает датчик сам: команда ушла один раз, без дубля от модели
            assert [m["action"] for m in h.sent()].count("set_data_trust") == 1
            # Повод по тому же узлу не отброшен: модель вызвана, и в запросе есть раздел «Алгоритмы» с их работой
            assert any("расходятся" in t for t in llm.triggers)
            with_block = [t for t in llm.triggers if ALGORITHMS_MARK in t]
            assert with_block and any("co2" in t for t in with_block)
            # Решение модели записано, её шаг по узлу, занятому алгоритмом, отложен и не отправлен
            decisions = [r for r in h.records("decision") if r.get("source") == "llm"]
            assert decisions and any("занят алгоритмом" in " ".join(r.get("plan", [])) for r in decisions)

    run(scenario())


def test_model_acts_on_nodes_no_playbook_leads(tmp_path: Path) -> None:
    def answer(trigger: str):
        if "Требование Оператора" in trigger:
            return {"situation": "Оператор просит снизить приток", "actions": [
                {"node_id": "supply_ventilation_01", "action": "set_speed", "value": 40, "reason": "требование"}],
                "operator_message": {"needed": True, "text": "Снижаю приток до 40 %."}}
        return QUIET

    async def scenario() -> None:
        w = World()
        start_work(w)
        llm = FakeLLM(answer)
        async with LlmHarness(tmp_path, w, llm, playbooks=None) as h:
            await h.step(2)
            assert h.brain is not None
            h.brain.operator_request("Снизь приток до 40 %", "req-1")
            await h.step(10)
            assert [m for m in h.sent("set_speed")]  # узел свободен от плейбуков: модель действует сама

    run(scenario())


def test_recommendation_mode_keeps_playbooks_and_model_advises(tmp_path: Path) -> None:
    async def scenario() -> None:
        w = World()
        start_work(w)
        w.at(10, spike("climate_sensor_01"))
        llm = FakeLLM(lambda t: {**FALSE_CO2, "actions": FALSE_CO2["actions"][1:2]} if "расходятся" in t else QUIET)
        async with LlmHarness(tmp_path, w, llm, playbooks=None) as h:
            assert h.runner is not None
            h.runner.set_mode("recommendation", True)
            await h.step(30)
            assert h.sent("set_data_trust")  # алгоритмы включены: плейбук работает
            assert any(ADVISORY_REASON in m or "set_primary_sensor" in m for m in w.posted)  # совет модели ушёл Оператору

    run(scenario())


def test_algorithms_off_leaves_only_the_model(tmp_path: Path) -> None:
    async def scenario() -> None:
        w = World()
        start_work(w)
        w.at(10, spike("climate_sensor_01"))
        llm = FakeLLM(sensor_trust_answer)
        async with LlmHarness(tmp_path, w, llm, playbooks=None) as h:
            assert h.runner is not None
            h.runner.set_mode("autonomous", False)
            await h.step(30)
            assert h.sent("set_data_trust")  # то же действие, но теперь его выполнила модель
            assert not [r for r in h.records("decision") if r.get("source") == "rules"
                        and r["situation"] != "Вызов человека"]

    run(scenario())


def test_algorithms_header_marks_the_section() -> None:
    assert ALGORITHMS_HEADER.strip().startswith(ALGORITHMS_MARK)
