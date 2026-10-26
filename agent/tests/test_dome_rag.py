"""RAG по материалам организаторов: находит эпизод ложного CO₂, справку по интерлокам и записи жильцов."""

from __future__ import annotations

from pathlib import Path

import pytest

from kupol.dome.rag import Chunk, Rag, build_rag, tokens

ROOT = Path(__file__).resolve().parents[1]
RAG_DIR = ROOT.parent  # shared corpus in the team repository


def test_tokens_stem_russian_and_keep_codes() -> None:
    assert tokens("Датчики CO2 показывают IL_CO2 3041") == ["датчи", "co2", "показ", "il_co2", "3041"]


def test_search_ranks_relevant_chunk_first() -> None:
    rag = Rag([Chunk("a", "Вытяжка FabLab отключилась при пайке, нужно включить линию"), Chunk("b", "Сейсмодатчик фиксирует землетрясение, амплитуда"), Chunk("c", "Принтер печатает корпус")])
    hits = rag.search("вытяжка не работает при пайке")
    assert hits and hits[0]["ref"] == "a"
    assert rag.search("совсем другое слово qwerty") == []


@pytest.mark.skipif(not (RAG_DIR / "knowledge").is_dir(), reason="нет материалов организаторов")
def test_knowledge_base_answers_interlock_question() -> None:
    rag = build_rag(RAG_DIR)
    assert len(rag) > 100
    hits = rag.search("ложная тревога CO2 пометить датчик недостоверным основной датчик IL_CO2", 5)
    refs = " ".join(h["ref"] for h in hits)
    assert "journal/" in refs or "01_API" in refs or "logbook/" in refs


@pytest.mark.skipif(not (RAG_DIR / "Logs").is_dir(), reason="нет журнала купола")
def test_journal_episode_with_false_co2_is_retrievable() -> None:
    rag = build_rag(RAG_DIR)
    hits = rag.search("IL_CO2 TRIP станки на паузе set_data_trust set_primary_sensor reset_interlock resume", 3)
    episode = [h for h in hits if h["ref"].startswith("journal/")]
    assert episode and "set_data_trust" in episode[0]["text"] and "resume" in episode[0]["text"]


@pytest.mark.skipif(not (RAG_DIR / "Logs").is_dir(), reason="нет журнала купола")
def test_logbook_mentions_webhook_lie() -> None:
    hits = build_rag(RAG_DIR).search("webhook COMPLETED принтер ещё печатает бокс открыли", 3)
    assert any("webhook" in h["text"].lower() for h in hits)
