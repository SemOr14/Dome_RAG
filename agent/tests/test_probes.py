"""Тесты безопасных проб: допустимость, выбор, запуск через Guard, оценка результата."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from kupol.core.baseline import Baseline
from kupol.core.config import AppConfig, ProbeSpec
from kupol.core.energy import EnergyPlanner, energy_status
from kupol.core.guard import Guard
from kupol.core.journal import Journal
from kupol.core.models import Command, CommandStatus, LineState, Probe, ProbeRisk
from kupol.core.probes import ProbeManager, operator_message, to_probe
from kupol.core.state import State
from kupol.engine import Engine
from tests.conftest import ROOT, T0, make_telemetry
from tests.fakes import FakeAdapter
from kupol.adapter.scenario import load_scenario
from kupol.adapter.simulator import SimulatorAdapter

SENSOR = "co2_ppm.co2_b"


def at(s: float):  # type: ignore[no-untyped-def]
    return T0 + timedelta(seconds=s)


@pytest.fixture
def env(config: AppConfig, tmp_path: Path):  # type: ignore[no-untyped-def]
    adapter = FakeAdapter()
    journal = Journal(tmp_path / "run", clock=adapter.now)
    state = State()
    state.update(make_telemetry(0))
    guard = Guard(config.guard, adapter, journal, state, response=config.response)
    mgr = ProbeManager(config, guard, journal, EnergyPlanner(config))
    yield mgr, guard, adapter, journal, state
    journal.close()


def energy(config: AppConfig, state: State, soc: float = 80.0, pv: float = 1500.0, load: float = 700.0):  # type: ignore[no-untyped-def]
    st = State()
    for i in range(90):
        st.update(make_telemetry(i, battery_soc_pct=soc, pv_power_w=pv, load_total_w=load))
    return st.latest(), energy_status(st, config)


def probe_by_id(mgr: ProbeManager, pid: str) -> Probe:
    return next(p for p in mgr.catalog if p.id == pid)


# --------------------------------------------------------------------------- каталог и допустимость


def test_catalog_loaded_from_config(env) -> None:  # type: ignore[no-untyped-def]
    mgr, *_ = env
    assert {p.id for p in mgr.catalog} == {"recheck_neighbor", "vent_min_30s", "operator_check_indicator", "video_check"}
    vent = probe_by_id(mgr, "vent_min_30s")
    assert vent.command is not None and vent.command.name == "vent_enable" and vent.command.args == {"mode": "low"}
    assert vent.risk is ProbeRisk.LOW and vent.reversible and vent.cost_wh == 0.5 and vent.duration_s == 30


@pytest.mark.parametrize("risk", ["medium", "high"])
def test_risky_probe_cannot_even_be_built(risk: str) -> None:
    spec = ProbeSpec(id="x", description="d", risk=risk, evaluator="neighbors_agree", expected_if_real="a", expected_if_false="b")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="допустимы только none/low"):
        to_probe(spec)


def test_probe_spec_requires_some_action() -> None:
    with pytest.raises(ValueError, match="command, human_action или evaluator"):
        ProbeSpec(id="x", description="d", expected_if_real="a", expected_if_false="b")


def test_all_conditions_for_admissible_probe(env, config: AppConfig) -> None:  # type: ignore[no-untyped-def]
    """8: проба выбирается среди допустимых; недопустимая не выбирается."""
    mgr, *_ = env
    tel, status = energy(config, None)
    ok = mgr.candidates(SENSOR, tel, status, T0)
    assert {p.id for p in ok} == {"recheck_neighbor", "vent_min_30s", "operator_check_indicator"}  # video_check к CO2 неприменима
    chosen = mgr.select(ok, SENSOR)
    assert chosen is not None and chosen.human_action is None  # автоматическая проба выбирается раньше вопроса человеку
    assert chosen.id == "vent_min_30s"  # из автоматических — самая информативная


def test_irreversible_probe_is_rejected(env, config: AppConfig) -> None:  # type: ignore[no-untyped-def]
    mgr, *_ = env
    tel, status = energy(config, None)
    bad = probe_by_id(mgr, "recheck_neighbor").model_copy(update={"reversible": False})
    assert mgr.reject_reason(bad, SENSOR, tel, status, T0) == "проба необратима"
    mgr.catalog = [bad]
    assert mgr.candidates(SENSOR, tel, status, T0) == []


def test_over_budget_probe_is_rejected(env, config: AppConfig) -> None:  # type: ignore[no-untyped-def]
    mgr, *_ = env
    tel, status = energy(config, None)
    pricey = probe_by_id(mgr, "recheck_neighbor").model_copy(update={"cost_wh": config.probes.energy_budget_wh + 1})
    assert "выше бюджета" in (mgr.reject_reason(pricey, SENSOR, tel, status, T0) or "")


def test_probe_with_cascade_risk_is_rejected(env, config: AppConfig) -> None:  # type: ignore[no-untyped-def]
    mgr, *_ = env
    tel, status = energy(config, None, soc=22.0, pv=300.0, load=600.0)
    tel = tel.model_copy(update={"lines": {"vent": LineState(on=True, power_w=0.0)}})
    reason = mgr.reject_reason(probe_by_id(mgr, "vent_min_30s"), SENSOR, tel, status, T0)
    assert reason is not None and "риск каскада" in reason
    assert "vent_min_30s" not in {p.id for p in mgr.candidates(SENSOR, tel, status, T0)}


def test_vent_probe_not_offered_when_vent_already_on(env, config: AppConfig) -> None:  # type: ignore[no-untyped-def]
    mgr, *_ = env
    tel, status = energy(config, None)
    tel = tel.model_copy(update={"vent_mode": "low"})
    reason = mgr.reject_reason(probe_by_id(mgr, "vent_min_30s"), SENSOR, tel, status, T0)
    assert reason is not None and "никогда не выключаем" in reason


def test_not_applicable_probe_for_other_fields(env, config: AppConfig) -> None:  # type: ignore[no-untyped-def]
    mgr, *_ = env
    tel, status = energy(config, None)
    assert mgr.reject_reason(probe_by_id(mgr, "recheck_neighbor"), "pv_power_w", tel, status, T0) == "проба неприменима к этому полю"
    assert {p.id for p in mgr.candidates("lines.printer.power_w", tel, status, T0)} == {"operator_check_indicator", "video_check"}


def test_rate_limits(env, config: AppConfig) -> None:  # type: ignore[no-untyped-def]
    mgr, _, _, _, state = env
    tel, status = energy(config, state)
    pr = probe_by_id(mgr, "recheck_neighbor")
    mgr.start(pr, SENSOR, tel, state, at(0))
    assert mgr.reject_reason(probe_by_id(mgr, "operator_check_indicator"), SENSOR, tel, status, at(10)) == "слишком рано после предыдущей пробы"
    for k in (1, 2):
        mgr.runs[0].state = "done"
        mgr.start(pr, SENSOR, tel, state, at(100 * k))
        mgr.runs[-1].state = "done"
    assert mgr.reject_reason(pr, SENSOR, tel, status, at(300)) == "исчерпан лимит проб за 10 минут"
    assert mgr.reject_reason(pr, SENSOR, tel, status, at(1000)) is None  # окно 10 минут прошло
    running = mgr.start(pr, SENSOR, tel, state, at(1000))
    assert running.state == "running" and mgr.reject_reason(pr, SENSOR, tel, status, at(1100)) == "эта проба уже выполняется"


def test_select_prefers_most_informative_and_escalates_to_human(env, config: AppConfig) -> None:  # type: ignore[no-untyped-def]
    mgr, *_ = env
    tel, status = energy(config, None)
    cands = mgr.candidates(SENSOR, tel, status, T0)
    auto_only = [p for p in cands if p.human_action is None]
    assert mgr.select(auto_only, SENSOR).id == "vent_min_30s"  # type: ignore[union-attr]  # информативнее соседей, но дороже: счёт выше
    assert mgr.select(cands, SENSOR, ran_auto_before=True).id == "operator_check_indicator"  # type: ignore[union-attr]
    assert mgr.select([], SENSOR) is None
    human_only = [p for p in cands if p.human_action is not None]
    assert mgr.select(human_only, SENSOR).id == "operator_check_indicator"  # type: ignore[union-attr]


# --------------------------------------------------------------------------- запуск через Guard


def test_command_probe_goes_through_guard_only(env, config: AppConfig) -> None:  # type: ignore[no-untyped-def]
    mgr, guard, adapter, journal, state = env
    tel, _ = energy(config, state)
    run = mgr.start(probe_by_id(mgr, "vent_min_30s"), SENSOR, tel, state, T0)
    assert run.state == "running" and [c.name for c in adapter.sent] == ["vent_enable"]
    cmds = journal.entries(kinds=["command"])
    assert cmds[-1].actor == "probes" and cmds[-1].status == "accepted"
    assert "проба vent_min_30s" in adapter.sent[0].reason
    assert journal.entries(kinds=["probe"])[-1].status == "running"


def test_command_probe_blocked_when_guard_refuses(env, config: AppConfig) -> None:  # type: ignore[no-untyped-def]
    mgr, guard, adapter, journal, state = env
    tel, _ = energy(config, state)
    for _ in range(3):  # исчерпан лимит частоты вытяжки
        guard.submit(Command(name="vent_enable", args={"mode": "low"}), "monitor")
    sent_before = len(adapter.sent)
    run = mgr.start(probe_by_id(mgr, "vent_min_30s"), SENSOR, tel, state, T0)
    assert run.state == "blocked" and "частота" in run.message and len(adapter.sent) == sent_before
    assert mgr.running() == []


def test_human_probe_uses_command_language_and_logs(env, config: AppConfig) -> None:  # type: ignore[no-untyped-def]
    mgr, _, adapter, journal, state = env
    tel, _ = energy(config, state)
    pr = probe_by_id(mgr, "operator_check_indicator")
    run = mgr.start(pr, SENSOR, tel, state, T0)
    assert adapter.sent == []  # просьба к человеку, не команда оборудованию
    assert run.message.startswith("ШТАБ→ОПЕРАТОР [проба operator_check_indicator]") and "Если реально:" in run.message and "Если ложно:" in run.message
    assert operator_message(pr, 60.0) in run.message or "в течение" in run.message
    e = journal.entries(kinds=["probe"])[-1]
    assert e.executor == "Оператор" and "ожидание при реальном" in e.cross_check and e.data["probe"] == "operator_check_indicator"


# --------------------------------------------------------------------------- оценка


def test_human_answer_becomes_strong_evidence(env, config: AppConfig) -> None:  # type: ignore[no-untyped-def]
    mgr, _, _, journal, state = env
    tel, _ = energy(config, state)
    run = mgr.start(probe_by_id(mgr, "operator_check_indicator"), SENSOR, tel, state, T0)
    assert mgr.tick(state, at(5)) == []  # ответа ещё нет
    assert not mgr.answer("нет-такого", "real", "x") and not mgr.answer(run.id, "maybe", "x")
    assert mgr.answer(run.id, "false", "оператор")
    ((done, ev),) = mgr.tick(state, at(6))
    assert done.state == "done" and ev is not None
    assert ev.source == "operator" and ev.supports == "false" and ev.weight == config.verdicts.weights["operator"] and ev.rule == "probe"
    assert "оператор подтверждает: ложно" in ev.detail
    assert journal.entries(kinds=["probe"])[-1].status == "done"


def test_unanswered_human_probe_expires(env, config: AppConfig) -> None:  # type: ignore[no-untyped-def]
    mgr, _, _, _, state = env
    tel, _ = energy(config, state)
    run = mgr.start(probe_by_id(mgr, "operator_check_indicator"), SENSOR, tel, state, T0)
    ((done, ev),) = mgr.tick(state, run.deadline + timedelta(seconds=1))
    assert done.state == "expired" and ev is None and mgr.running() == []


def test_neighbors_probe_real_vs_false(env, config: AppConfig) -> None:  # type: ignore[no-untyped-def]
    mgr, _, _, _, state = env
    base = Baseline(config)
    mgr._baseline = base
    tel, _ = energy(config, state)
    run = mgr.start(probe_by_id(mgr, "recheck_neighbor"), SENSOR, tel, state, T0)
    agree = make_telemetry(10, co2_ppm={"co2_a": 900.0, "co2_b": 905.0, "co2_c": 897.0})
    state.update(agree)
    ((_, ev),) = mgr.tick(state, at(10))
    assert ev is not None and ev.supports == "real" and ev.source == "probe:recheck_neighbor"
    run2 = mgr.start(probe_by_id(mgr, "recheck_neighbor"), SENSOR, tel, state, at(500))
    state.update(make_telemetry(510, co2_ppm={"co2_a": 900.0, "co2_b": 5000.0, "co2_c": 897.0}))
    ((_, ev2),) = mgr.tick(state, at(510))
    assert run2.state == "done" and ev2 is not None and ev2.supports == "false" and ev2.kind_hint == "false_signal"
    _ = run


def test_neighbors_probe_without_neighbors_is_neutral(env, config: AppConfig) -> None:  # type: ignore[no-untyped-def]
    mgr, _, _, _, state = env
    tel, _ = energy(config, state)
    mgr.start(probe_by_id(mgr, "recheck_neighbor"), SENSOR, tel, state, T0)
    state.update(make_telemetry(10, co2_ppm={"co2_b": 900.0}))
    ((run, ev),) = mgr.tick(state, at(10))
    assert ev is None and run.result is not None and run.result.supports == "neutral"


def test_vent_probe_evaluation(env, config: AppConfig) -> None:  # type: ignore[no-untyped-def]
    mgr, _, _, _, state = env
    base_tel = make_telemetry(0, co2_ppm={"co2_a": 1500.0, "co2_b": 1510.0, "co2_c": 1490.0})
    state.update(base_tel)
    pr = probe_by_id(mgr, "vent_min_30s")

    clock = {"t": 0}

    def run_case(after_b: float, vent: str) -> tuple[object, object]:
        mgr.runs.clear()
        mgr._started.clear()
        t0 = clock["t"]
        mgr.start(pr, SENSOR, base_tel, state, at(t0))
        clock["t"] = t0 + 31
        state.update(make_telemetry(clock["t"], co2_ppm={"co2_a": 1480.0, "co2_b": after_b, "co2_c": 1470.0}, vent_mode=vent))
        ((run, ev),) = mgr.tick(state, at(clock["t"]))
        return run, ev

    _, real = run_case(1470.0, "low")  # CO2 падает при включённой вытяжке — поведение реального роста
    assert real is not None and real.supports == "real"  # type: ignore[attr-defined]
    _, false = run_case(1510.0, "low")  # подозрительный датчик не реагирует
    assert false is not None and false.supports == "false" and false.kind_hint == "false_signal"  # type: ignore[attr-defined]
    run, none = run_case(1470.0, "off")  # вытяжка не включилась: проба недействительна
    assert none is None and run.result.supports == "neutral" and "недействительна" in run.result.detail  # type: ignore[attr-defined]


def test_tick_waits_for_duration_and_comms(env, config: AppConfig) -> None:  # type: ignore[no-untyped-def]
    mgr, _, _, _, state = env
    tel, _ = energy(config, state)
    mgr.start(probe_by_id(mgr, "recheck_neighbor"), SENSOR, tel, state, T0)
    assert mgr.tick(state, at(1)) == []  # срок пробы ещё не вышел
    state.update(make_telemetry(10, comms_ok=False))
    assert mgr.tick(state, at(10)) == []  # связи нет: оценивать нечем
    assert mgr.running()


def test_has_recent_and_ran_auto(env, config: AppConfig) -> None:  # type: ignore[no-untyped-def]
    mgr, _, _, _, state = env
    tel, _ = energy(config, state)
    assert not mgr.has_recent(SENSOR, T0) and not mgr.ran_auto(SENSOR)
    mgr.start(probe_by_id(mgr, "recheck_neighbor"), SENSOR, tel, state, T0)
    assert mgr.has_recent(SENSOR, at(100)) and not mgr.has_recent(SENSOR, at(400)) and mgr.ran_auto(SENSOR)
    assert CommandStatus.ACCEPTED is not None


# --------------------------------------------------------------------------- движок: сквозной прогон


@pytest.mark.slow
def test_engine_runs_probes_and_feeds_verdicts(config: AppConfig, tmp_path: Path) -> None:
    sim = SimulatorAdapter(config, load_scenario(ROOT / "scenarios" / "hidden.yaml"), seed=42)
    eng = Engine(config, sim, tmp_path / "run", sim_operator=True, auto_approve=True, duration_s=3600)
    eng.run(speed=0)
    probes = eng.journal.entries(kinds=["probe"])
    assert probes and eng.counters["probes"] >= 1
    assert {e.data["probe"] for e in probes} <= {p.id for p in eng.probes.catalog}  # type: ignore[union-attr]
    assert len([e for e in probes if e.status == "running"]) <= config.probes.max_per_10min * 7  # лимит частоты соблюдается
    operator_runs = [e for e in probes if e.data["probe"] in ("operator_check_indicator", "video_check") and e.status == "done"]
    if operator_runs:
        assert any(ev.rule == "probe" for v in eng.book.active() + eng.book.archive for ev in v.evidence)
    assert eng.counters["step_errors"] == 0
