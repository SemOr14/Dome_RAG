"""Каталог узлов из ``describe_nodes``: что реально разрешено команде (главный источник прав)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class NodeInfo:
    """Описание узла из каталога."""

    node_id: str
    title: str = ""
    controls: set[str] = field(default_factory=set)  # основные имена, нижний регистр
    aliases: dict[str, str] = field(default_factory=dict)  # алиас -> основное имя
    operator_controls: set[str] = field(default_factory=set)
    queries: set[str] = field(default_factory=set)
    service_actions: set[str] = field(default_factory=set)


class Catalog:
    """Разрешённые действия по узлам."""

    def __init__(self, nodes: dict[str, NodeInfo] | None = None) -> None:
        self.nodes: dict[str, NodeInfo] = nodes or {}

    @classmethod
    def from_response(cls, message: dict[str, Any]) -> Catalog:
        """Построить каталог из ответа ``describe_nodes_response``."""
        out: dict[str, NodeInfo] = {}
        for item in message.get("nodes", []) or []:
            if not isinstance(item, dict) or "node_id" not in item:
                continue
            aliases: dict[str, str] = {}
            for main, names in (item.get("control_aliases") or {}).items():
                for alias in names or []:
                    aliases[str(alias).lower()] = str(main).lower()
            out[item["node_id"]] = NodeInfo(
                node_id=item["node_id"],
                title=str(item.get("title", "")),
                controls={str(c).lower() for c in item.get("controls", []) or []},
                aliases=aliases,
                operator_controls={str(c).lower() for c in item.get("operator_controls", []) or []},
                queries={str(c).lower() for c in item.get("queries", []) or []},
                service_actions={str(c).lower() for c in item.get("service_actions", []) or []},
            )
        return cls(out)

    def __bool__(self) -> bool:
        return bool(self.nodes)

    def resolve(self, node_id: str, action: str) -> str | None:
        """Основное имя действия (алиасы приводятся) либо ``None``, если действие недоступно."""
        info = self.nodes.get(node_id)
        if info is None:
            return None
        name = action.lower()
        name = info.aliases.get(name, name)
        if name in info.controls or name in info.service_actions:
            return name
        return None

    def is_operator_only(self, node_id: str, action: str) -> bool:
        """Действие — физическое действие Оператора (агент может только попросить)."""
        info = self.nodes.get(node_id)
        return info is not None and action.lower() in info.operator_controls
