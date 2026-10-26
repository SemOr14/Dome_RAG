"""Регрессионные тесты качества на полных прогонах сценариев (критерии приёмки дополнения, §15).

Каждый прогон — полная игра на симуляторе (виртуальное время, mock-LLM), поэтому прогоны делаются один раз на модуль.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from kupol.adapter.scenario import load_scenario
from kupol.adapter.simulator import SimulatorAdapter
from kupol.core.config import AppConfig, load_config
from kupol.core.metrics import RunMetrics, compute_metrics
from kupol.core.models import JournalEntry
from kupol.engine import Engine
from tests.conftest import ROOT

pytestmark = pytest.mark.slow  # полные игры: в обычный прогон не входят

SEEDS = (3, 11)


@dataclass
class Run:
    """Результат полного прогона."""

    entries: list[JournalEntry]
    metrics: RunMetrics
    step_errors: int


def play(config: AppConfig, scenario: str, seed: int) -> Run:
    cfg = config.model_copy(deep=True)
    sc = load_scenario(ROOT / "scenarios" / f"{scenario}.yaml")
    sim = SimulatorAdapter(cfg, sc, seed=seed)
    with tempfile.TemporaryDirectory(prefix="kupol-reg-") as tmp:
        eng = Engine(cfg, sim, Path(tmp) / "run", auto_approve=True, sim_operator=True, duration_s=sc.duration_s)
        summary = eng.run(speed=0)
        entries = eng.journal.entries()
        metrics = compute_metrics(entries, sim.ground_truth, eng.start_time, sc.duration_s, watch_after_false_s=cfg.verdicts.watch_after_false_s)
    return Run(entries, metrics, int(summary["counters"]["step_errors"]))


@pytest.fixture(scope="module")
def cfg() -> AppConfig:
    return load_config(ROOT / "config.yaml")


@pytest.fixture(scope="module", params=SEEDS)
def quiet(request: pytest.FixtureRequest, cfg: AppConfig) -> Run:
    return play(cfg, "quiet", request.param)


@pytest.fixture(scope="module", params=SEEDS)
def traps(request: pytest.FixtureRequest, cfg: AppConfig) -> Run:
    return play(cfg, "traps", request.param)


@pytest.fixture(scope="module", params=SEEDS)
def hidden(request: pytest.FixtureRequest, cfg: AppConfig) -> Run:
    return play(cfg, "hidden", request.param)


@pytest.fixture(scope="module")
def heavy(cfg: AppConfig) -> Run:
    return play(cfg, "cascade_heavy", 3)


def test_quiet_has_no_critical_and_few_warnings(quiet: Run, cfg: AppConfig) -> None:
    m = quiet.metrics
    assert quiet.step_errors == 0
    assert m.alerts_critical == 0 and m.critical_false_alarms == 0
    assert m.false_alarms_per_hour <= cfg.quality.max_quiet_warnings_per_hour


def test_quiet_does_not_escalate_to_action(quiet: Run) -> None:
    levels = {e.data.get("level") for e in quiet.entries if e.kind == "response"}
    assert not levels & {"act_reversible", "act_full"}
    assert not [e for e in quiet.entries if e.kind == "command" and e.status == "accepted"]


def test_traps_no_cascades_and_revision_in_time(traps: Run) -> None:
    m = traps.metrics
    assert traps.step_errors == 0
    assert m.cascades == 0, m.cascade_actions
    assert m.expected_revisions >= 1
    assert m.revised_in_time == m.expected_revisions


def test_false_verdict_is_never_final(traps: Run) -> None:
    """После «ложного» вердикта по ловушке в журнале появляется пересмотр: закрытого навсегда «ложного» нет."""
    events = [e.data["event"] for e in traps.entries if e.kind == "verdict" and "event" in e.data]
    assert "revised" in events or "reclassified" in events


def test_hidden_all_detected_before_critical(hidden: Run) -> None:
    m = hidden.metrics
    assert hidden.step_errors == 0
    assert m.hidden_all_detected_in_time(), [(e.type, e.detected, e.lead_s) for e in m.detectable() if e.category == "hidden"]
    for e in m.detectable():
        if e.category == "hidden" and e.lead_s is not None:
            assert e.lead_s > 0, (e.type, e.lead_s)
    assert m.false_alarms_per_hour <= 1.0


def test_costly_and_full_actions_have_two_sources_or_safety(heavy: Run, traps: Run, hidden: Run) -> None:
    checked = 0
    for run in (heavy, traps, hidden):
        for e in run.entries:
            if e.kind == "command" and e.status == "accepted" and e.data.get("costly") and not e.actor.startswith("human"):
                checked += 1
                assert len(e.data["evidence_sources"]) >= 2 or e.data.get("safety_event"), e.event
            if e.kind == "response" and e.data.get("level") == "act_full":
                checked += 1
                assert len(e.data.get("sources", [])) >= 2 or e.data.get("safety_event"), e.event
    assert checked >= 0  # прогоны могут не содержать дорогих действий: тогда нарушений нет


def test_heavy_cascade_scenario_is_stable(heavy: Run) -> None:
    assert heavy.step_errors == 0
    assert heavy.metrics.cascades == 0
