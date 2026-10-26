"""Панель управляет запуском агента Купола и ставит ручные команды; решения и отправку выполняет агент.

Агент — тот же рантайм, что ``agent/main.py``: ``Runner`` с LLM-основой (``AGENT_BRAIN=llm``, плейбуки —
запасной путь), Gate и проверкой эффекта. Весь вход/выход агент пишет в БД этого же процесса через
HTTP API (``DATA_API_URL``). Команда человека из панели — намерение ``control_intent`` с
``source=operator`` (``contracts/manual-control.md``): сервер проверяет его, и панель сразу передаёт его в Gate
агента (без LLM); итог — ``control_dispatch``. Опрос очереди агентом подхватывает команды других клиентов
API и не исполняет уже исполненные. Своего соединения с Куполом и своего Gate у панели нет.
"""

import asyncio
import contextlib
import os
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from kupol.dome.agentlog import AgentLog
from kupol.dome.config import load_dome_config
from kupol.dome.datalog import DataApiRecorder
from kupol.dome.env import load_settings
from kupol.dome.runner import Runner

from llm_agent import DomeLLMAgent
from llm_agent.config import Settings as LLMSettings
from llm_agent.storage.data_api import DataApiClient, envelope

ROOT = Path(__file__).resolve().parents[1]
DISPATCH_WAIT_S = 15.0  # сколько ждать итога ручной команды от агента


class Hub:
    def __init__(self, store):
        self.store = store
        self.settings = load_settings(root=ROOT)
        self.run_id = os.getenv("DATA_API_RUN_ID") or "web-" + uuid4().hex
        self.runner: Runner | None = None
        self.task: asyncio.Task | None = None
        self.llm_lock = asyncio.Lock()
        self.last_error = None

    @property
    def client(self):
        return self.runner.client if self.runner else None

    def emit(self, kind, payload, *, source="operator", event_id=None, **kwargs):
        e = envelope(self.run_id, kind, payload, session_id=self.client.session_id if self.client else None,
                     source=source, **kwargs)
        if event_id:
            e["event_id"] = event_id
        return self.store.append([e])[0]

    def status(self):
        runner = self.runner
        brain = runner.brain if runner else None
        if brain is None:
            mode = "rules" if runner else None
        elif not runner.llm_enabled:
            mode = "rules (LLM выключен в панели)"
        else:
            mode = "llm" if brain.healthy else "rules (модель недоступна)"
        return {
            "run_id": self.run_id,
            "session_id": self.client.session_id if self.client else None,
            "connected": bool(self.client and self.client.connected),
            "connecting": bool(self.task and not self.task.done()),
            "role": self.client.role if self.client else None,
            "control_enabled": self.settings.can_control,
            "last_error": self.last_error,
            "auto_analysis": bool(runner and brain is not None and runner.llm_enabled),
            "brain": mode,
            "llm_configured": bool(self.settings.deepseek_key or LLMSettings.from_env().deepseek_api_key),
            "mode": "live" if self.settings.can_control else "dry_run",
            "stats": runner.gate.stats if runner else None,
        }

    async def connect(self):
        if self.task and not self.task.done():
            return self.status()
        s = self.settings
        if not s.dome_key:
            raise HTTPException(409, "Задайте DOME_KEY на сервере")
        url = os.getenv("DATA_API_URL", "http://127.0.0.1:8080")
        s.data_api_url, s.data_api_token, s.data_api_run_id = url, os.getenv("DATA_API_TOKEN", ""), self.run_id
        s.state_dir = str(ROOT / "state" / "web-agent")
        cfg = load_dome_config(ROOT / "agent/dome.yaml")
        cfg.runtime.state_file = str(ROOT / "state" / "web-agent" / "agent_state.json")
        log = AgentLog(ROOT / "state" / "web-agent" / "logs", secrets=[s.dome_key, s.deepseek_key])
        recorder = DataApiRecorder(url, s.data_api_token, self.run_id, s.state_dir)
        brain = None
        if s.brain == "llm" and s.deepseek_key:
            from kupol.dome.brain import LlmBrain

            def brain(r: Runner) -> LlmBrain:
                return LlmBrain(s, log, r.state, lambda: r.client.telemetry, lambda: r.client.session_id, r.recorder)

        self.runner = Runner(s, cfg, log, recorder=recorder, brain=brain)
        self.emit("connection_request", {"action": "connect", "brain": "llm" if brain else "rules"})

        async def run():
            try:
                await self.runner.run()
            except Exception as exc:
                self.last_error = type(exc).__name__
                self.emit("error", {"component": "agent", "error_type": self.last_error}, source="system")

        self.last_error = None
        self.task = asyncio.create_task(run())
        return self.status()

    async def disconnect(self):
        if self.runner:
            self.runner.stop()
        if self.task:
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                await asyncio.wait_for(self.task, 15)
        self.emit("connection_request", {"action": "disconnect"})
        return self.status()

    def _dispatch(self, intent_event_id):
        after = 0
        while True:
            page = self.store.events(after_id=after, limit=1000, run_id=self.run_id, event_type="control_dispatch")
            for e in page["items"]:
                if (e.get("payload") or {}).get("intent_event_id") == intent_event_id:
                    return e["payload"]
            if not page["items"]:
                return None
            after = page["next_after_id"]

    async def command(self, *, operation_id, node_id, action, value=None, reason="", decision_event_id=None):
        """Команда человека: намерение в БД → проверка сервера → агент исполняет через Gate → итог."""
        payload = {"type": "control", "request_id": "web-" + operation_id, "node_id": node_id, "action": action}
        if value is not None:
            payload["value"] = value
        prior = self.store.event(operation_id)
        if prior is not None and prior.get("payload") != payload:
            raise HTTPException(409, "operation_id уже относится к другой команде")
        if prior is None:  # повтор HTTP-запроса не создаёт вторую команду
            self.emit("control_intent", payload, event_id=operation_id, channel="websocket",
                      request_id=payload["request_id"], node_id=node_id,
                      metadata={"reason": reason, "operator": "веб-панель", "decision_event_id": decision_event_id})
        check = self.store.control(operation_id)
        outcome = {"intent_event_id": operation_id, "validation": check["validation"], "status": "rejected",
                   "reason": "; ".join(check["validation"]["reasons"])}
        if not check["validation"]["accepted"]:
            return outcome  # агент отклонённое сервером не исполняет
        if not (self.client and self.client.connected):
            return {**outcome, "status": "rejected", "reason": "агент не подключён к Куполу"}
        # Напрямую в Gate агента этого процесса, без LLM и без ожидания опроса очереди
        await self.runner.manual_command({"event": check["event"], "validation": check["validation"]})
        end = time.monotonic() + DISPATCH_WAIT_S
        while time.monotonic() < end:
            result = self._dispatch(operation_id)
            if result:
                return {**outcome, **result}
            await asyncio.sleep(0.3)
        return {**outcome, "status": "pending", "reason": "агент ещё не обработал команду; итог — событие control_dispatch"}

    def chat(self, run_id):
        """Переписка в meeting_01: сообщения Купола, последние сообщения из телеметрии, неотправленные (dry-run)."""
        messages = {}
        for item in self.store.latest_telemetry(run_id, "meeting_01")["items"]:
            for m in item["payload"].get("recent_messages") or []:
                if isinstance(m, dict) and isinstance(m.get("id"), int):
                    messages[m["id"]] = m
        local = []
        for kind in ("meeting_message", "control_response", "agent_message"):
            after = 0
            while True:
                page = self.store.events(after_id=after, limit=1000, run_id=run_id, event_type=kind)
                if not page["items"]:
                    break
                after = page["next_after_id"]
                for e in page["items"]:
                    p = e.get("payload") or {}
                    m = p.get("message")
                    if isinstance(m, dict) and isinstance(m.get("id"), int):
                        messages[m["id"]] = m
                    elif kind == "agent_message" and p.get("direction") == "out" and p.get("dry_run"):
                        local.append({"id": None, "time": e["ingested_at"], "to": p.get("to"), "text": p.get("text"),
                                      "sender": {"role": "team", "name": "агент"}, "not_sent": True})
        return {"items": sorted(messages.values(), key=lambda m: m["id"]) + local}

    def _agent(self):
        settings = replace(
            LLMSettings.from_env(), data_api_run_id=self.run_id,
            data_api_session_id=self.client.session_id if self.client else None,
            data_api_url=os.getenv("DATA_API_URL", "http://127.0.0.1:8080"),
        )
        if self.settings.deepseek_key:
            settings.deepseek_api_key = self.settings.deepseek_key
        client = DataApiClient(settings.data_api_url, settings.data_api_token, settings.data_api_timeout_s)
        try:
            return DomeLLMAgent.from_data_api(settings, client=client), client
        except Exception:
            client.http.close()
            raise

    async def analyze(self, trigger, prompt_only=False):
        """Разовый запрос к LLM-агенту из панели: решение записывается, команды не исполняются."""
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
                        raise HTTPException(409, "DEEPSEEK_API_KEY не настроен; доступен просмотр промпта")
                    return agent.decide(trigger).model_dump(mode="json")
                finally:
                    api.http.close()

            return await asyncio.to_thread(work)

    async def set_auto(self, enabled, interval=60):
        """Автономные решения LLM-агента: выключено — агент работает на плейбуках."""
        if self.runner:
            self.runner.llm_enabled = enabled
        return self.status()

