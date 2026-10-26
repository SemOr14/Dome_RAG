"""Поиск по материалам организаторов (RAG без внешних зависимостей): BM25 на чистом Python.

Источники: ``knowledge/*.md`` (чанки по заголовкам), ``Logs/*/events.jsonl`` (эпизоды вокруг срабатываний
автоматики и действий агента), ``Logs/*/logbook.jsonl`` (записи жильцов). Русские слова грубо
стеммятся обрезкой до 5 символов — для справочного поиска этого хватает.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TOKEN = re.compile(r"[0-9a-zа-яё_]+", re.IGNORECASE)
STEM = 5
EPISODE_BEFORE_MIN = 3
EPISODE_AFTER_MIN = 20
MAX_CHUNK_CHARS = 1400


def tokens(text: str) -> list[str]:
    """Слова в нижнем регистре, укороченные до STEM символов (грубый стемминг)."""
    out = []
    for w in TOKEN.findall(text.lower()):
        out.append(w[:STEM] if not w.isdigit() and len(w) > STEM and not re.search(r"_|\d", w) else w)
    return out


@dataclass
class Chunk:
    ref: str
    text: str


class Rag:
    """Индекс BM25 по фрагментам."""

    def __init__(self, chunks: list[Chunk], k1: float = 1.4, b: float = 0.75) -> None:
        self.chunks = chunks
        self._k1, self._b = k1, b
        self._tf = [Counter(tokens(c.ref + " " + c.text)) for c in chunks]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 1.0
        df: Counter[str] = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(chunks)
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def __len__(self) -> int:
        return len(self.chunks)

    def search(self, query: str, k: int = 4, min_score: float = 0.25) -> list[dict[str, Any]]:
        """Лучшие фрагменты по запросу: ``[{"ref", "text", "score"}]``."""
        q = [t for t in tokens(query) if t in self._idf]
        scored: list[tuple[float, int]] = []
        for i, tf in enumerate(self._tf):
            norm = self._k1 * (1 - self._b + self._b * self._len[i] / self._avg)
            s = sum(self._idf[t] * tf[t] * (self._k1 + 1) / (tf[t] + norm) for t in q if t in tf)
            if s >= min_score:
                scored.append((s, i))
        scored.sort(reverse=True)
        return [{"ref": self.chunks[i].ref, "text": self.chunks[i].text, "score": round(s, 2)} for s, i in scored[:k]]

    def references(self, query: str) -> list[dict[str, Any]]:
        """Интерфейс ``Advisor`` рантайма."""
        return self.search(query, 4)


# --------------------------------------------------------------------------- построение индекса


def _split_markdown(path: Path, max_chars: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    heading = path.stem
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if len(body) > 40:
            for i in range(0, len(body), max_chars):
                part = body[i : i + max_chars]
                chunks.append(Chunk(f"{path.name}#{heading}" + (f"/{i // max_chars + 1}" if len(body) > max_chars else ""), part))
        buf.clear()

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            flush()
            heading = line.lstrip("# ").strip()[:80]
        else:
            buf.append(line)
    flush()
    return chunks


def _event_line(e: dict[str, Any]) -> str:
    t = e.get("dome", {}).get("time", "")
    kind = e.get("kind")
    if kind == "automation":
        ev = e.get("event", {})
        return f"{t} автоматика {ev.get('interlock')} {ev.get('type')}: {ev.get('message')}"
    if kind == "action":
        who = "агент" if e.get("actor") == "agent" else "Оператор"
        return f"{t} {who}: {e.get('node_id')}.{e.get('action')} {json.dumps(e.get('value'), ensure_ascii=False)} → {'ok' if e.get('success') else e.get('error')}"
    if kind == "change":
        return f"{t} изменение {e.get('node_id')}.{e.get('param')}: {e.get('from')} → {e.get('to')}"
    if kind == "message":
        who = (e.get("sender") or {}).get("role", "?")
        return f"{t} чат [{who}]: {e.get('text')}"
    return ""


def _episodes(path: Path, day: str) -> list[Chunk]:
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue

    def minutes(e: dict[str, Any]) -> int:
        h, _, m = str(e.get("dome", {}).get("time", "00:00")).partition(":")
        return int(h or 0) * 60 + int(m or 0)

    anchors = [e for e in events if e.get("kind") == "automation" or (e.get("kind") == "action" and e.get("actor") == "agent")]
    chunks: list[Chunk] = []
    used: set[int] = set()
    for a in anchors:
        t0 = minutes(a)
        if t0 in used:
            continue
        used.add(t0)
        lines = [_event_line(e) for e in events if -EPISODE_BEFORE_MIN <= minutes(e) - t0 <= EPISODE_AFTER_MIN and e.get("kind") != "journal"]
        text = "\n".join(x for x in lines if x)[:MAX_CHUNK_CHARS * 2]
        if text:
            chunks.append(Chunk(f"journal/{day}/{a.get('dome', {}).get('time', '')}", text))
    return chunks


def build_rag(root: str | Path, chunk_chars: int = 900) -> Rag:
    """Построить индекс по каталогу организаторов (нет каталога — пустой индекс)."""
    base = Path(root)
    chunks: list[Chunk] = []
    for md in sorted((base / "knowledge").glob("*.md")) if (base / "knowledge").is_dir() else []:
        chunks += _split_markdown(md, chunk_chars)
    api_doc = base / "01_API.md"
    if api_doc.is_file():
        chunks += _split_markdown(api_doc, chunk_chars)
    logs = base / "Logs"
    for day_dir in sorted(logs.iterdir()) if logs.is_dir() else []:
        if (day_dir / "events.jsonl").is_file():
            chunks += _episodes(day_dir / "events.jsonl", day_dir.name)
        book = day_dir / "logbook.jsonl"
        if book.is_file():
            for line in book.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                text = str(rec.get("text", ""))
                for i in range(0, len(text), MAX_CHUNK_CHARS):
                    chunks.append(Chunk(f"logbook/{day_dir.name}/{rec.get('author', '?')}" + (f"/{i // MAX_CHUNK_CHARS + 1}" if len(text) > MAX_CHUNK_CHARS else ""), text[i : i + MAX_CHUNK_CHARS]))
    return Rag(chunks)
