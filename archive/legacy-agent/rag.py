"""Простой поиск по базе знаний и журналу купола (RAG без внешних зависимостей).

Индекс строится один раз при старте: заголовки базы знаний + события и бортовой
журнал прошлой недели. Запрос — взвешенное пересечение термов, без эмбеддингов.
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass

_WORD = re.compile(r"[а-яёa-z0-9]{3,}", re.IGNORECASE)
_STOP = {"это", "как", "что", "для", "при", "или", "его", "она", "они", "был", "была", "было",
         "если", "так", "там", "уже", "еще", "ещё", "чем", "тем", "все", "всё", "нет", "да",
         "по", "на", "из", "за", "от", "до", "не", "ни", "но", "же", "бы", "ли", "то", "the"}


def _terms(text: str) -> list[str]:
    out = []
    for word in _WORD.findall(text.lower()):
        if word in _STOP:
            continue
        out.append(word[:6])
    return out


@dataclass
class Chunk:
    cid: str
    source: str
    text: str
    terms: Counter


class Rag:
    def __init__(self) -> None:
        self.chunks: list[Chunk] = []
        self.df: Counter = Counter()
        self.ready = False

    # ------------------------------------------------------------------ построение
    def build(self, knowledge_dir: str = "", journal_dir: str = "") -> "Rag":
        if knowledge_dir:
            for path in sorted(glob.glob(os.path.join(knowledge_dir, "*.md"))):
                self._add_markdown(path)
        if journal_dir:
            for path in sorted(glob.glob(os.path.join(journal_dir, "*", "*.jsonl"))):
                self._add_journal(path)
        if self.chunks:
            for chunk in self.chunks:
                for term in set(chunk.terms):
                    self.df[term] += 1
            self.ready = True
        return self

    def _add(self, cid: str, source: str, text: str) -> None:
        text = " ".join(text.split())
        if len(text) < 40:
            return
        self.chunks.append(Chunk(cid=cid, source=source, text=text[:2000], terms=Counter(_terms(text))))

    def _add_markdown(self, path: str) -> None:
        name = os.path.basename(path)
        with open(path, encoding="utf-8") as fh:
            block, title = [], name
            for line in fh:
                if line.startswith("## "):
                    if block:
                        self._add(f"{name}#{title}", name, title + "\n" + "".join(block))
                    title, block = line[3:].strip(), []
                else:
                    block.append(line)
            if block:
                self._add(f"{name}#{title}", name, title + "\n" + "".join(block))

    def _add_journal(self, path: str) -> None:
        day = os.path.basename(os.path.dirname(path))
        name = os.path.basename(path)
        buffer: list[str] = []
        try:
            with open(path, encoding="utf-8") as fh:
                for i, line in enumerate(fh):
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    kind = rec.get("kind")
                    if kind == "logbook":
                        text = rec.get("text") or ""
                        self._add(f"{day}/logbook/{i}", f"{day}/logbook", text)
                        continue
                    if kind not in ("automation", "change", "action", "message"):
                        continue
                    clock = (rec.get("dome") or {}).get("time", "")
                    if kind == "automation":
                        event = rec.get("event") or {}
                        buffer.append(f"{clock} автоматика {event.get('interlock')} {event.get('type')}: {event.get('message')}")
                    elif kind == "change":
                        buffer.append(f"{clock} {rec.get('node_id')} {rec.get('param')}: {rec.get('from')} -> {rec.get('to')}")
                    elif kind == "action":
                        buffer.append(f"{clock} команда {rec.get('node_id')} {rec.get('action')} {rec.get('value')} success={rec.get('success')}")
                    elif kind == "message":
                        buffer.append(f"{clock} чат {rec.get('sender')}: {rec.get('text')}")
                    if len(buffer) >= 25:
                        self._add(f"{day}/events/{i}", f"{day}/events", "\n".join(buffer))
                        buffer = []
        except OSError:
            return
        if buffer:
            self._add(f"{day}/events/end", f"{day}/events", "\n".join(buffer))

    # ------------------------------------------------------------------ поиск
    def search(self, query: str, k: int = 4) -> list[dict]:
        if not self.ready:
            return []
        q = Counter(_terms(query))
        if not q:
            return []
        total = len(self.chunks)
        scored = []
        for chunk in self.chunks:
            score = 0.0
            for term, qn in q.items():
                tf = chunk.terms.get(term)
                if not tf:
                    continue
                idf = math.log(1 + total / (1 + self.df.get(term, 0)))
                score += qn * idf * (tf / (tf + 1.5))
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        out = []
        for score, chunk in scored[:k]:
            out.append({"id": chunk.cid, "source": chunk.source, "score": round(score, 3),
                        "text": chunk.text[:900]})
        return out
