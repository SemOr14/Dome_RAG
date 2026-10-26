"""Агент «Энергопланировщик»: порядок операций и что отложить.

Код (``core/energy.py``) считает запас времени, проверяет «можно ли начать» и формирует
КАНДИДАТОВ на экономию (с готовыми командами). Модель только ранжирует: выбирает номера
кандидатов, порядок и объяснение. Придумать команду она не может — применяются лишь
команды из списка кандидатов, и всё равно через ``Guard``.

Автономия: безопасные команды класса ``auto`` (например, увлажнитель) выполняются сами, как
разрешено в ``config.yaml``; остальное ждёт подтверждения человека на доске рекомендаций.

Резервный сценарий: расчёт по формуле из ``core/energy.py`` — применить всех кандидатов.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from kupol.agents.base import Agent, Situation, level_priority, telemetry_brief
from kupol.core.energy import PRIORITY_OTHER, PlanItem
from kupol.core.models import AlertLevel, Evidence, Recommendation, RecommendationStatus


COMFORT_T_LEFT_MIN = 240.0  # запас дольше этого считаем «в норме» (игра идёт ~90 минут)


class EnergyOutput(BaseModel):
    """Строгая схема ответа модели-Энергопланировщика."""

    summary: str = Field(description="Одна строка: энергетическая ситуация и главный вывод")
    order: list[str] = Field(description="Названия оставшихся этапов в рекомендуемом порядке (только из плана)")
    defer: list[str] = Field(default_factory=list, description="Названия этапов, которые отложить (только из плана)")
    apply_advice: list[int] = Field(default_factory=list, description="Номера применяемых кандидатов экономии")
    rationale: str = Field(description="Объяснение для человека, с цифрами")


class EnergyPlannerAgent(Agent):
    """Энергопланировщик: рекомендации по порядку операций и экономии."""

    name = "energy_planner"
    prompt_name = "energy_planner"
    schema = EnergyOutput

    def fingerprint(self, sit: Situation) -> str | None:
        """Отпечаток: уровень запаса, состав плана и кандидатов + временная корзина для планового обновления."""
        if sit.energy is None or sit.telemetry is None:
            return None
        bucket = int(sit.elapsed_s // self.config.agents.plan_interval_s)
        if sit.energy.level is None and not sit.advice:
            return f"ok|{bucket}"  # в норме — только плановое обновление
        plan = ",".join(f"{p.operation.name}:{int(p.can_start)}" for p in sit.plan)
        return f"{sit.energy.level.value if sit.energy.level else 'ok'}|{bucket}|{len(sit.advice)}|{plan}"

    def urgent(self, sit: Situation) -> bool:
        """Срочно при новой критической энергетической тревоге."""
        return any(a.kind == "energy_t_left" and a.level is AlertLevel.CRITICAL for a in sit.new_alerts)

    # ------------------------------------------------------------------ вход

    @staticmethod
    def _plan_brief(plan: list[PlanItem]) -> list[dict[str, Any]]:
        """Компактный план для модели."""
        return [
            {
                "name": p.operation.name,
                "priority_class": p.priority,
                "power_w": p.operation.power_w,
                "duration_min": p.operation.duration_min,
                "interruptible": p.operation.interruptible,
                "can_start_now": p.can_start,
                "reason": p.reason,
            }
            for p in plan
        ]

    def build_payload(self, sit: Situation) -> dict[str, Any]:
        """Энергостатус, план операций и кандидаты на экономию."""
        e = sit.energy
        return {
            "telemetry": telemetry_brief(sit.telemetry),
            "energy": None
            if e is None
            else {
                "soc_pct": round(e.soc_pct, 1),
                "pv_w": round(e.pv_w),
                "load_w": round(e.load_w),
                "net_w": round(e.net_w),
                "available_wh": round(e.available_wh),
                "t_left_min": None if e.t_left_min is None else round(e.t_left_min, 1),
                "level": e.level.value if e.level else "ok",
            },
            "plan": self._plan_brief(sit.plan),
            "advice_candidates": [
                {"index": i, "text": a.text, "saving_w": round(a.saving_w), "command": None if a.command is None else a.command.name}
                for i, a in enumerate(sit.advice)
            ],
            "vent_ok": sit.vent_ok,
        }

    def summarize(self, sit: Situation) -> dict[str, Any]:
        """Сводка входа для журнала."""
        e = sit.energy
        return {
            "t_left_min": None if e is None or e.t_left_min is None else round(e.t_left_min, 1),
            "level": "ok" if e is None or e.level is None else e.level.value,
            "advice": len(sit.advice),
            "plan": [p.operation.name for p in sit.plan],
        }

    # ------------------------------------------------------------------ заглушка и разбор

    def _formula_text(self, sit: Situation) -> str:
        """Объяснение расчёта по формуле t_left."""
        e = sit.energy
        assert e is not None
        if e.t_left_min is None:
            return f"АКБ не разряжается: генерация {e.pv_w:.0f} Вт ≥ нагрузки {e.load_w:.0f} Вт (SoC {e.soc_pct:.0f} %)."
        return (
            f"t_left = {e.available_wh:.0f} Вт·ч / ({e.load_w:.0f} − {e.pv_w:.0f}) Вт = {e.t_left_min:.0f} мин "
            f"до минимального заряда (SoC {e.soc_pct:.0f} %)."
        )

    def _defer_names(self, sit: Situation) -> list[str]:
        """Этапы, которые можно отложить: прерываемые, не связанные с безопасностью."""
        if sit.energy is None or sit.energy.level is None:
            return []
        return [p.operation.name for p in sit.plan if p.priority == PRIORITY_OTHER]

    def mock(self, sit: Situation) -> EnergyOutput:
        """Детерминированная заглушка ответа модели."""
        order = [p.operation.name for p in sit.plan]
        return EnergyOutput(
            summary=self._summary(sit),
            order=order,
            defer=self._defer_names(sit),
            apply_advice=list(range(len(sit.advice))),
            rationale=self._formula_text(sit),
        )

    def _summary(self, sit: Situation) -> str:
        """Заголовок рекомендации."""
        e = sit.energy
        assert e is not None
        if e.t_left_min is None:
            return "Энергия в норме: АКБ не разряжается, план операций обновлён"
        if e.t_left_min > COMFORT_T_LEFT_MIN:
            return f"Энергия в норме: запаса хватает больше чем на {COMFORT_T_LEFT_MIN / 60:.0f} ч"
        word = {"critical": "Критически мало энергии", "warning": "Запас энергии снижается", None: "План операций с учётом энергии"}
        return f"{word[e.level.value if e.level else None]}: ≈{e.t_left_min:.0f} мин до минимального заряда"

    def to_recommendations(self, out: EnergyOutput, sit: Situation) -> list[Recommendation]:
        """Ответ модели → рекомендация; команды берутся только из кандидатов кода.

        Raises:
            ValueError: Модель сослалась на несуществующий этап или кандидата (применится фолбэк).
        """
        names = {p.operation.name for p in sit.plan}
        unknown = [n for n in [*out.order, *out.defer] if n not in names]
        if unknown:
            raise ValueError(f"в ответе модели неизвестные этапы: {unknown}")
        bad_idx = [i for i in out.apply_advice if not 0 <= i < len(sit.advice)]
        if bad_idx:
            raise ValueError(f"в ответе модели неизвестные номера кандидатов: {bad_idx}")
        return [self._build(sit, out.summary, out.order, out.defer, list(dict.fromkeys(out.apply_advice)), out.rationale)]

    def fallback(self, sit: Situation) -> list[Recommendation]:
        """Резервный сценарий: расчёт по формуле, применить всех кандидатов кода."""
        order = [p.operation.name for p in sit.plan]
        return [
            self._build(
                sit,
                self._summary(sit) + " (расчёт по формуле)",
                order,
                self._defer_names(sit),
                list(range(len(sit.advice))),
                self._formula_text(sit),
            )
        ]

    def _evidence(self, sit: Situation) -> list[Evidence]:
        """Независимые свидетельства нехватки энергии для дорогих команд (правило двух).

        Два канала: датчик заряда АКБ (запас ниже порога) и энергобаланс по датчикам генерации и нагрузки
        (АКБ разряжается). Если сверка признала баланс недостоверным, второе свидетельство не прикладывается —
        и дорогое действие останется рекомендацией.
        """
        e = sit.energy
        if e is None or e.level is None or e.t_left_min is None:
            return []
        out = [
            Evidence(
                source="battery_soc",
                supports="real",
                weight=0.7,
                detail=f"SoC {e.soc_pct:.0f} %, запас до минимального заряда ≈{e.t_left_min:.0f} мин",
                rule="energy_t_left",
            )
        ]
        balance_bad = any(v.fields and v.fields[0] == "energy_balance" and v.is_false for v in sit.verdicts)
        if not balance_bad and e.net_w < 0:
            out.append(
                Evidence(
                    source="energy_balance",
                    supports="real",
                    weight=0.7,
                    detail=f"генерация {e.pv_w:.0f} Вт < нагрузки {e.load_w:.0f} Вт: АКБ разряжается",
                    rule="energy_balance",
                )
            )
        return out

    def _build(
        self, sit: Situation, title: str, order: list[str], defer: list[str], advice_idx: list[int], rationale: str
    ) -> Recommendation:
        """Собрать рекомендацию из выбранных кандидатов."""
        parts = [rationale]
        if order:
            by_name = {p.operation.name: p for p in sit.plan}
            lines = []
            for i, name in enumerate(order, 1):
                item = by_name.get(name)
                mark = "" if item is None else (" — можно начинать" if item.can_start else f" — нельзя сейчас: {item.reason}")
                lines.append(f"{i}. {name}{mark}")
            parts.append("Порядок этапов:\n" + "\n".join(lines))
        if defer:
            parts.append("Отложить (указание Оператору): " + ", ".join(defer))
        chosen = [sit.advice[i] for i in advice_idx]
        for a in chosen:
            parts.append(f"• {a.text}" + (f" (запас после: ≈{a.t_left_after_min:.0f} мин)" if a.t_left_after_min is not None else ""))
        evidence = self._evidence(sit)
        commands = [
            a.command.model_copy(update={"evidence": evidence}) if a.command is not None else None for a in chosen
        ]
        commands = [c for c in commands if c is not None]
        needs_human = bool(defer) or any(a.command is None for a in chosen)
        level = sit.energy.level if sit.energy else None
        return Recommendation(
            agent=self.name,
            title=title,
            rationale="\n".join(parts),
            priority=level_priority(level) if level else 4,
            commands=commands,
            needs_human=needs_human,
            status=RecommendationStatus.INFO,
        )
