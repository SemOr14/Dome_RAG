"""Каталог узлов (формат describe_nodes): индекс, разрешение алиасов, проверка прав."""

from __future__ import annotations


class NodeCatalog:
    def __init__(self, nodes: list[dict]):
        self.by_id: dict[str, dict] = {n["node_id"]: n for n in nodes if "node_id" in n}

    def get(self, node_id: str) -> dict | None:
        return self.by_id.get(node_id)

    def canonical_action(self, node_id: str, action: str) -> str | None:
        """Основное имя действия по имени или алиасу (регистр не важен), иначе None."""
        node = self.by_id.get(node_id)
        if not node:
            return None
        wanted = action.lower()
        for name in node.get("controls", []):
            if name.lower() == wanted:
                return name
        for name, aliases in (node.get("control_aliases") or {}).items():
            if wanted in (a.lower() for a in aliases):
                return name
        return None

    def is_operator_action(self, node_id: str, action: str) -> bool:
        node = self.by_id.get(node_id) or {}
        return action.lower() in (a.lower() for a in node.get("operator_controls", []))

    def for_prompt(self) -> list[dict]:
        """Компактное представление для промпта: что агент может, что — только Оператор."""
        rows = []
        for node in self.by_id.values():
            rows.append({
                "node_id": node["node_id"],
                "title": node.get("title", ""),
                "system": node.get("system", ""),
                "controls": node.get("controls", []),
                "operator_controls": node.get("operator_controls", []),
                "queries": node.get("queries", []),
                "notes": node.get("notes", ""),
            })
        return rows
