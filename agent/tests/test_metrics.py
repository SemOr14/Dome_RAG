"""Тесты метрик качества и бенчмарка (на синтетических журналах и коротком прогоне)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from kupol.benchmark import BenchResult, parse_seeds, run_one, summarize
from kupol.core.metrics import compute_metrics, metrics_markdown
from kupol.core.models import JournalEntry
from kupol.report import build_report
from tests.conftest import ROOT, T0

START = T0


def alert(t: float, kind: str, field: str, level: str = "warning") -> JournalEntry:
    return JournalEntry(
        ts=START + timedelta(seconds=t), actor="detector", event="alert", kind="alert",
        data={"alert": {"kind": kind, "field": field, "level": level, "message": "m"}},
    )


def verdict(t: float, event: str, field: str, kind: str, status: str = "provisional") -> JournalEntry:
    return JournalEntry(
        ts=START + timedelta(seconds=t), actor="verdicts", event=event, kind="verdict",
        data={"event": event, "verdict": {"fields": [field], "kind": kind, "status": status}},
    )


def gt(id_: str, typ: str, category: str, field: str, t0: float, t1: float | None, **extra: Any) -> dict[str, Any]:
    return {"id": id_, "type": typ, "category": category, "field": field, "t_start": t0, "t_end": t1, "source": "x", **extra}


def test_hidden_drift_detected_before_critical() -> None:
    truth = [gt("g1", "hidden_drift", "hidden", "co2_ppm.co2_a", 100, 900)]
    entries = [alert(160, "trend", "co2_ppm.co2_a"), alert(700, "threshold", "co2_ppm.co2_a", "critical")]
    m = compute_metrics(entries, truth, START, 1800)
    ev = m.events[0]
    assert ev.detected and ev.delay_s == 60 and ev.signal == "trend"
    assert ev.lead_s == 540 and ev.lead_ok
    assert m.hidden_all_detected_in_time()
    assert m.false_alarms_per_hour == 0


def test_late_detection_marks_lead_not_ok() -> None:
    truth = [gt("g1", "hidden_drift", "hidden", "co2_ppm.co2_a", 100, 900)]
    entries = [alert(500, "threshold", "co2_ppm.co2_a", "critical")]
    m = compute_metrics(entries, truth, START, 1800)
    assert not m.hidden_all_detected_in_time() or m.events[0].lead_s in (None, 0)


def test_missing_detection_counts() -> None:
    truth = [gt("g1", "frozen_sensor", "hidden", "humidity_pct", 100, 400)]
    m = compute_metrics([], truth, START, 1800)
    assert not m.events[0].detected
    assert m.detection_rate("hidden") == 0.0
    assert not m.hidden_all_detected_in_time()


def test_false_alarm_outside_windows_counted_per_hour() -> None:
    truth = [gt("g1", "frozen_sensor", "hidden", "humidity_pct", 100, 400)]
    entries = [alert(150, "freeze", "humidity_pct"), alert(1500, "threshold", "co2_ppm.co2_a", "critical"), alert(2000, "jump", "temp_c")]
    m = compute_metrics(entries, truth, START, 3600)
    assert len(m.false_alarms) == 2
    assert m.critical_false_alarms == 1
    assert m.false_alarms_per_hour == pytest.approx(2.0)


def test_quiet_run_without_truth_has_no_false_alarms() -> None:
    m = compute_metrics([], [], START, 5400)
    assert m.false_alarms_per_hour == 0 and m.cascades == 0 and not m.events


def test_revision_in_time_and_late() -> None:
    truth = [gt("g1", "false_signal", "false", "co2_ppm.co2_b", 100, 130, source="false_then_real:1")]
    ok = [verdict(105, "created", "co2_ppm.co2_b", "false_signal"), verdict(300, "revised", "co2_ppm.co2_b", "real")]
    late = [verdict(105, "created", "co2_ppm.co2_b", "false_signal"), verdict(500, "revised", "co2_ppm.co2_b", "real")]
    m_ok = compute_metrics(ok, truth, START, 1800, watch_after_false_s=300)
    m_late = compute_metrics(late, truth, START, 1800, watch_after_false_s=300)
    assert (m_ok.expected_revisions, m_ok.revised_in_time) == (1, 1)
    assert (m_late.expected_revisions, m_late.revised_in_time) == (1, 0)


def test_cascade_counts_harmful_command_in_trap_window() -> None:
    truth = [gt("g1", "cascade_trap", "trap", "co2_ppm.co2_a", 100, 160, harmful=["set_vent_mode:high"], window_s=300)]
    cmd = JournalEntry(
        ts=START + timedelta(seconds=150), actor="monitor", event="cmd", kind="command", status="accepted",
        data={"command": "set_vent_mode", "args": {"mode": "high"}},
    )
    safe = JournalEntry(
        ts=START + timedelta(seconds=151), actor="monitor", event="cmd", kind="command", status="accepted",
        data={"command": "set_vent_mode", "args": {"mode": "low"}},
    )
    m = compute_metrics([cmd, safe], truth, START, 1800)
    assert m.cascades == 1 and "set_vent_mode:high" in m.cascade_actions[0]


def test_markdown_has_sections_and_handles_no_truth() -> None:
    text = "\n".join(metrics_markdown(compute_metrics([], [], START, 600)))
    assert "Метрики качества" in text and "разметки нет" in text
    truth = [gt("g1", "frozen_sensor", "hidden", "humidity_pct", 100, 400)]
    text = "\n".join(metrics_markdown(compute_metrics([alert(150, "freeze", "humidity_pct")], truth, START, 1800)))
    assert "frozen_sensor" in text and "Каскады" in text


@pytest.mark.parametrize(("text", "expected"), [("1-3", [1, 2, 3]), ("5", [5]), ("1,3,2-4", [1, 2, 3, 4]), (" 7 , 8 ", [7, 8])])
def test_parse_seeds(text: str, expected: list[int]) -> None:
    assert parse_seeds(text) == expected


@pytest.mark.parametrize("bad", ["", "3-1", "a", ","])
def test_parse_seeds_rejects_bad(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_seeds(bad)


def test_summarize_builds_acceptance_section() -> None:
    from kupol.core.metrics import RunMetrics

    results = [
        BenchResult("quiet", 1, RunMetrics(duration_s=5400), 1.0, 0),
        BenchResult("traps", 1, RunMetrics(duration_s=5400), 1.0, 0),
    ]
    text = summarize(results, 1.0, [1])
    assert "Сводка по сценариям" in text and "`quiet`" in text and "`traps`" in text
    assert "❌" not in text


@pytest.mark.slow
def test_run_one_quiet_has_no_critical_alarms(tmp_path: Path) -> None:
    res = run_one(str(ROOT / "config.yaml"), str(ROOT / "scenarios" / "quiet.yaml"), 3)
    assert res.scenario == "quiet" and res.step_errors == 0
    assert res.metrics.critical_false_alarms == 0
    assert res.metrics.false_alarms_per_hour <= 1.0
    assert res.metrics.cascades == 0


@pytest.mark.slow
def test_report_contains_metrics_section(tmp_path: Path, config: Any) -> None:
    from kupol.adapter.scenario import load_scenario
    from kupol.adapter.simulator import SimulatorAdapter
    from kupol.engine import Engine

    sim = SimulatorAdapter(config, load_scenario(ROOT / "scenarios" / "basic.yaml"), seed=1)
    run = tmp_path / "run"
    Engine(config, sim, run, duration_s=900, auto_approve=True).run(speed=0)
    assert (run / "ground_truth.jsonl").exists()
    report = build_report(run)
    assert "Метрики качества" in report


@pytest.mark.slow
def test_benchmark_command_writes_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from kupol.main import main

    out = tmp_path / "BENCH.md"
    code = main([
        "benchmark", "--config", str(ROOT / "config.yaml"), "--scenarios", str(ROOT / "scenarios"),
        "--only", "quiet", "--seeds", "2", "--workers", "1", "--out", str(out),
    ])
    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert "Бенчмарк качества Штаба" in text and "quiet" in text


def test_benchmark_command_unknown_scenario(tmp_path: Path) -> None:
    from kupol.main import main

    assert main(["benchmark", "--scenarios", str(ROOT / "scenarios"), "--only", "нет-такого", "--out", ""]) != 0
