"""История величин и диагностических показателей для графиков панели.

Рекордер только копит числа, которые движок и так вычисляет (телеметрия, невязка баланса, накопление CUSUM,
разности пар в σ, вероятности вердиктов, уровень реакции). Новых вычислений для решений он не делает.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from collections.abc import Mapping
from itertools import combinations

from kupol.core.baseline import Baseline
from kupol.core.config import AppConfig
from kupol.core.models import Telemetry

# Префиксы имён диагностических рядов.
CUSUM = "cusum:"
PAIR_Z = "pair_z:"
P_REAL = "p_real:"
RESIDUAL = "residual_w"
T_LEFT = "t_left_min"
RESPONSE = "response_rank"

# Поля телеметрии, которые рисуются на графиках (все остальные — состояние линий — на них не нужны).
TELEMETRY_FIELDS = (
    "pv_power_w",
    "load_total_w",
    "battery_soc_pct",
    "battery_voltage_v",
    "battery_temp_c",
    "temp_c",
    "temp_c_2",
    "humidity_pct",
)
CO2_PREFIX = "co2_ppm."

MAX_POINTS = 8000  # хватает на игру 90 минут при такте 1 с с запасом


class ChartRecorder:
    """Потокобезопасная история рядов «время игры (с) → значение».

    Пишет поток движка, читает поток панели.
    """

    def __init__(self, config: AppConfig, baseline: Baseline | None = None, max_points: int = MAX_POINTS) -> None:
        """Args:
        config: Конфигурация (группы пар).
        baseline: Калибровка (σ и смещение пар) — для разности пар в σ.
        max_points: Сколько точек хранить на ряд.
        """
        self._groups = config.detectors.pair_diff.groups
        self._baseline = baseline
        self._max = max_points
        self._data: dict[str, deque[tuple[float, float]]] = {}
        self._lock = threading.Lock()

    def _put(self, name: str, t_s: float, value: float) -> None:
        if not math.isfinite(value):
            return
        self._data.setdefault(name, deque(maxlen=self._max)).append((t_s, value))

    def record(
        self,
        t_s: float,
        tel: Telemetry | None,
        *,
        residual_w: float | None = None,
        t_left_min: float | None = None,
        cusum: Mapping[str, float] | None = None,
        p_real: Mapping[str, float] | None = None,
        response_rank: int | None = None,
    ) -> None:
        """Записать значения одного такта (пустые пропускаются).

        Args:
            t_s: Время игры, с.
            tel: Последний снимок (``None`` — опрос не удался).
            residual_w: Невязка баланса, Вт, если окно только что закончилось.
            t_left_min: Запас до минимального заряда, мин (``None`` — АКБ не разряжается).
            cusum: Накопление CUSUM по ключам (в σ).
            p_real: Вероятность «реальный» по действующим вердиктам.
            response_rank: Наибольший уровень реакции (0..3).
        """
        with self._lock:
            if tel is not None:
                flat = tel.flat()
                for name in TELEMETRY_FIELDS:
                    if name in flat:
                        self._put(name, t_s, flat[name])
                for name, value in flat.items():
                    if name.startswith(CO2_PREFIX):
                        self._put(name, t_s, value)
                self._record_pairs(t_s, flat)
            if residual_w is not None:
                self._put(RESIDUAL, t_s, residual_w)
            if t_left_min is not None:
                self._put(T_LEFT, t_s, t_left_min)
            for key, value in (cusum or {}).items():
                self._put(CUSUM + key, t_s, value)
            for subject, p in (p_real or {}).items():
                self._put(P_REAL + subject, t_s, p)
            if response_rank is not None:
                self._put(RESPONSE, t_s, float(response_rank))

    def _record_pairs(self, t_s: float, flat: dict[str, float]) -> None:
        """Разность пары за вычетом типичного смещения, в σ разности (как её видят детектор пар и CUSUM)."""
        if self._baseline is None or not self._baseline.fitted:
            return
        for group in self._groups:
            present = [f for f in group if f in flat]
            for a, b in combinations(present, 2):
                off = self._baseline.pair_offset(a, b)
                sa, sb = self._baseline.noise_level(a), self._baseline.noise_level(b)
                if off is None or sa is None or sb is None:
                    continue
                sigma = math.hypot(sa, sb)
                if sigma > 0:
                    self._put(f"{PAIR_Z}{a}|{b}", t_s, (flat[a] - flat[b] - off) / sigma)

    def names(self, prefix: str = "") -> list[str]:
        """Имена рядов с данным префиксом (по алфавиту)."""
        with self._lock:
            return sorted(n for n in self._data if n.startswith(prefix))

    def series(self, name: str, since_s: float | None = None, max_points: int = 600) -> list[tuple[float, float]]:
        """Ряд за окно ``[since_s, конец]``, прореженный до ``max_points`` (последняя точка сохраняется).

        Args:
            name: Имя ряда.
            since_s: Начало окна, с (``None`` — с начала игры).
            max_points: Предел числа точек.

        Returns:
            Список ``(время игры в секундах, значение)``.
        """
        with self._lock:
            pts = list(self._data.get(name, ()))
        if since_s is not None:
            pts = [p for p in pts if p[0] >= since_s]
        if len(pts) > max_points:
            step = math.ceil(len(pts) / max_points)
            last = pts[-1]
            pts = pts[::step]
            if pts[-1] != last:
                pts.append(last)
        return pts

    def last_time(self) -> float:
        """Время последней записи, с (0, если записей нет)."""
        with self._lock:
            return max((d[-1][0] for d in self._data.values() if d), default=0.0)
