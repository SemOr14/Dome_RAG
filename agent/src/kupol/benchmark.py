"""Бенчмарк: прогон сценариев на нескольких seed и сводная таблица метрик.

Команда ``python -m kupol benchmark --scenarios scenarios/ --seeds 1-20`` прогоняет каждый сценарий на каждом
seed (в параллельных процессах), считает метрики по эталонной разметке симулятора (`core/metrics.py`) и
печатает/сохраняет сводку в ``docs/BENCHMARK.md``. Детерминизм по seed: один и тот же набор seed даёт те же
числа (кроме строки с временем формирования).
"""

from __future__ import annotations

import statistics
import tempfile
import time
from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kupol.adapter.scenario import load_scenario
from kupol.adapter.simulator import SimulatorAdapter
from kupol.core.config import load_config
from kupol.core.metrics import RunMetrics, compute_metrics
from kupol.engine import Engine


def parse_seeds(text: str) -> list[int]:
    """Разобрать список seed: ``1-20``, ``1,2,5`` или ``7``.

    Args:
        text: Строка диапазона/списка.

    Returns:
        Список seed по возрастанию без повторов.

    Raises:
        ValueError: Некорректная запись.
    """
    seeds: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            a, b = int(lo), int(hi)
            if b < a:
                raise ValueError(f"некорректный диапазон seed: {part}")
            seeds.update(range(a, b + 1))
        else:
            seeds.add(int(part))
    if not seeds:
        raise ValueError("не задано ни одного seed")
    return sorted(seeds)


@dataclass
class BenchResult:
    """Результат одного прогона бенчмарка."""

    scenario: str
    seed: int
    metrics: RunMetrics
    wall_s: float
    step_errors: int


def run_one(config_path: str, scenario_path: str, seed: int, sim_operator: bool = False) -> BenchResult:
    """Один прогон (функция верхнего уровня: вызывается в дочерних процессах).

    Args:
        config_path: Путь к ``config.yaml``.
        scenario_path: Путь к сценарию.
        seed: Seed симулятора.
        sim_operator: Симулированный Оператор отвечает на вопросы проб по разметке.

    Returns:
        Метрики прогона.
    """
    config = load_config(config_path)
    config.llm.mode = "mock"  # бенчмарк всегда офлайн
    scenario = load_scenario(scenario_path)
    sim = SimulatorAdapter(config, scenario, seed=seed)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="kupol-bench-") as tmp:
        eng = Engine(config, sim, Path(tmp) / "run", auto_approve=True, sim_operator=sim_operator, duration_s=scenario.duration_s)
        summary = eng.run(speed=0)
        entries = eng.journal.entries()
        metrics = compute_metrics(
            entries, sim.ground_truth, eng.start_time, scenario.duration_s, watch_after_false_s=config.verdicts.watch_after_false_s
        )
    return BenchResult(scenario.name, seed, metrics, time.monotonic() - started, int(summary["counters"]["step_errors"]))


def _run_args(args: tuple[str, str, int, bool]) -> BenchResult:
    """Распаковка аргументов для пула процессов."""
    return run_one(*args)


def run_benchmark(
    config_path: str | Path,
    scenarios: Sequence[Path],
    seeds: Sequence[int],
    workers: int = 1,
    sim_operator: bool = False,
) -> list[BenchResult]:
    """Прогнать все сценарии на всех seed.

    Args:
        config_path: Путь к конфигурации.
        scenarios: Файлы сценариев.
        seeds: Seed.
        workers: Число параллельных процессов (1 — последовательно).
        sim_operator: Симулированный Оператор для проб.

    Returns:
        Результаты по порядку (сценарий, seed).
    """
    jobs = [(str(config_path), str(s), seed, sim_operator) for s in scenarios for seed in seeds]
    if workers <= 1:
        return [_run_args(j) for j in jobs]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_run_args, jobs))


# --------------------------------------------------------------------------- сводка


def _pct(x: float | None) -> str:
    """Доля в процентах или прочерк."""
    return "—" if x is None else f"{x:.0%}"


def _median(values: list[float]) -> str:
    """Медиана или прочерк."""
    return "—" if not values else f"{statistics.median(values):.0f}"


def _p90(values: list[float]) -> str:
    """90-й процентиль или прочерк."""
    if not values:
        return "—"
    s = sorted(values)
    return f"{s[min(len(s) - 1, int(0.9 * len(s)))]:.0f}"


def summarize(results: Sequence[BenchResult], quiet_limit_per_hour: float, seeds: Sequence[int]) -> str:
    """Собрать Markdown-сводку бенчмарка.

    Args:
        results: Результаты прогонов.
        quiet_limit_per_hour: Допустимая частота ложных warning на сценарии quiet (из конфига).
        seeds: Использованные seed.

    Returns:
        Текст документа.
    """
    by_scn: dict[str, list[BenchResult]] = defaultdict(list)
    for r in results:
        by_scn[r.scenario].append(r)
    out = [
        "# Бенчмарк качества Штаба",
        "",
        f"Сформировано: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} командой `python -m kupol benchmark`. "
        f"Seed: {seeds[0]}–{seeds[-1]} ({len(seeds)} шт.), сценариев: {len(by_scn)}, прогонов: {len(results)}, LLM: mock. "
        "Числа детерминированы по seed.",
        "",
        "Метрики считаются по эталонной разметке симулятора (`ground_truth.jsonl`), см. `core/metrics.py`. "
        "«Обнаружено» — первый корректный сигнал (тревога или вердикт) в окне воздействия; «упреждение положительно» — "
        "сигнал раньше критического порога (или порога не было); ложные срабатывания — тревоги warning и выше вне окон воздействий.",
        "",
        "## Сводка по сценариям",
        "",
        "| Сценарий | Прогонов | Обнаружено воздействий | Скрытые обнаружены | Скрытые: упреждение > 0 (прогонов) | Ложные warning+/ч (среднее / макс.) | Ложные critical | Каскады | Пересмотры вовремя | Ошибки шагов |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, rs in by_scn.items():
        det = _mean_rate([r.metrics.detection_rate() for r in rs])
        hid = _mean_rate([r.metrics.detection_rate("hidden") for r in rs])
        lead_runs = [r for r in rs if r.metrics.detection_rate("hidden") is not None]
        lead_ok = sum(1 for r in lead_runs if r.metrics.hidden_all_detected_in_time())
        fa = [r.metrics.false_alarms_per_hour for r in rs]
        out.append(
            f"| {name} | {len(rs)} | {_pct(det)} | {_pct(hid)} | "
            f"{f'{lead_ok}/{len(lead_runs)}' if lead_runs else '—'} | {statistics.mean(fa):.2f} / {max(fa):.2f} | "
            f"{sum(r.metrics.critical_false_alarms for r in rs)} | {sum(r.metrics.cascades for r in rs)} | "
            f"{sum(r.metrics.revised_in_time for r in rs)}/{sum(r.metrics.expected_revisions for r in rs)} | {sum(r.step_errors for r in rs)} |"
        )
    out += ["", "## Обнаружение по типам воздействий (все сценарии и seed)", "", "| Тип | Событий | Обнаружено | Задержка, с (медиана / p90) | Верный вердикт | Чем обнаружено |", "|---|---|---|---|---|---|"]
    by_type: dict[str, list[Any]] = defaultdict(list)
    for r in results:
        for e in r.metrics.events:
            if e.category != "trap" and e.observable:
                by_type[e.type].append(e)
    for t, evs in sorted(by_type.items()):
        delays = [e.delay_s for e in evs if e.delay_s is not None]
        judged = [e for e in evs if e.verdict_ok is not None]
        signals: dict[str, int] = defaultdict(int)
        for e in evs:
            if e.signal:
                signals[e.signal] += 1
        top = ", ".join(f"{k}×{v}" for k, v in sorted(signals.items(), key=lambda kv: -kv[1])[:3])
        out.append(
            f"| {t} | {len(evs)} | {_pct(sum(1 for e in evs if e.detected) / len(evs))} | {_median(delays)} / {_p90(delays)} | "
            f"{_pct(sum(1 for e in judged if e.verdict_ok) / len(judged)) if judged else '—'} | {top or '—'} |"
        )
    hidden_rs = by_scn.get("hidden")
    if hidden_rs:
        out += ["", "## Сценарий hidden по seed", "", "| Seed | Скрытых обнаружено | Все до порога | Мин. упреждение, с | Ложные warning+/ч | Каскады |", "|---|---|---|---|---|---|"]
        for r in sorted(hidden_rs, key=lambda x: x.seed):
            ev = [e for e in r.metrics.detectable() if e.category == "hidden"]
            leads = [e.lead_s for e in ev if e.lead_s is not None]
            out.append(
                f"| {r.seed} | {sum(1 for e in ev if e.detected)}/{len(ev)} | {'да' if r.metrics.hidden_all_detected_in_time() else '**нет**'} | "
                f"{'—' if not leads else f'{min(leads):.0f}'} | {r.metrics.false_alarms_per_hour:.2f} | {r.metrics.cascades} |"
            )
    out += ["", "## Критерии приёмки дополнения", ""]
    out += _acceptance(by_scn, quiet_limit_per_hour)
    misses = [(r.scenario, r.seed, e) for r in results for e in r.metrics.detectable() if not e.detected]
    out += ["", "## Необнаруженные воздействия", ""]
    if not misses:
        out.append("Нет: каждое проявившееся воздействие обнаружено.")
    for scn, seed, e in misses[:40]:
        out.append(f"- {scn}, seed {seed}: {e.type} ({e.field}) с {e.t_start:.0f} с")
    walls = [r.wall_s for r in results]
    out += ["", f"Время прогона одного сценария: в среднем {statistics.mean(walls):.1f} с, максимум {max(walls):.1f} с.", ""]
    return "\n".join(out) + "\n"


def _mean_rate(values: list[float | None]) -> float | None:
    """Среднее по прогонам без пустых значений."""
    vals = [v for v in values if v is not None]
    return None if not vals else statistics.mean(vals)


def _acceptance(by_scn: dict[str, list[BenchResult]], quiet_limit: float) -> list[str]:
    """Проверка критериев приёмки дополнения по результатам."""
    lines: list[str] = []

    def mark(ok: bool) -> str:
        return "✅" if ok else "❌"

    hidden = by_scn.get("hidden", [])
    if hidden:
        ok = all(r.metrics.hidden_all_detected_in_time() for r in hidden)
        lines.append(f"- {mark(ok)} `hidden`: каждое скрытое событие обнаружено до критического порога на всех {len(hidden)} seed")
    quiet = by_scn.get("quiet", [])
    if quiet:
        worst = max(r.metrics.false_alarms_per_hour for r in quiet)
        crit = sum(r.metrics.critical_false_alarms for r in quiet)
        mean = statistics.mean(r.metrics.false_alarms_per_hour for r in quiet)
        lines.append(
            f"- {mark(mean <= quiet_limit and crit == 0)} `quiet`: ложных warning+ в среднем {mean:.2f}/ч (допустимо ≤ {quiet_limit:g}/ч, максимум по seed {worst:.2f}), critical: {crit}"
        )
    traps = by_scn.get("traps", [])
    if traps:
        casc = sum(r.metrics.cascades for r in traps)
        exp, got = sum(r.metrics.expected_revisions for r in traps), sum(r.metrics.revised_in_time for r in traps)
        lines.append(f"- {mark(casc == 0)} `traps`: каскадных действий {casc}")
        lines.append(f"- {mark(got == exp)} `traps`: вердикт «ложный» пересмотрен вовремя в {got} из {exp} случаев `false_then_real`")
    errs = sum(r.step_errors for rs in by_scn.values() for r in rs)
    lines.append(f"- {mark(errs == 0)} ошибок шагов цикла за все прогоны: {errs}")
    return lines
