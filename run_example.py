"""Пример: собрать промпт из заглушек, отправить в DeepSeek и напечатать структурированный ответ.

    python run_example.py --dry-run                       # только показать промпт
    python run_example.py --scenario co2_false_alarm      # вызвать модель
    python run_example.py --scenario bed_occupied --trigger "Оператор: напечатай gear.gcode"
"""

from __future__ import annotations

import argparse
import json
import sys

from llm_agent import DomeLLMAgent, Settings

TRIGGERS = {
    "co2_false_alarm": "IL_CO2 сработал, принтер и фрезер встали на удержание. Разберись и верни производство в работу.",
    "bed_occupied": "Оператор в чате: «Печать корпуса готова? Запусти следующую — gear.gcode».",
    "evening_low_soc": "Плановая проверка: вечер, заряд АКБ снижается. Оцени риски для производства.",
}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scenario", default="co2_false_alarm", choices=sorted(TRIGGERS))
    p.add_argument("--trigger", help="повод для анализа (по умолчанию — из сценария)")
    p.add_argument("--dry-run", action="store_true", help="только собрать и показать промпт")
    p.add_argument("--model", help="переопределить LLM_MODEL")
    p.add_argument("--source", choices=["auto", "files", "data-api"], default="auto",
                   help="откуда данные: файлы data/ или сервис БД (auto — БД, если задан DATA_API_URL)")
    p.add_argument("--record", action="store_true", help="дописать решение в журнал решений")
    args = p.parse_args()

    settings = Settings.from_env()
    if args.model:
        settings.model = args.model
    if args.source == "data-api" or (args.source == "auto" and settings.data_api_url):
        agent = DomeLLMAgent.from_data_api(settings)
    else:
        snapshot = settings.data_dir / "samples" / "scenarios" / f"{args.scenario}.json"
        agent = DomeLLMAgent.from_files(settings, snapshot_file=snapshot)
    trigger = args.trigger or TRIGGERS[args.scenario]

    prompt = agent.build_prompt(trigger)
    print("=== Статистика промпта ===")
    print(json.dumps(prompt.stats, ensure_ascii=False, indent=2))
    if args.dry_run:
        for m in prompt.messages:
            print(f"\n=== {m['role'].upper()} ===\n{m['content']}")
        return 0

    response = agent.decide(trigger, purpose=f"сценарий {args.scenario}")
    print("\n=== Вызов LLM ===")
    print(response.llm_call.model_dump_json(indent=2))
    if not response.ok:
        print("\nМодель не дала валидного ответа — агент продолжает работу по правилам.")
        if response.raw_text:
            print(response.raw_text[:2000])
        return 1
    print("\n=== Решение ===")
    print(response.decision.model_dump_json(indent=2))
    print("\n=== Проверка действий ===")
    for v in response.verdicts:
        mark = "OK " if v.accepted else "NO "
        print(f"{mark} {v.action.node_id}.{v.action.action}({json.dumps(v.action.value, ensure_ascii=False)})"
              + ("" if v.accepted else f"  ← {'; '.join(v.reasons)}"))
    if args.record:
        agent.record(response, prompt.dome_time)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
