"""Подозрительные наблюдения, которые модель записывает «на будущее».

Не более одного за решение (это проверяет схема ``AgentDecision.suspicion``); здесь — хранение между запусками
(``state/suspicions.jsonl``), отбор открытых записей для промпта и защита от повторов. Записи в БД уходят
событием ``agent_suspicion`` из ``LlmBrain``. Команд в Купол это не создаёт.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

TTL_S = 24 * 3600.0  # столько живёт открытая запись, потом перестаёт попадать в промпт
SHOWN = 8  # сколько записей показывать модели
KEEP = 200


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


class SuspicionStore:
    def __init__(self, path: Path | None = None, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        self._lock = threading.Lock()
        self._items: list[dict[str, Any]] = []
        if path is not None and path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines()[-KEEP:]:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and item.get("text"):
                    self._items.append(item)

    def add(self, suspicion: dict[str, Any], *, dome_time: str | None = None, decision_id: str | None = None) -> dict[str, Any] | None:
        """Записать подозрение. ``None`` — такое открытое уже есть (дубль не пишется)."""
        key = (suspicion.get("node_id"), suspicion.get("param"), _norm(str(suspicion.get("text", ""))))
        with self._lock:
            now = self.clock()
            for old in self._open(now):
                if (old.get("node_id"), old.get("param"), _norm(old["text"])) == key:
                    return None
            item = {
                "id": f"susp-{uuid.uuid4().hex[:10]}", "at": now, "dome_time": dome_time, "status": "open", "decision_id": decision_id,
                **{k: v for k, v in suspicion.items() if v not in (None, "")},
            }
            self._items.append(item)
            del self._items[:-KEEP]
            if self.path is not None:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(item, ensure_ascii=False) + "\n")
                except OSError:
                    pass  # заметка в памяти остаётся; запись на диск не должна ронять агента
            return item

    def _open(self, now: float) -> list[dict[str, Any]]:
        return [i for i in self._items if i.get("status") == "open" and now - float(i.get("at", 0)) < TTL_S]

    def open(self, limit: int = SHOWN) -> list[dict[str, Any]]:
        """Открытые записи, новые последними — для промпта."""
        with self._lock:
            return list(self._open(self.clock())[-limit:])
