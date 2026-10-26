"""Лог агента по формату организаторов (``03_Требования_к_агенту.md`` §5).

Файл ``agent_<run_id>.jsonl``: одна строка — один JSON-объект, UTF-8, построчный flush. Поля каждой
записи: ``ts`` (ISO с миллисекундами и поясом), ``run_id``, ``seq``, ``kind``, ``dome_time``, ``ref``.
Ключи в лог не попадают: известные секреты вычищаются из любой строки.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

KINDS = {
    "start", "stop", "connection", "observation", "decision", "action",
    "verification", "message", "llm_call", "error",
}  # fmt: skip

REDACTED = "***"


class AgentLog:
    """Потокобезопасный писатель лога агента."""

    def __init__(
        self,
        log_dir: str | Path = "logs",
        secrets: Iterable[str] = (),
        run_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Открыть новый файл лога.

        Args:
            log_dir: Каталог логов (``AGENT_LOG_DIR``).
            secrets: Значения, которые нельзя писать в лог (ключи).
            run_id: Идентификатор запуска (по умолчанию время запуска ``ГГГГММДД-ЧЧММСС``).
            clock: Источник времени (для тестов).
        """
        self._clock = clock or (lambda: datetime.now().astimezone())
        self.run_id = run_id or self._clock().strftime("%Y%m%d-%H%M%S")
        folder = Path(log_dir)
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / f"agent_{self.run_id}.jsonl"
        self._fh = self.path.open("a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()
        self._secrets = [s for s in secrets if s and len(s) >= 6]
        self.seq = 0
        self.dome_time: str | None = None  # обновляется рантаймом из game_time телеметрии

    def set_dome_time(self, game_time: dict[str, Any] | None) -> None:
        """Запомнить часы купола (``HH:MM``) для следующих записей."""
        if not game_time or game_time.get("hour") is None:
            return
        try:
            hour = float(game_time["hour"])
        except (TypeError, ValueError):
            return
        self.dome_time = f"{int(hour) % 24:02d}:{int((hour % 1) * 60):02d}"

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text

    def write(self, kind: str, *, ref: int | None = None, **fields: Any) -> int:
        """Записать строку лога.

        Args:
            kind: Тип записи (см. ``KINDS``).
            ref: ``seq`` записи, к которой относится эта (решение → действие → проверка).
            **fields: Поля по типу записи; ``None`` пропускаются.

        Returns:
            ``seq`` записанной строки (для ссылок ``ref``).
        """
        with self._lock:
            self.seq += 1
            record: dict[str, Any] = {
                "ts": self._clock().isoformat(timespec="milliseconds"),
                "run_id": self.run_id,
                "seq": self.seq,
                "kind": kind,
            }
            if self.dome_time:
                record["dome_time"] = self.dome_time
            if ref is not None:
                record["ref"] = ref
            record.update({k: v for k, v in fields.items() if v is not None})
            line = self._scrub(json.dumps(record, ensure_ascii=False, default=str))
            self._fh.write(line + "\n")
            self._fh.flush()
            return self.seq

    def close(self) -> None:
        """Закрыть файл (повторный вызов безопасен)."""
        with self._lock:
            if not self._fh.closed:
                self._fh.flush()
                self._fh.close()
