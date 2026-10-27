"""Current operator-request endpoint: persistent identity, retry and cold start."""
from uuid import uuid4

from fastapi.testclient import TestClient

from integration.server import create_app


def test_operator_request_is_recorded_once_and_conflict_is_not_silently_dropped(tmp_path):
    app = create_app(tmp_path / "web.sqlite3", token="")
    with TestClient(app) as client:
        body = {"operation_id": str(uuid4()), "text": "Проверь CO2 перед продолжением фрезера"}
        first = client.post("/control/chat", json=body)
        assert first.status_code == 200 and first.json()["status"] == "not_connected"
        assert client.post("/control/chat", json=body).json()["status"] == "duplicate"
        assert client.post("/control/chat", json={**body, "text": "Другая задача"}).status_code == 409
        records = app.state.store.events(event_type="operator_request")["items"]
        assert len(records) == 1 and records[0]["source"] == "operator"
        assert records[0]["payload"] == {"text": body["text"]}
        assert app.state.hub.client is None
        assert not app.state.store.events(event_type="control")["items"]


def test_same_run_after_server_restart_preserves_request_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_API_RUN_ID", "offline-test-stable-run")
    path = tmp_path / "restart.sqlite3"
    body = {"operation_id": str(uuid4()), "text": "Сверь состояние задания"}
    with TestClient(create_app(path, token="")) as client:
        assert client.post("/control/chat", json=body).json()["status"] == "not_connected"
    app = create_app(path, token="")
    with TestClient(app) as client:
        assert client.post("/control/chat", json=body).json()["status"] == "duplicate"
        [record] = app.state.store.events(event_type="operator_request")["items"]
        assert record["run_id"] == "offline-test-stable-run"
