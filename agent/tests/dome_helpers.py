"""Общие помощники тестов рантайма Dome: снимки телеметрии из журнала 14.10 и каталог узлов."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from kupol.dome.catalog import Catalog
from kupol.dome.state import DomeState

FIXTURES = Path(__file__).parent / "fixtures"
_SNAPSHOTS: dict[str, Any] = {}

# Каталог узлов по 01_API.md §8 (то, что вернул бы describe_nodes команде): основное имя -> алиасы.
CATALOG_SPEC: dict[str, dict[str, Any]] = {
    "solar_inverter_01": {
        "controls": ["set_mode_hybrid", "set_mode_grid_only", "enable_mppt", "disable_mppt", "start_cell_balancing",
                     "cancel_balancing", "reset_error", "set_fan"],
        "aliases": {"set_mode_hybrid": ["hybrid"], "set_mode_grid_only": ["grid_only"]},
    },
    "solar_panels_01": {"controls": ["enable_mppt", "disable_mppt", "enable_mppt_1", "disable_mppt_1", "enable_all_mppt", "disable_all_mppt"]},
    "smart_panel_01": {"controls": ["line_on", "line_off", "reset_protection", "reset_line_error"], "operator_controls": ["set_workstation", "inspect_line"]},
    "printer_3d_01": {
        "controls": ["home", "start_job", "resume", "upload_file", "delete_file", "set_speed", "move"],
        "aliases": {"start_job": ["start_print", "print_file"]},
        "operator_controls": ["remove_part"],
        "queries": ["list_files"],
        "service_actions": ["reconnect"],
    },
    "cnc_01": {"controls": ["home", "start_job", "resume", "move", "set_zero", "probe_z"], "queries": ["list_files"], "service_actions": ["reconnect"]},
    "thermal_insulation_01": {"controls": ["set_fan_pwm", "set_lock", "set_vacuum", "set_poll_interval"]},
    "climate_sensor_02": {"controls": ["recalibrate"]},
    "climate_sensor_03": {"controls": ["recalibrate"]},
    "fume_extraction_01": {"controls": ["turn_on", "turn_off", "reset_alarm"]},
    "supply_ventilation_01": {"controls": ["turn_on", "turn_off", "reset_alarm", "set_speed", "set_mode"]},
    "edge_compute_01": {"controls": ["restart_service", "limit_load", "sync_time", "reboot"]},
    "dome_automation_01": {
        "controls": ["set_data_trust", "set_primary_sensor", "reset_interlock", "block_interlock", "unblock_interlock",
                     "acknowledge", "set_ventilation_auto", "set_diesel_autostart"],
        "aliases": {"reset_interlock": ["release_interlock"]},
    },
    "smoke_detector_01": {"controls": ["reset_alarm"]},
    "smoke_detector_02": {"controls": ["reset_alarm"]},
    "fire_suppression_01": {"controls": ["manual_start_zone", "stop", "reset_alarm", "block_automation", "unblock_automation"]},
    "access_control_01": {"controls": ["lock_door", "unlock_door", "lockdown", "reset_alarm"]},
    "meeting_01": {"controls": ["post_message"], "queries": ["read_messages"]},
}


def catalog_message() -> dict[str, Any]:
    """Ответ ``describe_nodes_response`` для тестов."""
    nodes = []
    for node_id, spec in CATALOG_SPEC.items():
        nodes.append(
            {
                "node_id": node_id,
                "title": node_id,
                "controls": spec.get("controls", []),
                "control_aliases": {c: spec.get("aliases", {}).get(c, []) for c in spec.get("controls", [])},
                "operator_controls": spec.get("operator_controls", []),
                "queries": spec.get("queries", []),
                "service_actions": spec.get("service_actions", []),
            }
        )
    return {"type": "describe_nodes_response", "request_id": "1", "success": True, "nodes": nodes}


def catalog() -> Catalog:
    return Catalog.from_response(catalog_message())


def snapshot(name: str = "quiet") -> dict[str, Any]:
    """Копия снимка телеметрии (сообщение ``telemetry``) из журнала: quiet, co2_pending, co2_tripped, co2_after, printer_done."""
    if not _SNAPSHOTS:
        _SNAPSHOTS.update(json.loads((FIXTURES / "dome_snapshots.json").read_text(encoding="utf-8")))
    return copy.deepcopy(_SNAPSHOTS[name])


def state_from(name: str = "quiet", wall: float = 1000.0, **overrides: Any) -> DomeState:
    """Состояние из снимка; ``overrides`` — ``{"путь": значение}`` для правок (например ``"cnc_01.state": "IDLE"``)."""
    msg = snapshot(name)
    st = DomeState()
    st.update(msg["data"], "team", wall)
    for path, value in overrides.items():
        set_path(st, path, value)
    return st


def synchronous_snapshot(name: str = "quiet") -> dict[str, Any]:
    """Synthetic simultaneous measurements on a COPY; original history stays unchanged."""
    msg = snapshot(name)
    for node in ("climate_sensor_01", "climate_sensor_02", "climate_sensor_03"):
        msg["data"]["nodes"][node]["reading_delay_s"] = 0
    return msg


def synchronous_state_from(name: str = "quiet", wall: float = 1000.0, **overrides: Any) -> DomeState:
    st = DomeState()
    st.update(synchronous_snapshot(name)["data"], "team", wall)
    for path, value in overrides.items():
        set_path(st, path, value)
    return st


def synchronous_seq_state(*names: str, dt: float = 2.0, **overrides: Any) -> DomeState:
    st = DomeState()
    for i, name in enumerate(names):
        st.update(synchronous_snapshot(name)["data"], "team", 1000 + i * dt)
    for path, value in overrides.items():
        set_path(st, path, value)
    return st


def set_path(st: DomeState, path: str, value: Any) -> None:
    """Записать значение по пути в состояние (для сценариев тестов)."""
    from kupol.dome.state import split_path

    parts = split_path(path)
    current: Any = st.nodes
    for part in parts[:-1]:
        current = current[part]
    current[parts[-1]] = value


def make_ctx(st: DomeState, tmp_path: "Path", now: float = 1000.0, mem: dict[str, Any] | None = None, texts: list[str] | None = None) -> Any:
    """Контекст плейбука с настоящим Gate (режим «только чтение») для тестов правил."""
    from kupol.dome.agentlog import AgentLog
    from kupol.dome.config import load_dome_config
    from kupol.dome.gate import Gate
    from kupol.dome.playbooks.base import Ctx

    cfg = load_dome_config(Path(__file__).resolve().parents[1] / "dome.yaml")
    cat = catalog()
    log = AgentLog(tmp_path, run_id=f"ctx{id(st)}")

    async def send(node: str, action: str, value: Any) -> dict[str, Any]:
        return {"success": True}

    gate = Gate(cfg, st, lambda: cat, log, send, can_control=False, clock=lambda: now)
    return Ctx(st, cfg, gate, mem if mem is not None else {}, now, lambda since: list(texts or []))


def seq_state(*names: str, dt: float = 2.0, **overrides: Any) -> DomeState:
    """Состояние после нескольких снимков подряд (даёт историю для скоростей)."""
    st = DomeState()
    for i, n in enumerate(names):
        st.update(snapshot(n)["data"], "team", 1000.0 + i * dt)
    for path, value in overrides.items():
        set_path(st, path, value)
    return st
