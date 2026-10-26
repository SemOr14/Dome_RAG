"""Тесты накопления свидетельств и жизненного цикла вердиктов."""

from __future__ import annotations

import math
from datetime import timedelta

import pytest

from kupol.core.config import AppConfig
from kupol.core.models import Alert, AlertLevel, Evidence, Verdict
from kupol.core.verdicts import (
    VerdictBook,
    SubjectEvidence,
    context_evidence,
    evidence_summary,
    sigmoid,
    side_of,
)
from tests.conftest import T0, make_telemetry

SENSOR = "co2_ppm.co2_b"


def at(sec: float):  # type: ignore[no-untyped-def]
    return T0 + timedelta(seconds=sec)


def ev(source: str, supports: str, weight: float, rule: str = "r", hint: str = "", ts=None) -> Evidence:  # type: ignore[no-untyped-def]
    return Evidence(source=source, supports=supports, weight=weight, detail=f"{source}:{rule}", rule=rule, kind_hint=hint, ts=ts)  # type: ignore[arg-type]


def feed(book: VerdictBook, now, *items: tuple[str, Evidence]):  # type: ignore[no-untyped-def]
    return book.update(now, make_telemetry(0), [], [], [SubjectEvidence(s, e, [s]) for s, e in items])


@pytest.fixture
def book(config: AppConfig) -> VerdictBook:
    return VerdictBook(config)


# --------------------------------------------------------------------------- агрегация


def test_sigmoid_is_stable() -> None:
    assert sigmoid(0) == 0.5 and sigmoid(1000) == 1.0 and sigmoid(-1000) == 0.0
    assert sigmoid(2) == pytest.approx(1 / (1 + math.exp(-2)))
    assert sigmoid(-2) == pytest.approx(1 - sigmoid(2))


def test_confidence_follows_logistic_sum_of_weights(book: VerdictBook, config: AppConfig) -> None:
    scale = config.verdicts.aggregation.scale
    out = feed(book, T0, (SENSOR, ev("co2_ppm.co2_a", "false", 0.5, "co2_outlier", "false_signal")))
    (v,) = out.verdicts
    assert v.kind == "false_signal" and v.confidence == pytest.approx(sigmoid(scale * 0.5), abs=1e-3)
    out = feed(book, T0, (SENSOR, ev("co2_ppm.co2_c", "false", 0.5, "co2_outlier", "false_signal")))
    assert out.verdicts[0].confidence == pytest.approx(min(sigmoid(scale * 1.0), config.verdicts.max_confidence), abs=1e-3)
    assert {e.source for e in out.verdicts[0].evidence} == {"co2_ppm.co2_a", "co2_ppm.co2_c"}
    assert out.verdicts[0].sources_for("false") == {"co2_ppm.co2_a", "co2_ppm.co2_c"}


def test_mixed_evidence_gives_unknown_in_the_middle_band(book: VerdictBook) -> None:
    out = feed(book, T0, (SENSOR, ev("a", "false", 0.3)), (SENSOR, ev("b", "real", 0.25, "r2")))
    (v,) = out.verdicts
    assert v.kind == "unknown" and 0.5 <= v.confidence < 0.6


def test_confidence_capped(book: VerdictBook, config: AppConfig) -> None:
    out = feed(book, T0, *((SENSOR, ev(f"s{i}", "real", 1.0, f"r{i}")) for i in range(6)))
    assert out.verdicts[0].kind == "real" and out.verdicts[0].confidence == config.verdicts.max_confidence


def test_side_of() -> None:
    assert side_of("real") == "real" and side_of("drift") == "false" and side_of("unknown") == "unknown"


def test_context_alone_never_makes_a_verdict(book: VerdictBook) -> None:
    out = feed(book, T0, (SENSOR, ev("context", "real", 0.2, "context_work")))
    assert out.verdicts[0].kind == "unknown"
    out = feed(book, at(1), (SENSOR, ev("co2_ppm.co2_a", "real", 0.5, "co2_consensus_high")), (SENSOR, ev("context", "real", 0.2, "context_work")))
    assert out.verdicts[0].kind == "real"  # вместе с другими свидетельствами контекст повышает «реальный»


def test_context_evidence_helper() -> None:
    soldering = make_telemetry(0, solder_status="running")
    got = context_evidence(soldering, [SENSOR, "temp_c", "battery_soc_pct"], 0.2)
    assert [g.subject for g in got] == [SENSOR, "temp_c"] and all(g.evidence.rule == "context_work" for g in got)
    assert context_evidence(make_telemetry(0, cnc_status="running"), [SENSOR, "temp_c"], 0.2)[0].subject == "temp_c"
    assert context_evidence(make_telemetry(0), [SENSOR], 0.2) == [] and context_evidence(None, [SENSOR], 0.2) == []
    assert context_evidence(soldering, [SENSOR], 0.0) == []


# --------------------------------------------------------------------------- жизненный цикл


def test_provisional_then_confirmed_after_recheck(book: VerdictBook, config: AppConfig) -> None:
    recheck = config.verdicts.recheck_after_s
    out = feed(book, T0, (SENSOR, ev("a", "false", 0.9, "co2_outlier", "false_signal")))
    (v,) = out.verdicts
    assert v.status == "provisional" and v.created_at == T0 and v.recheck_at == at(recheck)
    assert [e.event for e in out.events] == ["created"]
    out = feed(book, at(recheck - 1), (SENSOR, ev("a", "false", 0.9, "co2_outlier", "false_signal")))
    assert out.verdicts[0].status == "provisional" and not out.events
    out = feed(book, at(recheck + 1), (SENSOR, ev("a", "false", 0.9, "co2_outlier", "false_signal")))
    assert out.verdicts[0].status == "confirmed" and [e.event for e in out.events] == ["confirmed"]
    assert out.verdicts[0].recheck_at == at(recheck + 1 + recheck)  # перепроверка продолжается


def test_false_verdict_is_revised_when_real_evidence_appears(book: VerdictBook, config: AppConfig) -> None:
    """5: «ложный» не закрывается навсегда: новые подтверждающие свидетельства дают пересмотр и тревогу."""
    watch = config.verdicts.watch_after_false_s
    feed(book, T0, (SENSOR, ev("a", "false", 0.9, "co2_outlier", "false_signal")), (SENSOR, ev("c", "false", 0.9, "rate_limit", "false_signal")))
    first = book.get(SENSOR)
    assert first is not None and first.is_false
    alert = None
    revised_at = None
    for t in range(1, int(watch)):
        out = feed(
            book,
            at(t),
            *(
                (SENSOR, ev(f"co2_ppm.co2_{x}", "real", 0.6, "co2_consensus_rise"))
                for x in ("a", "c")
            )
            if t >= 100
            else (),
        )
        revisions = [e for e in out.events if e.event == "revised"]
        if revisions:
            revised_at, alert = t, out.alerts
            break
    assert revised_at is not None and revised_at <= watch  # пересмотр не позже заданного времени
    (new,) = book.active()
    assert new.kind == "real" and new.id != first.id and new.status == "provisional"
    assert any(a.kind == "verdict_revised" and a.level is AlertLevel.CRITICAL for a in alert or [])
    assert [v for v in book.archive if v.id == first.id][0].status == "refuted"
    assert book.revisions == 1


def test_false_verdict_stays_under_watch_then_retires_but_reopens(book: VerdictBook, config: AppConfig) -> None:
    watch = config.verdicts.watch_after_false_s
    feed(book, T0, (SENSOR, ev("a", "false", 0.9, "co2_outlier", "false_signal")))
    for t in (60, 120, 200, watch - 1):
        out = feed(book, at(t))
        assert out.verdicts and out.verdicts[0].is_false, t  # всё ещё под наблюдением
    out = feed(book, at(watch + 1))
    assert out.verdicts == [] and "retired" in [e.event for e in out.events]
    # новые свидетельства открывают вердикт заново, а не «закрытый навсегда»
    out = feed(book, at(watch + 100), (SENSOR, ev("a", "real", 0.9, "co2_consensus_high")))
    assert out.verdicts[0].kind == "real"


def test_evidence_decays_with_age(book: VerdictBook, config: AppConfig) -> None:
    feed(book, T0, (SENSOR, ev("a", "real", 0.8, "r")))
    v0 = book.get(SENSOR)
    assert v0 is not None
    half = config.verdicts.evidence_half_life_s
    out = feed(book, at(half))
    assert out.verdicts[0].evidence[0].weight == pytest.approx(0.4, abs=0.01)
    assert out.verdicts[0].confidence < v0.confidence
    out = feed(book, at(half * 8))
    assert out.verdicts == []  # выдохлось


def test_revision_not_triggered_by_unrelated_stale_verdict(book: VerdictBook, config: AppConfig) -> None:
    feed(book, T0, (SENSOR, ev("a", "real", 0.9, "x")))
    later = config.verdicts.watch_after_false_s * 3
    out = feed(book, at(later), (SENSOR, ev("b", "false", 0.9, "y", "false_signal")))
    assert not [e for e in out.events if e.event == "revised"]  # старое «реальное» давно истекло


def test_flip_through_unknown_is_still_a_revision(book: VerdictBook) -> None:
    feed(book, T0, (SENSOR, ev("a", "real", 0.9, "x")))
    book._tracks[SENSOR].evidence.clear()  # вывод снова «не определён»
    feed(book, at(5))
    assert book.get(SENSOR) is not None and book.get(SENSOR).kind == "unknown"  # type: ignore[union-attr]
    out = feed(book, at(6), (SENSOR, ev("b", "false", 0.9, "y", "false_signal")))
    assert [e.event for e in out.events if e.event == "revised"] == ["revised"]


def test_false_kind_hysteresis(book: VerdictBook) -> None:
    feed(book, T0, (SENSOR, ev("a", "false", 0.6, "pair_diff", "sensor_fault")))
    assert book.get(SENSOR).kind == "sensor_fault"  # type: ignore[union-attr]
    out = feed(book, at(1), (SENSOR, ev("b", "false", 0.65, "cusum", "drift")))
    assert out.verdicts[0].kind == "sensor_fault"  # близкий вес не переключает вид
    out = feed(book, at(2), (SENSOR, ev("d", "false", 0.9, "cusum2", "drift")))
    assert out.verdicts[0].kind == "drift" and [e.event for e in out.events] == ["reclassified"]


# --------------------------------------------------------------------------- ввод извне


def test_external_evidence_and_manual_close(book: VerdictBook) -> None:
    book.add_evidence(SENSOR, ev("operator", "false", 0.9, "operator", "false_signal"), T0)
    out = book.update(T0, None, [], [])
    assert out.verdicts[0].kind == "false_signal" and out.verdicts[0].evidence[0].source == "operator"
    vid = out.verdicts[0].id
    assert book.by_id(vid) is not None
    closed = book.close_manual(vid, "диагност", T0)
    assert closed is not None and closed.event == "closed" and "диагност" in closed.reason
    assert book.active() == [] and book.close_manual(vid, "x", T0) is None
    assert book.by_id(vid) is None and book.archive[-1].status == "refuted"


def test_retract(book: VerdictBook) -> None:
    feed(book, T0, ("actuator:vent", ev("state", "real", 0.5, "cause_effect")), ("actuator:vent", ev("power", "real", 0.5, "cause_effect")))
    assert book.retract("actuator:vent", rule="cause_effect", supports="real") == 2
    assert book.retract("actuator:vent") == 0 and book.retract("nope") == 0


# --------------------------------------------------------------------------- свидетельства из правил и тревог


def test_evidence_from_legacy_rules(book: VerdictBook) -> None:
    tel = make_telemetry(0, co2_ppm={"co2_a": 800.0, "co2_b": 5000.0, "co2_c": 810.0})
    outlier = Verdict(ts=T0, kind="false_signal", confidence=0.9, explanation="e", rule="co2_outlier", fields=[SENSOR])
    items = book.evidence_from_rule(outlier, tel)
    assert {i.evidence.source for i in items} == {"co2_ppm.co2_a", "co2_ppm.co2_c"} and all(i.evidence.supports == "false" for i in items)
    assert all(i.subject == SENSOR and i.evidence.kind_hint == "false_signal" for i in items)
    cons = Verdict(ts=T0, kind="real", confidence=0.8, explanation="e", rule="co2_consensus_high", fields=["co2_ppm.co2_a", "co2_ppm.co2_b", "co2_ppm.co2_c"])
    items = book.evidence_from_rule(cons, tel)
    assert len(items) == 6 and {i.subject for i in items} == set(cons.fields)
    assert all(i.subject != i.evidence.source for i in items)  # датчик не подтверждает сам себя
    bal = Verdict(ts=T0, kind="real", confidence=0.6, explanation="e", rule="energy_balance", fields=["battery_soc_pct", "pv_power_w", "load_total_w"])
    (it,) = book.evidence_from_rule(bal, tel)
    assert it.subject == "energy_balance" and it.alerting and it.evidence.source == "energy_balance"
    unknown = Verdict(ts=T0, kind="unknown", confidence=0.4, explanation="e", rule="co2_pair_disagreement", fields=["co2_ppm.co2_a"])
    (it,) = book.evidence_from_rule(unknown, tel)
    assert it.evidence.supports == "neutral"
    temp = Verdict(ts=T0, kind="sensor_fault", confidence=0.7, explanation="e", rule="battery_temp_plausibility", fields=["battery_temp_c"])
    assert book.evidence_from_rule(temp, tel)[0].evidence.kind_hint == "drift"
    dev = Verdict(ts=T0, kind="sensor_fault", confidence=0.5, explanation="e", rule="device_status_vs_power", fields=["cnc_status", "lines.cnc.power_w"])
    assert book.evidence_from_rule(dev, tel)[0].evidence.kind_hint == "channel_fault"


def test_evidence_from_alerts(book: VerdictBook) -> None:
    def alert(kind: str, field: str | None) -> Alert:
        return Alert(ts=T0, level=AlertLevel.WARNING, kind=kind, field=field, message="m")

    assert book.evidence_from_alert(alert("rate_limit", SENSOR))[0].evidence.kind_hint == "false_signal"
    assert book.evidence_from_alert(alert("pair_diff", SENSOR))[0].evidence.source == "pair"
    assert book.evidence_from_alert(alert("noise_loss", "temp_c"))[0].evidence.supports == "false"
    assert book.evidence_from_alert(alert("freeze", "temp_c"))[0].evidence.source == "flatline"
    ts = book.evidence_from_alert(alert("time_monotonic", "ts"))[0]
    assert ts.subject == "ts" and ts.evidence.kind_hint == "channel_fault"
    eb = book.evidence_from_alert(alert("cusum", "energy_balance"))[0]
    assert eb.subject == "energy_balance" and eb.evidence.supports == "real" and eb.alerting
    pair = book.evidence_from_alert(alert("cusum", "temp_c|temp_c_2"))[0]
    assert pair.evidence.kind_hint == "drift" and pair.evidence.supports == "false"
    delayed = book.evidence_from_alert(alert("actuator_delayed", "vent"))[0]
    assert delayed.subject == "actuator:vent"
    assert book.evidence_from_alert(alert("threshold", SENSOR)) == [] and book.evidence_from_alert(alert("rate_limit", None)) == []


def test_claim_alerts_open_unknown_verdict_and_it_expires(book: VerdictBook, config: AppConfig) -> None:
    a = Alert(ts=T0, level=AlertLevel.CRITICAL, kind="threshold", field=SENSOR, message="m")
    out = book.update(T0, None, [], [a])
    assert out.verdicts[0].kind == "unknown" and out.verdicts[0].fields == [SENSOR]
    out = book.update(at(config.verdicts.recheck_after_s + 5), None, [], [])
    assert out.verdicts == []  # тревоги не повторяются, свидетельств нет


def test_alerting_rule_emits_alert_while_real(book: VerdictBook) -> None:
    rule = Verdict(
        ts=T0,
        kind="real",
        confidence=0.7,
        explanation="тихий брак",
        rule="plan_vs_fact",
        fields=["printer_status", "lines.printer.power_w"],
        evidence=[ev("power_meter", "real", 0.7, "plan_vs_fact")],
    )
    out = book.update(T0, make_telemetry(0), [rule], [])
    (a,) = [x for x in out.alerts if x.kind == "plan_vs_fact"]
    assert a.field == "printer_status" and a.level is AlertLevel.WARNING and a.details["sources"] == ["power_meter"]


def test_evidence_summary(book: VerdictBook) -> None:
    feed(book, T0, (SENSOR, ev("a", "real", 0.6, "r1")), (SENSOR, ev("b", "false", 0.1, "r2")))
    s = evidence_summary(book.get(SENSOR))  # type: ignore[arg-type]
    assert s["sources_real"] == ["a"] and s["sources_false"] == ["b"] and s["real"][0][0] == "a"


def test_active_sorted_by_definiteness(book: VerdictBook) -> None:
    feed(book, T0, ("x", ev("a", "real", 0.9)), ("y", ev("b", "false", 0.9, hint="drift")), ("z", ev("c", "neutral", 0.0)))
    assert [v.fields[0] for v in book.active()] == ["x", "y", "z"]
