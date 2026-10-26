"""JSON without model coercion, and the single shared envelope validator."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = json.loads((ROOT / "contracts/event.schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


class InvalidEvent(ValueError):
    pass


def dumps(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def loads(text):
    def invalid_constant(_):
        raise ValueError("Non-finite numbers are not JSON")

    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("Duplicate object key")
            obj[key] = value
        return obj

    return json.loads(text, parse_constant=invalid_constant, object_pairs_hook=unique_object)


def validate(event):
    try:
        # Check JSON compatibility even for in-process callers; never stringify arbitrary objects.
        dumps(event)
        error = next(VALIDATOR.iter_errors(event), None)
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidEvent("Not a finite JSON value") from exc
    if error:
        # Do not echo invalid data or secret values into HTTP errors.
        path = "/".join(map(str, error.absolute_path)) or "event"
        raise InvalidEvent(f"Invalid {path}: {error.validator}")
    return {"schema_version": "1.0", **event}
