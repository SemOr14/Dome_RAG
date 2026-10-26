"""Детекторы аномалий (без LLM, чистый код).

Каждый детектор читает ``State`` и возвращает список ``Alert``. Пороги задаются
по полям в ``config.yaml`` (раздел ``thresholds``):

* порог (warning/critical, раздельно);
* тренд — наклон по скользящему окну (ловит скрытый дрейф);
* скачок — изменение между соседними снимками (ловит ложные и прямые события);
* замерзание — значение не менялось дольше N секунд;
* устаревание — данные не обновлялись дольше N секунд (и потеря связи).

Расширенные детекторы (дополнение к ТЗ), все без LLM:

* ``CusumDetector`` — CUSUM по невязкам: разность пар датчиков за вычетом `pair_offset` и невязка
  энергобаланса, допуск и порог в сигмах baseline (к абсолютным значениям не применяется);
* ``TimeToThresholdDetector`` — время до критического порога по линейному тренду (прогноз: реакция
  не выше ``prepare``);
* ``PairDiffDetector`` — разность пар: коридор `pair_offset ± k·σ`, дрейф разности, жёсткий предел;
* ``NoiseLossDetector`` — потеря шума (только падение; рост шума аномалией не считается);
* ``FlatlineDetector`` — залипание: размах значений в долях `noise_level` за `freeze_s`;
* ``TimeMonotonicDetector`` — метка времени откатилась, повторилась или не идёт;
* ``RateLimitDetector`` — скачок быстрее физического предела (`max_rate_per_s`).

Принцип: оценки baseline используются только для величин, не зависящих от режима (разность пар,
невязка баланса, шум); к абсолютным значениям, зависящим от режима, применяются лишь пороги и
физические пределы из конфига. Пока baseline не набран, детекторы опираются на жёсткие пределы.

Дополнительно ``vent_required`` — жёсткое правило безопасности (пайка требует
вытяжки, регламент п. 9.1): оно в коде, а не у модели.

``DetectorSuite`` запускает всё вместе; при отсутствии достоверных данных
(потеря связи, устаревание) остальные детекторы молчат, чтобы не плодить
ложные тревоги по застывшим значениям. ``AlertTracker`` убирает повторы.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime
from itertools import combinations
from typing import TYPE_CHECKING, Any

from kupol.core.config import AppConfig, DetectorsConfig, FieldThreshold
from kupol.core.models import Alert, AlertLevel, DeviceStatus
from kupol.core.state import State

if TYPE_CHECKING:
    from kupol.core.baseline import Baseline


def _fmt(x: float) -> str:
    """Краткое представление числа в сообщениях."""
    return f"{x:.1f}" if abs(x) < 1000 else f"{x:.0f}"


class ThresholdDetector:
    """Пороговый детектор: значение выше/ниже предела."""

    def __init__(self, config: AppConfig) -> None:
        """Args:
        config: Конфигурация с порогами по полям.
        """
        self._cfg = config

    def detect(self, state: State, now: datetime) -> list[Alert]:
        """Найти нарушения порогов в последнем снимке.

        Args:
            state: Состояние.
            now: Текущее время.

        Returns:
            Тревоги: по одной на поле, берётся наиболее тяжёлый нарушенный порог.
        """
        latest = state.latest()
        if latest is None:
            return []
        alerts: list[Alert] = []
        for field, value in latest.flat().items():
            th = self._cfg.thresholds_for(field)
            if th is None:
                continue
            hit = self._evaluate(value, th)
            if hit is None:
                continue
            level, kind, bound = hit
            word = "выше" if kind.endswith("high") else "ниже"
            alerts.append(
                Alert(
                    ts=now,
                    level=level,
                    kind="threshold",
                    field=field,
                    message=f"{field}={_fmt(value)} {word} порога {kind.split('_')[0]} ({_fmt(bound)})",
                    value=value,
                    details={"bound": kind, "limit": bound},
                )
            )
        return alerts

    @staticmethod
    def _evaluate(value: float, th: FieldThreshold) -> tuple[AlertLevel, str, float] | None:
        """Вернуть (уровень, имя порога, порог) для самого тяжёлого нарушения."""
        if th.crit_high is not None and value >= th.crit_high:
            return AlertLevel.CRITICAL, "crit_high", th.crit_high
        if th.crit_low is not None and value <= th.crit_low:
            return AlertLevel.CRITICAL, "crit_low", th.crit_low
        if th.warn_high is not None and value >= th.warn_high:
            return AlertLevel.WARNING, "warn_high", th.warn_high
        if th.warn_low is not None and value <= th.warn_low:
            return AlertLevel.WARNING, "warn_low", th.warn_low
        return None


class TrendDetector:
    """Детектор тренда: наклон по скользящему окну превышает допустимый."""

    def __init__(self, config: AppConfig) -> None:
        """Args:
        config: Конфигурация с окнами и допустимыми наклонами.
        """
        self._cfg = config

    def detect(self, state: State, now: datetime) -> list[Alert]:
        """Найти поля с подозрительным трендом.

        Args:
            state: Состояние.
            now: Текущее время.

        Returns:
            Тревоги ``warning`` с наклоном в ``details``.
        """
        alerts: list[Alert] = []
        for field in state.fields():
            th = self._cfg.thresholds_for(field)
            if th is None or th.trend_window_min is None or th.max_slope_per_min is None:
                continue
            slope = state.slope(field, th.trend_window_min)
            if slope is None or abs(slope) <= th.max_slope_per_min:
                continue
            alerts.append(
                Alert(
                    ts=now,
                    level=AlertLevel.WARNING,
                    kind="trend",
                    field=field,
                    message=(
                        f"{field}: тренд {slope:+.2f}/мин за {th.trend_window_min:g} мин "
                        f"(допустимо ±{th.max_slope_per_min:g})"
                    ),
                    value=slope,
                    details={"slope_per_min": slope, "window_min": th.trend_window_min, "limit": th.max_slope_per_min},
                )
            )
        return alerts


class JumpDetector:
    """Детектор скачка: изменение между соседними снимками больше допустимого."""

    def __init__(self, config: AppConfig) -> None:
        """Args:
        config: Конфигурация с ``max_jump`` / ``max_jump_rel``.
        """
        self._cfg = config

    def detect(self, state: State, now: datetime) -> list[Alert]:
        """Найти резкие изменения последнего снимка относительно предыдущего.

        Args:
            state: Состояние.
            now: Текущее время.

        Returns:
            Тревоги ``warning`` (критичность решает сверка источников).
        """
        latest = state.latest()
        if latest is None:
            return []
        alerts: list[Alert] = []
        for field, value in latest.flat().items():
            th = self._cfg.thresholds_for(field)
            if th is None or (th.max_jump is None and th.max_jump_rel is None):
                continue
            prev = state.previous(field)
            if prev is None:
                continue
            delta = value - prev
            abs_hit = th.max_jump is not None and abs(delta) > th.max_jump
            rel_hit = (
                th.max_jump_rel is not None
                and abs(prev) >= th.rel_floor
                and abs(delta) / abs(prev) > th.max_jump_rel
            )
            if not (abs_hit or rel_hit):
                continue
            alerts.append(
                Alert(
                    ts=now,
                    level=AlertLevel.WARNING,
                    kind="jump",
                    field=field,
                    message=f"{field}: скачок {_fmt(prev)} → {_fmt(value)} за один такт",
                    value=value,
                    details={"previous": prev, "delta": delta},
                )
            )
        return alerts


class FreezeDetector:
    """Детектор «замерзания»: значение не менялось дольше N секунд."""

    def __init__(self, config: AppConfig) -> None:
        """Args:
        config: Конфигурация с ``freeze_s`` по полям.
        """
        self._cfg = config

    def detect(self, state: State, now: datetime) -> list[Alert]:
        """Найти поля, которые должны меняться, но застыли.

        Args:
            state: Состояние.
            now: Текущее время.

        Returns:
            Тревоги ``warning``.
        """
        latest = state.latest()
        if latest is None:
            return []
        alerts: list[Alert] = []
        for field, value in latest.flat().items():
            th = self._cfg.thresholds_for(field)
            if th is None or th.freeze_s is None:
                continue
            if th.freeze_ignore_ge is not None and value >= th.freeze_ignore_ge:
                continue
            age = state.age(field)
            if age is None or age <= th.freeze_s:
                continue
            alerts.append(
                Alert(
                    ts=now,
                    level=AlertLevel.WARNING,
                    kind="freeze",
                    field=field,
                    message=f"{field}={_fmt(value)} не меняется {age:.0f} с (допустимо {th.freeze_s:g} с)",
                    value=value,
                    details={"unchanged_s": age, "limit_s": th.freeze_s},
                )
            )
        return alerts


class StaleDetector:
    """Детектор устаревания данных и потери связи."""

    def __init__(self, config: DetectorsConfig) -> None:
        """Args:
        config: Раздел ``detectors`` конфигурации.
        """
        self._cfg = config

    @staticmethod
    def staleness(state: State, now: datetime) -> float:
        """Возраст данных, с: максимум из заявленного ``data_age_s`` и давности последнего снимка.

        Args:
            state: Состояние.
            now: Текущее время.

        Returns:
            Секунды; ``inf``, если снимков ещё не было.
        """
        latest = state.latest()
        if latest is None:
            return float("inf")
        return max(latest.data_age_s, (now - latest.ts).total_seconds(), 0.0)

    def detect(self, state: State, now: datetime) -> list[Alert]:
        """Проверить свежесть данных и признак связи.

        Args:
            state: Состояние.
            now: Текущее время.

        Returns:
            Тревога ``comms_loss`` (нет связи) или ``stale`` (данные старые), либо пусто.
        """
        latest = state.latest()
        if latest is None:
            return []
        age = self.staleness(state, now)
        if not latest.comms_ok:
            level = AlertLevel.CRITICAL if age >= self._cfg.comms_loss_s else AlertLevel.WARNING
            return [
                Alert(
                    ts=now,
                    level=level,
                    kind="comms_loss",
                    message=f"Потеряна связь с Комплексом, данные застыли {age:.0f} с — решения по ним принимать нельзя",
                    value=age,
                    details={"age_s": age},
                )
            ]
        if age >= self._cfg.stale_crit_s:
            level = AlertLevel.CRITICAL
        elif age >= self._cfg.stale_warn_s:
            level = AlertLevel.WARNING
        else:
            return []
        return [
            Alert(
                ts=now,
                level=level,
                kind="stale",
                message=f"Данные телеметрии устарели на {age:.0f} с",
                value=age,
                details={"age_s": age},
            )
        ]


def field_key(field: str) -> str:
    """Ключ группы поля для таблиц конфига: `co2_ppm.co2_a` → `co2_ppm`, `temp_c` → `temp_c`."""
    return field.split(".", 1)[0]


def lookup_by_field(table: dict[str, float], field: str) -> float | None:
    """Значение из таблицы конфига по точному имени поля, затем по ключу группы."""
    if field in table:
        return table[field]
    return table.get(field_key(field))


def _window(state: State, field: str, minutes: float) -> dict[str, Any]:
    """Ссылка на окно данных для хранения в тревоге."""
    pts = state.series(field, minutes)
    if not pts:
        return {}
    return {"start": pts[0][0].isoformat(), "end": pts[-1][0].isoformat(), "n": len(pts)}


class RateLimitDetector:
    """Скачок быстрее физического предела скорости: проблема датчика или канала, а не среды."""

    def __init__(self, config: AppConfig, baseline: Baseline | None = None) -> None:
        """Args:
        config: Конфигурация (`detectors.max_rate_per_s`, `detectors.rate_noise_k`).
        baseline: Калибровка шума: к пределу добавляется допуск на шум двух соседних отсчётов.
        """
        self._limits = config.detectors.max_rate_per_s
        self._noise_k = config.detectors.rate_noise_k
        self._baseline = baseline

    def detect(self, state: State, now: datetime) -> list[Alert]:
        """Найти поля, изменившиеся быстрее физически возможного.

        Args:
            state: Состояние.
            now: Текущее время.

        Returns:
            Тревоги `rate_limit` (warning) с подсказкой «вероятна проблема датчика/канала».
        """
        latest = state.latest()
        if latest is None:
            return []
        alerts: list[Alert] = []
        for field, value in latest.flat().items():
            limit = lookup_by_field(self._limits, field)
            if limit is None:
                continue
            prev = state.previous_point(field)
            if prev is None:
                continue
            dt = (latest.ts - prev[0]).total_seconds()
            if dt <= 0:
                continue
            sigma = self._baseline.noise_level(field) if self._baseline else None
            margin = self._noise_k * math.sqrt(2.0) * sigma if sigma else 0.0
            rate = abs(value - prev[1]) / dt
            if abs(value - prev[1]) <= limit * dt + margin:
                continue
            alerts.append(
                Alert(
                    ts=now,
                    level=AlertLevel.WARNING,
                    kind="rate_limit",
                    field=field,
                    message=(
                        f"{field}: изменение {_fmt(prev[1])} → {_fmt(value)} со скоростью {rate:.3g}/с, быстрее "
                        f"физического предела {limit:g}/с — вероятна проблема датчика или канала, а не среды"
                    ),
                    value=value,
                    detector="rate_limit",
                    expected=prev[1],
                    confidence=min(1.0, rate / (2 * limit)),
                    window=_window(state, field, 1.0),
                    details={"rate_per_s": rate, "limit_per_s": limit, "hypothesis": "sensor_or_channel"},
                )
            )
        return alerts


class TimeMonotonicDetector:
    """Монотонность и свежесть времени: метка откатилась, повторилась или не идёт."""

    def __init__(self, config: AppConfig) -> None:
        """Args:
        config: Конфигурация (`detectors.ts_bad_ticks`).
        """
        self._bad_ticks = config.detectors.ts_bad_ticks

    def detect(self, state: State, now: datetime) -> list[Alert]:
        """Проверить, не нарушена ли монотонность меток времени.

        Args:
            state: Состояние (счётчики нарушений ведёт `State.update`).
            now: Текущее время.

        Returns:
            Тревога `time_monotonic` (critical, если нарушение затянулось втрое) либо пусто.
        """
        issue = state.last_ts_issue
        if state.ts_bad_streak < self._bad_ticks or issue is None:
            return []
        kind, bad_ts, prev_ts = issue
        level = AlertLevel.CRITICAL if state.ts_bad_streak >= 3 * self._bad_ticks else AlertLevel.WARNING
        what = "откатилась назад" if kind == "rollback" else "не меняется"
        return [
            Alert(
                ts=now,
                level=level,
                kind="time_monotonic",
                field="ts",
                message=(
                    f"Метка времени телеметрии {what} ({state.ts_bad_streak} снимков подряд): данные могут быть "
                    "устаревшими, хотя значения обновляются"
                ),
                value=float(state.ts_bad_streak),
                detector="time_monotonic",
                confidence=min(1.0, state.ts_bad_streak / (3.0 * self._bad_ticks)),
                details={"issue": kind, "bad_ts": bad_ts.isoformat(), "previous_ts": prev_ts.isoformat()},
            )
        ]


class TimeToThresholdDetector:
    """Время до критического порога по линейному тренду (прогноз для Диагноста)."""

    def __init__(self, config: AppConfig) -> None:
        """Args:
        config: Конфигурация (`detectors.time_to_threshold`, пороги по полям).
        """
        self._cfg = config
        self._tt = config.detectors.time_to_threshold

    def detect(self, state: State, now: datetime) -> list[Alert]:
        """Найти поля, у которых критический порог будет достигнут в пределах горизонта.

        Рост при работе оборудования может быть нормальным, поэтому тревога не выше `warning` и
        помечена `max_response=prepare`: это прогноз, а не доказательство аномалии.

        Args:
            state: Состояние.
            now: Текущее время.

        Returns:
            Тревоги `time_to_threshold` с оценкой времени в `details['eta_s']`.
        """
        latest = state.latest()
        if latest is None:
            return []
        alerts: list[Alert] = []
        for field, value in latest.flat().items():
            th = self._cfg.thresholds_for(field)
            if th is None or (th.crit_high is None and th.crit_low is None):
                continue
            fit = state.linfit(field, self._tt.window_min)
            if fit is None or fit.n < self._tt.min_points or fit.r2 < self._tt.min_r2 or fit.slope_per_min == 0:
                continue
            slope_s = fit.slope_per_min / 60.0
            target: float | None = None
            if slope_s > 0 and th.crit_high is not None and value < th.crit_high:
                target = th.crit_high
            elif slope_s < 0 and th.crit_low is not None and value > th.crit_low:
                target = th.crit_low
            if target is None:
                continue
            eta = (target - fit.intercept) / slope_s
            if not 0 < eta <= self._tt.horizon_s:
                continue
            alerts.append(
                Alert(
                    ts=now,
                    level=AlertLevel.WARNING,
                    kind="time_to_threshold",
                    field=field,
                    message=(
                        f"{field}: при текущем тренде {fit.slope_per_min:+.3g}/мин критический порог {target:g} "
                        f"будет достигнут через ≈{eta / 60:.0f} мин (R²={fit.r2:.2f})"
                    ),
                    value=value,
                    detector="time_to_threshold",
                    expected=target,
                    confidence=fit.r2,
                    window={"start": fit.start.isoformat(), "end": fit.end.isoformat(), "n": fit.n},
                    details={"eta_s": eta, "slope_per_min": fit.slope_per_min, "r2": fit.r2, "max_response": "prepare"},
                )
            )
        return alerts


def pair_label(a: str, b: str) -> str:
    """Подпись пары для тревог и CUSUM."""
    return f"{a}|{b}"


class PairDiffDetector:
    """Разность пар одноимённых датчиков (CO2, температура)."""

    def __init__(self, config: AppConfig, baseline: Baseline | None = None) -> None:
        """Args:
        config: Конфигурация (`detectors.pair_diff`).
        baseline: Калибровка (шум и смещение пар); без неё действуют только жёсткие пределы.
        """
        self._cfg = config.detectors.pair_diff
        self._baseline = baseline
        self._streak: dict[str, int] = {}

    def _sigma(self, a: str, b: str) -> float | None:
        """σ разности пары из шума её датчиков (если оценки есть)."""
        if self._baseline is None:
            return None
        sa, sb = self._baseline.noise_level(a), self._baseline.noise_level(b)
        if sa is None or sb is None:
            return None
        return float(math.hypot(sa, sb))

    def detect(self, state: State, now: datetime) -> list[Alert]:
        """Найти пары, вышедшие из коридора, дрейфующие или превысившие жёсткий предел.

        Args:
            state: Состояние.
            now: Текущее время.

        Returns:
            Тревоги `pair_diff` (warning); при группе из трёх и более датчиков указан подозреваемый.
        """
        latest = state.latest()
        if latest is None:
            return []
        flat = latest.flat()
        alerts: list[Alert] = []
        for group in self._cfg.groups:
            present = [f for f in group if f in flat]
            if len(present) < 2:
                continue
            key = field_key(present[0])
            flagged: dict[tuple[str, str], list[str]] = {}
            info: dict[tuple[str, str], dict[str, float]] = {}
            for a, b in combinations(present, 2):
                reasons, data = self._check_pair(state, flat, a, b, key)
                label = pair_label(a, b)
                self._streak[label] = self._streak.get(label, 0) + 1 if reasons else 0
                if reasons and self._streak[label] >= self._cfg.confirm_ticks:
                    flagged[(a, b)] = reasons
                    info[(a, b)] = data
            if not flagged:
                continue
            suspect = self._suspect(present, flagged)
            pair = next(iter(flagged))
            reasons = flagged[pair]
            data = info[pair]
            who = suspect or pair_label(*pair)
            alerts.append(
                Alert(
                    ts=now,
                    level=AlertLevel.WARNING,
                    kind="pair_diff",
                    field=who,
                    message=(
                        f"Расхождение пары {pair[0]} и {pair[1]}: разность {data['diff']:+.3g} при типичной {data['offset']:+.3g} "
                        f"({', '.join(reasons)})" + (f"; подозрение на {suspect}" if suspect else "")
                    ),
                    value=data["diff"],
                    detector="pair_diff",
                    expected=data["offset"],
                    confidence=0.9 if suspect else 0.6,
                    window=_window(state, pair[0], self._cfg.slope_window_min),
                    details={"pair": list(pair), "reasons": reasons, "suspect": suspect, **data},
                )
            )
        return alerts

    def _check_pair(
        self, state: State, flat: dict[str, float], a: str, b: str, key: str
    ) -> tuple[list[str], dict[str, float]]:
        """Причины нарушения для пары и числа для тревоги."""
        offset = self._baseline.pair_offset(a, b) if self._baseline else None
        base = offset if offset is not None else 0.0
        diff = flat[a] - flat[b]
        data = {"diff": diff, "offset": base}
        reasons: list[str] = []
        max_abs = self._cfg.max_abs_diff.get(key)
        if max_abs is not None and abs(diff - base) > max_abs:
            reasons.append(f"жёсткий предел ±{max_abs:g}")
        sigma = self._sigma(a, b)
        if sigma is not None and offset is not None:
            data["sigma"] = sigma
            if abs(diff - offset) > self._cfg.corridor_k * sigma:
                reasons.append(f"вне коридора ±{self._cfg.corridor_k:g}σ")
        max_slope = self._cfg.max_diff_slope_per_min.get(key)
        if max_slope is not None:
            slope = self._diff_slope(state, a, b)
            if slope is not None:
                data["slope_per_min"] = slope
                if abs(slope) > max_slope:
                    reasons.append(f"дрейф разности {slope:+.3g}/мин")
        return reasons, data

    def _diff_slope(self, state: State, a: str, b: str) -> float | None:
        """Наклон разности пары за окно (ед./мин) или `None`, если данных мало."""
        minutes = self._cfg.slope_window_min
        snaps = state.snapshots(minutes)
        if len(snaps) < 10:
            return None
        t0 = snaps[0].ts
        pts = [((s.ts - t0).total_seconds() / 60.0, f[a] - f[b]) for s in snaps if a in (f := s.flat()) and b in f]
        if len(pts) < 10 or pts[-1][0] < minutes / 2.0:
            return None
        n = len(pts)
        mx, my = sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n
        sxx = sum((p[0] - mx) ** 2 for p in pts)
        return None if sxx == 0 else sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx

    @staticmethod
    def _suspect(present: list[str], flagged: dict[tuple[str, str], list[str]]) -> str | None:
        """Подозреваемый датчик: тот, кто входит во все нарушенные пары (при группе из трёх и более)."""
        if len(present) < 3:
            return None
        counts = {f: sum(1 for p in flagged if f in p) for f in present}
        top = max(counts, key=lambda f: counts[f])
        return top if counts[top] == len(present) - 1 and sum(1 for c in counts.values() if c == counts[top]) == 1 else None


class NoiseLossDetector:
    """Потеря шума: σ высокочастотной составляющей упала ниже доли baseline-шума."""

    def __init__(self, config: AppConfig, baseline: Baseline | None = None) -> None:
        """Args:
        config: Конфигурация (`detectors.noise_loss`, `detectors.freeze_flat_ratio`).
        baseline: Калибровка шума; без неё детектор молчит.
        """
        self._cfg = config.detectors.noise_loss
        self._flat_ratio = config.detectors.freeze_flat_ratio
        self._baseline = baseline

    def detect(self, state: State, now: datetime) -> list[Alert]:
        """Найти поля, шум которых пропал, хотя величина физически не может быть абсолютно постоянной.

        Полностью постоянное значение — это залипание (`FlatlineDetector`), здесь оно не считается.
        Рост шума аномалией не считается: он может зависеть от режима.

        Args:
            state: Состояние.
            now: Текущее время.

        Returns:
            Тревоги `noise_loss` (warning).
        """
        from kupol.core.baseline import detrended_sigma

        if self._baseline is None or not self._baseline.fitted:
            return []
        minutes = self._cfg.window_s / 60.0
        alerts: list[Alert] = []
        for field in self._baseline.snapshot().noise:
            pts = state.series(field, minutes)
            if len(pts) < 30 or (pts[-1][0] - pts[0][0]).total_seconds() < 0.9 * self._cfg.window_s:
                continue
            base = self._baseline.noise_level(field)
            if base is None or base <= 0:
                continue
            values = [v for _, v in pts]
            if max(values) - min(values) <= self._flat_ratio * base:
                continue  # залипание, не потеря шума
            t0 = pts[0][0]
            sigma = detrended_sigma([(t - t0).total_seconds() for t, _ in pts], values)
            if sigma is None or sigma >= self._cfg.min_noise_ratio * base:
                continue
            alerts.append(
                Alert(
                    ts=now,
                    level=AlertLevel.WARNING,
                    kind="noise_loss",
                    field=field,
                    message=(
                        f"{field}: шум упал до {sigma:.3g} при обычном {base:.3g} (менее {self._cfg.min_noise_ratio:.0%}), "
                        "хотя величина физически шумит — вероятна неисправность датчика"
                    ),
                    value=sigma,
                    detector="noise_loss",
                    expected=base,
                    confidence=min(1.0, 1.0 - sigma / base),
                    window=_window(state, field, minutes),
                    details={"sigma": sigma, "baseline_sigma": base},
                )
            )
        return alerts


class FlatlineDetector:
    """Залипание: значение не меняется (в пределах доли `noise_level`) дольше `freeze_s`."""

    def __init__(self, config: AppConfig, baseline: Baseline | None = None) -> None:
        """Args:
        config: Конфигурация (`detectors.freeze_s`, `detectors.freeze_flat_ratio`).
        baseline: Калибровка шума (без неё «не меняется» означает точное равенство).
        """
        self._freeze = config.detectors.freeze_s
        self._ratio = config.detectors.freeze_flat_ratio
        self._baseline = baseline

    def detect(self, state: State, now: datetime) -> list[Alert]:
        """Найти залипшие поля.

        Args:
            state: Состояние.
            now: Текущее время.

        Returns:
            Тревоги `freeze` (warning), отдельно по каждому полю.
        """
        latest = state.latest()
        if latest is None:
            return []
        alerts: list[Alert] = []
        for field in latest.flat():
            limit = lookup_by_field(self._freeze, field)
            if limit is None:
                continue
            pts = state.series(field, limit / 60.0)
            if len(pts) < 3 or (pts[-1][0] - pts[0][0]).total_seconds() < 0.95 * limit:
                continue
            values = [v for _, v in pts]
            spread = max(values) - min(values)
            sigma = self._baseline.noise_level(field) if self._baseline else None
            flat_limit = self._ratio * sigma if sigma is not None else 0.0
            if spread > flat_limit:
                continue
            alerts.append(
                Alert(
                    ts=now,
                    level=AlertLevel.WARNING,
                    kind="freeze",
                    field=field,
                    message=f"{field}={_fmt(values[-1])} не меняется {limit:g} с (размах {spread:.3g}, допуск {flat_limit:.3g})",
                    value=values[-1],
                    detector="flatline",
                    expected=None,
                    confidence=0.9 if sigma is not None else 0.7,
                    window=_window(state, field, limit / 60.0),
                    details={"spread": spread, "limit_s": limit, "noise_sigma": sigma},
                )
            )
        return alerts


class CusumDetector:
    """CUSUM по невязкам: накопление малого устойчивого смещения.

    Применяется ТОЛЬКО к (а) разности пары датчиков за вычетом `pair_offset` и (б) невязке
    энергобаланса; допуск и порог — в сигмах baseline. К абсолютным значениям CO2, температуры,
    мощности CUSUM не применяется: при работе оборудования они меняются штатно.
    """

    def __init__(self, config: AppConfig, baseline: Baseline | None = None) -> None:
        """Args:
        config: Конфигурация (`detectors.cusum`, группы пар).
        baseline: Калибровка; без неё детектор молчит (остаются жёсткие пределы разности пар).
        """
        self._k = config.detectors.cusum.tolerance_sigma
        self._h = config.detectors.cusum.threshold_sigma
        self._outlier = config.detectors.cusum.outlier_sigma
        self._groups = config.detectors.pair_diff.groups
        self._baseline = baseline
        self._sums: dict[str, tuple[float, float]] = {}
        self._last_ts: datetime | None = None

    @property
    def threshold_sigma(self) -> float:
        """Порог накопления, в σ."""
        return self._h

    def sums(self) -> dict[str, float]:
        """Текущее накопленное смещение по ключам (`a|b` и `energy_balance`), в σ: большая из двух сторон."""
        return {key: max(hi, lo) for key, (hi, lo) in self._sums.items()}

    def _step(self, key: str, z: float, clip_outliers: bool = True) -> tuple[float, float]:
        """Один шаг двустороннего CUSUM; суммы ограничены 2h, чтобы тревога снималась быстро.

        Грубый выброс (|z| > outlier_sigma) — не малый дрейф: его ловят детекторы скачка, физического
        предела и разности пар, а сумма сбрасывается, чтобы выброс не оставил «хвост» ложного дрейфа.
        """
        if clip_outliers and abs(z) > self._outlier:
            self._sums[key] = (0.0, 0.0)
            return 0.0, 0.0
        hi, lo = self._sums.get(key, (0.0, 0.0))
        hi = min(max(0.0, hi + z - self._k), 2 * self._h)
        lo = min(max(0.0, lo - z - self._k), 2 * self._h)
        self._sums[key] = (hi, lo)
        return hi, lo

    def detect(self, state: State, now: datetime, residual: float | None = None) -> list[Alert]:
        """Обновить суммы и вернуть тревоги, пока смещение накоплено.

        Args:
            state: Состояние.
            now: Текущее время.
            residual: Новая невязка энергобаланса, Вт (если закончилось очередное окно).

        Returns:
            Тревоги `cusum` (warning): по парам датчиков и по энергобалансу.
        """
        latest = state.latest()
        if latest is None or self._baseline is None or not self._baseline.fitted or not latest.comms_ok:
            return []
        alerts: list[Alert] = []
        fresh = self._last_ts is None or latest.ts > self._last_ts
        self._last_ts = latest.ts
        flat = latest.flat()
        for group in self._groups:
            present = [f for f in group if f in flat]
            labels: dict[tuple[str, str], tuple[float, float, float]] = {}
            for a, b in combinations(present, 2):
                off = self._baseline.pair_offset(a, b)
                sa, sb = self._baseline.noise_level(a), self._baseline.noise_level(b)
                if off is None or sa is None or sb is None:
                    continue
                sigma = math.hypot(sa, sb)
                z = (flat[a] - flat[b] - off) / sigma
                key = pair_label(a, b)
                hi, lo = self._step(key, z) if fresh else self._sums.get(key, (0.0, 0.0))
                if max(hi, lo) > self._h:
                    labels[(a, b)] = (hi, lo, z)
            if labels:
                alerts.append(self._pair_alert(now, present, labels, state))
        if residual is not None and self._baseline.balance_residual_sigma():
            sigma_b = self._baseline.balance_residual_sigma() or 1.0
            z = residual / sigma_b
            hi, lo = self._step("energy_balance", z, clip_outliers=False)  # крупная невязка — это и есть сигнал
        else:
            hi, lo = self._sums.get("energy_balance", (0.0, 0.0))
        if max(hi, lo) > self._h:
            sigma_b = self._baseline.balance_residual_sigma() or 1.0
            direction = "АКБ теряет больше" if lo > hi else "АКБ получает больше"
            alerts.append(
                Alert(
                    ts=now,
                    level=AlertLevel.WARNING,
                    kind="cusum",
                    field="energy_balance",
                    message=(
                        f"Невязка энергобаланса устойчиво смещена (CUSUM {max(hi, lo):.1f}σ > {self._h:g}σ): "
                        f"{direction}, чем объясняют генерация и нагрузка — скрытая нагрузка или врущий датчик"
                    ),
                    value=max(hi, lo),
                    detector="cusum",
                    expected=0.0,
                    confidence=min(1.0, max(hi, lo) / (2 * self._h)),
                    details={"S_hi": hi, "S_lo": lo, "sigma_w": sigma_b, "direction": "negative" if lo > hi else "positive"},
                )
            )
        return alerts

    def _pair_alert(
        self,
        now: datetime,
        present: list[str],
        labels: dict[tuple[str, str], tuple[float, float, float]],
        state: State,
    ) -> Alert:
        """Тревога по паре (или по подозреваемому датчику группы)."""
        suspect = PairDiffDetector._suspect(present, {p: [] for p in labels})
        pair = max(labels, key=lambda p: max(labels[p][:2]))
        hi, lo, z = labels[pair]
        who = suspect or pair_label(*pair)
        return Alert(
            ts=now,
            level=AlertLevel.WARNING,
            kind="cusum",
            field=who,
            message=(
                f"Разность пары {pair[0]} и {pair[1]} устойчиво смещена от типичной (CUSUM {max(hi, lo):.1f}σ > {self._h:g}σ)"
                + (f"; подозрение на {suspect}" if suspect else "")
            ),
            value=max(hi, lo),
            detector="cusum",
            expected=0.0,
            confidence=min(1.0, max(hi, lo) / (2 * self._h)),
            window=_window(state, pair[0], 5.0),
            details={"pair": list(pair), "S_hi": hi, "S_lo": lo, "z": z, "suspect": suspect},
        )


def vent_required(state: State, now: datetime) -> list[Alert]:
    """Правило безопасности: при пайке вытяжка обязательна (регламент п. 9.1).

    Это жёсткое правило в коде: пайка (`solder_status=running`) при выключенной
    вытяжке — критическая тревога, на которую Монитор отвечает командой включения
    вытяжки в щадящем режиме.

    Args:
        state: Состояние.
        now: Текущее время.

    Returns:
        Критическая тревога ``safety_vent_required`` или пусто.
    """
    latest = state.latest()
    if latest is None:
        return []
    if latest.solder_status is DeviceStatus.RUNNING and latest.vent_mode == "off":
        return [
            Alert(
                ts=now,
                level=AlertLevel.CRITICAL,
                kind="safety_vent_required",
                field="vent_mode",
                message="Идёт пайка, а вытяжка выключена: безопасность требует включить вытяжку (регламент п. 9.1)",
                details={"solder_status": "running", "vent_mode": latest.vent_mode},
            )
        ]
    return []


def device_errors(state: State, now: datetime, status_fields: Iterable[str]) -> list[Alert]:
    """Устройство перешло в состояние ``error`` (остановка, засор и т.п.).

    Штаб не управляет производственным оборудованием (только наблюдение и указания
    Оператору), поэтому тревога ``warning``: нужна реакция Оператора и перепланирование.

    Args:
        state: Состояние.
        now: Текущее время.
        status_fields: Имена полей статуса устройств (``cnc_status`` и т.д.).

    Returns:
        По одной тревоге ``device_error`` на каждое устройство в ошибке.
    """
    latest = state.latest()
    if latest is None:
        return []
    alerts: list[Alert] = []
    for name in status_fields:
        if getattr(latest, name, None) is DeviceStatus.ERROR:
            alerts.append(
                Alert(
                    ts=now,
                    level=AlertLevel.WARNING,
                    kind="device_error",
                    field=name,
                    message=f"Устройство {name.removesuffix('_status')} в состоянии ошибки (статус error)",
                    details={"status": "error"},
                )
            )
    return alerts


class DetectorSuite:
    """Все детекторы вместе с подавлением при недостоверных данных."""

    def __init__(self, config: AppConfig, baseline: Baseline | None = None) -> None:
        """Args:
        config: Конфигурация Штаба.
        baseline: Калибровка шума/пар/невязки (без неё расширенные детекторы работают по жёстким пределам).
        """
        self.threshold = ThresholdDetector(config)
        self.trend = TrendDetector(config)
        self.jump = JumpDetector(config)
        self.freeze = FreezeDetector(config)
        self.stale = StaleDetector(config.detectors)
        self.rate = RateLimitDetector(config, baseline)
        self.monotonic = TimeMonotonicDetector(config)
        self.time_to_threshold = TimeToThresholdDetector(config)
        self.pair = PairDiffDetector(config, baseline)
        self.noise_loss = NoiseLossDetector(config, baseline)
        self.flatline = FlatlineDetector(config, baseline)
        self.cusum = CusumDetector(config, baseline)
        self._status_fields = list(config.equipment.device_lines)

    def run(self, state: State, now: datetime, residual: float | None = None) -> list[Alert]:
        """Прогнать детекторы.

        Если связь потеряна или данные устарели, возвращается только тревога об этом (и о
        нарушении времени): остальные детекторы по застывшим данным дали бы ложные срабатывания.

        Args:
            state: Состояние.
            now: Текущее время.
            residual: Новая невязка энергобаланса для CUSUM (если закончилось очередное окно).

        Returns:
            Тревоги всех детекторов.
        """
        time_alerts = self.monotonic.detect(state, now)
        stale_alerts = self.stale.detect(state, now)
        if stale_alerts:
            return stale_alerts + time_alerts
        alerts: list[Alert] = list(time_alerts)
        for det in (
            self.threshold,
            self.trend,
            self.jump,
            self.freeze,
            self.rate,
            self.time_to_threshold,
            self.pair,
            self.noise_loss,
            self.flatline,
        ):
            alerts.extend(det.detect(state, now))
        alerts.extend(self.cusum.detect(state, now, residual))
        alerts.extend(vent_required(state, now))
        alerts.extend(device_errors(state, now, self._status_fields))
        return alerts


class AlertTracker:
    """Убирает повторы тревог и ведёт список активных.

    Тревога считается новой, если её ключ (``Alert.key``) не активен либо уровень
    вырос. Активной тревога остаётся ``hold_s`` секунд после последнего обнаружения:
    для разовых (скачок) это время видимости на панели, для длительных (порог) —
    «хвост» после исчезновения причины.
    """

    def __init__(self, hold_s: float = 60.0) -> None:
        """Args:
        hold_s: Сколько секунд тревога остаётся активной после последнего обнаружения.
        """
        self._hold = hold_s
        self._active: dict[str, tuple[Alert, datetime]] = {}

    def filter(self, alerts: list[Alert], now: datetime) -> list[Alert]:
        """Отфильтровать повторы.

        Args:
            alerts: Тревоги текущего такта.
            now: Текущее время.

        Returns:
            Только новые или усилившиеся тревоги.
        """
        self._expire(now)
        fresh: list[Alert] = []
        for alert in alerts:
            known = self._active.get(alert.key)
            if known is None or alert.level.rank > known[0].level.rank:
                fresh.append(alert)
                self._active[alert.key] = (alert, now)
            else:
                first = known[0]  # то же состояние: одна проблема — один алерт с обновлением
                first.value, first.message, first.details = alert.value, alert.message, alert.details
                first.expected, first.confidence, first.window = alert.expected, alert.confidence, alert.window
                self._active[alert.key] = (first, now)
        return fresh

    def active(self, now: datetime) -> list[Alert]:
        """Активные тревоги, самые тяжёлые первыми.

        Args:
            now: Текущее время.

        Returns:
            Список тревог.
        """
        self._expire(now)
        return sorted((a for a, _ in self._active.values()), key=lambda a: (-a.level.rank, a.ts))

    def clear_key(self, key: str) -> None:
        """Забыть тревогу (например, после её разбора человеком).

        Args:
            key: Ключ тревоги.
        """
        self._active.pop(key, None)

    def _expire(self, now: datetime) -> None:
        """Удалить тревоги, не виденные дольше ``hold_s``."""
        for key in [k for k, (_, seen) in self._active.items() if (now - seen).total_seconds() > self._hold]:
            del self._active[key]
