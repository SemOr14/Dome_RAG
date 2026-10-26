"""Единая модель состояния Комплекса.

Хранит последний снимок и кольцевой буфер истории (по умолчанию 15 минут).
Все агенты и детекторы читают состояние отсюда, а не напрямую из адаптера.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from kupol.core.models import Telemetry


@dataclass(frozen=True)
class LinFit:
    """Результат линейной аппроксимации ряда по методу наименьших квадратов.

    Attributes:
        slope_per_min: Наклон, единиц в минуту.
        intercept: Значение аппроксимации в момент последнего снимка.
        r2: Коэффициент детерминации (качество тренда), 0..1.
        n: Число точек.
        span_s: Длительность окна, с.
        start: Время первой точки.
        end: Время последней точки.
    """

    slope_per_min: float
    intercept: float
    r2: float
    n: int
    span_s: float
    start: datetime
    end: datetime


class State:
    """Состояние Комплекса: последний снимок и история телеметрии.

    Время всегда берётся из `Telemetry.ts` (виртуальное в симуляции), поэтому
    методы не зависят от системных часов.
    """

    def __init__(self, history_minutes: float = 15.0, change_eps: float = 1e-9) -> None:
        """Создать пустое состояние.

        Args:
            history_minutes: Глубина кольцевого буфера, минут.
            change_eps: Минимальное изменение значения, считающееся «обновлением»
                (для `age`).
        """
        self._history = timedelta(minutes=history_minutes)
        self._eps = change_eps
        self._buf: deque[tuple[datetime, Telemetry, dict[str, float]]] = deque()
        self._last_change: dict[str, datetime] = {}
        self._last_value: dict[str, float] = {}
        self.ts_bad_streak = 0  # подряд пришедших снимков с повторной или откатившейся меткой времени
        self.ts_issue_total = 0  # всего таких снимков
        self.last_ts_issue: tuple[str, datetime, datetime] | None = None  # (вид, метка снимка, предыдущая метка)

    def update(self, telemetry: Telemetry) -> None:
        """Добавить снимок и вытеснить устаревшие.

        Args:
            telemetry: Новый снимок. Его `ts` должен быть не меньше предыдущего;
                снимок «из прошлого» игнорируется, чтобы не ломать наклоны.
        """
        last = self._buf[-1][0] if self._buf else None
        if last is not None and telemetry.ts <= last:
            # Метка не растёт: откат (rollback) или повтор (repeat). Такой снимок в историю не берём
            # (иначе наклоны и «возраст» значений станут бессмысленными), но факт запоминаем для детектора.
            self.ts_bad_streak += 1
            self.ts_issue_total += 1
            self.last_ts_issue = ("rollback" if telemetry.ts < last else "repeat", telemetry.ts, last)
            return
        self.ts_bad_streak = 0
        flat = telemetry.flat()
        for name, value in flat.items():
            prev = self._last_value.get(name)
            if prev is None or abs(value - prev) > self._eps:
                self._last_change[name] = telemetry.ts
            self._last_value[name] = value
        self._buf.append((telemetry.ts, telemetry, flat))
        cutoff = telemetry.ts - self._history
        while self._buf and self._buf[0][0] < cutoff:
            self._buf.popleft()

    def latest(self) -> Telemetry | None:
        """Вернуть последний снимок.

        Returns:
            Снимок или `None`, если данных ещё не было.
        """
        return self._buf[-1][1] if self._buf else None

    @property
    def now(self) -> datetime | None:
        """Время последнего снимка (виртуальное «сейчас» состояния)."""
        return self._buf[-1][0] if self._buf else None

    def fields(self) -> list[str]:
        """Список плоских имён полей, известных состоянию."""
        return list(self._last_value)

    def snapshots(self, minutes: float | None = None) -> list[Telemetry]:
        """Вернуть снимки за последние минуты.

        Args:
            minutes: Глубина; `None` — весь буфер.

        Returns:
            Снимки по возрастанию времени.
        """
        cutoff = self._cutoff(minutes)
        out: list[Telemetry] = []
        for ts, t, _ in reversed(self._buf):
            if cutoff is not None and ts < cutoff:
                break
            out.append(t)
        out.reverse()
        return out

    def series(self, field: str, minutes: float | None = None) -> list[tuple[datetime, float]]:
        """Вернуть ряд значений поля за последние минуты.

        Args:
            field: Плоское имя поля (`Telemetry.flat`).
            minutes: Глубина окна от последнего снимка; `None` — весь буфер.

        Returns:
            Пары `(время, значение)` по возрастанию времени; пропуски (поле
            отсутствовало в снимке) опускаются.
        """
        cutoff = self._cutoff(minutes)
        out: list[tuple[datetime, float]] = []
        for ts, _, flat in reversed(self._buf):  # с конца: окно обычно короче буфера
            if cutoff is not None and ts < cutoff:
                break
            if field in flat:
                out.append((ts, flat[field]))
        out.reverse()
        return out

    def slope(self, field: str, minutes: float) -> float | None:
        """Оценить наклон поля методом наименьших квадратов.

        Args:
            field: Плоское имя поля.
            minutes: Окно анализа, минут.

        Returns:
            Наклон в единицах поля за минуту или `None`, если данных меньше
            трёх точек или окно покрыто хуже чем наполовину (ранний старт даёт
            ложные тренды).
        """
        pts = self.series(field, minutes)
        if len(pts) < 3:
            return None
        t0 = pts[0][0]
        span_min = (pts[-1][0] - t0).total_seconds() / 60.0
        if span_min < minutes / 2.0:
            return None
        xs = [(ts - t0).total_seconds() / 60.0 for ts, _ in pts]
        ys = [v for _, v in pts]
        n = len(xs)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        denom = sum((x - mean_x) ** 2 for x in xs)
        if denom == 0:
            return None
        return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / denom

    def linfit(self, field: str, minutes: float) -> LinFit | None:
        """Линейная аппроксимация поля за окно с оценкой качества (R²).

        Args:
            field: Плоское имя поля.
            minutes: Окно анализа, минут.

        Returns:
            `LinFit` или `None`, если точек меньше трёх либо окно покрыто хуже чем наполовину.
        """
        pts = self.series(field, minutes)
        if len(pts) < 3:
            return None
        t0 = pts[0][0]
        xs = [(ts - t0).total_seconds() / 60.0 for ts, _ in pts]
        span_min = xs[-1]
        if span_min < minutes / 2.0:
            return None
        ys = [v for _, v in pts]
        n = len(xs)
        mean_x, mean_y = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mean_x) ** 2 for x in xs)
        if sxx == 0:
            return None
        sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
        slope = sxy / sxx
        syy = sum((y - mean_y) ** 2 for y in ys)
        r2 = 0.0 if syy == 0 else min(1.0, (sxy * sxy) / (sxx * syy))
        intercept = mean_y + slope * (xs[-1] - mean_x)
        return LinFit(slope, intercept, r2, n, span_min * 60.0, pts[0][0], pts[-1][0])

    def age(self, field: str) -> float | None:
        """Сколько секунд значение поля не менялось.

        Именно это используется детектором замерзания: у живого датчика
        значение «дышит» (шум), у зависшего — нет.

        Args:
            field: Плоское имя поля.

        Returns:
            Секунды от последнего изменения до последнего снимка или `None`,
            если поле неизвестно.
        """
        changed = self._last_change.get(field)
        if changed is None or not self._buf:
            return None
        return (self._buf[-1][0] - changed).total_seconds()

    def previous(self, field: str) -> float | None:
        """Значение поля в предпоследнем снимке (для детектора скачка).

        Args:
            field: Плоское имя поля.

        Returns:
            Значение или `None`, если нет двух снимков с этим полем.
        """
        seen = 0
        for _, _, flat in reversed(self._buf):
            if field in flat:
                seen += 1
                if seen == 2:
                    return flat[field]
        return None

    def previous_point(self, field: str) -> tuple[datetime, float] | None:
        """Предпоследняя точка поля `(время, значение)` (для оценки скорости изменения).

        Args:
            field: Плоское имя поля.

        Returns:
            Точка или `None`, если нет двух снимков с этим полем.
        """
        seen = 0
        for ts, _, flat in reversed(self._buf):
            if field in flat:
                seen += 1
                if seen == 2:
                    return ts, flat[field]
        return None

    def _cutoff(self, minutes: float | None) -> datetime | None:
        """Нижняя граница окна относительно последнего снимка."""
        if minutes is None or not self._buf:
            return None
        return self._buf[-1][0] - timedelta(minutes=minutes)
