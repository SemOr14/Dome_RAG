"""Сборка промпта: собирает контекст из хранилищ, графа, правил и RAG и рендерит Jinja2-шаблоны."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .context.catalog import NodeCatalog
from .context.graph import InfluenceGraph, format_edge
from .context.telemetry import TelemetryContext, compact_telemetry
from .rag.base import RagDoc, Retriever
from .schemas import output_schema_example


def _tojson(value: Any, indent: int | None = None) -> str:
    # Своя версия фильтра: кириллица без \u-экранирования и компактно — экономим токены
    separators = None if indent else (",", ":")
    return json.dumps(value, ensure_ascii=False, default=str, indent=indent, separators=separators)


def _dome_time(game_time: dict | None) -> str | None:
    if not isinstance(game_time, dict) or not isinstance(game_time.get("hour"), (int, float)):
        return None
    hour = game_time["hour"]
    return f"день {game_time.get('day', '?')}, {int(hour):02d}:{int(round(hour % 1 * 60)) % 60:02d}"


@dataclass
class BuiltPrompt:
    messages: list[dict]
    telemetry: TelemetryContext
    rag_docs: list[RagDoc]
    journal: list[dict]
    dome_time: str | None
    stats: dict = field(default_factory=dict)


class PromptBuilder:
    def __init__(self, prompt_dir: Path, catalog: NodeCatalog, graph: InfluenceGraph, hard_rules: dict,
                 retriever: Retriever, *, team_name: str = "команда 1", journal_limit: int = 15,
                 rag_k: int = 3, graph_depth: int = 1,
                 system_template: str = "system.md.j2", user_template: str = "user.md.j2"):
        self.env = Environment(loader=FileSystemLoader(str(prompt_dir)), undefined=StrictUndefined,
                               trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
        self.env.filters["tojson"] = _tojson
        self.catalog = catalog
        self.graph = graph
        self.hard_rules = hard_rules
        self.retriever = retriever
        self.team_name = team_name
        self.journal_limit = journal_limit
        self.rag_k = rag_k
        self.graph_depth = graph_depth
        self.system_template = system_template
        self.user_template = user_template

    @staticmethod
    def load_rules(path: Path) -> dict:
        return yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def rag_query(self, trigger: str, tctx: TelemetryContext) -> str:
        parts = [trigger] + [f"{a['node']} {a.get('param') or ''} {a['note']}" for a in tctx.anomalies]
        return " ".join(parts)

    def build(self, trigger: str, snapshot: dict, journal: list[dict], focus_nodes: set[str] | None = None) -> BuiltPrompt:
        tctx = compact_telemetry(snapshot, self.catalog.by_id)
        seeds = set(tctx.hot_nodes) | set(focus_nodes or ())
        edges = [format_edge(e) for e in self.graph.subgraph(seeds, self.graph_depth)]
        journal = journal[-self.journal_limit:]
        try:
            rag_docs = self.retriever.search(self.rag_query(trigger, tctx), self.rag_k)
        except Exception:  # RAG не должен ронять агента
            rag_docs = []
        dome_time = _dome_time(snapshot.get("game_time"))

        system = self.env.get_template(self.system_template).render(
            hard_rules=self.hard_rules, output_schema=output_schema_example(), team_name=self.team_name)
        user = self.env.get_template(self.user_template).render(
            trigger=trigger, dome_time=dome_time, game_time=snapshot.get("game_time"),
            observed_at=snapshot.get("observed_at"),
            telemetry=tctx.nodes, anomalies=tctx.anomalies, catalog=self.catalog.for_prompt(),
            graph_edges=edges, journal=journal, rag_docs=rag_docs)

        stats = {"system_chars": len(system), "user_chars": len(user),
                 "approx_tokens": (len(system) + len(user)) // 3, "anomalies": len(tctx.anomalies),
                 "graph_edges": len(edges), "journal": len(journal), "rag_docs": [d.id for d in rag_docs]}
        return BuiltPrompt(messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                           telemetry=tctx, rag_docs=rag_docs, journal=journal, dome_time=dome_time, stats=stats)
