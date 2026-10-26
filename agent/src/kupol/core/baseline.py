"""Калибровка на начальном отрезке: ТОЛЬКО величины, не зависящие от режима оборудования.

Когда Оператор работает с оборудованием (пайка, станок, принтер), растут потребление, температура
и CO2: «нормальные» абсолютные значения зависят от режима, поэтому по начальному отрезку их
калибровать нельзя. Безопасно калибруются лишь три вида статистики, которые от режима не зависят:

* **шум датчика** — стандартное отклонение высокочастотной составляющей сигнала после вычитания
  линейного тренда в скользящем подокне (свойство самого датчика, а не процесса);
* **разность пары датчиков** — медиана разности двух одноимённых датчиков (при работе оборудования
  оба сдвигаются вместе, разность остаётся стабильной);
* **шум невязки энергобаланса** — σ разности между ожидаемым и измеренным изменением заряда АКБ.

Запрещено хранить среднее, «нормальный уровень» и пороги абсолютных значений, z-score абсолютных
значений и ожидания по режимам. Структура `BaselineSnapshot` намеренно не содержит таких полей
(это проверяет тест). В базовую линию не попадают данные из периода тревоги.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Collection, Sequence
from datetime import datetime
from itertools import combinations
from statistics import median

from pydantic import BaseModel, Field

from kupol.core.config import AppConfig
from kupol.core.models import Telemetry
from kupol.core.state import State


BALANCE_FIELDS = frozenset({"energy_balance", "battery_soc_pct", "pv_power_w", "load_total_w"})  # невязка зависит от этих полей


class BaselineSnapshot(BaseModel):
    """Сохраняемое состояние калибровки (для журнала, `replay` и `report`).

    Attributes:
        fitted: Набрана ли калибровка.
        fitted_at: Когда набрана (время игры).
        noise: σ высокочастотной составляющей по полю телеметрии.
        pair_offset: Медиана разности пары `"a|b"` (a − b).
        balance_sigma: σ невязки энергобаланса, Вт.
        subwindows: Сколько подокон использовано для оценки шума по полю.
        residuals: Сколько окон невязки использовано.
        updates: Сколько раз оценки обновлены скользящим обновлением.
        skipped_in_alert: Сколько отсчётов не взято в калибровку из-за активной тревоги.
    """

    fitted: bool = False
    fitted_at: datetime | None = None
    noise: dict[str, float] = Field(default_factory=dict)
    pair_offset: dict[str, float] = Field(default_factory=dict)
    balance_sigma: float | None = None
    subwindows: dict[str, int] = Field(default_factory=dict)
    residuals: int = 0
    updates: int = 0
    skipped_in_alert: int = 0


def detrended_sigma(times: Sequence[float], values: Sequence[float]) -> float | None:
    """σ высокочастотной составляющей: остаток после вычитания линейного тренда.

    Args:
        times: Моменты времени, с.
        values: Значения.

    Returns:
        Оценка σ (с поправкой на две подогнанные степени свободы) либо `None`, если точек мало.
    """
    n = len(values)
    if n < 6:
        return None
    mx, my = sum(times) / n, sum(values) / n
    sxx = sum((x - mx) ** 2 for x in times)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(times, values, strict=True)) / sxx
    sse = sum((y - (my + slope * (x - mx))) ** 2 for x, y in zip(times, values, strict=True))
    return float((sse / (n - 2)) ** 0.5)


def pooled_noise(points: Sequence[tuple[float, float]], subwindow_s: float, min_subwindows: int) -> tuple[float, int] | None:
    """Шум сигнала: медиана σ по подокнам после вычитания тренда в каждом (устойчива к событиям).

    Args:
        points: Пары `(время с, значение)` по возрастанию времени.
        subwindow_s: Длина подокна, с.
        min_subwindows: Минимум пригодных подокон.

    Returns:
        `(σ, число подокон)` либо `None`.
    """
    if not points:
        return None
    sigmas: list[float] = []
    start = points[0][0]
    chunk: list[tuple[float, float]] = []
    for t, v in points:
        if t - start >= subwindow_s and chunk:
            _close_chunk(chunk, sigmas)
            chunk, start = [], t
        chunk.append((t, v))
    _close_chunk(chunk, sigmas)
    if len(sigmas) < min_subwindows:
        return None
    return float(median(sigmas)), len(sigmas)


def _close_chunk(chunk: list[tuple[float, float]], out: list[float]) -> None:
    """Добавить σ подокна, если в нём достаточно точек."""
    if len(chunk) >= 6:
        s = detrended_sigma([c[0] for c in chunk], [c[1] for c in chunk])
        if s is not None:
            out.append(s)


class Baseline:
    """Калибровка шума, разностей пар и шума невязки баланса."""

    def __init__(self, config: AppConfig) -> None:
        """Args:
        config: Конфигурация Штаба (раздел `baseline`, группы пар из `detectors.pair_diff`).
        """
        self._cfg = config.baseline
        self._groups = config.detectors.pair_diff.groups
        self._noise: dict[str, float] = {}
        self._offset: dict[str, float] = {}
        self._sigma_bal: float | None = None
        self._subwindows: dict[str, int] = {}
        self._residual_count = 0
        self._fitted_at: datetime | None = None
        self._updates = 0
        self._skipped = 0
        self._first_ts: datetime | None = None
        self._last_stored: datetime | None = None
        self._last_update: datetime | None = None
        self._raw: list[Telemetry] = []
        self._raw_residuals: list[float] = []
        self._fitted = False
        self._bal_floor = config.baseline.balance_sigma_floor_w

    # ------------------------------------------------------------------ чтение оценок

    @property
    def fitted(self) -> bool:
        """Набрана ли калибровка."""
        return self._fitted

    def noise_level(self, field: str) -> float | None:
        """σ шума датчика (после вычитания тренда).

        Args:
            field: Плоское имя поля.

        Returns:
            σ или `None`, если оценка не набрана (тогда детекторы используют пороги из конфига).
        """
        return self._noise.get(field)

    def pair_offset(self, a: str, b: str) -> float | None:
        """Типичное смещение датчика `a` относительно `b` (медиана разности a − b).

        Args:
            a: Поле первого датчика.
            b: Поле второго датчика.

        Returns:
            Смещение (знак учитывает порядок аргументов) или `None`.
        """
        if f"{a}|{b}" in self._offset:
            return self._offset[f"{a}|{b}"]
        if f"{b}|{a}" in self._offset:
            return -self._offset[f"{b}|{a}"]
        return None

    def balance_residual_sigma(self) -> float | None:
        """σ невязки энергобаланса, Вт (или `None`, если не набрана)."""
        return self._sigma_bal

    def snapshot(self) -> BaselineSnapshot:
        """Состояние калибровки для журнала."""
        return BaselineSnapshot(
            fitted=self._fitted,
            fitted_at=self._fitted_at,
            noise=dict(self._noise),
            pair_offset=dict(self._offset),
            balance_sigma=self._sigma_bal,
            subwindows=dict(self._subwindows),
            residuals=self._residual_count,
            updates=self._updates,
            skipped_in_alert=self._skipped,
        )

    # ------------------------------------------------------------------ набор

    def _noise_fields(self, flat: dict[str, float]) -> list[str]:
        """Поля снимка, для которых оценивается шум (шаблоны из конфига)."""
        return [f for f in flat if any(fnmatch.fnmatchcase(f, p) for p in self._cfg.noise_fields)]

    def _pairs(self) -> list[tuple[str, str]]:
        """Все пары внутри групп."""
        return [p for g in self._groups for p in combinations(g, 2)]

    def fit(self, history: Sequence[Telemetry], residuals: Sequence[float] = ()) -> None:
        """Оценить шум, разности пар и σ невязки по набранному отрезку.

        Тренд абсолютных значений (Оператор уже начал работу) не искажает оценку: шум считается
        после вычитания тренда в подокнах, смещение пары — медианой.

        Args:
            history: Снимки начального отрезка (без периодов тревоги).
            residuals: Невязки энергобаланса за тот же отрезок, Вт.
        """
        if not history:
            return
        t0 = history[0].ts
        flats = [(t.ts, t.flat()) for t in history]
        for field in self._noise_fields(flats[-1][1]):
            pts = [((ts - t0).total_seconds(), f[field]) for ts, f in flats if field in f]
            est = pooled_noise(pts, self._cfg.subwindow_s, self._cfg.min_subwindows)
            if est is not None:
                self._noise[field], self._subwindows[field] = est
        for a, b in self._pairs():
            diffs = [f[a] - f[b] for _, f in flats if a in f and b in f]
            if len(diffs) >= self._cfg.min_subwindows * 6:
                self._offset[f"{a}|{b}"] = float(median(diffs))
        if len(residuals) >= self._cfg.min_residuals:
            mean = sum(residuals) / len(residuals)
            var = sum((r - mean) ** 2 for r in residuals) / (len(residuals) - 1)
            self._sigma_bal = max(var**0.5 * self._cfg.sigma_safety, self._bal_floor)
            self._residual_count = len(residuals)
        self._fitted = bool(self._noise or self._offset or self._sigma_bal)
        if self._fitted:
            self._fitted_at = history[-1].ts
            self._last_update = history[-1].ts

    def observe(self, state: State, blocked: Collection[str] = (), residual: float | None = None) -> bool:
        """Принять очередной такт: набор начального отрезка, затем медленное обновление.

        Данные из периода тревоги в калибровку не попадают: пока набирается начальный отрезок, такт
        пропускается целиком, если есть активная тревога; при скользящем обновлении блокируются только
        поля, к которым тревога относится (и пары с ними).

        Args:
            state: Состояние.
            blocked: Поля с активной тревогой (warning и выше, кроме прогнозов); `"*"` — тревога без поля.
            residual: Новая невязка энергобаланса, Вт (если закончилось очередное окно).

        Returns:
            `True`, если оценки изменились (набраны или обновлены): пора записать их в журнал.
        """
        latest = state.latest()
        if latest is None or not latest.comms_ok:
            return False
        if self._first_ts is None:
            self._first_ts = latest.ts
        alert_active = bool(blocked)
        fresh = self._last_stored is None or latest.ts > self._last_stored
        if not self._fitted:
            if fresh:
                if alert_active:
                    self._skipped += 1
                else:
                    self._raw.append(latest)
                self._last_stored = latest.ts
            if residual is not None and not alert_active:
                self._raw_residuals.append(residual)
            if (latest.ts - self._first_ts).total_seconds() >= self._cfg.window_s:
                self.fit(self._raw, self._raw_residuals)
                self._raw, self._raw_residuals = [], []
                if not self._fitted:  # данных не хватило: продолжаем собирать ещё окно
                    self._first_ts = latest.ts
                return self._fitted
            return False
        changed = False
        if residual is not None and self._sigma_bal is not None:
            if alert_active and BALANCE_FIELDS & set(blocked) or "*" in blocked:
                self._skipped += 1
            else:
                changed |= self._update_balance_sigma(abs(residual))
        if fresh:
            self._last_stored = latest.ts
        if self._last_update is not None and (latest.ts - self._last_update).total_seconds() >= self._cfg.subwindow_s:
            if "*" in blocked:
                self._skipped += 1
            else:
                changed |= self._slow_update(state, set(blocked))
            self._last_update = latest.ts
        return changed

    # ------------------------------------------------------------------ скользящее обновление

    def _update_balance_sigma(self, abs_residual: float) -> bool:
        """Медленное обновление σ невязки (выбросы отбрасываются)."""
        assert self._sigma_bal is not None
        if abs_residual > self._cfg.outlier_ratio * self._sigma_bal:
            return False
        rate = self._cfg.update_rate
        new = ((1 - rate) * self._sigma_bal**2 + rate * abs_residual**2) ** 0.5
        self._sigma_bal = max(new, self._bal_floor)
        self._updates += 1
        return True

    def _slow_update(self, state: State, blocked: set[str]) -> bool:
        """Скользящее обновление шума и смещения пар по последнему подокну."""
        snaps = state.snapshots(self._cfg.subwindow_s / 60.0)
        if len(snaps) < 6:
            return False
        t0 = snaps[0].ts
        flats = [(t.ts, t.flat()) for t in snaps]
        rate = self._cfg.update_rate
        changed = False
        for field, old in list(self._noise.items()):
            if field in blocked:
                continue  # по полю с тревогой калибровку не трогаем
            pts = [((ts - t0).total_seconds(), f[field]) for ts, f in flats if field in f]
            sigma = detrended_sigma([p[0] for p in pts], [p[1] for p in pts])
            if sigma is None or sigma <= 0:
                continue
            ratio = self._cfg.outlier_ratio
            if sigma > ratio * old or sigma < old / ratio:
                continue  # шум пропал или вырос скачком: это аномалия, а не новая норма
            self._noise[field] = old + rate * (sigma - old)
            changed = True
        for key, old_off in list(self._offset.items()):
            a, b = key.split("|")
            if a in blocked or b in blocked:
                continue
            diffs = [f[a] - f[b] for _, f in flats if a in f and b in f]
            if len(diffs) < 6:
                continue
            new_off = float(median(diffs))
            sa, sb = self._noise.get(a), self._noise.get(b)
            if sa is not None and sb is not None and abs(new_off - old_off) > self._cfg.outlier_ratio * (sa**2 + sb**2) ** 0.5:
                continue  # резкое смещение пары — тоже не «норма»
            self._offset[key] = old_off + rate * (new_off - old_off)
            changed = True
        if changed:
            self._updates += 1
        return changed


class ResidualSeries:
    """Невязка энергобаланса по непересекающимся окнам.

    Для каждого окна сравнивается измеренное изменение заряда АКБ (по средним первых и последних
    отсчётов SoC, чтобы шум датчика не раздувал оценку) с интегралом ожидаемой мощности
    `P_pv − P_load` с учётом КПД заряда/разряда. Отрицательная невязка — АКБ теряет больше, чем
    объясняет телеметрия (скрытая нагрузка или врущий датчик).
    """

    EDGE_POINTS = 5  # сколько отсчётов усредняется на краях окна

    def __init__(self, config: AppConfig) -> None:
        """Args:
        config: Конфигурация (окно невязки, ёмкость АКБ, КПД).
        """
        self._window_s = config.baseline.residual_window_s
        self._cap_wh = config.energy.battery_capacity_wh
        self._eta_c = config.energy.charge_efficiency
        self._eta_d = config.energy.discharge_efficiency
        self._bias_w = config.energy.balance_bias_w
        self._start: datetime | None = None
        self.samples: list[tuple[datetime, float]] = []

    def update(self, state: State) -> float | None:
        """Завершить окно, если оно набралось.

        Args:
            state: Состояние.

        Returns:
            Невязка, Вт, если окно только что закончилось и пригодно; иначе `None`.
        """
        latest = state.latest()
        if latest is None or not latest.comms_ok:
            self._start = None
            return None
        if self._start is None:
            self._start = latest.ts
            return None
        if (latest.ts - self._start).total_seconds() < self._window_s:
            return None
        start, self._start = self._start, latest.ts
        snaps = [s for s in state.snapshots((latest.ts - start).total_seconds() / 60.0 + 1e-6) if s.ts >= start]
        return self._residual(snaps, latest)

    def _residual(self, snaps: list[Telemetry], latest: Telemetry) -> float | None:
        """Невязка по снимкам окна (None — окно непригодно: связь, упор АКБ в 0/100 %, мало данных)."""
        if len(snaps) < 2 * self.EDGE_POINTS or any(not s.comms_ok for s in snaps):
            return None
        span = (snaps[-1].ts - snaps[0].ts).total_seconds()
        if span <= 0 or span > 3 * self._window_s:
            return None
        expected_wh = 0.0
        for a, b in zip(snaps, snaps[1:], strict=False):
            net = a.pv_power_w - a.load_total_w
            expected_wh += (net * self._eta_c if net > 0 else net / self._eta_d) * (b.ts - a.ts).total_seconds() / 3600.0
        soc0 = sum(s.battery_soc_pct for s in snaps[: self.EDGE_POINTS]) / self.EDGE_POINTS
        soc1 = sum(s.battery_soc_pct for s in snaps[-self.EDGE_POINTS :]) / self.EDGE_POINTS
        if (soc1 >= 99.0 and expected_wh > 0) or (soc1 <= 1.0 and expected_wh < 0):
            return None  # АКБ упёрлась в предел: изменение заряда уже не отражает баланс
        centers = (snaps[-1].ts - snaps[0].ts).total_seconds() - (
            (snaps[self.EDGE_POINTS - 1].ts - snaps[0].ts).total_seconds() / 2
            + (snaps[-1].ts - snaps[-self.EDGE_POINTS].ts).total_seconds() / 2
        )
        hours = max(centers, 1.0) / 3600.0
        measured_wh = (soc1 - soc0) / 100.0 * self._cap_wh
        # Ожидаемое считается по всему окну, измеренное — между центрами краёв: приводим к одному интервалу.
        expected_between = expected_wh * (hours * 3600.0 / span)
        r = (measured_wh - expected_between) / hours - self._bias_w
        self.samples.append((latest.ts, r))
        return float(r)
