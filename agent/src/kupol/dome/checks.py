"""Сверки независимых источников по сырым узлам купола.

Чистые функции от ``DomeState`` и конфига: ничего не отправляют. Уверенность считает код
(сумма взвешенных свидетельств через сигмоиду, как в вердиктах Штаба), модель её не назначает.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Literal

from kupol.dome.config import ChecksCfg
from kupol.dome.state import DomeState

Supports = Literal["real", "false"]
SENSORS = ("climate_sensor_01", "climate_sensor_02", "climate_sensor_03")
UNTRUSTED = ("UNRELIABLE", "FAILED")
SCALE = 2.5  # масштаб агрегации свидетельств (как в verdicts.aggregation.scale)


@dataclass
class Ev:
    """Свидетельство в пользу «реально» или «ложно»."""

    source: str
    supports: Supports
    weight: float
    detail: str
    node: str = ""
    param: str = ""
    value: Any = None

    def as_log(self) -> dict[str, Any]:
        out: dict[str, Any] = {"supports": self.supports, "weight": self.weight, "detail": self.detail}
        if self.node:
            out["node"] = self.node
        if self.param:
            out["param"] = self.param
        if self.value is not None:
            out["value"] = self.value
        return out


def p_real(evidence: list[Ev], scale: float = SCALE) -> float:
    """Вероятность «сигнал реален» по свидетельствам; без свидетельств — 0,5."""
    total = sum(e.weight if e.supports == "real" else -e.weight for e in evidence)
    return 1.0 / (1.0 + math.exp(-scale * total))


def independent_sources(evidence: list[Ev], supports: Supports) -> int:
    """Число различных независимых источников свидетельств заданной стороны."""
    return len({e.source for e in evidence if e.supports == supports and e.weight > 0})


def trust_map(state: DomeState) -> dict[str, str]:
    """``data_trust`` автоматики: ``"узел.параметр" -> UNRELIABLE | FAILED``."""
    raw = state.get("dome_automation_01.data_trust", {})
    return {str(k): str(v).upper() for k, v in raw.items()} if isinstance(raw, dict) else {}


def is_untrusted(state: DomeState, node: str, param: str) -> bool:
    """Пометила ли автоматика источник недостоверным (по параметру или всему узлу)."""
    trust = trust_map(state)
    return trust.get(f"{node}.{param}") in UNTRUSTED or trust.get(f"{node}.*") in UNTRUSTED


def max_rate(state: DomeState, path: str, window_s: float = 30.0) -> float:
    """Наибольшая скорость изменения (ед./с) между соседними разными отсчётами за окно."""
    pts = state.series(path, window_s)
    best = 0.0
    for (t0, v0), (t1, v1) in zip(pts, pts[1:], strict=False):
        if v1 != v0 and t1 > t0:
            best = max(best, abs(v1 - v0) / max(t1 - t0, 1.0))
    return best


# --------------------------------------------------------------------------- CO2


@dataclass
class Co2Assessment:
    """Сводка по CO₂ трёх датчиков."""

    values: dict[str, float] = field(default_factory=dict)  # живые (не None, не замёрзшие)
    untrusted: set[str] = field(default_factory=set)  # помечены автоматикой
    spikes: set[str] = field(default_factory=set)  # физически невозможная скорость роста
    outliers: set[str] = field(default_factory=set)  # расходятся с согласными остальными
    kind: Literal["ok", "false_sensor", "real", "unknown"] = "ok"
    reference: float | None = None  # типичное значение по «хорошим» датчикам
    evidence: list[Ev] = field(default_factory=list)

    @property
    def suspects(self) -> set[str]:
        return self.spikes | self.outliers

    def good(self) -> dict[str, float]:
        """Показания датчиков, которым можно верить (не помечены и не подозрительны)."""
        return {s: v for s, v in self.values.items() if s not in self.untrusted and s not in self.suspects}


def assess_co2(state: DomeState, cfg: ChecksCfg) -> Co2Assessment:
    """Сверить CO₂ датчиков: выброс одного при согласных двух — ложный; рост на всех — реальный."""
    c = cfg.co2
    a = Co2Assessment()
    for s in c.sensors:
        v = state.num(f"{s}.co2_ppm")
        if v is None or state.is_frozen(s):
            continue
        a.values[s] = v
        if is_untrusted(state, s, "co2_ppm"):
            a.untrusted.add(s)
        if max_rate(state, f"{s}.co2_ppm") > c.max_rate_ppm_s:
            a.spikes.add(s)
    for s in a.spikes:
        a.evidence.append(Ev("physics", "false", 0.9, f"{s}: скорость роста CO₂ выше {c.max_rate_ppm_s:g} ppm/с физически невозможна", s, "co2_ppm", a.values.get(s)))
    live = list(a.values)
    if len(live) >= 3:
        for s in live:
            others = [a.values[o] for o in live if o != s]
            if abs(others[0] - others[1]) <= c.agree_diff_ppm and abs(a.values[s] - statistics.mean(others)) > c.outlier_diff_ppm:
                a.outliers.add(s)
                a.evidence.append(
                    Ev(f"sensor:{s}", "false", 0.8, f"{s} = {a.values[s]:.0f} ppm при согласных остальных ({others[0]:.0f}/{others[1]:.0f})", s, "co2_ppm", a.values[s])
                )
    good = a.good()
    if good:
        a.reference = statistics.median(good.values())
    if a.suspects and good:
        a.kind = "false_sensor"
        return a
    non_susp = {s: v for s, v in a.values.items() if s not in a.suspects}
    if non_susp and statistics.median(non_susp.values()) >= c.high_ppm:
        agreeing = [s for s, v in non_susp.items() if v >= c.high_ppm]
        a.kind = "real" if len(agreeing) >= 2 else "unknown"
        for s in agreeing:
            a.evidence.append(Ev(f"sensor:{s}", "real", 0.6, f"{s} = {non_susp[s]:.0f} ppm", s, "co2_ppm", non_susp[s]))
    return a


# --------------------------------------------------------------------------- влажность / конденсат


@dataclass
class HumidityAssessment:
    """Сводка по риску конденсата."""

    rh: dict[str, float] = field(default_factory=dict)
    gap: dict[str, float] = field(default_factory=dict)  # T − Tросы
    bad: set[str] = field(default_factory=set)
    kind: Literal["ok", "false_sensor", "real", "unknown"] = "ok"
    evidence: list[Ev] = field(default_factory=list)

    def trigger(self, sensor: str, rh_limit: float, gap_limit: float) -> bool:
        return self.rh.get(sensor, 0) > rh_limit or self.gap.get(sensor, 99) < gap_limit


def assess_condensate(state: DomeState, cfg: ChecksCfg) -> HumidityAssessment:
    """Сверить условия ``IL_CONDENSATE`` (RH > 90 % или запас до точки росы < 1 °C) по трём датчикам."""
    c = cfg.condensate
    a = HumidityAssessment()
    for s in SENSORS:
        if state.is_frozen(s):
            continue
        rh = state.num(f"{s}.humidity_pct")
        t = state.num(f"{s}.temperature_c")
        dew = state.num(f"{s}.dew_point_c")
        if rh is not None:
            a.rh[s] = rh
        if t is not None and dew is not None:
            a.gap[s] = t - dew
    hot = [s for s in a.rh if a.trigger(s, c.rh_interlock_pct, c.dew_gap_c)]
    calm = [s for s in a.rh if s not in hot]
    if not hot:
        return a
    for s in hot:
        a.evidence.append(Ev(f"sensor:{s}", "real", 0.5, f"{s}: RH {a.rh.get(s, float('nan')):.0f} %, запас до точки росы {a.gap.get(s, float('nan')):.1f} °C", s, "humidity_pct", a.rh.get(s)))
    if len(calm) >= 2 and len(hot) == 1:
        a.bad = set(hot)
        a.kind = "false_sensor"
        a.evidence.append(Ev("cross_sensors", "false", 0.9, f"остальные датчики спокойны: RH {', '.join(f'{a.rh[x]:.0f}' for x in calm)} %", hot[0], "humidity_pct"))
    elif len(hot) >= 2:
        a.kind = "real"
    else:
        a.kind = "unknown"
    return a


# --------------------------------------------------------------------------- дым


@dataclass
class SmokeAssessment:
    """Сводка по дыму в зоне FABLAB."""

    alarm: bool = False
    zone_sensor: str = "smoke_detector_01"
    kind: Literal["ok", "false", "real", "unknown"] = "ok"
    evidence: list[Ev] = field(default_factory=list)
    needs: int = 2

    @property
    def false_sources(self) -> int:
        return independent_sources(self.evidence, "false")

    @property
    def real_sources(self) -> int:
        return independent_sources(self.evidence, "real")


def assess_smoke(state: DomeState, cfg: ChecksCfg, zone_sensor: str = "smoke_detector_01") -> SmokeAssessment:
    """Дым: «реально» при любом признаке реального, «ложно» только при ≥ N независимых свидетельствах ложности."""
    c = cfg.smoke
    a = SmokeAssessment(zone_sensor=zone_sensor, needs=c.false_evidence_needed)
    node = state.node(zone_sensor)
    if not node:
        return a
    a.alarm = bool(node.get("smoke_detected")) or str(node.get("alarm_state", "NORMAL")).upper() not in ("NORMAL", "")
    if not a.alarm:
        return a
    obs = state.num(f"{zone_sensor}.obscuration_pct")
    co = state.num("air_quality_sensor_01.co_ppm")
    voc = state.num("air_quality_sensor_01.voc_index")
    other = "smoke_detector_02" if zone_sensor == "smoke_detector_01" else "smoke_detector_01"
    if node.get("false_alarm_suspected") is True:
        a.evidence.append(Ev("detector_flag", "false", 0.5, "датчик сам отмечает false_alarm_suspected", zone_sensor))
    if obs is not None:
        if obs <= c.obscuration_ok_pct:
            a.evidence.append(Ev("obscuration", "false", 0.6, f"задымлённость {obs:.1f} % — нет дыма", zone_sensor, "obscuration_pct", obs))
        else:
            a.evidence.append(Ev("obscuration", "real", 0.8, f"задымлённость {obs:.1f} %", zone_sensor, "obscuration_pct", obs))
    if co is not None:
        if co <= c.co_ok_ppm:
            a.evidence.append(Ev("co", "false", 0.6, f"CO {co:.1f} ppm в норме", "air_quality_sensor_01", "co_ppm", co))
        else:
            a.evidence.append(Ev("co", "real", 0.8, f"CO {co:.1f} ppm повышен", "air_quality_sensor_01", "co_ppm", co))
    if voc is not None:
        if voc <= c.voc_ok_index:
            a.evidence.append(Ev("voc", "false", 0.4, f"VOC {voc:.0f} в норме", "air_quality_sensor_01", "voc_index", voc))
        else:
            a.evidence.append(Ev("voc", "real", 0.5, f"VOC {voc:.0f} повышен", "air_quality_sensor_01", "voc_index", voc))
    if state.get(f"{other}.smoke_detected") is True and state.get(f"{other}.zone") == node.get("zone"):
        a.evidence.append(Ev("second_detector", "real", 0.9, f"{other} тоже видит дым", other))
    if a.real_sources:
        a.kind = "real"
    elif a.false_sources >= a.needs:
        a.kind = "false"
    else:
        a.kind = "unknown"
    return a


# --------------------------------------------------------------------------- принтер


@dataclass
class PrinterAssessment:
    """Что на самом деле делает принтер (``webhook_state`` может врать)."""

    state: str
    real_state: str
    webhook_state: str
    progress: float | None
    webhook_lies: bool
    done: bool  # печать действительно завершена
    printing: bool
    bed_clear: bool | None
    bed_temp: float | None
    held: bool


def assess_printer(state: DomeState) -> PrinterAssessment:
    """Свести ``state``, ``real_state``, ``webhook_state`` и ход печати; ложное «готово» от webhook отсеивается."""
    n = "printer_3d_01"
    st = str(state.get(f"{n}.state", "")).upper()
    real = str(state.get(f"{n}.real_state", st)).upper()
    hook = str(state.get(f"{n}.webhook_state", "")).upper()
    progress = state.num(f"{n}.progress_pct")
    left = state.num(f"{n}.print_time_left_s")
    printing = "PRINTING" in (st, real)
    lies = hook == "COMPLETED" and (printing or (progress is not None and progress < 99.0) or (left is not None and left > 60))
    done = (
        st == "COMPLETED" and real in ("COMPLETED", "IDLE", "") and not lies and (progress is None or progress >= 99.0) and not printing
    )
    return PrinterAssessment(
        st, real, hook, progress, lies, done, printing, state.get(f"{n}.bed_clear"), state.num(f"{n}.bed_temp_c"), bool(state.get(f"{n}.hold_level"))
    )


# --------------------------------------------------------------------------- энергия


@dataclass
class EnergyAssessment:
    """Энергобаланс и запас времени."""

    soc: float | None
    mode: str
    pv_w: float
    load_w: float
    battery_w: float  # > 0 заряд, < 0 разряд (как в телеметрии инвертора)
    grid_ok: bool
    capacity_wh: float
    t_left_h: float | None  # до критического SOC при текущем разряде


def assess_energy(state: DomeState, cfg: ChecksCfg) -> EnergyAssessment:
    """Сводка по инвертору: SOC, режим, баланс и время до критического заряда."""
    inv = "solar_inverter_01"
    soc = state.num(f"{inv}.battery_soc_pct")
    if soc is None:
        soc = state.num("battery_01.soc_pct")
    pv = state.num(f"{inv}.pv_power_w") or 0.0
    load = state.num(f"{inv}.load_power_w") or 0.0
    batt = state.num(f"{inv}.battery_power_w") or 0.0
    cap_kwh = state.num(f"{inv}.battery_capacity_kwh") or state.num("battery_01.nominal_capacity_kwh") or 2.5
    cap_wh = cap_kwh * 1000.0
    discharge = -batt if batt < 0 else 0.0
    t_left = None
    if soc is not None and discharge > 20:
        t_left = max(0.0, (soc - cfg.energy.soc_critical_pct) / 100.0 * cap_wh / discharge)
    grid_ok = str(state.get(f"{inv}.grid_state", "")).upper() == "AVAILABLE"
    return EnergyAssessment(soc, str(state.get(f"{inv}.mode", "")).upper(), pv, load, batt, grid_ok, cap_wh, t_left)
