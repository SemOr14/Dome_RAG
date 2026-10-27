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


def independent_group(state: Any, nodes: Any, *, target: str | None = None) -> bool:
    """Explicit, contemporaneous same-zone sources in distinct failure domains.

    Missing topology is unknown. Telemetry is never enriched with guessed independence.
    The target must also belong to the comparison group and a separate domain.
    """
    ids = list(dict.fromkeys(nodes))
    if len(ids) < 2:
        return False
    if target is not None and target not in ids:
        ids.append(target)
    groups, domains, stamps = [], [], []
    for node in ids:
        body = state.node(node)
        if not measurement_usable(state, node):
            return False
        group = body.get('sensor_group') or body.get('zone')
        domain = body.get('failure_domain')
        if not isinstance(group, str) or not group.strip() or not isinstance(domain, str) or not domain.strip():
            return False
        groups.append(group)
        domains.append(domain)
        stamps.append(body['updated_at'] - body.get('reading_delay_s', 0))
    return len(set(groups)) == 1 and len(set(domains)) == len(domains) and max(stamps) - min(stamps) <= 30
