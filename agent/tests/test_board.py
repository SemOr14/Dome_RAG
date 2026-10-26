"""Тесты доски рекомендаций: связь агентов, предохранителя и решений человека."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from kupol.agents.board import RecommendationBoard
from kupol.core.config import AppConfig
from kupol.core.guard import Guard
from kupol.core.journal import Journal
from kupol.core.models import Command, Hypothesis, Recommendation, RecommendationStatus
from kupol.core.state import State
from tests.fakes import FakeAdapter


@pytest.fixture
def env(config: AppConfig, tmp_path: Path):  # type: ignore[no-untyped-def]
    adapter = FakeAdapter()
    journal = Journal(tmp_path / "run", clock=adapter.now)
    guard = Guard(config.guard, adapter, journal, State())
    board = RecommendationBoard(guard, journal, adapter.now, ttl_s=600)
    yield board, guard, adapter, journal
    journal.close()


def rec(title: str = "t", cmds: list[Command] | None = None, human: bool = False, agent: str = "monitor") -> Recommendation:
    return Recommendation(agent=agent, title=title, commands=cmds or [], needs_human=human, ts=FakeAdapter().now())


def vent_low() -> Command:
    return Command(name="vent_enable", args={"mode": "low"})


def eco() -> Command:
    return Command(name="set_power_mode", args={"mode": "eco"})


def test_info_recommendation(env) -> None:  # type: ignore[no-untyped-def]
    board, *_ = env
    r = board.add(rec())
    assert r is not None and r.status is RecommendationStatus.INFO and board.pending() == []


def test_human_flag_without_commands_is_pending(env) -> None:  # type: ignore[no-untyped-def]
    board, *_ = env
    r = board.add(rec(human=True))
    assert r is not None and r.status is RecommendationStatus.PENDING
    assert board.accept(r.id, "диагност") is r and r.status is RecommendationStatus.ACCEPTED


def test_auto_command_applied_immediately(env) -> None:  # type: ignore[no-untyped-def]
    board, guard, adapter, journal = env
    r = board.add(rec(cmds=[vent_low()]))
    assert r is not None and r.status is RecommendationStatus.AUTO_APPLIED and not r.needs_human
    assert [c.name for c in adapter.sent] == ["vent_enable"]
    e = journal.entries(kinds=["recommendation"])[0]
    assert e.status == "auto_applied" and "vent_enable" in e.action


def test_needs_human_command_waits_then_accept_sends(env) -> None:  # type: ignore[no-untyped-def]
    board, guard, adapter, journal = env
    r = board.add(rec(cmds=[eco()], agent="energy_planner"))
    assert r is not None and r.status is RecommendationStatus.PENDING and r.needs_human
    assert adapter.sent == [] and len(guard.pending()) == 1
    board.accept(r.id, "диагност")
    assert [c.name for c in adapter.sent] == ["set_power_mode"] and guard.pending() == []
    assert r.status is RecommendationStatus.ACCEPTED
    actors = [e.actor for e in journal.entries(kinds=["recommendation"])]
    assert actors == ["energy_planner", "диагност"]


def test_reject_never_sends(env) -> None:  # type: ignore[no-untyped-def]
    board, guard, adapter, _ = env
    r = board.add(rec(cmds=[eco()], agent="energy_planner"))
    assert r is not None
    board.reject(r.id, "диагност", "не сейчас")
    assert adapter.sent == [] and guard.pending() == []
    assert r.status is RecommendationStatus.REJECTED
    assert board.accept(r.id) is None and board.reject(r.id) is None  # уже обработана


def test_unknown_id(env) -> None:  # type: ignore[no-untyped-def]
    board, *_ = env
    assert board.accept("nope") is None and board.reject("nope") is None


def test_guard_rejection_is_noted_not_hidden(env) -> None:  # type: ignore[no-untyped-def]
    board, _, adapter, _ = env
    bad = Command(name="vent_enable", args={"mode": "high"})  # high агенту нельзя
    r = board.add(rec(cmds=[bad]))
    assert r is not None and adapter.sent == []
    assert r.status is RecommendationStatus.INFO and "не выполнена" in r.rationale


def test_mixed_commands(env) -> None:  # type: ignore[no-untyped-def]
    board, _, adapter, _ = env
    r = board.add(rec(cmds=[vent_low(), eco()]))
    assert r is not None and r.status is RecommendationStatus.PENDING  # авто ушла, вторая ждёт
    assert [c.name for c in adapter.sent] == ["vent_enable"]


def test_duplicate_suppressed_within_window(env) -> None:  # type: ignore[no-untyped-def]
    board, _, adapter, _ = env
    assert board.add(rec("одно и то же")) is not None
    assert board.add(rec("одно и то же")) is None
    assert board.add(rec("другое")) is not None
    adapter.t += timedelta(seconds=61)
    assert board.add(rec("одно и то же")) is not None


def test_expire_and_listing(env) -> None:  # type: ignore[no-untyped-def]
    board, guard, adapter, _ = env
    r = board.add(rec(human=True, title="ждёт"))
    assert r is not None
    adapter.t += timedelta(seconds=601)
    assert board.expire() == 1 and r.status is RecommendationStatus.EXPIRED
    assert board.pending() == [] and board.all() == [r] and board.recent(5) == [r]


def test_accept_all_for_headless(env) -> None:  # type: ignore[no-untyped-def]
    board, _, adapter, journal = env
    board.add(rec("a", cmds=[eco()], agent="energy_planner"))
    board.add(rec("b", human=True))
    assert board.accept_all("sim-auto-approve") == 2
    assert [c.name for c in adapter.sent] == ["set_power_mode"]
    assert {e.actor for e in journal.entries(kinds=["recommendation"])} >= {"sim-auto-approve"}


def test_hypothesis_goes_to_journal(env) -> None:  # type: ignore[no-untyped-def]
    board, _, _, journal = env
    r = rec("диагноз")
    r.hypotheses = [Hypothesis(cause="датчик врёт", probability=0.7, safe_check="сверить соседние")]
    board.add(r)
    e = journal.entries(kinds=["recommendation"])[0]
    assert "датчик врёт" in e.hypothesis and e.cross_check == "сверить соседние"


def test_recommendation_expires_when_guard_dropped_its_commands(env) -> None:  # type: ignore[no-untyped-def]
    board, guard, adapter, _ = env
    r = board.add(rec(cmds=[eco()], agent="energy_planner"))
    assert r is not None and board.expire() == 0
    adapter.t += timedelta(seconds=guard._cfg.pending_ttl_s + 1)  # предохранитель забыл команду, доска ещё помнит
    guard.check_effects()
    assert board.expire() == 1 and r.status is RecommendationStatus.EXPIRED
    assert board.accept(r.id) is None


def test_guard_is_pending(env) -> None:  # type: ignore[no-untyped-def]
    board, guard, *_ = env
    cmd = eco()
    board.add(rec(cmds=[cmd], agent="energy_planner"))
    assert guard.is_pending(cmd.id) and not guard.is_pending("nope")
