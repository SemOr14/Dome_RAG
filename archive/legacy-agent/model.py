"""Обёртка над телеметрией: удобный доступ к узлам, полям и признакам старения."""
from __future__ import annotations

import re
import time
from typing import Any

_INDEX = re.compile(r"\[(\d+)\]")


def _walk(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        m = _INDEX.search(part)
        if m:
            name = part[: m.start()]
            if name:
                cur = cur.get(name) if isinstance(cur, dict) else None
            idx = int(m.group(1))
            if isinstance(cur, (list, tuple)) and 0 <= idx < len(cur):
                cur = cur[idx]
            else:
                return None
        else:
            cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


class Snap:
    """Снимок телеметрии с помощниками."""

    def __init__(self, data: dict, wall_ts: float | None = None, ages: dict | None = None) -> None:
        self.data = data or {}
        self.wall_ts = wall_ts or time.time()
        # ages: сколько секунд локального времени узел не менял updated_at
        self.ages = ages or {}

    # ------------------------------------------------------------------ доступ
    @property
    def nodes(self) -> dict:
        return self.data.get("nodes") or {}

    def node(self, node_id: str) -> dict:
        return self.nodes.get(node_id) or {}

    def get(self, node_id: str, path: str, default: Any = None) -> Any:
        value = _walk(self.node(node_id), path)
        return default if value is None else value

    def raw(self, node_id: str, path: str, default: Any = None) -> Any:
        return _walk(self.node(node_id), path) if path else self.node(node_id)

    # ------------------------------------------------------------------ время
    def game_time(self) -> dict:
        return self.data.get("game_time") or {}

    def dome_clock(self) -> str:
        g = self.game_time()
        hour = g.get("hour")
        if hour is None:
            return "--:--"
        h = int(hour) % 24
        m = int(round((hour - int(hour)) * 60)) % 60
        return f"{h:02d}:{m:02d}"

    def age(self, node_id: str, default: float = 0.0) -> float:
        """Возраст данных: с момента, когда узел перестал обновлять updated_at.

        Часы купола и ноутбука могут расходиться, поэтому сравнивать updated_at
        напрямую с локальным временем нельзя.
        """
        if node_id in self.ages:
            return float(self.ages[node_id])
        ts = self.node(node_id).get("updated_at")
        if not isinstance(ts, (int, float)):
            return default
        return max(0.0, self.wall_ts - float(ts))

    def stale(self, node_id: str, max_age: float = 30.0) -> bool:
        n = self.node(node_id)
        if not n:
            return True
        if n.get("status") == "UNKNOWN":
            return True
        return self.age(node_id) > max_age

    # ------------------------------------------------------------------ линии
    def lines(self) -> list:
        return self.get("smart_panel_01", "lines") or []

    def line(self, index: int) -> dict:
        lines = self.lines()
        return lines[index - 1] if 1 <= index <= len(lines) else {}

    # ------------------------------------------------------------------ датчики
    def co2_values(self) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for nid in ("climate_sensor_01", "climate_sensor_02", "climate_sensor_03"):
            value = self.raw(nid, "co2_ppm")
            out[nid] = float(value) if isinstance(value, (int, float)) else None
        return out

    def primary_co2_sensor(self) -> str:
        return self.get("dome_automation_01", "primary_co2_sensor") or "climate_sensor_01"

    def interlock(self, name: str) -> dict:
        return self.get("dome_automation_01", f"interlocks.{name}") or {}

    def interlocks(self) -> dict:
        return self.get("dome_automation_01", "interlocks") or {}

    def soc(self) -> float | None:
        for nid, path in (("battery_01", "soc_pct"), ("solar_inverter_01", "battery_soc_pct")):
            value = self.raw(nid, path)
            if isinstance(value, (int, float)):
                return float(value)
        return None

    def autonomy_h(self) -> float | None:
        value = self.raw("dome_automation_01", "autonomy_forecast_h")
        return float(value) if isinstance(value, (int, float)) else None

    def machine(self, node_id: str) -> dict:
        d = self.node(node_id)
        return {
            "state": d.get("state"),
            "hold": d.get("hold_level"),
            "reason": d.get("hold_reason"),
            "attempts": d.get("resume_attempts") or 0,
            "lockout": d.get("lockout_remaining_s") or 0,
            "progress": d.get("progress_pct"),
            "file": d.get("file_name"),
        }
