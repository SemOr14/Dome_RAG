"""HTTP-only storage API. No websocket client or equipment credentials are used here."""

import hmac
import os
import sqlite3
from typing import Annotated, Literal
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .codec import SCHEMA, InvalidEvent, loads
from .controls import is_control
from .redaction import MASK, Redactor, sensitive
from .store import Conflict, Store, utcnow

Page = Annotated[int, Query(ge=1, le=1000)]
After = Annotated[int, Query(ge=0)]


class AuditMiddleware:
    """Buffer both directions and commit sanitized audit before releasing the response.

    This outer ASGI wrapper sees auth failures, validation errors, 404/405, CORS and 500.
    It audits one exchange per request, without generating recursive HTTP requests.
    """

    def __init__(self, app, store):
        self.app, self.store = app, store

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = utcnow().isoformat()
        body, disconnected = bytearray(), False
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                disconnected = True
                break
            body.extend(msg.get("body", b""))
            if not msg.get("more_body", False):
                break
        messages, delivered = [], False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        async def capture(message):
            messages.append(message)

        failure = None
        if disconnected:
            messages = [
                {"type": "http.response.start", "status": 499, "headers": []},
                {"type": "http.response.body", "body": b""},
            ]
        else:
            try:
                await self.app(scope, replay, capture)
            except Exception as exc:
                failure = type(exc).__name__
                messages.clear()
                await JSONResponse({"detail": "Internal server error"}, status_code=500)(
                    scope, replay, capture
                )
        start = next(m for m in messages if m["type"] == "http.response.start")
        output = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        redactor = self.store.redactor

        def headers(pairs):
            return [
                [
                    k.decode("latin1"),
                    MASK if sensitive(k.decode("latin1")) else redactor.text(v.decode("latin1")),
                ]
                for k, v in pairs
            ]

        query = [
            [k, MASK if sensitive(k) else redactor.text(v)]
            for k, v in parse_qsl(
                scope.get("query_string", b"").decode("latin1"), keep_blank_values=True
            )
        ]
        exchange = {
            "started_at": started,
            "finished_at": utcnow().isoformat(),
            "request": {
                "method": scope["method"],
                "path": scope["path"],
                "query": query,
                "headers": headers(scope.get("headers", [])),
                "body": redactor.body(bytes(body)),
                "disconnected": disconnected,
            },
            "response": {
                "status": start["status"],
                "headers": headers(start.get("headers", [])),
                "body": redactor.body(output),
            },
            "error_type": failure,
        }
        try:
            audit_id = await run_in_threadpool(self.store.audit, exchange)
        except Exception:
            # Logging failure must never produce a positive acknowledgement to the caller.
            if not disconnected:
                await JSONResponse(
                    {"detail": "HTTP audit unavailable; retry with the same event_id"},
                    status_code=503,
                )(scope, receive, send)
            return
        if not disconnected:
            start.setdefault("headers", []).append((b"x-audit-id", str(audit_id).encode()))
            for message in messages:
                await send(message)


def create_app(db_path=None, token=None, *, redactor=None, max_state_age=30):
    token = os.environ.get("DATA_API_TOKEN", "") if token is None else token
    if redactor is None:
        secrets = [token, *os.environ.get("DATA_API_REDACT_VALUES", "").split(",")]
        redactor = Redactor(secrets)
    store = Store(
        db_path or os.environ.get("DATA_API_DB", "state/data-api.sqlite3"),
        redactor=redactor,
        max_state_age=max_state_age,
    )

    async def authorize(request: Request):
        if token:
            supplied = request.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
                raise HTTPException(401, "Invalid service token")

    app = FastAPI(title="Dome storage API", version="1.0", dependencies=[Depends(authorize)])
    app.state.store = store

    @app.exception_handler(InvalidEvent)
    async def invalid_event(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(Conflict)
    async def conflict(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(sqlite3.Error)
    async def database_error(_request, _exc):
        return JSONResponse(
            {"detail": "Storage unavailable; retry with the same event_id"}, status_code=500
        )

    async def json_body(request):
        try:
            return loads(await request.body())
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise HTTPException(422, "Invalid JSON") from exc

    def request_filter(text):
        try:
            value = loads(text)
        except (ValueError, RecursionError) as exc:
            raise HTTPException(422, "request_id must be a JSON string or number") from exc
        if type(value) not in (str, int, float) and value is not None:
            raise HTTPException(422, "request_id must be a JSON string, number or null")
        return value

    event_body = {
        "requestBody": {"required": True, "content": {"application/json": {"schema": SCHEMA}}}
    }

    @app.get("/health")
    def health():
        with store.connection() as conn:
            conn.execute("SELECT version FROM schema_migrations").fetchone()
        return {"status": "ok", "schema_version": "1.0"}

    @app.post("/v1/events", openapi_extra=event_body)
    async def append_event(request: Request, response: Response):
        event = await json_body(request)
        item = (await run_in_threadpool(store.append, [event]))[0]
        response.status_code = 201 if item["created"] else 200
        return item

    @app.post(
        "/v1/events/batch",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["events"],
                            "additionalProperties": False,
                            "properties": {
                                "events": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": 1000,
                                    "items": SCHEMA,
                                }
                            },
                        }
                    }
                },
            }
        },
    )
    async def append_batch(request: Request):
        body = await json_body(request)
        if (
            not isinstance(body, dict)
            or set(body) != {"events"}
            or not isinstance(body["events"], list)
            or not 1 <= len(body["events"]) <= 1000
        ):
            raise HTTPException(422, "Expected {events: [...]} containing 1..1000 events")
        return {"items": await run_in_threadpool(store.append, body["events"])}

    @app.get("/v1/events")
    def list_events(
        after_id: After = 0,
        limit: Page = 100,
        run_id: str | None = None,
        session_id: str | None = None,
        source: str | None = None,
        channel: str | None = None,
        direction: str | None = None,
        event_type: str | None = None,
        node_id: str | None = None,
        request_id: str | None = None,
    ):
        filters = {
            k: v
            for k, v in locals().items()
            if k not in ("after_id", "limit", "request_id", "store", "request_filter")
            and v is not None
        }
        if request_id is not None:
            filters["request_id"] = request_filter(request_id)
        return store.events(after_id=after_id, limit=limit, **filters)

    @app.get("/v1/events/{event_id}")
    def get_event(event_id: str):
        item = store.event(event_id)
        if item is None:
            raise HTTPException(404, "Event not found")
        return item

    @app.get("/v1/runs")
    def list_runs(after_run_id: str = "", limit: Page = 100):
        return store.runs(after_run_id=after_run_id, limit=limit)

    @app.get("/v1/telemetry/latest")
    def telemetry(
        run_id: str, node_id: str | None = None, after_node_id: str = "", limit: Page = 1000
    ):
        return store.latest_telemetry(run_id, node_id, after_node_id, limit)

    @app.get("/v1/exchanges")
    def exchanges(run_id: str, session_id: str, request_id: str):
        value = request_filter(request_id)
        if value is None or not session_id:
            raise HTTPException(422, "A non-null request_id and nonempty session_id are required")
        return store.exchanges(run_id, session_id, value)

    @app.get("/v1/http-exchanges")
    def http_list(after_id: After = 0, limit: Page = 100):
        return store.http_exchanges(after_id, limit)

    @app.get("/v1/http-exchanges/{row_id}")
    def http_detail(row_id: int):
        item = store.http_exchange(row_id)
        if item is None:
            raise HTTPException(404, "HTTP exchange not found")
        return item

    @app.post("/v1/controls/validate", openapi_extra=event_body)
    async def validate_control(request: Request, response: Response):
        event = await json_body(request)
        # Store even rejected commands, but keep this preflight distinct from an actual send.
        from .codec import validate

        event = validate(event)
        if (
            event["source"] not in ("operator", "agent")
            or event["direction"] != "internal"
            or event["event_type"] != "control_intent"
            or not is_control(event)
        ):
            raise HTTPException(422, "Use an internal agent/operator control_intent event")
        item = (await run_in_threadpool(store.append, [event]))[0]
        response.status_code = 201 if item["created"] else 200
        return {**item, **await run_in_threadpool(store.control, item["event_id"])}

    @app.get("/v1/controls")
    def control_list(
        after_id: After = 0,
        limit: Page = 100,
        run_id: str | None = None,
        actor: Literal["agent", "operator", "unknown"] | None = None,
        validation: Literal["accepted", "rejected"] | None = None,
    ):
        return store.controls(
            after_id=after_id,
            limit=limit,
            run_id=run_id,
            actor=actor,
            validation=None if validation is None else int(validation == "accepted"),
        )

    @app.get("/v1/controls/{event_id}")
    def control_detail(event_id: str):
        item = store.control(event_id)
        if item is None:
            raise HTTPException(404, "Control event not found")
        return item

    origins = [
        v.strip() for v in os.environ.get("DATA_API_CORS_ORIGINS", "").split(",") if v.strip()
    ]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
            expose_headers=["X-Audit-ID"],
        )
    app.add_middleware(AuditMiddleware, store=store)
    return app
