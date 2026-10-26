"""Atomic, deterministic offline JSONL import. Does not import or start agent code."""

import argparse
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .codec import InvalidEvent, loads, validate
from .store import Conflict, Store


def import_file(
    store,
    path,
    *,
    run_id=None,
    origin=None,
    input_id=None,
    session_id=None,
    mode="history",
    strict=False,
):
    path = Path(path)
    input_id = input_id or path.as_posix()
    if mode == "history" and (
        origin not in ("historical", "simulator", "legacy")
        or not run_id
        or not run_id.startswith(origin + "/")
    ):
        raise ValueError("Use origin historical/simulator/legacy and run_id '<origin>/...'")
    count = created = errors = 0
    # A file is one transaction, including derived snapshots and control checks.
    with store.transaction() as conn, path.open(encoding="utf-8-sig", newline="") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                record = loads(line)
            except (ValueError, RecursionError):
                if strict or mode == "envelope":
                    raise InvalidEvent(f"Invalid JSON on line {line_number}") from None
                record = None
                errors += 1
                event_type = "import_error"
            else:
                event_type = "record"
                if isinstance(record, dict):
                    kind = record.get("kind", record.get("type", "record"))
                    if isinstance(kind, str) and 0 < len(kind) <= 200:
                        event_type = kind
                if path.name == "telemetry.jsonl" and event_type == "record":
                    event_type = "telemetry"
            if mode == "envelope":
                event = record
            else:
                p = record if isinstance(record, dict) else {}
                timestamp = p.get("ts", p.get("timestamp"))
                event = {
                    "event_id": str(
                        uuid5(
                            NAMESPACE_URL,
                            f"dome-jsonl:{run_id}:{session_id}:{input_id}:{line_number}",
                        )
                    ),
                    "run_id": run_id,
                    "session_id": session_id,
                    "source": "import",
                    "channel": "jsonl",
                    "direction": "internal",
                    "event_type": event_type,
                    "payload": record,
                    "raw_text": line,
                    "source_timestamp": str(timestamp) if timestamp is not None else None,
                    "metadata": {"data_origin": origin, "input_id": input_id, "line": line_number},
                }
                if isinstance(p.get("node_id"), str):
                    event["node_id"] = p["node_id"]
                if type(p.get("request_id")) in (str, int, float) or p.get("request_id") is None:
                    event["request_id"] = p.get("request_id")
            item = store._append(conn, store.redactor.value(validate(event)))
            count += 1
            created += item["created"]
    return {
        "records": count,
        "created": created,
        "duplicates": count - created,
        "import_errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--db", required=True)
    parser.add_argument("--mode", choices=("history", "envelope"), default="history")
    parser.add_argument("--origin", choices=("historical", "simulator", "legacy"))
    parser.add_argument("--run-id")
    parser.add_argument("--session-id")
    parser.add_argument("--input-id", help="Stable relative path/identity; keep on retries")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    from .codec import dumps

    try:
        result = import_file(
            Store(args.db),
            args.file,
            run_id=args.run_id,
            origin=args.origin,
            input_id=args.input_id,
            session_id=args.session_id,
            mode=args.mode,
            strict=args.strict,
        )
    except (ValueError, InvalidEvent, Conflict, OSError) as exc:
        print(f"Import failed ({type(exc).__name__}); transaction rolled back", file=sys.stderr)
        return 1
    print(dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
