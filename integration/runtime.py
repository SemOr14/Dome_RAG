"""Integrated panel/runtime: one Runner owns transport and Gate; server modes arbitrate all actors.

Deterministic algorithms run first, the model handles uncovered events, incidents call a human.
Production and safety use the same policy epoch, recorder and database. No connection on startup.
"""

import asyncio
import contextlib
import os
import hashlib
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from kupol.dome.agentlog import AgentLog
from kupol.dome.config import load_dome_config
from kupol.dome.datalog import DataApiRecorder
from kupol.dome.env import load_settings
from kupol.dome.runner import Runner
from kupol.dome.control import ControlPolicy
from kupol.dome.incidents import IncidentCenter
from integration.production import ProductionService
from integration.safety import SafetyController

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
        self.deployment_id = hashlib.sha256((self.settings.dome_url + self.settings.dome_key + str(store.path)).encode()).hexdigest()[:20]
        self.runner: Runner | None = None
        self.task: asyncio.Task | None = None
        self.llm_lock = asyncio.Lock()
        self.last_error = None
        self.policy = ControlPolicy.from_env()
        events, after = [], 0
        while True:
            page = store.events(event_type='incident_changed', after_id=after, limit=1000)
            events += [e for e in page['items'] if (e.get('metadata') or {}).get('deployment_id') == self.deployment_id]
            if not page['items']:
                break
            after = page['next_after_id']
        self.incidents = IncidentCenter(self.emit, events)
        self.production = ProductionService(self)
        self.safety = SafetyController(self)

    def set_mode(self, mode, algorithms):
        self.safety.cancel()
        if self.runner:
            self.runner.set_mode(mode, algorithms)
        else:
            self.policy.change(mode, algorithms)
        self.emit('policy_changed', self.policy.public(), metadata={'actor': 'operator'})
        return self.status()

    @property
    def client(self):
        return self.runner.client if self.runner else None

    def emit(self, kind, payload, *, source="operator", event_id=None, **kwargs):
        kwargs['metadata'] = {**kwargs.get('metadata', {}), 'deployment_id': self.deployment_id}
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
            **self.policy.public(),
            "run_id": self.run_id,
            "session_id": self.client.session_id if self.client else None,
            "connected": bool(self.client and self.client.connected),
            "connecting": bool(self.task and not self.task.done()),
            "role": self.client.role if self.client else None,
            "control_enabled": self.settings.can_control and self.policy.api_scope == 'full',
            "last_error": self.last_error,
            "auto_analysis": bool(runner and brain is not None and runner.llm_enabled),
            "review_s": brain.settings.trigger_review_s if brain else LLMSettings.from_env().trigger_review_s,
            "brain": mode,
            "llm_configured": bool(self.settings.deepseek_key or LLMSettings.from_env().deepseek_api_key),
            "mode": "live" if self.settings.can_control else "dry_run",
            "stats": runner.gate.stats if runner else None,
        }

    async def connect(self):
        if self.policy.operating_state == 'shutting_down':
            raise HTTPException(409, 'Приложение завершается')
        if self.policy.api_scope != 'full':
            raise HTTPException(409, 'Настроен только журнал: живое подключение выключено (DOME_API_SCOPE=history)')
        if self.task and not self.task.done():
            return self.status()
        s = self.settings
        if not s.dome_key:
            raise HTTPException(409, "Задайте DOME_KEY на сервере")
        url = os.getenv("DATA_API_URL", "http://127.0.0.1:8080")
        s.data_api_url, s.data_api_token, s.data_api_run_id = url, os.getenv("DATA_API_TOKEN", ""), self.run_id
        # Состояние (прочитанный чат, курсор ручных команд, паузы Gate) — своё у каждого запуска:
        # чужие id сообщений и строк БД из другого запуска или эмулятора привели бы к пропуску команд
        state = Path(os.getenv('PANEL_STATE_DIR', str(ROOT / 'state' / 'web-agent' / self.deployment_id)))
        s.state_dir = str(state)
        cfg = load_dome_config(ROOT / "agent/dome.yaml")
        cfg.runtime.state_file = str(state / "agent_state.json")
        log = AgentLog(state / "logs", secrets=[s.dome_key, s.deepseek_key])
        recorder = DataApiRecorder(url, s.data_api_token, self.run_id, s.state_dir)
        brain = None
        if s.brain == "llm" and s.deepseek_key:
            from kupol.dome.brain import LlmBrain

            def brain(r: Runner) -> LlmBrain:
                return LlmBrain(s, log, r.state, lambda: r.client.telemetry, lambda: r.client.session_id, r.recorder)

        self.runner = Runner(s, cfg, log, recorder=recorder, brain=brain, policy=self.policy)
        self.runner.incidents = self.incidents
        self.runner.production_tracker = self.production.tracker
        from kupol.dome.printer_supervisor import PrinterSupervisor
        self.runner.printer_supervisor = PrinterSupervisor(self.runner, self.incidents, self.production.tracker)
        if os.getenv('PRODUCTION_APPROVED_SHA256', '').strip():
            cfg.policy.allowed.setdefault('printer_3d_01', {})['upload_file'] = {'object': ['file_name', 'content']}
        self.runner.gate.dispatch_guard = lambda cmd: ('Шаг жёлтого плана отменён ручным включением линии'
            if cmd.actor == 'safety' and cmd.node_id == 'smart_panel_01' and cmd.action == 'line_off'
            and (cmd.value.get('line') if isinstance(cmd.value, dict) else cmd.value) in self.safety.cancelled_lines else None)
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
        self.safety.cancel()
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

    async def command(self, *, operation_id, node_id, action, value=None, reason="", decision_event_id=None, confirmed=False):
        """Команда человека: намерение в БД → проверка сервера → агент исполняет через Gate → итог."""
        if self.policy.profile != 'service':
            raise HTTPException(403, 'competition: ручные команды запрещены; передайте задачу агенту через чат')
        if self.policy.operating_state == 'shutting_down':
            raise HTTPException(409, 'Приложение завершается')
        if node_id == 'smart_panel_01' and action == 'line_on':
            self.safety.cancel_line(value.get('line') if isinstance(value, dict) else value)
        payload = {"type": "control", "request_id": "web-" + operation_id, "node_id": node_id, "action": action}
        if value is not None:
            payload["value"] = value
        prior = self.store.event(operation_id)
        if prior is not None and prior.get("payload") != payload:
            raise HTTPException(409, "operation_id уже относится к другой команде")
        if prior is not None:
            return self._dispatch(operation_id) or {'status': 'ambiguous', 'intent_event_id': operation_id,
                'reason': 'Намерение уже сохранено; проверьте итог, повторная отправка не выполняется'}
        if prior is None:  # повтор HTTP-запроса не создаёт вторую команду
            self.emit("control_intent", payload, event_id=operation_id, channel="websocket",
                      request_id=payload["request_id"], node_id=node_id,
                      metadata={"reason": reason, "operator": "веб-панель", "decision_event_id": decision_event_id,
                                "confirmed": confirmed, 'actor': 'operator', 'policy_epoch': self.policy.policy_epoch,
                                'agent_mode': self.policy.agent_mode, 'operation_id': operation_id})
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

    async def request(self, operation_id, text):
        """Требование Оператора (пользователь панели) агенту: срочный повод для LLM, ответ — рекомендация в чат."""
        if self.policy.operating_state == 'shutting_down':
            raise HTTPException(409, 'Приложение завершается')
        brain = self.runner.brain if self.runner else None
        previous = self.store.event(operation_id)
        if previous:
            if previous.get("event_type") != "operator_request" or previous.get("payload") != {"text": text}:
                raise HTTPException(409, "operation_id уже относится к другому обращению")
            return {"status": "duplicate", "event_id": operation_id}  # identical HTTP retry
        self.emit("operator_request", {"text": text}, event_id=operation_id, metadata={"operator": "веб-панель"})
        if not (self.client and self.client.connected):
            return {"status": "not_connected", "event_id": operation_id, "reason": "агент не подключён к Куполу"}
        if brain is None or not self.runner.llm_enabled or not brain.healthy:
            return {"status": "no_llm", "event_id": operation_id,
                    "reason": "LLM-агент выключен или недоступен: требование записано, но решения сейчас принимают плейбуки"}
        brain.operator_request(text, operation_id)
        return {"status": "queued", "event_id": operation_id}

    def chat(self, run_id):
        """Лента переписки: требования Оператора из панели, ответы-рекомендации агента, чат Купола meeting_01."""
        items, seen = [], {}
        for item in self.store.latest_telemetry(run_id, "meeting_01")["items"]:
            for m in item["payload"].get("recent_messages") or []:
                if isinstance(m, dict) and isinstance(m.get("id"), int):
                    seen[m["id"]] = m
        for kind in ("operator_request", "meeting_message", "control_response", "agent_message"):
            after = 0
            while True:
                page = self.store.events(after_id=after, limit=1000, run_id=run_id, event_type=kind)
                if not page["items"]:
                    break
                after = page["next_after_id"]
                for e in page["items"]:
                    p = e.get("payload") or {}
                    m = p.get("message")
                    if kind == "operator_request":
                        items.append({"from": "operator", "via": "panel", "time": e["ingested_at"], "text": p.get("text")})
                    elif isinstance(m, dict) and isinstance(m.get("id"), int):
                        seen[m["id"]] = m
                    elif kind == "agent_message" and p.get("direction") == "out" and p.get("dry_run"):
                        items.append({"from": "agent", "via": "dome_chat", "time": e["ingested_at"], "text": p.get("text"),
                                      "not_sent": True})
        for m in seen.values():
            sender = m.get("sender") or {}
            who = {"operator": "operator", "team": "agent"}.get(sender.get("role"), "other")
            t = m.get("time")
            items.append({"from": who, "via": "dome_chat", "id": m["id"], "to": m.get("to"), "text": m.get("text"),
                          "name": sender.get("name"), "team": sender.get("team"),
                          "time": datetime.fromtimestamp(t, timezone.utc).isoformat() if isinstance(t, (int, float)) else t})
        items.sort(key=lambda x: str(x.get("time") or ""))
        return {"items": items}

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
        if self.policy.operating_state == 'shutting_down' or (not prompt_only and self.policy.agent_mode == 'off'):
            raise HTTPException(409, 'LLM выключен или приложение завершается')
        epoch = self.policy.policy_epoch
        async with self.llm_lock:

            def work():
                agent, api = self._agent()
                sink = agent.events
                class PolicySink:
                    def emit(_self, kind, payload, **kwargs):
                        kwargs['metadata'] = {**kwargs.get('metadata', {}), 'policy_epoch': epoch,
                                              'agent_mode': self.policy.agent_mode}
                        return sink.emit(kind, payload, **kwargs)
                agent.events = PolicySink()
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

            result = await asyncio.to_thread(work)
            if self.policy.policy_epoch != epoch:
                return {'status': 'cancelled', 'reason': 'Режим изменён; результат устарел'}
            return result

    async def set_auto(self, enabled, interval=60):
        """Автономные решения LLM-агента (выключено — плейбуки) и интервал планового обзора, с."""
        self.set_mode('autonomous' if enabled else 'off', True)
        if self.runner:
            if self.runner.brain is not None:
                self.runner.brain.settings.trigger_review_s = float(interval)
        return self.status()

