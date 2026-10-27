"""Read historical JSONL only. Usage: python replay.py /path/to/WhiteDots_Dome_RAG/Logs."""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from llm_agent.monitoring import NumericMonitor


def replay(root):
    days = []
    for path in sorted(root.glob('*/telemetry.jsonl')):
        monitor = NumericMonitor()
        thresholds = Counter()
        frames = 0
        digest = hashlib.sha256()
        with path.open('rb') as lines:
            for line in lines:
                digest.update(line)
                frame = json.loads(line)
                nodes = frame.get('nodes') or (frame.get('data') or {}).get('nodes') or {}
                stamp = max((b.get('updated_at', 0) for b in nodes.values() if isinstance(b, dict) and type(b.get('updated_at')) in (int, float)), default=0)
                findings = monitor.observe(nodes, stamp)
                frames += 1
                for finding in findings:
                    if finding['note'].startswith('Вне рабочего диапазона'):
                        thresholds[f"{finding['node']}.{finding['field']}"] += 1
        days.append({'day': path.parent.name, 'frames': frames, 'sha256': digest.hexdigest(),
                     'numeric_fields': len(monitor.baselines), 'distinct_numeric_values_checked': monitor.values_seen,
                     'threshold_frames': dict(thresholds)})
        print(path.parent.name, frames, 'frames', flush=True)
    return {'source_repository': 'https://github.com/WhiteDots/Dome_RAG',
            'source_commit': '5e1ec4854ddcccb12efc5f0b4a9bfd94fa190438',
            'mode': 'historical replay; no ground truth, no control, no LLM calls', 'days': days}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('logs', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(replay(args.logs), ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
