"""Журнал агента в формате требований (JSON Lines, один объект на строку).

Каждая запись: ts, run_id, seq, kind (+ dome_time, ref).
Файл: <AGENT_LOG_DIR>/agent_<run_id>.jsonl, построчная запись с flush.
"""
from __future__ import annotations

import json
import os
from datetime import datetime


class AgentLog:
    def __init__(self, log_dir: str, run_id: str | None = None) -> None:
        os.makedirs(log_dir, exist_ok=True)
        self.run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = os.path.join(log_dir, f"agent_{self.run_id}.jsonl")
        self._fh = open(self.path, "a", encoding="utf-8", buffering=1)
        self.seq = 0
        self._dome_time: str | None = None
        self._closed = False

    def set_dome_time(self, value: str | None) -> None:
        self._dome_time = value

    def write(self, kind: str, **fields) -> int:
        """Пишет запись и возвращает её seq (для ссылок через ref)."""
        if self._closed:
            return -1
        self.seq += 1
        record = {
            "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
            "seq": self.seq,
            "kind": kind,
        }
        if self._dome_time:
            record["dome_time"] = self._dome_time
        record.update({k: v for k, v in fields.items() if v is not None})
        try:
            self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except (OSError, ValueError) as exc:  # лог не должен ронять агента
            print(f"[log] не удалось записать запись: {exc}")
            return -1
        return self.seq

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._fh.flush()
            self._fh.close()
        except OSError:
            pass
