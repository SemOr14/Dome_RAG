"""Политика реакции: чем сильнее уверенность и критичнее тревога, тем смелее допустимое действие.

Вердикт с уверенностью, а не бинарный флаг, позволяет реагировать *пропорционально*:

====================  ====================================================  ===============================
Уровень               Когда                                                 Допустимые действия
====================  ====================================================  ===============================
``observe``           низкая уверенность, нет риска безопасности            наблюдение, запись в журнал
``prepare``           средняя уверенность или один источник                 подготовка, безопасная проба,
                                                                            предупреждение Штаба
``act_reversible``    высокая уверенность или два независимых источника    обратимое малозатратное
                                                                            действие с проверкой
``act_full``          подтверждённая угроза, ≥ 2 независимых источников    полное действие по протоколу
                                                                            (с подтверждением человека)
====================  ====================================================  ===============================

Правила:

* уровень ограничен сверху уровнем тревоги (``response.max_level_by_alert``); прогноз «время до порога»
  сам по себе поднимает реакцию не выше ``prepare`` (рост при работе оборудования может быть нормальным);
* **исключение безопасности**: признаки угрозы жизни или здоровью (``smoke``, ``operator_stop``…) дают
  ``act_full`` немедленно, без правила двух; это ускорение безопасного ответа, а не обход блокировок —
  белый список и диапазоны команд проверяет ``Guard`` в любом случае;
* ложные виды вердикта (``false_signal``, ``sensor_fault``…) — только ``observe``.

Решение всегда считает код, а не модель; причина выбора записывается в журнал.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kupol.core.config import ResponseConfig
from kupol.core.models import AlertLevel, ResponseLevel, Verdict


@dataclass
class ResponseDecision:
    """Решение об уровне реакции.

    Attributes:
        subject: Утверждение (поле), к которому относится решение.
        level: Выбранный уровень.
        confidence: Уверенность вердикта в «реальном» (0, если вердикт ложный/нет вердикта).
        sources: Независимые источники подтверждения реального.
        alert_level: Уровень тревоги, ограничивающий реакцию.
        safety_event: Событие безопасности, обошедшее правило двух.
        reason: Почему выбран именно этот уровень.
        verdict_id: Вердикт, на котором основано решение.
        forecast_only: Тревога по утверждению — только прогноз «время до порога».
    """

    subject: str
    level: ResponseLevel
    confidence: float = 0.0
    sources: list[str] = field(default_factory=list)
    alert_level: AlertLevel = AlertLevel.INFO
    safety_event: str | None = None
    reason: str = ""
    verdict_id: str | None = None
    forecast_only: bool = False

    @property
    def independent_confirmations(self) -> int:
        """Число независимых подтверждений реального."""
        return len(self.sources)


def _min_level(a: ResponseLevel, b: ResponseLevel) -> ResponseLevel:
    """Меньший из двух уровней."""
    return a if a.rank <= b.rank else b


class ResponsePolicy:
    """Выбор уровня реакции по вердикту и тревоге."""

    def __init__(self, config: ResponseConfig) -> None:
        """Args:
        config: Раздел `response` конфигурации.
        """
        self._cfg = config

    def decide(
        self,
        subject: str,
        verdict: Verdict | None,
        alert_level: AlertLevel,
        *,
        safety_event: str | None = None,
        forecast_only: bool = False,
    ) -> ResponseDecision:
        """Определить уровень реакции.

        Args:
            subject: Утверждение (поле).
            verdict: Вердикт по нему (если есть).
            alert_level: Уровень самой тяжёлой тревоги по утверждению.
            safety_event: Событие безопасности (обходит правило двух).
            forecast_only: Тревога — только прогноз «время до порога».

        Returns:
            Решение с причиной.
        """
        cfg = self._cfg
        if safety_event is not None and safety_event in cfg.safety_override_events:
            return ResponseDecision(
                subject,
                ResponseLevel.ACT_FULL,
                confidence=1.0,
                alert_level=alert_level,
                safety_event=safety_event,
                reason=f"исключение безопасности '{safety_event}': реакция немедленная (правило двух не применяется)",
                verdict_id=verdict.id if verdict else None,
            )
        sources = sorted(verdict.sources_for("real")) if verdict else []
        if verdict is None or verdict.kind == "unknown":
            conf = verdict.confidence if verdict else 0.0
            base = ResponseLevel.PREPARE if alert_level.rank >= AlertLevel.WARNING.rank else ResponseLevel.OBSERVE
            why = "вывод пока не определён: готовим безопасную проверку" if base is ResponseLevel.PREPARE else "вывод не определён, тревога слабая"
        elif verdict.kind != "real":
            return ResponseDecision(
                subject,
                ResponseLevel.OBSERVE,
                alert_level=alert_level,
                reason=f"вердикт «{verdict.kind}» ({verdict.confidence:.0%}): сигнал не реален, действовать не нужно",
                verdict_id=verdict.id,
            )
        else:
            conf, n = verdict.confidence, len(sources)
            if conf >= cfg.full_confidence and n >= cfg.min_sources_full:
                base, why = ResponseLevel.ACT_FULL, f"уверенность {conf:.0%} и {n} независимых подтверждений"
            elif conf >= cfg.reversible_confidence or n >= cfg.min_sources_full:
                base, why = ResponseLevel.ACT_REVERSIBLE, f"высокая уверенность {conf:.0%} или {n} независимых подтверждений"
            elif conf >= cfg.prepare_confidence or n >= 1:
                base, why = ResponseLevel.PREPARE, f"средняя уверенность {conf:.0%} или один источник"
            else:
                base, why = ResponseLevel.OBSERVE, f"низкая уверенность {conf:.0%}"
        ceiling = ResponseLevel(cfg.max_level_by_alert.get(alert_level.value, "act_full"))
        level = _min_level(base, ceiling)
        notes = [why]
        if level is not base:
            notes.append(f"ограничено уровнем тревоги ({alert_level.value}) до {level.value}")
        if forecast_only and level.rank > ResponseLevel.PREPARE.rank:
            level = ResponseLevel.PREPARE
            notes.append("тревога — лишь прогноз до порога: не выше prepare")
        return ResponseDecision(
            subject,
            level,
            confidence=conf if verdict and verdict.kind == "real" else 0.0,
            sources=sources,
            alert_level=alert_level,
            reason="; ".join(notes),
            verdict_id=verdict.id if verdict else None,
            forecast_only=forecast_only,
        )

    def enough_for_costly(self, sources: set[str], safety_event: str | None) -> tuple[bool, str]:
        """Достаточно ли независимых свидетельств для дорогого действия (правило двух).

        Args:
            sources: Независимые источники подтверждения.
            safety_event: Событие безопасности (обходит правило).

        Returns:
            `(можно, пояснение)`.
        """
        if safety_event is not None and safety_event in self._cfg.safety_override_events:
            return True, f"исключение безопасности '{safety_event}'"
        need = self._cfg.min_sources_full
        if len(sources) >= need:
            return True, f"{len(sources)} независимых свидетельств: {', '.join(sorted(sources))}"
        return False, f"нужно не менее {need} независимых свидетельств, есть {len(sources)}" + (f" ({', '.join(sorted(sources))})" if sources else "")
