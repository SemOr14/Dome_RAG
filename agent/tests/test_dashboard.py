"""Тесты панели Штаба на streamlit.testing (AppTest): без браузера и сервера."""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from kupol.adapter.scenario import load_scenario
from kupol.adapter.simulator import SimulatorAdapter
from kupol.core.config import AppConfig
from kupol.engine import Engine
from kupol.ui import dashboard
from kupol.core.models import AlertLevel
from tests.conftest import ROOT, T0


def make_engine(config: AppConfig, tmp_path: Path, scenario: str = "basic", until: int = 700) -> Engine:
    sim = SimulatorAdapter(config, load_scenario(ROOT / "scenarios" / f"{scenario}.yaml"), seed=42)
    eng = Engine(config, sim, tmp_path / "run", duration_s=5400)
    eng.view.running = True  # как в живом цикле
    for _ in range(until):
        eng.step()
    return eng


def script(engine, who="диагност"):  # type: ignore[no-untyped-def]  # без аннотаций: AppTest копирует исходник функции
    from kupol.ui.dashboard import render

    render(engine, who)


def run_app(engine: Engine, who: str = "диагност") -> AppTest:
    at = AppTest.from_function(script, args=(engine, who), default_timeout=20)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


@pytest.fixture
def engine(config: AppConfig, tmp_path: Path):  # type: ignore[no-untyped-def]
    eng = make_engine(config, tmp_path)
    yield eng
    eng.close()


def texts(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption] + [m.label + " " + m.value for m in at.metric]
    return "\n".join(parts)


def test_dashboard_shows_key_parameters(engine: Engine) -> None:
    at = run_app(engine)
    labels = {m.label: m.value for m in at.metric}
    assert {"Заряд АКБ", "Генерация", "Нагрузка", "Запас до мин. заряда", "CO2 (макс.)", "Влажность", "Температура", "Температура АКБ"} <= set(labels)
    assert labels["Заряд АКБ"].endswith("%") and labels["Генерация"].endswith("Вт")
    body = texts(at)
    assert "**Связь:** :green[есть]" in body and "**Игра:** 11:39 / 1:30:00" in body and "**LLM:** mock" in body
    assert "**Вытяжка:**" in body and "**ЧПУ:**" in body and "**Паяльная станция:**" in body
    assert "завершена" not in body


def test_dashboard_shows_alerts_with_confirmation_labels(engine: Engine) -> None:
    body = texts(run_app(engine))
    assert "Тревоги" in body and "co2_ppm.co2_b" in body
    assert "подтверждено двумя источниками (ложный сигнал)" in body  # ложный CO2: co2_a и co2_c подтверждают независимо


def test_confirmation_label_rules() -> None:
    from kupol.core.models import Alert, Evidence, Verdict
    from kupol.ui.dashboard import confirmation_label

    a = Alert(ts=T0, level=AlertLevel.WARNING, kind="threshold", field="temp_c", message="m")
    one = Verdict(ts=T0, kind="real", confidence=0.8, explanation="e", fields=["temp_c"], evidence=[Evidence(source="a", supports="real", weight=0.5)])
    two = one.model_copy(update={"evidence": [Evidence(source="a", supports="real", weight=0.5), Evidence(source="b", supports="real", weight=0.5)]})
    unknown = Verdict(ts=T0, kind="unknown", confidence=0.5, explanation="e", fields=["temp_c"])
    assert "ждёт подтверждения" in confirmation_label(a, [])
    assert "ждёт подтверждения" in confirmation_label(a, [one]) and "ждёт подтверждения" in confirmation_label(a, [unknown])
    assert "подтверждено двумя источниками (реальный)" in confirmation_label(a, [two])
    other = Alert(ts=T0, level=AlertLevel.WARNING, kind="threshold", field="humidity_pct", message="m")
    assert "ждёт подтверждения" in confirmation_label(other, [two])


def test_verdict_block_shows_evidence_status_and_recheck(engine: Engine) -> None:
    at = run_app(engine)
    body = texts(at)
    assert "Вердикты" in body and "co2_ppm.co2_b" in body and "ложный сигнал" in body
    assert "За «реальный»" in body and "За «ложный»" in body and "Следующая перепроверка через" in body
    keys = {b.key for b in at.button}
    v = next(v for v in engine.view.verdicts if v.fields[0] == "co2_ppm.co2_b")
    assert f"probe-{v.id}" in keys and f"close-{v.id}" in keys
    assert f"статус `{v.status}`" in body


def test_close_verdict_manually_via_button(engine: Engine) -> None:
    v = next(v for v in engine.view.verdicts if v.fields[0] == "co2_ppm.co2_b")
    at = run_app(engine)
    {b.key: b for b in at.button}[f"close-{v.id}"].click().run()
    engine.step()  # действие человека исполняется в цикле
    assert engine.book.by_id(v.id) is None
    assert any(e.data["event"] == "closed" for e in engine.journal.entries(kinds=["verdict"]))
    assert any("закрыл вердикт" in n for n in engine.view.notices)


def test_run_probe_via_button_goes_through_engine(config: AppConfig, tmp_path: Path) -> None:
    eng = make_engine(config, tmp_path, "basic", until=700)
    try:
        v = next(v for v in eng.view.verdicts if v.fields[0] == "co2_ppm.co2_b")
        at = run_app(eng)
        {b.key: b for b in at.button}[f"probe-{v.id}"].click().run()
        eng.step()
        probes = eng.journal.entries(kinds=["probe"])
        assert probes and probes[0].data["subject"] == "co2_ppm.co2_b"
        assert any("запустил пробу" in n for n in eng.view.notices)
    finally:
        eng.close()


def test_human_probe_answer_buttons(config: AppConfig, tmp_path: Path) -> None:
    eng = make_engine(config, tmp_path, "basic", until=700)
    try:
        v = next(v for v in eng.view.verdicts if v.fields[0] == "co2_ppm.co2_b")
        eng.request_probe(v.id, "диагност", probe_id="operator_check_indicator")
        eng.step()
        run = next(r for r in eng.view.probe_runs if r["probe"] == "operator_check_indicator")
        assert run["state"] == "running" and run["by_human"]
        at = run_app(eng)
        assert "Безопасные пробы" in texts(at) or any("Безопасные пробы" in e.label for e in at.expander)
        {b.key: b for b in at.button}[f"ans-false-{run['id']}"].click().run()
        eng.step()
        done = next(r for r in eng.view.probe_runs if r["id"] == run["id"])
        assert done["state"] == "done" and "false" in (done["result"] or "")
    finally:
        eng.close()


def test_forecast_block(engine: Engine) -> None:
    engine.view.forecasts = [{"field": "battery_temp_c", "eta_s": 540.0, "limit": 55.0, "slope_per_min": 1.0, "r2": 0.98}]
    at = run_app(engine)
    assert any("Прогноз до порога (1)" in e.label for e in at.expander)
    assert "battery_temp_c" in texts(at) or any("battery_temp_c" in m.value for m in at.markdown)
    engine.view.forecasts = []
    assert any("Прогноз до порога (0)" in e.label for e in run_app(engine).expander)


def test_round_block_opens_by_timer_and_marks(engine: Engine) -> None:
    assert engine.view.round_no == 1 and len(engine.view.round_items) == len(engine.config.round.pairs)  # 420 с < 700 с
    at = run_app(engine)
    assert any("Обход №1" in e.label for e in at.expander)
    keys = {b.key: b for b in at.button}
    first, second = engine.view.round_items[0], engine.view.round_items[1]
    keys[f"ok-{first.id}"].click()
    at.run()
    keys2 = {b.key: b for b in at.button}
    keys2[f"bad-{second.id}"].click().run()
    engine.step()
    assert engine.round.items[0].status == "ok" and engine.round.items[1].status == "mismatch"
    rounds = engine.journal.entries(kinds=["round"])
    assert any("РАСХОЖДЕНИЕ" in e.event for e in rounds) and any("расхождений нет" in e.event for e in rounds)
    assert [a for a in engine.alerts.active(engine.adapter.now()) if a.kind == "round_mismatch"]
    assert engine.counters["round_mismatches"] == 1


def test_pending_recommendation_can_be_accepted(engine: Engine) -> None:
    pending = engine.board.pending()
    assert pending, "для проверки нужна рекомендация, ожидающая решения"
    target = sorted(pending, key=lambda r: r.priority)[0]
    at = run_app(engine)
    buttons = {b.key: b for b in at.button}
    assert f"acc-{target.id}" in buttons and f"rej-{target.id}" in buttons
    buttons[f"acc-{target.id}"].click().run()
    assert not at.exception
    assert target.status.value == "accepted"
    entries = [e for e in engine.journal.entries(kinds=["recommendation"]) if e.actor == "диагност"]
    assert entries and "принято человеком" in entries[-1].event  # решение сразу в журнале


def test_reject_button_rejects_and_logs(engine: Engine) -> None:
    target = engine.board.pending()[0]
    at = run_app(engine, who="оператор")
    {b.key: b for b in at.button}[f"rej-{target.id}"].click().run()
    assert target.status.value == "rejected"
    assert any(e.actor == "оператор" and "отклонено" in e.event for e in engine.journal.entries(kinds=["recommendation"]))


@pytest.mark.slow
def test_accept_confirms_commands_in_guard(config: AppConfig, tmp_path: Path) -> None:
    eng = make_engine(config, tmp_path, "cascade_heavy", until=1500)
    try:
        with_cmds = [r for r in eng.board.pending() if r.commands]
        assert with_cmds, "энергопланировщик должен предложить команды, требующие подтверждения"
        rec = with_cmds[0]
        assert all(eng.guard.is_pending(c.id) for c in rec.commands if c.name == "set_line")
        assert eng.guard.pending() and not [c for c in eng.journal.entries(kinds=["command"]) if c.status == "approved"]
        run_app(eng)
        at = AppTest.from_function(script, args=(eng, "диагност"), default_timeout=20).run()
        {b.key: b for b in at.button}[f"acc-{rec.id}"].click().run()
        assert [c for c in eng.journal.entries(kinds=["command"]) if c.status == "approved"]
    finally:
        eng.close()


def test_manual_command_goes_through_guard(engine: Engine) -> None:
    at = run_app(engine)
    at.selectbox(key="manual-cmd").select("set_humidifier_mode").run()
    at.selectbox(key="arg-set_humidifier_mode-mode").select("low").run()
    {b.key: b for b in at.button}["manual-send"].click().run()
    assert not at.exception
    cmds = engine.journal.entries(kinds=["command"])
    assert cmds[-1].actor == "human:диагност" and "set_humidifier_mode" in cmds[-1].action
    assert any("accepted" in s.value for s in at.success)


def test_manual_out_of_range_is_rejected_by_guard(engine: Engine) -> None:
    at = run_app(engine)
    # set_line допускает только lighting/aux: «cnc» выбрать нельзя, а ограничение частоты проверяется предохранителем
    options = at.selectbox(key="manual-cmd").options
    assert "set_vent_mode" in options and "disable_interlock" not in options
    at.selectbox(key="manual-cmd").select("vent_enable").run()
    assert at.selectbox(key="arg-vent_enable-mode").options == ["low", "auto"]  # high для авто-команды недоступен


def test_stage_can_be_marked_done(engine: Engine) -> None:
    at = run_app(engine)
    name = "Сборка в корпус"
    {b.key: b for b in at.button}[f"done-{name}"].click().run()
    assert engine.process.status[name].value == "done"
    assert any("отмечен выполненным" in e.event for e in engine.journal.entries(kinds=["human"]))


def test_journal_tail_has_at_most_20_rows(engine: Engine) -> None:
    at = run_app(engine)
    tables = [d for d in at.dataframe if "Время" in d.value.columns]
    assert tables and 0 < len(tables[0].value) <= 20


def test_error_banner(engine: Engine) -> None:
    def err_script(engine):  # type: ignore[no-untyped-def]
        from kupol.ui.dashboard import render

        render(engine, "x", "NotImplementedError: адаптер")

    at = AppTest.from_function(err_script, args=(engine,), default_timeout=20).run()
    assert any("Главный цикл остановился" in e.value for e in at.error)


# --------------------------------------------------------------------------- вспомогательное


def test_parse_args_defaults_and_values() -> None:
    a = dashboard.parse_args([])
    assert a.config == "config.yaml" and a.scenario is None and a.speed == 1.0
    b = dashboard.parse_args(["--scenario", "s.yaml", "--speed", "10", "--seed", "7", "--llm-mode", "mock"])
    assert (b.scenario, b.speed, b.seed, b.llm_mode) == ("s.yaml", 10.0, 7, "mock")


def test_fmt_clock() -> None:
    assert dashboard.fmt_clock(65) == "01:05" and dashboard.fmt_clock(3725) == "1:02:05" and dashboard.fmt_clock(-3) == "00:00"


@pytest.mark.slow
def test_start_runtime_runs_engine_in_thread(tmp_path: Path) -> None:
    args = dashboard.parse_args(
        ["--config", str(ROOT / "config.yaml"), "--scenario", str(ROOT / "scenarios" / "quiet.yaml"), "--speed", "0", "--logs-dir", str(tmp_path)]
    )
    rt = dashboard.start_runtime(args)
    rt.thread.join(timeout=300)
    assert not rt.thread.is_alive() and rt.error is None
    assert rt.engine.view.elapsed_s >= 5399 and (rt.engine.run_dir / "summary.json").exists()


def test_runtime_reports_unimplemented_real_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kupol.adapter.real import RealAdapter

    def not_implemented(self: object) -> None:
        raise NotImplementedError("Адаптер реального Комплекса не реализован")

    monkeypatch.setattr(RealAdapter, "get_telemetry", not_implemented)  # без обращения к сети
    args = dashboard.parse_args(["--config", str(ROOT / "config.yaml"), "--logs-dir", str(tmp_path)])
    rt = dashboard.start_runtime(args)
    rt.thread.join(timeout=30)
    assert rt.error and "NotImplementedError" in rt.error


def test_app_from_file_starts_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Полный путь страницы: файл dashboard.py → get_runtime → фрагмент (аргументы через окружение)."""
    monkeypatch.setenv(
        dashboard.ENV_ARGS,
        f'--config "{ROOT / "config.yaml"}" --scenario "{ROOT / "scenarios" / "quiet.yaml"}" --speed 0 --logs-dir "{tmp_path}"',
    )
    at = AppTest.from_file(str(Path(dashboard.__file__)), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(m.label == "Заряд АКБ" for m in at.metric)
    dashboard.get_runtime.clear()
