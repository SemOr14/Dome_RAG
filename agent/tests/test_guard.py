"""Тесты предохранителя команд (в том числе критерии приёмки 5 и 6)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from kupol.adapter.base import AdapterError, AdapterTimeout, KupolAdapter
from kupol.adapter.scenario import ActuatorFailEvent, CommsLossEvent, Scenario
from kupol.adapter.simulator import SimulatorAdapter
from kupol.core.config import AppConfig, ArgRule
from kupol.core.guard import Guard
from kupol.core.journal import Journal
from kupol.core.models import AlertLevel, Command, CommandResult, CommandStatus, Telemetry
from kupol.core.state import State
from tests.conftest import T0, make_telemetry
from tests.fakes import FakeAdapter


@pytest.fixture
def env(config: AppConfig, tmp_path: Path):  # type: ignore[no-untyped-def]
    adapter = FakeAdapter()
    journal = Journal(tmp_path / "run", clock=adapter.now)
    state = State()
    guard = Guard(config.guard, adapter, journal, state)
    yield guard, adapter, journal, state
    journal.close()


def vent(mode: str = "low") -> Command:
    return Command(name="vent_enable", args={"mode": mode})


# --------------------------------------------------------------------------- запреты и белый список


@pytest.mark.parametrize(
    "cmd",
    [
        Command(name="disable_interlock"),
        Command(name="set_line", args={"line": "interlock_bypass", "enabled": True}),
        Command(name="vent_enable", args={"mode": "low", "note": "Dis-able_Safety"}),
        Command(name="Reset Emergency Stop"),
        Command(name="отключить блокировку"),
        Command(name="set_vent_mode", args={"mode": "OVERRIDE"}),
    ],
)
def test_forbidden_never_reaches_adapter(env, cmd: Command) -> None:  # type: ignore[no-untyped-def]
    guard, adapter, journal, _ = env
    res = guard.submit(cmd, "monitor")
    assert res.status is CommandStatus.REJECTED and "ЗАПРЕЩЕНО" in res.message
    assert adapter.sent == []
    alerts = guard.check_effects()
    assert any(a.kind == "forbidden_command" and a.level is AlertLevel.CRITICAL for a in alerts)
    assert any(e.status == "rejected" and e.kind == "command" for e in journal.entries())


def test_forbidden_applies_to_humans_too(env) -> None:  # type: ignore[no-untyped-def]
    guard, adapter, *_ = env
    res = guard.submit(Command(name="bypass_interlock"), "human:диагност")
    assert res.status is CommandStatus.REJECTED and adapter.sent == []


def test_forbidden_checked_before_whitelist(env) -> None:  # type: ignore[no-untyped-def]
    guard, *_ = env
    res = guard.submit(Command(name="interlock_off"), "monitor")
    assert "ЗАПРЕЩЕНО" in res.message  # а не «не входит в белый список»


def test_not_in_whitelist(env) -> None:  # type: ignore[no-untyped-def]
    guard, adapter, *_ = env
    res = guard.submit(Command(name="reboot_controller"), "monitor")
    assert res.status is CommandStatus.REJECTED and "белый список" in res.message
    assert adapter.sent == []


@pytest.mark.parametrize(
    "cmd",
    [
        Command(name="vent_enable", args={"mode": "high"}),  # high агенту запрещён
        Command(name="vent_enable", args={"mode": "off"}),
        Command(name="vent_enable", args={}),
        Command(name="vent_enable", args={"mode": "low", "extra": 1}),
        Command(name="set_humidifier_mode", args={"mode": 5}),
        Command(name="set_line", args={"line": "cnc", "enabled": True}),  # станок не линия Штаба
        Command(name="set_line", args={"line": "aux", "enabled": "yes"}),
        Command(name="set_line", args={"line": "aux", "enabled": 1}),
    ],
)
def test_out_of_range_args_never_reach_adapter(env, cmd: Command) -> None:  # type: ignore[no-untyped-def]
    guard, adapter, *_ = env
    res = guard.submit(cmd, "energy_planner")
    assert res.status is CommandStatus.REJECTED
    assert adapter.sent == []
    assert guard.pending() == []


def test_number_arg_rules() -> None:
    rule = ArgRule(type="number", min=0, max=10)
    assert Guard._check_value("x", 5, rule) is None
    assert Guard._check_value("x", 11, rule) is not None
    assert Guard._check_value("x", -1, rule) is not None
    assert Guard._check_value("x", True, rule) is not None
    assert Guard._check_value("x", "5", rule) is not None


# --------------------------------------------------------------------------- классы команд


def test_auto_command_is_sent(env) -> None:  # type: ignore[no-untyped-def]
    guard, adapter, journal, _ = env
    res = guard.submit(vent("low"), "monitor")
    assert res.status is CommandStatus.ACCEPTED
    assert [c.name for c in adapter.sent] == ["vent_enable"]
    assert guard.awaiting_verification() == 1
    e = [x for x in journal.entries(kinds=["command"]) if x.status == "accepted"][0]
    assert e.actor == "monitor" and e.executor == "vent"


def test_needs_human_not_executed_without_confirmation(env) -> None:  # type: ignore[no-untyped-def]
    guard, adapter, *_ = env
    cmd = Command(name="set_power_mode", args={"mode": "eco"})
    res = guard.submit(cmd, "energy_planner")
    assert res.status is CommandStatus.PENDING
    assert adapter.sent == []
    assert [p.command.id for p in guard.pending()] == [cmd.id]
    guard.check_effects()
    assert adapter.sent == []  # сколько бы ни ждали, без подтверждения не уходит


def test_needs_human_approve_sends(env) -> None:  # type: ignore[no-untyped-def]
    guard, adapter, journal, _ = env
    cmd = Command(name="set_power_mode", args={"mode": "eco"})
    guard.submit(cmd, "energy_planner")
    res = guard.approve(cmd.id, "диагност")
    assert res.status is CommandStatus.ACCEPTED
    assert adapter.sent == [cmd] and guard.pending() == []
    statuses = [e.status for e in journal.entries(kinds=["command"])]
    assert statuses == ["pending", "approved", "accepted"]


def test_needs_human_reject(env) -> None:  # type: ignore[no-untyped-def]
    guard, adapter, journal, _ = env
    cmd = Command(name="set_power_mode", args={"mode": "eco"})
    guard.submit(cmd, "energy_planner")
    res = guard.reject(cmd.id, "диагност", "не сейчас")
    assert res.status is CommandStatus.DENIED and "не сейчас" in res.message
    assert adapter.sent == [] and guard.pending() == []
    assert guard.approve(cmd.id).status is CommandStatus.REJECTED  # повторно уже нельзя
    assert journal.entries(kinds=["command"])[-1].status == "denied"


def test_unknown_ids(env) -> None:  # type: ignore[no-untyped-def]
    guard, *_ = env
    assert guard.approve("nope").status is CommandStatus.REJECTED
    assert guard.reject("nope").status is CommandStatus.REJECTED


def test_duplicate_pending_not_queued_twice(env) -> None:  # type: ignore[no-untyped-def]
    guard, *_ = env
    guard.submit(Command(name="set_power_mode", args={"mode": "eco"}), "energy_planner")
    res = guard.submit(Command(name="set_power_mode", args={"mode": "eco"}), "energy_planner")
    assert res.status is CommandStatus.REJECTED and len(guard.pending()) == 1


def test_human_request_needs_no_queue(env) -> None:  # type: ignore[no-untyped-def]
    guard, adapter, *_ = env
    res = guard.submit(Command(name="set_vent_mode", args={"mode": "high"}), "human:оператор")
    assert res.status is CommandStatus.ACCEPTED and len(adapter.sent) == 1


def test_pending_expires(env) -> None:  # type: ignore[no-untyped-def]
    guard, adapter, journal, _ = env
    guard.submit(Command(name="set_power_mode", args={"mode": "eco"}), "energy_planner")
    adapter.t += timedelta(seconds=301)
    guard.check_effects()
    assert guard.pending() == [] and adapter.sent == []
    assert journal.entries(kinds=["command"])[-1].status == "expired"


def test_revalidated_on_approve(env, config: AppConfig) -> None:  # type: ignore[no-untyped-def]
    guard, adapter, *_ = env
    cmd = Command(name="set_power_mode", args={"mode": "eco"})
    guard.submit(cmd, "energy_planner")
    guard._cfg.commands["set_power_mode"].args["mode"].values.remove("eco")  # правила изменились
    try:
        res = guard.approve(cmd.id)
    finally:
        guard._cfg.commands["set_power_mode"].args["mode"].values.append("eco")
    assert res.status is CommandStatus.REJECTED and adapter.sent == []


# --------------------------------------------------------------------------- частота


def test_rate_limit(env) -> None:  # type: ignore[no-untyped-def]
    guard, adapter, *_ = env
    for _ in range(3):
        assert guard.submit(vent("low"), "monitor").status is CommandStatus.ACCEPTED
    res = guard.submit(vent("low"), "monitor")
    assert res.status is CommandStatus.REJECTED and "частота" in res.message
    assert len(adapter.sent) == 3
    adapter.t += timedelta(seconds=61)
    assert guard.submit(vent("low"), "monitor").status is CommandStatus.ACCEPTED


def test_rate_limit_is_per_executor(env) -> None:  # type: ignore[no-untyped-def]
    guard, *_ = env
    for _ in range(3):
        guard.submit(vent("low"), "monitor")
    # вытяжка исчерпана, увлажнитель — другой исполнитель
    assert guard.submit(Command(name="set_humidifier_mode", args={"mode": "low"}), "monitor").status is CommandStatus.ACCEPTED
    # set_vent_mode — тот же исполнитель 'vent'
    assert guard.submit(Command(name="set_vent_mode", args={"mode": "low"}), "human:x").status is CommandStatus.REJECTED


def test_rate_limit_per_line(env) -> None:  # type: ignore[no-untyped-def]
    guard, *_ = env
    for _ in range(3):
        guard.submit(Command(name="set_line", args={"line": "aux", "enabled": False}), "human:x")
    assert guard.submit(Command(name="set_line", args={"line": "aux", "enabled": True}), "human:x").status is CommandStatus.REJECTED
    assert guard.submit(Command(name="set_line", args={"line": "lighting", "enabled": False}), "human:x").status is CommandStatus.ACCEPTED


# --------------------------------------------------------------------------- ошибки адаптера


@pytest.mark.parametrize("exc", [AdapterTimeout("t"), AdapterError("e")])
def test_adapter_errors_become_failed(env, exc: Exception) -> None:  # type: ignore[no-untyped-def]
    guard, adapter, *_ = env
    adapter.raise_exc = exc
    res = guard.submit(vent(), "monitor")
    assert res.status is CommandStatus.FAILED and guard.awaiting_verification() == 0


def test_adapter_non_accepted_is_failed(env) -> None:  # type: ignore[no-untyped-def]
    guard, adapter, *_ = env
    adapter.status = CommandStatus.FAILED
    assert guard.submit(vent(), "monitor").status is CommandStatus.FAILED


# --------------------------------------------------------------------------- проверка эффекта


def test_effect_verified(env) -> None:  # type: ignore[no-untyped-def]
    guard, adapter, journal, state = env
    state.update(make_telemetry(0))
    guard.submit(vent("low"), "monitor")
    adapter.t += timedelta(seconds=4)
    state.update(make_telemetry(4, vent_mode="low"))
    assert guard.check_effects() == []
    assert guard.awaiting_verification() == 0
    assert journal.entries(kinds=["command"])[-1].status == "verified"


def test_no_effect_creates_alert(env) -> None:  # type: ignore[no-untyped-def]
    guard, adapter, journal, state = env
    state.update(make_telemetry(0))
    guard.submit(vent("low"), "monitor")
    adapter.t += timedelta(seconds=5)
    state.update(make_telemetry(5))
    assert guard.check_effects() == []  # срок ещё не вышел
    adapter.t += timedelta(seconds=6)
    state.update(make_telemetry(11))
    alerts = guard.check_effects()
    assert [a.kind for a in alerts] == ["actuator_no_effect"]
    assert alerts[0].field == "vent" and "эффекта нет" in alerts[0].message
    assert journal.entries(kinds=["command"])[-1].status == "no_effect"
    assert guard.check_effects() == []  # тревога выдаётся один раз


def test_verification_pauses_without_comms(env) -> None:  # type: ignore[no-untyped-def]
    guard, adapter, _, state = env
    state.update(make_telemetry(0))
    guard.submit(vent("low"), "monitor")
    adapter.t += timedelta(seconds=30)
    state.update(make_telemetry(30, comms_ok=False))
    assert guard.check_effects() == []  # нет связи: вывод «нет эффекта» невозможен
    assert guard.awaiting_verification() == 1


def test_set_line_verification_path(env) -> None:  # type: ignore[no-untyped-def]
    guard, adapter, _, state = env
    state.update(make_telemetry(0))
    guard.submit(Command(name="set_line", args={"line": "lighting", "enabled": False}), "human:x")
    adapter.t += timedelta(seconds=3)
    from kupol.core.models import LineState

    state.update(make_telemetry(3, lines={"lighting": LineState(on=False, power_w=0.0)}))
    guard.check_effects()
    assert guard.awaiting_verification() == 0


# --------------------------------------------------------------------------- с настоящим симулятором


def sim_env(config: AppConfig, tmp_path: Path, *events: object):  # type: ignore[no-untyped-def]
    sim = SimulatorAdapter(config, Scenario(name="t", initial={"noise": False}, events=list(events)), seed=1)  # type: ignore[arg-type]
    journal = Journal(tmp_path / "r", clock=sim.now)
    state = State()
    state.update(sim.get_telemetry())
    return sim, journal, state, Guard(config.guard, sim, journal, state)


def run(sim: SimulatorAdapter, state: State, guard: Guard, seconds: int) -> list:  # type: ignore[type-arg]
    alerts = []
    for _ in range(seconds):
        sim.advance(1)
        state.update(sim.get_telemetry())
        alerts += guard.check_effects()
    return alerts


def test_simulator_command_verified(config: AppConfig, tmp_path: Path) -> None:
    sim, journal, state, guard = sim_env(config, tmp_path)
    guard.submit(vent("low"), "monitor")
    assert run(sim, state, guard, 8) == []
    assert guard.awaiting_verification() == 0
    assert journal.entries(kinds=["command"])[-1].status == "verified"
    journal.close()


def test_simulator_actuator_failure_detected(config: AppConfig, tmp_path: Path) -> None:
    sim, journal, state, guard = sim_env(config, tmp_path, ActuatorFailEvent(at=0, target="vent"))
    guard.submit(vent("low"), "monitor")
    alerts = run(sim, state, guard, 15)
    assert [a.kind for a in alerts] == ["actuator_no_effect"]
    journal.close()


def test_simulator_comms_loss_does_not_fake_no_effect(config: AppConfig, tmp_path: Path) -> None:
    sim, journal, state, guard = sim_env(
        config,
        tmp_path,
        ActuatorFailEvent(at=0, target="vent"),
        CommsLossEvent(at=3, duration_s=40),
    )
    guard.submit(vent("low"), "monitor")
    assert run(sim, state, guard, 40) == []  # пока связи нет, «нет эффекта» не объявляется
    assert guard.awaiting_verification() == 1
    alerts = run(sim, state, guard, 30)  # связь вернулась: теперь отказ исполнителя виден
    assert [a.kind for a in alerts] == ["actuator_no_effect"]
    journal.close()
