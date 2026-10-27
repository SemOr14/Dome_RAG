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
