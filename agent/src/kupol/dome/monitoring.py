"""Streaming numeric checks shared by the agent and the web panel; no control actions.

A statistical departure is a reason to investigate, never proof of a real incident.
Only distinct source updates train the baseline. Three healthy updates clear a warning.
"""
from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterator
from statistics import median
from typing import Any

# Operational warning limits, not equipment protection thresholds.
LIMITS: dict[tuple[str, str], tuple[float | None, float | None]] = {
    ('battery_01', 'soc_pct'): (35, 100),
    ('battery_01', 'temperature_c'): (0, 50),
    ('solar_inverter_01', 'battery_soc_pct'): (35, 100),
    ('solar_inverter_01', 'inverter_temp_c'): (None, 70),
    **{(f'climate_sensor_0{i}', 'co2_ppm'): (0, 1500) for i in (1, 2, 3)},
    **{(f'climate_sensor_0{i}', 'humidity_pct'): (0, 85) for i in (1, 2, 3)},
    **{(f'climate_sensor_0{i}', 'temperature_c'): (10, 35) for i in (1, 2, 3)},
}
SERVICE_FIELDS = {'updated_at', 'timestamp', 'id', 'line', 'day', 'hour', 'last_message_id'}
SKIP_TREES = {'recent_messages', 'passage_events', 'events', 'files', 'services'}


def leaves(value: Any, path: str = '') -> Iterator[tuple[str, float | None]]:
    """Numeric and null leaves, including arrays and unknown device fields."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key not in SKIP_TREES:
                yield from leaves(item, f'{path}.{key}' if path else key)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from leaves(item, f'{path}[{i}]')
    elif value is None or type(value) in (int, float):
        yield path, value


class NumericMonitor:
    def __init__(self) -> None:
        self.baselines: dict[tuple[str, str], deque[float]] = {}
        self.active: dict[tuple[str, str], dict[str, Any]] = {}
        self.good: dict[tuple[str, str], int] = {}
        self.marks: dict[str, Any] = {}
        self.values_seen = 0

    def observe(self, nodes: dict[str, Any], frame: Any) -> list[dict[str, Any]]:
        for node, body in nodes.items():
            if not isinstance(body, dict) or node == 'meeting_01':
                continue
            mark = body.get('updated_at')
            if not isinstance(mark, (int, float)) or isinstance(mark, bool) or not math.isfinite(mark):
                mark = frame
            if node in self.marks and mark == self.marks[node]:
                continue
            self.marks[node] = mark
            present = dict(leaves(body))
            for n, field in self.baselines:
                if n == node and field not in present:
                    present[field] = None
            for field, value in present.items():
                if field.rsplit('.', 1)[-1] in SERVICE_FIELDS:
                    continue
                key = (node, field)
                if value is None and key not in self.baselines and key not in LIMITS:
                    continue
                self.values_seen += 1
                baseline = self.baselines.setdefault(key, deque(maxlen=60))
                note = ''
                if value is None or not math.isfinite(value):
                    note = 'Измерение отсутствует или не является конечным числом'
                else:
                    lo, hi = LIMITS.get(key, (None, None))
                    if (lo is not None and value < lo) or (hi is not None and value > hi):
                        note = f'Вне рабочего диапазона {lo if lo is not None else "−∞"} … {hi if hi is not None else "∞"}'
                    # Counters, setpoints and positions change normally during operations.
                    dynamic = any(s in field for s in ('elapsed', 'time_left', 'duration', 'progress', 'target', 'count', 'attempt', 'remaining', 'trips', '_mm', 'used_g'))
                    if not note and len(baseline) >= 12 and value != baseline[-1] and not dynamic:
                        center = median(baseline)
                        mad = median(abs(v - center) for v in baseline)
                        band = max(6 * 1.4826 * mad, abs(center) * .35, 1.0)
                        if abs(value - center) > band:
                            note = f'Отклонение от недавнего фона {center:.4g} более чем на {band:.4g}; проверить режим и независимый источник'
                if note:
                    self.active[key] = {'node': node, 'field': field, 'value': value if value is not None and math.isfinite(value) else None,
                                        'note': note, 'kind': 'candidate', 'frame': frame}
                    self.good[key] = 0
                else:
                    assert value is not None
                    baseline.append(float(value))
                    self.good[key] = self.good.get(key, 0) + 1
                    if self.good[key] >= 3:
                        self.active.pop(key, None)
                    elif key in self.active:
                        self.active[key]['value'] = value
        return list(self.active.values())


def assess_impacts(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Independent axes: mechanism and authenticity; a false signal can cause a real cascade."""
    nodes = snapshot.get('nodes') or {}
    out: list[dict[str, Any]] = []
    timestamp = snapshot.get('timestamp')

    def fresh(body: dict[str, Any]) -> bool:
        stamp = body.get('updated_at')
        age_ok = isinstance(timestamp, (int, float)) and isinstance(stamp, (int, float)) and 0 <= timestamp - stamp <= 30
        age = snapshot.get('telemetry_age_s')
        received_fresh = not snapshot.get('stale') and (age is None or age <= 30)
        return received_fresh and body.get('online') is True and body.get('status') == 'OK' and age_ok and body.get('sensor_state', 'OK') == 'OK'

    trust = (nodes.get('dome_automation_01') or {}).get('data_trust') or {}
    co2 = {n: float(b['co2_ppm']) for n, b in nodes.items() if n.startswith('climate_sensor_') and isinstance(b, dict)
           and trust.get(f'{n}.co2_ppm') not in ('UNRELIABLE', 'FAILED')
           and trust.get(f'{n}.*') not in ('UNRELIABLE', 'FAILED')
           and fresh(b) and type(b.get('co2_ppm')) in (int, float) and math.isfinite(b['co2_ppm'])}
    zones = {(nodes[n] or {}).get('zone') for n in co2}
    if len(co2) >= 3 and len(zones) == 1:
        center = median(co2.values())
        suspects = [n for n, v in co2.items() if abs(v - center) > 400]
        peers = [v for n, v in co2.items() if n not in suspects]
        if len(suspects) == 1 and len(peers) >= 2 and max(peers) - min(peers) < 250 and max(peers) < 1200:
            out.append({'kind': 'false', 'authenticity': 'suspected_false', 'mechanism': 'base',
                        'cause': 'Один CO₂ расходится с двумя свежими согласованными источниками', 'nodes': list(co2),
                        'evidence': co2, 'check': 'Проверить зону датчика и тренд; не снимать защиту только по этой гипотезе'})
    if sum(v >= 1500 for v in co2.values()) >= 2:
        out.append({'kind': 'base', 'authenticity': 'real', 'mechanism': 'base', 'nodes': list(co2),
                    'cause': 'Высокий CO₂ подтверждён несколькими свежими датчиками', 'evidence': co2,
                    'check': 'Проверить вентиляцию, питание и снижение CO₂ после воздействия'})
    plc = nodes.get('dome_automation_01') or {}
    for il, body in (plc.get('interlocks') or {}).items():
        if not isinstance(body, dict) or body.get('state') != 'TRIPPED':
            continue
        held = [n for n in ('printer_3d_01', 'cnc_01') if (nodes.get(n) or {}).get('hold_reason') == il]
        if held:
            out.append({'kind': 'cascade', 'authenticity': 'unknown', 'mechanism': 'cascade',
                        'nodes': ['dome_automation_01', *held], 'cause': f'{il} → удержание {", ".join(held)}',
                        'evidence': {'interlock': il, 'held': held},
                        'check': 'Устранить первопричину; подтвердить свежими данными; затем разрешённый сброс и отдельная проверка станка'})
    printer = nodes.get('printer_3d_01') or {}
    if printer.get('webhook_state') == 'COMPLETED' and printer.get('real_state', printer.get('state')) in ('PRINTING', 'PAUSED'):
        out.append({'kind': 'hidden', 'authenticity': 'unknown', 'mechanism': 'hidden', 'nodes': ['printer_3d_01'],
                    'cause': 'Webhook сообщает завершение при незавершённой физической операции',
                    'evidence': {k: printer.get(k) for k in ('webhook_state', 'real_state', 'state', 'progress_pct')},
                    'check': 'Сверить прогресс, мощность и осмотр стола; не запускать следующую работу'})
    return out
