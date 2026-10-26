"""Интерфейсы хранилищ. Сейчас реализованы файловые заглушки (file_store.py), позже — БД."""

from __future__ import annotations

from typing import Protocol


class TelemetryRepo(Protocol):
    def latest(self) -> dict:
        """Последний снимок телеметрии: {"timestamp", "nodes": {node_id: {...}}, "game_time": {...}}."""

    def history(self, node_id: str, param: str, limit: int = 20) -> list[dict]:
        """Ряд значений [{"ts", "value"}] — для трендов. Может вернуть пустой список."""


class DecisionJournalRepo(Protocol):
    def recent(self, limit: int = 15) -> list[dict]:
        """Последние записи журнала решений агента и действий человека, по возрастанию времени.

        Запись: {"ts", "dome_time"?, "actor": "agent"|"operator"|"human", "kind", "text",
                 "node_id"?, "action"?, "value"?, "success"?, "outcome"?}
        """

    def append(self, record: dict) -> None:
        ...


class CatalogRepo(Protocol):
    def nodes(self) -> list[dict]:
        """Каталог в формате ответа describe_nodes (01_API §5.2)."""
