"""Заглушка RAG без внешних зависимостей: BM25 по файлам data/rag.

Форматы документов:
- `*.md` — один документ на файл; необязательный заголовок-frontmatter с `id`, `title`, `tags`;
- `*.jsonl` — по документу на строку: {"id", "title", "text", "tags"?}.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path

import yaml

from .base import RagDoc

_WORD = re.compile(r"\w+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    tokens = []
    for word in _WORD.findall(text.lower()):
        if len(word) < 2:
            continue
        tokens.append(word)
        if "_" in word:  # climate_sensor_01 → ещё и climate, sensor
            tokens.extend(p for p in word.split("_") if len(p) > 2 and not p.isdigit())
        elif len(word) > 6 and not word.isascii():
            tokens.append(word[:6])  # грубый стемминг для русских словоформ
    return tokens


def _parse_md(path: Path) -> RagDoc:
    text = path.read_text(encoding="utf-8")
    meta: dict = {}
    if text.startswith("---"):
        _, front, body = text.split("---", 2)
        meta = yaml.safe_load(front) or {}
        text = body.strip()
    title = meta.get("title") or next(
        (line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("#")), path.stem)
    return RagDoc(id=str(meta.get("id", path.stem)), title=title, text=text, tags=list(meta.get("tags", [])))


class KeywordRetriever:
    def __init__(self, folder: Path, k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs: list[RagDoc] = []
        for path in sorted(folder.glob("*.md")):
            self.docs.append(_parse_md(path))
        for path in sorted(folder.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.docs.append(RagDoc(id=str(row["id"]), title=row.get("title", ""),
                                            text=row["text"], tags=row.get("tags", [])))
        self._tf = [Counter(tokenize(f"{d.title} {' '.join(d.tags)} {d.text}")) for d in self.docs]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = sum(self._len) / len(self._len) if self._len else 0.0
        df = Counter(t for tf in self._tf for t in tf)
        n = len(self.docs)
        self._idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def search(self, query: str, k: int = 3) -> list[RagDoc]:
        terms = set(tokenize(query))
        scored = []
        for doc, tf, length in zip(self.docs, self._tf, self._len):
            score = 0.0
            for t in terms:
                f = tf.get(t)
                if f:
                    norm = f + self.k1 * (1 - self.b + self.b * length / (self._avg or 1))
                    score += self._idf[t] * f * (self.k1 + 1) / norm
            if score > 0:
                scored.append((score, doc))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [RagDoc(d.id, d.title, d.text, round(s, 3), d.tags) for s, d in scored[:k]]
