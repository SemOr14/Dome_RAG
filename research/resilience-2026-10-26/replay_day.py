"""Chronological diagnostic replay; no network, commands, LLM or future records."""
import collections
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent" / "src"))
from kupol.dome.checks import assess_co2, assess_condensate, assess_printer  # noqa: E402
from kupol.dome.config import load_dome_config  # noqa: E402
from kupol.dome.state import DomeState  # noqa: E402


def replay():
    source = ROOT / "Logs" / "2026-10-14" / "telemetry.jsonl"
    state = DomeState()
    cfg = load_dome_config(ROOT / "agent" / "dome.yaml")
    counts = collections.defaultdict(collections.Counter)
    samples = 0
    for index, line in enumerate(source.read_text(encoding="utf-8").splitlines()):
        record = json.loads(line)
        state.update({"nodes": record["nodes"], "game_time": record["dome"]}, "team", index * 60 + 1)
        counts["co2"][assess_co2(state, cfg.checks).kind] += 1
        counts["condensate"][assess_condensate(state, cfg.checks).kind] += 1
        counts["webhook_discrepancy"][str(assess_printer(state).webhook_lies)] += 1
        samples += 1
    result = {
        "upstream_base": "2e0c0350952ea251a98a35c1597433e1acac2f99",
        "mode": "modified local code, chronological pure diagnostics only",
        "source": "Logs/2026-10-14/telemetry.jsonl",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "samples": samples,
        "sample_counts": {k: dict(v) for k, v in counts.items()},
        "limitations": ["No closed-loop or counterfactual effects", "No future logbook supplied to current decisions",
                        "Historical delays and types preserved", "Unknown is not proof of a real or false crisis",
                        "No hardware or actual DeepSeek calls"],
    }
    (Path(__file__).parent / "replay_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["sample_counts"], ensure_ascii=False))


if __name__ == "__main__":
    replay()
