"""Иерархия узлов: схема в системном сообщении и положение затронутых узлов в пользовательском."""

from __future__ import annotations

from pathlib import Path

import yaml

from llm_agent.context.hierarchy import Hierarchy

ROOT = Path(__file__).resolve().parents[2]
H = Hierarchy.load(ROOT / "data" / "node_hierarchy.yaml")


def test_every_catalog_node_is_in_exactly_one_system() -> None:
    expected = {"solar_panels_01", "solar_inverter_01", "battery_01", "wind_turbine_01", "dizel_1", "fuel_tank_01", "smart_panel_01",
                "printer_3d_01", "cnc_01", "material_inventory_01", "equipment_cooling_01", "thermal_insulation_01", "thermal_insulation_02",
                "climate_sensor_01", "climate_sensor_02", "climate_sensor_03", "air_quality_sensor_01", "fume_extraction_01",
                "supply_ventilation_01", "dome_sealing_01", "smoke_detector_01", "smoke_detector_02", "fire_suppression_01",
                "access_control_01", "radiation_sensor_01", "chem_sensor_01", "weather_station_01", "seysmo_01", "water_pump_01",
                "water_tank_01", "water_filter_01", "network_link_01", "radio_01", "backup_comms_01", "edge_compute_01",
                "dome_automation_01", "meeting_01"}
    spec = yaml.safe_load((ROOT / "data" / "node_hierarchy.yaml").read_text(encoding="utf-8"))
    listed = [n for s in spec["systems"].values() for n in s["nodes"]]
    assert len(listed) == len(set(listed)), "узел в двух системах"
    assert set(listed) == expected
    assert set(spec["roles"]) == expected


def test_printer_chain_matches_the_api_line_assignment() -> None:
    line = next(h for h in H.context({"printer_3d_01"}))
    assert "питание: printer_3d_01 ← line:3 ← smart_panel_01 ← solar_inverter_01 ← solar_panels_01|wind_turbine_01|battery_01|dizel_1" in line
    assert "внутри thermal_insulation_01" in line and "данные: printer_3d_01 ← network_link_01 ← edge_compute_01 ← dome_api" in line


def test_unknown_power_is_not_invented_and_dependents_are_listed() -> None:
    assert "питание в документации не описано" in H.context({"climate_sensor_02"})[0]
    inverter = H.context({"solar_inverter_01"})[0]
    assert "printer_3d_01" in inverter and "cnc_01" in inverter and "line:" not in inverter.split("от него зависят:")[1].split(";")[0]
    assert H.context({"unknown_node"}) == []


def test_outline_is_short_and_states_prototype_status() -> None:
    text = "\n".join(H.outline())
    assert "прототип" in text and "line:1 → workstation, fume_extraction_01" in text
    assert len(H.outline()) < 40 and len(text) < 5000
