"""Матрица «симптом → причины → безопасная проверка» и подбор строк по тревогам.

Данные лежат в ``symptom_matrix.yaml``. Подбор и ранжирование причин — чистый код:
вердикты сверки источников усиливают причины, которые они подтверждают (``supports``).
Матрица используется Диагностом дважды: как few-shot примеры в промпте и как основа
резервного сценария.
"""

from __future__ import annotations

import fnmatch
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from kupol.core.models import Alert, Hypothesis, Verdict

MATRIX_FILE = Path(__file__).parent / "symptom_matrix.yaml"
SUPPORT_BOOST = 3.0  # во сколько раз вердикт сверки усиливает подтверждаемую причину


class Cause(BaseModel):
    """Вероятная причина с безопасной проверкой."""

    model_config = ConfigDict(extra="forbid")

    cause: str
    probability: float
    safe_check: str
    supports: list[str] = []


class SymptomRow(BaseModel):
    """Строка матрицы."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    alert_kinds: list[str]
    fields: list[str]
    verdict_rules: list[str]
    causes: list[Cause]
    action: str
    needs_human: bool


@lru_cache(maxsize=1)
def load_matrix(path: Path = MATRIX_FILE) -> tuple[SymptomRow, ...]:
    """Загрузить матрицу из YAML.

    Args:
        path: Путь к файлу матрицы.

    Returns:
        Строки матрицы.

    Raises:
        ValueError: Файл повреждён или строка не прошла проверку.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path}: ожидался список строк матрицы")
    rows = tuple(SymptomRow.model_validate(r) for r in raw)
    ids = [r.id for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("id строк матрицы должны быть уникальны")
    return rows


def row_matches(row: SymptomRow, alerts: list[Alert], verdicts: list[Verdict]) -> bool:
    """Подходит ли строка к текущим тревогам и вердиктам.

    Args:
        row: Строка матрицы.
        alerts: Активные тревоги.
        verdicts: Вердикты сверки.

    Returns:
        ``True``, если совпала хотя бы одна тревога (вид и поле) либо правило сверки.
    """
    for a in alerts:
        if a.kind not in row.alert_kinds:
            continue
        if row.fields and not (a.field is not None and any(fnmatch.fnmatchcase(a.field, p) for p in row.fields)):
            continue
        # Если по полю тревоги уже есть вердикты сверки, строка должна быть «про» один из них:
        # так скачок одного датчика CO2 не смешивается с реальным ростом на всех датчиках.
        related = [v for v in verdicts if a.field is not None and a.field in v.fields]
        if row.verdict_rules and related and not any(_rules_of(v) & set(row.verdict_rules) for v in related):
            continue
        return True
    return any(bool(_rules_of(v) & set(row.verdict_rules)) for v in verdicts)


def match_rows(alerts: list[Alert], verdicts: list[Verdict]) -> list[SymptomRow]:
    """Строки матрицы, подходящие к ситуации (в порядке матрицы).

    Args:
        alerts: Активные тревоги.
        verdicts: Вердикты сверки.

    Returns:
        Список подходящих строк.
    """
    return [r for r in load_matrix() if row_matches(r, alerts, verdicts)]


def rank_causes(row: SymptomRow, verdicts: list[Verdict]) -> list[Hypothesis]:
    """Ранжировать причины строки с учётом вердиктов сверки.

    Вероятность причины умножается на ``SUPPORT_BOOST``, если её ``supports`` содержит вид
    вердикта, относящегося к этой строке (по правилу или по полю), затем нормируется на 1.

    Args:
        row: Строка матрицы.
        verdicts: Вердикты сверки.

    Returns:
        Гипотезы по убыванию вероятности.
    """
    relevant: set[str] = set()
    for v in verdicts:
        if _rules_of(v) & set(row.verdict_rules) or _fields_relevant(row, v):
            relevant |= _kinds_of(v)
    weights = [c.probability * (SUPPORT_BOOST if relevant & set(c.supports) else 1.0) for c in row.causes]
    total = sum(weights) or 1.0
    ranked = sorted(
        (
            Hypothesis(cause=c.cause, probability=round(w / total, 2), safe_check=c.safe_check)
            for c, w in zip(row.causes, weights, strict=True)
        ),
        key=lambda h: -h.probability,
    )
    return ranked


def _rules_of(v: Verdict) -> set[str]:
    """Все правила, давшие свидетельства вердикту (главное правило и правила из его свидетельств)."""
    return {v.rule, *(e.rule for e in v.evidence)} - {""}


def _kinds_of(v: Verdict) -> set[str]:
    """Виды вердикта для сопоставления с `supports`: новые виды «ложного» сводятся и к `sensor_fault`."""
    return {v.kind, "sensor_fault"} if v.kind in ("channel_fault", "drift") else {v.kind}


def _fields_relevant(row: SymptomRow, verdict: Verdict) -> bool:
    """Относится ли вердикт к полям, которыми занимается строка."""
    return any(fnmatch.fnmatchcase(f, p) for f in verdict.fields for p in row.fields)


def render_markdown(rows: tuple[SymptomRow, ...] | None = None) -> str:
    """Построить читаемую таблицу матрицы для ``docs/SYMPTOM_MATRIX.md``.

    Args:
        rows: Строки матрицы; по умолчанию загруженные из YAML.

    Returns:
        Markdown-текст. Обновить файл: ``python -m kupol.agents.symptoms > docs/SYMPTOM_MATRIX.md``.
    """
    rows = rows if rows is not None else load_matrix()
    out = [
        "# Матрица «симптом → причины → безопасная проверка»",
        "",
        "Файл сгенерирован из `src/kupol/agents/symptom_matrix.yaml` (источник истины). Не править вручную:",
        "`python -m kupol.agents.symptoms > docs/SYMPTOM_MATRIX.md`. Вероятности — априорные оценки (ASSUMPTION).",
        "",
        "| id | Симптом | Причины (вероятность) | Безопасная проверка | Действие | Нужен человек |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        causes = "<br>".join(f"{c.cause} ({c.probability:.0%})" for c in r.causes)
        checks = "<br>".join(f"{i}. {c.safe_check}" for i, c in enumerate(r.causes, 1))
        human = "да" if r.needs_human else "нет"
        out.append(f"| `{r.id}` | {r.title} | {causes} | {checks} | {r.action} | {human} |")
    return "\n".join(out) + "\n"


if __name__ == "__main__":  # pragma: no cover - служебный запуск
    import sys

    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stdout.write(render_markdown())
