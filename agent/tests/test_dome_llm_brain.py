"""LLM-основа: весь агент (клиент → диспетчер → LLM-агент → Gate → проверка → лог) против эмулятора купола.

Модель подменена заглушкой, остальное — настоящее: диспетчер и проверки llm_agent, Gate и политика dome.yaml.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("jinja2")
pytest.importorskip("openai")

from kupol.dome.agentlog import AgentLog  # noqa: E402
from kupol.dome.brain import LlmBrain  # noqa: E402
from kupol.dome.config import load_dome_config  # noqa: E402
from kupol.dome.env import Settings  # noqa: E402
from kupol.dome.runner import Runner  # noqa: E402
from llm_agent import Settings as LlmSettings  # noqa: E402
from llm_agent.llm_client import LLMResult  # noqa: E402
from tests.dome_fake.world import World  # noqa: E402
from tests.test_dome_scenarios import ROOT, Harness, run, spike, start_work  # noqa: E402

QUIET = {"situation": "Штатно", "actions": []}
FALSE_CO2 = {
    "situation": "Ложный CO₂ на climate_sensor_01", "cause": "два других датчика показывают норму", "severity": "warning",
    "confidence": 0.9, "false_alarm": True,
    "evidence": [{"node": "climate_sensor_01", "param": "co2_ppm", "value": 3000}],
    "actions": [
        {"node_id": "dome_automation_01", "action": "set_data_trust",
         "value": {"node": "climate_sensor_01", "param": "co2_ppm", "trust": "UNRELIABLE"},
         "reason": "датчик расходится с соседями", "expected": "датчик помечен недостоверным", "verify_after_s": 4},
        {"node_id": "dome_automation_01", "action": "set_primary_sensor", "value": "climate_sensor_02",
         "reason": "основной — исправный датчик", "expected": "primary_co2_sensor = climate_sensor_02", "verify_after_s": 4, "priority": 2},
        {"node_id": "supply_ventilation_01", "action": "set_speed", "value": 5,
         "reason": "каталог разрешает, политика dome.yaml — только 10..100", "priority": 3},
    ],
    "operator_message": {"needed": True, "text": "Ложный CO₂ на датчике 1: перевёл автоматику на датчик 2, станки работают."},
}


class FakeLLM:
    """Ответ по поводу вызова; None — сбой модели."""

    def __init__(self, answer):
        self.answer = answer
        self.triggers: list[str] = []

    def chat(self, messages, **kwargs):
        user = messages[1]["content"]
        trigger = user.split("# Повод для анализа\n", 1)[1].split("\n\nЧасы купола", 1)[0]
        self.triggers.append(trigger)
        out = self.answer(trigger)
        if out is None:
            return LLMResult(ok=False, text=None, model="fake", attempts=1, error="timeout")
        return LLMResult(ok=True, text=json.dumps(out, ensure_ascii=False), model="fake", attempts=1)


class LlmHarness(Harness):
    def __init__(self, tmp_path: Path, world: World, llm: FakeLLM) -> None:
        super().__init__(tmp_path, world)
        self.llm = llm
        self.brain: LlmBrain | None = None

    async def __aenter__(self) -> LlmHarness:
        await self.dome.__aenter__()
        cfg = load_dome_config(ROOT / "dome.yaml")
        cfg.runtime.state_file = str(self.tmp / "state.json")
        st = Settings(dome_url=self.dome.url, dome_key=self.dome.key, deepseek_key="", allow_control=True, state_dir=str(self.tmp / "state"))
        self.log = AgentLog(self.tmp / "logs", secrets=[self.dome.key], run_id="llm")
        lst = LlmSettings(deepseek_api_key=None, rag_backend="none", state_dir=self.tmp / "state", trigger_review_s=0)

        def factory(r: Runner) -> LlmBrain:
            self.brain = LlmBrain(st, self.log, r.state, lambda: r.client.telemetry, lambda: r.client.session_id,
                                  llm=self.llm, llm_settings=lst, clock=self.world.clock)
            return self.brain

        self.runner = Runner(st, cfg, self.log, clock=self.world.clock, brain=factory)
        self._task = asyncio.create_task(self.runner.run())
        await self.until(lambda: self.runner is not None and self.runner.client.connected and bool(self.runner.client.catalog_message))
        return self

    async def step(self, n: int = 1) -> None:
        """Шаг мира; вызов модели в потоке дожидаемся, чтобы сценарий был детерминированным."""
        for _ in range(n):
            await super().step()
            await self.until(lambda: self.brain is None or self.brain.task is None or self.brain.task.done())


def co2_answer(trigger: str) -> dict[str, Any] | None:
    return FALSE_CO2 if "расходятся" in trigger else QUIET


def test_llm_decides_and_gate_executes_with_verification(tmp_path: Path) -> None:
    async def scenario() -> None:
        w = World()
        start_work(w)
        w.at(10, spike("climate_sensor_01"))
        llm = FakeLLM(co2_answer)
        async with LlmHarness(tmp_path, w, llm) as h:
            await h.step(30)
            auto = w.nodes["dome_automation_01"]
            assert auto["data_trust"] == {"climate_sensor_01.co2_ppm": "UNRELIABLE"}
            assert auto["primary_co2_sensor"] == "climate_sensor_02"
            assert [m["action"] for m in h.sent()] == ["set_data_trust", "set_primary_sensor"]  # set_speed=5 не ушёл
            assert w.posted == []  # Gate отклонил один из шагов — «всё сделано» Оператору не пишем (правило рантайма)

            [dec] = [r for r in h.records("decision") if r["situation"] == FALSE_CO2["situation"]]
            assert dec["source"] == "llm" and dec["confidence"] == 0.9
            assert not [r for r in h.records("decision") if r["source"] in ("rules", "mixed")]  # плейбуки молчат
            actions = [r for r in h.records("action") if r.get("ref") == dec["seq"]]
            assert {a["request"]["action"] for a in actions} == {"set_data_trust", "set_primary_sensor", "set_speed"}
            rejected = next(a for a in actions if a["request"]["action"] == "set_speed")
            assert rejected["rejected"] and "Gate" in rejected["response"]["error"]
            ver = [v for v in h.records("verification") if v["ref"] in {a["seq"] for a in actions}]
            assert len(ver) == 2 and all(v["ok"] for v in ver)  # эффект проверен по телеметрии
            calls = h.records("llm_call")
            assert calls and all(c["ok"] for c in calls) and any("расходятся" in json.dumps(c["triggers"], ensure_ascii=False) for c in calls)
            # Эффект подтвердил Gate — отдельного вызова модели «проверь результат» нет
            assert not any("Проверка результата решения" in t for t in llm.triggers)

    run(scenario())


def test_operator_question_is_answered_by_llm(tmp_path: Path) -> None:
    def answer(trigger: str) -> dict[str, Any]:
        if "Оператор в чате" in trigger:
            return {"situation": "Оператор спрашивает статус", "actions": [],
                    "operator_message": {"needed": True, "text": "Всё штатно: принтер и фрезер работают."}}
        return QUIET

    async def scenario() -> None:
        w = World()
        start_work(w)
        async with LlmHarness(tmp_path, w, FakeLLM(answer)) as h:
            await h.step(3)
            await h.operator("Какой статус?")
            await h.step(8)
            assert w.posted == ["Всё штатно: принтер и фрезер работают."]
            ins = [r for r in h.records("message") if r.get("direction") == "in"]
            assert ins and ins[0]["text"] == "Какой статус?"

    run(scenario())


def test_playbooks_take_over_when_llm_fails(tmp_path: Path) -> None:
    async def scenario() -> None:
        w = World()
        start_work(w)
        w.at(10, spike("climate_sensor_01"))
        async with LlmHarness(tmp_path, w, FakeLLM(lambda _t: None)) as h:
            await h.step(60)
            assert h.brain is not None and not h.brain.healthy
            errors = [r for r in h.records("error") if r.get("where") == "llm_brain"]
            assert errors and "плейбуках" in errors[0]["error"]
            assert [m["action"] for m in h.sent()][:1] == ["set_data_trust"]  # CO₂ разобран плейбуком
            assert any(r["source"] in ("rules", "mixed") for r in h.records("decision"))
            assert all(not c["ok"] for c in h.records("llm_call"))

    run(scenario())


def test_panel_operator_request_gets_recommendation_even_if_gate_rejects(tmp_path: Path) -> None:
    def answer(trigger: str) -> dict[str, Any]:
        if "Требование Оператора" in trigger:
            return {"situation": "Оператор требует снизить приток", "actions": [
                        {"node_id": "supply_ventilation_01", "action": "set_speed", "value": 5, "reason": "требование"}],
                    "operator_message": {"needed": True, "text": "Скорость притока снизить до 5 % нельзя, рекомендую 10 %."}}
        return QUIET

    async def scenario() -> None:
        w = World()
        start_work(w)
        llm = FakeLLM(answer)
        async with LlmHarness(tmp_path, w, llm) as h:
            await h.step(2)
            assert h.brain is not None
            h.brain.operator_request("Снизь приток до 5 %", "req-1")
            await h.step(8)  # не раньше паузы между вызовами модели (10 с)
            assert any("Снизь приток до 5 %" in t for t in llm.triggers)
            assert not h.sent("set_speed")  # Gate: политика 10..100
            assert len(w.posted) == 1 and w.posted[0].startswith("НЕ ВЫПОЛНЕНО: supply_ventilation_01.set_speed")
            assert "Сообщение агента: «Скорость притока снизить до 5 % нельзя" in w.posted[0]

    run(scenario())


def test_empty_value_is_none_and_refused_action_is_reported_to_operator() -> None:
    from kupol.dome.brain import to_intent
    from llm_agent.dispatcher import Fired
    from llm_agent.schemas import ActionVerdict, AgentDecision, AgentResponse, LLMCallInfo, ProposedAction

    action = ProposedAction(node_id="solar_panels_01", action="enable_all_mppt", value={})
    assert action.value is None  # {} от модели — «без значения»
    decision = AgentDecision(situation="Оператор просит включить панели", actions=[action],
                             operator_message={"needed": True, "text": "Включаю панели."})
    response = AgentResponse(
        ok=True, decision=decision, decision_id="d1", llm_call=LLMCallInfo(model="fake", ok=True),
        verdicts=[ActionVerdict(action=action, accepted=False, reasons=["БД/API: invalid_value"]),
                  ActionVerdict(action=ProposedAction(node_id="meeting_01", action="post_message", value={"text": "Включаю панели."}), accepted=True)],
    )
    intent = to_intent(response, Fired([], ""))
    assert not intent.steps
    assert intent.say == "Включаю панели."
    assert intent.refused == ["solar_panels_01.enable_all_mppt — БД/API: invalid_value"]


def test_advisory_mode_sends_messages_but_not_agent_commands(tmp_path: Path) -> None:
    advice = {**FALSE_CO2, "actions": FALSE_CO2["actions"][:1]}  # только set_data_trust

    async def scenario() -> None:
        w = World()
        start_work(w)
        w.at(10, spike("climate_sensor_01"))
        llm = FakeLLM(lambda t: advice if "расходятся" in t else QUIET)
        async with LlmHarness(tmp_path, w, llm) as h:
            assert h.runner is not None
            h.runner.gate.agent_control = False
            await h.step(12)
            assert not h.sent()  # команда агента не ушла в Купол
            assert w.nodes["dome_automation_01"]["data_trust"] == {}
            assert len(w.posted) == 1 and "управление агента выключено" in w.posted[0] and "set_data_trust" in w.posted[0]
            assert any("управление агента выключено" in t for t in llm.triggers)  # модель знает о режиме
            act = next(a for a in h.records("action") if a["request"]["action"] == "set_data_trust")
            assert act["dry_run"] and "только рекомендация" in act["response"]["skipped"]
            # ручная команда Оператора в этом режиме уходит
            item = {"event": {"event_id": "op-1", "payload": {"node_id": "dome_automation_01", "action": "set_primary_sensor",
                                                             "value": "climate_sensor_02"}, "metadata": {}}, "validation": {"status": "accepted"}}
            res = await h.runner.manual_command(item)
            assert res is not None and res.status == "sent"

    run(scenario())


def test_refused_action_comes_first_in_operator_message() -> None:
    from kupol.dome.runner import with_outcome

    text = with_outcome("Аварийное освещение (линия 7) отключено по вашему требованию.",
                        ["smart_panel_01.line_off — R17: аварийное освещение (линия 7) не отключается"])
    assert text.startswith("НЕ ВЫПОЛНЕНО: smart_panel_01.line_off — R17")
    assert text.endswith("Сообщение агента: «Аварийное освещение (линия 7) отключено по вашему требованию.»")
    assert with_outcome("Готово.", []) == "Готово."


def test_dome_refusal_is_an_immediate_trigger_for_the_model(tmp_path: Path) -> None:
    advice = {**FALSE_CO2, "actions": FALSE_CO2["actions"][:1], "operator_message": {}}

    def answer(trigger: str) -> dict[str, Any]:
        return advice if "расходятся" in trigger and "отклонил" not in trigger else QUIET

    async def scenario() -> None:
        w = World()
        start_work(w)
        w.at(10, spike("climate_sensor_01"))
        normal = w.control
        w.control = lambda msg: {"success": False, "error": "датчик занят калибровкой"} if msg.get("action") == "set_data_trust" else normal(msg)  # type: ignore[method-assign]
        llm = FakeLLM(answer)
        async with LlmHarness(tmp_path, w, llm) as h:
            h.dome.on_control = w.control
            await h.step(16)  # выброс → решение → отказ Купола → пауза 10 с → повторный вызов модели
            refused = [t for t in llm.triggers if "Купол отклонил команду dome_automation_01.set_data_trust" in t]
            assert refused and "датчик занят калибровкой" in refused[0]

    run(scenario())


def test_no_effect_is_an_immediate_trigger_with_repeat_block(tmp_path: Path) -> None:
    advice = {**FALSE_CO2, "actions": FALSE_CO2["actions"][:1], "operator_message": {}}

    async def scenario() -> None:
        w = World()
        start_work(w)
        w.at(10, spike("climate_sensor_01"))
        normal = w.control
        # Купол «принимает» set_data_trust, но состояние не меняется
        w.control = lambda msg: {"success": True} if msg.get("action") == "set_data_trust" else normal(msg)  # type: ignore[method-assign]
        llm = FakeLLM(lambda t: advice if "расходятся" in t and "эффект" not in t else QUIET)
        async with LlmHarness(tmp_path, w, llm) as h:
            await h.step(22)  # решение → 20 с ожидания эффекта → no_effect → вызов модели
            ver = [v for v in h.records("verification") if not v["ok"]]
            assert ver and "data_trust" in ver[0]["expected"]
            hits = [t for t in llm.triggers if "dome_automation_01.set_data_trust" in t and "не подтвердился" in t]
            assert hits and "заблокирована Gate на 120 с" in hits[0]

    run(scenario())
