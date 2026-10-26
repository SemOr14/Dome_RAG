"""CLI Штаба «Купол»: simulate, run, replay, report, dashboard.

Примеры::

    python -m kupol simulate --scenario scenarios/basic.yaml --speed 0 --seed 42
    python -m kupol run --config config.yaml
    python -m kupol replay logs/<прогон>/
    python -m kupol report logs/<прогон>/
    python -m kupol dashboard --scenario scenarios/basic.yaml --speed 10
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from kupol.adapter.base import AdapterError
from kupol.adapter.real import RealAdapter
from kupol.adapter.scenario import ScenarioError, load_scenario
from kupol.adapter.simulator import SimulatorAdapter
from kupol.agents.llm import LLMClient, LLMError
from kupol.core.config import AppConfig, ConfigError, load_config
from kupol.core.journal import new_run_dir
from kupol.engine import Engine
from kupol.report import build_report, replay_lines

DEFAULT_CONFIG = "config.yaml"
EXIT_USAGE = 2


def load_env_file(path: str | Path = ".env") -> None:
    """Загрузить переменные из ``.env`` (``KEY=VALUE``), не затирая уже заданные.

    Args:
        path: Путь к файлу; если его нет — ничего не происходит. Файл не попадает в git.
    """
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _setup_console() -> Console:
    """Консоль с UTF-8 (Windows-консоли с cp1251 иначе падают на кириллице и стрелках)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    return Console(file=sys.stdout, highlight=False)


def build_parser() -> argparse.ArgumentParser:
    """Построить разбор аргументов командной строки."""
    parser = argparse.ArgumentParser(prog="kupol", description="Штаб «Купол»: наблюдение, диагностика, управление")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", default=DEFAULT_CONFIG, help="путь к config.yaml")
        p.add_argument("--logs-dir", default=None, help="каталог журналов (по умолчанию из config.yaml)")
        p.add_argument("--llm-mode", choices=["mock", "dev", "competition"], default=None, help="переопределить llm.mode")

    sim = sub.add_parser("simulate", help="прогон симулированной игры с агентами")
    common(sim)
    sim.add_argument("--scenario", required=True, help="YAML-сценарий из scenarios/")
    sim.add_argument("--speed", type=float, default=0.0, help="ускорение: 1 — реальное время, 0 — максимально быстро")
    sim.add_argument("--seed", type=int, default=42, help="seed симулятора (детерминизм)")
    sim.add_argument("--duration", type=float, default=None, help="ограничить длительность игры, с")
    sim.add_argument("--auto-approve", action="store_true", help="сам подтверждать рекомендации (только headless-симуляция)")
    sim.add_argument("--sim-operator", action="store_true", help="симулированный Оператор отвечает на вопросы проб по разметке симулятора")
    sim.add_argument("--llm-fail", choices=["timeout", "bad_json"], default=None, help="имитировать отказ модели (mock)")

    run = sub.add_parser("run", help="боевой режим с реальным адаптером")
    common(run)

    rep = sub.add_parser("replay", help="проиграть сохранённый прогон")
    rep.add_argument("run_dir")
    rep.add_argument("--speed", type=float, default=0.0, help="0 — вывести сразу; N — ускорение относительно реального времени")
    rep.add_argument("--every", type=float, default=60.0, help="период срезов телеметрии, с")

    rpt = sub.add_parser("report", help="итоговый отчёт по прогону (Markdown)")
    rpt.add_argument("run_dir")
    rpt.add_argument("--out", default=None, help="файл отчёта (по умолчанию <прогон>/report.md)")

    bench = sub.add_parser("benchmark", help="прогнать все сценарии на нескольких seed и свести метрики")
    bench.add_argument("--config", default=DEFAULT_CONFIG)
    bench.add_argument("--scenarios", default="scenarios", help="каталог сценариев или один файл")
    bench.add_argument("--seeds", default="1-20", help="диапазон или список seed: 1-20, 1,2,5")
    bench.add_argument("--only", default=None, help="только эти сценарии (через запятую)")
    bench.add_argument("--workers", type=int, default=0, help="процессов (0 — по числу ядер, не более 6)")
    bench.add_argument("--out", default="docs/BENCHMARK.md", help="куда сохранить сводку (пусто — не сохранять)")
    bench.add_argument("--sim-operator", action="store_true", help="симулированный Оператор отвечает на вопросы проб")

    dome = sub.add_parser("dome", help="агент для соревновательного Купола (то же, что python main.py)")
    dome.add_argument("rest", nargs=argparse.REMAINDER, help="параметры агента: --config, --dry-run, --max-ticks")

    dash = sub.add_parser("dashboard", help="панель Штаба (Streamlit)")
    common(dash)
    dash.add_argument("--scenario", default=None, help="сценарий симулятора; без него — реальный адаптер")
    dash.add_argument("--speed", type=float, default=1.0)
    dash.add_argument("--seed", type=int, default=42)
    dash.add_argument("--port", type=int, default=8501)
    return parser


def _prepare(args: argparse.Namespace) -> AppConfig:
    """Загрузить .env и конфиг, применить переопределения командной строки."""
    load_env_file()
    config = load_config(args.config)
    if args.llm_mode:
        config = AppConfig.model_validate({**config.model_dump(), "llm": {**config.llm.model_dump(), "mode": args.llm_mode}})
    if args.logs_dir:
        config.paths.logs_dir = args.logs_dir
    return config


def _print_summary(console: Console, summary: dict[str, object]) -> None:
    """Короткий итог прогона в консоль."""
    table = Table(title="Итог прогона", show_header=False)
    counters = summary.get("counters", {})
    assert isinstance(counters, dict)
    table.add_row("Каталог", str(summary.get("run_dir")))
    table.add_row("Игровое время", f"{summary.get('elapsed_s')} с из {summary.get('duration_s')} с")
    table.add_row("LLM", f"{summary.get('llm_mode')} / {summary.get('llm_model')}")
    table.add_row("Тревоги", f"critical={counters.get('alerts_critical')}, warning={counters.get('alerts_warning')}")
    table.add_row("Вердикты сверки", str(counters.get("verdicts")))
    table.add_row("Рекомендации", f"{counters.get('recommendations')} (резервных запусков агентов: {counters.get('agent_fallbacks')})")
    table.add_row("Ошибки шагов", f"{counters.get('step_errors')} (ошибок опроса: {counters.get('poll_errors')})")
    console.print(table)


def cmd_simulate(args: argparse.Namespace, console: Console) -> int:
    """Команда ``simulate``."""
    config = _prepare(args)
    scenario = load_scenario(args.scenario)
    sim = SimulatorAdapter(config, scenario, seed=args.seed)
    llm = LLMClient(config.llm)
    if args.llm_fail:
        if llm.mode != "mock":
            console.print("[red]--llm-fail работает только в режиме mock[/red]")
            return EXIT_USAGE
        llm.simulate_failure = args.llm_fail
    run_dir = new_run_dir(config.paths.logs_dir, scenario.name)
    engine = Engine(
        config,
        sim,
        run_dir,
        llm=llm,
        auto_approve=args.auto_approve,
        sim_operator=args.sim_operator,
        agent_workers=0 if llm.mode == "mock" else 2,
        duration_s=args.duration or scenario.duration_s,
        meta={"mode": "simulate", "scenario": scenario.name, "seed": args.seed, "speed": args.speed, "auto_approve": args.auto_approve, "sim_operator": args.sim_operator},
    )
    console.print(f"Симуляция [bold]{scenario.name}[/bold], seed={args.seed}, LLM={llm.mode}, журнал: {run_dir}")
    started = time.monotonic()
    summary = engine.run(speed=args.speed)
    console.print(f"Готово за {time.monotonic() - started:.1f} с (реального времени).")
    _print_summary(console, summary)
    console.print(f"Отчёт: python -m kupol report {run_dir}")
    return 0


def cmd_run(args: argparse.Namespace, console: Console) -> int:
    """Команда ``run`` (боевой режим)."""
    config = _prepare(args)
    adapter = RealAdapter(config.real)
    try:
        adapter.get_telemetry()
    except NotImplementedError as exc:
        console.print(f"[red]{exc}[/red]\nПошагово: docs/MORNING_CHECKLIST.md")
        return EXIT_USAGE
    except AdapterError as exc:
        console.print(f"[yellow]Предупреждение: пробный запрос телеметрии не удался ({escape(str(exc))}); цикл продолжит попытки.[/yellow]")
    llm = LLMClient(config.llm)
    if llm.mode == "mock":
        console.print("[yellow]LLM в режиме mock: реальная модель не используется (для игры нужен llm.mode: competition).[/yellow]")
    run_dir = new_run_dir(config.paths.logs_dir, "run")
    engine = Engine(config, adapter, run_dir, llm=llm, agent_workers=2, meta={"mode": "run", "config": args.config})
    console.print(f"Боевой режим, LLM={llm.mode}/{llm.model}, журнал: {run_dir}. Остановка: Ctrl+C.")
    summary = engine.run(speed=1.0)
    _print_summary(console, summary)
    return 0


def cmd_replay(args: argparse.Namespace, console: Console) -> int:
    """Команда ``replay``."""
    last = 0.0
    for t, line in replay_lines(args.run_dir, args.every):
        if args.speed > 0 and t > last:
            time.sleep((t - last) / args.speed)
        last = max(last, t)
        console.print(line, markup=False)
    return 0


def cmd_report(args: argparse.Namespace, console: Console) -> int:
    """Команда ``report``."""
    text = build_report(args.run_dir)
    out = Path(args.out) if args.out else Path(args.run_dir) / "report.md"
    out.write_text(text, encoding="utf-8")
    console.print(f"Отчёт сохранён: {out}")
    return 0


def cmd_benchmark(args: argparse.Namespace, console: Console) -> int:
    """Команда ``benchmark``."""
    from kupol.benchmark import parse_seeds, run_benchmark, summarize

    config = load_config(args.config)
    seeds = parse_seeds(args.seeds)
    root = Path(args.scenarios)
    files = [root] if root.is_file() else sorted(root.glob("*.yaml"))
    if args.only:
        wanted = {w.strip() for w in args.only.split(",")}
        files = [f for f in files if f.stem in wanted]
    if not files:
        console.print("[red]Сценарии не найдены[/red]")
        return EXIT_USAGE
    workers = args.workers or min(os.cpu_count() or 1, 6)
    console.print(f"Бенчмарк: {len(files)} сценариев × {len(seeds)} seed, процессов: {workers}")
    started = time.monotonic()
    results = run_benchmark(args.config, files, seeds, workers=workers, sim_operator=args.sim_operator)
    text = summarize(results, config.quality.max_quiet_warnings_per_hour, seeds)
    console.print(text, markup=False)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        console.print(f"Сводка сохранена: {out} (за {time.monotonic() - started:.0f} с)")
    return 0


def cmd_dashboard(args: argparse.Namespace, console: Console) -> int:
    """Команда ``dashboard``: запускает Streamlit с панелью."""
    load_env_file()
    app = Path(__file__).parent / "ui" / "dashboard.py"
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app),
        "--server.port",
        str(args.port),
        "--server.showEmailPrompt",
        "false",
        "--browser.gatherUsageStats",
        "false",
        "--",
        "--config",
        args.config,
        "--speed",
        str(args.speed),
        "--seed",
        str(args.seed),
    ]
    if args.scenario:
        cmd += ["--scenario", args.scenario]
    if args.llm_mode:
        cmd += ["--llm-mode", args.llm_mode]
    if args.logs_dir:
        cmd += ["--logs-dir", args.logs_dir]
    console.print(f"Панель Штаба: http://localhost:{args.port}  (Ctrl+C — остановить)")
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:
        return 0


def cmd_dome(args: argparse.Namespace, console: Console) -> int:
    """Запустить агента соревновательного Купола."""
    from kupol.dome.app import main as dome_main

    return dome_main(args.rest)


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа CLI.

    Args:
        argv: Аргументы командной строки; по умолчанию ``sys.argv[1:]``.

    Returns:
        Код возврата процесса: 0 — успех, 2 — ошибка конфигурации или использования.
    """
    console = _setup_console()
    args = build_parser().parse_args(argv)
    handlers = {
        "simulate": cmd_simulate,
        "run": cmd_run,
        "replay": cmd_replay,
        "report": cmd_report,
        "dashboard": cmd_dashboard,
        "benchmark": cmd_benchmark,
        "dome": cmd_dome,
    }
    try:
        return handlers[args.command](args, console)
    except (ConfigError, ScenarioError, LLMError, FileNotFoundError) as exc:
        console.print(f"[red]Ошибка:[/red] {escape(str(exc))}")
        return EXIT_USAGE
    except NotImplementedError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]\nПошагово: docs/MORNING_CHECKLIST.md")
        return EXIT_USAGE
