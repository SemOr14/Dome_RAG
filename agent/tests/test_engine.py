"""Тесты главного цикла: устойчивость к сбоям, артефакты, остановка."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from kupol.adapter.base import AdapterTimeout
from kupol.adapter.scenario import Scenario, load_scenario
from kupol.adapter.simulator import SimulatorAdapter
from kupol.core.config import AppConfig
from kupol.core.journal import read_jsonl
from kupol.engine import Engine
from tests.conftest import ROOT


def sim_for(config: AppConfig, name: str = "quiet", cls: type[SimulatorAdapter] = SimulatorAdapter, **kw: Any) -> SimulatorAdapter:
    sc = load_scenario(ROOT / "scenarios" / f"{name}.yaml")
    return cls(config, sc, seed=42, **kw)


def test_engine_produces_all_artifacts(config: AppConfig, tmp_path: Path) -> None:
    eng = Engine(config, sim_for(config), tmp_path / "run", duration_s=300, meta={"scenario": "quiet"})
    summary = eng.run(speed=0)
    assert summary["elapsed_s"] == 300 and summary["counters"]["ticks"] == 300
    for name in ("journal.jsonl", "telemetry.jsonl", "summary.json", "meta.json", "journal.csv", "journal.md", "sim_events.jsonl"):
        assert (tmp_path / "run" / name).exists(), name
    assert len(list(read_jsonl(tmp_path / "run" / "telemetry.jsonl"))) == 300
    assert eng.view.telemetry is not None and not eng.view.running


def test_close_is_idempotent(config: AppConfig, tmp_path: Path) -> None:
    eng = Engine(config, sim_for(config), tmp_path / "run", duration_s=10)
    first = eng.run(speed=0)
    assert eng.close() == first


class FlakyAdapter(SimulatorAdapter):
    """Телеметрия «падает»: сначала таймаутами, потом неожиданными исключениями."""

    calls = 0

    def get_telemetry(self):  # type: ignore[no-untyped-def]
        FlakyAdapter.calls += 1
        n = FlakyAdapter.calls
        if 10 <= n < 20:
            raise AdapterTimeout("нет ответа")
        if 30 <= n < 35:
            raise RuntimeError("странный сбой адаптера")
        return super().get_telemetry()


def test_loop_survives_adapter_failures(config: AppConfig, tmp_path: Path) -> None:
    FlakyAdapter.calls = 0
    eng = Engine(config, sim_for(config, cls=FlakyAdapter), tmp_path / "run", duration_s=60)
    summary = eng.run(speed=0)
    assert summary["counters"]["ticks"] == 60
    assert summary["counters"]["poll_errors"] == 15
    errors = [e for e in eng.journal.entries(kinds=["error"])]
    assert errors and all(e.status == "error" for e in errors)
    assert len(errors) < 15  # повторы одной ошибки не засоряют журнал
    assert len(list(read_jsonl(tmp_path / "run" / "telemetry.jsonl"))) == 45  # остальные снимки сохранены


def test_loop_survives_step_exception(config: AppConfig, tmp_path: Path) -> None:
    eng = Engine(config, sim_for(config), tmp_path / "run", duration_s=60)

    def boom(*a: object, **k: object) -> list[Any]:
        raise ValueError("сломанный детектор")

    eng.suite.run = boom  # type: ignore[method-assign]
    summary = eng.run(speed=0)
    assert summary["counters"]["ticks"] == 60 and summary["counters"]["step_errors"] == 60
    assert any("сломанный детектор" in e.event for e in eng.journal.entries(kinds=["error"]))
    assert len(list(read_jsonl(tmp_path / "run" / "telemetry.jsonl"))) == 60  # остальные шаги работали


class InterruptingAdapter(SimulatorAdapter):
    """Имитация Ctrl+C посреди игры."""

    calls = 0

    def get_telemetry(self):  # type: ignore[no-untyped-def]
        InterruptingAdapter.calls += 1
        if InterruptingAdapter.calls == 25:
            raise KeyboardInterrupt
        return super().get_telemetry()


def test_ctrl_c_saves_journal(config: AppConfig, tmp_path: Path) -> None:
    InterruptingAdapter.calls = 0
    eng = Engine(config, sim_for(config, cls=InterruptingAdapter), tmp_path / "run", duration_s=600)
    summary = eng.run(speed=0)
    assert summary["counters"]["ticks"] == 25
    events = [e.event for e in eng.journal.entries()]
    assert "остановка по Ctrl+C" in events and "прогон завершён" in events
    assert (tmp_path / "run" / "summary.json").exists() and (tmp_path / "run" / "journal.md").exists()
    assert len(list(read_jsonl(tmp_path / "run" / "journal.jsonl"))) == len(events)  # на диске всё


def test_stop_from_another_thread(config: AppConfig, tmp_path: Path) -> None:
    eng = Engine(config, sim_for(config), tmp_path / "run", duration_s=5400)
    result: dict[str, Any] = {}
    t = threading.Thread(target=lambda: result.update(eng.run(speed=100)))
    t.start()
    time.sleep(0.4)
    eng.stop()
    t.join(timeout=10)
    assert not t.is_alive() and 0 < result["counters"]["ticks"] < 5400


@pytest.mark.slow
def test_agents_in_threads(config: AppConfig, tmp_path: Path) -> None:
    eng = Engine(config, sim_for(config, "basic"), tmp_path / "run", agent_workers=2, auto_approve=True, duration_s=700)
    summary = eng.run(speed=0)
    c = summary["counters"]
    assert c["agent_runs"] > 0 and c["recommendations"] > 0
    assert len(eng.journal.entries(kinds=["llm_call"])) == c["agent_runs"]  # close() дождался потоков


@pytest.mark.slow
def test_pending_recommendations_wait_without_auto_approve(config: AppConfig, tmp_path: Path) -> None:
    eng = Engine(config, sim_for(config, "basic"), tmp_path / "run", auto_approve=False, duration_s=700)
    eng.run(speed=0)
    assert eng.board.pending(), "рекомендации, требующие человека, должны ждать решения"
    assert not [e for e in eng.journal.entries() if e.actor == "sim-auto-approve"]


@pytest.mark.slow
def test_auto_approve_is_labelled(config: AppConfig, tmp_path: Path) -> None:
    eng = Engine(config, sim_for(config, "basic"), tmp_path / "run", auto_approve=True, duration_s=700)
    eng.run(speed=0)
    assert [e for e in eng.journal.entries() if e.actor == "sim-auto-approve"]
    assert not eng.board.pending()


def test_elapsed_follows_adapter_time(config: AppConfig, tmp_path: Path) -> None:
    eng = Engine(config, SimulatorAdapter(config, Scenario(name="e"), seed=1), tmp_path / "run", duration_s=30)
    eng.run(speed=0)
    assert eng.elapsed_s == pytest.approx(30)


@pytest.mark.slow
def test_ground_truth_file_written(config: AppConfig, tmp_path: Path) -> None:
    eng = Engine(config, sim_for(config, "hidden"), tmp_path / "run", duration_s=1000)
    eng.run(speed=0)
    gt = list(read_jsonl(tmp_path / "run" / "ground_truth.jsonl"))
    assert {g["type"] for g in gt} >= {"hidden_drift", "hidden_load"}
    assert all(g["category"] in ("real", "false", "hidden", "trap") for g in gt)
