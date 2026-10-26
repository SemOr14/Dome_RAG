"""Адаптер к RAG Купола из каталога rag/ (FAQ, карточки ситуаций, конфликты источников).

Берутся только согласованные записи (approved_advisory); справочные фрагменты в промпт не идут.
Без локальной ONNX-модели поиск автоматически работает только по BM25 (см. rag/README.md).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .base import RagDoc

# Отсечка как в rag/context.py: записи заметно слабее лучшей в промпт не попадают
RELATIVE_CUTOFF = 0.78
MAX_QUERY_CHARS = 4000


class DomeRagRetriever:
    def __init__(self, rag_root: Path, semantic: bool = True):
        root = str(Path(rag_root).resolve())
        if root not in sys.path:
            sys.path.insert(0, root)
        from rag.common import DATA
        from rag.search import SearchEngine

        self.engine = SearchEngine(semantic=semantic)
        self.conflicts = {c["id"]: c for c in json.loads(
            (DATA / "knowledge_base" / "conflicts.json").read_text(encoding="utf-8"))}

    @property
    def semantic_error(self) -> str | None:
        """Почему семантический поиск не сработал на последнем запросе (None — сработал или выключен)."""
        return self.engine.last_semantic_error

    def search(self, query: str, k: int = 3) -> list[RagDoc]:
        query = query.strip()[:MAX_QUERY_CHARS]
        if not query or k < 1:
            return []
        hits = self.engine.search(query, limit=min(max(k * 3, 10), 20))
        if not hits:
            return []
        top = hits[0]["score"]
        docs = []
        for hit in hits:
            if hit["score"] < top * RELATIVE_CUTOFF or len(docs) >= k:
                break
            docs.append(self._to_doc(self.engine.record(hit["id"]), hit["score"]))
        return docs

    def _to_doc(self, record: dict, score: float) -> RagDoc:
        p = record["payload"]
        lines = []
        if record["kind"] == "situation":
            lines.append(f"Тип: {p['evidence_class']}"
                         + (f", окно {p['historical_window']}" if p.get("historical_window") else "")
                         + " — прошлые значения не являются текущей телеметрией.")
            for title, key, field in (("Наблюдение", "observation", "signals"),
                                      ("Интерпретация (гипотезы)", "interpretation", "hypotheses"),
                                      ("Решение", "decision", "proposed_steps"),
                                      ("Проверка", "verification", "expected_and_checks"),
                                      ("Если не помогло", "correction", "on_failed_or_uncertain")):
                items = p[key][field]
                if items:
                    lines.append(f"{title}: " + " ".join(items))
            if p.get("open_questions"):
                lines.append("Открытые вопросы: " + " ".join(p["open_questions"]))
        else:
            lines.append(p["answer"])
        for cid in record["conflict_ids"]:
            c = self.conflicts.get(cid)
            if c:
                lines.append(f"Конфликт источников {cid}: {c['issue']} Как поступать: {c['proposed_handling']}")
        return RagDoc(id=record["id"], title=record["title"], text="\n".join(lines),
                      score=round(score, 4), tags=list(record["tags"]))
