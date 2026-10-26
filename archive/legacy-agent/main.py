"""Точка входа: python main.py (без интерактива, без подтверждений)."""
from __future__ import annotations

import asyncio
import os
import signal
import sys

from agent_log import AgentLog
from brain import Brain
from config import AGENT_NAME, VERSION, Config
from dome import DomeClient
from llm import LLM
from rag import Rag


def build_rag(cfg: Config) -> Rag:
    rag = Rag()
    if not cfg.rag_enabled:
        return rag
    knowledge = cfg.knowledge_dir
    journal = cfg.journal_dir
    try:
        rag.build(knowledge_dir=knowledge, journal_dir=journal)
    except Exception as exc:  # noqa: BLE001 — без RAG агент работает
        print(f"[rag] не удалось построить индекс: {exc}")
    print(f"[rag] фрагментов: {len(rag.chunks)}")
    return rag


async def amain() -> int:
    cfg = Config()
    problems = cfg.check()
    for problem in problems:
        print(f"[config] {problem}")
    if not cfg.dome_key:
        return 2

    log = AgentLog(cfg.log_dir)
    print(f"[agent] run_id={log.run_id} лог={log.path}")
    log.write(
        "start",
        agent=AGENT_NAME,
        version=VERSION,
        models=[cfg.model] if cfg.llm_enabled else [],
        config=cfg.public(),
    )

    rag = build_rag(cfg)
    llm = LLM(cfg, log)
    brain_holder: dict[str, Brain | None] = {"brain": None}

    dome = DomeClient(cfg, log)
    brain = Brain(cfg, log, dome, llm, rag)
    brain_holder["brain"] = brain
    dome.on_telemetry = brain.on_telemetry
    dome.on_message = brain.on_message
    dome.on_role = lambda role, team: asyncio.ensure_future(brain.on_role(role, team))

    stop = asyncio.Event()

    def request_stop(*_: object) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except (NotImplementedError, RuntimeError):
            try:
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(request_stop))
            except (ValueError, OSError):
                pass

    dome_task = asyncio.create_task(dome.run(), name="dome")
    brain_task = asyncio.create_task(brain.run(), name="brain")

    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    finally:
        print("[agent] остановка")
        await brain.stop()
        await dome.stop()
        brain_task.cancel()
        dome_task.cancel()
        for task in (brain_task, dome_task):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        log.write("stop", reason="signal")
        log.close()
    return 0


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
