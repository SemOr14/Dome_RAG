"""Ручные команды Оператора: веб-интерфейс → сервис БД → этот агент → Gate → Купол.

Интерфейс не подключается к Куполу сам: соединение с ключом команды одно, и все команды идут через Gate
агента (политика ``dome.yaml``, защитные правила, режим «только чтение», проверка эффекта). Интерфейс
записывает намерение ``control_intent`` с ``source=operator`` через ``POST /v1/controls/validate``; сервер
проверяет его по каталогу и свежему состоянию сессии. Принятые сервером намерения запуска агент забирает
из ``GET /v1/controls`` и исполняет; итог пишет событием ``control_dispatch`` (контракт —
``contracts/manual-control.md``).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from kupol.dome.datalog import DataApiRecorder

MAX_AGE_S = 60.0  # старше — не исполнять: состояние купола могло измениться (и после перезапуска агента)


def _age_s(ingested_at: Any, now: float) -> float | None:
    try:
        return now - datetime.fromisoformat(str(ingested_at).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class ManualControls:
    """Очередь ручных команд Оператора из сервиса БД для текущего запуска."""

    def __init__(self, recorder: DataApiRecorder, max_age_s: float = MAX_AGE_S, clock: Callable[[], float] = time.time) -> None:
        self.recorder = recorder
        self.max_age_s = max_age_s
        self.clock = clock
        self.cursor: int | None = None  # None — начать с текущего конца: прошлые команды не исполняются

    def poll(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Новые команды Оператора, принятые сервером: (к исполнению, устаревшие). Блокирующий вызов."""
        fresh: list[dict[str, Any]] = []
        stale: list[dict[str, Any]] = []
        start = self.cursor
        while True:
            page = self.recorder.get("/v1/controls", after_id=start or 0, run_id=self.recorder.run_id, actor="operator",
                                     validation="accepted", limit=100)  # fmt: skip
            items = page.get("items") or []
            if not items:
                break
            start = page["next_after_id"]
            if self.cursor is None:
                continue  # первый опрос только находит конец очереди
            for item in items:
                ev = item.get("event") or {}
                if ev.get("event_type") != "control_intent" or not isinstance(ev.get("payload"), dict):
                    continue
                age = _age_s(ev.get("ingested_at"), self.clock())
                (stale if age is None or age > self.max_age_s else fresh).append(item)
        self.cursor = start or 0
        return fresh, stale
