"""Сборка промпта: собирает контекст из хранилищ, графа, правил и RAG и рендерит Jinja2-шаблоны."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .context.catalog import NodeCatalog
from .context.graph import InfluenceGraph, format_edge
from .context.hierarchy import Hierarchy
from .context.peers import compare_peers, peer_lines
from .context.telemetry import TelemetryContext, compact_telemetry
from .rag.base import RagDoc, Retriever
from .schemas import output_schema_example
from .patterns import MARK as PATTERNS_MARK, END_MARK as PATTERNS_END

ALGORITHMS_MARK = "Алгоритмы (плейбуки)"  # с этих слов в тексте повода начинается раздел о работе плейбуков
LISTED = re.compile(r"^\d+\. \[(\w+)\] ")  # строка повода в объединённом тексте диспетчера


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
                 rag_k: int = 3, graph_depth: int = 1, stale_s: float = 30.0,
                 system_template: str = "system.md.j2", user_template: str = "user.md.j2",
                 hierarchy: Hierarchy | None = None, scenario_hints: list[dict] | None = None):
        self.env = Environment(loader=FileSystemLoader(str(prompt_dir)), undefined=StrictUndefined,
                               trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
        self.env.filters["tojson"] = _tojson
        self.env.filters["defang"] = lambda v: re.sub(r"</?\s*untrusted", "< untrusted", str(v), flags=re.I)  # данные не закрывают свой тег
        self.catalog = catalog
        self.graph = graph
        self.hierarchy = hierarchy or Hierarchy()
        self.scenario_hints = [dict(h, _re=re.compile(h['match'], re.I)) for h in (scenario_hints or []) if h.get('match') and h.get('text')]
        self.hard_rules = hard_rules
        self.retriever = retriever
        self.team_name = team_name
        self.journal_limit = journal_limit
        self.rag_k = rag_k
        self.graph_depth = graph_depth
        self.stale_s = stale_s
        self.system_template = system_template
        self.user_template = user_template

    @staticmethod
    def load_hints(path: Path) -> list[dict]:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
        return list((data or {}).get("hints") or [])

    def pick_hints(self, trigger: str, anomalies: list[dict]) -> list[str]:
        """Сценарные правила, чьё условие совпало с поводом или аномалиями (порядок файла)."""
        text = trigger.split(ALGORITHMS_MARK)[0] + " " + " ".join(f"{a.get('node')} {a.get('param')} {a.get('note')}" for a in anomalies)
        return [h["text"] for h in self.scenario_hints if h["_re"].search(text)]

    def limits(self) -> dict:
        return {"max_state_changes_per_decision": 1, "max_suspicions_per_decision": 1, **(self.hard_rules.get("limits") or {})}

    @staticmethod
    def load_rules(path: Path) -> dict:
        return yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def rag_query(self, trigger: str, tctx: TelemetryContext) -> str:
        # Поиск по существу повода: числовые гипотезы и обзор не заслоняют срочные поводы, а одинаковые
        # строки (десятки узлов UNKNOWN разом) идут в запрос один раз
        lines = trigger.split(ALGORITHMS_MARK)[0].split(PATTERNS_MARK)[0].splitlines()  # раздел «Алгоритмы» в запрос к RAG не входит
        listed = [line for line in lines if LISTED.match(line)]
        urgent = [line for line in listed if LISTED.match(line).group(1) not in ("numeric", "review")]
        seen: set[str] = set()
        parts = []
        for line in urgent if urgent else lines:
            key = LISTED.sub("", line).split(":", 1)[-1]
            if key not in seen:
                seen.add(key)
                parts.append(line)
        # Full telemetry still goes to the model. Retrieval follows the actual new
        # evidence, rather than all persistent alarms elsewhere in the dome.
        return " ".join(parts)

    def retrieve(self, trigger: str, tctx: TelemetryContext) -> list[RagDoc]:
        """Retrieve per independent reason; one verbose fault cannot consume every slot."""
        raw = trigger.split(ALGORITHMS_MARK)[0].split(PATTERNS_MARK)[0].split('\n\nРежим:')[0]
        listed = [line for line in raw.splitlines() if LISTED.match(line)]
        urgent = [line for line in listed if LISTED.match(line).group(1) not in ('numeric', 'review')]
        queries = [LISTED.sub('', line) for line in (urgent or listed)] or [self.rag_query(raw, tctx)]
        # An operator's short question needs the current contradiction as context.
        if 'Оператор' in raw:
            queries = [f"{a['node']}.{a.get('param')}: {a.get('value')!r}: {a['note']}"
                       for a in tctx.anomalies if a.get('kind') in ('conflict', 'alarm')] + queries
        queries = list(dict.fromkeys(q.strip() for q in queries if q.strip()))[:12]
        batches = [self.retriever.search(self.query_context(q, tctx)[:4000], self.rag_k) for q in queries]
        docs, seen = [], set()
        for rank in range(self.rag_k):
            for batch in batches:
                if rank < len(batch) and batch[rank].id not in seen:
                    docs.append(batch[rank]); seen.add(batch[rank].id)
                    if len(docs) >= self.rag_k:
                        return docs
        return docs

    @staticmethod
    def query_context(query: str, tctx: TelemetryContext) -> str:
        """Attach the measurements behind an interlock, rather than unrelated alarms."""
        evidence = []
        fields = ()
        if 'IL_CONDENSATE' in query:
            fields = ('humidity_pct', 'temperature_c', 'dew_point_c')
        elif 'IL_CO2' in query:
            fields = ('co2_ppm',)
        if fields:
            for node in ('climate_sensor_01', 'climate_sensor_02', 'climate_sensor_03'):
                body = tctx.nodes.get(node) or {}
                evidence.append(node + ' ' + ' '.join(f'{f}={body[f]}' for f in fields if f in body))
        if 'IL_SMOKE' in query:
            for node in ('smoke_detector_01', 'smoke_detector_02', 'air_quality_sensor_01'):
                body = tctx.nodes.get(node) or {}
                evidence.append(node + ' ' + ' '.join(f'{f}={body[f]}' for f in
                    ('smoke_detected', 'obscuration_pct', 'alarm_state', 'co_ppm') if f in body))
        return query + ('\nСвязанные измерения: ' + '; '.join(evidence) if evidence else '')

    def build(self, trigger: str, snapshot: dict, journal: list[dict], focus_nodes: set[str] | None = None) -> BuiltPrompt:
        pattern_context = ''
        if PATTERNS_MARK in trigger and PATTERNS_END in trigger:
            before, rest = trigger.split(PATTERNS_MARK, 1)
            pattern_context, _, after = rest.partition(PATTERNS_END)
            trigger = before.rstrip() + after
        tctx = compact_telemetry(snapshot, self.catalog.by_id)
        seeds = set(tctx.hot_nodes) | set(focus_nodes or ())
        edges = [format_edge(e) for e in self.graph.subgraph(seeds, self.graph_depth)]
        journal = journal[-self.journal_limit:]
        try:
            rag_docs = self.retrieve(trigger, tctx)
        except Exception:  # RAG не должен ронять агента
            rag_docs = []
        dome_time = _dome_time(snapshot.get("game_time"))
        peers = peer_lines(compare_peers(snapshot, tctx.anomalies, self.catalog.by_id))

        system = self.env.get_template(self.system_template).render(
            hard_rules=self.hard_rules, output_schema=output_schema_example(), team_name=self.team_name,
            hierarchy=self.hierarchy.outline(), limits=self.limits())
        user = self.env.get_template(self.user_template).render(
            trigger=trigger, dome_time=dome_time, game_time=snapshot.get("game_time"),
            observed_at=snapshot.get("observed_at"), telemetry_age_s=snapshot.get("telemetry_age_s"), stale_s=self.stale_s,
            telemetry=tctx.nodes, anomalies=tctx.anomalies, catalog=self.catalog.for_prompt(),
            graph_edges=edges, journal=journal, rag_docs=rag_docs,
            forecasts=snapshot.get("forecasts") or [], peers=peers,
            hierarchy_focus=self.hierarchy.context(seeds), scenario_hints=self.pick_hints(trigger, tctx.anomalies), suspicions=snapshot.get("suspicions") or [])
        if pattern_context:
            user += '\n\n# Диагностические стратегии (проверки и анализ, не команды)\n' + pattern_context
        user += ('\n\nКарточки RAG — примеры и возможные аналогии, а не диагноз текущего сбоя. '
                 'Проверь совпадение характерных признаков и противоречащие данные. Новая аномалия может '
                 'не иметь карточки: опиши фактические сигналы, оставь неподтверждённую причину неизвестной, '
                 'запроси недостающие проверки или помощь человека. Не копируй историческую причину и разметку '
                 'только из-за похожего названия или высокого места в поиске.')

        stats = {"system_chars": len(system), "user_chars": len(user),
                 "approx_tokens": (len(system) + len(user)) // 3, "anomalies": len(tctx.anomalies),
                 "graph_edges": len(edges), "peer_rows": len(peers), "hierarchy_focus": len(self.hierarchy.context(seeds)), "journal": len(journal), "rag_docs": [d.id for d in rag_docs],
                 "telemetry_age_s": snapshot.get("telemetry_age_s")}
        return BuiltPrompt(messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                           telemetry=tctx, rag_docs=rag_docs, journal=journal, dome_time=dome_time, stats=stats)
