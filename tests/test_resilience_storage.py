"""Durable recorder and current prompt integration. All HTTP is in-memory."""
import json
import threading
import time

import httpx
from kupol.dome.datalog import DataApiRecorder


def wait_for(predicate, timeout=5):
    until = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < until
        time.sleep(0.01)


def test_event_is_durable_before_delivery_and_append_is_not_lost(tmp_path):
    entered, release = threading.Event(), threading.Event()
    received = {}

    def request(req):
        body = json.loads(req.content)
        entered.set()
        assert release.wait(5)
        for ev in body["events"]:
            received[ev["event_id"]] = ev
        return httpx.Response(200, json={"ok": True})

    http = httpx.Client(base_url="http://mock", transport=httpx.MockTransport(request))
    recorder = DataApiRecorder("http://mock", run_id="offline", state_dir=tmp_path, http=http)
    try:
        first = recorder.event("telemetry", {"unknown": {"nullable": None, "typed": True}})
        assert entered.wait(5)
        assert first in recorder.outbox.read_text(encoding="utf-8")
        second = recorder.event("decision", {"route": "RECORD_ONLY"})
        assert second in recorder.outbox.read_text(encoding="utf-8")
        release.set()
        assert recorder.flush(5)
        assert set(received) == {first, second}
        assert received[first]["payload"] == {"unknown": {"nullable": None, "typed": True}}
        assert not recorder.outbox.read_text(encoding="utf-8").strip()
    finally:
        release.set()
        recorder.close()


def test_db_recovers_without_waiting_for_new_event_and_keeps_same_id(tmp_path):
    online = threading.Event()
    stored = {}

    def request(req):
        if not online.is_set():
            return httpx.Response(503)
        for ev in json.loads(req.content)["events"]:
            stored[ev["event_id"]] = ev
        return httpx.Response(200, json={"ok": True})

    recorder = DataApiRecorder("http://mock", run_id="offline", state_dir=tmp_path,
                               http=httpx.Client(base_url="http://mock", transport=httpx.MockTransport(request)))
    try:
        eid = recorder.event("telemetry", {"co2": 900})
        wait_for(lambda: recorder.last_error)
        assert eid in recorder.outbox.read_text(encoding="utf-8")
        online.set()
        wait_for(lambda: eid in stored)
        assert recorder.flush(5)
        assert list(stored) == [eid]
    finally:
        recorder.close()


def test_restart_replays_existing_spool_with_original_identity(tmp_path):
    failed = httpx.Client(base_url="http://mock", transport=httpx.MockTransport(lambda req: httpx.Response(503)))
    first = DataApiRecorder("http://mock", run_id="old-run", state_dir=tmp_path, http=failed)
    eid = first.event("decision", {"severity": "warning"})
    first.close()
    stored = []

    def accept(req):
        stored.extend(json.loads(req.content)["events"])
        return httpx.Response(200, json={"ok": True})

    second = DataApiRecorder("http://mock", run_id="new-run", state_dir=tmp_path,
                             http=httpx.Client(base_url="http://mock", transport=httpx.MockTransport(accept)))
    try:
        wait_for(lambda: bool(stored))
        assert stored[0]["event_id"] == eid and stored[0]["run_id"] == "old-run"
        assert second.flush(5)
    finally:
        second.close()


def test_corrupt_spool_remains_intact_and_reports_storage_failure(tmp_path):
    spool = tmp_path / "data_api_outbox.jsonl"
    spool.write_text('{"torn_record":', encoding="utf-8")
    http = httpx.Client(base_url="http://mock", transport=httpx.MockTransport(lambda req: httpx.Response(200)))
    recorder = DataApiRecorder("http://mock", run_id="offline", state_dir=tmp_path, http=http)
    try:
        wait_for(lambda: bool(recorder.storage_error))
        assert spool.read_text(encoding="utf-8") == '{"torn_record":'
        assert not recorder.flush(0.1)
    finally:
        recorder.close()
