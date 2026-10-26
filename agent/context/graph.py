"""Граф влияний узлов: загрузка из YAML и выбор подграфа вокруг узлов, о которых идёт речь."""

from __future__ import annotations

from collections import deque
from pathlib import Path

import yaml


class InfluenceGraph:
    def __init__(self, edges: list[dict]):
        self.edges = edges
        self._adj: dict[str, list[int]] = {}
        for i, e in enumerate(edges):
            self._adj.setdefault(e["from"], []).append(i)
            self._adj.setdefault(e["to"], []).append(i)

    @classmethod
    def load(cls, path: Path) -> "InfluenceGraph":
        if not path.is_file():
            return cls([])
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls([e for e in data.get("edges", []) if "from" in e and "to" in e])

    def subgraph(self, seeds: set[str], depth: int = 1) -> list[dict]:
        """Рёбра в пределах `depth` шагов от seeds (без учёта направления). Пустой seeds — весь граф."""
        if not seeds:
            return list(self.edges)
        seen = {s for s in seeds if s in self._adj}
        queue = deque((s, 0) for s in seen)
        picked: set[int] = set()
        while queue:
            vertex, dist = queue.popleft()
            for i in self._adj.get(vertex, []):
                picked.add(i)
                e = self.edges[i]
                other = e["to"] if e["from"] == vertex else e["from"]
                if dist + 1 < depth and other not in seen:
                    seen.add(other)
                    queue.append((other, dist + 1))
        return [self.edges[i] for i in sorted(picked)]


def format_edge(e: dict) -> str:
    text = f"{e['from']} —{e['relation']}→ {e['to']}"
    return f"{text}: {e['effect']}" if e.get("effect") else text
