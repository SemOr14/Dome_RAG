"""Интерфейс RAG: поиск типовых ситуаций и FAQ."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class RagDoc:
    id: str
    title: str
    text: str
    score: float = 0.0
    tags: list[str] = field(default_factory=list)


class Retriever(Protocol):
    def search(self, query: str, k: int = 3) -> list[RagDoc]:
        ...


class NullRetriever:
    """RAG отключён или недоступен — промпт собирается без примеров."""

    def search(self, query: str, k: int = 3) -> list[RagDoc]:
        return []
