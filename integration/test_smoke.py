"""Three focused offline checks; no equipment, cloud LLM or historical imports."""

import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from integration.server import create_app


def test_empty_panel_auth_and_http_audit(tmp_path):
    app = create_app(tmp_path / "empty.sqlite3", token="local-test-token")
    with TestClient(app) as client:
        assert client.get("/ui/").status_code == 200
        assert client.get("/control/status").status_code == 401
        client.headers["Authorization"] = "Bearer local-test-token"
        status = client.get("/control/status").json()
        assert status["connected"] is False and status["connecting"] is False
        assert client.get("/v1/events").json()["items"] == []
        assert client.get("/control/overview").json()["nodes"] == []
        refused = client.post(
            "/control/disconnect", headers={"Origin": "https://unrelated.test"}
        )
        assert refused.status_code == 403
        audit = app.state.store.http_exchange(int(refused.headers["x-audit-id"]))
        assert audit["response"]["status"] == 403


def test_operator_rejection_is_saved_and_retries_do_not_send(tmp_path):
    app = create_app(tmp_path / "commands.sqlite3", token="")
    with TestClient(app) as client:
        body = {
            "operation_id": str(uuid4()),
            "node_id": "smart_panel_01",
            "action": "line_off",
            "value": 8,
        }
        first = client.post("/control/commands", json=body)
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "rejected"
        assert client.post("/control/commands", json=body).json() == first.json()
        assert (
            client.post("/control/commands", json={**body, "value": 7}).status_code
            == 409
        )
        command = app.state.store.control(body["operation_id"])
        assert command["event"]["source"] == "operator"
        assert command["validation"]["accepted"] is False
        assert not app.state.store.events(direction="outbound")["items"]


def test_current_llm_prompt_response_and_decision_use_storage_api(
    tmp_path, monkeypatch
):
    import integration.runtime as runtime
    from llm_agent.llm_client import LLMResult
    from llm_agent.storage.data_api import DataApiClient

    monkeypatch.setenv("DEEPSEEK_API_KEY", "mock-only-no-cloud-request")
    app = create_app(tmp_path / "llm.sqlite3", token="")
    with TestClient(app) as client:

        def transport(request):
            response = client.request(
                request.method,
                request.url.raw_path.decode(),
                content=request.content,
                headers=dict(request.headers),
            )
            return httpx.Response(
                response.status_code,
                content=response.content,
                headers={"Content-Type": "application/json"},
            )

        monkeypatch.setattr(
            runtime,
            "DataApiClient",
            lambda *a, **kw: DataApiClient(
                "http://storage",
                http=httpx.Client(
                    base_url="http://storage", transport=httpx.MockTransport(transport)
                ),
            ),
        )
        import llm_agent.agent as llm_module

        monkeypatch.setattr(
            llm_module,
            "DeepSeekClient",
            lambda settings: SimpleNamespace(
                chat=lambda messages, **kwargs: LLMResult(
                    ok=True,
                    text=json.dumps(
                        {
                            "situation": "Нет актуальных измерений; управление не предлагается",
                            "actions": [],
                        }
                    ),
                    model="mock-only",
                    attempts=1,
                )
            ),
        )
        preview = client.post("/control/prompt", json={"trigger": "Проверка промпта"})
        assert preview.status_code == 200, preview.text
        assert len(preview.json()["messages"]) == 2
        result = client.post(
            "/control/decide", json={"trigger": "Оцени текущее состояние"}
        )
        assert result.status_code == 200, result.text
        assert result.json()["ok"] is True
        types = {e["event_type"] for e in app.state.store.events()["items"]}
        assert {"prompt_preview", "llm_request", "llm_response", "decision"} <= types
        assert app.state.hub.client is None
