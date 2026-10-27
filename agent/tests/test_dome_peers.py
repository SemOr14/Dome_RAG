"""Сверка аномалии одного датчика с близкими и похожими: что попадает в промпт."""

from __future__ import annotations

from llm_agent.context.peers import compare_peers, peer_lines
from llm_agent.context.telemetry import compact_telemetry

SNAP = {
    "timestamp": 1000.0,
    "nodes": {
        "climate_sensor_01": {"co2_ppm": 3012, "updated_at": 998.0, "status": "OK", "sensor_state": "OK"},
        "climate_sensor_02": {"co2_ppm": 781, "updated_at": 999.0, "status": "OK"},
        "climate_sensor_03": {"co2_ppm": 790, "updated_at": 990.0, "status": "DEGRADED"},
        "battery_01": {"soc_pct": 20.0, "updated_at": 999.0},
        "solar_inverter_01": {"battery_soc_pct": 20.2, "updated_at": 999.0},
        "printer_3d_01": {"co2_ppm": 5},  # другое семейство и система: не сосед
    },
}
CATALOG = {"climate_sensor_01": {"system": "climate"}, "climate_sensor_02": {"system": "climate"},
           "climate_sensor_03": {"system": "climate"}, "printer_3d_01": {"system": "production"}}


def test_diverging_co2_is_shown_against_family_with_state_and_age() -> None:
    ctx = compact_telemetry(SNAP, CATALOG)
    rows = compare_peers(SNAP, ctx.anomalies, CATALOG)
    row = next(r for r in rows if r["node"] == "climate_sensor_01" and r["param"] == "co2_ppm")
    assert [p["node"] for p in row["peers"]] == ["climate_sensor_02", "climate_sensor_03"]
    assert row["median"] == 785.5 and row["delta"] == 3012 - 785.5
    assert "status=DEGRADED" in row["peers"][1]["state"] and "10 с назад" in row["peers"][1]["state"]
    line = peer_lines([row])[0]
    assert "climate_sensor_02 = 781" in line and "printer_3d_01" not in line


def test_battery_peer_is_marked_as_not_independent() -> None:
    ctx = compact_telemetry(SNAP, CATALOG)
    row = next(r for r in compare_peers(SNAP, ctx.anomalies, CATALOG) if r["node"] == "battery_01")
    assert row["peers"][0]["node"] == "solar_inverter_01" and row["peers"][0]["param"] == "battery_soc_pct"
    assert "не независимый" in row["peers"][0]["relation"]


def test_no_peers_no_row_and_service_params_skipped() -> None:
    snap = {"timestamp": 1.0, "nodes": {"printer_3d_01": {"bed_clear": False, "updated_at": 1.0}}}
    anomalies = [{"node": "printer_3d_01", "param": "bed_clear", "value": False, "note": "x"},
                 {"node": "printer_3d_01", "param": "updated_at", "value": 99, "note": "y"},
                 {"node": "smart_panel_01", "param": "lines[0]", "value": "TRIPPED", "note": "z"}]
    assert compare_peers(snap, anomalies, {}) == []
