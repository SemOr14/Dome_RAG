"""Системные прогоны по логам 12–18.10.2026: несколько эпизодов подряд через весь рантайм.

Runner, мозг на LLM, Gate и фейковый Купол по WebSocket — настоящие; кадры телеметрии берутся из
rag/data/sources/Logs окнами вокруг эпизодов, между окнами время прыгает вперёд. Немного синтетики
вставлено в настоящие кадры (отмечено). Модель — заглушка: она читает карточки RAG в своём промпте и
повторяет разметку верхней, поэтому проверяется путь «карточка → промпт → решение → журнал».

Для каждого прогона ожидание записано явно: какие эпизоды доходят до модели со своей карточкой, а какие
пока нет (известные пробелы). Улучшение или регрессия меняют это множество, и тест падает.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kupol.dome.agentlog import AgentLog
from kupol.dome.brain import LlmBrain
from kupol.dome.config import load_dome_config
from kupol.dome.env import Settings
from kupol.dome.runner import Runner
from llm_agent import Settings as LlmSettings
from llm_agent.llm_client import LLMResult
from tests.dome_fake.world import World
from tests.test_dome_scenarios import ROOT, Harness, run

REPO = ROOT.parent
LOGS = REPO / "rag/data/sources/Logs"
_days: dict[str, list[str]] = {}

Mutate = Callable[[dict[str, Any], int], None]


def window(day: str, start: str, end: str, label: str, mutate: Mutate | None = None) -> list[tuple[str, dict[str, Any], float]]:
    """Кадры дня с HH:MM по HH:MM включительно; mutate(nodes, k) — синтетика поверх настоящего кадра."""
    lines = _days.setdefault(day, (LOGS / f"2026-10-{day}/telemetry.jsonl").read_text(encoding="utf-8").splitlines())
    first, last = (int(t[:2]) * 60 + int(t[3:]) for t in (start, end))
    out = []
    for k, minute in enumerate(range(first, last + 1)):
        nodes = json.loads(lines[minute])["nodes"]
        epoch = max(b["updated_at"] for b in nodes.values() if isinstance(b, dict) and isinstance(b.get("updated_at"), (int, float)))
        if mutate is not None:
            mutate(nodes, k)
        out.append((label, nodes, float(epoch)))
    return out


class ReplayWorld(World):
    """Купол, который отдаёт записанные кадры; чат и ответы на команды — от эмулятора."""

    def __init__(self, frames: list[tuple[str, dict[str, Any], float]]) -> None:
        super().__init__()
        self.frames = frames
        self.i = -1
        self.t = frames[0][2] - 60.0
        self.labels: list[str] = []

    @property
    def label(self) -> str:
        return self.frames[max(self.i, 0)][0]

    def telemetry(self) -> dict[str, Any]:
        self.i = min(self.i + 1, len(self.frames) - 1)
        label, nodes, epoch = self.frames[self.i]
        self.t = max(self.t + 60.0, epoch)
        self.nodes = copy.deepcopy(nodes)
        # Чат исторического дня не переносим: сообщения Оператора в прогоне — только те, что подаёт тест
        self.nodes["meeting_01"] = {"online": True, "message_count": len(self.chat), "recent_messages": self.chat[-10:],
                                    "updated_at": self.t, "status": "OK"}
        return {"nodes": self.nodes, "game_time": {"day": 3, "hour": 12.0}}


class FollowRag:
    """Заглушка модели: разметка — как у верхней карточки RAG в промпте; ничего не делает, Оператору отвечает."""

    LABEL = re.compile(r"Разметка: impact_kind=(\w+), authenticity=(\w+)(?:; цепочка: (.*))?")

    def __init__(self, world: ReplayWorld) -> None:
        self.world = world
        self.calls: list[tuple[str, str, list[str]]] = []  # (окно, повод, карточки RAG)

    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResult:
        user = messages[1]["content"]
        trigger = user.split("# Повод для анализа\n", 1)[1].split("\n\nЧасы купола", 1)[0]
        ids = re.findall(r"^## \[(S\d\d|F\d\d)\]", user, re.M)
        self.calls.append((self.world.label, trigger, ids))
        found = self.LABEL.search(user)
        kind, authenticity, chain = found.groups() if found else ("unknown", "unknown", None)
        answer = {"situation": f"Похоже на {ids[0] if ids else 'неизвестное'}", "cause": "по базе знаний",
                  "impact_kind": kind, "authenticity": authenticity, "causal_chain": (chain or "").split(" → ") if chain else [],
                  "evidence": [{"node": "dome_automation_01", "note": trigger[:80]}], "actions": [],
                  "rag_refs": ids[:1]}
        if "Оператор в чате" in trigger or "Требование Оператора" in trigger:
            answer["operator_message"] = {"needed": True, "text": "Вижу, разбираюсь.", "request_status": "answered"}
        return LLMResult(ok=True, text=json.dumps(answer, ensure_ascii=False), model="fake", attempts=1)


class DayHarness(Harness):
    def __init__(self, tmp_path: Path, world: ReplayWorld) -> None:
        super().__init__(tmp_path, world)
        self.llm = FollowRag(world)
        self.brain: LlmBrain | None = None

    async def __aenter__(self) -> DayHarness:
        await self.dome.__aenter__()
        cfg = load_dome_config(ROOT / "dome.yaml")
        cfg.runtime.state_file = str(self.tmp / "state.json")
        st = Settings(dome_url=self.dome.url, dome_key=self.dome.key, deepseek_key="", allow_control=True, state_dir=str(self.tmp / "state"))
        self.log = AgentLog(self.tmp / "logs", secrets=[self.dome.key], run_id="logs")
        lst = LlmSettings(deepseek_api_key=None, rag_backend="dome", rag_dir=REPO / "rag", rag_semantic=False, rag_k=5,
                          state_dir=self.tmp / "state", trigger_review_s=0)

        def factory(r: Runner) -> LlmBrain:
            self.brain = LlmBrain(st, self.log, r.state, lambda: r.client.telemetry, lambda: r.client.session_id,
                                  llm=self.llm, llm_settings=lst, clock=self.world.clock)
            return self.brain

        self.runner = Runner(st, cfg, self.log, clock=self.world.clock, brain=factory,
                             playbooks=[])  # проверяем путь модели и RAG отдельно от плейбуков
        self._task = asyncio.create_task(self.runner.run())
        await self.until(lambda: self.runner is not None and self.runner.client.connected and bool(self.runner.client.catalog_message))
        return self

    async def play(self, operator: dict[str, str] | None = None) -> None:
        """Все кадры по очереди; operator — сообщение Оператора перед первым кадром окна с этой меткой."""
        assert isinstance(self.world, ReplayWorld)
        said: set[str] = set()
        for label, _, _ in self.world.frames:
            if operator and label in operator and label not in said:
                said.add(label)
                await self.operator(operator[label])
            await self.step()
            await self.until(lambda: self.brain is None or self.brain.task is None or self.brain.task.done(), timeout=30)

    def reached(self) -> dict[str, set[str]]:
        """Окно → карточки RAG, которые были в промптах модели во время этого окна."""
        out: dict[str, set[str]] = {}
        for label, _, ids in self.llm.calls:
            out.setdefault(label, set()).update(ids)
        return out


def outcome(h: DayHarness, expected: dict[str, str]) -> tuple[set[str], set[str]]:
    """(окна, где карточка дошла до модели; окна, где нет) по ожиданию окно → id карточки."""
    reached = h.reached()
    hit = {label for label, sid in expected.items() if sid in reached.get(label, set())}
    return hit, set(expected) - hit


def check_run(h: DayHarness, operator: dict[str, str]) -> None:
    """Общее для всех прогонов: заглушка не предлагает команд, на каждое сообщение Оператора есть ответ,
    разметка из карточек RAG проходит проверку согласованности без замечаний."""
    assert h.sent() == []
    chat = [m for m in h.world.posted if not m.startswith(("[RED]", "[YELLOW]"))]  # без вызовов человека по инцидентам
    assert chat == ["Вижу, разбираюсь."] * len(operator)
    decisions = [r for r in h.records("decision") if r.get("source") in ("llm", "mixed")]
    assert decisions and all(r["impact_kind"] in ("base", "hidden", "cascade", "false", "unknown") for r in decisions)
    assert not [r for r in decisions if r.get("classification_issues")]


def spike_co2(nodes: dict[str, Any], k: int) -> None:  # синтетика: ложный CO₂ на одном датчике спокойного дня
    nodes["climate_sensor_01"]["co2_ppm"] = 3050 + 10 * k


def growing_leak(nodes: dict[str, Any], k: int) -> None:  # синтетика: утечка на линии фрезера растёт к порогу УЗО
    nodes["smart_panel_01"]["lines"][3]["leakage_ma"] = round(2 + 4.5 * k, 2)


def test_monday_tuesday_lock_bus_degraded_sensor_access_alarm_and_fan_motor(tmp_path: Path) -> None:
    frames = (window("12", "05:00", "05:06", "S52") + window("12", "11:26", "11:31", "S25a")
              + window("12", "12:22", "12:32", "S25b") + window("12", "14:30", "14:36", "synthetic false CO2", spike_co2)
              + window("13", "08:49", "08:55", "S30") + window("13", "19:58", "20:04", "S08a")
              + window("13", "20:50", "20:56", "S08b"))
    expected = {"S52": "S52", "S25a": "S25", "S25b": "S25", "synthetic false CO2": "S02", "S30": "S30", "S08a": "S08", "S08b": "S08"}
    operator = {"S25a": "Готово на принтере? Иду за деталью.", "S08b": "Душновато в мастерской, что с вентиляцией?"}

    async def scenario() -> None:
        async with DayHarness(tmp_path, ReplayWorld(frames)) as h:
            await h.play(operator)
            hit, miss = outcome(h, expected)
            # S08 доходит, только когда о духоте пишет Оператор: по одной телеметрии рост CO₂ при FAN_MOTOR не узнаётся
            assert hit == {"S52", "synthetic false CO2", "S30", "S08b"}
            assert miss == {"S25a", "S25b", "S08a"}  # пробелы: шина МС-ТЮК, начало аварии притока
            check_run(h, operator)

    run(scenario())


def test_wednesday_seven_episodes_of_one_day(tmp_path: Path) -> None:
    frames = (window("14", "04:28", "04:34", "S03") + window("14", "08:09", "08:16", "S04")
              + window("14", "10:51", "10:57", "S01") + window("14", "12:49", "12:54", "S02")
              + window("14", "16:31", "16:41", "S06") + window("14", "21:01", "21:08", "S05")
              + window("14", "22:58", "23:02", "S07"))
    expected = {label: label for label in ("S03", "S04", "S01", "S02", "S06", "S05", "S07")}
    operator = {"S01": "Он ещё печатает! Сопло ездит, на экране 34.3 %. Дверь не открываю.",
                "S02": "Станки встали оба — что случилось?"}

    async def scenario() -> None:
        async with DayHarness(tmp_path, ReplayWorld(frames)) as h:
            await h.play(operator)
            hit, miss = outcome(h, expected)
            assert hit == {"S03", "S02", "S06", "S05", "S07"}
            assert miss == {"S04", "S01"}  # пробелы: фантомная нагрузка линии 8, ложное завершение по webhook
            check_run(h, operator)

    run(scenario())


def test_end_of_week_radiation_seismic_access_and_synthetic_leak(tmp_path: Path) -> None:
    frames = (window("15", "16:55", "17:03", "S53a") + window("16", "09:20", "09:27", "synthetic leak", growing_leak)
              + window("18", "05:46", "05:51", "S30b") + window("18", "09:33", "09:38", "S53b"))
    expected = {"S53a": "S53", "synthetic leak": "S31", "S30b": "S30", "S53b": "S53"}
    operator = {"S30b": "Сработала тревога двери серверной — я рядом, дверь закрыта."}

    async def scenario() -> None:
        async with DayHarness(tmp_path, ReplayWorld(frames)) as h:
            await h.play(operator)
            hit, miss = outcome(h, expected)
            assert hit == {"S53a", "S30b", "S53b"}
            assert miss == {"synthetic leak"}  # пробел: рост leakage_ma не становится поводом раньше УЗО
            check_run(h, operator)

    run(scenario())
