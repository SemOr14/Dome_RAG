"""«Обход»: чек-лист сверки пар источников по таймеру.

Каждые ``round.interval_s`` секунд (по умолчанию 420) Штаб открывает чек-лист: для каждой пары источников из
конфига (CO2 по датчикам, температура, баланс энергии, статус против потребления у станков…) человек сверяет
значения и отмечает «проверил, расхождений нет» или «расхождение». Расхождение создаёт тревогу, любая отметка
пишется в журнал. Обход — страховка от того, что автоматические сверки пропустят то, что видно глазами
(показание прибора, видео, слова Оператора).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from kupol.core.config import RoundConfig
from kupol.core.models import Telemetry


@dataclass
class RoundItem:
    """Пункт чек-листа обхода.

    Attributes:
        id: Идентификатор пункта в рамках обхода.
        label: Что сверяем (пара источников).
        fields: Участвующие источники.
        hint: Текущие значения источников для сверки глазами.
        status: ``pending`` / ``ok`` / ``mismatch``.
        marked_by: Кто отметил.
        note: Комментарий.
    """

    id: str
    label: str
    fields: list[str]
    hint: str = ""
    status: str = "pending"
    marked_by: str = ""
    note: str = ""


def describe_fields(tokens: list[str], latest: Telemetry | None, residual_w: float | None) -> str:
    """Текущие значения источников пункта обхода (для сверки глазами).

    Args:
        tokens: Имена источников из конфига (поля телеметрии, ``energy_balance``, ``<устройство>_status`` / ``_power``).
        latest: Последний снимок.
        residual_w: Последняя невязка энергобаланса, Вт.

    Returns:
        Строка вида ``co2_a=850; co2_b=858 (Δ=-8)``.
    """
    if latest is None:
        return "нет данных"
    flat = latest.flat()
    parts: list[str] = []
    values: list[float] = []
    for t in tokens:
        if t in flat:
            parts.append(f"{t}={flat[t]:.4g}")
            values.append(flat[t])
        elif t == "energy_balance":
            res = "н/д" if residual_w is None else f"{residual_w:+.0f} Вт"
            parts.append(f"невязка баланса {res}; P_pv−P_load={latest.pv_power_w - latest.load_total_w:+.0f} Вт; SoC {latest.battery_soc_pct:.0f} %")
        elif t.endswith("_status") and hasattr(latest, t):
            parts.append(f"{t}={getattr(latest, t).value}")
        elif t.endswith("_power"):
            line = latest.lines.get(t.removesuffix("_power"))
            parts.append(f"{t}={'н/д' if line is None else f'{line.power_w:.0f} Вт'}")
        else:
            parts.append(f"{t}=н/д")
    text = "; ".join(parts)
    if len(values) == 2:
        text += f" (Δ={values[0] - values[1]:+.4g})"
    return text


class RoundChecklist:
    """Чек-лист обхода по таймеру (потокобезопасность обеспечивает движок: действия идут через его очередь)."""

    def __init__(self, config: RoundConfig, start: datetime) -> None:
        """Args:
        config: Раздел `round` конфигурации.
        start: Время старта игры (первый обход — через `interval_s`).
        """
        self._cfg = config
        self._next_due = start + timedelta(seconds=config.interval_s)
        self.round_no = 0
        self.items: list[RoundItem] = []
        self.opened_at: datetime | None = None

    def due(self, now: datetime) -> bool:
        """Пора ли открывать очередной обход."""
        return bool(self._cfg.pairs) and now >= self._next_due

    def next_in_s(self, now: datetime) -> float:
        """Сколько секунд до следующего обхода (0, если уже пора)."""
        return max(0.0, (self._next_due - now).total_seconds())

    @property
    def is_open(self) -> bool:
        """Есть ли неотмеченные пункты текущего обхода."""
        return any(i.status == "pending" for i in self.items)

    def open_round(self, now: datetime, latest: Telemetry | None, residual_w: float | None) -> list[RoundItem]:
        """Открыть новый обход; незавершённый предыдущий закрывается как пропущенный.

        Args:
            now: Текущее время.
            latest: Последний снимок.
            residual_w: Невязка энергобаланса.

        Returns:
            Пункты нового обхода.
        """
        self.round_no += 1
        self.opened_at = now
        self._next_due = now + timedelta(seconds=self._cfg.interval_s)
        self.items = [
            RoundItem(
                id=f"r{self.round_no}-{i + 1}",
                label=" ↔ ".join(pair) if len(pair) > 1 else pair[0],
                fields=list(pair),
                hint=describe_fields(list(pair), latest, residual_w),
            )
            for i, pair in enumerate(self._cfg.pairs)
        ]
        return self.items

    def mark(self, item_id: str, ok: bool, who: str, note: str = "") -> RoundItem | None:
        """Отметить пункт обхода.

        Args:
            item_id: Идентификатор пункта.
            ok: ``True`` — «проверил, расхождений нет», ``False`` — «расхождение».
            who: Кто отметил.
            note: Комментарий.

        Returns:
            Пункт либо `None`, если такого нет или он уже отмечен.
        """
        for item in self.items:
            if item.id == item_id and item.status == "pending":
                item.status = "ok" if ok else "mismatch"
                item.marked_by, item.note = who, note
                return item
        return None

