"""Ожидаемый эффект команды по узлу и действию: для команд модели и ручных команд Оператора.

Проверку по телеметрии выполняет Gate (``verification`` в логе). Эффекты повторяют плейбуки.
"""

from __future__ import annotations

from typing import Any

from kupol.dome.gate import Expect
from kupol.dome.playbooks.base import trust_step
from kupol.dome.playbooks.machines import working
from kupol.dome.state import DomeState


def _interlock(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for k in ("interlock", "id", "name"):
            if isinstance(value.get(k), str):
                return str(value[k])
    return None


def _line(value: Any) -> int | None:
    value = value.get("line") if isinstance(value, dict) else value
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def expect_for(node: str, action: str, value: Any) -> Expect:
    """Проверяемый эффект команды (как в плейбуках); для неописанных действий — узел на связи после команды."""
    a = action.lower()
    if a == "set_data_trust" and isinstance(value, dict) and {"node", "param", "trust"} <= value.keys():
        return trust_step(str(value["node"]), str(value["param"]), str(value["trust"])).expect
    if a == "set_primary_sensor" and isinstance(value, str):
        return Expect.equals("dome_automation_01.primary_co2_sensor", value)
    if a in ("reset_interlock", "block_interlock") and (il := _interlock(value)):
        return Expect.equals(f"dome_automation_01.interlocks.{il}.state", "ARMED" if a == "reset_interlock" else "BLOCKED")
    if a in ("line_on", "line_off", "reset_protection") and (n := _line(value)):
        return Expect.equals(f"smart_panel_01.lines[{n - 1}].state", "OFF" if a == "line_off" else "ON")
    if a == "resume":
        return working(node)
    if a == "home":
        return Expect.equals(f"{node}.homed", True, timeout_s=90.0)
    if a == "start_job":
        name = value.get("file_name") if isinstance(value, dict) else value

        def running(st: DomeState) -> bool:
            return str(st.get(f"{node}.state", "")).upper() in ("RUNNING", "PRINTING", "HOMING", "BUSY") and (not name or st.get(f"{node}.file_name") == name)

        return Expect.custom(f"{node} работает над {name}", running, [f"{node}.state", f"{node}.file_name"], timeout_s=60.0)
    if a == "turn_on":
        return Expect.custom(f"{node} включён", lambda s: s.get(f"{node}.state") == "ON" or s.get(f"{node}.enabled") is True, [f"{node}.state", f"{node}.enabled"])
    if a == "reset_alarm":
        return Expect.custom(f"{node}: авария сброшена", lambda s: s.get(f"{node}.alarm") in (False, None, "NONE", "NORMAL"), [f"{node}.alarm", f"{node}.alarm_state"])
    if a in ("set_mode_grid_only", "set_mode_hybrid"):
        return Expect.equals(f"{node}.mode", "GRID_ONLY" if a == "set_mode_grid_only" else "HYBRID")
    if a in ("set_speed", "set_mode") and value is not None and not isinstance(value, dict):
        return Expect.equals(f"{node}.{'speed_pct' if a == 'set_speed' else 'mode'}", value)
    return Expect.custom(
        f"{node}: на связи и обновляется после {action} (конкретный эффект не описан)", lambda s: node in s.nodes and not s.is_frozen(node), [f"{node}.status", f"{node}.updated_at"]
    )
