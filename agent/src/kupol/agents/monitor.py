"""Агент «Монитор»: краткая интерпретация тревог для человека.

Вход: снимок, тревоги детекторов, вердикты сверки. Выход модели: интерпретация и
приоритет (только рекомендации). Резервный сценарий: показать сырые тревоги без
интерпретации.

Отдельно от модели работает код ``react`` (каждый такт, без LLM): жёсткие правила
безопасности и реакции на проверенные вердикты. Именно код, а не модель:

* пайка при выключенной вытяжке → команда ``vent_enable low`` (класс ``auto``,
  регламент п. 9.1); если вытяжка не отвечает — эскалация человеку, а не спам командами;
* CO2 высок на ВСЕХ датчиках (вердикт ``real``) → вытяжка ``low``; на максимум только
  с подтверждением человека; ложный сигнал одного датчика вытяжку не включает;
* потеря связи → решения по застывшим данным не принимаются, управление у Оператора.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from kupol.agents.base import (
    Agent,
    Situation,
    alert_brief,
    journal_brief,
    level_priority,
    telemetry_brief,
    verdict_brief,
)
from kupol.core.models import (
    Alert,
    AlertLevel,
    Command,
    DeviceStatus,
    Recommendation,
    RecommendationStatus,
    ResponseLevel,
)
from kupol.core.response import ResponsePolicy

VENT_RETRY_S = 30.0  # не чаще одной команды вытяжки за этот интервал
ESCALATION_S = 120.0  # повторять эскалацию человеку не чаще
REACT_COOLDOWN_S = 120.0
VENT_BACKOFF_S = 300.0  # после отказа вытяжки команды ей не повторяем (эскалация человеку вместо спама)


class MonitorOutput(BaseModel):
    """Строгая схема ответа модели-Монитора."""

    headline: str = Field(description="Одна строка: главное, что нужно знать сейчас")
    interpretation: str = Field(description="2–4 предложения для человека: что происходит и насколько это достоверно")
    priority: int = Field(ge=1, le=5, description="1 — срочно, 5 — справочно")
    operator_message: str = Field(default="", description="Что передать Оператору голосом (пусто, если не нужно)")
    watch: list[str] = Field(default_factory=list, description="На какие поля смотреть дальше")


class Monitor(Agent):
    """Монитор: интерпретация тревог (LLM) и правила безопасности (код)."""

    name = "monitor"
    prompt_name = "monitor"
    schema = MonitorOutput

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """См. ``Agent.__init__``."""
        super().__init__(*args, **kwargs)
        self._cooldown: dict[str, datetime] = {}
        self._vent_blocked_until: datetime | None = None
        self.policy = ResponsePolicy(self.config.response)

    def _ready(self, key: str, now: datetime, every_s: float) -> bool:
        """Можно ли снова выполнить правило ``key`` (и запомнить момент)."""
        last = self._cooldown.get(key)
        if last is not None and (now - last).total_seconds() < every_s:
            return False
        self._cooldown[key] = now
        return True

    # ------------------------------------------------------------------ код: правила

    def react(self, sit: Situation) -> list[Recommendation]:
        """Правила безопасности и проверенные реакции (без модели).

        Args:
            sit: Текущая ситуация.

        Returns:
            Рекомендации; команды в них пойдут через ``Guard`` (доска рекомендаций).
        """
        tel = sit.telemetry
        if tel is None:
            return []
        out: list[Recommendation] = []
        if not tel.comms_ok:
            if self._ready("comms", sit.ts, ESCALATION_S):
                out.append(
                    Recommendation(
                        agent=self.name,
                        title="Связь потеряна: решения по застывшим данным не принимаю",
                        rationale=(
                            "Данные телеметрии застыли. Команды не подаю. Резервный сценарий: управление у людей — "
                            "Оператор действует по инструкции и докладывает состояние голосом, Диагност сверяет по "
                            "независимым каналам. После восстановления связи перепроверить состояние перед командами."
                        ),
                        priority=1,
                        needs_human=True,
                    )
                )
            return out
        out += self._vent_for_soldering(tel, sit)
        out += self._co2_reaction(sit)
        return out

    def _vent_for_soldering(self, tel: Any, sit: Situation) -> list[Recommendation]:
        """Пайка при выключенной вытяжке (регламент п. 9.1)."""
        if not (tel.solder_status is DeviceStatus.RUNNING and tel.vent_mode == "off"):
            return []
        if not sit.vent_ok:
            self._vent_blocked_until = sit.ts + timedelta(seconds=VENT_BACKOFF_S)
        if self._vent_blocked_until is not None and sit.ts < self._vent_blocked_until:
            if not self._ready("vent-escalation", sit.ts, ESCALATION_S):
                return []
            return [
                Recommendation(
                    agent=self.name,
                    title="Вытяжка не отвечает при пайке: прекратить пайку",
                    rationale=(
                        "Идёт пайка, вытяжка выключена, а команда включения не дала эффекта. Безопасность важнее "
                        "результата (регламент п. 1.5, 9.1): Оператору прекратить пайку, включить вытяжку вручную или "
                        "проветрить помещение, сообщить ответственному за безопасность. Повторными командами не спамлю."
                    ),
                    priority=1,
                    needs_human=True,
                )
            ]
        if not self._ready("vent-cmd", sit.ts, VENT_RETRY_S):
            return []
        return [
            Recommendation(
                agent=self.name,
                title="Пайка без вытяжки: включаю вытяжку (low)",
                rationale="Правило безопасности: при пайке вытяжка обязательна (регламент п. 9.1). Режим low — щадящий для энергии.",
                priority=1,
                commands=[Command(name="vent_enable", args={"mode": "low"}, reason="пайка требует вытяжку")],
                needs_human=False,
            )
        ]

    def _co2_reaction(self, sit: Situation) -> list[Recommendation]:
        """Реакция на высокий CO2 — пропорционально уверенности вердикта (политика реакции)."""
        tel = sit.telemetry
        assert tel is not None
        out: list[Recommendation] = []
        for a in sit.alerts:
            if not (a.kind == "threshold" and a.field and a.field.startswith("co2_ppm.")):
                continue
            related = [v for v in sit.verdicts if a.field in v.fields]
            sensor = a.field.split(".", 1)[1]
            real = next((v for v in related if v.kind == "real"), None)
            if any(v.is_false for v in related):
                if self._ready(f"co2-bad:{a.field}", sit.ts, REACT_COOLDOWN_S):
                    out.append(
                        Recommendation(
                            agent=self.name,
                            title=f"CO2 {sensor}: показание недостоверно, вытяжку на полную не включаю",
                            rationale=next(v.explanation for v in related if v.is_false),
                            priority=3,
                            needs_human=False,
                            status=RecommendationStatus.INFO,
                        )
                    )
                continue
            if real is None:
                continue
            decision = self.policy.decide(a.field, real, a.level)
            if decision.level is ResponseLevel.PREPARE and self._ready(f"co2-prepare:{a.field}", sit.ts, REACT_COOLDOWN_S):
                out.append(
                    Recommendation(
                        agent=self.name,
                        title=f"CO2 {sensor} повышен, подтверждение пока слабое: готовлюсь и проверяю",
                        rationale=f"{real.explanation}\nУровень реакции: {decision.level.value} ({decision.reason}).",
                        priority=3,
                        needs_human=False,
                        status=RecommendationStatus.INFO,
                    )
                )
            if decision.level.rank >= ResponseLevel.ACT_REVERSIBLE.rank and tel.vent_mode == "off":
                if self._ready("co2-real", sit.ts, REACT_COOLDOWN_S):
                    out.append(
                        Recommendation(
                            agent=self.name,
                            title="CO2 повышен на всех датчиках: включаю вытяжку (low)",
                            rationale=f"{real.explanation}\nУровень реакции: {decision.level.value} ({decision.reason}).",
                            priority=2,
                            commands=[Command(name="vent_enable", args={"mode": "low"}, reason="реальный рост CO2")],
                            needs_human=False,
                        )
                    )
            elif decision.level is ResponseLevel.ACT_FULL and a.level is AlertLevel.CRITICAL and tel.vent_mode != "high":
                if self._ready("co2-high", sit.ts, REACT_COOLDOWN_S):
                    out.append(
                        Recommendation(
                            agent=self.name,
                            title="CO2 критичен и вытяжка low не справляется: подтвердите режим high",
                            rationale=(
                                f"Рост подтверждён независимыми источниками ({', '.join(decision.sources)}); режим high — "
                                "дорогое действие: нужны два подтверждения (есть) и решение человека."
                            ),
                            priority=1,
                            commands=[
                                Command(
                                    name="set_vent_mode",
                                    args={"mode": "high"},
                                    reason="критический CO2",
                                    costly=True,
                                    evidence=[e for e in real.evidence if e.supports == "real"],
                                )
                            ],
                            needs_human=True,
                        )
                    )
        return out

    # ------------------------------------------------------------------ модель

    def fingerprint(self, sit: Situation) -> str | None:
        """Отпечаток: набор значимых тревог и вердиктов сверки."""
        alerts = sit.significant_alerts()
        if not alerts and not sit.verdicts:
            return None
        a = sorted(f"{x.key}:{x.level.value}" for x in alerts)
        v = sorted(f"{x.rule}:{x.kind}:{','.join(x.fields)}" for x in sit.verdicts)
        return "|".join(a) + "||" + "|".join(v)

    def build_payload(self, sit: Situation) -> dict[str, Any]:
        """Снимок, тревоги, вердикты и свежие записи журнала."""
        return {
            "telemetry": telemetry_brief(sit.telemetry),
            "alerts": [alert_brief(a) for a in sit.significant_alerts()],
            "verdicts": [verdict_brief(v) for v in sit.verdicts],
            "journal_tail": [journal_brief(e) for e in sit.journal_tail[-5:]],
            "elapsed_s": round(sit.elapsed_s),
        }

    def summarize(self, sit: Situation) -> dict[str, Any]:
        """Сводка входа для журнала."""
        return {
            "alerts": [f"{a.level.value}:{a.kind}:{a.field}" for a in sit.significant_alerts()],
            "verdicts": [f"{v.kind}:{v.rule}" for v in sit.verdicts],
        }

    def mock(self, sit: Situation) -> MonitorOutput:
        """Детерминированная заглушка ответа модели (по тем же данным кода)."""
        alerts = sorted(sit.significant_alerts(), key=lambda a: (-a.level.rank, a.ts))
        if not alerts:
            v = sit.verdicts[0]
            return MonitorOutput(
                headline=f"Сверка: {v.kind} ({v.rule})", interpretation=v.explanation, priority=4, watch=list(v.fields)
            )
        top = alerts[0]
        parts = [a.message for a in alerts[:3]]
        parts += [v.explanation for v in sit.verdicts[:2]]
        return MonitorOutput(
            headline=top.message,
            interpretation=" ".join(parts),
            priority=level_priority(top.level),
            operator_message=self._operator_message(alerts),
            watch=[a.field for a in alerts if a.field][:4],
        )

    @staticmethod
    def _operator_message(alerts: list[Alert]) -> str:
        """Что сказать Оператору по виду тревоги (простые шаблоны)."""
        kinds = {a.kind for a in alerts}
        if "comms_loss" in kinds:
            return "Связь с Комплексом потеряна: доложите состояние оборудования голосом и ждите указаний."
        if "safety_vent_required" in kinds:
            return "Проверьте, что вытяжка включена; без вытяжки пайку не продолжать."
        if "device_error" in kinds:
            return "Осмотрите устройство в ошибке, не перезапуская; доложите результат."
        return ""

    def to_recommendations(self, out: MonitorOutput, sit: Situation) -> list[Recommendation]:
        """Ответ модели → рекомендация (только информирование)."""
        text = out.interpretation
        if out.operator_message:
            text += f"\nСообщить Оператору: {out.operator_message}"
        if out.watch:
            text += f"\nСмотреть: {', '.join(out.watch)}"
        return [
            Recommendation(
                agent=self.name,
                title=out.headline,
                rationale=text,
                priority=out.priority,
                needs_human=False,
                status=RecommendationStatus.INFO,
            )
        ]

    def fallback(self, sit: Situation) -> list[Recommendation]:
        """Резервный сценарий: сырые тревоги без интерпретации."""
        alerts = sorted(sit.significant_alerts(), key=lambda a: (-a.level.rank, a.ts))
        lines = [f"[{a.level.value}] {a.message}" for a in alerts] or ["Активных тревог нет"]
        prio = level_priority(alerts[0].level) if alerts else 4
        return [
            Recommendation(
                agent=self.name,
                title=f"Тревоги без интерпретации ({len(alerts)})",
                rationale="\n".join(lines),
                priority=prio,
                needs_human=False,
                status=RecommendationStatus.INFO,
            )
        ]
