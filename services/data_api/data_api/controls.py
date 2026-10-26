"""Read-only preflight, shared by operator and agent commands. Never sends commands."""

from datetime import datetime

from .codec import dumps
from .control_rules import ALIASES, value_errors

MACHINES = ("printer_3d_01", "cnc_01")


def is_control(event):
    p = event["payload"]
    return (
        event["event_type"] in ("control", "control_intent", "action")
        or isinstance(p, dict)
        and (p.get("type") == "control" or p.get("kind") == "action")
    )


def actor(event):
    if event["source"] in ("agent", "operator"):
        return event["source"]
    if event["source"] == "import":
        p = event["payload"]
        value = p.get("actor", "unknown") if isinstance(p, dict) else "unknown"
        return value if value in ("agent", "operator") else "unknown"
    return "unknown"


def age_seconds(timestamp, now):
    try:
        date = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if date.tzinfo is None:
            return None
        return (now - date).total_seconds()
    except (ValueError, TypeError, AttributeError):
        return None


def preflight(store, conn, event, now):
    p = event["payload"] if isinstance(event["payload"], dict) else {}
    reasons = []
    context_ids = []
    who = actor(event)
    node, action = p.get("node_id"), p.get("action")
    value = p.get("value")
    canonical = None

    def reject(reason):
        if reason not in reasons:
            reasons.append(reason)

    def result():
        return {
            "accepted": not reasons,
            "status": "rejected" if reasons else "accepted",
            "reasons": reasons,
            "actor": who,
            "canonical_action": canonical,
            "context_event_ids": sorted(set(context_ids)),
            "rules_version": "01_API-v1",
            "checked_at": now.isoformat(),
            "max_state_age_seconds": store.max_state_age,
            "scope": "syntax_catalogue_state",
            "effect_verified": False,
        }

    if who not in ("agent", "operator"):
        reject("unknown_actor")
    if p.get("type") != "control":
        reject("invalid_control_type")
    if event["channel"] != "websocket":
        reject("unsupported_control_channel")
    if not isinstance(node, str) or not node:
        reject("invalid_node_id")
    if not isinstance(action, str) or not action:
        reject("invalid_action")
    if event.get("node_id") is not None and event["node_id"] != node:
        reject("node_id_mismatch")
    if event.get("request_id") is None or p.get("request_id") is None:
        reject("request_id_required")
    elif dumps(event["request_id"]) != dumps(p["request_id"]):
        reject("request_id_mismatch")
    if not event.get("session_id"):
        reject("session_id_required")
    if not isinstance(node, str) or not isinstance(action, str):
        return result()

    # Fetch context only from this connection, not a previous session or another run.
    auth = store.latest_message(conn, event, ("auth_response", "session_revoked"))
    if not auth or auth["payload"].get("type") == "session_revoked":
        reject("session_not_authorized")
    elif auth["payload"].get("success") is not True or auth["payload"].get("role") != "team":
        reject("session_not_authorized")
    if auth:
        context_ids.append(auth["id"])
    catalogue = store.latest_message(conn, event, ("describe_nodes_response",))
    descriptor = None
    names = {}
    if catalogue:
        context_ids.append(catalogue["id"])
        cp = catalogue["payload"]
        if cp.get("success") is True and isinstance(cp.get("nodes"), list):
            descriptor = next(
                (n for n in cp["nodes"] if isinstance(n, dict) and n.get("node_id") == node), None
            )
    if descriptor is None:
        reject("node_not_in_session_catalogue")
    else:
        controls = descriptor.get("controls", [])
        operators = descriptor.get("operator_controls", [])
        if not isinstance(controls, list) or not isinstance(operators, list):
            permitted = []
            reject("invalid_catalogue")
        else:
            permitted = controls + operators if who == "operator" else controls
        names = {a.lower(): a.lower() for a in permitted if isinstance(a, str)}
        aliases = descriptor.get("control_aliases", {})
        if isinstance(aliases, dict):
            for primary, variants in aliases.items():
                if (
                    isinstance(primary, str)
                    and primary.lower() in names
                    and isinstance(variants, list)
                ):
                    for alias in variants:
                        if isinstance(alias, str):
                            names[alias.lower()] = primary.lower()
        canonical = names.get(action.lower())
        if canonical is None:
            reject("action_not_permitted")
    canonical = ALIASES.get((node, canonical or action.lower()), canonical or action.lower())
    for reason in value_errors(node, canonical, value):
        reject(reason)

    states = {}

    def state_for(target):
        if target in states:
            return states[target]
        snapshot = store.latest_snapshot(conn, event, target)
        if not snapshot:
            reject("state_missing:" + target)
            states[target] = {}
            return {}
        context_ids.append(snapshot["id"])
        state = snapshot["payload"]
        if not isinstance(state, dict):
            state = {}
        age = age_seconds(snapshot["ingested_at"], now)
        observed = snapshot["event"].get("metadata", {}).get("observed_at")
        observed_age = age_seconds(observed, now)
        # Sender observation time prevents a freshly imported backlog from authorizing control.
        if (
            age is None
            or not 0 <= age <= store.max_state_age
            or observed_age is None
            or not -5 <= observed_age <= store.max_state_age
        ):
            reject("state_stale:" + target)
        if state.get("online") is not True or state.get("status") != "OK":
            reject("state_unavailable:" + target)
        frame = snapshot["event"]["payload"]
        tick = frame.get("timestamp")
        updated = state.get("updated_at")
        if type(tick) in (int, float) and type(updated) in (int, float):
            if not 0 <= tick - updated <= store.max_state_age:
                reject("measurement_stale:" + target)
        else:
            reject("measurement_time_unknown:" + target)
        states[target] = state
        return state

    state = state_for(node)
    if not reasons:
        _state_rules(node, canonical, value, state, state_for, reject)

    # Check global/session limits against commands already recorded, not against validation calls.
    recent = store.recent_outbound(conn, event, seconds=120, now=now)
    if sum(age_seconds(e["ingested_at"], now) <= 1 for e in recent) >= 5:
        reject("session_rate_limit")
    if (
        node == "meeting_01"
        and sum(
            e["payload"].get("node_id") == node
            and e["payload"].get("type") == "control"
            and age_seconds(e["ingested_at"], now) <= 10
            for e in recent
        )
        >= 10
    ):
        reject("chat_rate_limit")
    if node == "smart_panel_01" and canonical in ("line_on", "line_off", "reset_protection"):
        line = value.get("line") if isinstance(value, dict) else value
        count = 0
        for e in recent:
            msg = e["payload"]
            val = msg.get("value")
            prior_action = msg.get("action")
            prior_action = (
                names.get(prior_action.lower(), prior_action.lower())
                if isinstance(prior_action, str)
                else None
            )
            if (
                msg.get("node_id") == node
                and prior_action in ("line_on", "line_off", "reset_protection")
                and (val.get("line") if isinstance(val, dict) else val) == line
            ):
                count += 1
        if count >= 3:
            reject("line_switch_rate_limit")
    return result()


def _state_rules(node, action, value, state, state_for, reject):
    if node == "solar_panels_01" and action in ("enable_mppt", "disable_mppt"):
        count = state.get("channels_count")
        if type(count) is not int or value > count:
            reject("mppt_channel_unavailable")
    if node == "fuel_tank_01" and value is not None:
        available = state.get("reserve_l")
        volume, capacity = state.get("volume_l"), state.get("capacity_l")
        if not all(type(x) in (int, float) for x in (available, volume, capacity)):
            reject("fuel_capacity_unknown")
        elif value > min(available, capacity - volume):
            reject("fuel_capacity_exceeded")
    if node in MACHINES:
        if state.get("lockout_remaining_s", 0) > 0:
            reject("machine_lockout")
        if action == "resume":
            reason = state.get("hold_reason")
            if state.get("hold_level") == "ALARM" or reason == "FILAMENT_RUNOUT":
                reject("machine_hold_active")
            elif reason:
                automation = state_for("dome_automation_01")
                interlock = automation.get("interlocks", {}).get(reason, {})
                if not isinstance(interlock, dict) or interlock.get("state") not in (
                    "ARMED",
                    "BLOCKED",
                ):
                    reject("hold_cause_not_cleared")
        if action == "start_job":
            filename = value.get("file_name") if isinstance(value, dict) else value
            files = state.get("files", [])
            names = (
                {
                    f.get("name", f.get("file_name")) if isinstance(f, dict) else f
                    for f in files
                    if isinstance(f, (str, dict))
                }
                if isinstance(files, list)
                else set()
            )
            if filename not in names:
                reject("file_not_in_catalogue")
            if state.get("state") not in ("IDLE", "COMPLETED"):
                reject("machine_not_idle")
            if node == "printer_3d_01" and state.get("bed_clear") is not True:
                reject("printer_bed_not_clear")
        if action == "delete_file" and state.get("file_name") == value:
            reject("file_in_use")
        if action in ("move", "set_zero", "probe_z"):
            if state.get("state") != "IDLE" or state.get("homed") is not True:
                reject("machine_not_idle_or_homed")
        if action == "upload_file" and len(value["content"].encode("utf-8")) > 16 * 1024 * 1024:
            reject("file_too_large")
        if action == "move":
            mode = value.get("mode", "relative")
            for index, axis in enumerate("xyz"):
                if axis not in value:
                    continue
                if node == "printer_3d_01":
                    if mode == "absolute":
                        reject("absolute_printer_limits_unknown")
                    elif abs(value[axis]) > 50:
                        reject("axis_travel_exceeded")
                else:
                    limit = (300, 180, 80)[index]
                    mpos, wco = state.get("mpos_mm"), state.get("wco_mm")
                    coordinates = mpos if mode == "relative" else wco
                    if (
                        not isinstance(coordinates, list)
                        or len(coordinates) != 3
                        or type(coordinates[index]) not in (int, float)
                    ):
                        reject("machine_coordinates_unknown")
                    elif not 0 <= coordinates[index] + value[axis] <= limit:
                        reject("axis_travel_exceeded")
    if node.startswith("thermal_insulation_") and action == "set_lock":
        locked = value.get("locked") if isinstance(value, dict) else value
        if locked is False:
            machine = "printer_3d_01" if node.endswith("01") else None
            if machine and state_for(machine).get("state") not in ("IDLE", "COMPLETED"):
                reject("enclosure_machine_active")
    if node == "smart_panel_01":
        line = value.get("line") if isinstance(value, dict) else value
        lines = state.get("lines", [])
        current = next((x for x in lines if isinstance(x, dict) and x.get("line") == line), None)
        if not current:
            reject("line_state_missing")
        else:
            if current.get("lockout_remaining_s", 0) > 0:
                reject("line_lockout")
            if action == "line_on" and current.get("protection_state") != "ARMED":
                reject("line_protection_active")
    if node == "backup_comms_01" and action == "set_protocol":
        allowed = {
            "SATELLITE": ("IRIDIUM_SBD", "IP"),
            "LORA": ("LORAWAN", "MESHTASTIC"),
            "MESH": ("BATMAN", "OLSR"),
        }
        if value not in allowed.get(state.get("active_channel"), ()):
            reject("protocol_channel_mismatch")
    if node == "dome_automation_01" and action in (
        "reset_interlock",
        "unblock_interlock",
        "block_interlock",
    ):
        interlock = value.get("id") if isinstance(value, dict) else value
        if interlock not in state.get("interlocks", {}):
            reject("unknown_interlock")
