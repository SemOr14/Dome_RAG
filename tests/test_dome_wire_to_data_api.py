"""Весь обмен агентной системы с куполом попадает в БД: эмулятор купола (agent/tests) → DomeClient → data_api."""

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for path in (ROOT / "services" / "data_api", ROOT / "agent" / "src", ROOT / "agent"):
    sys.path.insert(0, str(path))
pytest.importorskip("websockets")
fastapi_testclient = pytest.importorskip("fastapi.testclient")

from data_api.app import create_app  # noqa: E402
from kupol.dome.agentlog import AgentLog  # noqa: E402
from kupol.dome.client import DomeClient  # noqa: E402
from kupol.dome.config import RuntimeCfg  # noqa: E402
from kupol.dome.datalog import DataApiRecorder  # noqa: E402
from tests.dome_fake.server import FakeDome  # noqa: E402

from llm_agent.storage.data_api import DataApiClient, journal_record  # noqa: E402

TOKEN = "test-service-secret"
RUN = "run-wire"
CFG = RuntimeCfg(reconnect_min_s=1.0, reconnect_max_s=2.0, request_timeout_s=2.0, request_rate_per_s=5.0)


async def until(pred, timeout=8.0):
    end = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < end, "условие не выполнено вовремя"
        await asyncio.sleep(0.02)


def test_sent_commands_and_dome_responses_are_stored(tmp_path):
    with fastapi_testclient.TestClient(create_app(tmp_path / "db.sqlite3", token=TOKEN)) as http:
        recorder = DataApiRecorder("http://testserver", TOKEN, RUN, tmp_path / "state", http=http)
        log = AgentLog(tmp_path / "logs", run_id="log-1")

        async def scenario():
            async with FakeDome(on_control=lambda msg: {"success": True, "mode": "virtual"}) as dome:
                client = DomeClient(dome.url, dome.key, CFG, recorder=recorder)
                log.sink = lambda kind, rec: recorder.event(f"agent_{kind}", rec, session_id=client.session_id)
                task = asyncio.create_task(client.run())
                await until(lambda: client.catalog_message is not None)
                assert await client.wait_telemetry(3)
                resp = await client.control("printer_3d_01", "home")
                log.write("action", request={"type": "control", "node_id": "printer_3d_01", "action": "home"},
                          response={"success": resp["success"], "request_id": resp["request_id"]})
                client.stop()
                await asyncio.wait_for(task, 5)
                return dome.key, client.session_id, resp["request_id"]

        key, session, request_id = asyncio.run(scenario())
        assert recorder.flush(10), recorder.last_error
        recorder.close()

        events = DataApiClient("http://testserver", TOKEN, http=http)
        stored = list(events.pages("/v1/events", "after_id", "next_after_id", run_id=RUN, limit=1000))
        types = {(e["direction"], e["event_type"]) for e in stored}
        assert {("outbound", "auth"), ("inbound", "auth_response"), ("outbound", "describe_nodes"),
                ("inbound", "describe_nodes_response"), ("inbound", "telemetry"), ("outbound", "control"),
                ("inbound", "control_response"), ("internal", "connection"), ("internal", "agent_action")} <= types

        assert key not in json.dumps(stored, ensure_ascii=False)  # ключ купола не хранится
        auth = next(e for e in stored if e["event_type"] == "auth")
        assert auth["payload"]["key"] in ("***", "[REDACTED]")  # маскирует агент, сервер — повторно
        telemetry = next(e for e in stored if e["event_type"] == "telemetry")
        assert telemetry["metadata"]["observed_at"]

        # Команда и ответ купола связаны run/session/request_id; сервер видит результат команды
        exchange = http.get("/v1/exchanges", params={"run_id": RUN, "session_id": session,
                                                     "request_id": json.dumps(request_id)}).json()
        assert exchange["state"] == "complete"
        assert exchange["requests"][0]["payload"]["action"] == "home"
        assert exchange["responses"][0]["payload"]["success"] is True
        control = next(e for e in stored if e["event_type"] == "control")
        assert http.get(f"/v1/controls/{control['event_id']}").json()["upstream_status"] == "accepted"

        # Действие агента читается журналом LLM-слоя
        action = next(e for e in stored if e["event_type"] == "agent_action")
        rec = journal_record(action)
        assert rec["node_id"] == "printer_3d_01" and rec["action"] == "home" and rec["success"] is True


def test_recorder_keeps_events_while_db_is_down(tmp_path):
    recorder = DataApiRecorder("http://127.0.0.1:9", run_id=RUN, state_dir=tmp_path, timeout_s=0.3)
    event_id = recorder.frame("outbound", {"type": "control", "request_id": "1", "node_id": "cnc_01", "action": "home"}, "s")
    assert not recorder.flush(5)
    recorder.close()
    saved = [json.loads(line) for line in recorder.outbox.read_text(encoding="utf-8").splitlines()]
    assert [e["event_id"] for e in saved] == [event_id]
