"""Интеграционные прогоны сценариев: критерии приёмки 2–4, 7, 9 и метрики качества.

Каждый сценарий прогоняется один раз на модуль (быстро: виртуальное время, mock-LLM),
затем проверяются свойства журнала. Это и есть «метрики качества» Штаба:
скорость обнаружения воздействий, отсутствие ложных срабатываний, корректность реакций.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kupol.adapter.scenario import load_scenario
from kupol.adapter.simulator import SimulatorAdapter
from kupol.agents.llm import LLMClient
from kupol.core.config import AppConfig, LLMConfig, load_config
from kupol.core.models import JournalEntry
from kupol.engine import Engine
from kupol.report import RunData, evaluate_detection, find_false_alarms, load_run
from tests.conftest import ROOT

pytestmark = pytest.mark.slow  # полные игры: в обычный прогон не входят


def play(config: AppConfig, name: str, out: Path, **kw: Any) -> tuple[Engine, SimulatorAdapter, RunData]:
    sim = SimulatorAdapter(config, load_scenario(ROOT / "scenarios" / f"{name}.yaml"), seed=42)
    llm = kw.pop("llm", None)
    eng = Engine(config, sim, out / name, llm=llm, auto_approve=True, meta={"scenario": name}, **kw)
    eng.run(speed=0)
    return eng, sim, load_run(out / name)


@pytest.fixture(scope="module")
def cfg() -> AppConfig:
    return load_config(ROOT / "config.yaml")


@pytest.fixture(scope="module")
def basic(cfg: AppConfig, tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    return play(cfg, "basic", tmp_path_factory.mktemp("basic"))


@pytest.fixture(scope="module")
def quiet(cfg: AppConfig, tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    return play(cfg, "quiet", tmp_path_factory.mktemp("quiet"))


@pytest.fixture(scope="module")
def heavy(cfg: AppConfig, tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    return play(cfg, "cascade_heavy", tmp_path_factory.mktemp("heavy"))


def alerts(data: RunData, **match: str) -> list[tuple[float, dict[str, Any]]]:
    out = []
    for e in data.entries:
        if e.kind == "alert":
            a = e.data["alert"]
            if all(a.get(k) == v for k, v in match.items()):
                out.append((data.rel(e.ts), a))
    return out


def commands(data: RunData, status: str | None = None) -> list[JournalEntry]:
    return [e for e in data.entries if e.kind == "command" and (status is None or e.status == status)]


# --------------------------------------------------------------------------- критерий 2: доходит до конца


def test_basic_runs_to_the_end_and_logs(basic) -> None:  # type: ignore[no-untyped-def]
    eng, _, data = basic
    assert data.summary["elapsed_s"] == 5400 and data.summary["counters"]["ticks"] == 5400
    assert data.summary["counters"]["step_errors"] == 0
    assert data.entries and data.entries[-1].event == "прогон завершён"


# --------------------------------------------------------------------------- критерий 3: сценарий basic


def test_false_signal_recognised_and_vent_not_driven_to_full(basic) -> None:  # type: ignore[no-untyped-def]
    _, _, data = basic
    verdicts = [(t, v) for t, v in [(data.rel(e.ts), e.data["verdict"]) for e in data.entries if e.kind == "verdict"]]
    false = [(t, v) for t, v in verdicts if v["kind"] == "false_signal" and "co2_ppm.co2_b" in v["fields"]]
    assert false and 600 <= false[0][0] <= 605, "ложный сигнал распознан сразу"
    # За окно ложного сигнала (600–700 с) вытяжку никто не включал ни в каком режиме
    in_window = [e for e in commands(data) if 590 <= data.rel(e.ts) <= 700]
    assert in_window == []
    # и вообще режим high не уходил в адаптер без человека
    assert not [e for e in commands(data, "accepted") if "'high'" in e.action and e.actor == "monitor"]
    assert any("недостоверно" in e.event for e in data.entries if e.kind == "recommendation")


def test_hidden_drift_noticed_before_critical(basic) -> None:  # type: ignore[no-untyped-def]
    _, _, data = basic
    trend = alerts(data, field="battery_temp_c", kind="trend")
    crit = alerts(data, field="battery_temp_c", kind="threshold", level="critical")
    assert trend and crit
    assert trend[0][0] < crit[0][0] - 1200, "тренд замечен минимум за 20 минут до критического порога"
    assert trend[0][0] - 1000 <= 180, "и не позже чем через 3 минуты после начала дрейфа"


def test_actuator_failure_is_recorded(basic) -> None:  # type: ignore[no-untyped-def]
    _, _, data = basic
    no_effect = commands(data, "no_effect")
    assert no_effect and no_effect[0].actor == "guard"
    assert alerts(data, kind="actuator_no_effect")
    # вместо спама командами — эскалация человеку
    esc = [e for e in data.entries if e.kind == "recommendation" and "прекратить пайку" in e.event]
    assert esc
    sent_to_vent = [e for e in commands(data, "accepted") if "vent_enable" in e.action and 3300 <= data.rel(e.ts) <= 4200]
    assert len(sent_to_vent) <= 2


def test_comms_loss_handled(basic) -> None:  # type: ignore[no-untyped-def]
    _, _, data = basic
    loss = alerts(data, kind="comms_loss")
    assert any(a["level"] == "critical" for _, a in loss)
    assert 3000 <= loss[0][0] <= 3003
    # во время потери связи команд не подаётся
    assert not [e for e in commands(data) if 3000 <= data.rel(e.ts) <= 3026 and e.actor != "guard"]
    assert any("Связь потеряна" in e.event for e in data.entries if e.kind == "recommendation")
    # данные после восстановления снова обрабатываются
    assert alerts(data, kind="comms_loss")[-1][0] < 3030


def test_every_injected_event_detected_quickly(basic) -> None:  # type: ignore[no-untyped-def]
    _, _, data = basic
    rows = evaluate_detection(data)
    assert rows and all(r["detected"] for r in rows), [r for r in rows if not r["detected"]]
    fast = {r["kind"]: r["delay_s"] for r in rows if r["kind"] in ("false_signal", "comms_loss", "device_fault")}
    assert all(d <= 15 for d in fast.values()), fast


def test_real_co2_growth_is_not_mistaken_for_false(basic) -> None:  # type: ignore[no-untyped-def]
    _, _, data = basic
    real = [
        e
        for e in data.entries
        if e.kind == "verdict"
        and e.data["verdict"]["kind"] == "real"
        and {ev["rule"] for ev in e.data["verdict"]["evidence"]} & {"co2_consensus_high", "co2_consensus_rise"}
    ]
    assert real
    late_vent = [e for e in commands(data, "accepted") if "vent_enable" in e.action and data.rel(e.ts) > 4900]
    assert late_vent, "при реальном росте CO2 вытяжка (low) включается"
    assert not [e for e in commands(data) if "'high'" in e.action and e.status == "accepted" and e.actor == "monitor"]


def test_no_false_alarms_in_basic(basic) -> None:  # type: ignore[no-untyped-def]
    _, _, data = basic
    assert find_false_alarms(data) == []


# --------------------------------------------------------------------------- критерий 4: quiet


def test_quiet_has_no_critical_and_no_commands(quiet) -> None:  # type: ignore[no-untyped-def]
    _, _, data = quiet
    assert alerts(data, level="critical") == []
    assert alerts(data, level="warning") == []
    assert commands(data) == []
    assert data.summary["counters"]["step_errors"] == 0


# --------------------------------------------------------------------------- cascade_heavy


def test_cascade_heavy_energy_alerts_and_planner(heavy) -> None:  # type: ignore[no-untyped-def]
    _, _, data = heavy
    warn = alerts(data, kind="energy_t_left", level="warning")
    crit = alerts(data, kind="energy_t_left", level="critical")
    assert warn and crit and warn[0][0] < crit[0][0]
    planner = [e for e in data.entries if e.kind == "recommendation" and e.data.get("agent") == "energy_planner"]
    assert any("мин до минимального заряда" in e.event for e in planner)
    # советы по экономии проходят через предохранитель; неприрываемые этапы не трогаем
    sent = commands(data, "accepted")
    assert sent and all(e.actor in ("monitor", "human:sim-auto-approve", "energy_planner") for e in sent)
    assert not [e for e in data.entries if e.kind == "recommendation" and "Отложить" in e.action and "Фрезеровка" in e.action]


# --------------------------------------------------------------------------- критерий 9: проверяемость


def test_every_command_and_llm_call_is_reconstructable(basic) -> None:  # type: ignore[no-untyped-def]
    _, sim, data = basic
    cmds = commands(data)
    assert cmds
    for e in cmds:
        assert e.actor and e.action and e.status and e.data["command_id"]
    accepted_ids = {e.data["command_id"] for e in commands(data, "accepted")}
    reached_adapter = {ev["cmd_id"] for ev in sim.event_log if ev["kind"] == "command_accepted"}
    assert reached_adapter == accepted_ids, "в адаптер ушли ровно те команды, что прошли предохранитель"
    calls = [e for e in data.entries if e.kind == "llm_call"]
    assert calls and len(calls) == data.summary["counters"]["agent_runs"]
    for c in calls:
        d = c.data
        assert d["prompt_hash"] and d["inputs"] and d["raw_output"] and d["parsed_decision"] and d["used_fallback"] is False


def test_runs_are_deterministic(cfg: AppConfig, tmp_path: Path) -> None:
    def signature(sub: str) -> list[tuple[str, str, str, int]]:
        _, _, data = play(cfg, "basic", tmp_path / sub, duration_s=1800)
        return [(e.kind, e.actor, e.status, round(data.rel(e.ts))) for e in data.entries if e.kind != "llm_call"]

    assert signature("a") == signature("b")


# --------------------------------------------------------------------------- критерий 7 на полном прогоне


def test_llm_outage_for_whole_game_still_gives_results(cfg: AppConfig, tmp_path: Path) -> None:
    llm = LLMClient(LLMConfig(mode="mock"))
    llm.simulate_failure = "timeout"
    _, _, data = play(cfg, "basic", tmp_path, llm=llm, duration_s=1800)
    calls = [e for e in data.entries if e.kind == "llm_call"]
    assert calls and all(c.data["used_fallback"] is True for c in calls)
    recs = [e for e in data.entries if e.kind == "recommendation"]
    assert recs and any(e.result == "резервный сценарий" for e in recs)
    # безопасность не зависит от модели: ложный сигнал всё равно распознан кодом
    assert any(e.kind == "verdict" and e.data["verdict"]["kind"] == "false_signal" for e in data.entries)


# --------------------------------------------------------------------------- дополнение: ловушки и скрытые события


@pytest.fixture(scope="module")
def traps(cfg: AppConfig, tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    return play(cfg, "traps", tmp_path_factory.mktemp("traps"))


@pytest.fixture(scope="module")
def hidden(cfg: AppConfig, tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    return play(cfg, "hidden", tmp_path_factory.mktemp("hidden"))


def verdict_events(data: RunData, event: str | None = None) -> list[tuple[float, dict[str, Any]]]:
    return [
        (data.rel(e.ts), e.data)
        for e in data.entries
        if e.kind == "verdict" and (event is None or e.data["event"] == event)
    ]


def test_false_then_real_is_revised_within_watch_time(cfg: AppConfig, traps) -> None:  # type: ignore[no-untyped-def]
    """5: вердикт «ложный» создан, затем автоматически пересмотрен после подтверждающих свидетельств."""
    _, _, data = traps
    sensor = "co2_ppm.co2_b"
    created = [t for t, d in verdict_events(data, "created") if d["verdict"]["fields"][0] == sensor and d["verdict"]["kind"] == "false_signal"]
    revised = [t for t, d in verdict_events(data, "revised") if d["verdict"]["fields"][0] == sensor and d["verdict"]["kind"] == "real"]
    assert created and revised
    assert created[0] == pytest.approx(900, abs=3)
    assert 0 < revised[0] - created[0] <= cfg.verdicts.watch_after_false_s
    assert alerts(data, kind="verdict_revised", level="critical")  # Штаб получил тревогу «пересмотр вердикта»


def test_revision_is_journaled_with_both_verdicts(traps) -> None:  # type: ignore[no-untyped-def]
    _, _, data = traps
    ev = next(d for _, d in verdict_events(data, "revised") if d["verdict"]["fields"][0] == "co2_ppm.co2_b")
    assert ev["previous"]["status"] == "refuted" and ev["previous"]["kind"] == "false_signal"
    assert ev["verdict"]["status"] == "provisional" and ev["verdict"]["evidence"]


def test_trap_false_signal_is_recognised_and_not_acted_on(traps) -> None:  # type: ignore[no-untyped-def]
    _, _, data = traps
    trap = [d for t, d in verdict_events(data) if 2695 <= t <= 2720 and d["event"] in ("created", "revised")]
    assert trap and trap[0]["verdict"]["kind"] == "false_signal"  # ложный сигнал распознан (вердикт создан или пересмотрен)
    acted = [e for e in commands(data, "accepted") if 2690 <= data.rel(e.ts) <= 3300]
    assert not [e for e in acted if "'high'" in e.action]  # вытяжку на максимум никто не включал


def test_every_hidden_event_has_a_matching_signal(hidden) -> None:  # type: ignore[no-untyped-def]
    _, _, data = hidden
    kinds_by_field = {(a["kind"], a["field"]) for _, a in alerts(data)}
    kinds = {k for k, _ in kinds_by_field}
    assert ("cusum", "energy_balance") in kinds_by_field  # hidden_load
    assert any(k == "cusum" and f and "temp_c_2" in f for k, f in kinds_by_field)  # sensor_drift_pair
    assert "plan_vs_fact" in kinds  # silent_print_fail
    assert ("freeze", "co2_ppm.co2_a") in kinds_by_field  # frozen_sensor
    assert any(k == "noise_loss" and f == "temp_c" for k, f in kinds_by_field)  # noise_loss
    assert "time_monotonic" in kinds  # stale_timestamp
    assert "actuator_delayed" in kinds  # delayed_actuator: запаздывание распознано, а не отказ
    assert ("trend", "battery_temp_c") in kinds_by_field  # hidden_drift


def test_hidden_load_detected_before_critical_energy(hidden) -> None:  # type: ignore[no-untyped-def]
    _, _, data = hidden
    detect = min(t for t, a in alerts(data, kind="cusum", field="energy_balance"))
    crit = [t for t, a in alerts(data, kind="threshold", field="battery_soc_pct", level="critical")] + [
        t for t, a in alerts(data, kind="energy_t_left", level="critical")
    ]
    assert 900 < detect < 1100
    assert not crit or detect < min(crit) - 300


def test_delayed_actuator_is_not_reported_as_failure(hidden) -> None:  # type: ignore[no-untyped-def]
    _, _, data = hidden
    late = [e for e in commands(data) if e.status == "late_verified"]
    assert late and late[0].actor == "guard"
    revised = [d for t, d in verdict_events(data, "revised") if d["verdict"]["fields"][0] == "actuator:vent"]
    assert revised and revised[0]["verdict"]["kind"] == "channel_fault"  # «отказ» пересмотрен в «запаздывание»


# --------------------------------------------------------------------------- политика реакции в прогонах


def responses(data: RunData) -> list[JournalEntry]:
    return [e for e in data.entries if e.kind == "response"]


def test_quiet_has_no_act_level_responses(quiet) -> None:  # type: ignore[no-untyped-def]
    """4: на сценарии quiet нет реакций уровня act_* (регрессия на ложные срабатывания)."""
    _, _, data = quiet
    assert [e for e in responses(data) if e.data["level"].startswith("act_")] == []
    assert alerts(data, level="critical") == []


def test_costly_commands_and_act_full_always_have_two_sources_or_safety_mark(basic, traps, heavy, hidden) -> None:  # type: ignore[no-untyped-def]
    """Критерий приёмки 5: act_full и команды costly в журнале имеют ≥ 2 независимых свидетельств либо исключение безопасности."""
    checked = 0
    for _, _, data in (basic, traps, heavy, hidden):
        for e in responses(data):
            if e.data["level"] == "act_full":
                checked += 1
                assert len(e.data["sources"]) >= 2 or e.data["safety_event"], e.data
        for e in data.entries:
            if e.kind == "command" and e.data.get("costly") and e.status in ("accepted", "pending", "approved"):
                checked += 1
                assert len(e.data["evidence_sources"]) >= 2 or e.data["safety_event"] or e.actor.startswith("human"), e.data
    assert checked > 0, "в прогонах должны встретиться дорогие команды или act_full, иначе проверка пуста"


def test_traps_produce_no_cascade_actions(traps) -> None:  # type: ignore[no-untyped-def]
    """4: на сценарии traps система не совершает каскадных действий (вредные реакции в окне ловушки)."""
    _, sim, data = traps
    trap = next(g for g in sim.ground_truth if g["category"] == "trap")
    start, end = trap["t_start"], trap["t_start"] + trap["window_s"]
    harmful = set(trap["harmful"])
    done = [
        e
        for e in commands(data, "accepted")
        if start <= data.rel(e.ts) <= end and f"{e.data['command']}:{e.data['args'].get('mode')}" in harmful
    ]
    assert done == []
