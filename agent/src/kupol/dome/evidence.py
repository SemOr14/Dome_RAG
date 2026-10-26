"""Deterministic admission of measurements; raw records are never changed."""
from __future__ import annotations

import math
from typing import Any


def finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def measurement_usable(state: Any, node: str, max_delay_s: float = 30.0) -> bool:
    """Admit current readings only. Delay budget is a policy, not a physical law.

    Missing delay in the existing wire protocol means no declared buffering.
    An explicit null/negative/nonfinite delay is unknown, never zero.
    Historical delayed readings require alignment upstream; they cannot vote now.
    """
    body = state.node(node)
    if not body or state.is_frozen(node) or body.get("online") is False:
        return False
    if not finite_number(body.get("updated_at")):
        return False
    if str(body.get("status", "OK")).upper() not in ("OK", "ONLINE"):
        return False
    if str(body.get("sensor_state", "OK")).upper() != "OK":
        return False
    delay = body.get("reading_delay_s", 0)
    return finite_number(delay) and 0 <= delay <= max_delay_s
