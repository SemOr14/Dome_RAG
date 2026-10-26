"""Панель Штаба (Streamlit).

Запуск: ``python -m kupol dashboard --scenario scenarios/basic.yaml --speed 10`` (симулятор)
или ``python -m kupol dashboard`` (реальный адаптер). Панель показывает ключевые параметры,
тревоги, вердикты сверки, рекомендации агентов с кнопками «Принять» и «Отклонить»
(решение сразу пишется в журнал), план операций, ручной ввод команд через предохранитель,
ленту журнала (20 записей), таймер игры и индикатор связи. Дополнение: блок «Вердикты» (вид, уверенность,
статус, свидетельства за и против, время перепроверки, кнопки «выполнить пробу» и «закрыть вручную»), блок
«Обход» (чек-лист сверки пар источников по таймеру), прогноз «время до порога», метки на тревогах
(«ждёт подтверждения» или «подтверждено двумя источниками») и кнопки событий безопасности.

Главный цикл ``Engine`` работает в фоновом потоке и создаётся один раз на процесс
(``st.cache_resource``); интерфейс обновляется каждую секунду фрагментом ``st.fragment``.
Все команды из панели идут только через ``Guard``.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import streamlit as st
from streamlit.errors import StreamlitAPIException

from kupol.core.config import AppConfig, ArgRule
from kupol.core.models import Alert, AlertLevel, Command, CommandStatus, DeviceStatus, Recommendation, Verdict
from kupol.core.verdicts import KIND_RU
from kupol.engine import Engine
from kupol.ui.charts import CHARTS_RUN_EVERY_S, render_charts

ENV_ARGS = "KUPOL_DASHBOARD_ARGS"  # аргументы через окружение (для тестов AppTest)
LEVEL_BADGE = {
    AlertLevel.CRITICAL: ":red[КРИТИЧНО]",
    AlertLevel.WARNING: ":orange[ВНИМАНИЕ]",
    AlertLevel.INFO: ":blue[инфо]",
}
STATUS_COLOR = {
    DeviceStatus.RUNNING: "green",
    DeviceStatus.IDLE: "gray",
    DeviceStatus.ERROR: "red",
    DeviceStatus.UNKNOWN: "orange",
}


# --------------------------------------------------------------------------- запуск движка


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Разобрать аргументы панели (их передаёт ``python -m kupol dashboard``).

    Args:
        argv: Аргументы после ``--`` в команде ``streamlit run``.

    Returns:
        Разобранные аргументы.
    """
    p = argparse.ArgumentParser(prog="kupol dashboard")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--scenario", default=None)
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--llm-mode", default=None)
    p.add_argument("--logs-dir", default=None)
    return p.parse_args(list(argv))


@dataclass
class Runtime:
    """Работающий движок и его поток."""

    engine: Engine
    thread: threading.Thread
    error: str | None = None


def build_engine(args: argparse.Namespace) -> Engine:
    """Собрать движок по аргументам (симулятор или реальный адаптер).

    Args:
        args: Аргументы панели.

    Returns:
        Готовый к запуску ``Engine``.

    Raises:
        ConfigError: Некорректный конфиг (панель покажет понятное сообщение).
    """
    from kupol.adapter.real import RealAdapter
    from kupol.adapter.scenario import load_scenario
    from kupol.adapter.simulator import SimulatorAdapter
    from kupol.agents.llm import LLMClient
    from kupol.core.config import load_config
    from kupol.core.journal import new_run_dir
    from kupol.main import load_env_file

    load_env_file()
    config = load_config(args.config)
    if args.llm_mode:
        config = AppConfig.model_validate({**config.model_dump(), "llm": {**config.llm.model_dump(), "mode": args.llm_mode}})
    if args.logs_dir:
        config.paths.logs_dir = args.logs_dir
    llm = LLMClient(config.llm)
    if args.scenario:
        scenario = load_scenario(args.scenario)
        adapter: Any = SimulatorAdapter(config, scenario, seed=args.seed)
        label, duration = scenario.name, scenario.duration_s
        meta = {"mode": "dashboard-simulate", "scenario": scenario.name, "seed": args.seed, "speed": args.speed}
    else:
        adapter = RealAdapter(config.real)
        label, duration = "dashboard", None
        meta = {"mode": "dashboard-run", "config": args.config}
    return Engine(
        config,
        adapter,
        new_run_dir(config.paths.logs_dir, label),
        llm=llm,
        agent_workers=2,
        duration_s=duration,
        meta=meta,
    )


def start_runtime(args: argparse.Namespace) -> Runtime:
    """Запустить движок в фоновом потоке.

    Args:
        args: Аргументы панели.

    Returns:
        Рабочее окружение; ошибка потока (например, не реализован адаптер) попадает в ``error``.
    """
    engine = build_engine(args)
    holder: dict[str, Runtime] = {}

    def target() -> None:
        try:
            engine.run(speed=args.speed)
        except BaseException as exc:  # noqa: BLE001 - поток не должен умирать молча
            holder["rt"].error = f"{exc.__class__.__name__}: {exc}"

    thread = threading.Thread(target=target, name="kupol-engine", daemon=True)
    rt = Runtime(engine, thread)
    holder["rt"] = rt
    thread.start()
    return rt


# --------------------------------------------------------------------------- отрисовка


def fmt_clock(seconds: float) -> str:
    """Секунды в ``мм:сс`` (или ``ч:мм:сс``)."""
    s = int(max(seconds, 0))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def _refresh() -> None:
    """Перерисовать панель после действия человека (фрагмент, а вне фрагмента — страницу целиком)."""
    try:
        st.rerun(scope="fragment")
    except StreamlitAPIException:
        st.rerun()


def render_header(engine: Engine, error: str | None) -> None:
    """Таймер игры, индикатор связи, режим LLM."""
    view = engine.view
    c1, c2, c3, c4 = st.columns([3, 3, 2, 2])
    c1.subheader("Штаб «Купол»")
    done = not view.running and view.ts is not None
    c2.markdown(f"**Игра:** {fmt_clock(view.elapsed_s)} / {fmt_clock(view.duration_s)}" + (" — завершена" if done else ""))
    c2.progress(min(view.elapsed_s / view.duration_s, 1.0) if view.duration_s else 0.0)
    if view.telemetry is None:
        c3.markdown("**Связь:** :gray[нет данных]")
    elif view.comms_ok:
        c3.markdown("**Связь:** :green[есть]")
    else:
        c3.markdown("**Связь:** :red[ПОТЕРЯНА, данные застыли]")
    c4.markdown(f"**LLM:** {view.llm_mode}")
    if error:
        st.error(f"Главный цикл остановился: {error}")


def render_metrics(engine: Engine) -> None:
    """Ключевые параметры."""
    view = engine.view
    tel, en = view.telemetry, view.energy
    t_left = "—" if en is None else ("∞" if en.t_left_min is None else f"{en.t_left_min:.0f} мин")
    cols = st.columns(4)
    cols[0].metric("Заряд АКБ", "—" if tel is None else f"{tel.battery_soc_pct:.0f} %")
    cols[1].metric("Генерация", "—" if tel is None else f"{tel.pv_power_w:.0f} Вт")
    cols[2].metric("Нагрузка", "—" if tel is None else f"{tel.load_total_w:.0f} Вт")
    cols[3].metric("Запас до мин. заряда", t_left, help="t_left = E·(SoC−SoC_min)/(P_load−P_pv), усреднение за окно")
    cols = st.columns(4)
    co2 = "—" if tel is None or not tel.co2_ppm else f"{max(tel.co2_ppm.values()):.0f} ppm"
    help_co2 = "" if tel is None else ", ".join(f"{k}: {v:.0f}" for k, v in sorted(tel.co2_ppm.items()))
    cols[0].metric("CO2 (макс.)", co2, help=help_co2)
    cols[1].metric("Влажность", "—" if tel is None else f"{tel.humidity_pct:.0f} %")
    cols[2].metric("Температура", "—" if tel is None else f"{tel.temp_c:.1f} °C")
    bt = None if tel is None else tel.battery_temp_c
    cols[3].metric("Температура АКБ", "—" if bt is None else f"{bt:.1f} °C")
    if tel is not None:
        cols = st.columns(6)
        for col, (name, status) in zip(
            cols[:3],
            [("ЧПУ", tel.cnc_status), ("3D-принтер", tel.printer_status), ("Паяльная станция", tel.solder_status)],
            strict=True,
        ):
            col.markdown(f"**{name}:** :{STATUS_COLOR[status]}[{status.value}]")
        cols[3].markdown(f"**Вытяжка:** {tel.vent_mode}")
        cols[4].markdown(f"**Увлажнитель:** {tel.humidifier_mode}")
        cols[5].markdown(f"**Энергорежим:** {tel.power_mode}")


def confirmation_label(alert: Alert, verdicts: list[Verdict]) -> str:
    """Метка тревоги: «ждёт подтверждения» или «подтверждено двумя источниками».

    Подтверждение считает код по вердикту поля: нужны определённый вид вердикта и не менее двух независимых
    источников свидетельств на стороне этого вида.
    """
    v = next((x for x in verdicts if alert.field and alert.field in x.fields and x.kind != "unknown"), None)
    if v is not None:
        side = "real" if v.kind == "real" else "false"
        if len(v.sources_for(side)) >= 2:  # type: ignore[arg-type]
            return f":green[подтверждено двумя источниками ({KIND_RU[v.kind]})]"
    return ":orange[ждёт подтверждения]"


def render_notices(engine: Engine) -> None:
    """Уведомления о действиях человека (запуск пробы, закрытие вердикта, отметки обхода)."""
    for text in engine.view.notices[-3:]:
        st.caption(f"ℹ {text}")


def render_alerts(engine: Engine) -> None:
    """Активные тревоги с метками подтверждения."""
    view = engine.view
    st.markdown("#### Тревоги")
    if not view.alerts:
        st.caption("Активных тревог нет")
    for a in view.alerts[:12]:
        st.markdown(f"{LEVEL_BADGE[a.level]} {a.message} — {confirmation_label(a, view.verdicts)}")


def render_forecast(engine: Engine) -> None:
    """Индикатор «прогноз до порога»: поля с оценкой времени до критического значения."""
    fc = engine.view.forecasts
    with st.expander(f"Прогноз до порога ({len(fc)})", expanded=bool(fc)):
        if not fc:
            st.caption("Ни одно поле не подходит к критическому порогу в пределах горизонта прогноза")
        for f in fc:
            eta = f["eta_s"]
            st.markdown(
                f"**{f['field']}** → порог {f['limit']:g} через ≈{eta / 60:.0f} мин "
                f"(тренд {f['slope_per_min']:+.3g}/мин, R²={f['r2']:.2f}); прогноз — не доказательство аномалии, реакция не выше «prepare»"
            )


def render_verdicts(engine: Engine, who: str) -> None:
    """Блок «Вердикты»: вид, уверенность, статус, свидетельства, перепроверка, кнопки пробы и ручного закрытия."""
    view = engine.view
    st.markdown("#### Вердикты")
    if not view.verdicts:
        st.caption("Вердиктов пока нет")
    for v in view.verdicts[:10]:
        with st.container(border=True):
            subject = ", ".join(v.fields[:2])
            level = view.responses.get(v.fields[0]) if v.fields else None
            reaction = f" · реакция `{level.level.value}`" if level is not None else ""
            st.markdown(f"**{subject}** — {KIND_RU[v.kind]} · {v.confidence:.0%} · статус `{v.status}`{reaction}")
            if v.recheck_at is not None and view.ts is not None:
                left = max(0.0, (v.recheck_at - view.ts).total_seconds())
                st.caption(f"Следующая перепроверка через {left:.0f} с")
            c1, c2 = st.columns(2)
            real = [e for e in v.evidence if e.supports == "real"]
            false = [e for e in v.evidence if e.supports == "false"]
            c1.markdown("**За «реальный»**\n" + ("\n".join(f"- {e.source}: {e.detail[:90]} ({e.weight:.2f})" for e in real) or "—"))
            c2.markdown("**За «ложный»**\n" + ("\n".join(f"- {e.source}: {e.detail[:90]} ({e.weight:.2f})" for e in false) or "—"))
            b1, b2, _ = st.columns([1, 1, 3])
            if b1.button("Выполнить пробу", key=f"probe-{v.id}"):
                engine.request_probe(v.id, who)
                _refresh()
            if b2.button("Закрыть вручную", key=f"close-{v.id}"):
                engine.close_verdict(v.id, who)
                _refresh()


def render_probes(engine: Engine, who: str) -> None:
    """Пробы: вопросы Оператору с кнопками ответа и результаты выполненных."""
    runs = engine.view.probe_runs
    if not runs:
        return
    with st.expander(f"Безопасные пробы ({len(runs)})", expanded=any(r["state"] == "running" and r["by_human"] for r in runs)):
        for r in reversed(runs):
            st.markdown(f"`{r['state']}` · **{r['probe']}** по {r['subject']}" + (f" — {r['result']}" if r["result"] else ""))
            if r["state"] == "running" and r["by_human"]:
                st.caption(r["message"])
                c1, c2, _ = st.columns([1, 1, 3])
                if c1.button("Оператор: реально", key=f"ans-real-{r['id']}"):
                    engine.answer_probe(r["id"], "real", f"human:{who}")
                    _refresh()
                if c2.button("Оператор: ложно", key=f"ans-false-{r['id']}"):
                    engine.answer_probe(r["id"], "false", f"human:{who}")
                    _refresh()


def render_round(engine: Engine, who: str) -> None:
    """Блок «Обход»: чек-лист сверки пар источников по таймеру."""
    view = engine.view
    pending = [i for i in view.round_items if i.status == "pending"]
    title = f"Обход №{view.round_no}: осталось {len(pending)}" if view.round_items else f"Обход: следующий через {fmt_clock(view.round_next_in_s)}"
    with st.expander(title, expanded=bool(pending)):
        if not view.round_items:
            st.caption("Первый обход откроется по таймеру; каждый пункт — сверка двух-трёх источников")
        for item in view.round_items:
            st.markdown(f"**{item.label}** — `{item.status}`  \n{item.hint}")
            if item.status == "pending":
                c1, c2, _ = st.columns([2, 1, 2])
                if c1.button("Проверил, расхождений нет", key=f"ok-{item.id}"):
                    engine.round_mark(item.id, True, f"human:{who}")
                    _refresh()
                if c2.button("Расхождение", key=f"bad-{item.id}"):
                    engine.round_mark(item.id, False, f"human:{who}")
                    _refresh()
        if view.round_items:
            st.caption(f"Следующий обход через {fmt_clock(view.round_next_in_s)}")


def _hypotheses_table(rec: Recommendation) -> None:
    """Гипотезы Диагноста с безопасными проверками."""
    st.dataframe(
        [{"Вероятность": f"{h.probability:.0%}", "Причина": h.cause, "Безопасная проверка": h.safe_check} for h in rec.hypotheses],
        hide_index=True,
        width="stretch",
    )


def render_recommendations(engine: Engine, who: str) -> None:
    """Рекомендации агентов с кнопками «Принять» и «Отклонить»."""
    st.markdown("#### Рекомендации агентов")
    pending = engine.board.pending()
    if not pending:
        st.caption("Рекомендаций, ожидающих решения, нет")
    for rec in sorted(pending, key=lambda r: r.priority):
        with st.container(border=True):
            badge = " · :orange[резервный сценарий]" if rec.used_fallback else ""
            st.markdown(f"**{rec.title}**  \nприоритет {rec.priority} · {rec.agent}{badge}")
            if rec.rationale:
                st.write(rec.rationale)
            if rec.hypotheses:
                _hypotheses_table(rec)
            if rec.commands:
                st.caption("Команды (после подтверждения, через предохранитель): " + "; ".join(f"{c.name} {c.args}" for c in rec.commands))
            c1, c2, _ = st.columns([1, 1, 4])
            if c1.button("Принять", key=f"acc-{rec.id}", type="primary"):
                engine.board.accept(rec.id, who)
                _refresh()
            if c2.button("Отклонить", key=f"rej-{rec.id}"):
                engine.board.reject(rec.id, who, "отклонено в панели")
                _refresh()
    recent = [r for r in engine.board.recent(15) if r.status.value != "pending"]
    if recent:
        with st.expander(f"Недавние рекомендации ({len(recent)})"):
            for r in recent:
                st.markdown(f"`{r.status.value}` · {r.agent} · **{r.title}**")


def _arg_widget(cmd_name: str, arg: str, rule: ArgRule) -> Any:
    """Виджет ввода аргумента по правилу из конфига."""
    key = f"arg-{cmd_name}-{arg}"
    if rule.type == "enum":
        return st.selectbox(arg, rule.values, key=key)
    if rule.type == "bool":
        return st.checkbox(arg, key=key)
    return st.number_input(arg, min_value=rule.min, max_value=rule.max, key=key)


def render_manual_command(engine: Engine, who: str) -> None:
    """Ручная команда: тоже только через предохранитель, диапазоны из конфига."""
    with st.expander("Ручная команда (через предохранитель)"):
        rules = engine.config.guard.commands
        name = st.selectbox("Команда", sorted(rules), key="manual-cmd")
        rule = rules[name]
        st.caption(f"{rule.description or name}; класс: {rule.klass}")
        args = {arg: _arg_widget(name, arg, r) for arg, r in rule.args.items()}
        if st.button("Отправить", key="manual-send"):
            res = engine.guard.submit(Command(name=name, args=args, reason="ручная команда из панели"), f"human:{who}")
            st.session_state["last_cmd"] = f"{res.status.value}: {res.message}"
        if "last_cmd" in st.session_state:
            ok = st.session_state["last_cmd"].startswith(CommandStatus.ACCEPTED.value)
            (st.success if ok else st.warning)(st.session_state["last_cmd"])


def render_plan(engine: Engine) -> None:
    """План операций и ручная отметка этапов."""
    view = engine.view
    with st.expander("План операций и этапы изделия"):
        if view.plan:
            st.dataframe(
                [
                    {
                        "Этап": p.operation.name,
                        "Приоритет": ("безопасность", "неприрываемый", "прерываемый")[p.priority],
                        "Можно начать": "да" if p.can_start else "нет",
                        "Пояснение": p.reason,
                    }
                    for p in view.plan
                ],
                hide_index=True,
                width="stretch",
            )
        st.markdown("**Статус этапов** (отметьте вручную то, что Оператор завершил):")
        for op in engine.config.process.operations:
            status = engine.process.status[op.name]
            c1, c2 = st.columns([4, 1])
            c1.markdown(f"{op.name} — `{status.value}`")
            if status.value != "done" and c2.button("Выполнен", key=f"done-{op.name}"):
                engine.process.mark_done(op.name)
                engine.journal.log(actor="human", event=f"этап «{op.name}» отмечен выполненным", status="done", kind="human")
                _refresh()


def render_journal(engine: Engine) -> None:
    """Лента журнала: последние 20 записей."""
    st.markdown("#### Журнал (последние 20 записей)")
    start = engine.start_time
    rows = []
    for e in reversed(engine.journal.tail(20, exclude_kinds=("llm_call",))):
        rel = (e.ts - start).total_seconds() if start else 0.0
        rows.append(
            {"Время": fmt_clock(rel), "Кто": e.actor, "Событие": e.event[:120], "Действие": e.action[:80], "Результат": e.result[:80], "Статус": e.status}
        )
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch")
    else:
        st.caption("Записей пока нет")


def render_head(engine: Engine, error: str | None = None) -> None:
    """Шапка, ключевые параметры и уведомления (видны над вкладками)."""
    render_header(engine, error)
    render_metrics(engine)
    render_notices(engine)


def render_main(engine: Engine, who: str = "диагност") -> None:
    """Содержимое вкладки «Штаб»: тревоги, вердикты, рекомендации, журнал."""
    left, right = st.columns([5, 7])
    with left:
        render_alerts(engine)
        render_forecast(engine)
        render_verdicts(engine, who)
        render_plan(engine)
    with right:
        render_recommendations(engine, who)
        render_probes(engine, who)
        render_round(engine, who)
        render_manual_command(engine, who)
    render_journal(engine)
    counters = engine.view.counters
    st.caption(
        "Счётчики: " + ", ".join(f"{k}={v}" for k, v in counters.items() if k in ("ticks", "agent_runs", "agent_fallbacks", "step_errors", "poll_errors", "verdict_revisions", "probes"))
    )


def render(engine: Engine, who: str = "диагност", error: str | None = None) -> None:
    """Нарисовать панель целиком одним вызовом (тесты); в браузере части живут в отдельных фрагментах (см. ``main``).

    Args:
        engine: Работающий движок.
        who: Имя человека, подтверждающего решения (попадает в журнал).
        error: Ошибка главного цикла, если он остановился.
    """
    render_head(engine, error)
    main_tab, charts_tab = st.tabs(["Штаб", "Графики"])
    with main_tab:
        render_main(engine, who)
    with charts_tab:
        render_charts(engine)


# --------------------------------------------------------------------------- точка входа Streamlit


@st.cache_resource(show_spinner="Запуск главного цикла…")
def get_runtime(argv: tuple[str, ...]) -> Runtime:
    """Единственный на процесс движок (переживает перезагрузки страницы)."""
    return start_runtime(parse_args(argv))


def main() -> None:
    """Страница Streamlit."""
    st.set_page_config(page_title="Штаб Купол", layout="wide")
    raw = os.environ.get(ENV_ARGS)
    argv = tuple(shlex.split(raw)) if raw is not None else tuple(sys.argv[1:])
    try:
        rt = get_runtime(argv)
    except Exception as exc:  # noqa: BLE001 - ошибки конфига показываем человеку, а не трассировкой
        st.error(f"Не удалось запустить Штаб: {exc}")
        st.stop()
        return
    who = st.sidebar.text_input("Кто подтверждает решения", value="диагност")
    st.sidebar.caption(f"Журнал: {rt.engine.run_dir}")
    with st.sidebar.expander("Событие безопасности"):
        st.caption("Немедленная реакция: правило двух не применяется, белый список и диапазоны команд действуют.")
        for event, label in (("smoke", "Дым"), ("solder_overheat", "Перегрев паяльника"), ("operator_stop", "«Стоп» Оператора")):
            if st.button(label, key=f"safety-{event}"):
                rt.engine.report_safety_event(event, "сообщено из панели", who=f"human:{who or 'диагност'}")
    if st.sidebar.button("Завершить игру"):
        rt.engine.stop()

    # Части обновляются независимо: экран — раз в секунду, графики — редко (каждое их обновление перерисовывает
    # график целиком, и частые обновления выглядят как мигание).
    @st.fragment(run_every=1)
    def live_head() -> None:
        render_head(rt.engine, rt.error)

    @st.fragment(run_every=1)
    def live_main() -> None:
        render_main(rt.engine, who or "диагност")

    @st.fragment(run_every=CHARTS_RUN_EVERY_S)
    def live_charts() -> None:
        render_charts(rt.engine)

    live_head()
    main_tab, charts_tab = st.tabs(["Штаб", "Графики"])
    with main_tab:
        live_main()
    with charts_tab:
        live_charts()


if __name__ == "__main__":  # Streamlit исполняет скрипт как __main__; при импорте из тестов страница не строится
    main()
