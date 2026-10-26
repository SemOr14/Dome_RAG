"""One local HTTP origin: storage API, isolated agent runtime and a new web panel."""

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "services/data_api", ROOT / "agent/src", ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from data_api.app import create_app as storage_app  # noqa: E402
from data_api.codec import loads  # noqa: E402
from kupol.dome.state import flatten_numbers, split_path  # noqa: E402

from integration.runtime import Hub  # noqa: E402


class CommandIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    node_id: str = Field(min_length=1, max_length=200)
    action: str = Field(min_length=1, max_length=200)
    value: Any = None
    reason: str = Field(default="Команда человека из веб-панели", max_length=2000)
    confirmed: bool = False  # галка подтверждения: Gate не применяет политику агента и защитные правила


class AnalysisIn(BaseModel):
    trigger: str = Field(min_length=1, max_length=4000)


class ExecuteIn(BaseModel):
    operation_id: UUID
    action_index: int = Field(ge=0, le=1000)


class ChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    text: str = Field(min_length=1, max_length=2000)


class ToggleIn(BaseModel):
    enabled: bool


class AutoIn(BaseModel):
    enabled: bool
    interval_s: int = Field(default=180, ge=30, le=3600)


def create_app(db_path=None, token=None):
    from llm_agent.config import Settings as LLMSettings

    # Load the common .env before storage freezes its auth/database configuration.
    LLMSettings.from_env()
    # Empty on first start. No archive/sample import, no WebSocket connection on startup.
    db_path = db_path or os.getenv("DATA_API_DB", str(ROOT / "state/data-api.sqlite3"))
    app = storage_app(db_path, token)
    from integration.charts import ChartFeed

    chart_feed = ChartFeed(app.state.store)
    hub = Hub(app.state.store)
    app.state.hub = hub
    secrets = [hub.settings.dome_key, hub.settings.deepseek_key]
    app.state.store.redactor.secrets = (
        *app.state.store.redactor.secrets,
        *(x for x in secrets if x),
    )

    @asynccontextmanager
    async def lifespan(_app):
        yield
        # Shutdown must close the owned client even if the user did not click Disconnect.
        if hub.task and not hub.task.done():
            await hub.disconnect()

    app.router.lifespan_context = lifespan

    @app.middleware("http")
    async def origin_guard(request: Request, call_next):
        # Browser mutations must originate on this panel, never from an unrelated site.
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
            from urllib.parse import urlsplit

            own = urlsplit(str(request.base_url))
            supplied = urlsplit(origin)
            if (supplied.scheme, supplied.netloc) != (own.scheme, own.netloc):
                from fastapi.responses import JSONResponse

                return JSONResponse({"detail": "Посторонний Origin"}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith("/ui"):
            # Браузер должен сверять файлы панели при каждой загрузке: иначе после обновления кода
            # он продолжает показывать старую разметку событий из своего кэша
            response.headers["Cache-Control"] = "no-cache"
        return response

    # Keep the durable HTTP auditor outside the Origin guard as well.
    from data_api.app import AuditMiddleware

    app.user_middleware.sort(
        key=lambda middleware: 0 if middleware.cls is AuditMiddleware else 1
    )

    @app.get("/control/status")
    def status():
        return hub.status()

    @app.post("/control/connect")
    async def connect():
        return await hub.connect()

    @app.post("/control/disconnect")
    async def disconnect():
        return await hub.disconnect()

    @app.post("/control/commands")
    async def command(body: CommandIn):
        return await hub.command(
            **body.model_dump(exclude={"operation_id"}),
            operation_id=str(body.operation_id),
        )

    @app.get("/control/chat")
    def chat(run_id: str | None = None):
        return hub.chat(run_id or hub.run_id)

    @app.post("/control/chat")
    async def chat_send(body: ChatIn):
        # Пользователь панели — Оператор: его сообщение — требование LLM-агенту, ответ агента — рекомендация
        return await hub.request(str(body.operation_id), body.text)

    @app.post("/control/prompt")
    async def prompt(body: AnalysisIn):
        return await hub.analyze(body.trigger, prompt_only=True)

    @app.post("/control/decide")
    async def decide(body: AnalysisIn):
        return await hub.analyze(body.trigger)

    @app.post("/control/agent-control")
    def agent_control(body: ToggleIn):
        return hub.set_agent_control(body.enabled)

    @app.post("/control/auto")
    async def auto(body: AutoIn):
        return await hub.set_auto(body.enabled, body.interval_s)

    @app.post("/control/decisions/{event_id}/execute")
    async def execute(event_id: UUID, body: ExecuteIn):
        decision = app.state.store.event(str(event_id))
        if (
            not decision
            or decision["run_id"] != hub.run_id
            or decision["event_type"] != "decision"
            or decision["source"] != "agent"
        ):
            raise HTTPException(404, "Решение текущего агента не найдено")
        actions = decision["payload"].get("proposed_controls", [])
        if body.action_index >= len(actions):
            raise HTTPException(422, "Действие не найдено")
        action = actions[body.action_index]
        if action.get("accepted") is not True:
            raise HTTPException(409, "Действие отклонено LLM-слоем")
        # One execution attempt per proposal, even across reloads/clients and HTTP retries.
        operation_id = str(
            uuid5(NAMESPACE_URL, f"decision-action:{event_id}:{body.action_index}")
        )
        return await hub.command(
            operation_id=operation_id,
            node_id=action["node_id"],
            action=action["action"],
            value=action.get("value"),
            reason=decision["payload"].get("summary", "Решение LLM"),
            decision_event_id=str(event_id),
        )

    @app.get("/control/overview")
    def overview(run_id: str | None = None):
        store = app.state.store
        selected = run_id or hub.run_id
        with store.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE run_id=? AND event_type!='telemetry' "
                "ORDER BY id DESC LIMIT 300",
                (selected,),
            ).fetchall()
            controls = conn.execute(
                "SELECT e.event_id FROM control_checks c JOIN events e ON e.id=c.event_row_id "
                "WHERE e.run_id=? ORDER BY e.id DESC LIMIT 30",
                (selected,),
            ).fetchall()
            count = conn.execute(
                "SELECT count(*) FROM events WHERE run_id=?", (selected,)
            ).fetchone()[0]
        catalogue = None
        with store.connection() as conn:
            candidates = conn.execute(
                "SELECT body_json FROM events WHERE run_id=? AND event_type='describe_nodes_response' "
                "AND source='dome' AND direction='inbound' ORDER BY id DESC LIMIT 20",
                (selected,),
            ).fetchall()
            for row in candidates:
                frame = loads(row["body_json"])
                if (
                    isinstance(frame["payload"], dict)
                    and frame["payload"].get("success") is True
                ):
                    catalogue = frame["payload"].get("nodes", [])
                    break
        from data_api.store import decode

        nodes = store.latest_telemetry(selected)["items"]
        return {
            "runtime": hub.status(),
            "run_id": selected,
            "event_count": count,
            "nodes": nodes,
            "catalogue": catalogue or [],
            "events": [decode(r) for r in rows],
            "controls": [store.control(r["event_id"]) for r in controls],
        }

    @app.get("/control/charts")
    def charts(run_id: str):
        return chart_feed.read(run_id)

    @app.get("/control/series")
    def series(
        run_id: str, node_id: str, field: str, limit: int = Query(240, ge=1, le=1000),
        source: str | None = None, session_id: str | None = None, channel: str | None = None
    ):
        store = app.state.store
        tokens = split_path(field)
        points = []
        stream_filter = " AND e.source=? AND e.session_id IS ? AND e.channel=?" if source is not None else ""
        stream_args = (source, session_id, channel or "websocket") if source is not None else ()
        with store.connection() as conn:
            rows = conn.execute(
                "SELECT e.id,e.ingested_at,e.body_json,n.payload_json FROM node_snapshots n "
                "JOIN events e ON e.id=n.event_row_id WHERE e.run_id=? AND n.node_id=? "
                f"{stream_filter} ORDER BY e.id DESC LIMIT ?",
                (run_id, node_id, *stream_args, limit),
            ).fetchall()
        for row in reversed(rows):
            value = loads(row["payload_json"])
            for key in tokens:
                try:
                    value = value[key]
                except (KeyError, TypeError, IndexError):
                    value = None
                    break
            original = loads(row["body_json"])
            points.append(
                {
                    "id": row["id"],
                    "received_at": row["ingested_at"],
                    "source_timestamp": original.get("source_timestamp"),
                    "value": value if type(value) in (int, float) else None,
                }
            )
        return {"items": points}

    @app.get("/control/fields")
    def fields(run_id: str, node_id: str):
        items = app.state.store.latest_telemetry(run_id, node_id)["items"]
        return {
            "items": [path for path, _ in flatten_numbers(items[0]["payload"])]
            if items
            else []
        }

    app.mount(
        "/ui", StaticFiles(directory=ROOT / "integration/web", html=True), name="panel"
    )
    return app
