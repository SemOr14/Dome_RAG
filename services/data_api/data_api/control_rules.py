"""Parameter rules transcribed from 01_API.md §§5–8, never from observed data types.

The live describe_nodes catalogue grants permissions. These schemas only restrict values;
an unknown action is retained in storage but fails local validation.
"""

from jsonschema import Draft202012Validator

NONE = {"type": "null"}
TEXT = {"type": "string", "minLength": 1}
BOOL = {"type": "boolean"}


def number(low=None, high=None, integer=False):
    result = {"type": "integer" if integer else "number"}
    if low is not None:
        result["minimum"] = low
    if high is not None:
        result["maximum"] = high
    return result


def enum(*values):
    return {"enum": list(values)}


def obj(properties, required=(), at_least_one=False):
    result = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        result["required"] = list(required)
    if at_least_one:
        result["minProperties"] = 1
    return result


def either(*schemas):
    return {"anyOf": list(schemas)}


def scalar_or_object(field, schema):
    return either(schema, obj({field: schema}, [field]))


RULES = {}
ALIASES = {}


def add(node, actions, schema=NONE, aliases=()):
    for action in actions.split():
        RULES[node, action] = schema
    for alias in aliases:
        ALIASES[node, alias] = actions.split()[0]


add("solar_panels_01", "enable_mppt disable_mppt", number(1, integer=True))
add("solar_panels_01", "enable_mppt_1 disable_mppt_1 enable_all_mppt disable_all_mppt")
add("solar_inverter_01", "set_mode_hybrid", aliases=["hybrid"])
add("solar_inverter_01", "set_mode_grid_only", aliases=["grid_only"])
add(
    "solar_inverter_01",
    "enable_mppt disable_mppt start_cell_balancing cancel_balancing reset_error",
)
add("solar_inverter_01", "set_fan", enum("AUTO", "ON", "OFF"))
add("wind_turbine_01", "turn_on turn_off")
add("dizel_1", "turn_on turn_off preheat")
add("fuel_tank_01", "transfer_fuel", either(NONE, {"type": "number", "exclusiveMinimum": 0}))
add("smart_panel_01", "line_on line_off", scalar_or_object("line", number(1, 8, True)))
add("smart_panel_01", "reset_protection reset_line_error", number(1, 8, True))
for machine in ("printer_3d_01", "cnc_01"):
    add(machine, "home resume")
    add(machine, "start_job", scalar_or_object("file_name", TEXT))
    axes = {axis: number() for axis in "xyz"}
    move = obj({**axes, "mode": enum("relative", "absolute"), "feedrate": number(10, 5000)})
    move["anyOf"] = [{"required": [axis]} for axis in "xyz"]
    add(machine, "move", move, aliases=["jog"])
add(
    "printer_3d_01", "start_job", RULES["printer_3d_01", "start_job"], ["start_print", "print_file"]
)
add("printer_3d_01", "set_speed", scalar_or_object("pct", number(50, 200)), ["set_print_speed"])
add(
    "printer_3d_01",
    "upload_file",
    obj({"file_name": TEXT, "content": {"type": "string"}}, ["file_name", "content"]),
    ["upload"],
)
add("printer_3d_01", "delete_file", TEXT)
ALIASES["printer_3d_01", "move_axes"] = "move"
ALIASES["cnc_01", "$h"] = "home"
ALIASES["cnc_01", "cycle_start"] = "resume"
ALIASES["cnc_01", "~"] = "resume"
axes_schema = either(
    enum("xyz", "xy", "z"),
    {
        "type": "array",
        "items": enum("x", "y", "z"),
        "minItems": 1,
        "maxItems": 3,
        "uniqueItems": True,
    },
)
add("cnc_01", "set_zero", either(NONE, axes_schema, obj({"axes": axes_schema}, ["axes"])), ["zero"])
add(
    "cnc_01",
    "probe_z",
    either(
        NONE,
        obj(
            {
                "distance": number(0.1, 30),
                "feed": number(1, 300),
                "plate_thickness": number(0, 30),
                "retract": number(0, 20),
                "second_pass_feed": number(1, 100),
                "latch_distance": number(0.1, 5),
            }
        ),
    ),
)
add("equipment_cooling_01", "turn_on turn_off reset_alarm")
add("equipment_cooling_01", "set_speed", number(0, 100))
for node, fans in (("thermal_insulation_01", 5), ("thermal_insulation_02", 1)):
    add(
        node,
        "set_fan_pwm",
        obj({"fan": number(0, fans, True), "pwm": number(0, 255, True)}, ["fan", "pwm"]),
    )
add(
    "thermal_insulation_01",
    "set_lock",
    obj({"lock": number(0, 1, True), "locked": BOOL}, ["lock", "locked"]),
    ["lock"],
)
add("thermal_insulation_01", "set_vacuum", scalar_or_object("mode", number(0, 1, True)), ["vacuum"])
add("thermal_insulation_01", "set_poll_interval", number(50, 5000, True))
add(
    "thermal_insulation_02",
    "set_relay_fan_level",
    scalar_or_object("level", number(0, 10, True)),
    ["set_relay_level"],
)
add("thermal_insulation_02", "set_lock", scalar_or_object("locked", BOOL), ["lock"])
for node in ("climate_sensor_02", "climate_sensor_03"):
    add(node, "recalibrate", aliases=["calibrate"])
add("fume_extraction_01", "turn_on turn_off reset_alarm")
add("supply_ventilation_01", "turn_on turn_off reset_alarm")
add("supply_ventilation_01", "set_speed", number(10, 100))
add("supply_ventilation_01", "set_mode", enum("RECIRCULATION", "FRESH_AIR", "MIXED"))
add("dome_sealing_01", "open_valves", aliases=["open"])
add("dome_sealing_01", "close_valves", aliases=["close"])
add("dome_sealing_01", "reset_error")
add("dome_sealing_01", "seal_zone", either(NONE, enum("MAIN", "FABLAB", "LIVING", "STORAGE")))
for node in ("smoke_detector_01", "smoke_detector_02", "radiation_sensor_01", "chem_sensor_01"):
    add(node, "reset_alarm")
for node in ("radiation_sensor_01", "chem_sensor_01"):
    add(node, "set_thresholds", obj({"warning": number(), "alarm": number()}, at_least_one=True))
add(
    "fire_suppression_01",
    "manual_start_zone",
    enum("FABLAB", "LIVING", "STORAGE", "POWER_ROOM"),
    ["start_zone"],
)
add("fire_suppression_01", "stop reset_alarm block_automation unblock_automation")
add(
    "access_control_01",
    "lock_door unlock_door",
    enum("MAIN_AIRLOCK", "FABLAB", "STORAGE", "SERVER_ROOM"),
)
add("access_control_01", "lockdown", BOOL, ["force_lock"])
add("access_control_01", "reset_alarm")
add("seysmo_01", "set_threshold", number(0.01, 1000))
add("water_pump_01", "turn_on turn_off reset_alarm")
add("water_pump_01", "set_power_limit", number(20, 100), ["limit_power"])
add("water_filter_01", "switch_to_reserve", aliases=["switch_filter"])
add("water_filter_01", "force_flush", aliases=["flush"])
add("water_filter_01", "reset_warning")
add("radio_01", "set_frequency", number(430, 440))
add("radio_01", "set_protocol", enum("AX25", "FSK", "LORA", "DMR"))
add("backup_comms_01", "switch_channel", enum("SATELLITE", "LORA", "MESH"))
add(
    "backup_comms_01",
    "set_protocol",
    enum("IRIDIUM_SBD", "IP", "LORAWAN", "MESHTASTIC", "BATMAN", "OLSR"),
)
add("backup_comms_01", "set_traffic_priority", enum("NORMAL", "TELEMETRY", "EMERGENCY"))
add(
    "edge_compute_01",
    "restart_service",
    enum("agent_runtime", "telemetry_collector", "vision_inference", "api_gateway"),
)
add("edge_compute_01", "limit_load", number(10, 100), ["set_load_limit"])
add("edge_compute_01", "sync_time", aliases=["ntp_sync"])
add("edge_compute_01", "reboot")
add(
    "dome_automation_01",
    "set_data_trust",
    obj(
        {
            "node": TEXT,
            "param": TEXT,
            "trust": enum("UNRELIABLE", "FAILED", "TRUSTED"),
            "line": number(1, 8, True),
        },
        ["node", "param", "trust"],
    ),
)
add(
    "dome_automation_01",
    "set_primary_sensor",
    enum("climate_sensor_01", "climate_sensor_02", "climate_sensor_03"),
)
add("dome_automation_01", "reset_interlock", TEXT, ["release_interlock"])
add("dome_automation_01", "unblock_interlock", TEXT)
add(
    "dome_automation_01",
    "block_interlock",
    obj(
        {"id": TEXT, "minutes": {"type": "number", "exclusiveMinimum": 0, "maximum": 30}},
        ["id", "minutes"],
    ),
)
add(
    "dome_automation_01",
    "acknowledge",
    either(enum("all"), obj({"event_id": TEXT}, ["event_id"])),
    ["ack"],
)
add("dome_automation_01", "set_ventilation_auto set_diesel_autostart", BOOL)
add(
    "meeting_01",
    "post_message",
    either(
        {"type": "string", "minLength": 1, "maxLength": 4000},
        obj(
            {
                "text": {"type": "string", "minLength": 1, "maxLength": 4000},
                "to": either(NONE, {"type": "string", "maxLength": 64}),
                "name": {"type": "string"},
            },
            ["text"],
        ),
    ),
    ["post", "send_message"],
)


def value_errors(node, action, value):
    schema = RULES.get((node, action))
    if schema is None:
        return ["parameter_rule_unavailable"]
    return ["invalid_value"] if not Draft202012Validator(schema).is_valid(value) else []
