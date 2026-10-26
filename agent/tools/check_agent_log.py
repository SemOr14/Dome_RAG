"""Проверка лога агента по формату организаторов (03_Требования_к_агенту.md §5).

    python tools/check_agent_log.py logs/agent_20261024-133005.jsonl

Проверяет: JSON Lines в UTF-8, обязательные поля, нумерацию ``seq``, ссылки ``ref`` (action → decision,
verification → action, исходящие message → decision), наличие ``verification`` после команд, меняющих
состояние, отсутствие ключей. Код возврата 0 — замечаний нет.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

REQUIRED = {"ts", "run_id", "seq", "kind"}
KINDS = {"start", "stop", "connection", "observation", "decision", "action", "verification", "message", "llm_call", "error"}
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}$")
KIND_FIELDS = {
    "start": {"agent", "version", "models", "config"},
    "stop": {"reason"},
    "connection": {"event"},
    "observation": {"summary", "metrics", "anomalies"},
    "decision": {"situation", "cause", "confidence", "evidence", "plan", "source"},
    "action": {"request", "response"},
    "verification": {"ref", "expected", "observed", "ok"},
    "message": {"direction", "text"},
    "llm_call": {"model", "purpose", "ok"},
    "error": {"where", "error"},
}
STATE_FREE_NODES = {"meeting_01"}


def check(path: Path, secrets: list[str] | None = None) -> list[str]:
    """Вернуть список замечаний к файлу лога."""
    problems: list[str] = []
    rows: list[dict[str, Any]] = []
    text = path.read_text(encoding="utf-8")
    for n, line in enumerate(text.splitlines(), 1):
        try:
            row = json.loads(line)
        except ValueError:
            problems.append(f"строка {n}: не JSON")
            continue
        rows.append(row)
    by_seq = {r.get("seq"): r for r in rows}
    run_ids = {r.get("run_id") for r in rows}
    if len(run_ids) > 1:
        problems.append(f"несколько run_id в одном файле: {run_ids}")
    if path.stem != f"agent_{next(iter(run_ids), '')}":
        problems.append("имя файла не соответствует run_id (agent_<run_id>.jsonl)")
    for i, r in enumerate(rows, 1):
        where = f"seq {r.get('seq')}"
        missing = REQUIRED - r.keys()
        if missing:
            problems.append(f"{where}: нет полей {sorted(missing)}")
            continue
        if r["seq"] != i:
            problems.append(f"{where}: нарушена нумерация (ожидался {i})")
        if not ISO.match(str(r["ts"])):
            problems.append(f"{where}: ts не ISO 8601 с миллисекундами и поясом: {r['ts']}")
        if r["kind"] not in KINDS:
            problems.append(f"{where}: неизвестный kind {r['kind']}")
            continue
        miss = KIND_FIELDS[r["kind"]] - r.keys()
        if miss:
            problems.append(f"{where} ({r['kind']}): нет полей {sorted(miss)}")
        ref = r.get("ref")
        if r["kind"] == "action":
            if ref is None or by_seq.get(ref, {}).get("kind") != "decision":
                problems.append(f"{where}: action без ref на decision")
        if r["kind"] == "verification" and by_seq.get(ref, {}).get("kind") != "action":
            problems.append(f"{where}: verification без ref на action")
        if r["kind"] == "message" and r.get("direction") == "out" and by_seq.get(ref, {}).get("kind") != "decision":
            problems.append(f"{where}: исходящее сообщение без ref на decision")
    verified = {r.get("ref") for r in rows if r["kind"] == "verification"}
    for r in rows:
        if r["kind"] != "action" or r.get("dry_run") or r.get("rejected"):
            continue
        req, resp = r.get("request", {}), r.get("response", {})
        if req.get("type") == "control" and req.get("node_id") not in STATE_FREE_NODES and resp.get("success") and r["seq"] not in verified:
            problems.append(f"seq {r['seq']}: после команды {req.get('node_id')}.{req.get('action')} нет verification")
    for s in secrets or []:
        if s and s in text:
            problems.append("в логе найден секрет (ключ)")
    return problems


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    problems = check(path)
    for p in problems:
        print("ЗАМЕЧАНИЕ:", p)
    print(f"{path.name}: {'замечаний нет' if not problems else f'замечаний: {len(problems)}'}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
