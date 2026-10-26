"""Диспетчер LLM-слоя: вызывает модель по событиям и контрольным таймерам (rag/AGENT_TRIGGERS.md).

    python run_dispatcher.py --dry-run --once          # какие поводы видны сейчас, без вызова модели
    python run_dispatcher.py                           # постоянная работа (данные из БД, если задан DATA_API_URL)
    python run_dispatcher.py --source files --scenario co2_false_alarm --once

Команды в Купол диспетчер не отправляет: принятые действия исполняет агентная система после своего Gate.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys

from llm_agent import DomeLLMAgent, Settings
from llm_agent.dispatcher import Dispatcher, Fired
from llm_agent.storage.data_api import ApiEventFeed


def show(fired: Fired) -> None:
    print(f"\n=== Вызов: {', '.join(sorted({t.kind for t in fired.triggers}))} ===")
    print(fired.text)
    resp = fired.response
    if resp is None:
        return
    if not resp.ok:
        print(f"модель не ответила: {resp.llm_call.error} — повод остаётся в очереди")
        return
    print(f"решение {resp.decision_id}: {resp.decision.situation}")
    for v in resp.verdicts:
        mark = "OK " if v.accepted else "NO "
        print(f"  {mark}{v.action.node_id}.{v.action.action}({json.dumps(v.action.value, ensure_ascii=False)})"
              + ("" if v.accepted else f"  ← {'; '.join(v.reasons)}"))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", choices=["auto", "files", "data-api"], default="auto")
    p.add_argument("--scenario", default="co2_false_alarm", help="снимок data/samples/scenarios/ для --source files")
    p.add_argument("--dry-run", action="store_true", help="только найти поводы, модель не вызывать")
    p.add_argument("--once", action="store_true", help="один цикл и выход")
    args = p.parse_args()

    settings = Settings.from_env()
    feed = None
    if args.source == "data-api" or (args.source == "auto" and settings.data_api_url):
        agent = DomeLLMAgent.from_data_api(settings)
        dispatcher = Dispatcher(agent, dry_run=args.dry_run)
        feed = ApiEventFeed(agent.telemetry.client, agent.telemetry.run_id, dispatcher.saved_cursors)
        dispatcher.feed = feed
    else:
        # Решения пишутся в state/, чтобы не менять образец журнала в data/samples/
        journal = settings.state_dir / "decision_journal.jsonl"
        if not journal.is_file():
            journal.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(settings.data_dir / "samples" / "decision_journal.jsonl", journal)
        snapshot = settings.data_dir / "samples" / "scenarios" / f"{args.scenario}.json"
        agent = DomeLLMAgent.from_files(settings, snapshot_file=snapshot, journal_file=journal)
        dispatcher = Dispatcher(agent, dry_run=args.dry_run)

    if args.once:
        fired = dispatcher.tick()
        for f in fired:
            show(f)
        if not fired:
            print("Поводов для вызова модели нет")
        return 0
    print(f"Диспетчер запущен: {json.dumps(settings.public(), ensure_ascii=False)}", file=sys.stderr)
    try:
        dispatcher.run(on_fire=show)
    except KeyboardInterrupt:
        pass
    print(f"\nИтог: {json.dumps(dispatcher.stats, ensure_ascii=False)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
