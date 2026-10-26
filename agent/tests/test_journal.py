"""Тесты журнала решений."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from kupol.core.journal import (
    Journal,
    TelemetryLog,
    new_run_dir,
    read_jsonl,
    read_telemetry,
)
from kupol.core.models import JournalEntry
from tests.conftest import T0, make_telemetry


@pytest.fixture
def journal(tmp_path: Path) -> Journal:
    j = Journal(tmp_path / "run", clock=lambda: T0)
    yield j  # type: ignore[misc]
    j.close()


def test_new_run_dir_unique(tmp_path: Path) -> None:
    a = new_run_dir(tmp_path, "basic scenario!")
    b = new_run_dir(tmp_path, "basic scenario!")
    assert a != b and a.is_dir() and b.is_dir()
    assert "basic_scenario_" in a.name


def test_log_fields_and_default_clock(journal: Journal) -> None:
    entry = journal.log(actor="monitor", event="CO2 скачок", status="open")
    assert entry.ts == T0
    loaded = journal.entries()
    assert len(loaded) == 1 and loaded[0].event == "CO2 скачок"


def test_log_prebuilt_entry_keeps_ts(journal: Journal) -> None:
    e = JournalEntry(ts=T0, actor="human", event="e")
    journal.log(e)
    assert journal.entries()[0] == e


def test_flushed_after_each_line_without_close(tmp_path: Path) -> None:
    j = Journal(tmp_path / "r")
    j.log(actor="a", event="e1")
    j.log(actor="a", event="e2")
    # не закрываем: данные должны уже лежать в файле (падение процесса)
    lines = (tmp_path / "r" / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    j.close()


def test_truncated_last_line_is_skipped(journal: Journal) -> None:
    journal.log(actor="a", event="ok")
    with journal.path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-10-27T10:00:00Z", "actor": "a", "ev')  # оборвано
    assert [e.event for e in journal.entries()] == ["ok"]


def test_log_llm_call(journal: Journal) -> None:
    e = journal.log_llm_call(
        agent="diagnost",
        prompt_hash="abc123",
        inputs_summary={"alerts": 2},
        raw_output="",
        parsed_decision=None,
        used_fallback=True,
        error="timeout",
    )
    assert e.kind == "llm_call" and e.status == "fallback"
    data = journal.entries(kinds=["llm_call"])[0].data
    assert data["used_fallback"] is True and data["prompt_hash"] == "abc123"
    assert data["error"] == "timeout" and data["agent"] == "diagnost"


def test_llm_call_ok_status(journal: Journal) -> None:
    e = journal.log_llm_call("monitor", "h", "s", "{}", {"a": 1}, False)
    assert e.status == "ok" and e.data["parsed_decision"] == {"a": 1}


def test_kinds_filter_and_tail(journal: Journal) -> None:
    for i in range(30):
        journal.log(actor="a", event=f"e{i}")
    journal.log_llm_call("a", "h", "s", "", None, True)
    assert len(journal.entries(kinds=["llm_call"])) == 1
    tail = journal.tail(20)
    assert len(tail) == 20 and tail[-1].event == "e29" and all(t.kind != "llm_call" for t in tail)


def test_export_csv(journal: Journal) -> None:
    journal.log(actor="a", event="e, с запятой", action="act")
    journal.log_llm_call("a", "h", "s", "", None, False)
    path = journal.export_csv()
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0][:3] == ["ts", "actor", "event"]
    assert len(rows) == 2 and rows[1][2] == "e, с запятой"


def test_export_markdown_escapes(journal: Journal) -> None:
    journal.log(actor="a", event="a|b\nc")
    text = journal.export_markdown().read_text(encoding="utf-8")
    assert "| ts | actor | event |" in text
    assert "a\\|b c" in text


def test_closed_journal_rejects_writes(tmp_path: Path) -> None:
    j = Journal(tmp_path / "r")
    j.close()
    j.close()  # повторное закрытие безопасно
    with pytest.raises(ValueError):
        j.log(actor="a", event="e")


def test_set_clock(journal: Journal) -> None:
    journal.set_clock(lambda: T0.replace(year=2027))
    assert journal.log(actor="a", event="e").ts.year == 2027


def test_read_jsonl_missing_file(tmp_path: Path) -> None:
    assert list(read_jsonl(tmp_path / "none.jsonl")) == []


def test_read_jsonl_skips_non_dict_and_blank(tmp_path: Path) -> None:
    p = tmp_path / "x.jsonl"
    p.write_text('\n[1,2]\n{"a": 1}\n', encoding="utf-8")
    assert list(read_jsonl(p)) == [{"a": 1}]


def test_invalid_entries_skipped(journal: Journal) -> None:
    with journal.path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"bogus": 1}) + "\n")
    journal.log(actor="a", event="ok")
    assert [e.event for e in journal.entries()] == ["ok"]


def test_telemetry_log_roundtrip(tmp_path: Path) -> None:
    log = TelemetryLog(tmp_path / "run")
    for i in range(3):
        log.write(make_telemetry(i))
    log.close()
    got = read_telemetry(tmp_path / "run")
    assert [t.ts for t in got] == [make_telemetry(i).ts for i in range(3)]


def test_read_telemetry_skips_bad(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "telemetry.jsonl").write_text('{"bad": 1}\n' + make_telemetry().model_dump_json() + "\n", encoding="utf-8")
    assert len(read_telemetry(run)) == 1
