"""Иерархия узлов Купола: питание, физическая вложенность, путь данных и группы систем.

Дополняет граф влияний: граф говорит, что на что влияет, иерархия — кто от кого зависит. В промпт идёт
короткая схема целиком (системное сообщение) и положение узлов, о которых речь (пользовательское). Файл
``data/node_hierarchy.yaml``; без него модель работает как раньше.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

MAX_FOCUS = 6
MAX_LIST = 8
DEPTH = 5


class Hierarchy:
    def __init__(self, spec: dict[str, Any] | None = None) -> None:
        spec = spec or {}
        self.systems: dict[str, dict] = spec.get("systems") or {}
        self.roles: dict[str, str] = spec.get("roles") or {}
        self.power: dict[str, list[str]] = {k: list(v) for k, v in (spec.get("power") or {}).items()}
        self.power_unknown: set[str] = set(spec.get("power_unknown") or [])
        self.inside: dict[str, str] = spec.get("inside") or {}
        self.data: dict[str, str] = spec.get("data") or {}
        self.data_default: str | None = spec.get("data_default")
        self.notes: list[str] = list(spec.get("notes") or [])
        self.system_of = {n: sid for sid, s in self.systems.items() for n in s.get("nodes", [])}
        self.fed: dict[str, list[str]] = defaultdict(list)  # источник -> кого питает
        for child, parents in self.power.items():
            for parent in parents:
                self.fed[parent].append(child)

    @classmethod
    def load(cls, path: Path) -> "Hierarchy":
        if not path.is_file():
            return cls()
        return cls(yaml.safe_load(path.read_text(encoding="utf-8")) or {})

    def __bool__(self) -> bool:
        return bool(self.systems or self.power or self.data)

    def known(self, node: str) -> bool:
        return node in self.system_of or node in self.power or node in self.fed or node in self.data

    # ------------------------------------------------------------------ схема целиком

    def outline(self) -> list[str]:
        """Схема для системного сообщения: группы систем, цепочка питания, путь данных."""
        if not self:
            return []
        lines = ["Системы: " + "; ".join(f"{s['title']} — {', '.join(s.get('nodes', []))}" for s in self.systems.values())]
        if self.power:
            lines.append("Питание (источник → кого питает):")
            for parent, children in self.fed.items():
                lines.append(f"  {parent} → {', '.join(children)}")
            if self.power_unknown:
                lines.append(f"  питание не описано в документации: {', '.join(sorted(self.power_unknown))}")
        if self.inside:
            lines.append("Вложенность: " + "; ".join(f"{k} внутри {v}" for k, v in self.inside.items()))
        if self.data:
            by_parent: dict[str, list[str]] = defaultdict(list)
            for child, parent in self.data.items():
                by_parent[parent].append(child)
            lines.append("Путь данных (прототип топологии; телеметрия идёт к агенту вверх по цепочке):")
            for parent, children in by_parent.items():
                lines.append(f"  через {parent}: {', '.join(children)}")
            if self.data_default:
                lines.append(f"  остальные узлы — через {self.data_default}")
        lines += [f"Важно: {n}" for n in self.notes]
        return lines

    # ------------------------------------------------------------------ положение узлов

    def _up(self, node: str) -> str | None:
        """Цепочка питания вверх: уровни через « ← », альтернативные источники через « | »."""
        level, seen, parts = [node], {node}, []
        for _ in range(DEPTH):
            nxt = [p for n in level for p in self.power.get(n, []) if p not in seen]
            if not nxt:
                break
            seen.update(nxt)
            parts.append("|".join(dict.fromkeys(nxt)))
            level = nxt
        return " ← ".join([node, *parts]) if parts else None

    def _down(self, node: str) -> list[str]:
        """Всё, что питается от узла напрямую или через него (без линий щитка)."""
        out, queue, seen = [], list(self.fed.get(node, [])), {node}
        while queue:
            cur = queue.pop(0)
            if cur in seen:
                continue
            seen.add(cur)
            if not cur.startswith("line:"):
                out.append(cur)
            queue += self.fed.get(cur, [])
        return out

    def _data_chain(self, node: str) -> str:
        chain, cur = [], node
        for _ in range(DEPTH):
            parent = self.data.get(cur) or (self.data_default if cur != self.data_default and cur not in self.data else None)
            if not parent or parent in chain or parent == node:
                break
            chain.append(parent)
            cur = parent
        return " ← ".join([node, *chain]) if chain else node

    def context(self, nodes: set[str] | list[str]) -> list[str]:
        """Положение каждого узла из списка: система, роль, питание, зависимые, канал данных."""
        lines = []
        for node in sorted(n for n in nodes if self.known(n))[:MAX_FOCUS]:
            sid = self.system_of.get(node)
            head = f"{node}: " + (f"{self.systems[sid]['title']}, " if sid else "") + (f"роль {self.roles[node]}" if node in self.roles else "")
            bits = [head.rstrip(", ")]
            up = self._up(node)
            if up:
                bits.append(f"питание: {up}")
            elif node in self.power_unknown:
                bits.append("питание в документации не описано")
            down = self._down(node)
            if down:
                shown = ", ".join(down[:MAX_LIST]) + (f" и ещё {len(down) - MAX_LIST}" if len(down) > MAX_LIST else "")
                bits.append(f"от него зависят: {shown}")
            if node in self.inside:
                bits.append(f"внутри {self.inside[node]}")
            bits.append(f"данные: {self._data_chain(node)}")
            if sid:
                mates = [n for n in self.systems[sid].get("nodes", []) if n != node][:MAX_LIST]
                if mates:
                    bits.append(f"в той же системе: {', '.join(mates)}")
            lines.append("; ".join(bits))
        return lines
