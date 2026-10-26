"""Графики панели: величины и диагностические показатели, по которым Штаб принимает решения.

Три группы: энергия (мощность, заряд, запас времени, АКБ), микроклимат (CO2, температура, влажность) и
диагностика решений (невязка баланса, CUSUM, разности пар в σ, вероятности вердиктов, уровень реакции).
Пороги и коридоры берутся из конфигурации — те же числа, что у детекторов, поэтому линия на графике и тревога
всегда согласованы. Сами числа копит ``core.charts.ChartRecorder``; здесь только отрисовка.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

import altair as alt
import streamlit as st

from kupol.core import charts as ch
from kupol.engine import Engine

# Категориальная палитра по слотам: фиксированный порядок, свои ступени для светлой и тёмной темы.
SERIES_LIGHT = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
SERIES_DARK = ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767")
WARN_COLOR = "#fab219"
CRIT_COLOR = "#d03b3b"
REF_COLOR = "#898781"
INK_LIGHT, INK_DARK = "#52514e", "#c3c2b7"

CHART_HEIGHT = 190
CHARTS_RUN_EVERY_S = 10  # период обновления вкладки (реальные секунды): каждое обновление перерисовывает графики целиком,
# поэтому чаще секунды они мигают
WINDOWS = {"5 мин": 5.0, "15 мин": 15.0, "Вся игра": None}
DEFAULT_WINDOW = "15 мин"
MAX_VERDICT_LINES = 6
RESPONSE_LABELS = ("наблюдение", "подготовка", "обратимое действие", "полное действие")

KIND_COLORS = {"warn": WARN_COLOR, "crit": CRIT_COLOR, "ref": REF_COLOR}


@dataclass(frozen=True)
class Rule:
    """Горизонтальная линия: порог, коридор или справочное значение."""

    value: float
    label: str
    kind: str  # warn | crit | ref


def short(name: str) -> str:
    """Короткая подпись ряда: ``co2_ppm.co2_a`` → ``co2_a``."""
    return name.removeprefix(ch.CO2_PREFIX)


def pair_title(key: str) -> str:
    """Подпись пары: ``a|b`` → ``a − b``."""
    parts = key.split("|")
    return " − ".join(short(p) for p in parts) if len(parts) == 2 else short(key)


def is_dark() -> bool:
    """Тёмная ли тема (если узнать нельзя — светлая)."""
    try:
        return bool(st.context.theme.type == "dark")
    except Exception:  # noqa: BLE001 - вне сессии Streamlit темы нет
        return False


def y_domain(values: Sequence[float], rules: Sequence[Rule]) -> list[float]:
    """Диапазон оси Y: данные с запасом; линии порогов входят, когда значение к ним приблизилось.

    Далёкий порог (CO2 600 ppm при пороге 2000) не должен сплющивать график, поэтому он остаётся
    только в подзаголовке; при приближении линия появляется сама.
    """
    lo, hi = min(values), max(values)
    mean = (abs(lo) + abs(hi)) / 2
    reach = max(2 * (hi - lo), 0.25 * mean)
    for r in rules:
        if lo - reach <= r.value <= hi + reach:
            lo, hi = min(lo, r.value), max(hi, r.value)
    pad = (hi - lo) * 0.08 if hi > lo else max(abs(hi) * 0.05, 1.0)
    return [lo - pad, hi + pad]


def rules_subtitle(rules: Sequence[Rule]) -> list[str]:
    """Строка с числами порогов под заголовком (видна всегда, даже когда линия за пределами оси)."""
    return ["  ·  ".join(r.label for r in rules)] if rules else []


def line_chart(
    title: str,
    series: dict[str, list[tuple[float, float]]],
    *,
    y_title: str,
    x_domain: tuple[float, float],
    rules: Sequence[Rule] = (),
    domain: Sequence[float] | None = None,
    step: bool = False,
    y_axis: alt.Axis | None = None,
    dark: bool = False,
) -> alt.LayerChart | None:
    """Линейный график с порогами, перекрестием и подсказкой по наведению.

    Args:
        title: Заголовок.
        series: Подпись ряда → точки ``(время, с; значение)``; цвета идут в порядке словаря.
        y_title: Подпись оси Y (единицы).
        x_domain: Окно по времени, минуты игры.
        rules: Линии порогов.
        domain: Фиксированный диапазон Y (иначе по данным).
        step: Ступенчатая линия (для дискретных величин).
        y_axis: Своя ось Y.
        dark: Ступени палитры для тёмной темы.

    Returns:
        График или ``None``, если данных нет.
    """
    rows: list[dict[str, Any]] = [{"t": t / 60.0, "v": v, "series": label} for label, pts in series.items() for t, v in pts if x_domain[0] <= t / 60.0 <= x_domain[1]]
    if not rows:
        return None
    dom = list(domain) if domain is not None else y_domain([r["v"] for r in rows], rules)
    shown = [r for r in rules if dom[0] <= r.value <= dom[1]]
    palette = SERIES_DARK if dark else SERIES_LIGHT
    labels = list(series)
    x_scale = alt.Scale(domain=list(x_domain), nice=False)
    y_scale = alt.Scale(domain=dom, nice=False, zero=False)
    x = alt.X("t:Q", scale=x_scale, axis=alt.Axis(title="время игры, мин", grid=False, tickMinStep=1))
    y = alt.Y("v:Q", scale=y_scale, axis=y_axis or alt.Axis(title=y_title))
    color = alt.Color(
        "series:N",
        scale=alt.Scale(domain=labels, range=list(palette[: len(labels)])),
        legend=alt.Legend(orient="top", title=None, symbolType="stroke") if len(labels) > 1 else None,
    )
    base = alt.Chart(alt.InlineData(values=rows)).encode(x=x, y=y, color=color)
    line = base.mark_line(strokeWidth=2, clip=True, interpolate="step-after" if step else "linear")
    nearest = alt.selection_point(fields=["t"], nearest=True, on="pointerover", clear="pointerout", empty=False)
    hover = base.mark_point(size=160, opacity=0).encode(
        tooltip=[
            alt.Tooltip("series:N", title="ряд"),
            alt.Tooltip("t:Q", title="время, мин", format=".1f"),
            alt.Tooltip("v:Q", title=y_title, format=".4~g"),
        ]
    ).add_params(nearest)
    dots = base.mark_point(filled=True, size=55).encode(opacity=alt.condition(nearest, alt.value(1), alt.value(0)))
    cross = alt.Chart(alt.InlineData(values=rows)).mark_rule(color=REF_COLOR, strokeWidth=1).encode(x=x).transform_filter(nearest)
    layers: list[Any] = [line, cross, dots, hover]
    if shown:
        rule_data = alt.InlineData(values=[{"y": r.value, "label": r.label, "kind": r.kind, "x": x_domain[1]} for r in shown])
        kinds = list(KIND_COLORS)
        rule_color = alt.Color("kind:N", scale=alt.Scale(domain=kinds, range=[KIND_COLORS[k] for k in kinds]), legend=None)
        layers.append(alt.Chart(rule_data).mark_rule(strokeDash=[6, 4], strokeWidth=1.5).encode(y=alt.Y("y:Q", scale=y_scale), color=rule_color))
        layers.append(
            alt.Chart(rule_data)
            .mark_text(align="right", dx=-4, dy=-6, fontSize=11, color=INK_DARK if dark else INK_LIGHT)
            .encode(x=alt.X("x:Q", scale=x_scale), y=alt.Y("y:Q", scale=y_scale), text="label:N")
        )
    params = alt.TitleParams(text=title, subtitle=rules_subtitle(rules), anchor="start", fontSize=14, subtitleFontSize=11)
    return cast(alt.LayerChart, alt.layer(*layers).properties(height=CHART_HEIGHT, title=params))


# --------------------------------------------------------------------------- сборка рядов


def _threshold_rules(engine: Engine, field: str, names: dict[str, str]) -> list[Rule]:
    """Пороги поля из конфигурации (те же, что у ``ThresholdDetector``)."""
    th = engine.config.thresholds_for(field)
    if th is None:
        return []
    out: list[Rule] = []
    for attr, kind in (("warn_high", "warn"), ("crit_high", "crit"), ("warn_low", "warn"), ("crit_low", "crit")):
        value = getattr(th, attr)
        if value is not None:
            out.append(Rule(float(value), f"{names[kind]} {value:g}", kind))
    return out


WORDS = {"warn": "предупр.", "crit": "крит."}


def _grab(engine: Engine, names: dict[str, str], since: float) -> dict[str, list[tuple[float, float]]]:
    """Ряды из рекордера под подписями; пустые отбрасываются."""
    out = {label: engine.charts.series(name, since) for name, label in names.items()}
    return {k: v for k, v in out.items() if v}


def build_charts(engine: Engine, window_min: float | None, dark: bool = False) -> list[tuple[str, list[alt.LayerChart | str]]]:
    """Собрать все графики: ``[(раздел, [график или текст-заглушка, ...]), ...]``.

    Args:
        engine: Работающий движок.
        window_min: Окно в минутах (``None`` — вся игра).
        dark: Тёмная ли тема.
    """
    cfg, rec = engine.config, engine.charts
    now_s = rec.last_time()
    since = 0.0 if window_min is None else max(0.0, now_s - window_min * 60.0)
    if window_min is None:
        x_domain = (0.0, max(engine.duration_s / 60.0, 1.0))
    else:
        hi = max(now_s / 60.0, window_min)
        x_domain = (hi - window_min, hi)
    kw: dict[str, Any] = {"x_domain": x_domain, "dark": dark}
    wait = "данных пока нет"

    def chart(title: str, series: dict[str, list[tuple[float, float]]], empty: str = wait, **extra: Any) -> alt.LayerChart | str:
        built = line_chart(title, series, **kw, **extra)
        return built if built is not None else f"{title}: {empty}"

    en = cfg.energy
    soc_rules = _threshold_rules(engine, "battery_soc_pct", WORDS) + [Rule(float(en.soc_min_pct), f"мин. заряд {en.soc_min_pct:g}", "ref")]
    t_left_rules = [Rule(float(en.t_left_warn_min), f"предупр. {en.t_left_warn_min:g}", "warn"), Rule(float(en.t_left_crit_min), f"крит. {en.t_left_crit_min:g}", "crit")]
    energy_section: list[alt.LayerChart | str] = [
        chart("Мощность: генерация и нагрузка", _grab(engine, {"pv_power_w": "генерация", "load_total_w": "нагрузка"}, since), y_title="Вт"),
        chart("Заряд АКБ", _grab(engine, {"battery_soc_pct": "заряд"}, since), y_title="%", rules=soc_rules),
        chart(
            "Запас времени до минимального заряда",
            _grab(engine, {ch.T_LEFT: "запас"}, since),
            y_title="мин",
            rules=t_left_rules,
            empty="АКБ не разряжается — запас не ограничен",
        ),
        chart("Напряжение АКБ", _grab(engine, {"battery_voltage_v": "напряжение"}, since), y_title="В", rules=_threshold_rules(engine, "battery_voltage_v", WORDS)),
        chart("Температура АКБ", _grab(engine, {"battery_temp_c": "АКБ"}, since), y_title="°C", rules=_threshold_rules(engine, "battery_temp_c", WORDS)),
    ]

    co2 = {n: short(n) for n in rec.names(ch.CO2_PREFIX)}
    co2_rules = _threshold_rules(engine, next(iter(co2), "co2_ppm.co2_a"), WORDS)
    climate_section: list[alt.LayerChart | str] = [
        chart("CO2 по датчикам", _grab(engine, co2, since), y_title="ppm", rules=co2_rules),
        chart("Температура в помещении (два датчика)", _grab(engine, {"temp_c": "temp_c", "temp_c_2": "temp_c_2"}, since), y_title="°C", rules=_threshold_rules(engine, "temp_c", WORDS)),
        chart("Влажность", _grab(engine, {"humidity_pct": "влажность"}, since), y_title="%", rules=_threshold_rules(engine, "humidity_pct", WORDS)),
    ]

    sigma = engine.baseline.balance_residual_sigma()
    residual_rules = [] if sigma is None else [Rule(3 * sigma, f"+3σ ({3 * sigma:.0f} Вт)", "ref"), Rule(-3 * sigma, "−3σ", "ref")]
    cusum_names = {n: ("баланс энергии" if n.removeprefix(ch.CUSUM) == "energy_balance" else pair_title(n.removeprefix(ch.CUSUM))) for n in rec.names(ch.CUSUM)}
    h = engine.suite.cusum.threshold_sigma
    pair_names = {n: pair_title(n.removeprefix(ch.PAIR_Z)) for n in rec.names(ch.PAIR_Z)}
    k = cfg.detectors.pair_diff.corridor_k
    p_names = rec.names(ch.P_REAL)
    p_series = {short(n.removeprefix(ch.P_REAL)): rec.series(n, since) for n in p_names}
    p_series = {n: pts for n, pts in p_series.items() if pts}
    shown = sorted(p_series, key=lambda n: -max(abs(p - 0.5) for _, p in p_series[n]))[:MAX_VERDICT_LINES]
    v = cfg.verdicts
    axis = alt.Axis(title="уровень", values=[0, 1, 2, 3], labelExpr="['" + "','".join(RESPONSE_LABELS) + "'][datum.value]", labelLimit=150)
    diag_section: list[alt.LayerChart | str] = [
        chart(
            "Невязка энергобаланса",
            _grab(engine, {ch.RESIDUAL: "невязка"}, since),
            y_title="Вт",
            rules=residual_rules,
            empty="окно невязки ещё не закончилось (нужна калибровка)",
        ),
        chart(
            "CUSUM: накопленное смещение",
            _grab(engine, cusum_names, since),
            y_title="σ",
            rules=[Rule(h, f"порог {h:g}σ", "crit")],
            empty="калибровка ещё не набрана",
        ),
        chart(
            "Разность пар датчиков за вычетом типичной",
            _grab(engine, pair_names, since),
            y_title="σ",
            rules=[Rule(k, f"коридор ±{k:g}σ", "warn"), Rule(-k, f"−{k:g}σ", "warn")],
            empty="калибровка ещё не набрана",
        ),
        chart(
            "Вероятность «реальный» по вердиктам",
            {n: p_series[n] for n in shown},
            y_title="вероятность",
            rules=[Rule(v.real_above, f"реальный ≥ {v.real_above:g}", "ref"), Rule(v.false_below, f"ложный ≤ {v.false_below:g}", "ref")],
            domain=(0.0, 1.0),
            empty="вердиктов пока нет",
        ),
        chart("Уровень реакции", _grab(engine, {ch.RESPONSE: "уровень"}, since), y_title="уровень", domain=(-0.15, 3.15), step=True, y_axis=axis),
    ]
    return [("Энергия", energy_section), ("Микроклимат", climate_section), ("Диагностика решений", diag_section)]


def latest_values(engine: Engine) -> list[dict[str, str]]:
    """Последнее значение каждого ряда (текстовая замена графикам: таблица)."""
    rec = engine.charts
    rows = []
    for name in rec.names():
        pts = rec.series(name, max_points=1)  # последняя точка (редкие ряды обновляются не каждый такт)
        if pts:
            rows.append({"Ряд": name, "Значение": f"{pts[-1][1]:.4g}", "Время игры, с": f"{pts[-1][0]:.0f}"})
    return rows


def render_charts(engine: Engine) -> None:
    """Вкладка «Графики»: выбор окна, кнопка обновления и три раздела."""
    c1, c2, _ = st.columns([3, 1, 4])
    with c1:
        choice = st.segmented_control("Окно", list(WINDOWS), default=DEFAULT_WINDOW, key="chart-window")
    with c2:
        st.button("Обновить", key="charts-refresh", help=f"Графики обновляются сами раз в {CHARTS_RUN_EVERY_S} с")
    sections = build_charts(engine, WINDOWS[choice or DEFAULT_WINDOW], is_dark())
    for title, items in sections:
        st.markdown(f"#### {title}")
        cols = st.columns(2)
        for i, item in enumerate(items):
            with cols[i % 2]:
                if isinstance(item, str):
                    st.caption(item)
                else:
                    st.altair_chart(item, width="stretch")
    with st.expander("Последние значения рядов (таблица)"):
        st.dataframe(latest_values(engine), hide_index=True, width="stretch")
