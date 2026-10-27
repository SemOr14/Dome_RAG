"""Native sensor reads in the real LlmBrain/Runner stack, using only the local emulator."""
import asyncio
import json

from tests.dome_fake.world import World
from tests.test_dome_llm_brain import LlmHarness, FakeLLM, QUIET
from llm_agent.llm_client import LLMResult


def test_brain_reads_sensors_in_advisory_mode_without_sending_controls(tmp_path):
    class ReaderLLM(FakeLLM):
        def __init__(self):
            super().__init__(lambda trigger: QUIET)
            self.requested = False
            self.results = []
        def chat(self, messages, **kwargs):
            if not self.requested:
                self.requested = True
                return LLMResult(ok=True, text=None, model='fake', tool_calls=[{
                    'id': 'read-' + str(i), 'type': 'function', 'function': {'name': 'read_sensor',
                    'arguments': json.dumps({'node_id': 'climate_sensor_02', 'fields': ['co2_ppm']})}} for i in range(4)])
            self.results = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
            return LLMResult(ok=True, text=json.dumps(QUIET), model='fake')
    async def scenario():
        llm = ReaderLLM()
        async with LlmHarness(tmp_path, World(), llm) as harness:
            harness.runner.gate.agent_control = False
            harness.brain.operator_request('Проверь CO₂')
            await harness.step(2)
            assert sum(r['ok'] for r in llm.results) == 3
            assert llm.results[3]['error'] == 'limit_exceeded'
            assert all('co2_ppm' in r['readings'] for r in llm.results[:3])
            assert not harness.sent()
    asyncio.run(scenario())
