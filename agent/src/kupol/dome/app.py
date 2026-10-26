"""Запуск агента одной командой: ``python main.py`` (или ``python -m kupol dome``).

Без ввода с клавиатуры. Настройки — из окружения/``.env``/файла ключей (см. ``env.py``). По умолчанию
агент работает в режиме «только чтение»: решения пишутся в лог, команды в купол не уходят, пока
пользователь сам не выставил ``DOME_ALLOW_CONTROL=1``.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kupol.dome.agentlog import AgentLog
from kupol.dome.config import DomeConfig, DomeConfigError, load_dome_config
from kupol.dome.env import Settings, export_to_environ, load_settings
from kupol.dome.llm import DeepSeek, LlmAdvisor
from kupol.dome.rag import build_rag
from kupol.dome.runner import Runner

BRAINS = ("llm", "rules")

EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="main.py", description="Агент Штаба для соревновательного Купола")
    p.add_argument("--config", default="dome.yaml", help="конфиг рантайма")
    p.add_argument("--dry-run", action="store_true", help="всё анализировать и писать в лог, но не отправлять команды (как AGENT_DRY_RUN=1)")
    p.add_argument("--max-ticks", type=int, default=None, help="остановиться после N телеметрий (для проверок)")
    return p


def _install_signals() -> None:
    def stop(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, stop)
            except (ValueError, OSError):
                pass


def build_advisor(settings: Settings, cfg: DomeConfig, log: AgentLog) -> tuple[LlmAdvisor, list[str]]:
    """RAG по материалам организаторов + DeepSeek (если есть ключ и советник не выключен)."""
    rag = build_rag(settings.rag_dir, cfg.rag.chunk_chars) if Path(settings.rag_dir).is_dir() else None
    llm: DeepSeek | None = None
    if settings.deepseek_key and cfg.advisor.enabled:
        llm = DeepSeek(settings.deepseek_key, settings.deepseek_base_url, settings.deepseek_model or cfg.advisor.model, log, cfg.advisor)
    return LlmAdvisor(rag, llm), ([llm.model] if llm else [])


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа. Возвращает код процесса (0 — штатно, 2 — ошибка настройки)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    settings: Settings = load_settings()
    if args.dry_run:
        settings.dry_run = True
    try:
        cfg = load_dome_config(args.config)
    except DomeConfigError as exc:
        print(f"Ошибка конфигурации: {exc}")
        return EXIT_USAGE
    if not settings.dome_key:
        print("Нет ключа купола: задайте DOME_KEY (окружение/.env) или файл ключей (KEYS_FILE, строка «team: ...»).")
        return EXIT_USAGE
    export_to_environ(settings)
    log = AgentLog(settings.log_dir, secrets=[settings.dome_key, settings.deepseek_key])
    mode = "ТОЛЬКО ЧТЕНИЕ (команды не отправляются)" if not settings.can_control else "УПРАВЛЕНИЕ ВКЛЮЧЕНО"
    print(f"Агент Штаба: {settings.dome_url}, режим: {mode}, лог: {Path(log.path)}")
    if settings.brain not in BRAINS:
        print(f"AGENT_BRAIN={settings.brain!r}: допустимо {', '.join(BRAINS)}")
        return EXIT_USAGE
    advisor, models = build_advisor(settings, cfg, log)
    brain = None
    if settings.brain == "llm" and settings.deepseek_key:
        from kupol.dome.brain import LlmBrain

        def brain(r: Runner) -> LlmBrain:
            return LlmBrain(settings, log, r.state, lambda: r.client.telemetry, lambda: r.client.session_id, r.recorder)

        print("Решения: LLM-агент Купола (llm_agent/), при недоступности модели — плейбуки")
    else:
        why = "AGENT_BRAIN=rules" if settings.brain == "rules" else "нет ключа DeepSeek"
        print(f"Решения: плейбуки ({why})")
    runner = Runner(settings, cfg, log, advisor=advisor, models=models, brain=brain)
    _install_signals()
    try:
        asyncio.run(runner.run(max_ticks=args.max_ticks))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
