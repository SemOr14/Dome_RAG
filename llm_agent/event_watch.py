"""Short-lived source events are evidence even when absent from the next telemetry frame.

Only delivered events are consumed. Re-emitted automation history and acknowledgements
are deduplicated by source identity, never treated as a fresh trip.
"""
from __future__ import annotations

import json
import re
from collections import OrderedDict

from .context.diagnostics import state_fields
from .triggers import Trigger


class EventWatch:
    def __init__(self):
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.frame_time: float | None = None

    def observe(self, events: list[dict], snapshot: dict) -> list[Trigger]:
        out: dict[str, Trigger] = {}
        nodes = snapshot.get('nodes') or {}
        for ev in events:
            p = ev.get('payload', ev)
            if not isinstance(p, dict):
                continue
            if isinstance(p.get('data'), dict):
                p = p['data']
            automation = p.get('event') if isinstance(p.get('event'), dict) else None
            identity = ((f"automation|{automation.get('event_id')}|{automation.get('t')}" if automation else '')
                        or ev.get('event_id') or json.dumps(p, sort_keys=True, default=str))
            if identity in self.seen:
                continue
            self.seen[identity] = None
            if len(self.seen) > 8192:
                self.seen.popitem(last=False)
            if p.get('updated'):
                continue
            node, param = p.get('node_id') or ev.get('node_id'), p.get('param', '')
            ids = [ev['event_id']] if ev.get('event_id') else []
            value = p.get('to')
            kind = p.get('kind') or ev.get('event_type') or p.get('type') or ev.get('type')
            text = None
            if automation:
                if automation.get('type') != 'TRIP':
                    continue
                live_interlocks = (nodes.get('dome_automation_01') or {}).get('interlocks') or {}
                current = live_interlocks.get(automation.get('interlock')) or {}
                if current.get('state') in ('TRIPPED', 'BLOCKED'):
                    continue  # already represented by the telemetry detector
                node = node or 'dome_automation_01'
                param = 'interlocks.' + str(automation.get('interlock', ''))
                text = f"Событие автоматики {automation.get('interlock')}: {automation.get('message')}"
            elif kind in ('change', 'state_change', 'node_change') and node:
                alarming = (bool(list(state_fields({'state': value})))
                            or (param.rsplit('.', 1)[-1] in ('hold_reason', 'alarm_code', 'error_code') and value not in (None, False, 0, '', 'OK', 'NONE'))
                            or (param.rsplit('.', 1)[-1] in ('smoke_detected', 'quake_detected', 'alarm') and value is True))
                if not alarming:
                    continue
                text = f'Короткое событие: {node}.{param}: {p.get("from")!r} → {value!r}; могло исчезнуть между кадрами'
                if value == 'UNDERVOLTAGE':
                    printer = nodes.get('printer_3d_01') or {}
                    text += ('; удержание фрезера UNDERVOLTAGE: просадка напряжения шины, '
                             f'проверить старт нагрева стола принтера: bed_target_c={printer.get("bed_target_c")}, '
                             f'bed_heater_power_pct={printer.get("bed_heater_power_pct")}')
                line_match = re.match(r'lines\[(\d+)\]', param)
                if node == 'smart_panel_01' and line_match:
                    i = int(line_match[1]); lines = (nodes.get(node) or {}).get('lines') or []
                    line = lines[i] if i < len(lines) and isinstance(lines[i], dict) else {}
                    text += f'; линия {line.get("line", i + 1)} ({line.get("name", "")}), leakage_ma={line.get("leakage_ma")}, alarm_code={line.get("alarm_code")}'
                    if value == 'EMULATED':
                        text += '; защита сработала без подтверждённой перегрузки/утечки'
            if text is None:
                continue
            key = f'event|{node}|{param}|{value if isinstance(value, str) else "alarm"}'
            out[key] = Trigger('alarm', key, text, {node} if node else set(), ids,
                               {'source_event': True})
        return list(out.values())

    def from_snapshot(self, snapshot: dict) -> list[Trigger]:
        stamp = snapshot.get('timestamp')
        if not isinstance(stamp, (int, float)):
            return []
        events = ((snapshot.get('nodes') or {}).get('dome_automation_01') or {}).get('events') or []
        # First frame establishes a cursor; old history is not a new emergency.
        fresh = [dict(kind='automation', event=e, node_id='dome_automation_01') for e in events
                 if isinstance(e, dict) and self.frame_time is not None
                 and isinstance(e.get('t'), (int, float)) and self.frame_time < e['t'] <= stamp]
        self.frame_time = max(stamp, self.frame_time or stamp)
        return self.observe(fresh, snapshot)
