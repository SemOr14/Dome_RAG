"""Общая основа агентов: ситуация, шаблон вызова модели с фолбэком.

Принцип: **код считает, LLM объясняет и ранжирует**. Решения, критичные для
безопасности, принимает код (метод ``react``, выполняется каждый такт без модели);
модель вызывается методом ``run`` для интерпретации, ранжирования гипотез и плана, а при
любом её сбое результат даёт резервный сценарий (``fallback``) — регламент п. 10.4.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from kupol.agents.llm import LLMClient, load_prompt
from kupol.core.config import AppConfig
from kupol.core.energy import EnergyStatus, PlanItem, SheddingAdvice
from kupol.core.journal import Journal
from kupol.core.response import ResponseDecision
from kupol.core.models import Alert, AlertLevel, JournalEntry, Probe, Recommendation, Telemetry, Verdict


@dataclass
class Situation:
    """Срез ситуации, который главный цикл отдаёт агентам.

    Attributes:
        ts: Время среза (виртуальное в симуляции).
        telemetry: Последний снимок.
        alerts: Активные тревоги (включая уже известные).
        new_alerts: Тревоги, появившиеся или усилившиеся в этом такте.
        verdicts: Текущие вердикты сверки источников.
        energy: Усреднённый энергостатус.
        plan: План операций (``EnergyPlanner.order``).
        advice: Кандидаты «что отложить» (``EnergyPlanner.shedding_advice``).
        journal_tail: Последние записи журнала решений.
        vent_ok: Вытяжка отвечает на команды (нет активной тревоги ``actuator_no_effect``).
        elapsed_s: Секунд с начала игры.
        responses: Решения об уровне реакции по полям (политика реакции, `core/response.py`).
        available_probes: Допустимые безопасные пробы (отфильтрованы кодом по риску, обратимости, бюджету).
        residual_w: Последняя невязка энергобаланса, Вт.
    """

    ts: datetime
    telemetry: Telemetry | None
    alerts: list[Alert] = field(default_factory=list)
    new_alerts: list[Alert] = field(default_factory=list)
    verdicts: list[Verdict] = field(default_factory=list)
    energy: EnergyStatus | None = None
    plan: list[PlanItem] = field(default_factory=list)
    advice: list[SheddingAdvice] = field(default_factory=list)
    journal_tail: list[JournalEntry] = field(default_factory=list)
    vent_ok: bool = True
    elapsed_s: float = 0.0
    responses: dict[str, ResponseDecision] = field(default_factory=dict)
    available_probes: list[Probe] = field(default_factory=list)
    residual_w: float | None = None

    @property
    def comms_ok(self) -> bool:
        """Есть ли достоверные данные (связь есть и снимок получен)."""
        return self.telemetry is not None and self.telemetry.comms_ok

    def significant_alerts(self) -> list[Alert]:
        """Тревоги уровня warning и выше."""
        return [a for a in self.alerts if a.level.rank >= AlertLevel.WARNING.rank]


@dataclass
class AgentResult:
    """Итог работы агента за один вызов.

    Attributes:
        recommendations: Рекомендации (передаются на доску).
        used_fallback: Применён ли резервный сценарий.
        error: Причина фолбэка.
    """

    recommendations: list[Recommendation]
    used_fallback: bool = False
    error: str | None = None


def alert_brief(a: Alert) -> dict[str, Any]:
    """Компактное представление тревоги для модели."""
    return {"level": a.level.value, "kind": a.kind, "field": a.field, "message": a.message, "value": a.value}


def verdict_brief(v: Verdict) -> dict[str, Any]:
    """Компактное представление вердикта сверки для модели."""
    return {
        "kind": v.kind,
        "confidence": v.confidence,
        "status": v.status,
        "rule": v.rule,
        "fields": v.fields,
        "explanation": v.explanation,
        "evidence": [{"source": e.source, "supports": e.supports, "weight": e.weight, "detail": e.detail} for e in v.evidence[:6]],
        "recheck_at": v.recheck_at.isoformat() if v.recheck_at else None,
    }


def journal_brief(e: JournalEntry) -> dict[str, Any]:
    """Компактное представление записи журнала для модели."""
    return {"ts": e.ts.isoformat(), "actor": e.actor, "event": e.event, "action": e.action, "result": e.result, "status": e.status}


def telemetry_brief(t: Telemetry | None) -> dict[str, Any]:
    """Ключевые значения телеметрии для модели."""
    if t is None:
        return {}
    return {
        "ts": t.ts.isoformat(),
        "comms_ok": t.comms_ok,
        "pv_power_w": round(t.pv_power_w),
        "load_total_w": round(t.load_total_w),
        "battery_soc_pct": round(t.battery_soc_pct, 1),
        "battery_voltage_v": round(t.battery_voltage_v, 1),
        "battery_temp_c": None if t.battery_temp_c is None else round(t.battery_temp_c, 1),
        "temp_c": round(t.temp_c, 1),
        "humidity_pct": round(t.humidity_pct),
        "co2_ppm": {k: round(v) for k, v in t.co2_ppm.items()},
        "vent_mode": t.vent_mode,
        "humidifier_mode": t.humidifier_mode,
        "power_mode": t.power_mode,
        "cnc": t.cnc_status.value,
        "printer": t.printer_status.value,
        "solder": t.solder_status.value,
    }


def level_priority(level: AlertLevel) -> int:
    """Приоритет рекомендации по уровню тревоги: critical → 1, warning → 2, info → 4."""
    return {AlertLevel.CRITICAL: 1, AlertLevel.WARNING: 2, AlertLevel.INFO: 4}[level]


class Agent(ABC):
    """Базовый агент: ``react`` (код, каждый такт) и ``run`` (модель с фолбэком, по событию)."""

    name: str = "agent"
    prompt_name: str = ""
    schema: type[BaseModel]

    def __init__(self, config: AppConfig, llm: LLMClient, journal: Journal) -> None:
        """Args:
        config: Конфигурация Штаба.
        llm: Клиент модели.
        journal: Журнал решений.
        """
        self.config = config
        self.llm = llm
        self.journal = journal
        self._last_fp: str | None = None
        self._last_run: datetime | None = None

    # ------------------------------------------------------------------ запуск по событию

    @abstractmethod
    def fingerprint(self, sit: Situation) -> str | None:
        """«Отпечаток» входа: изменился — пора звать модель; ``None`` — делать нечего."""

    def urgent(self, sit: Situation) -> bool:
        """Нужно ли игнорировать минимальный интервал между вызовами (новая критическая тревога)."""
        return any(a.level is AlertLevel.CRITICAL for a in sit.new_alerts)

    def try_claim(self, sit: Situation) -> bool:
        """Решить, нужно ли сейчас вызывать ``run``, и зафиксировать запуск.

        Args:
            sit: Текущая ситуация.

        Returns:
            ``True``, если вход изменился и прошёл минимальный интервал (или ситуация срочная).
        """
        fp = self.fingerprint(sit)
        if fp is None or fp == self._last_fp:
            return False
        waited = None if self._last_run is None else (sit.ts - self._last_run).total_seconds()
        interval = self.config.agents.intervals.get(self.name, self.config.agents.min_interval_s)
        if waited is not None and waited < interval and not self.urgent(sit):
            return False
        self._last_fp, self._last_run = fp, sit.ts
        return True

    # ------------------------------------------------------------------ детерминированная часть

    def react(self, sit: Situation) -> list[Recommendation]:
        """Правила, которые выполняет код каждый такт без участия модели (по умолчанию нет)."""
        return []

    # ------------------------------------------------------------------ часть с моделью

    def system_prompt(self) -> str:
        """Системный промпт агента (файл из `agents/prompts`); агент может дополнить его примерами."""
        return load_prompt(self.prompt_name)

    @abstractmethod
    def build_payload(self, sit: Situation) -> dict[str, Any]:
        """Входные данные для модели (JSON-совместимый словарь)."""

    @abstractmethod
    def summarize(self, sit: Situation) -> dict[str, Any]:
        """Краткая сводка входа для журнала."""

    @abstractmethod
    def mock(self, sit: Situation) -> BaseModel:
        """Детерминированный ответ-заглушка для режима ``mock``."""

    @abstractmethod
    def to_recommendations(self, out: Any, sit: Situation) -> list[Recommendation]:
        """Превратить проверенный ответ модели в рекомендации (с валидацией по данным кода)."""

    @abstractmethod
    def fallback(self, sit: Situation) -> list[Recommendation]:
        """Резервный сценарий: результат без модели (регламент п. 10.4)."""

    def run(self, sit: Situation) -> AgentResult:
        """Вызвать модель; при любом сбое применить резервный сценарий.

        Args:
            sit: Текущая ситуация.

        Returns:
            Результат с признаком фолбэка. Исключения модели наружу не выходят (кроме запрета
            чужой модели в боевом режиме — это нарушение, а не сбой).
        """
        res = self.llm.call(
            self.name,
            self.system_prompt(),
            self.build_payload(sit),
            self.schema,
            mock=lambda: self.mock(sit),
            inputs_summary=self.summarize(sit),
        )
        if res.used_fallback or res.parsed is None:
            recs = self.fallback(sit)
            for r in recs:
                r.used_fallback = True
            self.journal.log(
                actor=self.name,
                event="модель недоступна или ответила неверно",
                action="применён резервный сценарий",
                result=res.error or "",
                status="fallback",
                kind="agent",
                ts=sit.ts,
            )
            return AgentResult(recs, True, res.error)
        try:
            recs = self.to_recommendations(res.parsed, sit)
        except (ValueError, KeyError, IndexError) as exc:
            recs = self.fallback(sit)
            for r in recs:
                r.used_fallback = True
            self.journal.log(
                actor=self.name,
                event="ответ модели не согласуется с данными кода",
                action="применён резервный сценарий",
                result=str(exc),
                status="fallback",
                kind="agent",
                ts=sit.ts,
            )
            return AgentResult(recs, True, str(exc))
        return AgentResult(recs, False, None)
