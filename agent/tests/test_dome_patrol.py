import asyncio
import json

from kupol.dome.agentlog import AgentLog
from kupol.dome.config import DomeConfig
from kupol.dome.env import Settings
from kupol.dome.runner import Runner


def test_patrol_runs_on_idle_without_llm_or_telemetry_and_does_not_send(tmp_path):
    now = [1000.0]
    log = AgentLog(tmp_path, run_id='patrol')
    runner = Runner(Settings(dome_url='ws://unused', dome_key='', deepseek_key=''), DomeConfig(), log, clock=lambda: now[0])
    for t in (1119, 1120, 1120, 1239, 1240, 1600):
        now[0] = t
        asyncio.run(runner._idle())
    log.close()
    records = [json.loads(line) for line in log.path.read_text().splitlines()]
    patrols = [r for r in records if 'patrol' in r]
    assert len(patrols) == 3  # missed periods coalesce, no burst of stale inspections
    assert [p['patrol']['number'] for p in patrols] == [1, 2, 3]
    assert all(p['metrics']['fresh'] is False for p in patrols)
    assert not any(r['kind'] == 'action' for r in records)
    assert runner.state.updates == 0
