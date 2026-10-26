"""Итоговый отчёт и проигрывание прогона по сохранённым файлам.

Работает только по файлам каталога прогона (``journal.jsonl``, ``telemetry.jsonl``,
``meta.json``, ``summary.json`` и, для симулятора, ``sim_events.jsonl`` — «правда» о
воздействиях), поэтому годится и для разбора реальной игры.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from kupol.core.journal import JOURNAL_FILE, read_jsonl, read_telemetry
from kupol.core.metrics import RunMetrics, compute_metrics, metrics_markdown
from kupol.core.models import JournalEntry

SIM_EVENTS_FILE = "sim_events.jsonl"
DETECT_WINDOW_S = 600.0  # тревога в этом окне после воздействия считается его обнаружением


@dataclass
class RunData:
    """Загруженные данные прогона."""

    run_dir: Path
    entries: list[JournalEntry]
    truth: list[dict[str, Any]]
    meta: dict[str, Any]
    summary: dict[str, Any]
    start: datetime | None
    ground_truth: list[dict[str, Any]] = field(default_factory=list)

    def rel(self, ts: datetime) -> float:
        """Секунды от начала прогона."""
        return (ts - self.start).total_seconds() if self.start else 0.0


def _read_json(path: Path) -> dict[str, Any]:
    """Прочитать JSON-файл или вернуть пустой словарь."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_run(run_dir: str | Path) -> RunData:
    """Загрузить файлы прогона.

    Args:
        run_dir: Каталог ``logs/<прогон>/``.

    Returns:
        Данные прогона.

    Raises:
        FileNotFoundError: Каталога или журнала нет.
    """
    d = Path(run_dir)
    if not (d / JOURNAL_FILE).exists():
        raise FileNotFoundError(f"В {d} нет {JOURNAL_FILE}: это не каталог прогона")
    entries: list[JournalEntry] = []
    for obj in read_jsonl(d / JOURNAL_FILE):
        try:
            entries.append(JournalEntry.model_validate(obj))
        except ValueError:
            continue
    telemetry = read_telemetry(d)
    start = telemetry[0].ts if telemetry else (entries[0].ts if entries else None)
    return RunData(
        run_dir=d,
        entries=entries,
        truth=list(read_jsonl(d / SIM_EVENTS_FILE)),
        meta=_read_json(d / "meta.json"),
        summary=_read_json(d / "summary.json"),
        start=start,
        ground_truth=list(read_jsonl(d / "ground_truth.jsonl")),
    )


def _clock(seconds: float) -> str:
    """Секунды в ``ч:мм:сс``."""
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _cell(text: object) -> str:
    """Безопасная ячейка Markdown-таблицы."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def _alerts(data: RunData) -> list[tuple[float, dict[str, Any]]]:
    """Тревоги из журнала: (время от старта, данные тревоги)."""
    out = []
    for e in data.entries:
        if e.kind == "alert" and "alert" in e.data:
            out.append((data.rel(e.ts), e.data["alert"]))
    return out


def _verdicts(data: RunData) -> list[tuple[float, dict[str, Any]]]:
    """Вердикты сверки из журнала."""
    return [(data.rel(e.ts), e.data["verdict"]) for e in data.entries if e.kind == "verdict" and "verdict" in e.data]


# --------------------------------------------------------------------------- распознавание воздействий


def _rules(verdict: dict[str, Any]) -> set[str]:
    """Правила, давшие свидетельства вердикту (главное и из списка свидетельств)."""
    return {verdict.get("rule", "")} | {e.get("rule", "") for e in verdict.get("evidence", [])}


def _first(items: list[tuple[float, dict[str, Any]]], after: float, until: float, pred: Any) -> tuple[float, dict[str, Any]] | None:
    """Первая запись в окне ``[after, until]``, удовлетворяющая условию."""
    for t, item in items:
        if after <= t <= until and pred(item):
            return t, item
    return None


def evaluate_detection(data: RunData) -> list[dict[str, Any]]:
    """Сопоставить воздействия симулятора с реакцией Штаба.

    Args:
        data: Данные прогона.

    Returns:
        Для каждого воздействия: что вброшено, обнаружено ли, с какой задержкой, что сказала сверка.
    """
    alerts, verdicts = _alerts(data), _verdicts(data)
    rows: list[dict[str, Any]] = []
    for ev in data.truth:
        kind, t0 = ev.get("kind"), float(ev.get("t", 0.0))
        window_end = t0 + DETECT_WINDOW_S
        row: dict[str, Any] = {"t": t0, "kind": kind, "text": ev.get("text", ""), "detected": None, "delay_s": None, "note": ""}
        found: tuple[float, dict[str, Any]] | None = None
        if kind == "false_signal":
            sensor = ev.get("sensor", "")
            found = _first(alerts, t0, window_end, lambda a: a.get("field") == sensor)
            v = _first(verdicts, t0, window_end, lambda x: sensor in x.get("fields", []))
            row["note"] = f"сверка: {v[1]['kind']}" if v else "сверка: нет вердикта"
            row["correct"] = bool(v and v[1]["kind"] in ("false_signal", "sensor_fault"))
        elif kind == "hidden_drift":
            sensor = ev.get("sensor", "")
            found = _first(alerts, t0, window_end + 1800, lambda a: a.get("field") == sensor and a.get("kind") == "trend")
            crit = _first(alerts, t0, data.rel(data.entries[-1].ts) if data.entries else t0, lambda a: a.get("field") == sensor and a.get("kind") == "threshold" and a.get("level") == "critical")
            if found and crit:
                row["note"] = f"тренд замечен за {crit[0] - found[0]:.0f} с до критического порога"
                row["correct"] = found[0] < crit[0]
            elif found:
                row["note"] = "тренд замечен; критического порога не было"
                row["correct"] = True
        elif kind == "direct":
            target = ev.get("target")
            if target == "pv":
                found = _first(alerts, t0, window_end, lambda a: a.get("field") == "pv_power_w")
                v = _first(verdicts, t0, window_end, lambda x: "pv_drop" in _rules(x) and x.get("kind") == "real")
                row["note"] = "сверка подтвердила падение" if v else "сверка не подтвердила"
            elif target == "occupants":
                found = _first(alerts, t0, window_end + 1800, lambda a: str(a.get("field", "")).startswith("co2_ppm."))
                v = _first(verdicts, t0, window_end + 1800, lambda x: bool(_rules(x) & {"co2_consensus_high", "co2_consensus_rise"}))
                row["note"] = "сверка: реальный рост" if v else "сверка не подтвердила"
            else:
                found = _first(alerts, t0, window_end + 600, lambda a: a.get("kind") in ("energy_t_left", "energy_deficit", "threshold"))
        elif kind == "comms_loss":
            found = _first(alerts, t0, window_end, lambda a: a.get("kind") in ("comms_loss", "stale"))
        elif kind == "actuator_fail":
            found = _first(alerts, t0, t0 + 3600, lambda a: a.get("kind") == "actuator_no_effect")
            if not found:
                row["note"] = "проявится только если на исполнитель подавалась команда"
        elif kind == "device_fault":
            dev = ev.get("device", "")
            found = _first(alerts, t0, window_end, lambda a: a.get("field") == f"{dev}_status")
        else:
            continue  # operator, command_* и т.п. — не воздействия
        if found:
            row["detected"], row["delay_s"] = True, found[0] - t0
        elif row["detected"] is None:
            row["detected"] = False
        rows.append(row)
    return rows


def find_false_alarms(data: RunData) -> list[tuple[float, dict[str, Any]]]:
    """Тревоги warning и выше, вне окон реальных воздействий (по «правде» симулятора).

    Args:
        data: Данные прогона.

    Returns:
        Список подозрительных тревог. Пусто и для чистого прогона, и когда «правды» нет.
    """
    if not data.truth:
        return []
    windows: list[tuple[float, float]] = []
    for ev in data.truth:
        if ev.get("kind") in ("false_signal", "hidden_drift", "direct", "comms_loss", "actuator_fail", "device_fault", "cascade"):
            windows.append((float(ev["t"]), float(ev["t"]) + 5400.0 if ev["kind"] in ("hidden_drift", "direct", "cascade", "actuator_fail", "device_fault") else float(ev["t"]) + 900.0))
    suspicious = []
    for t, a in _alerts(data):
        if a.get("level") not in ("warning", "critical"):
            continue
        if a.get("kind") in ("safety_vent_required", "time_to_threshold", "energy_deficit"):
            continue  # правило безопасности и прогнозы/справочные тревоги, а не показания датчиков
        if not any(lo <= t <= hi for lo, hi in windows):
            suspicious.append((t, a))
    return suspicious


# --------------------------------------------------------------------------- отчёт


def build_report(run_dir: str | Path) -> str:
    """Собрать итоговый отчёт в Markdown.

    Args:
        run_dir: Каталог прогона.

    Returns:
        Текст отчёта.

    Raises:
        FileNotFoundError: Это не каталог прогона.
    """
    data = load_run(run_dir)
    out: list[str] = [f"# Отчёт о прогоне `{data.run_dir.name}`", ""]
    out += _section_params(data)
    out += _section_timeline(data)
    out += _section_alerts(data)
    out += _section_detection(data)
    out += _section_metrics(data)
    out += _section_commands(data)
    out += _section_recommendations(data)
    out += _section_llm(data)
    return "\n".join(out) + "\n"


def _section_params(data: RunData) -> list[str]:
    """Параметры и итоговые счётчики."""
    out = ["## Параметры прогона", ""]
    for k, v in data.meta.items():
        out.append(f"- **{k}**: {v}")
    s = data.summary
    if s:
        out.append(f"- **длительность**: {_clock(float(s.get('elapsed_s', 0)))} из {_clock(float(s.get('duration_s', 0)))}")
        out.append("- **счётчики**: " + ", ".join(f"{k}={v}" for k, v in s.get("counters", {}).items()))
    out.append("")
    return out


def _section_timeline(data: RunData) -> list[str]:
    """Хронология ключевых событий."""
    out = ["## Хронология", "", "| Время | Тип | Событие | Результат |", "|---|---|---|---|"]
    kinds = ("alert", "verdict", "command", "recommendation", "agent", "error", "engine")
    n = 0
    for e in data.entries:
        if e.kind not in kinds:
            continue
        if e.kind == "alert" and e.status == "info":
            continue
        out.append(f"| {_clock(data.rel(e.ts))} | {e.kind} | {_cell(e.event)[:160]} | {_cell(e.result or e.status)[:100]} |")
        n += 1
    if n == 0:
        out.append("| — | — | событий нет | — |")
    out.append("")
    return out


def _section_alerts(data: RunData) -> list[str]:
    """Сводка тревог."""
    alerts = _alerts(data)
    out = ["## Тревоги", ""]
    if not alerts:
        return out + ["Тревог не было.", ""]
    by: dict[tuple[str, str], list[float]] = defaultdict(list)
    for t, a in alerts:
        by[(a.get("level", "?"), a.get("kind", "?"))].append(t)
    out += ["| Уровень | Вид | Количество | Первая |", "|---|---|---|---|"]
    for (lvl, kind), ts in sorted(by.items(), key=lambda kv: ({"critical": 0, "warning": 1}.get(kv[0][0], 2), kv[0][1])):
        out.append(f"| {lvl} | {kind} | {len(ts)} | {_clock(min(ts))} |")
    out.append("")
    return out


def _section_detection(data: RunData) -> list[str]:
    """Распознавание воздействий и ложные срабатывания (если есть «правда» симулятора)."""
    out = ["## Распознавание воздействий и ложные срабатывания", ""]
    if not data.truth:
        return out + ["Нет данных о воздействиях (прогон не на симуляторе): оценка ложных срабатываний недоступна.", ""]
    rows = evaluate_detection(data)
    if rows:
        out += ["| Время | Воздействие | Обнаружено | Задержка, с | Сверка |", "|---|---|---|---|---|"]
        for r in rows:
            mark = "да" if r["detected"] else "нет"
            delay = "—" if r["delay_s"] is None else f"{r['delay_s']:.0f}"
            out.append(f"| {_clock(r['t'])} | {_cell(r['text'])[:100]} | {mark} | {delay} | {_cell(r['note'])} |")
    else:
        out.append("Кризисных воздействий в сценарии не было.")
    out.append("")
    false_alarms = find_false_alarms(data)
    out.append(f"**Ложные срабатывания** (тревоги уровня warning и выше вне окон воздействий): {len(false_alarms)}")
    for t, a in false_alarms[:20]:
        out.append(f"- {_clock(t)} [{a.get('level')}] {a.get('message')}")
    out.append("")
    return out


def run_metrics(data: RunData) -> RunMetrics:
    """Метрики прогона по журналу и эталонной разметке (разметки нет — только метрики по журналу)."""
    duration = float(data.summary.get("duration_s") or data.summary.get("elapsed_s") or 0.0)
    if not duration and data.entries:
        duration = data.rel(data.entries[-1].ts)
    start = data.start or (data.entries[0].ts if data.entries else datetime.fromtimestamp(0))
    return compute_metrics(data.entries, data.ground_truth, start, duration)


def _section_metrics(data: RunData) -> list[str]:
    """Раздел «Метрики качества»."""
    return metrics_markdown(run_metrics(data))


def _section_commands(data: RunData) -> list[str]:
    """Команды и их судьба."""
    cmds = [e for e in data.entries if e.kind == "command"]
    out = ["## Команды и решения предохранителя", ""]
    if not cmds:
        return out + ["Команд не было.", ""]
    stat = Counter(e.status for e in cmds)
    out.append("Статусы: " + ", ".join(f"{k}={v}" for k, v in sorted(stat.items())))
    out += ["", "| Время | Кто | Команда | Статус | Пояснение |", "|---|---|---|---|---|"]
    for e in cmds:
        out.append(f"| {_clock(data.rel(e.ts))} | {_cell(e.actor)} | {_cell(e.action)[:80]} | {e.status} | {_cell(e.result)[:100]} |")
    out.append("")
    return out


def _section_recommendations(data: RunData) -> list[str]:
    """Рекомендации агентов."""
    recs = [e for e in data.entries if e.kind == "recommendation"]
    out = ["## Рекомендации агентов", ""]
    if not recs:
        return out + ["Рекомендаций не было.", ""]
    by = Counter((e.data.get("agent", e.actor), e.status) for e in recs)
    out += ["| Агент | Статус | Количество |", "|---|---|---|"]
    for (agent, status), n in sorted(by.items()):
        out.append(f"| {agent} | {status} | {n} |")
    out.append("")
    return out


def _section_llm(data: RunData) -> list[str]:
    """Использование LLM и фолбэки."""
    calls = [e for e in data.entries if e.kind == "llm_call"]
    out = ["## Использование LLM", ""]
    if not calls:
        return out + ["Вызовов модели не было.", ""]
    fallbacks = [c for c in calls if c.data.get("used_fallback")]
    out.append(f"Вызовов: {len(calls)}, из них резервный сценарий: {len(fallbacks)}")
    out += ["", "| Агент | Вызовов | Фолбэков | Средняя длительность, с |", "|---|---|---|---|"]
    per: dict[str, list[JournalEntry]] = defaultdict(list)
    for c in calls:
        per[c.actor].append(c)
    for agent, items in sorted(per.items()):
        fb = sum(1 for c in items if c.data.get("used_fallback"))
        avg = sum(float(c.data.get("duration_s", 0.0)) for c in items) / len(items)
        out.append(f"| {agent} | {len(items)} | {fb} | {avg:.2f} |")
    if fallbacks:
        reasons = Counter(str(c.data.get("error")) for c in fallbacks)
        out += ["", "Причины резервного сценария: " + "; ".join(f"{k} ×{v}" for k, v in reasons.items())]
    modes = Counter(f"{c.data.get('mode')}/{c.data.get('model')}" for c in calls)
    out += ["", "Режим/модель: " + ", ".join(f"{k} ×{v}" for k, v in modes.items()), ""]
    return out


# --------------------------------------------------------------------------- replay


def replay_lines(run_dir: str | Path, every_s: float = 60.0) -> Iterator[tuple[float, str]]:
    """Построить ленту проигрывания: записи журнала и периодические срезы телеметрии.

    Args:
        run_dir: Каталог прогона.
        every_s: Период срезов телеметрии, секунд игры.

    Yields:
        Пары ``(время от старта, строка)`` по возрастанию времени.
    """
    data = load_run(run_dir)
    items: list[tuple[float, int, str]] = []
    last = -every_s
    for tel in read_telemetry(data.run_dir):
        t = data.rel(tel.ts)
        if t - last >= every_s:
            last = t
            comms = "связь ок" if tel.comms_ok else "СВЯЗИ НЕТ"
            items.append(
                (
                    t,
                    0,
                    f"· SoC {tel.battery_soc_pct:.0f}%  PV {tel.pv_power_w:.0f} Вт  нагрузка {tel.load_total_w:.0f} Вт  "
                    f"CO2 {max(tel.co2_ppm.values(), default=0):.0f}  T {tel.temp_c:.1f}°C  вытяжка {tel.vent_mode}  {comms}",
                )
            )
    for e in data.entries:
        if e.kind == "llm_call":
            continue
        text = f"[{e.kind}] {e.actor}: {e.event}"
        if e.result:
            text += f" → {e.result}"
        items.append((data.rel(e.ts), 1, text))
    items.sort(key=lambda x: (x[0], x[1]))
    for t, _, text in items:
        yield t, f"{_clock(t)}  {text}"
