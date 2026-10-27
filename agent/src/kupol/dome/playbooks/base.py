"""Основа плейбуков: намерение (``Intent``), контекст (``Ctx``) и базовый класс.

Плейбук — чистая функция состояния: на каждой телеметрии он возвращает список намерений. Намерение
описывает решение (что видим, почему, на каком основании, что делаем) и шаги-команды; отправку и
запись в лог выполняет рантайм через ``Gate``. Многошаговые сценарии (пометить датчик → снять
интерлок → продолжить станок) плейбук ведёт по состоянию: следующий шаг делается, когда телеметрия
подтвердила предыдущий.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kupol.dome.checks import Ev
from kupol.dome.config import DomeConfig
from kupol.dome.gate import Expect, Gate
from kupol.dome.state import DomeState


@dataclass
class Step:
    """Одна команда в составе намерения."""

    node: str
    action: str
    value: Any = None
    expect: Expect | None = None
    reason: str = ""


@dataclass
class Intent:
    """Решение агента и его команды."""

    key: str  # ключ дедупликации (одинаковое решение не повторяется чаще кулдауна)
    situation: str
    cause: str
    confidence: float
    evidence: list[dict[str, Any]] = field(default_factory=list)
    plan: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    say: str | None = None  # короткое сообщение Оператору (если без него не обойтись)
    priority: int = 5  # 1 — самое срочное
    rag_query: str = ""
    cooldown_s: float | None = None
    llm_question: str | None = None  # свободный вопрос Оператора: ответ попробует дать модель (say — запасной текст)
    impact_kind: str = "unknown"
    authenticity: str = "unknown"
    causal_chain: list[str] = field(default_factory=list)
    classification_issues: list[str] = field(default_factory=list)  # противоречия в разметке модели (llm_agent.validate)
    source: str = "rules"  # rules | llm | rag | mixed (поле source записи decision)
    rag_refs: list[str] = field(default_factory=list)  # источники RAG, на которые опирается решение модели
    say_always: bool = False  # ответ на требование Оператора: отправить и при отказе части шагов (с пояснением)
    refused: list[str] = field(default_factory=list)  # действия, отклонённые до Gate (LLM-слой, сервер БД), с причинами
    human_escalation: dict[str, Any] | None = None
    deferred: list[str] = field(default_factory=list)  # принятые шаги модели, отложенные до нового решения по телеметрии

    @classmethod
    def from_evidence(cls, evidence: list[Ev], **kw: Any) -> Intent:
        """Намерение с доказательствами из сверок."""
        return cls(evidence=[e.as_log() for e in evidence], **kw)


@dataclass
class Ctx:
    """Всё, что плейбук может читать."""

    state: DomeState
    cfg: DomeConfig
    gate: Gate
    mem: dict[str, Any]  # память этого плейбука (сохраняется между запусками)
    now: float  # Unix-время
    operator_texts: Callable[[float], list[str]] = field(default=lambda since: [])  # сообщения Оператора за период (с)
    chat_new: list[dict[str, Any]] = field(default_factory=list)  # новые сообщения чата (meeting_message.message)
    handled_message_ids: set[int] = field(default_factory=set)
    model_answers: bool = False  # модель работает параллельно и сама отвечает на вопросы Оператора


class Playbook:
    """Базовый класс плейбука."""

    name = "playbook"

    def step(self, ctx: Ctx) -> list[Intent]:
        """Вернуть намерения на текущей телеметрии (пустой список — ничего делать не нужно)."""
        raise NotImplementedError


def trust_step(node: str, param: str, trust: str) -> Step:
    """Шаг ``set_data_trust`` с проверкой в ``data_trust`` (ключи содержат точки — проверяем через checks)."""
    from kupol.dome.checks import is_untrusted

    want_untrusted = trust != "TRUSTED"
    return Step(
        "dome_automation_01",
        "set_data_trust",
        {"node": node, "param": param, "trust": trust},
        Expect.custom(
            f"data_trust[{node}.{param}] = {trust}",
            lambda st: is_untrusted(st, node, param) == want_untrusted,
            ["dome_automation_01.data_trust"],
        ),
        reason=f"{node}.{param} → {trust}",
    )


def interlock_state(state: DomeState, il: str) -> str:
    """Состояние интерлока (``ARMED`` / ``TRIPPED`` / ``BLOCKED`` / пусто)."""
    return str(state.get(f"dome_automation_01.interlocks.{il}.state", ""))


def interlock_pending_s(state: DomeState, il: str) -> float:
    """Сколько секунд идёт отсчёт до срабатывания интерлока (условие уже выполняется)."""
    return float(state.get(f"dome_automation_01.interlocks.{il}.pending_s", 0) or 0)
