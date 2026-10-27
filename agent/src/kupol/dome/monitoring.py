"""Shared, read-only numeric evidence for charts and model triggers.

Limits are advisory, never protection settings. Known operating cycles are not compared
with a single median. Only fresh source updates count, including for recovery and stuck sensors.
"""
from __future__ import annotations

import math
import re
from collections import deque
from collections.abc import Iterator
from statistics import median
from typing import Any

LIMITS: dict[tuple[str, str], tuple[float | None, float | None]] = {
    ('battery_01', 'soc_pct'): (35, 100),
    ('battery_01', 'temperature_c'): (0, 50),
    ('solar_inverter_01', 'battery_soc_pct'): (35, 100),
    ('solar_inverter_01', 'inverter_temp_c'): (None, 60),
    ('fuel_tank_01', 'water_content_ppm'): (0, 500),
    ('network_link_01', 'rx_power_dbm'): (-12, None),
    ('weather_station_01', 'temperature_c'): (-50, 45),
    **{(f'climate_sensor_0{i}', 'co2_ppm'): (250, 1500) for i in (1, 2, 3)},
    **{(f'climate_sensor_0{i}', 'humidity_pct'): (0, 85) for i in (1, 2, 3)},
    **{(f'climate_sensor_0{i}', 'temperature_c'): (10, 35) for i in (1, 2, 3)},
}
SERVICE_FIELDS = {'updated_at', 'timestamp', 'id', 'line', 'day', 'hour', 'last_message_id'}
SKIP_TREES = {'recent_messages', 'passage_events', 'events', 'files', 'services', 'last_probe', 'data_trust', 'interlocks'}
# Normal on/off cycles, generation, signed battery current and control settings need
# a physical bound or a contextual rule, not a level-change alarm.
CYCLIC = re.compile(r'(?:power|current|voltage)_?[wav]?$|_(?:w|a|v|rpm|lux|mm|mm_h|mm_s|l|l_min|l_h|m3_h)$')
DYNAMIC = ('elapsed', 'time_left', 'duration', 'progress', 'target', 'count', 'attempt',
           'remaining', 'trips', 'used_g', 'uptime', 'capacity', 'confidence', 'crc_errors',
           'poll_interval', 'link_flaps', 'wind_', 'pwm', 'voc_index', 'dust_', 'speed_pct',
           'load_pct', 'limit_pct', 'level_pct', 'soc_pct', 'free_memory_pct')
DYNAMIC += ('bitrate', 'signal_quality', 'snr_db', 'airflow', 'pressure_diff', 'stage', 'cell_voltage_delta')


def number(value: Any) -> float | None:
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def leaves(value: Any, path: str = '') -> Iterator[tuple[str, float | None]]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key not in SKIP_TREES:
                yield from leaves(item, f'{path}.{key}' if path else key)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from leaves(item, f'{path}[{i}]')
    elif value is None or type(value) in (int, float):
        yield path, value


def contextual_note(node: str, field: str, value: float, body: dict, nodes: dict | None = None) -> str:
    if field.endswith('_temp_c'):
        prefix = field[:-len('_temp_c')]
        target = number(body.get(prefix + '_target_c'))
        power = number(body.get(prefix + '_heater_power_pct', body.get('heater_power_pct')))
        # Cooling after a disabled heater and normal warm-up are expected.
        if target is not None and target > 0 and power is not None and power > 5 and value > target + max(10, target * .15):
            return f'Температура {field} выше цели {target:g} °C при включённом нагревателе {power:g} %; рассогласование температуры и мощности'
    if body.get('fan_state') == 'FAULT':
        if field == 'fan_current_a' and value < .05:
            return 'Отказ вентилятора устройства: fan_state FAULT, ток вентилятора отсутствует'
        if field == 'inverter_temp_c' and value >= 50:
            return 'Рост температуры инвертора при отказе вентилятора fan_state FAULT'
    if field == 'cell_voltage_delta_mv' and value > 60:
        # A delta alone has no universal failure threshold in the device contract.
        # Corroborate it with an unexpected BMS balancing/charge restriction.
        peers = [b for b in (nodes or {}).values() if isinstance(b, dict) and 'battery_soc_pct' in b]
        inverter = peers[0] if len(peers) == 1 else {}
        soc = number(body.get('soc_pct'))
        balancing = body.get('balancing_state') == 'BALANCING' or inverter.get('balancing_state') == 'BALANCING'
        blocked = body.get('charge_allowed') is False or inverter.get('battery_charge_blocked') is True
        if soc is not None and soc < 90 and (balancing or blocked):
            return 'Разбаланс ячеек >60 мВ при балансировке/ограничении заряда BMS; проверить состояние батареи'
    return ''


def unit_floor(field: str) -> float:
    name = re.sub(r'\[\d+\]', '', field)
    for suffix, floor in (('_a', .05), ('_v', .5), ('_dbm', .5), ('_bar', .02),
                          ('_pa', 20), ('_c', 1), ('_ppm', 10), ('_pct', 3), ('_mm', .2)):
        if name.endswith(suffix):
            return floor
    return 1.0


class NumericMonitor:
    def __init__(self, limits: dict[tuple[str, str], tuple[float | None, float | None]] | None = None) -> None:
        self.limits = {**LIMITS, **(limits or {})}
        self.baselines: dict[tuple[str, str], deque[float]] = {}
        self.active: dict[tuple[str, str], dict[str, Any]] = {}
        self.good: dict[tuple[str, str], int] = {}
        self.marks: dict[str, Any] = {}
        self.values_seen = 0
        self.stuck: dict[tuple[str, str], tuple[float, float, int]] = {}
        self.frozen_signals: dict[tuple[str, str], tuple[float, float, int]] = {}
        self.regimes: dict[str, tuple] = {}
        self.peer_bias: dict[tuple[str, str, tuple], deque] = {}
        self.peer_pending: dict[tuple[str, str, tuple], int] = {}

    def _limits(self, node: str, field: str, body: dict | None = None):
        if field.endswith('.leakage_ma') or field == 'leakage_ma':
            return (0, 10)
        bounds = {'soc_pct': (35, 100), 'battery_soc_pct': (35, 100), 'co2_ppm': (250, 1500),
                  'inverter_temp_c': (None, 60), 'water_content_ppm': (0, 500), 'rx_power_dbm': (-12, None)}
        if not any(f in (body or {}) for f in ('active_path', 'reserve_link_state', 'link_flaps')):
            bounds.pop('rx_power_dbm', None)  # an optical receiver bound is not a radio RSSI bound
        return self.limits.get((node, field), bounds.get(field, (None, None)))

    @staticmethod
    def _statistical(node: str, field: str) -> bool:
        field = re.sub(r'\[\d+\]', '', field)
        if field in ('nozzle_temp_c', 'bed_temp_c', 'heater_power_pct', 'bed_heater_power_pct'):
            return False
        if field == 'rx_power_dbm':
            return False  # gradual optical degradation has its own bound
        if field.endswith('leakage_ma') or field == 'motor_temp_c':
            return False  # line switching and actuator duty cycles have explicit checks
        if field == 'spindle_temp_c':
            return False  # normal motor warm-up; faults come from state/alarm fields
        return field != 'rpm' and not CYCLIC.search(field) and not any(s in field for s in DYNAMIC)

    def observe(self, nodes: dict[str, Any], frame: Any) -> list[dict[str, Any]]:
        # An absent/offline node is a connection problem, not hundreds of missing values.
        newest = max((number(b.get('updated_at')) or 0 for b in nodes.values() if isinstance(b, dict)), default=0)
        clock_skew = any(abs(number(b.get('clock_offset_s')) or 0) > 30 for b in nodes.values() if isinstance(b, dict))
        for key in list(self.active):
            body = nodes.get(key[0])
            stamp = number(body.get('updated_at')) if isinstance(body, dict) else None
            if (not isinstance(body, dict) or body.get('status') == 'UNKNOWN' or body.get('online') is False
                    or (not clock_skew and stamp is not None and newest - stamp > 30)):
                self.active.pop(key, None)
                self.stuck.pop(key, None)
        for node, body in nodes.items():
            if not isinstance(body, dict) or node == 'meeting_01':
                continue
            stamp = number(body.get('updated_at'))
            if body.get('status') == 'UNKNOWN' or body.get('online') is False or (not clock_skew and stamp is not None and newest - stamp > 30):
                for state in (self.stuck, self.frozen_signals):
                    for key in [k for k in state if k[0] == node]:
                        state.pop(key, None)
                continue
            mark = stamp if stamp is not None else frame
            if node in self.marks and mark == self.marks[node]:
                continue
            previous_stamp = number(self.marks.get(node))
            if stamp is not None and previous_stamp is not None:
                if stamp < previous_stamp:
                    continue
                if stamp - previous_stamp > 300:
                    for key in [k for k in self.peer_pending if k[0] == node]:
                        self.peer_pending.pop(key, None)
                    for state in (self.stuck, self.frozen_signals):
                        for key in [k for k in state if k[0] == node]:
                            state.pop(key, None)
            regime = (body.get('state'), body.get('mode'), body.get('file_name'),
                      tuple(sorted((k, v) for k, v in body.items() if '_target_' in k and number(v) is not None)))
            if node in self.regimes and regime != self.regimes[node]:
                for key in [k for k in self.baselines if k[0] == node]:
                    self.baselines[key].clear()
                    self.frozen_signals.pop(key, None)
                    if self.active.get(key, {}).get('kind') == 'candidate':
                        self.active.pop(key, None)
            self.regimes[node] = regime
            self.marks[node] = mark
            present = dict(leaves(body))
            for n, field in self.baselines:
                if n == node and field not in present:
                    present[field] = None
            for field, raw in present.items():
                if field.rsplit('.', 1)[-1] in SERVICE_FIELDS:
                    continue
                key = (node, field)
                lo, hi = self._limits(node, field, body)
                if raw is None and key not in self.baselines and lo is None and hi is None:
                    continue
                # An array replaced by null produces gaps only in its established children.
                if raw is None and any(f.startswith(field + '[') for f in present):
                    continue
                self.values_seen += 1
                baseline = self.baselines.setdefault(key, deque(maxlen=60))
                value = number(raw)
                note, kind = '', 'candidate'
                if value is None:
                    for peer_key in [k for k in self.peer_pending if k[:2] == key]:
                        self.peer_pending.pop(peer_key, None)
                    note, kind = 'Измерение отсутствует или не является конечным числом', 'missing'
                else:
                    note = contextual_note(node, field, value, body, nodes)
                    if note:
                        kind = 'conflict'
                    elif (lo is not None and value < lo) or (hi is not None and value > hi):
                        note = f'Вне рабочего диапазона {lo if lo is not None else "−∞"} … {hi if hi is not None else "∞"}'
                        kind = 'limit'
                    if not note and len(baseline) >= 12 and self._statistical(node, field):
                        center = median(baseline)
                        mad = median(abs(v - center) for v in baseline)
                        floor = 12 if 'temperature_c' in field else 150 if 'co2_ppm' in field else .5 if field.endswith('_ppm') else unit_floor(field)
                        band = max(6 * 1.4826 * mad, abs(center) * .35, floor)
                        if abs(value - center) > band:
                            note = f'Отклонение от недавнего фона {center:.4g} более чем на {band:.4g}; проверить режим и независимый источник'
                    if field == 'wind_direction_deg' and stamp is not None:
                        speed = number(body.get('wind_speed_ms'))
                        before = self.stuck.get(key)
                        # Fixed direction in calm weather is not evidence of a stuck vane.
                        if speed is None or speed < 1:
                            self.stuck.pop(key, None)
                        else:
                            delta = abs((value - before[0] + 180) % 360 - 180) if before else 360
                            same = before is not None and delta < .1 and stamp > before[1]
                            start, count = (before[1], before[2] + 1) if same else (stamp, 1)
                            self.stuck[key] = (value, start, count)
                            if count >= 12 and stamp - start >= 20 * 60:
                                note, kind = 'Флюгер застыл: направление ветра не меняется ≥20 минут при живом узле и ненулевом ветре', 'stuck'
                if value is not None and stamp is not None and not note:
                    disagreement = self._peer_disagreement(node, field, value, stamp, body, nodes)
                    if disagreement:
                        note, kind = disagreement, 'conflict'
                if value is not None and stamp is not None and not note:
                    frozen = self._frozen_peer(node, field, value, stamp, body, nodes)
                    if frozen:
                        note, kind = frozen, 'stuck'
                if note:
                    self.active[key] = {'node': node, 'field': field, 'value': value, 'note': note,
                                        'kind': kind, 'frame': frame}
                    self.good[key] = 0
                    # Statistical excursions must eventually become the new background;
                    # bounds and missing measurements never train it.
                    if kind == 'candidate' and value is not None:
                        baseline.append(value)
                else:
                    if value is not None:
                        baseline.append(value)
                    self.good[key] = self.good.get(key, 0) + 1
                    if self.good[key] >= 3:
                        self.active.pop(key, None)
                    elif key in self.active:
                        self.active[key]['value'] = value
        return list(self.active.values())


    def _frozen_peer(self, node, field, value, stamp, body, nodes):
        """A constant sensor needs independent, comparable changing evidence, not just time."""
        if not re.search(r'_(?:c|ppm|pa|bar|pct|v)(?:\[\d+\])?$', field) or not self._statistical(node, field):
            return ''
        key = (node, field)
        before = self.frozen_signals.get(key)
        same = before is not None and value == before[0] and stamp >= before[1]
        start, count = (before[1], before[2] + 1) if same else (stamp, 1)
        self.frozen_signals[key] = value, start, count
        if count < 12 or stamp - start < 1200:
            return ''
        zone = body.get('zone') or body.get('sensor_group')
        if not zone:
            return ''  # clocks progressing alone do not prove that a measurement must vary
        for peer, other in nodes.items():
            if (peer == node or not isinstance(other, dict) or (other.get('zone') or other.get('sensor_group')) != zone
                    or other.get('online') is False or other.get('status') == 'UNKNOWN'):
                continue
            peer_stamp = number(other.get('updated_at'))
            values = self.baselines.get((peer, field))
            if peer_stamp is None or abs(peer_stamp - stamp) > 30 or not values or len(values) < 12:
                continue
            center = median(values)
            scale = unit_floor(field)
            if max(values) - min(values) > 3 * scale and abs(value - center) > max(6 * scale, abs(center) * .35):
                return f'Зависшее измерение {field}: не меняется ≥20 минут при живом узле, сопоставимый датчик {peer} меняется и показывает другое; проверить независимым источником'
        return ''


    def _peer_disagreement(self, node, field, value, stamp, body, nodes):
        """Learn normal offsets, then compare with two agreeing independent peers.

        Zone/group and exact field/unit are required; a shared drift is not resolvable
        without an independent reference. Confirmation counts source updates, not polls.
        """
        group = body.get('sensor_group') or body.get('zone')
        stamp -= max(0, number(body.get('reading_delay_s')) or 0)
        if not group or not self._statistical(node, field) or not re.search(r'_(?:c|ppm|pa|bar|pct)$', field):
            return ''
        peers = []
        for peer, other in nodes.items():
            if peer == node or not isinstance(other, dict) or (other.get('sensor_group') or other.get('zone')) != group:
                continue
            t, v = number(other.get('updated_at')), number(other.get(field))
            if t is not None:
                t -= max(0, number(other.get('reading_delay_s')) or 0)
            if (v is not None and t is not None and abs(t - stamp) <= 30
                    and other.get('online') is not False and other.get('status') != 'UNKNOWN'
                    and other.get('sensor_state') in (None, 'OK')):
                peers.append((peer, v))
        if len(peers) < 2:
            for key in [k for k in self.peer_pending if k[:2] == (node, field)]:
                self.peer_pending.pop(key, None)
            return ''
        center = median(v for _, v in peers)
        scale = unit_floor(field)
        tolerance = max(3 * scale, abs(center) * .05)
        if max(v for _, v in peers) - min(v for _, v in peers) > tolerance:
            for key in [k for k in self.peer_pending if k[:2] == (node, field)]:
                self.peer_pending.pop(key, None)
            return ''  # no reference majority; do not choose a sensor arbitrarily
        key = (node, field, (repr(body.get('mode')), repr(body.get('state')),
                             tuple(sorted((p, repr(nodes[p].get('mode')), repr(nodes[p].get('state'))) for p, _ in peers))))
        history = self.peer_bias.setdefault(key, deque(maxlen=60))
        residual = value - center
        if len(history) < 12:
            history.append(residual)
            return ''
        normal = median(history)
        mad = median(abs(x - normal) for x in history)
        band = max(tolerance, 6 * 1.4826 * mad)
        if abs(residual - normal) > band:
            self.peer_pending[key] = self.peer_pending.get(key, 0) + 1
            if self.peer_pending[key] >= 3:
                return f'{field}: устойчивое расхождение с согласованными датчиками {", ".join(p for p, _ in peers)}; отклонение от обычной разницы {residual - normal:.4g}, допуск {band:.4g}; проверить датчик и условия измерения'
        else:
            self.peer_pending.pop(key, None)
            history.append(residual)
        return ''
