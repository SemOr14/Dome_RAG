"""Тесты истории и графиков панели: рекордер, сборка графиков по прогону, вкладка «Графики»."""

from __future__ import annotations

from pathlib import Path

import altair as alt
import pytest

from kupol.core import charts as ch
from kupol.core.config import AppConfig
from kupol.engine import Engine
from kupol.ui import charts as ui
from tests.test_dashboard import make_engine, run_app


@pytest.fixture(scope="module")
def engine(config: AppConfig, tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    eng = make_engine(config, tmp_path_factory.mktemp("charts"), until=700)
    yield eng
    eng.close()


def test_recorder_keeps_series_and_downsamples(config: AppConfig) -> None:
    rec = ch.ChartRecorder(config)
    for t in range(1000):
        rec.record(float(t), None, residual_w=float(t) if t % 10 == 0 else None, cusum={"energy_balance": t / 100})
    pts = rec.series(ch.CUSUM + "energy_balance", max_points=100)
    assert len(pts) <= 101 and pts[-1] == (999.0, 9.99)  # последняя точка сохраняется
    assert len(rec.series(ch.RESIDUAL)) == 100  # редкий ряд пишется только при обновлении
    assert rec.series(ch.CUSUM + "energy_balance", since_s=990.0)[0][0] >= 990.0
    assert rec.last_time() == 999.0 and rec.names(ch.CUSUM) == [ch.CUSUM + "energy_balance"]


def test_recorder_ignores_non_finite_and_caps_length(config: AppConfig) -> None:
    rec = ch.ChartRecorder(config, max_points=10)
    for t in range(30):
        rec.record(float(t), None, residual_w=float("nan") if t == 5 else 1.0)
    assert len(rec.series(ch.RESIDUAL)) == 10


def test_engine_records_decision_quantities(engine: Engine) -> None:
    rec = engine.charts
    for name in ("pv_power_w", "load_total_w", "battery_soc_pct", "co2_ppm.co2_a", ch.RESIDUAL, ch.RESPONSE):
        assert rec.series(name), name
    assert rec.names(ch.PAIR_Z), "после калибровки есть разности пар в σ"
    assert rec.names(ch.CUSUM)
    assert rec.last_time() == pytest.approx(699, abs=1)
    p = [pt[1] for n in rec.names(ch.P_REAL) for pt in rec.series(n)]
    assert p and all(0.0 <= v <= 1.0 for v in p)


def test_y_domain_includes_only_nearby_rules() -> None:
    far = ui.y_domain([590.0, 610.0], [ui.Rule(2000.0, "крит. 2000", "crit")])
    assert far[1] < 700  # далёкий порог не сплющивает график
    near = ui.y_domain([900.0, 960.0], [ui.Rule(1000.0, "предупр. 1000", "warn")])
    assert near[1] > 1000
    flat = ui.y_domain([5.0, 5.0], [])
    assert flat[0] < 5.0 < flat[1]


def test_line_chart_is_none_without_data_and_uses_fixed_palette_order() -> None:
    assert ui.line_chart("пусто", {}, y_title="Вт", x_domain=(0.0, 5.0)) is None
    series = {"a": [(0.0, 1.0), (60.0, 2.0)], "b": [(0.0, 3.0), (60.0, 4.0)]}
    spec = ui.line_chart("два ряда", series, y_title="Вт", x_domain=(0.0, 5.0), rules=[ui.Rule(3.5, "предупр. 3.5", "warn")]).to_dict()
    scale = spec["layer"][0]["encoding"]["color"]["scale"]
    assert scale["domain"] == ["a", "b"] and scale["range"] == list(ui.SERIES_LIGHT[:2])
    assert spec["layer"][0]["encoding"]["color"]["legend"]["orient"] == "top"
    dark = ui.line_chart("тёмная", series, y_title="Вт", x_domain=(0.0, 5.0), dark=True).to_dict()
    assert dark["layer"][0]["encoding"]["color"]["scale"]["range"] == list(ui.SERIES_DARK[:2])
    assert spec["title"]["subtitle"] == ["предупр. 3.5"]
    assert len(spec["layer"]) == 6  # линия, перекрестие, точка, подсказка, линия порога, подпись порога


def test_single_series_has_no_legend() -> None:
    spec = ui.line_chart("один", {"a": [(0.0, 1.0), (60.0, 2.0)]}, y_title="Вт", x_domain=(0.0, 5.0)).to_dict()
    assert spec["layer"][0]["encoding"]["color"]["legend"] is None


def test_build_charts_covers_energy_climate_and_diagnostics(engine: Engine) -> None:
    sections = dict(ui.build_charts(engine, 15.0))
    assert list(sections) == ["Энергия", "Микроклимат", "Диагностика решений"]
    titles = {item.to_dict()["title"]["text"] for items in sections.values() for item in items if not isinstance(item, str)}
    assert {"Заряд АКБ", "CO2 по датчикам", "Невязка энергобаланса", "CUSUM: накопленное смещение"} <= titles
    assert "Разность пар датчиков за вычетом типичной" in titles and "Уровень реакции" in titles
    assert all(isinstance(i, (alt.LayerChart, str)) for items in sections.values() for i in items)
    co2 = next(i for i in sections["Микроклимат"] if not isinstance(i, str) and i.to_dict()["title"]["text"] == "CO2 по датчикам")
    assert co2.to_dict()["layer"][0]["encoding"]["color"]["scale"]["domain"] == ["co2_a", "co2_b", "co2_c"]
    assert ui.build_charts(engine, None)  # вся игра


def test_empty_engine_gives_placeholders_not_errors(config: AppConfig, tmp_path: Path) -> None:
    eng = make_engine(config, tmp_path, until=0)
    try:
        sections = ui.build_charts(eng, 15.0)
        assert all(isinstance(i, str) for _, items in sections for i in items)
    finally:
        eng.close()


def test_dashboard_has_charts_tab_with_vega_charts(engine: Engine) -> None:
    at = run_app(engine)
    assert [t.label for t in at.tabs] == ["Штаб", "Графики"]
    assert len(at.get("vega_lite_chart")) >= 10
    assert any("Диагностика решений" in m.value for m in at.markdown)
    assert len(at.get("button_group")) == 1  # выбор окна


def test_latest_values_table(engine: Engine) -> None:
    rows = ui.latest_values(engine)
    assert {r["Ряд"] for r in rows} >= {"battery_soc_pct", ch.RESIDUAL}
