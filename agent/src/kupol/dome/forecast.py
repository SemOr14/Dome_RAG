"""Прогноз важных параметров: через сколько часов купола значение дойдёт до порога.

Линейный тренд по недавним показаниям узла и, для АКБ, оценка по мощности разряда. Время — реальные часы:
физика устройств идёт в реальном времени, ускорены только сутки окружения (солнце, температура снаружи),
поэтому «через N ч» — это часы реального времени, а не часы купола. Прогноз — подсказка, а не факт: тренд не знает о
будущих переключениях и действиях людей, поэтому у каждой записи есть основа расчёта и качество подгонки.
Ничего не отправляет в Купол; результат уходит в промпт модели и в БД (событие ``forecast``).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any

WINDOW_S = 1800.0  # окно подгонки, секунды
MIN_POINTS = 6
MIN_SPAN_S = 120.0
MIN_R2 = 0.6  # хуже — тренд не отличим от шума
HORIZON_H = 48.0  # дальше этого не прогнозируем
KEEP_POINTS = 600
CRITICAL_SOC_PCT = 25.0  # IL_LOAD_SHED, как soc_critical_pct в dome.yaml


@dataclass(frozen=True)
class Spec:
    id: str
    label: str
    unit: str
    sources: tuple[tuple[str, str], ...]  # (узел, параметр); берётся первый присутствующий
    below: tuple[tuple[float, str], ...] = ()  # пороги при падении
    above: tuple[tuple[float, str], ...] = ()  # пороги при росте
    always: bool = False  # показывать и без тренда


SPECS: tuple[Spec, ...] = (
    Spec("battery_soc", "Заряд АКБ", "%", (("solar_inverter_01", "battery_soc_pct"), ("battery_01", "soc_pct")),
         below=((CRITICAL_SOC_PCT, "критический заряд (сброс нагрузки IL_LOAD_SHED)"), (0.0, "полная разрядка")),
         above=((100.0, "полный заряд"),), always=True),
    Spec("fuel_level", "Уровень топлива", "%", (("fuel_tank_01", "level_pct"),),
         below=((10.0, "низкий уровень топлива"), (0.0, "пустой бак")), always=True),
    Spec("battery_temp", "Температура АКБ", "°C", (("battery_01", "temperature_c"),), below=((0.0, "ниже 0 °C"),), above=((50.0, "выше 50 °C"),)),
    Spec("inverter_temp", "Температура инвертора", "°C", (("solar_inverter_01", "inverter_temp_c"),), above=((70.0, "выше 70 °C"),)),
    *(
        Spec(f"co2_{i}", f"CO2, climate_sensor_0{i}", "ppm", ((f"climate_sensor_0{i}", "co2_ppm"),), above=((1500.0, "выше 1500 ppm"),))
        for i in (1, 2, 3)
    ),
    *(
        Spec(f"humidity_{i}", f"Влажность, climate_sensor_0{i}", "%", ((f"climate_sensor_0{i}", "humidity_pct"),), above=((85.0, "выше 85 %"),))
        for i in (1, 2, 3)
    ),
    *(
        Spec(f"temp_{i}", f"Температура, climate_sensor_0{i}", "°C", ((f"climate_sensor_0{i}", "temperature_c"),),
             below=((10.0, "ниже 10 °C"),), above=((35.0, "выше 35 °C"),))
        for i in (1, 2, 3)
    ),
)  # fmt: skip


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def fit(points: list[tuple[float, float]]) -> tuple[float, float, float] | None:
    """Наименьшие квадраты: (наклон на секунду, значение в последней точке по линии, R²)."""
    n = len(points)
    if n < 2:
        return None
    t0 = points[0][0]
    xs = [t - t0 for t, _ in points]
    ys = [v for _, v in points]
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    syy = sum((y - my) ** 2 for y in ys)
    sse = sum((y - (my + slope * (x - mx))) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 if syy == 0 else max(0.0, 1.0 - sse / syy)
    return slope, my + slope * (xs[-1] - mx), r2


class Forecaster:
    """Ряды показаний и расчёт прогнозов. ``observe`` вызывается на каждой телеметрии."""

    def __init__(self) -> None:
        self._series: dict[tuple[str, str], deque[tuple[float, float]]] = {}
        self._marks: dict[tuple[str, str], Any] = {}

    def observe(self, nodes: dict[str, Any], wall: float) -> None:
        wanted = {src for spec in SPECS for src in spec.sources}
        for node, param in wanted:
            body = nodes.get(node)
            if not isinstance(body, dict):
                continue
            value = _number(body.get(param))
            if value is None:
                continue
            dq = self._series.setdefault((node, param), deque(maxlen=KEEP_POINTS))
            if dq and (wall <= dq[-1][0] or (body.get("updated_at") == self._marks.get((node, param)) and value == dq[-1][1])):
                continue  # то же измерение: тренд обучают только новые показания
            self._marks[(node, param)] = body.get("updated_at")
            dq.append((wall, value))

    def _points(self, source: tuple[str, str]) -> list[tuple[float, float]]:
        dq = self._series.get(source)
        if not dq:
            return []
        cutoff = dq[-1][0] - WINDOW_S
        return [p for p in dq if p[0] >= cutoff]

    def compute(self, nodes: dict[str, Any]) -> list[dict[str, Any]]:
        """Прогнозы по всем параметрам, у которых есть тренд к порогу (и обязательные — всегда)."""
        out: list[dict[str, Any]] = []
        for spec in SPECS:
            source = next((s for s in spec.sources if self._series.get(s)), None)
            if source is None:
                continue
            points = self._points(source)
            now = points[-1][1] if points else None
            if now is None:
                continue
            item: dict[str, Any] = {"id": spec.id, "label": spec.label, "node_id": source[0], "param": source[1],
                                    "unit": spec.unit, "now": round(now, 2), "trend": "нет данных для тренда"}
            span = points[-1][0] - points[0][0]
            line = fit(points) if len(points) >= MIN_POINTS and span >= MIN_SPAN_S else None
            if line is not None and line[2] >= MIN_R2:
                slope, _, r2 = line
                per_h = slope * 3600.0
                item.update(rate_per_h=round(per_h, 3), fit={"points": len(points), "span_min": round(span / 60, 1), "r2": round(r2, 2)})
                item["trend"] = "падает" if per_h < 0 else "растёт"
                targets = spec.below if per_h < 0 else spec.above
                etas = []
                for threshold, name in targets:
                    reach = (threshold - now) / per_h if per_h else None
                    if reach is not None and 0 < reach <= HORIZON_H:
                        etas.append({"threshold": threshold, "name": name, "eta_h": round(reach, 2)})
                if etas:
                    item["reaches"] = etas
            elif line is not None:
                item["trend"] = "стабильно (тренд не выделен)"
            if spec.id == "battery_soc":
                balance = self._battery_balance(nodes, now)
                if balance:
                    item["by_power"] = balance
            if item.get("reaches") or item.get("by_power") or spec.always:
                out.append(item)
        return out

    @staticmethod
    def _battery_balance(nodes: dict[str, Any], soc: float) -> dict[str, Any] | None:
        """Оценка по мощности разряда: сколько часов хватит запаса при нынешней нагрузке на АКБ."""
        inv = nodes.get("solar_inverter_01")
        if not isinstance(inv, dict):
            return None
        power = _number(inv.get("battery_power_w"))  # < 0 — разряд
        cap_kwh = _number(inv.get("battery_capacity_kwh")) or _number((nodes.get("battery_01") or {}).get("nominal_capacity_kwh")) or 2.5
        if power is None or power >= -20.0:
            return {"discharge_w": 0.0 if power is None or power >= 0 else round(-power, 1), "note": "АКБ не разряжается заметно"}
        discharge = -power
        reaches = []
        for threshold, name in ((CRITICAL_SOC_PCT, "критический заряд (сброс нагрузки IL_LOAD_SHED)"), (0.0, "полная разрядка")):
            if soc > threshold:
                reaches.append({"threshold": threshold, "name": name, "eta_h": round((soc - threshold) / 100.0 * cap_kwh * 1000.0 / discharge, 2)})
        return {"discharge_w": round(discharge, 1), "capacity_kwh": cap_kwh, "reaches": reaches}


def forecast_lines(items: list[dict[str, Any]]) -> list[str]:
    """Короткие строки для промпта и журнала: по одной на параметр."""
    lines = []
    for f in items:
        head = f"{f['label']} ({f['node_id']}.{f['param']}): {f['now']} {f['unit']}, {f['trend']}"
        if "rate_per_h" in f:
            head += f" ({f['rate_per_h']:+g} {f['unit']}/ч)"
        parts = [f"до «{r['name']}» ({r['threshold']:g} {f['unit']}) ≈ {r['eta_h']:g} ч"
                 for r in f.get("reaches", [])]
        bp = f.get("by_power")
        if bp:
            if bp.get("reaches"):
                parts += [f"по мощности разряда {bp['discharge_w']:g} Вт до «{r['name']}» ≈ {r['eta_h']:g} ч" for r in bp["reaches"]]
            else:
                parts.append(bp.get("note", ""))
        lines.append(head + ("; " + "; ".join(p for p in parts if p) if any(parts) else ""))
    return lines
