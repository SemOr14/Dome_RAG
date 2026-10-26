"""Интерфейсы хранилищ: файловые заглушки (file_store.py) и сервис БД/API (data_api.py)."""

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


class EventSink(Protocol):
    """Журнал всего входа/выхода агента (обмен с LLM, решения) — конверт contracts/event.schema.json."""

    def emit(self, event_type: str, payload, *, source: str = "agent", channel: str = "internal",
             direction: str = "internal", request_id=None, node_id: str | None = None,
             metadata: dict | None = None) -> str | None:
        """Возвращает event_id записанного события (None — запись выключена)."""


class ControlChecker(Protocol):
    def check(self, node_id: str, action: str, value, metadata: dict) -> dict:
        """Предпроверка команды до отправки: {status, reasons, intent_event_id, request_id}."""


class NullEventSink:
    def emit(self, event_type, payload, **kwargs) -> None:
        return None
