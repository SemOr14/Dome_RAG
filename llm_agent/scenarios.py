"""Grounded and synthetic scenario cards, explicitly advisory and available without external RAG."""
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

import yaml


@lru_cache(maxsize=1)
def scenario_cards() -> list[dict[str, Any]]:
    path = Path(__file__).resolve().parent.parent / 'data/operational_scenarios.yaml'
    return cast(list[dict[str, Any]], yaml.safe_load(path.read_text(encoding='utf-8'))['cases'])


def relevant_scenarios(query: str, kinds: set[str], limit: int = 4) -> list[dict[str, Any]]:
    query = query.lower()
    cards = scenario_cards()
    def score(card: dict[str, Any]) -> int:
        return sum(2 for t in card['tags'] if t.lower() in query) + int(card['kind'] in kinds)
    ranked = sorted(cards, key=score, reverse=True)
    return [c for c in ranked if score(c) > 0][:limit]
