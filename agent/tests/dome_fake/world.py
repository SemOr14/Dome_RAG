"""Мини-модель купола с правилами сервера из ``01_API.md`` §7 для сценарных тестов.

Время — симулированное (``World.t``): рантайм получает его как часы, поэтому кулдауны и таймауты
проверки эффекта проходят быстро. Модель умеет ровно то, что нужно сценариям: интерлоки ``IL_CO2``
и ``IL_CONDENSATE`` с отсчётом 60 с, удержание станков, ``resume`` (три пустых → ALARM), запуск
заданий, дребезг линий, ошибку инвертора E09, замок бокса, чат.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from tests.dome_helpers import snapshot

MACHINES = ("printer_3d_01", "cnc_01")
UNTRUSTED = ("UNRELIABLE", "FAILED")


class World:
    """Состояние и правила."""

    def __init__(self, base: str = "quiet", job_duration_s: float = 600.0, phantom: bool = False) -> None:
        data = snapshot(base)["data"]
        self.nodes: dict[str, dict[str, Any]] = copy.deepcopy(data["nodes"])
        self.t = 1_000_000.0
        self.hour = 10.0
        self.job_duration_s = job_duration_s
        self.commands: list[dict[str, Any]] = []
        self.posted: list[str] = []
        self.chat: list[dict[str, Any]] = []
        self.script: list[tuple[float, Callable[[World], None]]] = []
        self._line_log: dict[int, list[float]] = {}
        self._msg_id = 0
        self.webhook_lies = False
        if not phantom:  # в журнале на линии 8 с ночи висит ≈52 Вт; для остальных сценариев убираем
            self.nodes["smart_panel_01"]["lines"][7]["power_w"] = 0.0
        for m in MACHINES:
            self.nodes[m].setdefault("resume_attempts", 0)
        if base == "quiet":
            p = self.nodes["printer_3d_01"]
            p.update({"state": "IDLE", "bed_clear": True, "progress_pct": 0.0, "hold_level": None, "hold_reason": None, "paused": False, "homed": True})
            c = self.nodes["cnc_01"]
            c.update({"state": "IDLE", "progress_pct": 0.0, "hold_level": None, "hold_reason": None, "paused": False, "homed": True})

    # ------------------------------------------------------------------ время и телеметрия

    def clock(self) -> float:
        return self.t

    def at(self, seconds: float, fn: Callable[[World], None]) -> None:
        """Выполнить ``fn`` через ``seconds`` симулированных секунд от начала."""
        self.script.append((self.t + seconds, fn))

    def telemetry(self) -> dict[str, Any]:
        self.advance(2.0)
        nodes = copy.deepcopy(self.nodes)
        nodes["meeting_01"] = {"online": True, "message_count": len(self.chat), "recent_messages": self.chat[-10:], "updated_at": self.t, "status": "OK"}
        return {"nodes": nodes, "game_time": {"day": 3, "hour": self.hour}}

    def advance(self, dt: float) -> None:
        self.t += dt
        self.hour = (self.hour + dt / 3600.0) % 24
        for node in self.nodes.values():
            node["updated_at"] = self.t
        for item in [s for s in self.script if s[0] <= self.t]:
            self.script.remove(item)
            item[1](self)
        self._interlocks(dt)
        self._jobs(dt)

    # ------------------------------------------------------------------ физика и автоматика

    def _auto(self) -> dict[str, Any]:
        return self.nodes["dome_automation_01"]

    def _trusted(self, node: str, param: str) -> bool:
        return str(self._auto().get("data_trust", {}).get(f"{node}.{param}", "")).upper() not in UNTRUSTED

    def _pause_all(self, reason: str) -> None:
        for m in MACHINES:
            n = self.nodes[m]
            if n["state"] in ("RUNNING", "PRINTING"):
                n["_prev"] = n["state"]
                n.update({"state": "PAUSED", "paused": True, "hold_level": "WARNING", "hold_reason": reason})

    def _interlocks(self, dt: float) -> None:
        il = self._auto()["interlocks"]
        primary = self._auto().get("primary_co2_sensor", "climate_sensor_01")
        co2 = self.nodes[primary].get("co2_ppm") or 0
        co2_il = il["IL_CO2"]
        if co2_il["state"] == "ARMED":
            if self._trusted(primary, "co2_ppm") and co2 > 2000:
                co2_il["pending_s"] += dt
                co2_il["state_detail"] = "PENDING"
            else:
                co2_il["pending_s"] = 0.0
                co2_il.pop("state_detail", None)
            if co2_il["pending_s"] >= 60:
                co2_il.update({"state": "TRIPPED", "trips": co2_il.get("trips", 0) + 1})
                self._pause_all("IL_CO2")
                self._auto()["events"].append({"event_id": f"AUT-{len(self._auto()['events']) + 1:04d}", "interlock": "IL_CO2", "type": "TRIP", "acknowledged": False})
        cond = il["IL_CONDENSATE"]
        if cond["state"] == "ARMED":
            hot = [s for s in ("climate_sensor_01", "climate_sensor_02", "climate_sensor_03") if self._trusted(s, "humidity_pct") and (self.nodes[s].get("humidity_pct") or 0) > 90]
            cond["pending_s"] = cond["pending_s"] + dt if hot else 0.0
            if cond["pending_s"] >= 60:
                cond["state"] = "TRIPPED"
                self._pause_all("IL_CONDENSATE")

    def _jobs(self, dt: float) -> None:
        for m in MACHINES:
            n = self.nodes[m]
            if n["state"] in ("RUNNING", "PRINTING"):
                n["progress_pct"] = min(100.0, n["progress_pct"] + dt / self.job_duration_s * 100.0)
                if m == "printer_3d_01":
                    n["print_time_left_s"] = max(0.0, (100 - n["progress_pct"]) / 100 * self.job_duration_s)
                    n["real_state"] = "PRINTING"
                    n["webhook_state"] = "COMPLETED" if self.webhook_lies else "PRINTING"
                if n["progress_pct"] >= 100.0:
                    n["state"] = "COMPLETED" if m == "printer_3d_01" else "IDLE"
                    if m == "printer_3d_01":
                        n.update({"bed_clear": False, "real_state": "COMPLETED", "webhook_state": "COMPLETED", "bed_temp_c": 30.0})
            if n.get("lockout_remaining_s"):
                n["lockout_remaining_s"] = max(0.0, n["lockout_remaining_s"] - dt)

    # ------------------------------------------------------------------ команды

    def control(self, msg: dict[str, Any]) -> dict[str, Any]:
        self.commands.append(msg)
        node, action, value = msg.get("node_id"), str(msg.get("action", "")).lower(), msg.get("value")
        handler = getattr(self, f"_do_{node}", None)
        if node == "meeting_01":
            return self._chat_post(value)
        if handler is None:
            return {"success": True, "mode": "virtual"}
        return handler(action, value) or {"success": True, "mode": "virtual"}

    def _chat_post(self, value: Any) -> dict[str, Any]:
        text = value.get("text") if isinstance(value, dict) else str(value)
        self.posted.append(str(text))
        self._msg_id += 1
        self.chat.append({"id": self._msg_id, "time": self.t, "sender": {"role": "team", "team": 1, "name": "agent"}, "to": "operator", "text": str(text)})
        return {"success": True}

    def operator_says(self, text: str | None) -> dict[str, Any] | None:
        """Сообщение Оператора в чат (``None`` — только служебная отметка)."""
        if text is None:
            return None
        self._msg_id += 1
        m = {"id": self._msg_id, "time": self.t, "sender": {"role": "operator", "team": None, "name": "Оператор"}, "to": "agent", "text": text}
        self.chat.append(m)
        return m

    def _do_dome_automation_01(self, action: str, value: Any) -> dict[str, Any] | None:
        auto = self._auto()
        if action == "set_data_trust":
            key = f"{value['node']}.{value['param']}"
            if value["trust"] == "TRUSTED":
                auto["data_trust"].pop(key, None)
            else:
                auto["data_trust"][key] = value["trust"]
        elif action == "set_primary_sensor":
            auto["primary_co2_sensor"] = value
        elif action in ("reset_interlock", "release_interlock"):
            il = auto["interlocks"][value]
            il.update({"state": "ARMED", "pending_s": 0.0})
        elif action == "acknowledge":
            for ev in auto["events"]:
                if value in ("all", ev.get("event_id")) or (isinstance(value, dict) and value.get("event_id") == ev.get("event_id")):
                    ev["acknowledged"] = True
        return None

    def _do_machine(self, node: str, action: str, value: Any) -> dict[str, Any] | None:
        n = self.nodes[node]
        if n.get("lockout_remaining_s"):
            return {"success": False, "error": "Станок заблокирован (ALARM)"}
        if action == "resume":
            reason = n.get("hold_reason")
            if reason is None and n["state"] == "PAUSED":
                n.update({"state": n.pop("_prev", "RUNNING"), "paused": False})
            elif reason is not None:
                cleared = self._auto()["interlocks"].get(reason, {}).get("state") == "ARMED" and self._cause_gone(reason)
                if cleared:
                    n.update({"state": n.pop("_prev", "RUNNING"), "paused": False, "hold_level": None, "hold_reason": None, "resume_attempts": 0})
                else:
                    n["resume_attempts"] += 1
                    if n["resume_attempts"] >= 3:
                        n.update({"hold_level": "ALARM", "lockout_remaining_s": 1200.0})
        elif action == "start_job":
            name = value.get("file_name") if isinstance(value, dict) else value
            if name not in n.get("files", []):
                return {"success": False, "error": "Файл не найден"}
            if n["state"] not in ("IDLE", "COMPLETED"):
                return {"success": False, "error": "Станок занят"}
            if node == "printer_3d_01" and not n.get("bed_clear"):
                n.update({"hold_level": "ALARM", "hold_reason": "BED_COLLISION", "lockout_remaining_s": 1200.0, "state": "ERROR"})
                return None
            n.update({"state": "PRINTING" if node == "printer_3d_01" else "RUNNING", "file_name": name, "progress_pct": 0.0, "paused": False})
            if node == "printer_3d_01":
                n.update({"real_state": "PRINTING", "webhook_state": "PRINTING", "bed_temp_c": 60.0, "print_time_left_s": self.job_duration_s})
        return None

    def _cause_gone(self, reason: str) -> bool:
        if reason == "IL_CO2":
            primary = self._auto().get("primary_co2_sensor")
            return not self._trusted(primary, "co2_ppm") or (self.nodes[primary].get("co2_ppm") or 0) < 2000
        return True

    def _do_printer_3d_01(self, action: str, value: Any) -> dict[str, Any] | None:
        return self._do_machine("printer_3d_01", action, value)

    def _do_cnc_01(self, action: str, value: Any) -> dict[str, Any] | None:
        return self._do_machine("cnc_01", action, value)

    def _do_smart_panel_01(self, action: str, value: Any) -> dict[str, Any] | None:
        n = value.get("line") if isinstance(value, dict) else value
        line = next(x for x in self.nodes["smart_panel_01"]["lines"] if x["line"] == n)
        if line.get("lockout_remaining_s", 0) > 0:
            return {"success": False, "error": "Линия заблокирована"}
        times = [x for x in self._line_log.setdefault(n, []) if self.t - x < 120] + [self.t]
        self._line_log[n] = times
        if len(times) > 3:
            line.update({"lockout_remaining_s": 300.0, "lockout_reason": "CHATTER"})
            return {"success": False, "error": "Слишком частые переключения"}
        if action == "line_off":
            line.update({"state": "OFF", "power_w": 0.0, "current_a": 0.0})
        elif action == "line_on":
            line.update({"state": "ON", "power_w": 20.0})
        return None

    def _do_solar_inverter_01(self, action: str, value: Any) -> dict[str, Any] | None:
        inv = self.nodes["solar_inverter_01"]
        if action == "set_mode_grid_only":
            inv["mode"] = "GRID_ONLY"
        elif action == "set_mode_hybrid":
            inv["mode"] = "HYBRID"
        elif action == "reset_error":
            if self.nodes["weather_station_01"]["humidity_pct"] > 80:
                return None  # ошибка вернётся
            inv.update({"error": False, "error_code": None, "error_text": None, "mppt_state": "TRACKING"})
        return None

    def _do_thermal_insulation_01(self, action: str, value: Any) -> dict[str, Any] | None:
        if action == "set_lock":
            box = self.nodes["thermal_insulation_01"]
            box["locks"][value["lock"]] = "LOCKED" if value["locked"] else "UNLOCKED"
        return None

    def _do_smoke_detector_01(self, action: str, value: Any) -> dict[str, Any] | None:
        if action == "reset_alarm":
            self.nodes["smoke_detector_01"].update({"smoke_detected": False, "alarm_state": "NORMAL"})
        return None
