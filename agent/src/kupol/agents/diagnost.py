"""Агент «Диагност»: ранжированные гипотезы, безопасные пробы и рекомендуемый уровень реакции.

**Разделение труда: код считает и разрешает, модель ранжирует, объясняет и предлагает.**

* Вход модели: текущие вердикты со свидетельствами и статусами, невязка баланса, сигналы детекторов,
  доступные безопасные пробы (уже отфильтрованные кодом по риску, обратимости и бюджету энергии), решения
  политики реакции, фрагмент журнала и подходящие строки матрицы «симптом → причины → проверка».
* Выход модели (строгий JSON): ранжированные гипотезы; для каждой — безопасная проба и ожидаемый результат при
  каждом исходе; рекомендуемый уровень реакции; нужен ли человек.
* **Уверенность модель не считает**: вероятности гипотез и уровень реакции берутся из кода (вердикты и
  политика реакции). Рекомендованный моделью уровень срезается до кодового; придуманные пробы и
  несуществующие поля отвергаются — тогда применяется резервный сценарий.
* Диагност команд не подаёт: только рекомендации (команды проб запускает движок через `Guard`).

Резервный сценарий (модель недоступна): гипотезы и пробы из каталога по правилам (без LLM) плюс чек-лист
регламента п. 7.3; рекомендации помечены `used_fallback=True`.
"""

from __future__ import annotations

from typing import Any, Literal

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
from kupol.agents.llm import load_examples, load_prompt
from kupol.agents.symptoms import SymptomRow, match_rows, rank_causes
from kupol.core.models import (
    Alert,
    AlertLevel,
    Hypothesis,
    Probe,
    Recommendation,
    RecommendationStatus,
    ResponseLevel,
)

MAX_HYPOTHESES = 5
NOVEL_HYPOTHESIS_CAP = 0.1  # гипотеза вне матрицы получает не больше этой вероятности (ею не управляет модель)
LevelName = Literal["observe", "prepare", "act_reversible", "act_full"]


class DiagHypothesisOut(BaseModel):
    """Гипотеза в ответе модели."""

    cause: str = Field(description="Предполагаемая причина (по возможности дословно из matrix)")
    safe_check: str = Field(description="Безопасная проверка словами")
    safe_probe: str | None = Field(default=None, description="id пробы из available_probes, если подходит")
    expected_if_real: str = Field(default="", description="Что увидим при пробе, если событие реально")
    expected_if_false: str = Field(default="", description="Что увидим при пробе, если сигнал ложный")
    probability: float | None = Field(default=None, ge=0, le=1, description="Подсказка порядка; итог считает код")


class DiagnostOutput(BaseModel):
    """Строгая схема ответа модели-Диагноста."""

    summary: str = Field(description="Одна строка: что, по-видимому, происходит")
    hypotheses: list[DiagHypothesisOut] = Field(description="Гипотезы по убыванию правдоподобия; у каждой безопасная проверка")
    recommended_action: str = Field(description="Рекомендуемое корректирующее действие и исполнитель")
    recommended_level: LevelName = Field(default="observe", description="Рекомендуемый уровень реакции")
    needs_human: bool = Field(description="Нужно ли решение или действие человека")


def checklist_7_3(alerts: list[Alert], rows: list[SymptomRow]) -> list[str]:
    """Чек-лист регламента п. 7.3 для текущих тревог (без модели).

    Args:
        alerts: Значимые тревоги.
        rows: Подходящие строки матрицы.

    Returns:
        Нумерованные шаги.
    """
    first = alerts[0] if alerts else None
    where = f"{first.kind}/{first.field}" if first and first.field else (first.kind if first else "—")
    when = first.ts.strftime("%H:%M:%S") if first else "—"
    action = rows[0].action if rows else "уточнить причину дополнительными проверками"
    return [
        f"1. Зафиксировать событие и время обнаружения: {where}, {when} UTC.",
        "2. Проверить достоверность по независимым источникам (другие датчики той же величины, энергобаланс, статусы и потребление, слова Оператора).",
        "3. Сформировать гипотезу и проверить её безопасным способом (см. причины ниже).",
        "4. Оценить влияние на людей, оборудование, ресурсы (энергия, вытяжка) и производственную задачу.",
        f"5. Выбрать допустимое действие и исполнителя: {action}.",
        "6. Выполнить действие в пределах полномочий (команды — только через предохранитель; оборудование — через Оператора).",
        "7. Проверить результат и записать решение в журнал.",
    ]


class Diagnost(Agent):
    """Диагност: гипотезы, пробы и рекомендуемый уровень реакции."""

    name = "diagnost"
    prompt_name = "diagnost"
    schema = DiagnostOutput

    def system_prompt(self) -> str:
        """Промпт с few-shot примерами по типам воздействий (`prompts/examples/diagnost_*.md`)."""
        return load_prompt(self.prompt_name) + "\n\n" + load_examples(self.prompt_name)

    # ------------------------------------------------------------------ отбор

    def _rows(self, sit: Situation) -> list[SymptomRow]:
        """Строки матрицы, подходящие к значимым тревогам и вердиктам."""
        return match_rows(sit.significant_alerts(), sit.verdicts)

    def fingerprint(self, sit: Situation) -> str | None:
        """Отпечаток: значимые тревоги (без чисто энергетических и прогнозов) и определённые вердикты."""
        alerts = [a for a in sit.significant_alerts() if a.kind not in ("energy_t_left", "time_to_threshold")]
        verdicts = [v for v in sit.verdicts if v.kind != "unknown"]
        if not alerts and not verdicts:
            return None
        a = sorted(f"{x.key}:{x.level.value}" for x in alerts)
        v = sorted(f"{x.fields[0] if x.fields else ''}:{x.kind}" for x in verdicts)
        return "|".join(a) + "||" + "|".join(v)

    # ------------------------------------------------------------------ вход модели

    def build_payload(self, sit: Situation) -> dict[str, Any]:
        """Вердикты со свидетельствами, невязка, сигналы детекторов, пробы, уровни реакции, журнал и матрица."""
        rows = self._rows(sit)
        return {
            "telemetry": telemetry_brief(sit.telemetry),
            "alerts": [alert_brief(a) for a in sit.significant_alerts()],
            "verdicts": [verdict_brief(v) for v in sit.verdicts],
            "balance_residual_w": None if sit.residual_w is None else round(sit.residual_w),
            "available_probes": [self._probe_brief(p) for p in sit.available_probes],
            "response_levels": {k: {"level": d.level.value, "confidence": round(d.confidence, 2), "reason": d.reason} for k, d in sit.responses.items()},
            "journal_tail": [journal_brief(e) for e in sit.journal_tail[-8:]],
            "matrix": [
                {
                    "id": r.id,
                    "symptom": r.title,
                    "causes": [{"cause": h.cause, "probability": h.probability, "safe_check": h.safe_check} for h in rank_causes(r, sit.verdicts)],
                    "action": r.action,
                    "needs_human": r.needs_human,
                }
                for r in rows
            ],
        }

    @staticmethod
    def _probe_brief(p: Probe) -> dict[str, Any]:
        """Проба для модели: что делает, чем платим, что ожидаем."""
        return {
            "id": p.id,
            "description": p.description,
            "cost_wh": p.cost_wh,
            "cost_s": p.cost_s,
            "by_human": p.human_action is not None,
            "expected_if_real": p.expected_if_real,
            "expected_if_false": p.expected_if_false,
        }

    def summarize(self, sit: Situation) -> dict[str, Any]:
        """Сводка входа для журнала."""
        return {
            "alerts": [f"{a.level.value}:{a.kind}:{a.field}" for a in sit.significant_alerts()],
            "verdicts": [f"{v.kind}:{v.confidence:.2f}:{v.fields[0] if v.fields else ''}" for v in sit.verdicts],
            "matrix_rows": [r.id for r in self._rows(sit)],
            "probes": [p.id for p in sit.available_probes],
        }

    # ------------------------------------------------------------------ расчёты кода

    def _hypotheses(self, sit: Situation) -> list[Hypothesis]:
        """Гипотезы по всем подходящим строкам: вероятности считает код (матрица и вердикты сверки)."""
        rows = self._rows(sit)
        top_verdict = next((v for v in sit.verdicts if v.kind != "unknown"), sit.verdicts[0] if sit.verdicts else None)
        alert_keys = [a.key for a in sit.significant_alerts()]
        if not rows:
            return [
                Hypothesis(
                    cause="Причина не определена по матрице симптомов",
                    probability=1.0,
                    safe_check="Собрать больше данных: сверить независимые источники и запросить наблюдения Оператора",
                    safe_probe=self._pick_probe(sit, "")[0],
                    related_alerts=alert_keys,
                    verdict_id=top_verdict.id if top_verdict else None,
                )
            ]
        pool: list[Hypothesis] = []
        for r in rows:
            pool += rank_causes(r, sit.verdicts)[:3]
        pool.sort(key=lambda h: -h.probability)
        top = pool[:MAX_HYPOTHESES]
        total = sum(h.probability for h in top) or 1.0
        out: list[Hypothesis] = []
        for h in top:
            probe_id, exp_real, exp_false = self._pick_probe(sit, h.cause)
            out.append(
                Hypothesis(
                    cause=h.cause,
                    probability=round(h.probability / total, 2),
                    safe_check=h.safe_check,
                    safe_probe=probe_id,
                    expected_if_real=exp_real,
                    expected_if_false=exp_false,
                    related_alerts=alert_keys,
                    verdict_id=top_verdict.id if top_verdict else None,
                )
            )
        return out

    @staticmethod
    def _pick_probe(sit: Situation, cause: str) -> tuple[str | None, str, str]:
        """Подобрать пробу из доступных по правилу (без LLM): сенсорные причины — автоматическая сверка, остальные — Оператор."""
        if not sit.available_probes:
            return None, "", ""
        sensor_like = any(w in cause.lower() for w in ("датчик", "канал", "ложн", "дрейф", "метка", "помеха"))
        order = sorted(
            sit.available_probes,
            key=lambda p: (p.human_action is not None) if sensor_like else (p.human_action is None),  # сначала подходящие по типу причины
        )
        p = order[0]
        return p.id, p.expected_if_real, p.expected_if_false

    def _code_level(self, sit: Situation) -> ResponseLevel:
        """Максимальный уровень реакции по решениям кода (по умолчанию — наблюдение)."""
        return max((d.level for d in sit.responses.values()), key=lambda lv: lv.rank, default=ResponseLevel.OBSERVE)

    def _priority(self, sit: Situation) -> int:
        """Приоритет по самой тяжёлой значимой тревоге."""
        alerts = sit.significant_alerts()
        return level_priority(max(alerts, key=lambda a: a.level.rank).level) if alerts else 4

    def _needs_human(self, sit: Situation) -> bool:
        """Нужен ли человек: по строке матрицы или критической тревоге."""
        return any(r.needs_human for r in self._rows(sit)) or any(a.level is AlertLevel.CRITICAL for a in sit.significant_alerts())

    def _confidence_text(self, sit: Situation) -> str:
        """Уверенность по вердиктам — её считает код, а не модель."""
        parts = [
            f"{v.fields[0] if v.fields else '—'}: {v.kind} {v.confidence:.0%} ({v.status})" for v in sit.verdicts if v.kind != "unknown"
        ]
        return "Уверенность (расчёт кода): " + ("; ".join(parts[:4]) if parts else "вердиктов пока нет") + "."

    # ------------------------------------------------------------------ заглушка и разбор

    def mock(self, sit: Situation) -> DiagnostOutput:
        """Детерминированная заглушка ответа модели (по тем же данным кода)."""
        rows = self._rows(sit)
        action = "; ".join(dict.fromkeys(r.action for r in rows)) or "Продолжать наблюдение и собрать больше данных"
        titles = ", ".join(r.title.lower() for r in rows) or "отклонение без совпадения в матрице"
        return DiagnostOutput(
            summary=f"Диагностика: {titles}",
            hypotheses=[
                DiagHypothesisOut(
                    cause=h.cause,
                    safe_check=h.safe_check,
                    safe_probe=h.safe_probe,
                    expected_if_real=h.expected_if_real,
                    expected_if_false=h.expected_if_false,
                    probability=h.probability,
                )
                for h in self._hypotheses(sit)
            ],
            recommended_action=f"{action}. Исполнитель: Диагност проверяет, Оператор выполняет физические проверки.",
            recommended_level=self._code_level(sit).value,
            needs_human=self._needs_human(sit),
        )

    def to_recommendations(self, out: DiagnostOutput, sit: Situation) -> list[Recommendation]:
        """Ответ модели → рекомендация; вероятности и уровень реакции перепроверяет код.

        Raises:
            ValueError: Модель сослалась на несуществующую пробу (применится резервный сценарий).
        """
        allowed = {p.id for p in sit.available_probes}
        unknown = [h.safe_probe for h in out.hypotheses if h.safe_probe and h.safe_probe not in allowed]
        if unknown:
            raise ValueError(f"в ответе модели недоступные пробы: {unknown}")
        by_cause = {h.cause.strip().lower(): h for h in self._hypotheses(sit)}
        top_verdict = next((v for v in sit.verdicts if v.kind != "unknown"), None)
        alert_keys = [a.key for a in sit.significant_alerts()]
        matched: list[Hypothesis] = []
        novel: list[Hypothesis] = []
        for h in out.hypotheses[:MAX_HYPOTHESES]:
            code_h = by_cause.get(h.cause.strip().lower())
            base = dict(
                cause=h.cause,
                safe_check=h.safe_check,
                safe_probe=h.safe_probe,
                expected_if_real=h.expected_if_real,
                expected_if_false=h.expected_if_false,
                related_alerts=alert_keys,
                verdict_id=top_verdict.id if top_verdict else None,
            )
            if code_h is not None:
                matched.append(Hypothesis(probability=code_h.probability, **base))
            else:
                novel.append(Hypothesis(probability=min(h.probability or NOVEL_HYPOTHESIS_CAP, NOVEL_HYPOTHESIS_CAP), **base))
        hyps = sorted(matched, key=lambda h: -h.probability) + novel
        total = sum(h.probability for h in hyps) or 1.0
        hyps = [h.model_copy(update={"probability": round(h.probability / total, 2)}) for h in hyps]
        code_level = self._code_level(sit)
        model_level = ResponseLevel(out.recommended_level)
        level = model_level if model_level.rank <= code_level.rank else code_level
        note = f"Уровень реакции: {level.value}"
        if level is not model_level:
            note += f" (модель предлагала {model_level.value}, код ограничил до {code_level.value})"
        needs_human = out.needs_human or self._needs_human(sit)
        return [
            Recommendation(
                agent=self.name,
                title=out.summary,
                rationale=f"{out.recommended_action}\n{note}.\n{self._confidence_text(sit)}",
                priority=self._priority(sit),
                hypotheses=hyps,
                needs_human=needs_human,
                status=RecommendationStatus.PENDING if needs_human else RecommendationStatus.INFO,
            )
        ]

    def fallback(self, sit: Situation) -> list[Recommendation]:
        """Резервный сценарий: гипотезы и пробы из каталога по правилам + чек-лист п. 7.3 (без модели)."""
        alerts = sorted(sit.significant_alerts(), key=lambda a: (-a.level.rank, a.ts))
        rows = self._rows(sit)
        steps = "\n".join(checklist_7_3(alerts, rows))
        probes = "; ".join(f"{p.id}: {p.description}" for p in sit.available_probes) or "доступных проб нет"
        return [
            Recommendation(
                agent=self.name,
                title="Чек-лист диагностики (п. 7.3), модель недоступна",
                rationale=f"{steps}\nДоступные безопасные пробы: {probes}.\nУровень реакции (код): {self._code_level(sit).value}.\n{self._confidence_text(sit)}",
                priority=self._priority(sit),
                hypotheses=self._hypotheses(sit),
                needs_human=True,
                status=RecommendationStatus.PENDING,
            )
        ]
