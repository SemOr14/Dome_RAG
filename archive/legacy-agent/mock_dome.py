"""Локальный тренажёр протокола: поднимает WS-сервер купола и проигрывает сценарий.

Запуск: python mock_dome.py
Затем:  DOME_URL=ws://127.0.0.1:8765 DOME_KEY=test python main.py
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import websockets
from websockets.asyncio.server import serve

KEY = os.getenv("MOCK_KEY", "test")
PORT = int(os.getenv("MOCK_PORT", "8765"))

LINES = [
    ("Мастерская + вытяжка", 5.0), ("Теплица ФабЛаб", 1.5), ("3D-принтер", 5.0), ("ЧПУ-фрезер", 5.0),
    ("Теплица Химлаб", 1.5), ("Освещение", 3.0), ("Аварийное освещение", 3.0), ("Резерв", 6.0),
]


class MockDome:
    def __init__(self) -> None:
        self.nodes: dict = {}
        self.clients: set = set()
        self.t0 = time.time()
        self.injected = False
        self.resume_attempts = {"printer_3d_01": 0, "cnc_01": 0}
        self.queue: list[dict] = []
        self.sent: dict[str, float] = {}
        self.msg_id = 0
        self._init_nodes()

    def _init_nodes(self) -> None:
        self.nodes["smart_panel_01"] = {
            "status": "OK", "online": True, "bus_voltage_v": 228.0, "bus_powered": True,
            "lines": [{"line": i + 1, "name": name, "state": "ON" if i < 6 else "OFF",
                       "voltage_v": 228.0, "current_a": 0.4, "power_w": 90.0 if i == 0 else 10.0,
                       "protection_state": "ARMED", "lockout_remaining_s": 0, "leakage_ma": 2.0,
                       "alarm": None, "alarm_code": None, "lockout_reason": None,
                       "rated_current_a": rated} for i, (name, rated) in enumerate(LINES)],
            "total_power_w": 240.0, "total_current_a": 1.1, "lines_on_count": 6,
        }
        self.nodes["printer_3d_01"] = {
            "status": "OK", "online": True, "state": "IDLE", "paused": False, "homed": True,
            "file_name": None, "progress_pct": 0.0, "bed_clear": True, "hold_level": None,
            "hold_reason": None, "resume_attempts": 0, "lockout_remaining_s": 0,
            "nozzle_temp_c": 210.0, "bed_temp_c": 60.0, "print_time_left_s": 0,
            "files": [{"name": "housing.gcode", "size_bytes": 120000, "estimated_time_s": 2400}],
        }
        self.nodes["cnc_01"] = {
            "status": "OK", "online": True, "state": "IDLE", "paused": False, "homed": True,
            "file_name": None, "progress_pct": 0.0, "hold_level": None, "hold_reason": None,
            "resume_attempts": 0, "lockout_remaining_s": 0, "spindle_rpm": 0, "alarm": None,
            "files": [{"name": "pcb_power_board.nc", "size_bytes": 80000, "valid": True}],
        }
        for i in (1, 2, 3):
            self.nodes[f"climate_sensor_{i:02d}"] = {
                "status": "OK", "sensor_state": "OK", "confidence": 0.95,
                "temperature_c": 24.0, "humidity_pct": 45.0, "co2_ppm": 780.0, "dew_point_c": 12.0,
            }
        self.nodes["dome_automation_01"] = {
            "status": "OK", "mode": "AUTO", "primary_co2_sensor": "climate_sensor_01",
            "autonomy_forecast_h": 9.0, "ventilation_auto": True, "diesel_autostart": True,
            "interlocks": {name: {"state": "ARMED", "trips": 0, "title": name}
                           for name in ("IL_CO2", "IL_CONDENSATE", "IL_SMOKE", "IL_LOAD_SHED",
                                        "IL_STORM", "IL_OVERVOLT", "IL_INV_OVERLOAD", "IL_HEAT",
                                        "IL_OVERCURRENT")},
            "data_trust": {}, "events": [],
        }
        self.nodes["supply_ventilation_01"] = {"status": "OK", "state": "ON", "speed_pct": 55.0,
                                               "mode": "MIXED", "airflow_m3_h": 120.0, "power_w": 30.0}
        self.nodes["battery_01"] = {"status": "OK", "soc_pct": 78.0, "state": "CHARGING", "voltage_v": 52.0}
        self.nodes["solar_inverter_01"] = {"status": "OK", "battery_soc_pct": 78.0, "power_deficit_w": 0.0,
                                           "mode": "HYBRID", "load_power_w": 240.0, "grid_state": "AVAILABLE"}
        self.nodes["solar_panels_01"] = {"status": "OK", "power_w": 640.0}
        self.nodes["dizel_1"] = {"status": "OK", "state": "OFF", "power_w": 0.0, "fuel_consumption_l_h": 0.0}
        self.nodes["fuel_tank_01"] = {"status": "OK", "level_pct": 72.0, "volume_l": 140.0}
        self.nodes["thermal_insulation_01"] = {"status": "OK", "locks": ["LOCKED", "LOCKED"],
                                               "doors": ["CLOSED", "CLOSED"], "fans_pwm": [0] * 6,
                                               "temperature_c": [24.0, 24.0, 12.0]}
        self.nodes["fume_extraction_01"] = {"status": "OK", "enabled": True, "power_w": 45.0, "fan_rpm": 1200}
        self.nodes["smoke_detector_01"] = {"status": "OK", "zone": "FABLAB", "smoke_detected": False,
                                           "obscuration_pct": 2.0, "false_alarm_suspected": False}
        self.nodes["smoke_detector_02"] = {"status": "OK", "zone": "STORAGE", "smoke_detected": False,
                                           "obscuration_pct": 2.0, "false_alarm_suspected": False}
        self.nodes["network_link_01"] = {"status": "OK", "link_state": "OK", "packet_loss_pct": 0.0}
        self.nodes["backup_comms_01"] = {"status": "OK", "available": True, "active_channel": "SATELLITE"}
        self.nodes["meeting_01"] = {"status": "OK", "online": True, "message_count": 0,
                                    "last_message_id": 0, "recent_messages": []}

    # ------------------------------------------------------------------ сценарий
    def tick(self, dt: float) -> None:
        t = time.time() - self.t0
        pr = self.nodes["printer_3d_01"]
        if pr["state"] == "PRINTING":
            pr["progress_pct"] = min(100.0, pr["progress_pct"] + dt * 1.2)
            if pr["progress_pct"] >= 100:
                pr["state"] = "COMPLETED"
                pr["bed_clear"] = False
        if not self.injected and t > 18:
            self.injected = True
            self.nodes["climate_sensor_01"]["co2_ppm"] = 3041.0
            self.nodes["dome_automation_01"]["interlocks"]["IL_CO2"]["state"] = "TRIPPED"
            for nid in ("printer_3d_01", "cnc_01"):
                self.nodes[nid]["hold_level"] = "WARNING"
                self.nodes[nid]["hold_reason"] = "IL_CO2"
                if nid == "printer_3d_01":
                    self.nodes[nid]["state"] = "PAUSED"
                    pr["paused"] = True
                else:
                    self.nodes[nid]["state"] = "PAUSED"
            self.nodes["supply_ventilation_01"]["speed_pct"] = 100.0

    def operator(self) -> None:
        """Сценарий Оператора: выдаёт задание и забирает деталь."""
        t = time.time() - self.t0
        pr = self.nodes["printer_3d_01"]
        if t > 6 and "task" not in self.sent:
            self.sent["task"] = t
            self.say("Оператор", "Задание на смену: напечатать корпус housing.gcode, "
                                 "затем отфрезеровать плату pcb_power_board.nc")
        if (pr["state"] == "COMPLETED" and pr["bed_clear"] is False
                and self.nodes["thermal_insulation_01"]["locks"][0] == "UNLOCKED"
                and t - self.sent.get("part", 0) > 6):
            self.sent["part"] = t
            pr["bed_clear"] = True
            self.nodes["thermal_insulation_01"]["locks"][0] = "LOCKED"
            self.say("Оператор", "Деталь забрал, бокс закрыл.")

    def say(self, name: str, text: str) -> None:
        self.msg_id += 1
        message = {"id": self.msg_id, "time": time.time(),
                   "sender": {"role": "operator", "team": None, "name": name},
                   "to": None, "text": text}
        self.nodes["meeting_01"]["message_count"] += 1
        self.nodes["meeting_01"]["last_message_id"] = self.msg_id
        self.nodes["meeting_01"]["recent_messages"] = (
            self.nodes["meeting_01"].get("recent_messages") or [])[-9:] + [message]
        self.queue.append(message)

    def telemetry(self) -> dict:
        nodes = json.loads(json.dumps(self.nodes))
        for node in nodes.values():
            node["updated_at"] = time.time()
        return {"type": "telemetry", "timestamp": time.time(), "role": "team",
                "data": {"nodes": nodes, "game_time": {"day": 0, "hour": 14.0 + (time.time() - self.t0) / 3600}}}

    # ------------------------------------------------------------------ команды
    def control(self, node_id: str, action: str, value) -> dict:
        a = (action or "").lower()
        if node_id == "printer_3d_01" and a in ("start_job", "start_print"):
            pr = self.nodes[node_id]
            if pr["state"] != "IDLE":
                return {"success": False, "error": "станок занят"}
            if not pr["bed_clear"]:
                return {"success": False, "error": "стол занят"}
            pr.update(state="PRINTING", file_name=value if isinstance(value, str) else value.get("file_name"),
                      progress_pct=0.0)
            return {"success": True, "mode": "virtual"}
        if node_id in ("printer_3d_01", "cnc_01") and a == "resume":
            d = self.nodes[node_id]
            if not d.get("hold_level"):
                return {"success": True, "mode": "virtual"}
            if self.nodes["dome_automation_01"]["interlocks"]["IL_CO2"]["state"] == "TRIPPED":
                self.resume_attempts[node_id] += 1
                d["resume_attempts"] = self.resume_attempts[node_id]
                if self.resume_attempts[node_id] >= 3:
                    d["hold_level"] = "ALARM"
                    d["lockout_remaining_s"] = 1200
                return {"success": True, "mode": "virtual"}
            resumed = "PRINTING" if (node_id == "printer_3d_01" and d.get("file_name")) else (
                "RUNNING" if node_id == "cnc_01" and d.get("file_name") else "IDLE")
            d.update(hold_level=None, hold_reason=None, paused=False, state=resumed)
            d["resume_attempts"] = 0
            return {"success": True, "mode": "virtual"}
        if node_id == "dome_automation_01":
            auto = self.nodes[node_id]
            if a == "set_data_trust":
                v = value or {}
                auto["data_trust"][f"{v.get('node')}.{v.get('param')}"] = v.get("trust")
                return {"success": True, "mode": "virtual"}
            if a == "set_primary_sensor":
                auto["primary_co2_sensor"] = value
                auto["data_trust"][f"{value}.co2_ppm"] = "TRUSTED"
                return {"success": True, "mode": "virtual"}
            if a == "reset_interlock":
                name = value if isinstance(value, str) else (value or {}).get("id")
                if name in auto["interlocks"]:
                    auto["interlocks"][name]["state"] = "ARMED"
                auto["events"].append({"event_id": f"AUT-{len(auto['events'])+1:04d}", "interlock": name,
                                       "type": "CLEAR", "acknowledged": True})
                return {"success": True, "mode": "virtual"}
            if a == "acknowledge":
                for event in auto["events"]:
                    event["acknowledged"] = True
                return {"success": True, "mode": "virtual"}
        if node_id == "smart_panel_01":
            lines = self.nodes[node_id]["lines"]
            if a == "reset_protection":
                idx = int(value) - 1
                lines[idx]["state"] = "ON"
                lines[idx]["protection_state"] = "ARMED"
                return {"success": True, "mode": "virtual"}
            if a in ("line_on", "line_off"):
                idx = int(value) - 1
                lines[idx]["state"] = "ON" if a == "line_on" else "OFF"
                return {"success": True, "mode": "virtual"}
        if node_id == "supply_ventilation_01":
            if a == "set_speed":
                self.nodes[node_id]["speed_pct"] = float(value)
                return {"success": True, "mode": "virtual"}
            if a == "set_mode":
                self.nodes[node_id]["mode"] = value
                return {"success": True, "mode": "virtual"}
        if node_id == "dizel_1":
            if a == "turn_on":
                self.nodes[node_id].update(state="ON", power_w=2200.0)
                return {"success": True, "mode": "virtual"}
            if a == "turn_off":
                self.nodes[node_id].update(state="OFF", power_w=0.0)
                return {"success": True, "mode": "virtual"}
            if a == "preheat":
                return {"success": True, "mode": "virtual"}
        if node_id == "thermal_insulation_01" and a == "set_lock":
            v = value or {}
            idx = int(v.get("lock", 0))
            self.nodes[node_id]["locks"][idx] = "UNLOCKED" if v.get("locked") is False else "LOCKED"
            return {"success": True, "mode": "virtual"}
        if node_id == "meeting_01" and a in ("post_message", "post"):
            text = value.get("text") if isinstance(value, dict) else str(value)
            meeting = self.nodes[node_id]
            meeting["message_count"] += 1
            meeting["last_message_id"] += 1
            print(f"[chat] -> {text}")
            return {"success": True, "mode": "virtual",
                    "message": {"id": meeting["last_message_id"], "text": text,
                                "sender": {"role": "team", "team": 1, "name": "agent"}}}
        if a == "reconnect":
            return {"success": True, "mode": "virtual"}
        # прочие действия считаем принятыми
        return {"success": True, "mode": "virtual"}


DOME = MockDome()


async def handler(ws) -> None:
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        auth = json.loads(raw)
        if auth.get("type") != "auth" or auth.get("key") != KEY:
            await ws.send(json.dumps({"type": "auth_response", "success": False, "error": "Неверный ключ доступа"}))
            await ws.close(4401)
            return
        await ws.send(json.dumps({"type": "auth_response", "success": True, "role": "team", "team": 1}))
        DOME.clients.add(ws)
        async for raw in ws:
            msg = json.loads(raw)
            kind = msg.get("type")
            rid = msg.get("request_id")
            if kind == "ping":
                await ws.send(json.dumps({"type": "ping_response", "request_id": rid, "success": True}))
            elif kind == "describe_nodes":
                nodes = [{"node_id": nid, "controls": ["start_job", "resume", "home", "set_speed", "turn_on",
                                                      "turn_off", "reset_protection", "set_data_trust",
                                                      "set_primary_sensor", "reset_interlock", "acknowledge",
                                                      "set_lock", "reconnect", "preheat", "set_mode"],
                          "control_aliases": {}, "queries": ["list_files"], "service_actions": ["reconnect"]}
                         for nid in DOME.nodes if nid != "meeting_01"]
                await ws.send(json.dumps({"type": "describe_nodes_response", "request_id": rid,
                                          "success": True, "nodes": nodes}))
            elif kind == "control":
                resp = DOME.control(msg.get("node_id"), msg.get("action"), msg.get("value"))
                await ws.send(json.dumps({"type": "control_response", "request_id": rid,
                                          "node_id": msg.get("node_id"), "action": msg.get("action"), **resp}))
            elif kind == "query":
                result = DOME.nodes.get(msg.get("node_id"), {}).get("files", [])
                await ws.send(json.dumps({"type": "query_response", "request_id": rid, "success": True,
                                          "node_id": msg.get("node_id"), "query": msg.get("query"),
                                          "result": result}))
    except Exception:  # noqa: BLE001
        pass
    finally:
        DOME.clients.discard(ws)


async def broadcaster() -> None:
    last = time.time()
    while True:
        await asyncio.sleep(2.0)
        now = time.time()
        DOME.tick(now - last)
        DOME.operator()
        last = now
        if DOME.clients:
            payload = json.dumps(DOME.telemetry())
            for ws in list(DOME.clients):
                try:
                    await ws.send(payload)
                except Exception:  # noqa: BLE001
                    DOME.clients.discard(ws)
            while DOME.queue:
                message = DOME.queue.pop(0)
                note = json.dumps({"type": "meeting_message", "message": message})
                for ws in list(DOME.clients):
                    try:
                        await ws.send(note)
                    except Exception:  # noqa: BLE001
                        DOME.clients.discard(ws)


async def main() -> None:
    async with serve(handler, "127.0.0.1", PORT, max_size=50_000_000):
        print(f"mock dome on ws://127.0.0.1:{PORT} key={KEY}")
        await broadcaster()


if __name__ == "__main__":
    asyncio.run(main())
