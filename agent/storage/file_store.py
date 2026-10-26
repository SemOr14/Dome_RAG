"""Файловые заглушки хранилищ: JSON/JSONL в каталоге data/. Заменяются реализацией на БД."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # битая строка не должна ронять агента
    return rows


class FileTelemetryRepo:
    def __init__(self, snapshot: Path, history: Path | None = None):
        self.snapshot = snapshot
        self.history_file = history

    def latest(self) -> dict:
        if not self.snapshot.is_file():
            return {"nodes": {}, "game_time": None}
        data = json.loads(self.snapshot.read_text(encoding="utf-8"))
        # Принимаем и сообщение telemetry целиком, и только его data
        return data.get("data", data)

    def history(self, node_id: str, param: str, limit: int = 20) -> list[dict]:
        if not self.history_file:
            return []
        rows = [r for r in _read_jsonl(self.history_file)
                if r.get("node_id") == node_id and r.get("param") == param]
        return rows[-limit:]


class FileDecisionJournalRepo:
    def __init__(self, path: Path):
        self.path = path

    def recent(self, limit: int = 15) -> list[dict]:
        return _read_jsonl(self.path)[-limit:]

    def append(self, record: dict) -> None:
        record = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), **record}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


class FileCatalogRepo:
    def __init__(self, path: Path):
        self.path = path

    def nodes(self) -> list[dict]:
        if not self.path.is_file():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return data.get("nodes", data) if isinstance(data, dict) else data
