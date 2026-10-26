"""Тесты моделей данных."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from kupol.core.models import (
    Alert,
    AlertLevel,
    Command,
    CommandResult,
    CommandStatus,
    DeviceStatus,
    Hypothesis,
    JournalEntry,
    LineState,
    Recommendation,
    Telemetry,
    Verdict,
    ensure_utc,
    new_id,
    utcnow,
)

T0 = datetime(2026, 10, 27, 10, 0, 0, tzinfo=UTC)


def make_tel(**kw: object) -> Telemetry:
    base: dict[str, object] = dict(
        ts=T0,
        pv_power_w=800.0,
        battery_soc_pct=80.0,
        battery_voltage_v=51.2,
        load_total_w=500.0,
        temp_c=22.0,
        humidity_pct=45.0,
        co2_ppm={"co2_a": 600.0, "co2_b": 610.0},
        lines={"cnc": LineState(on=True, power_w=300.0)},
    )
    base.update(kw)
    return Telemetry(**base)  # type: ignore[arg-type]


def test_utcnow_is_aware() -> None:
    assert utcnow().tzinfo is not None


def test_ensure_utc_naive_and_foreign_zone() -> None:
    naive = datetime(2026, 1, 1, 12, 0)
    assert ensure_utc(naive).tzinfo == UTC
    msk = datetime(2026, 1, 1, 15, 0, tzinfo=timezone(timedelta(hours=3)))
    assert ensure_utc(msk) == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_new_id_unique_and_prefixed() -> None:
    a, b = new_id("x"), new_id("x")
    assert a != b and a.startswith("x-")
    assert "-" not in new_id()


def test_telemetry_normalizes_time_to_utc() -> None:
    tel = make_tel(ts=datetime(2026, 10, 27, 13, 0, tzinfo=timezone(timedelta(hours=3))))
    assert tel.ts == T0


def test_telemetry_serializes_iso() -> None:
    dumped = make_tel().model_dump(mode="json")
    assert dumped["ts"].startswith("2026-10-27T10:00:00")
    assert Telemetry.model_validate(dumped) == make_tel()


def test_telemetry_flat_and_get() -> None:
    tel = make_tel(battery_temp_c=30.0)
    flat = tel.flat()
    assert flat["co2_ppm.co2_a"] == 600.0
    assert flat["lines.cnc.power_w"] == 300.0
    assert flat["lines.cnc.on"] == 1.0
    assert tel.get("battery_temp_c") == 30.0
    assert tel.get("nope") is None


def test_telemetry_lookup_paths() -> None:
    tel = make_tel(vent_mode="low", cnc_status="running")
    assert tel.lookup("vent_mode") == "low"
    assert tel.lookup("cnc_status") == "running"  # StrEnum -> str
    assert tel.lookup("lines.cnc.on") is True
    assert tel.lookup("co2_ppm.co2_a") == 600.0
    assert tel.lookup("lines.ghost.on") is None
    assert tel.lookup("temp_c.deeper") is None


def test_telemetry_flat_without_battery_temp() -> None:
    assert "battery_temp_c" not in make_tel().flat()


def test_telemetry_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        make_tel(bogus=1)


def test_line_state_negative_power_rejected() -> None:
    with pytest.raises(ValidationError):
        LineState(on=True, power_w=-1.0)


def test_device_status_values() -> None:
    assert make_tel().cnc_status is DeviceStatus.UNKNOWN
    assert make_tel(cnc_status="running").cnc_status is DeviceStatus.RUNNING


def test_alert_level_rank_and_key() -> None:
    assert AlertLevel.INFO.rank < AlertLevel.WARNING.rank < AlertLevel.CRITICAL.rank
    alert = Alert(ts=T0, level=AlertLevel.WARNING, kind="threshold", field="temp_c", message="m")
    assert alert.key == "threshold:temp_c"
    assert Alert(ts=T0, level=AlertLevel.INFO, kind="stale", message="m").key == "stale:-"


def test_verdict_confidence_bounds() -> None:
    Verdict(ts=T0, kind="real", confidence=1.0, explanation="e")
    with pytest.raises(ValidationError):
        Verdict(ts=T0, kind="real", confidence=1.5, explanation="e")
    with pytest.raises(ValidationError):
        Verdict(ts=T0, kind="weird", confidence=0.5, explanation="e")  # type: ignore[arg-type]


def test_command_defaults_and_result() -> None:
    cmd = Command(name="set_vent_mode", args={"mode": "low"})
    assert cmd.id.startswith("cmd-") and cmd.ts.tzinfo is not None
    res = CommandResult(command_id=cmd.id, status=CommandStatus.ACCEPTED)
    assert res.model_dump(mode="json")["status"] == "accepted"


def test_journal_entry_defaults() -> None:
    entry = JournalEntry(actor="monitor", event="e")
    assert entry.kind == "decision" and entry.data == {}
    assert JournalEntry.model_validate_json(entry.model_dump_json()) == entry


def test_recommendation_priority_bounds_and_hypothesis() -> None:
    rec = Recommendation(
        agent="diagnost",
        title="t",
        hypotheses=[Hypothesis(cause="c", probability=0.7, safe_check="проверить")],
    )
    assert rec.priority == 3 and rec.needs_human and not rec.used_fallback
    with pytest.raises(ValidationError):
        Recommendation(agent="a", title="t", priority=9)


# --------------------------------------------------------------------------- расширение: вердикты, свидетельства, пробы


def test_verdict_extended_defaults_and_serialization() -> None:
    from kupol.core.models import Evidence

    v = Verdict(
        ts=T0,
        kind="drift",
        confidence=0.8,
        explanation="e",
        evidence=[Evidence(source="co2_ppm.co2_b", supports="false", weight=0.5, detail="d", kind_hint="drift")],
    )
    assert v.status == "provisional" and v.created_at == T0 and v.recheck_at is None and v.id.startswith("vd-")
    assert v.is_false and v.sources_for("false") == {"co2_ppm.co2_b"} and v.sources_for("real") == set()
    assert Verdict.model_validate_json(v.model_dump_json()) == v
    assert not Verdict(ts=T0, kind="real", confidence=0.7, explanation="e").is_false


def test_verdict_new_kinds_and_status_validated() -> None:
    for kind in ("channel_fault", "drift"):
        Verdict(ts=T0, kind=kind, confidence=0.5, explanation="e")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Verdict(ts=T0, kind="real", confidence=0.5, explanation="e", status="closed")  # type: ignore[arg-type]


def test_evidence_weight_and_support_validated() -> None:
    from kupol.core.models import Evidence

    Evidence(source="s", supports="neutral", weight=0.0)
    with pytest.raises(ValidationError):
        Evidence(source="s", supports="real", weight=1.5)
    with pytest.raises(ValidationError):
        Evidence(source="s", supports="maybe", weight=0.5)  # type: ignore[arg-type]


def test_probe_model_and_response_level_order() -> None:
    from kupol.core.models import Probe, ProbeRisk, ResponseLevel

    p = Probe(
        id="vent_min_30s",
        description="d",
        cost_wh=0.5,
        cost_s=30,
        risk=ProbeRisk.LOW,
        expected_if_real="CO2 падает на всех датчиках",
        expected_if_false="подозрительный датчик не реагирует",
        duration_s=30,
        command=Command(name="vent_enable", args={"mode": "low"}),
    )
    assert Probe.model_validate_json(p.model_dump_json()) == p and p.reversible
    levels = [ResponseLevel.OBSERVE, ResponseLevel.PREPARE, ResponseLevel.ACT_REVERSIBLE, ResponseLevel.ACT_FULL]
    assert [lv.rank for lv in levels] == [0, 1, 2, 3]
    with pytest.raises(ValidationError):
        Probe(id="x", description="d", expected_if_real="a", expected_if_false="b", risk="high")  # type: ignore[arg-type]


def test_hypothesis_extended_and_command_flags() -> None:
    h = Hypothesis(cause="датчик врёт", probability=0.6, safe_check="сверить", safe_probe="recheck_neighbor")
    assert h.id.startswith("hyp-") and h.description == "датчик врёт" and h.related_alerts == []
    c = Command(name="set_line", args={"line": "aux", "enabled": False}, costly=True, safety_event="smoke")
    assert c.costly and c.evidence == [] and CommandStatus.DOWNGRADED.value == "downgraded"


def test_alert_new_fields_and_temp_c_2() -> None:
    a = Alert(ts=T0, level=AlertLevel.WARNING, kind="pair_diff", message="m", detector="pair_diff", expected=0.0, confidence=0.9, window={"n": 60})
    assert a.detector == "pair_diff" and a.window["n"] == 60
    tel = make_tel(temp_c_2=22.1)
    assert tel.flat()["temp_c_2"] == 22.1 and "temp_c_2" not in make_tel().flat()
