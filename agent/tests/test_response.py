"""Тесты политики реакции, правила двух, защиты от каскада и исключения безопасности."""

from __future__ import annotations

from pathlib import Path

import pytest

from kupol.adapter.scenario import load_scenario
from kupol.adapter.simulator import SimulatorAdapter
from kupol.core.config import AppConfig
from kupol.core.energy import EnergyPlanner, energy_status
from kupol.core.guard import Guard
from kupol.core.journal import Journal
from kupol.core.models import AlertLevel, Command, CommandStatus, Evidence, LineState, ResponseLevel, Verdict
from kupol.core.response import ResponsePolicy
from kupol.core.state import State
from kupol.engine import Engine
from tests.conftest import ROOT, T0, make_telemetry
from tests.fakes import FakeAdapter

SENSOR = "co2_ppm.co2_b"


def verdict(kind: str = "real", conf: float = 0.9, sources: tuple[str, ...] = ()) -> Verdict:
    ev = [Evidence(source=s, supports="real" if kind == "real" else "false", weight=0.5, rule="r") for s in sources]
    return Verdict(ts=T0, kind=kind, confidence=conf, explanation="e", fields=[SENSOR], evidence=ev)  # type: ignore[arg-type]


@pytest.fixture
def policy(config: AppConfig) -> ResponsePolicy:
    return ResponsePolicy(config.response)


# --------------------------------------------------------------------------- уровни


@pytest.mark.parametrize(
    ("conf", "sources", "alert", "expected"),
    [
        (0.3, (), AlertLevel.CRITICAL, ResponseLevel.OBSERVE),  # низкая уверенность
        (0.55, (), AlertLevel.CRITICAL, ResponseLevel.PREPARE),  # средняя уверенность
        (0.4, ("a",), AlertLevel.CRITICAL, ResponseLevel.PREPARE),  # один источник
        (0.8, (), AlertLevel.CRITICAL, ResponseLevel.ACT_REVERSIBLE),  # высокая уверенность
        (0.4, ("a", "b"), AlertLevel.CRITICAL, ResponseLevel.ACT_REVERSIBLE),  # два независимых подтверждения
        (0.95, ("a", "b"), AlertLevel.CRITICAL, ResponseLevel.ACT_FULL),  # подтверждённая угроза
        (0.95, ("a",), AlertLevel.CRITICAL, ResponseLevel.ACT_REVERSIBLE),  # одного источника мало для act_full
        (0.95, ("a", "b"), AlertLevel.WARNING, ResponseLevel.ACT_REVERSIBLE),  # потолок по тревоге
        (0.95, ("a", "b"), AlertLevel.INFO, ResponseLevel.OBSERVE),
    ],
)
def test_levels_by_confidence_sources_and_alert(policy: ResponsePolicy, conf: float, sources: tuple[str, ...], alert: AlertLevel, expected: ResponseLevel) -> None:
    d = policy.decide(SENSOR, verdict("real", conf, sources), alert)
    assert d.level is expected and d.subject == SENSOR and d.reason
    assert d.independent_confirmations == len(sources)


def test_ceiling_is_explained(policy: ResponsePolicy) -> None:
    d = policy.decide(SENSOR, verdict("real", 0.95, ("a", "b")), AlertLevel.WARNING)
    assert "ограничено уровнем тревоги" in d.reason and d.confidence == 0.95 and d.sources == ["a", "b"]


def test_false_verdicts_only_observe(policy: ResponsePolicy) -> None:
    for kind in ("false_signal", "sensor_fault", "channel_fault", "drift"):
        d = policy.decide(SENSOR, verdict(kind, 0.99, ("a", "b")), AlertLevel.CRITICAL)
        assert d.level is ResponseLevel.OBSERVE and "не реален" in d.reason and d.confidence == 0.0


def test_unknown_or_missing_verdict_prepares_probe_only_for_real_alerts(policy: ResponsePolicy) -> None:
    assert policy.decide(SENSOR, verdict("unknown", 0.5), AlertLevel.WARNING).level is ResponseLevel.PREPARE
    assert policy.decide(SENSOR, None, AlertLevel.CRITICAL).level is ResponseLevel.PREPARE
    assert policy.decide(SENSOR, None, AlertLevel.INFO).level is ResponseLevel.OBSERVE


def test_forecast_alone_never_exceeds_prepare(policy: ResponsePolicy) -> None:
    d = policy.decide(SENSOR, verdict("real", 0.95, ("a", "b")), AlertLevel.CRITICAL, forecast_only=True)
    assert d.level is ResponseLevel.PREPARE and "прогноз" in d.reason


def test_safety_override_is_immediate_and_independent_of_verdict(policy: ResponsePolicy) -> None:
    d = policy.decide("smoke", None, AlertLevel.CRITICAL, safety_event="smoke")
    assert d.level is ResponseLevel.ACT_FULL and d.safety_event == "smoke" and "правило двух не применяется" in d.reason
    d2 = policy.decide(SENSOR, verdict("false_signal", 0.99), AlertLevel.WARNING, safety_event="operator_stop")
    assert d2.level is ResponseLevel.ACT_FULL  # слово Оператора «Стоп» важнее вердикта датчика
    unknown_event = policy.decide(SENSOR, None, AlertLevel.INFO, safety_event="coffee_break")
    assert unknown_event.level is ResponseLevel.OBSERVE and unknown_event.safety_event is None


def test_enough_for_costly(policy: ResponsePolicy) -> None:
    assert policy.enough_for_costly({"a", "b"}, None)[0]
    ok, why = policy.enough_for_costly({"a"}, None)
    assert not ok and "нужно не менее 2" in why and "есть 1" in why
    assert not policy.enough_for_costly(set(), None)[0]
    ok, why = policy.enough_for_costly(set(), "smoke")
    assert ok and "исключение безопасности" in why
    assert not policy.enough_for_costly(set(), "coffee_break")[0]


# --------------------------------------------------------------------------- guard: правило двух


@pytest.fixture
def env(config: AppConfig, tmp_path: Path):  # type: ignore[no-untyped-def]
    adapter = FakeAdapter()
    journal = Journal(tmp_path / "run", clock=adapter.now)
    state = State()
    state.update(make_telemetry(0))
    guard = Guard(config.guard, adapter, journal, state, response=config.response)
    yield guard, adapter, journal, state
    journal.close()


def high(**kw: object) -> Command:
    return Command(name="set_vent_mode", args={"mode": "high"}, **kw)  # type: ignore[arg-type]


def two() -> list[Evidence]:
    return [
        Evidence(source="co2_ppm.co2_a", supports="real", weight=0.5, rule="x"),
        Evidence(source="co2_ppm.co2_c", supports="real", weight=0.5, rule="x"),
    ]


def test_costly_without_two_confirmations_is_rejected(env) -> None:  # type: ignore[no-untyped-def]
    guard, adapter, journal, _ = env
    res = guard.submit(high(), "monitor")
    assert res.status is CommandStatus.REJECTED and "правилом двух" in res.message and "есть 0" in res.message
    one = guard.submit(high(evidence=two()[:1]), "monitor")
    assert one.status is CommandStatus.REJECTED and "есть 1" in one.message
    assert adapter.sent == [] and guard.pending() == []
    assert journal.entries(kinds=["command"])[-1].status == "rejected"


def test_independence_means_distinct_sources(env) -> None:  # type: ignore[no-untyped-def]
    guard, *_ = env
    same_source = [two()[0], two()[0].model_copy(update={"rule": "y"})]
    assert guard.submit(high(evidence=same_source), "monitor").status is CommandStatus.REJECTED
    zero_weight = [two()[0], two()[1].model_copy(update={"weight": 0.0})]
    assert guard.submit(high(evidence=zero_weight), "monitor").status is CommandStatus.REJECTED
    against = [two()[0], two()[1].model_copy(update={"supports": "false"})]
    assert guard.submit(high(evidence=against), "monitor").status is CommandStatus.REJECTED


def test_two_independent_confirmations_pass_to_human_queue(env) -> None:  # type: ignore[no-untyped-def]
    guard, adapter, journal, _ = env
    res = guard.submit(high(evidence=two()), "monitor")
    assert res.status is CommandStatus.PENDING and adapter.sent == []  # правило двух выполнено, класс needs_human остаётся
    data = journal.entries(kinds=["command"])[-1].data
    assert data["costly"] is True and data["evidence_sources"] == ["co2_ppm.co2_a", "co2_ppm.co2_c"]


def test_costly_flag_on_command_applies_even_if_rule_is_cheap(env) -> None:  # type: ignore[no-untyped-def]
    guard, *_ = env
    cheap = Command(name="vent_enable", args={"mode": "low"}, costly=True)
    assert guard.submit(cheap, "monitor").status is CommandStatus.REJECTED
    assert guard.submit(Command(name="vent_enable", args={"mode": "low"}), "monitor").status is CommandStatus.ACCEPTED


def test_set_line_is_costly_by_config(env) -> None:  # type: ignore[no-untyped-def]
    guard, *_ = env
    res = guard.submit(Command(name="set_line", args={"line": "aux", "enabled": False}), "energy_planner")
    assert res.status is CommandStatus.REJECTED and "правилом двух" in res.message


def test_human_commands_are_on_human_responsibility(env) -> None:  # type: ignore[no-untyped-def]
    guard, adapter, *_ = env
    assert guard.submit(high(), "human:диагност").status is CommandStatus.ACCEPTED and len(adapter.sent) == 1


# --------------------------------------------------------------------------- исключение безопасности


def test_safety_event_bypasses_rule_of_two_but_not_whitelist_or_ranges(env) -> None:  # type: ignore[no-untyped-def]
    """7: событие smoke обходит правило двух, но не белый список и диапазоны."""
    guard, adapter, *_ = env
    ok = guard.submit(high(safety_event="smoke"), "safety")
    assert ok.status is CommandStatus.PENDING  # правило двух не помешало (дальше — обычный класс needs_human)
    assert guard.submit(Command(name="reboot_controller", safety_event="smoke"), "safety").status is CommandStatus.REJECTED  # вне белого списка
    out_of_range = guard.submit(Command(name="set_vent_mode", args={"mode": "turbo"}, safety_event="smoke"), "safety")
    assert out_of_range.status is CommandStatus.REJECTED and "вне допустимого набора" in out_of_range.message
    forbidden = guard.submit(Command(name="disable_interlock", safety_event="smoke"), "safety")
    assert forbidden.status is CommandStatus.REJECTED and "ЗАПРЕЩЕНО" in forbidden.message
    assert adapter.sent == []


def test_unknown_safety_event_does_not_bypass(env) -> None:  # type: ignore[no-untyped-def]
    guard, *_ = env
    assert guard.submit(high(safety_event="coffee_break"), "monitor").status is CommandStatus.REJECTED


# --------------------------------------------------------------------------- защита от каскада


def test_cascade_risk_turns_command_into_recommendation(env) -> None:  # type: ignore[no-untyped-def]
    """6: команда, разряжающая АКБ ниже безопасного уровня, превращена в рекомендацию."""
    guard, adapter, journal, _ = env
    seen: list[tuple[str, str, str]] = []
    guard.cascade_check = lambda cmd: "вытяжка high разрядит АКБ: запас упадёт до 9 мин"
    guard.on_downgrade = lambda cmd, who, risk: seen.append((cmd.name, who, risk))
    res = guard.submit(high(evidence=two()), "monitor")
    assert res.status is CommandStatus.DOWNGRADED and "риск каскада" in res.message
    assert adapter.sent == [] and guard.pending() == []  # ни отправки, ни очереди подтверждения
    assert seen == [("set_vent_mode", "monitor", "вытяжка high разрядит АКБ: запас упадёт до 9 мин")]
    assert journal.entries(kinds=["command"])[-1].status == "downgraded"


def test_cascade_check_not_asked_for_cheap_or_human_or_safety(env) -> None:  # type: ignore[no-untyped-def]
    guard, adapter, *_ = env
    calls: list[str] = []
    guard.cascade_check = lambda cmd: calls.append(cmd.name) or "риск"
    assert guard.submit(Command(name="vent_enable", args={"mode": "low"}), "monitor").status is CommandStatus.ACCEPTED
    assert guard.submit(high(), "human:диагност").status is CommandStatus.ACCEPTED
    assert guard.submit(high(safety_event="smoke"), "safety").status is CommandStatus.PENDING
    assert calls == []


def test_cascade_check_passes_when_safe(env) -> None:  # type: ignore[no-untyped-def]
    guard, *_ = env
    guard.cascade_check = lambda cmd: None
    assert guard.submit(high(evidence=two()), "monitor").status is CommandStatus.PENDING


# --------------------------------------------------------------------------- что-если энергобаланса


def tight(config: AppConfig, soc: float, pv: float, load: float, **kw: object):  # type: ignore[no-untyped-def]
    st = State()
    lines = {"vent": LineState(on=True, power_w=60.0), "lighting": LineState(on=True, power_w=50.0), "aux": LineState(on=True, power_w=40.0)}
    for i in range(90):
        st.update(make_telemetry(i, battery_soc_pct=soc, pv_power_w=pv, load_total_w=load, lines=lines, **kw))
    return st.latest(), energy_status(st, config)


def test_command_risk_vent_high_when_energy_is_tight(config: AppConfig) -> None:
    planner = EnergyPlanner(config)
    tel, status = tight(config, 22.0, 300.0, 600.0)  # запас ≈ 80 Вт·ч: любая добавка нагрузки уводит ниже безопасного минимума
    risk = planner.command_risk(Command(name="set_vent_mode", args={"mode": "high"}), tel, status)
    assert risk is not None and "добавит ≈120 Вт" in risk and "безопасный минимум" in risk
    assert planner.command_risk(Command(name="set_vent_mode", args={"mode": "low"}), tel, status) is None  # не добавляет нагрузку


def test_command_risk_none_when_energy_is_plentiful_or_unknown(config: AppConfig) -> None:
    planner = EnergyPlanner(config)
    tel, status = tight(config, 90.0, 1800.0, 600.0)
    assert planner.command_risk(Command(name="set_vent_mode", args={"mode": "high"}), tel, status) is None
    assert planner.command_risk(Command(name="set_vent_mode", args={"mode": "high"}), None, None) is None


def test_command_risk_other_commands(config: AppConfig) -> None:
    planner = EnergyPlanner(config)
    tel, status = tight(config, 22.0, 300.0, 600.0)
    assert planner.command_risk(Command(name="set_line", args={"line": "aux", "enabled": True}), tel, status) is not None
    assert planner.command_risk(Command(name="set_humidifier_mode", args={"mode": "high"}), tel, status) is not None
    assert planner.command_risk(Command(name="set_line", args={"line": "aux", "enabled": False}), tel, status) is None
    tel_eco, status_eco = tight(config, 22.0, 300.0, 600.0, power_mode="eco")
    assert planner.command_risk(Command(name="set_power_mode", args={"mode": "normal"}), tel_eco, status_eco) is not None


def test_command_risk_stopping_uninterruptible_operation(config: AppConfig) -> None:
    planner = EnergyPlanner(config)
    tel, status = tight(config, 80.0, 1800.0, 600.0, cnc_status="running")
    risk = planner.command_risk(Command(name="set_line", args={"line": "cnc", "enabled": False}), tel, status)
    assert risk is not None and "неприрываемую операцию" in risk and "Фрезеровка" in risk
    assert planner.command_risk(Command(name="set_line", args={"line": "printer", "enabled": False}), tel, status) is None  # принтер не работает


# --------------------------------------------------------------------------- движок: каскад, безопасность, журнал


def run_engine(config: AppConfig, tmp_path: Path, scenario: str, steps: int) -> Engine:
    sim = SimulatorAdapter(config, load_scenario(ROOT / "scenarios" / f"{scenario}.yaml"), seed=42)
    eng = Engine(config, sim, tmp_path / "run", duration_s=5400)
    for _ in range(steps):
        eng.step()
    return eng


@pytest.mark.slow
def test_engine_downgrades_cascade_command_into_recommendation(config: AppConfig, tmp_path: Path) -> None:
    eng = run_engine(config, tmp_path, "cascade_heavy", 1800)  # энергии мало
    try:
        res = eng.guard.submit(high(evidence=two()), "monitor")
        assert res.status is CommandStatus.DOWNGRADED
        rec = next(r for r in eng.board.pending() if "риск каскада" in r.title)
        assert rec.needs_human and not rec.commands and "минимум" in rec.rationale
        assert eng.counters["downgrades"] == 1
    finally:
        eng.close()


def test_engine_safety_event_is_immediate(config: AppConfig, tmp_path: Path) -> None:
    eng = run_engine(config, tmp_path, "quiet", 200)
    try:
        eng.report_safety_event("smoke", "дым над паяльной станцией", who="human:оператор")
        eng.step()
        assert eng.counters["safety_events"] == 1
        crit = [a for a in eng.alerts.active(eng.adapter.now()) if a.kind == "safety_event"]
        assert crit and crit[0].level is AlertLevel.CRITICAL and "smoke" in crit[0].message
        resp = [e for e in eng.journal.entries(kinds=["response"]) if e.data["safety_event"] == "smoke"]
        assert resp and resp[0].data["level"] == "act_full" and "исключение безопасности" in resp[0].data["reason"]
        rec = next(r for r in eng.board.all() if r.agent == "safety")
        assert rec.priority == 1 and rec.commands and rec.commands[0].safety_event == "smoke"
        sent = [e for e in eng.journal.entries(kinds=["command"]) if e.status == "accepted"]
        assert sent and sent[-1].data["safety_event"] == "smoke"  # вытяжка включена без правила двух
    finally:
        eng.close()


@pytest.mark.slow
def test_engine_logs_response_levels_with_reason(config: AppConfig, tmp_path: Path) -> None:
    eng = run_engine(config, tmp_path, "basic", 1700)
    try:
        resp = eng.journal.entries(kinds=["response"])
        assert resp and all(e.data["reason"] and e.data["level"] == e.status for e in resp)
        first = next(e for e in resp if e.data["subject"] == SENSOR)
        assert first.data["level"] == "observe" and "не реален" in first.data["reason"]  # ложный CO2: только наблюдение
    finally:
        eng.close()
