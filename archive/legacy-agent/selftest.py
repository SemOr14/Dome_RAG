"""Самопроверка правил на синтетической телеметрии: python selftest.py"""
from __future__ import annotations

import time

from model import Snap
from rules import Rules


def base():
    nodes = {
        "smart_panel_01": {"status": "OK", "updated_at": time.time(), "bus_voltage_v": 228.0,
                           "lines": [{"line": i + 1, "name": f"L{i+1}", "state": "ON", "current_a": 1.0,
                                      "power_w": 100.0, "protection_state": "ARMED",
                                      "lockout_remaining_s": 0} for i in range(8)]},
        "printer_3d_01": {"status": "OK", "updated_at": time.time(), "state": "PRINTING", "homed": True,
                          "progress_pct": 40.0, "bed_clear": True, "hold_level": None,
                          "hold_reason": None, "resume_attempts": 0, "lockout_remaining_s": 0},
        "cnc_01": {"status": "OK", "updated_at": time.time(), "state": "IDLE", "homed": True,
                   "file_name": None, "hold_level": None, "hold_reason": None,
                   "resume_attempts": 0, "lockout_remaining_s": 0},
        "climate_sensor_01": {"status": "OK", "updated_at": time.time(), "co2_ppm": 780.0,
                              "temperature_c": 24.0, "humidity_pct": 45.0, "dew_point_c": 12.0},
        "climate_sensor_02": {"status": "OK", "updated_at": time.time(), "co2_ppm": 790.0,
                              "temperature_c": 24.0, "humidity_pct": 45.0, "dew_point_c": 12.0},
        "climate_sensor_03": {"status": "OK", "updated_at": time.time(), "co2_ppm": 800.0,
                              "temperature_c": 24.0, "humidity_pct": 45.0, "dew_point_c": 12.0},
        "dome_automation_01": {"status": "OK", "updated_at": time.time(),
                               "primary_co2_sensor": "climate_sensor_01",
                               "interlocks": {"IL_CO2": {"state": "ARMED"}, "IL_SMOKE": {"state": "ARMED"},
                                              "IL_CONDENSATE": {"state": "ARMED"}},
                               "data_trust": {}, "autonomy_forecast_h": 9.0},
        "supply_ventilation_01": {"status": "OK", "updated_at": time.time(), "state": "ON", "speed_pct": 50.0},
        "battery_01": {"status": "OK", "updated_at": time.time(), "soc_pct": 75.0},
        "solar_inverter_01": {"status": "OK", "updated_at": time.time(), "power_deficit_w": 0.0},
        "dizel_1": {"status": "OK", "updated_at": time.time(), "state": "OFF", "power_w": 0.0},
        "fuel_tank_01": {"status": "OK", "updated_at": time.time(), "level_pct": 60.0},
        "thermal_insulation_01": {"status": "OK", "updated_at": time.time(), "locks": ["LOCKED", "LOCKED"]},
        "smoke_detector_01": {"status": "OK", "updated_at": time.time(), "smoke_detected": False},
        "smoke_detector_02": {"status": "OK", "updated_at": time.time(), "smoke_detected": False},
        "network_link_01": {"status": "OK", "updated_at": time.time(), "link_state": "OK"},
        "backup_comms_01": {"status": "OK", "updated_at": time.time(), "available": True,
                            "active_channel": "SATELLITE"},
    }
    return {"nodes": nodes, "game_time": {"day": 0, "hour": 14.0}}


def snap(**overrides):
    data = base()
    for path, value in overrides.items():
        node, field = path.split("__", 1)
        data["nodes"][node][field] = value
    return Snap(data, time.time())


def find(decisions, prefix):
    return [d for d in decisions if d.key.startswith(prefix)]


failures = []


def check(name, condition):
    print(("  ok   " if condition else "  FAIL ") + name)
    if not condition:
        failures.append(name)


# 1. ложный CO2
s = snap(climate_sensor_01__co2_ppm=3041.0, printer_3d_01__hold_level="WARNING",
         printer_3d_01__hold_reason="IL_CO2")
s.data["nodes"]["dome_automation_01"]["interlocks"]["IL_CO2"]["state"] = "TRIPPED"
d = find(Rules({}).evaluate(s, {}), "hold:printer_3d_01:IL_CO2")
check("ложный CO2 распознан", bool(d) and "ложное показание" in d[0].cause)
check("ложный CO2: есть set_data_trust", any(a.action == "set_data_trust" for a in d[0].actions))
check("ложный CO2: есть resume", any(a.action == "resume" for a in d[0].actions))

# 2. реальный CO2
s = snap(climate_sensor_01__co2_ppm=2600.0, climate_sensor_02__co2_ppm=2400.0,
         climate_sensor_03__co2_ppm=2500.0, printer_3d_01__hold_level="WARNING",
         printer_3d_01__hold_reason="IL_CO2")
d = find(Rules({}).evaluate(s, {}), "hold:printer_3d_01:IL_CO2")
check("реальный CO2 не снимает удержание", bool(d) and not any(a.action == "resume" for a in d[0].actions))
check("реальный CO2 поднимает приток", any(a.action == "set_speed" for a in d[0].actions))

# 3. неизвестная причина — без действий
s = snap(printer_3d_01__hold_level="WARNING", printer_3d_01__hold_reason="HOWEVER")
d = find(Rules({}).evaluate(s, {}), "hold:printer_3d_01")
check("неизвестная причина: нет действий", bool(d) and not d[0].actions)

# 4. защита линии
s = snap()
s.data["nodes"]["smart_panel_01"]["lines"][2]["state"] = "TRIPPED"
d = find(Rules({}).evaluate(s, {}), "line:3")
check("сработавшая защита: reset_protection", bool(d) and d[0].actions[0].action == "reset_protection")

# 5. деталь на столе
s = snap(printer_3d_01__state="COMPLETED", printer_3d_01__bed_clear=False)
d = find(Rules({}).evaluate(s, {}), "bed:part_ready")
check("деталь на столе: открывается замок", bool(d) and d[0].actions[0].action == "set_lock")

# 6. запуск печати по заданию
s = snap(printer_3d_01__state="IDLE")
d = find(Rules({}).evaluate(s, {"task": {"print_file": "housing.gcode"}}), "print:")
check("задание: start_job", bool(d) and any(a.action == "start_job" for a in d[0].actions))

# 7. печать на занятый стол не запускается
s = snap(printer_3d_01__state="IDLE", printer_3d_01__bed_clear=False)
d = find(Rules({}).evaluate(s, {"task": {"print_file": "housing.gcode"}}), "print:")
check("занятый стол: запуска нет", not d)

# 8. дизель при низком SOC
s = snap(battery_01__soc_pct=22.0)
d = find(Rules({}).evaluate(s, {}), "energy:diesel_on")
check("низкий SOC: дизель включается", bool(d) and any(a.action == "turn_on" for a in d[0].actions))

# 9. застарелые данные
s = snap()
s.data["nodes"]["printer_3d_01"]["updated_at"] = time.time() - 120
d = find(Rules({}).evaluate(s, {"stale_after": 30}), "stale:printer_3d_01")
check("застывшие данные: reconnect", bool(d) and d[0].actions[0].action == "reconnect")

print("\nпровалов:", len(failures))
raise SystemExit(1 if failures else 0)
