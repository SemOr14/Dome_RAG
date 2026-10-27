"""Bounded read-only telemetry access during one model request; never sends controls."""
from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

MAX_SENSOR_READS = 3
MAX_DEFAULT_FIELDS = 32
_PATH = re.compile(r'[^.\[\]\s]+(?:\[\d+\]|\.[^.\[\]\s]+)*')
_TOKENS = re.compile(r'([^.\[\]]+)|\[(\d+)\]')
_BLOCKED = {'recent_messages', 'passage_events', 'events', 'files', 'services'}
_SECRET = ('password', 'secret', 'token', 'authorization', 'cookie', 'api_key', 'dome_key')


class SensorRead(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    node_id: str = Field(min_length=1, max_length=200)
    fields: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(default_factory=list, max_length=16)


SENSOR_TOOL = {'type': 'function', 'function': {
    'name': 'read_sensor',
    'description': 'Прочитать выбранный датчик из последней телеметрии сейчас, с метками времени и свежести. '
                   'Не принудительный физический опрос. Всего максимум 3 обращения на весь запрос. '
                   'Пустой fields возвращает до 32 скалярных полей.',
    'parameters': SensorRead.model_json_schema(),
}}


def _blocked(path: str) -> bool:
    parts = [p for p, _ in _TOKENS.findall(path) if p]
    return any(p.lower() in _BLOCKED or (p.lower() == 'key' or p.lower().endswith('_key')) or any(s in p.lower() for s in _SECRET) for p in parts)


def _number(value: Any) -> float | None:
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, bool)) or _number(value) is not None


def _meta(value: Any) -> Any:
    return value if _scalar(value) and (not isinstance(value, str) or len(value) <= 512) else None


def _lookup(body: dict, path: str):
    if not _PATH.fullmatch(path) or _blocked(path):
        return None, False
    current: Any = body
    for name, index in _TOKENS.findall(path):
        if name and isinstance(current, dict) and name in current:
            current = current[name]
        elif index and isinstance(current, list) and int(index) < len(current):
            current = current[int(index)]
        else:
            return None, False
    return (current, True) if _scalar(current) and (not isinstance(current, str) or len(current) <= 512) else (None, False)


def _default_fields(value: Any, prefix: str = '', depth: int = 0):
    if depth > 6 or _blocked(prefix):
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _default_fields(item, f'{prefix}.{key}' if prefix else str(key), depth + 1)
    elif isinstance(value, list):
        for i, item in enumerate(value[:32]):
            yield from _default_fields(item, f'{prefix}[{i}]', depth + 1)
    elif prefix and _scalar(value) and (not isinstance(value, str) or len(value) <= 512):
        yield prefix, value


class SensorSession:
    """Request-local budget, shared across model rounds and JSON repair attempts."""
    def __init__(self, telemetry, catalog, *, clock=time.time):
        self.telemetry = telemetry
        self.catalog = catalog
        self.clock = clock
        self.used = 0

    @property
    def remaining(self) -> int:
        return MAX_SENSOR_READS - self.used

    def execute(self, call: dict) -> dict:
        if self.remaining <= 0:
            return {'ok': False, 'error': 'limit_exceeded', 'remaining': 0}
        self.used += 1  # Invalid and failed calls consume budget too.
        function = call.get('function') or {}
        if not isinstance(function, dict) or call.get('type') != 'function' or function.get('name') != 'read_sensor':
            return {'ok': False, 'error': 'unknown_tool', 'remaining': self.remaining}
        try:
            arguments = function.get('arguments', '')
            if not isinstance(arguments, str) or len(arguments) > 8192:
                raise ValueError('invalid arguments')
            request = SensorRead.model_validate_json(arguments)
        except (ValueError, ValidationError):
            return {'ok': False, 'error': 'invalid_arguments', 'remaining': self.remaining}
        if request.node_id == 'meeting_01' or request.node_id not in self.catalog().by_id:
            return {'ok': False, 'error': 'node_not_allowed', 'node_id': request.node_id, 'remaining': self.remaining}
        try:
            snapshot = self.telemetry.latest()
            nodes = snapshot.get('nodes') or {}
            body = nodes.get(request.node_id)
            if not isinstance(body, dict):
                return {'ok': False, 'error': 'node_unavailable', 'node_id': request.node_id, 'remaining': self.remaining}
            if request.fields:
                pairs = [(field, *_lookup(body, field)) for field in request.fields]
                readings = {f: v for f, v, present in pairs if present}
                missing = [f for f, _, present in pairs if not present]
            else:
                readings = {}
                for field, value in _default_fields(body):
                    readings[field] = value
                    if len(readings) >= MAX_DEFAULT_FIELDS:
                        break
                missing = []
            age = _number(snapshot.get('telemetry_age_s'))
            observed = snapshot.get('observed_at')
            if age is None and isinstance(observed, str):
                try:
                    dt = datetime.fromisoformat(observed.replace('Z', '+00:00'))
                    if dt.tzinfo is not None:
                        age = self.clock() - dt.timestamp()
                except ValueError:
                    pass
            stamp = _number(snapshot.get('timestamp'))
            updated = _number(body.get('updated_at'))
            node_age = stamp - updated if stamp is not None and updated is not None else None
            bad = snapshot.get('stale') or body.get('online') is False or body.get('status') == 'UNKNOWN'
            bad = bad or age is not None and (age < 0 or age > 30) or node_age is not None and (node_age < 0 or node_age > 30)
            fresh = False if bad else (True if age is not None and node_age is not None and body.get('online') is True and body.get('status') == 'OK' else None)
            return {'ok': True, 'node_id': request.node_id, 'source': 'telemetry_snapshot',
                    'readings': readings, 'missing_fields': missing,
                    'timestamp': _meta(snapshot.get('timestamp')), 'observed_at': _meta(observed),
                    'node_updated_at': _meta(body.get('updated_at')), 'telemetry_age_s': age, 'node_age_s': node_age,
                    'online': _meta(body.get('online')), 'status': _meta(body.get('status')), 'sensor_state': _meta(body.get('sensor_state')),
                    'fresh': fresh, 'remaining': self.remaining}
        except Exception:  # Source failures are observations, not successful measurements.
            return {'ok': False, 'error': 'source_unavailable', 'node_id': request.node_id, 'remaining': self.remaining}
