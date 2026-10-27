"""Сверка аномального показания с близкими и похожими датчиками.

Когда у одного узла аномалия, модели нужны соседи для сравнения: ложь одного датчика видна только на фоне
остальных. Соседями считаются: узлы того же семейства (``climate_sensor_01`` → ``02``, ``03``), узлы той же
системы каталога с таким же полем и явно заданные источники одного измерения (АКБ и инвертор). Значения берутся
из снимка, а не из аномалии, и показываются рядом со свежестью и состоянием датчика. Это сверка для чтения:
код не решает, какой датчик врёт.
"""

from __future__ import annotations

import re
import statistics
from typing import Any

MAX_SUBJECTS = 8
MAX_PEERS = 6
FAMILY = re.compile(r"_\d+$")
# Поля, значение которых соседи не сравнивают (служебные, а не измерения)
SKIP_PARAMS = {None, "updated_at", "hold_reason"}
# Разные узлы, показывающие одну величину: (узел, поле) -> [(узел, поле)]. Копии одного показания не независимы
SAME_MEASUREMENT: dict[tuple[str, str], list[tuple[str, str]]] = {
    ("battery_01", "soc_pct"): [("solar_inverter_01", "battery_soc_pct")],
    ("solar_inverter_01", "battery_soc_pct"): [("battery_01", "soc_pct")],
    ("battery_01", "voltage_v"): [("solar_inverter_01", "battery_voltage_v")],
    ("solar_inverter_01", "battery_voltage_v"): [("battery_01", "voltage_v")],
}


def family(node_id: str) -> str:
    return FAMILY.sub("", node_id)


def _num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _state(data: dict, ts: float | None) -> str:
    """Состояние датчика одной строкой: online/status/sensor_state и возраст показания."""
    bits = []
    if data.get("online") is False:
        bits.append("offline")
    if data.get("status") not in (None, "OK"):
        bits.append(f"status={data['status']}")
    if data.get("sensor_state") not in (None, "OK"):
        bits.append(f"sensor_state={data['sensor_state']}")
    if ts and _num(data.get("updated_at")):
        bits.append(f"{max(0, round(ts - data['updated_at']))} с назад")
    return ", ".join(bits) or "OK"


def _params(anomaly: dict) -> list[str]:
    param = anomaly.get("param")
    if not isinstance(param, str) or param in SKIP_PARAMS:
        return []
    if param.startswith(("lines[", "interlocks.")):
        return []  # линии щитка и интерлоки — не измерения датчиков
    return [p for p in param.split(",") if p]


def compare_peers(snapshot: dict, anomalies: list[dict], catalog: dict[str, dict] | None = None) -> list[dict[str, Any]]:
    """Для каждой пары (узел, поле) из аномалий — значения того же поля у близких узлов.

    Returns:
        ``[{"node", "param", "note", "value", "state", "peers": [{"node", "value", "state", "relation"}], "median", "delta"}]``;
        пусто, если у аномального узла нет соседей с таким полем.
    """
    nodes: dict = snapshot.get("nodes") or {}
    ts = snapshot.get("timestamp")
    catalog = catalog or {}
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for anomaly in anomalies:
        node = anomaly.get("node")
        data = nodes.get(node)
        if not isinstance(data, dict):
            continue
        for param in _params(anomaly):
            if (node, param) in seen or param not in data:
                continue
            seen.add((node, param))
            peers = _peers(node, param, nodes, catalog, ts)
            if not peers:
                continue
            row: dict[str, Any] = {"node": node, "param": param, "note": anomaly.get("note", ""), "value": data[param],
                                   "state": _state(data, ts), "peers": peers[:MAX_PEERS]}
            values = [p["value"] for p in peers if _num(p["value"])]
            if _num(data[param]) and values:
                row["median"] = round(statistics.median(values), 3)
                row["delta"] = round(data[param] - row["median"], 3)
            out.append(row)
            if len(out) >= MAX_SUBJECTS:
                return out
    return out


def _peers(node: str, param: str, nodes: dict, catalog: dict[str, dict], ts: float | None) -> list[dict[str, Any]]:
    found: dict[str, tuple[str, str]] = {}  # узел -> (поле у него, отношение)
    own_family = family(node)
    for other, body in nodes.items():
        if other != node and isinstance(body, dict) and param in body and family(other) == own_family:
            found[other] = (param, "того же типа")
    system = (catalog.get(node) or {}).get("system")
    if system:
        for other, body in nodes.items():
            if other != node and other not in found and isinstance(body, dict) and param in body \
                    and (catalog.get(other) or {}).get("system") == system and family(other) != own_family:
                found[other] = (param, f"та же система ({system})")
    for other, other_param in SAME_MEASUREMENT.get((node, param), []):
        if isinstance(nodes.get(other), dict) and other_param in nodes[other]:
            found[other] = (other_param, "то же измерение, не независимый источник")
    rows = []
    for other, (field, relation) in found.items():
        body = nodes[other]
        rows.append({"node": other, "param": field, "value": body.get(field), "state": _state(body, ts), "relation": relation})
    rank = {"того же типа": 0}
    rows.sort(key=lambda r: (rank.get(r["relation"], 1), r["node"]))
    return rows


def peer_lines(rows: list[dict[str, Any]]) -> list[str]:
    """Строки для промпта: аномальное значение и соседи рядом."""
    lines = []
    for r in rows:
        peers = "; ".join(
            f"{p['node']}{'' if p['param'] == r['param'] else '.' + p['param']} = {p['value']!r} [{p['state']}] — {p['relation']}" for p in r["peers"])
        tail = f"; медиана соседей {r['median']:g}, отклонение {r['delta']:+g}" if "median" in r else ""
        lines.append(f"{r['node']}.{r['param']} = {r['value']!r} [{r['state']}] ({r['note']}) ↔ {peers}{tail}")
    return lines
