"""Сжатие телеметрии для промпта и простые эвристики аномалий.

В модель не отправляется весь снимок (бюджет ключа, 03 §3): по каждому узлу берутся ключевые поля
из каталога, служебные флаги и всё, что выглядит необычно.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

STALE_AFTER_S = 30.0
LINE_FIELDS = ("line", "name", "state", "voltage_v", "current_a", "power_w", "protection_state",
               "alarm_code", "lockout_remaining_s", "lockout_reason")
CLIMATE_SENSORS = ("climate_sensor_01", "climate_sensor_02", "climate_sensor_03")
ALARM_FIELDS = ("alarm", "alarm_code", "error_code", "fault_code", "error")
# null подозрителен только у измерений; у кодов ошибок, причин, зон null — норма
_MEASUREMENT = re.compile(r"(_(c|pct|ppm|w|v|a|ma|ms|bar|lux|hpa|mm|mm_s|kwh|l|l_h|l_min|m3_h|db|dbm|ntu|g|s|h)|rpm|ph)$")


@dataclass
class TelemetryContext:
    nodes: dict[str, dict]
    anomalies: list[dict]
    hot_nodes: set[str] = field(default_factory=set)  # затравка для подграфа влияний
    timestamp: float | None = None


def _anomaly(out: list, hot: set, node: str, param: str | None, value: Any, note: str, extra_hot=()) -> None:
    out.append({"node": node, "param": param, "value": value, "note": note})
    hot.add(node)
    hot.update(extra_hot)


def _drop_noise(d: dict) -> dict:
    """Убирает штатные пустые значения, кроме пропавших измерений (их null важен)."""
    return {k: v for k, v in d.items()
            if not (v is None and not _MEASUREMENT.search(k)) and not (k == "lockout_remaining_s" and not v)}


def _compact_node(node_id: str, data: dict, key_fields: list[str]) -> dict:
    out: dict[str, Any] = {}
    for name in ("updated_at", "reading_delay_s"):  # свежесть измерения нужна модели всегда
        if name in data:
            out[name] = data[name]
    # online/status показываем только когда они не штатные
    if data.get("online") is False:
        out["online"] = False
    if data.get("status") not in (None, "OK"):
        out["status"] = data["status"]
    if node_id == "smart_panel_01":
        for name in ("bus_voltage_v", "bus_powered", "total_power_w", "lines_on_count"):
            if name in data:
                out[name] = data[name]
        out["lines"] = [_drop_noise({k: ln.get(k) for k in LINE_FIELDS if k in ln})
                        for ln in data.get("lines") or [] if isinstance(ln, dict)]
        return out
    if node_id == "dome_automation_01":
        out["mode"] = data.get("mode")
        out["primary_co2_sensor"] = data.get("primary_co2_sensor")
        out["ventilation_auto"] = data.get("ventilation_auto")
        out["diesel_autostart"] = data.get("diesel_autostart")
        out["autonomy_forecast_h"] = data.get("autonomy_forecast_h")
        out["interlocks"] = {k: {f: v.get(f) for f in ("state", "pending_s", "blocked_remaining_s", "trips") if f in v}
                             for k, v in (data.get("interlocks") or {}).items() if isinstance(v, dict)}
        out["data_trust"] = data.get("data_trust") or {}
        events = [e for e in data.get("events") or [] if isinstance(e, dict)]
        out["events_last"] = events[-5:]
        return out
    fields = [f for f in key_fields if "[]" not in f] or [k for k in data if k not in ("updated_at",)]
    for name in fields:
        if name in data:
            out[name] = data[name]
    for name in ("sensor_state", "confidence", *ALARM_FIELDS):
        if name in data and name not in out:
            out[name] = data[name]
    return _drop_noise(out)


def _is_alarm(value: Any) -> bool:
    return value not in (None, False, 0, "", "NONE", "OK", "NORMAL")


def compact_telemetry(snapshot: dict, catalog: dict[str, dict]) -> TelemetryContext:
    nodes_raw: dict = snapshot.get("nodes") or {}
    ts = snapshot.get("timestamp")
    nodes: dict[str, dict] = {}
    anomalies: list[dict] = []
    hot: set[str] = set()

    for node_id, data in nodes_raw.items():
        if not isinstance(data, dict) or node_id == "meeting_01":
            continue
        key_fields = (catalog.get(node_id) or {}).get("key_fields", [])
        nodes[node_id] = _compact_node(node_id, data, key_fields)

        # Свежесть данных
        if data.get("status") == "UNKNOWN" or data.get("online") is False:
            _anomaly(anomalies, hot, node_id, "status", data.get("status"),
                     "нет связи или данные устарели (status/online)")
        elif ts and isinstance(data.get("updated_at"), (int, float)) and ts - data["updated_at"] > STALE_AFTER_S:
            _anomaly(anomalies, hot, node_id, "updated_at", round(ts - data["updated_at"]),
                     "данные не обновлялись дольше порога, сек")

        # Пропавшие показания
        nulls = [f for f in key_fields
                 if "[]" not in f and f in data and data[f] is None and _MEASUREMENT.search(f)]
        if nulls:
            _anomaly(anomalies, hot, node_id, ",".join(nulls), None, "показание пропало (null)")

        if data.get("sensor_state") not in (None, "OK"):
            _anomaly(anomalies, hot, node_id, "sensor_state", data["sensor_state"], "датчик сообщает о деградации")

        for name in ALARM_FIELDS:
            if _is_alarm(data.get(name)):
                _anomaly(anomalies, hot, node_id, name, data[name], "авария/ошибка узла")
                break

        if data.get("hold_level"):
            reason = data.get("hold_reason")
            _anomaly(anomalies, hot, node_id, "hold_reason", reason,
                     f"станок удержан ({data['hold_level']}), попыток resume: {data.get('resume_attempts')}",
                     extra_hot=[reason] if reason else [])

    # Щиток: линии
    for ln in (nodes_raw.get("smart_panel_01") or {}).get("lines") or []:
        if not isinstance(ln, dict):
            continue
        n = ln.get("line")
        if ln.get("state") in ("TRIPPED", "RCD_TRIP") or (ln.get("lockout_remaining_s") or 0) > 0:
            _anomaly(anomalies, hot, "smart_panel_01", f"lines[{(n or 1) - 1}]", ln.get("state"),
                     f"линия {n}: защита/блокировка ({ln.get('lockout_reason') or ln.get('protection_state')})",
                     extra_hot=[f"line:{n}"])

    # Интерлоки
    plc = nodes_raw.get("dome_automation_01") or {}
    for il, st in (plc.get("interlocks") or {}).items():
        if isinstance(st, dict) and st.get("state") in ("TRIPPED", "BLOCKED"):
            _anomaly(anomalies, hot, "dome_automation_01", f"interlocks.{il}", st.get("state"),
                     "интерлок сработал" if st["state"] == "TRIPPED" else "интерлок заблокирован агентом",
                     extra_hot=[il])

    # Расхождение датчиков CO2 — признак ложного показания
    co2 = {s: (nodes_raw.get(s) or {}).get("co2_ppm") for s in CLIMATE_SENSORS}
    vals = {s: v for s, v in co2.items() if isinstance(v, (int, float))}
    if len(vals) >= 2:
        hi, lo = max(vals.values()), min(vals.values())
        if hi - lo > max(300, 0.5 * lo):
            _anomaly(anomalies, hot, "climate_sensor_01", "co2_ppm", vals,
                     "датчики CO2 расходятся — один из них может врать", extra_hot=list(vals) + ["IL_CO2"])

    # Энергия
    soc = (nodes_raw.get("battery_01") or {}).get("soc_pct")
    if isinstance(soc, (int, float)) and soc < 35:
        _anomaly(anomalies, hot, "battery_01", "soc_pct", soc, "низкий заряд АКБ (IL_LOAD_SHED при < 25 %)",
                 extra_hot=["IL_LOAD_SHED"])

    # Стол принтера занят
    printer = nodes_raw.get("printer_3d_01") or {}
    if printer.get("bed_clear") is False and printer.get("state") in ("IDLE", "COMPLETED"):
        _anomaly(anomalies, hot, "printer_3d_01", "bed_clear", False,
                 "на столе деталь — запуск печати приведёт к BED_COLLISION", extra_hot=["thermal_insulation_01"])

    return TelemetryContext(nodes=nodes, anomalies=anomalies, hot_nodes=hot, timestamp=ts)
