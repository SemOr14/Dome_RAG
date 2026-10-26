"""Метрики качества прогона по эталонной разметке симулятора (`ground_truth.jsonl`) и журналу.

Считаются:

* **задержка обнаружения** — время от начала воздействия до первого корректного сигнала (тревоги или вердикта);
* **упреждение** — для скрытых событий: насколько раньше критического порога сработал первый сигнал;
* **доля ложных срабатываний** — тревоги warning и выше вне окон воздействий, на час симулированного времени;
* **верность вердиктов** «ложный» и «реальный», число пересмотров (в том числе ожидаемых — `false_then_real`);
* **каскады** — вредные действия системы в окне ловушки (`cascade_trap`: например, вытяжка на максимум);
* **использование LLM и фолбэков**, проб и реакций.

Модуль работает по простым данным (записи журнала, разметка), поэтому годится и для разбора прогона после игры.
"""

from __future__ import annotations

import fnmatch
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from kupol.core.models import FALSE_KINDS, JournalEntry

DETECT_GRACE_S = 300.0  # сигнал в этом окне после конца воздействия всё ещё считается его обнаружением
# Тревоги, которые не считаются ложными срабатываниями вне окон воздействий: это правила безопасности,
# прогнозы, справочные и производные от действий человека/команд тревоги.
NOT_FALSE_ALARM_KINDS = frozenset(
    {
        "safety_vent_required",
        "time_to_threshold",
        "energy_deficit",
        "round_mismatch",
        "safety_event",
        "verdict_revised",
        "actuator_no_effect",
        "actuator_delayed",
    }
)
# Какие воздействия по природе «ложные показания / неисправность канала» (верный вердикт ложного вида: ложный сигнал,
# неисправность датчика или канала, дрейф) и какие «реальные» (верный вердикт «реальный»).
FALSE_READING_TYPES = frozenset(
    {"false_signal", "hidden_drift", "sensor_drift_pair", "frozen_sensor", "noise_loss", "stale_timestamp", "delayed_actuator"}
)
REAL_EVENT_TYPES = frozenset({"direct", "hidden_load", "silent_print_fail", "device_fault", "actuator_fail", "comms_loss"})


@dataclass
class EventResult:
    """Итог по одному воздействию из эталонной разметки."""

    id: str
    type: str
    category: str
    field: str
    t_start: float
    t_end: float | None
    observable: bool = True  # проявилось ли воздействие (для отказа исполнителя нужна команда ему)
    detected: bool = False
    delay_s: float | None = None
    signal: str = ""  # чем обнаружено: вид тревоги/вердикта
    lead_s: float | None = None  # упреждение относительно критического порога
    lead_ok: bool = True  # критического порога не было или сигнал раньше него
    expected_side: str = ""  # какой вердикт верен: real / false / ""
    verdict_kind: str = ""  # итоговый вид вердикта по полю в окне
    verdict_ok: bool | None = None  # None — вердикта по полю не было


@dataclass
class RunMetrics:
    """Метрики одного прогона."""

    duration_s: float
    events: list[EventResult] = field(default_factory=list)
    alerts_total: int = 0
    alerts_critical: int = 0
    alerts_warning: int = 0
    false_alarms: list[dict[str, Any]] = field(default_factory=list)
    false_alarms_per_hour: float = 0.0
    critical_false_alarms: int = 0
    verdict_events: Counter[str] = field(default_factory=Counter)
    revisions: int = 0
    expected_revisions: int = 0
    revised_in_time: int = 0
    cascades: int = 0
    cascade_actions: list[str] = field(default_factory=list)
    llm_calls: int = 0
    llm_fallbacks: int = 0
    llm_errors: dict[str, int] = field(default_factory=dict)
    probes: dict[str, int] = field(default_factory=dict)
    responses: dict[str, int] = field(default_factory=dict)
    commands: dict[str, int] = field(default_factory=dict)

    # --- производные показатели ---
    def detectable(self) -> list[EventResult]:
        """Воздействия, которые должны быть замечены (без ловушек и непроявившихся)."""
        return [e for e in self.events if e.category != "trap" and e.observable]

    def detection_rate(self, category: str | None = None) -> float | None:
        """Доля обнаруженных воздействий (по категории); `None`, если таких нет."""
        items = [e for e in self.detectable() if category is None or e.category == category]
        return None if not items else sum(1 for e in items if e.detected) / len(items)

    def hidden_all_detected_in_time(self) -> bool:
        """Каждое скрытое событие обнаружено, и упреждение положительно (или порога не было)."""
        hidden = [e for e in self.detectable() if e.category == "hidden"]
        return all(e.detected and e.lead_ok for e in hidden)

    def verdict_accuracy(self, side: str) -> float | None:
        """Доля верных вердиктов «ложный» (side=false) или «реальный» (side=real) среди событий, по которым был вердикт."""
        items = [e for e in self.events if e.expected_side == side and e.verdict_ok is not None]
        return None if not items else sum(1 for e in items if e.verdict_ok) / len(items)

    def to_dict(self) -> dict[str, Any]:
        """Сериализация для JSON."""
        d = asdict(self)
        d["verdict_events"] = dict(self.verdict_events)
        return d


# --------------------------------------------------------------------------- сопоставление событий и сигналов


def _fmatch(pattern: str, value: str | None) -> bool:
    """Поле сигнала относится к полю разметки (шаблоны и метки пар `a|b` допускаются)."""
    if value is None:
        return False
    return any(fnmatch.fnmatchcase(part, pattern) or fnmatch.fnmatchcase(pattern, part) for part in value.split("|"))


def _signal_rule(gt: dict[str, Any]) -> tuple[set[str], Callable[[str | None], bool]]:
    """Какие виды тревог и какие поля считаются корректным обнаружением воздействия."""
    t, f = gt["type"], gt["field"]
    if t == "false_signal" or t == "cascade_trap":
        return {"threshold", "jump", "rate_limit", "pair_diff", "trend", "cusum"}, lambda x: _fmatch(f, x)
    if t == "hidden_drift":
        return {"trend", "time_to_threshold", "threshold", "cusum", "pair_diff"}, lambda x: _fmatch(f, x)
    if t == "sensor_drift_pair":
        return {"cusum", "pair_diff", "threshold", "thermal_balance"}, lambda x: _fmatch(f, x) or x == "temp_c|temp_c_2"
    if t == "hidden_load":
        return {"cusum", "energy_balance", "energy_t_left", "energy_deficit"}, lambda x: x in ("energy_balance", "battery_soc_pct")
    if t == "silent_print_fail":
        return {"plan_vs_fact"}, lambda x: x is not None and x.startswith(f"{gt.get('device', 'printer')}_status")
    if t in ("frozen_sensor", "noise_loss"):
        return {"freeze", "noise_loss"}, lambda x: _fmatch(f, x)
    if t == "stale_timestamp":
        return {"time_monotonic", "stale"}, lambda x: True
    if t == "delayed_actuator":
        return {"actuator_delayed", "actuator_no_effect"}, lambda x: x == f or f == "all"
    if t == "actuator_fail":
        return {"actuator_no_effect", "cause_effect"}, lambda x: x in (f, "all") or (x or "").endswith(f)
    if t == "device_fault":
        return {"device_error"}, lambda x: x == f
    if t == "comms_loss":
        return {"comms_loss", "stale"}, lambda x: True
    if t == "direct":
        target = gt.get("target")
        if target == "pv":
            return {"jump", "threshold", "rate_limit", "energy_t_left", "energy_deficit"}, lambda x: x in ("pv_power_w", "battery_soc_pct", "battery_voltage_v")
        if target == "load":
            return {"energy_deficit", "energy_t_left", "cusum", "threshold"}, lambda x: True
        return {"threshold", "trend", "time_to_threshold"}, lambda x: (x or "").startswith("co2_ppm")
    return set(), lambda x: False


def _critical_fields(gt: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Критические тревоги, определяющие «критический порог» события: (виды, поля)."""
    t, f = gt["type"], gt["field"]
    if t in ("hidden_drift", "sensor_drift_pair", "false_signal"):
        return {"threshold"}, {f}
    if t == "hidden_load":
        return {"threshold", "energy_t_left"}, {"battery_soc_pct"}
    return set(), set()


class _Alerts:
    """Тревоги журнала с относительным временем."""

    def __init__(self, entries: list[JournalEntry], start: datetime) -> None:
        self.items: list[tuple[float, dict[str, Any]]] = [
            ((e.ts - start).total_seconds(), e.data["alert"]) for e in entries if e.kind == "alert" and "alert" in e.data
        ]

    def first(self, lo: float, hi: float, kinds: set[str], field_ok: Callable[[str | None], bool], level: str | None = None) -> tuple[float, dict[str, Any]] | None:
        for t, a in self.items:
            if lo <= t <= hi and a["kind"] in kinds and field_ok(a.get("field")) and (level is None or a["level"] == level):
                return t, a
        return None


def _executors_commanded(entries: list[JournalEntry], start: datetime, lo: float, hi: float) -> set[str]:
    """Исполнители, которым подавали команды в окне (для событий, проявляющихся только при команде)."""
    out: set[str] = set()
    for e in entries:
        if e.kind == "command" and e.status in ("accepted", "approved") and lo <= (e.ts - start).total_seconds() <= hi:
            name = e.data.get("command", "")
            out.add("vent" if "vent" in name else "humidifier" if "humidifier" in name else "line" if "line" in name else "power")
    return out


# --------------------------------------------------------------------------- вычисление


def compute_metrics(
    entries: list[JournalEntry],
    ground_truth: list[dict[str, Any]],
    start: datetime,
    duration_s: float,
    watch_after_false_s: float = 300.0,
    grace_s: float = DETECT_GRACE_S,
) -> RunMetrics:
    """Вычислить метрики прогона.

    Args:
        entries: Записи журнала.
        ground_truth: Эталонная разметка симулятора (пусто — оценка по воздействиям невозможна).
        start: Время старта игры (начало отсчёта).
        duration_s: Длительность игры, с.
        watch_after_false_s: Окно наблюдения за вердиктом «ложный» (допустимое время пересмотра).
        grace_s: Допуск по времени после окончания воздействия.

    Returns:
        Метрики прогона.
    """
    alerts = _Alerts(entries, start)
    m = RunMetrics(duration_s=duration_s)
    for _, a in alerts.items:
        m.alerts_total += 1
        m.alerts_critical += a["level"] == "critical"
        m.alerts_warning += a["level"] == "warning"
    verdicts = [((e.ts - start).total_seconds(), e.data) for e in entries if e.kind == "verdict" and "verdict" in e.data]
    m.verdict_events = Counter(d["event"] for _, d in verdicts)
    m.revisions = m.verdict_events.get("revised", 0)

    for gt in ground_truth:
        res = EventResult(id=gt["id"], type=gt["type"], category=gt["category"], field=gt["field"], t_start=gt["t_start"], t_end=gt["t_end"])
        if gt["category"] == "trap":
            m.events.append(res)
            continue
        lo = gt["t_start"] - 1.0
        hi = (gt["t_end"] if gt["t_end"] is not None else duration_s) + grace_s
        if gt["type"] in ("actuator_fail", "delayed_actuator"):
            res.observable = gt["field"] in _executors_commanded(entries, start, lo, hi) or gt["field"] == "all" and bool(_executors_commanded(entries, start, lo, hi))
        kinds, field_ok = _signal_rule(gt)
        hit = alerts.first(lo, hi, kinds, field_ok)
        if hit is not None:
            res.detected, res.delay_s, res.signal = True, hit[0] - gt["t_start"], hit[1]["kind"]
        ck, cf = _critical_fields(gt)
        if ck:
            crit = alerts.first(gt["t_start"], duration_s, ck, lambda x: x in cf, level="critical")
            if crit is not None and hit is not None:
                res.lead_s = crit[0] - hit[0]
                res.lead_ok = res.lead_s > 0
            elif crit is not None:
                res.lead_ok = False
        _verdict_check(res, gt, verdicts, lo, hi)
        m.events.append(res)

    _false_alarms(m, alerts, ground_truth, duration_s, grace_s)
    _revisions(m, ground_truth, verdicts, watch_after_false_s)
    _cascades(m, entries, ground_truth, start)
    _usage(m, entries)
    return m


def _verdict_check(res: EventResult, gt: dict[str, Any], verdicts: list[tuple[float, dict[str, Any]]], lo: float, hi: float) -> None:
    """Верен ли итоговый вердикт по полю воздействия."""
    side = "false" if gt["type"] in FALSE_READING_TYPES else ("real" if gt["type"] in REAL_EVENT_TYPES else "")
    res.expected_side = side
    if not side or "false_then_real" in str(gt.get("source", "")):
        return  # ложный-затем-реальный оценивается метрикой пересмотров
    subjects = {gt["field"], f"actuator:{gt['field']}", f"{gt.get('device', '')}_status", "energy_balance" if gt["type"] == "hidden_load" else "", "ts" if gt["type"] == "stale_timestamp" else ""}
    subjects.discard("")
    last: dict[str, Any] | None = None
    seen = False
    for t, d in verdicts:
        if lo <= t <= hi and d["event"] in ("created", "revised", "reclassified", "confirmed"):
            v = d["verdict"]
            if any(_fmatch(s, f) or _fmatch(f, s) for s in subjects for f in v["fields"]):
                seen = True
                if v["kind"] != "unknown":  # возврат в «неизвестно» по мере старения свидетельств вердикт не отменяет
                    last = v
    if not seen:
        return
    res.verdict_kind = last["kind"] if last is not None else "unknown"
    res.verdict_ok = last is not None and (last["kind"] in FALSE_KINDS) == (side == "false")


def _false_alarms(m: RunMetrics, alerts: _Alerts, ground_truth: list[dict[str, Any]], duration_s: float, grace_s: float) -> None:
    """Тревоги warning и выше вне окон воздействий."""
    windows = [(g["t_start"] - 1.0, (g["t_end"] if g["t_end"] is not None else duration_s) + grace_s) for g in ground_truth]
    for t, a in alerts.items:
        if a["level"] not in ("warning", "critical") or a["kind"] in NOT_FALSE_ALARM_KINDS:
            continue
        if any(lo <= t <= hi for lo, hi in windows):
            continue
        m.false_alarms.append({"t": t, "level": a["level"], "kind": a["kind"], "field": a.get("field"), "message": a.get("message", "")[:120]})
    m.critical_false_alarms = sum(1 for a in m.false_alarms if a["level"] == "critical")
    m.false_alarms_per_hour = len(m.false_alarms) / (duration_s / 3600.0) if duration_s else 0.0


def _revisions(m: RunMetrics, ground_truth: list[dict[str, Any]], verdicts: list[tuple[float, dict[str, Any]]], watch_s: float) -> None:
    """Ожидаемые пересмотры (`false_then_real`) и их своевременность."""
    false_signals = [g for g in ground_truth if g["category"] == "false"]
    for fs in (g for g in false_signals if "false_then_real" in str(g.get("source", ""))):
        m.expected_revisions += 1
        created = [t for t, d in verdicts if d["event"] in ("created", "revised") and _fmatch(fs["field"], (d["verdict"]["fields"] or [""])[0]) and d["verdict"]["kind"] in FALSE_KINDS and t >= fs["t_start"] - 1]
        revised = [
            t
            for t, d in verdicts
            if d["event"] in ("revised", "reclassified")
            and _fmatch(fs["field"], (d["verdict"]["fields"] or [""])[0])
            and d["verdict"]["kind"] not in FALSE_KINDS
            and t >= fs["t_start"]
        ]
        if created and revised and revised[0] - created[0] <= watch_s:
            m.revised_in_time += 1


def _cascades(m: RunMetrics, entries: list[JournalEntry], ground_truth: list[dict[str, Any]], start: datetime) -> None:
    """Вредные действия системы в окне ловушки каскада."""
    for trap in (g for g in ground_truth if g["category"] == "trap"):
        harmful = set(trap.get("harmful", []))
        lo, hi = trap["t_start"], trap["t_start"] + trap.get("window_s", 600.0)
        for e in entries:
            t = (e.ts - start).total_seconds()
            if e.kind == "command" and e.status == "accepted" and lo <= t <= hi:
                key = f"{e.data.get('command')}:{e.data.get('args', {}).get('mode')}"
                if key in harmful:
                    m.cascades += 1
                    m.cascade_actions.append(f"{t:.0f} с: {key} от {e.actor}")


def _usage(m: RunMetrics, entries: list[JournalEntry]) -> None:
    """LLM, пробы, реакции и команды."""
    calls = [e for e in entries if e.kind == "llm_call"]
    m.llm_calls = len(calls)
    m.llm_fallbacks = sum(1 for c in calls if c.data.get("used_fallback"))
    m.llm_errors = dict(Counter(str(c.data.get("error")) for c in calls if c.data.get("used_fallback")))
    m.probes = dict(Counter(e.status for e in entries if e.kind == "probe"))
    m.responses = dict(Counter(e.data.get("level", "?") for e in entries if e.kind == "response"))
    m.commands = dict(Counter(e.status for e in entries if e.kind == "command"))


# --------------------------------------------------------------------------- текст


def metrics_markdown(m: RunMetrics) -> list[str]:
    """Раздел отчёта «Метрики качества» (Markdown)."""
    out = ["## Метрики качества", ""]
    if not m.events:
        out.append("Эталонной разметки нет (прогон не на симуляторе): задержки, упреждение и верность вердиктов не считаются.")
    else:
        out += ["| Воздействие | Категория | Обнаружено | Задержка, с | Чем | Упреждение, с | Вердикт | Верен |", "|---|---|---|---|---|---|---|---|"]
        for e in m.events:
            if e.category == "trap":
                out.append(f"| {e.type} ({e.field}) | ловушка | — | — | — | — | — | — |")
                continue
            det = "не проявилось" if not e.observable else ("да" if e.detected else "**нет**")
            delay = "—" if e.delay_s is None else f"{e.delay_s:.0f}"
            lead = "—" if e.lead_s is None else f"{e.lead_s:.0f}" + ("" if e.lead_ok else " ⚠")
            ok = "—" if e.verdict_ok is None else ("да" if e.verdict_ok else "нет")
            out.append(f"| {e.type} ({e.field}) | {e.category} | {det} | {delay} | {e.signal or '—'} | {lead} | {e.verdict_kind or '—'} | {ok} |")
        out.append("")
        hidden = m.detection_rate("hidden")
        out.append(f"- Обнаружено скрытых: {'—' if hidden is None else f'{hidden:.0%}'}; все скрытые замечены до критического порога: {'да' if m.hidden_all_detected_in_time() else 'НЕТ'}")
        for side, name in (("false", "«ложный»"), ("real", "«реальный»")):
            acc = m.verdict_accuracy(side)
            out.append(f"- Верность вердиктов {name}: {'нет вердиктов' if acc is None else f'{acc:.0%}'}")
        out.append(f"- Пересмотров вердиктов: {m.revisions} (ожидаемых по разметке: {m.expected_revisions}, пересмотрено вовремя: {m.revised_in_time})")
        out.append(f"- **Каскады** (вредные действия в окнах ловушек): {m.cascades}" + (": " + "; ".join(m.cascade_actions) if m.cascade_actions else ""))
    out.append(f"- Ложные срабатывания вне окон воздействий: {len(m.false_alarms)} ({m.false_alarms_per_hour:.2f} в час), из них critical: {m.critical_false_alarms}")
    for a in m.false_alarms[:10]:
        out.append(f"  - {a['t']:.0f} с [{a['level']}] {a['kind']} {a['field']}: {a['message']}")
    out.append(f"- LLM: вызовов {m.llm_calls}, резервный сценарий {m.llm_fallbacks}" + (f" ({m.llm_errors})" if m.llm_errors else ""))
    out.append(f"- Пробы: {m.probes or 'не было'}; реакции по уровням: {m.responses or 'не было'}; команды: {m.commands or 'не было'}")
    out.append("")
    return out
