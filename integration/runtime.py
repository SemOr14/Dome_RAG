"""Composition only: unchanged decision algorithms and Gate own equipment execution."""

import asyncio
import contextlib
import os
import time
from dataclasses import replace
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from data_api.codec import dumps
from data_api.store import utcnow
from fastapi import HTTPException
from kupol.dome.agentlog import AgentLog
from kupol.dome.catalog import Catalog
from kupol.dome.client import DomeClient, DomeError
from kupol.dome.config import load_dome_config
from kupol.dome.env import load_settings
from kupol.dome.gate import Cmd, Gate
from kupol.dome.state import DomeState

from llm_agent import DomeLLMAgent
from llm_agent.config import Settings as LLMSettings
from llm_agent.storage.data_api import DataApiClient, envelope

ROOT = Path(__file__).resolve().parents[1]


class RecordedClient(DomeClient):
    def _record(self, direction, message, raw=None):
        # A failed durable write must stop this connection, not silently lose an I/O frame.
        text = (
            raw.decode("utf-8", "backslashreplace") if isinstance(raw, bytes) else raw
        )
        self.recorder.frame(direction, message, self.session_id, text)


class Hub:
    def __init__(self, store):
        self.store = store
        self.settings = load_settings(root=ROOT)
        self.run_id = os.getenv("DATA_API_RUN_ID") or "web-" + uuid4().hex
        self.client = None
        self.task = None
        self.pump_task = None
        self.gate = None
        self.log = None
        self.catalog = Catalog()
        self.state = DomeState()
        self.command_lock = asyncio.Lock()
        self.llm_lock = asyncio.Lock()
        self.send_context = None
        self.last_error = None
        self.auto_task = None
        self.auto_interval = 60

    def emit(
        self,
        kind,
        payload,
        *,
        source="agent",
        direction="internal",
        channel="internal",
        event_id=None,
        request_id=None,
        node_id=None,
        metadata=None,
        session_id=None,
    ):
        e = envelope(
            self.run_id,
            kind,
            payload,
            session_id=session_id or (self.client.session_id if self.client else None),
            source=source,
            direction=direction,
            channel=channel,
            request_id=request_id,
            node_id=node_id,
            metadata=metadata,
        )
        if event_id:
            e["event_id"] = event_id
        return self.store.append([e])[0]

    def frame(self, direction, message, session_id, raw=None):
        p = message if isinstance(message, dict) else {}
        kind = p.get("type", "unknown") if message is not None else "invalid_json"
        # The exact outbound control was already committed synchronously before send.
        if direction == "outbound" and kind == "control" and self.send_context:
            return self.send_context["outbound_event_id"]
        e = envelope(
            self.run_id,
            kind,
            message,
            session_id=session_id,
            source="dome" if direction == "inbound" else "agent",
            direction=direction,
            channel="websocket",
            request_id=p.get("request_id"),
            node_id=p.get("node_id"),
            metadata={"observed_at": utcnow().isoformat()},
        )
        e["raw_text"] = raw
        if p.get("timestamp") is not None:
            e["source_timestamp"] = str(p["timestamp"])
        item = self.store.append([e])[0]
        if direction == "inbound" and kind == "telemetry":
            self.state.update(p.get("data") or {}, str(p.get("role", "")), time.time())
            if self.log:
                self.log.set_dome_time(p.get("data", {}).get("game_time"))
            if self.gate:
                self.gate.check_pending()
        if (
            direction == "inbound"
            and kind == "describe_nodes_response"
            and p.get("success") is True
        ):
            self.catalog = Catalog.from_response(p)
        return item["event_id"]

    def event(self, kind, payload, **kwargs):
        # DomeClient recorder protocol; run/session changes and errors are events too.
        self.emit(kind, payload, **kwargs)

    def status(self):
        return {
            "run_id": self.run_id,
            "session_id": self.client.session_id if self.client else None,
            "connected": bool(self.client and self.client.connected),
            "connecting": bool(self.task and not self.task.done()),
            "role": self.client.role if self.client else None,
            "control_enabled": self.settings.can_control,
            "last_error": self.last_error,
            "auto_analysis": bool(self.auto_task and not self.auto_task.done()),
            "llm_configured": bool(LLMSettings.from_env().deepseek_api_key),
            "mode": "live" if self.settings.can_control else "dry_run",
        }

    async def connect(self):
        if self.task and not self.task.done():
            return self.status()
        if not self.settings.dome_key:
            raise HTTPException(409, "Задайте DOME_KEY на сервере")
        cfg = load_dome_config(ROOT / "agent/dome.yaml")
        self.state = DomeState()
        self.catalog = Catalog()
        self.log = AgentLog(
            ROOT / "state/web-agent",
            run_id=uuid4().hex,
            secrets=[self.settings.dome_key, self.settings.deepseek_key],
        )
        self.log.sink = lambda kind, rec: self.emit(
            "verification" if kind == "verification" else "agent_" + kind, rec
        )
        self.client = RecordedClient(
            self.settings.dome_url, self.settings.dome_key, cfg.runtime, recorder=self
        )
        self.gate = Gate(
            cfg,
            self.state,
            lambda: self.catalog,
            self.log,
            self._send,
            can_control=self.settings.can_control,
            dry_run=self.settings.dry_run,
        )
        self.gate.max_telemetry_age_s = 30
        self.emit("connection_request", {"action": "connect"}, source="operator")

        async def run():
            try:
                await self.client.run()
            except Exception as exc:
                self.last_error = type(exc).__name__
                self.emit(
                    "error", {"component": "websocket", "error_type": self.last_error}
                )

        self.last_error = None
        self.task = asyncio.create_task(run())
        return self.status()

    async def disconnect(self):
        async with self.command_lock:
            return await self._disconnect()

    async def _disconnect(self):
        await self.set_auto(False)
        if self.client:
            self.client.stop()
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        self.emit("connection_request", {"action": "disconnect"}, source="operator")
        if self.client:
            self.client.connected = False
        if self.log:
            self.log.close()
            self.log = None
        return self.status()

    async def _send(self, node_id, action, value=None):
        ctx = self.send_context
        if not ctx or not self.client or self.client.session_id != ctx["session_id"]:
            raise DomeError("Сессия изменилась; нужна новая проверка")
        payload = {
            "type": "control",
            "request_id": ctx["request_id"],
            "node_id": node_id,
            "action": action,
        }
        if value is not None:
            payload["value"] = value
        outbound = self.emit(
            "control",
            payload,
            source=ctx["actor"],
            direction="outbound",
            channel="websocket",
            request_id=ctx["request_id"],
            node_id=node_id,
            metadata={
                "intent_event_id": ctx["intent_event_id"],
                "decision_event_id": ctx.get("decision_event_id"),
            },
        )
        ctx["outbound_event_id"] = outbound["event_id"]
        if not self.store.control(outbound["event_id"])["validation"]["accepted"]:
            raise DomeError(
                "Повторная проверка перед отправкой отклонена; команда не отправлена"
            )
        try:
            return await self.client.control(
                node_id, action, value, request_id=ctx["request_id"]
            )
        except DomeError:
            self.emit(
                "send_error",
                {"message": "Нет подтверждения; повтор вслепую запрещён"},
                request_id=ctx["request_id"],
                node_id=node_id,
                metadata={"control_event_id": outbound["event_id"]},
            )
            raise

    async def command(
        self,
        *,
        operation_id,
        node_id,
        action,
        value=None,
        actor="operator",
        reason="",
        decision_event_id=None,
    ):
        async with self.command_lock:
            result_id = str(uuid5(NAMESPACE_URL, "web-command-result:" + operation_id))
            existing = self.store.event(result_id)
            if existing:
                prior = self.store.event(operation_id)
                expected = {
                    "type": "control",
                    "request_id": "web-" + operation_id,
                    "node_id": node_id,
                    "action": action,
                    "value": value,
                }
                if (
                    not prior
                    or dumps(prior["payload"]) != dumps(expected)
                    or prior["source"] != actor
                    or prior.get("metadata", {}).get("decision_event_id")
                    != decision_event_id
                    or prior.get("metadata", {}).get("reason") != reason
                ):
                    raise HTTPException(
                        409, "operation_id уже относится к другой команде"
                    )
                return existing["payload"]
            # Stable intent ID makes an HTTP retry unable to send a second command.
            request_id = "web-" + operation_id
            payload = {
                "type": "control",
                "request_id": request_id,
                "node_id": node_id,
                "action": action,
                "value": value,
            }
            item = self.emit(
                "control_intent",
                payload,
                source=actor,
                channel="websocket",
                event_id=operation_id,
                request_id=request_id,
                node_id=node_id,
                metadata={"reason": reason, "decision_event_id": decision_event_id},
            )
            if not item["created"]:
                raise HTTPException(
                    409, "Попытка уже записана, результат неизвестен; не повторяем"
                )
            check = self.store.control(operation_id)
            outcome = {
                "intent_event_id": operation_id,
                "request_id": request_id,
                "validation": check["validation"],
                "actor": actor,
                "status": "rejected",
                "reason": "; ".join(check["validation"]["reasons"]),
            }
            if check["validation"]["accepted"]:
                if not self.client or not self.client.connected or not self.gate:
                    outcome["reason"] = "Нет активного соединения агента"
                else:
                    seq = self.log.write(
                        "decision",
                        situation="Ручная команда"
                        if actor == "operator"
                        else "Исполнение решения LLM",
                        cause=reason,
                        decision_event_id=decision_event_id,
                        actor=actor,
                    )
                    self.send_context = {
                        "actor": actor,
                        "request_id": request_id,
                        "session_id": self.client.session_id,
                        "intent_event_id": operation_id,
                        "decision_event_id": decision_event_id,
                    }
                    try:
                        response = await self.gate.submit(
                            Cmd(node_id, action, value, reason, seq)
                        )
                        outcome.update(
                            status=response.status,
                            reason=response.reason,
                            response=response.response,
                            control_event_id=self.send_context.get("outbound_event_id"),
                        )
                    finally:
                        self.send_context = None
            self.emit(
                "command_result",
                outcome,
                event_id=result_id,
                source=actor,
                request_id=request_id,
                node_id=node_id,
                metadata={
                    "intent_event_id": operation_id,
                    "decision_event_id": decision_event_id,
                },
            )
            return outcome

    def _agent(self):
        settings = LLMSettings.from_env()
        settings = replace(
            settings,
            data_api_run_id=self.run_id,
            data_api_session_id=self.client.session_id if self.client else None,
            data_api_url=os.getenv("DATA_API_URL", "http://127.0.0.1:8080"),
            rag_backend="none",
        )
        client = DataApiClient(
            settings.data_api_url, settings.data_api_token, settings.data_api_timeout_s
        )
        try:
            return DomeLLMAgent.from_data_api(settings, client=client), client
        except Exception:
            client.http.close()
            raise

    async def analyze(self, trigger, prompt_only=False):
        async with self.llm_lock:

            def work():
                agent, api = self._agent()
                try:
                    if prompt_only:
                        prompt = agent.build_prompt(trigger)
                        result = {"messages": prompt.messages, "stats": prompt.stats}
                        self.emit("prompt_preview", result)
                        return result
                    if not agent.settings.deepseek_api_key:
                        raise HTTPException(
                            409,
                            "DEEPSEEK_API_KEY не настроен; доступен просмотр промпта",
                        )
                    response = agent.decide(trigger)
                    result = response.model_dump(mode="json")
                    self.emit(
                        "llm_analysis_result",
                        result,
                        metadata={"decision_event_id": response.decision_event_id},
                    )
                    return result
                finally:
                    api.http.close()

            return await asyncio.to_thread(work)

    async def set_auto(self, enabled, interval=60):
        if self.auto_task:
            self.auto_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.auto_task
            self.auto_task = None
        self.auto_interval = max(30, interval)
        if enabled:

            async def loop():
                while True:
                    if self.client and self.client.connected:
                        try:
                            await self.analyze(
                                "Оцени актуальную телеметрию и предложи действия"
                            )
                        except Exception as exc:
                            self.emit(
                                "error",
                                {
                                    "component": "auto_analysis",
                                    "error_type": type(exc).__name__,
                                },
                            )
                    await asyncio.sleep(self.auto_interval)

            self.auto_task = asyncio.create_task(loop())
        return self.status()
