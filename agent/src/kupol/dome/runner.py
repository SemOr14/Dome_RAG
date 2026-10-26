"""Главный цикл агента: телеметрия → решения → Gate → проверка → лог.

Решения принимает LLM-агент Купола (``brain.py``: диспетчер поводов → DeepSeek → проверенное решение);
без модели или при её сбоях — плейбуки. Один asyncio-цикл: модель работает в фоновом потоке, а такт,
Gate и проверка эффекта отрабатывают на каждой телеметрии и никогда её не ждут.
Состояние (память плейбуков, кулдауны, обработанные сообщения чата) сохраняется в файл: после
перезапуска агент продолжает с того же места и не повторяет решения вслепую.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from kupol.dome.agentlog import AgentLog
from kupol.dome.catalog import Catalog
from kupol.dome.checks import assess_co2, assess_energy, assess_printer
from kupol.dome.client import DomeClient
from kupol.dome.config import DomeConfig
from kupol.dome.control import ControlPolicy
from kupol.dome.incidents import IncidentCenter, classify_live
from kupol.dome.routing import route_playbooks
from kupol.dome.printer_supervisor import PrinterSupervisor
from kupol.dome.datalog import DataApiRecorder
from kupol.dome.env import Settings
from kupol.dome.expects import expect_for
from kupol.dome.gate import Cmd, Gate, GateResult
from kupol.dome.manual import ManualControls
from kupol.dome.playbooks.air import Co2Playbook, CondensatePlaybook
from kupol.dome.playbooks.base import Ctx, Intent, Playbook, Step
from kupol.dome.playbooks.devices import (
    AlarmPlaybook,
    ClockPlaybook,
    NetworkPlaybook,
    StalePlaybook,
)
from kupol.dome.playbooks.energy import EnergyPlaybook
from kupol.dome.playbooks.lines import OvercurrentPlaybook, PhantomLinePlaybook
from kupol.dome.playbooks.machines import ResumePlaybook, SmokePlaybook
from kupol.dome.playbooks.production import PrinterPlaybook
from kupol.dome.playbooks.tasks import TasksPlaybook, unanswered_operator_messages
from kupol.dome.state import DomeState

if TYPE_CHECKING:  # LLM-слой нужен только в режиме AGENT_BRAIN=llm
    from kupol.dome.brain import LlmBrain

AGENT_NAME = "kupol-dome"
STALE_TELEMETRY_S = 20.0
STATE_SAVE_EVERY_S = 10.0
CATALOG_RETRY_S = 30.0
OPERATOR_TEXTS_KEEP_S = 1800.0


class Advisor(Protocol):
    """Необязательный советник (RAG + LLM): справка из базы знаний, гипотезы и ответы модели (только текст)."""

    def references(self, query: str) -> list[dict[str, Any]]:
        """Фрагменты базы знаний по запросу: ``[{"ref": ..., "text": ...}]``."""
        ...

    def diagnose(self, topic: str, context: dict[str, Any], ref: int | None = None) -> Any:
        """Гипотеза модели по непонятной ситуации (``Diagnosis`` либо ``None``). Блокирующий вызов — в потоке."""
        ...

    def answer(self, question: str, context: dict[str, Any], ref: int | None = None) -> str | None:
        """Ответ Оператору на свободный вопрос (``None`` — нет ответа). Блокирующий вызов — в потоке."""
        ...


def default_playbooks() -> list[Playbook]:
    """Плейбуки в порядке приоритета безопасности."""
    return [
        SmokePlaybook(),
        Co2Playbook(),
        CondensatePlaybook(),
        ResumePlaybook(),
        TasksPlaybook(),
        PrinterPlaybook(),
        EnergyPlaybook(),
        OvercurrentPlaybook(),
        PhantomLinePlaybook(),
        AlarmPlaybook(),
        StalePlaybook(),
        ClockPlaybook(),
        NetworkPlaybook(),
    ]


def git_version() -> str:
    """Короткий хеш коммита (для записи ``start``) либо ``unknown``."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=3, check=False)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


class Runner:
    """Собирает компоненты и крутит цикл."""

    def __init__(
        self,
        settings: Settings,
        cfg: DomeConfig,
        log: AgentLog,
        client: DomeClient | None = None,
        playbooks: list[Playbook] | None = None,
        advisor: Advisor | None = None,
        models: list[str] | None = None,
        clock: Callable[[], float] = time.time,
        recorder: DataApiRecorder | None = None,
        brain: LlmBrain | Callable[[Runner], LlmBrain] | None = None,  # None — только плейбуки
        policy: ControlPolicy | None = None,
    ) -> None:
        self.settings = settings
        self.cfg = cfg
        self.log = log
        self.clock = clock
        self.advisor = advisor
        self.models = models or []
        self.state = DomeState(stale_age_s=cfg.checks.stale.node_age_s)
        if recorder is None and settings.data_api_url:
            recorder = DataApiRecorder(settings.data_api_url, settings.data_api_token, settings.data_api_run_id or None, settings.state_dir)
        self.recorder = recorder
        self.client = client or DomeClient(settings.dome_url, settings.dome_key, cfg.runtime, on_event=self._on_client_event, recorder=recorder)
        if client is not None:
            client._on_event = self._on_client_event
            client.recorder = recorder or client.recorder
        if recorder is not None:
            # Решения, действия и проверки эффекта — в ту же БД; action ссылается на control через request_id
            log.sink = lambda kind, rec: recorder.event(
                f"agent_{kind}", rec, session_id=self.client.session_id, metadata={"log_run_id": rec.get("run_id"), "log_seq": rec.get("seq"),
                'actor': rec.get('actor') or ('operator' if rec.get('source') == 'operator' else 'agent')}
            )  # fmt: skip
        if brain is not None and not hasattr(brain, "step"):
            brain = brain(self)  # фабрике нужны состояние, клиент и рекордер этого рантайма
        self.brain = brain
        self.policy = policy or ControlPolicy.from_env()
        self.client.policy = self.policy
        if brain is not None:
            brain.policy = self.policy
        self.manual = ManualControls(recorder) if recorder is not None else None  # команды Оператора из БД
        self._manual_task: asyncio.Task[Any] | None = None
        self.manual_done: set[str] = set()  # исполненные намерения: прямой вызов и опрос очереди не дублируют команду
        if brain is not None:
            db_sink = log.sink
            log.sink = lambda kind, rec: (db_sink and db_sink(kind, rec), brain.on_log(kind, rec))  # журнал для промпта
        self.catalog = Catalog()
        self._catalog_msg: dict[str, Any] | None = None
        self.gate = Gate(
            cfg, self.state, lambda: self.catalog, log, self.client.control,
            can_control=settings.can_control, dry_run=settings.dry_run, clock=clock, agent_name=AGENT_NAME,
        )  # fmt: skip
        self.gate.max_telemetry_age_s = STALE_TELEMETRY_S
        self.gate.policy = self.policy
        self.playbooks = playbooks if playbooks is not None else default_playbooks()
        self.mem: dict[str, dict[str, Any]] = {pb.name: {} for pb in self.playbooks}
        self.last_decision: dict[str, float] = {}
        self.seen_msgs: set[int] = set()
        self.operator_log: list[tuple[float, str]] = []
        self._chat_ready = False
        self._last_observation = 0.0
        self._last_anomalies: frozenset[str] = frozenset()
        self._last_save = 0.0
        self._last_catalog_try = 0.0
        self._state_path = Path(cfg.runtime.state_file)
        self.ticks = 0
        self._stop = asyncio.Event()
        self._bg: list[asyncio.Task[None]] = []
        self._diagnosed: dict[str, float] = {}
        self._plans: dict[int, tuple[Intent, int, list[Step], str | None]] = {}
        self.gate.checkpoint = lambda: (not self.recorder or not self.recorder.storage_error) and self._save_state(force=True)
        self.incidents = IncidentCenter(lambda kind, payload, **kw: self.recorder.event(kind, payload, **kw) if self.recorder else None)
        self.printer_supervisor = PrinterSupervisor(self, self.incidents)
        self._incident_delivery = None

    # ------------------------------------------------------------------ жизненный цикл

    @property
    def llm_enabled(self):
        return self.policy.agent_mode != 'off'

    @llm_enabled.setter
    def llm_enabled(self, enabled):
        self.set_mode('autonomous' if enabled else 'off', True)

    def set_mode(self, mode, algorithms):
        self.policy.change(mode, algorithms)
        self._plans.clear()
        if self.brain is not None and self.brain.task is not None:
            self.brain.task.cancel()
            self.brain.dispatcher.busy = False
            self.brain.dispatcher.pending.clear()
        return self.policy.public()

    def stop(self) -> None:
        """Остановить цикл после текущего такта."""
        self._stop.set()
        self.client.stop()

    async def run(self, max_ticks: int | None = None) -> None:
        """Работать, пока не остановят (или пока не обработано ``max_ticks`` телеметрий — для тестов)."""
        self._load_state()
        self.log.write(
            "start", agent=AGENT_NAME, version=git_version(), models=self.models,
            config={
                "dome_url": self.settings.dome_url, "dry_run": self.settings.dry_run, "allow_control": self.settings.allow_control,
                "can_control": self.settings.can_control, "key_sources": self.settings.sources, "playbooks": [p.name for p in self.playbooks],
                "brain": "llm" if self.brain is not None else "rules",
            },
        )  # fmt: skip
        reason = "завершение работы"
        task = asyncio.create_task(self.client.run())
        try:
            while not self._stop.is_set():
                if not await self.client.wait_telemetry(timeout=5.0):
                    await self._idle()
                    continue
                await self.tick()
                if max_ticks is not None and self.ticks >= max_ticks:
                    break
        except (KeyboardInterrupt, asyncio.CancelledError):
            reason = "остановка по сигналу"
        except Exception as exc:  # noqa: BLE001 - причина остановки должна попасть в лог
            reason = f"сбой: {exc!r}"
            self.log.write("error", where="runner.run", error=repr(exc), traceback=traceback.format_exc()[-1500:])
            raise
        finally:
            self.client.stop()
            for bg in self._bg:
                bg.cancel()
            if self._manual_task is not None:
                self._manual_task.cancel()
            if self.brain is not None:
                await self.brain.close()
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                task.cancel()
            self._save_state(force=True)
            self.log.write("stop", reason=reason, stats=self.gate.stats)
            self.log.close()
            if self.recorder is not None:
                await asyncio.to_thread(self.recorder.close)

    # ------------------------------------------------------------------ события связи

    def _on_client_event(self, event: str, details: dict[str, Any]) -> None:
        if event in ('disconnected', 'revoked'):
            self.state.role, self.state.wall = '', 0
            self.catalog, self._catalog_msg = Catalog(), None
            self.policy.policy_epoch += 1
            self._plans.clear()
        if event == "catalog":
            self.log.write("connection", event="connected", details={"catalog_nodes": details.get("nodes"), "success": details.get("success")})
            return
        if event in ("connected", "auth", "disconnected", "revoked"):
            fields: dict[str, Any] = {"event": event, "details": {k: v for k, v in details.items() if k not in ("role", "team")}}
            if event == "auth":
                fields.update(role=details.get("role"), team=details.get("team"))
            self.log.write("connection", **fields)

    # ------------------------------------------------------------------ такт

    async def tick(self) -> None:
        """Обработать новую телеметрию."""
        msg = self.client.telemetry
        if not msg:
            return
        now = self.clock()
        data = msg.get("data") or {}
        self.state.update(data, str(msg.get("role") or self.client.role), now)
        self.log.set_dome_time(data.get("game_time"))
        self._refresh_catalog()
        chat_new = self._collect_chat(now)
        await self._ensure_catalog(now)
        completed = self.gate.check_pending()
        for pending in completed:
            self.printer_supervisor.verification(pending)
        for pending in completed:
            plan = self._plans.pop(pending.action_seq, None)
            if plan is None:
                continue
            intent, decision, remaining, session = plan
            if pending.verified and session == self.client.session_id and self.state.role == "team" and self.policy.algorithms_enabled:
                await self._advance_plan(intent, decision, remaining)
            else:
                self.log.write("observation", ref=decision, summary="план прерван; требуется новое решение",
                               metrics={"verified": pending.verified, "session_changed": session != self.client.session_id}, anomalies=[])
                if intent.say_always:
                    await self.say("Результат команды не подтверждён. Зависимые шаги остановлены; проверьте состояние оборудования.", decision)
        await self._manual_step()  # ручные команды Оператора — раньше автоматических решений
        classify_live(self.state, self.cfg, self.incidents, now)
        route = route_playbooks(self, now, chat_new)
        intents = []
        for intent in route.intents:
            # Printer resume has exactly one owner: the 12-second supervisor.
            if any(s.node == 'printer_3d_01' and s.action == 'resume' for s in intent.steps):
                continue
            intents.append(intent)
        intents += self.printer_supervisor.step(now)
        use_llm = self.brain is not None and self.llm_enabled and self.brain.healthy
        intents.sort(key=lambda i: i.priority)
        for intent in intents:
            await self.execute(intent, now)
        if self.state.role == 'team' and use_llm:
            epoch, session = self.policy.policy_epoch, self.client.session_id
            job_files = {node: self.state.get(f'{node}.file_name') for node in ('printer_3d_01', 'cnc_01')}
            async def current_result(intent):
                if epoch == self.policy.policy_epoch and session == self.client.session_id and self.llm_enabled and not self._stop.is_set():
                    if any(s.action == 'resume' and self.state.get(f'{s.node}.file_name') != job_files.get(s.node) for s in intent.steps):
                        return
                    await self.execute(intent, self.clock())
            self.brain.step(chat_new, current_result, covered_nodes=route.covered_nodes, algorithm_summary=route.summary)
        elif self.brain is not None:
            self.brain.mark_seen(chat_new)
        if self._incident_delivery is None or self._incident_delivery.done():
            self._incident_delivery = asyncio.create_task(self._deliver_incidents())
            self._bg.append(self._incident_delivery)
        self._observe(now)
        self._save_state()
        self._bg = [t for t in self._bg if not t.done()]
        self.ticks += 1

    async def _idle(self) -> None:
        """Нет телеметрии: раз в минуту отмечаем это в наблюдениях, ничего не решаем."""
        now = self.clock()
        classify_live(self.state, self.cfg, self.incidents, now)
        if self._incident_delivery is None or self._incident_delivery.done():
            self._incident_delivery = asyncio.create_task(self._deliver_incidents())
            self._bg.append(self._incident_delivery)
        if self.client.connected and now - self._last_observation >= self.cfg.runtime.observation_interval_s:
            self._last_observation = now
            self.log.write("observation", summary="телеметрия не приходит", metrics={"connected": True}, anomalies=["нет телеметрии"])

    # ------------------------------------------------------------------ каталог и чат

    def _refresh_catalog(self) -> None:
        msg = self.client.catalog_message
        if msg is not None and msg is not self._catalog_msg:
            self._catalog_msg = msg
            self.catalog = Catalog.from_response(msg)
            if self.brain is not None:
                self.brain.set_catalog([n for n in msg.get("nodes") or [] if isinstance(n, dict)])

    async def _ensure_catalog(self, now: float) -> None:
        if self.catalog or not self.client.connected or now - self._last_catalog_try < CATALOG_RETRY_S:
            return
        self._last_catalog_try = now
        try:
            self.client.catalog_message = await self.client.request("describe_nodes")
            self._refresh_catalog()
        except Exception as exc:  # noqa: BLE001
            self.log.write("error", where="describe_nodes", error=repr(exc))

    def _operator_texts(self, since: float) -> list[str]:
        return [t for ts, t in self.operator_log if ts >= since]

    def _collect_chat(self, now: float) -> list[dict[str, Any]]:
        """Новые сообщения чата (в порядке прихода). После старта — ещё и неотвеченные из истории."""
        new: list[dict[str, Any]] = []
        if not self._chat_ready:
            self._chat_ready = True
            recent = self.state.get("meeting_01.recent_messages", []) or []
            for m in unanswered_operator_messages([m for m in recent if isinstance(m, dict)]):
                if self._take(m, now, history=True):
                    new.append(m)
        while not self.client.chat.empty():
            m = self.client.chat.get_nowait().get("message") or {}
            if self._take(m, now):
                new.append(m)
        self.operator_log = [(t, x) for t, x in self.operator_log if now - t <= OPERATOR_TEXTS_KEEP_S]
        return new

    def _take(self, m: dict[str, Any], now: float, history: bool = False) -> bool:
        mid = m.get("id")
        if not isinstance(mid, int) or mid in self.seen_msgs:
            return False
        self.seen_msgs.add(mid)
        sender = m.get("sender") or {}
        if sender.get("role") != "operator":
            return False
        text = str(m.get("text", ""))
        self.operator_log.append((now, text))
        self.log.write("message", direction="in", sender="operator", text=text, message_id=mid, from_history=history or None)
        return True

    # ------------------------------------------------------------------ решение → действие

    async def execute(self, intent: Intent, now: float) -> None:
        """Записать решение, выполнить шаги через Gate, при успехе — сообщить Оператору."""
        if intent.human_escalation:
            h = intent.human_escalation
            self.incidents.open('human:' + h['node_id'] + ':' + h['incident'], h['severity'], h['node_id'],
                h['incident'], h['required_action'], {'evidence': h.get('facts', []), 'already_done': h.get('already_done', [])})
        actor = 'agent' if intent.source in ('llm', 'mixed') else 'algorithm'
        epoch = self.policy.policy_epoch
        if self.policy.reject(actor, epoch, '', ''):
            if actor == 'agent' and self.policy.agent_mode == 'recommendation':
                self.log.write('decision', situation=intent.situation, cause=intent.cause, evidence=intent.evidence,
                               source='llm', plan=intent.plan, proposed_only=True, policy_epoch=epoch)
            return
        cooldown = self.cfg.runtime.decision_cooldown_s if intent.cooldown_s is None else intent.cooldown_s
        last = self.last_decision.get(intent.key)
        if last is not None and now - last < cooldown:
            return
        self.last_decision[intent.key] = now
        refs: list[dict[str, Any]] = []
        if self.advisor is not None and intent.rag_query:
            try:
                refs = self.advisor.references(intent.rag_query)
            except Exception as exc:  # noqa: BLE001
                self.log.write("error", where="rag", error=repr(exc))
        fields: dict[str, Any] = {
            "situation": intent.situation, "cause": intent.cause, "confidence": intent.confidence, "evidence": intent.evidence,
            "plan": intent.plan, "source": "mixed" if refs and intent.source == "rules" else intent.source, "priority": intent.priority,
        }  # fmt: skip
        if refs or intent.rag_refs:
            fields["rag_refs"] = intent.rag_refs + [r["ref"] for r in refs]
        decision = self.log.write("decision", **fields)
        await self._advance_plan(intent, decision, list(intent.steps))

    async def _advance_plan(self, intent: Intent, decision: int, steps: list[Step]) -> None:
        """Advance only after a verified effect. Every next command re-enters Gate."""
        results: list[GateResult] = []
        epoch = self.policy.policy_epoch
        for index, step in enumerate(steps):
            results.append(await self.gate.submit(Cmd(step.node, step.action, step.value, step.reason or intent.cause, decision, step.expect,
                actor='agent' if intent.source in ('llm', 'mixed') else 'algorithm', policy_epoch=epoch,
                recovery_owner='printer_supervisor' if intent.key.startswith('supervisor:resume:') else None,
                operator_requested=intent.say_always and intent.source in ('llm', 'mixed'))))
            if epoch != self.policy.policy_epoch:
                return
            if not results[-1].ok or (results[-1].status == "sent" and step.expect is not None):
                # A plan is a hypothesis. Re-plan from fresh telemetry after each effect;
                # never execute its dependent tail after rejection or just an ACK.
                self.log.write("observation", ref=decision, summary="план ожидает проверки эффекта" if results[-1].ok else "план прерван",
                               anomalies=[], metrics={"action_seq": results[-1].action_seq})
                if results[-1].ok:
                    if not hasattr(self, "_plans"):
                        self._plans = {}
                    session = getattr(getattr(self, "client", None), "session_id", None)
                    self._plans[results[-1].action_seq] = (intent, decision, steps[index + 1:], session)
                elif intent.say_always:
                    await self.say(f"Не выполнено: {step.node}.{step.action} — {results[-1].reason}. Рекомендация: проверьте причину отказа; зависимые шаги отложены.", decision)
                if not results[-1].ok:
                    self.incidents.open('action:' + step.node + ':' + step.action, 'red' if results[-1].status == 'ambiguous' else 'yellow',
                        step.node, results[-1].reason or 'Действие не выполнено', 'Проверьте итог и причину отказа; автоматического повтора нет',
                        {'action_seq': results[-1].action_seq, 'status': results[-1].status})
                return
        if intent.llm_question and self.advisor is not None and self.llm_enabled:
            self._spawn(self._answer_operator(intent, decision))
        elif intent.say and all(r.ok for r in results):
            await self.say(intent.say, decision)
        elif intent.say and intent.say_always:
            failed = "; ".join(f"{s.node}.{s.action} — {r.reason}" for s, r in zip(intent.steps, results) if not r.ok)
            await self.say(f"{intent.say} Не выполнено: {failed}.", decision)

    async def say(self, text: str, ref: int) -> GateResult:
        """Сообщение Оператору: команда ``post_message`` через Gate и запись ``message``."""
        text = text if len(text) <= 900 else text[:897] + "…"
        res = await self.gate.submit(Cmd("meeting_01", "post_message", {"text": text, "to": "operator", "name": AGENT_NAME}, "сообщение Оператору", ref,
            actor='algorithm', policy_epoch=self.policy.policy_epoch))
        if res.ok:
            self.log.write("message", ref=ref, direction="out", to="operator", text=text, dry_run=(res.status == "dry_run") or None)
        return res

    async def _deliver_incidents(self):
        for item in self.incidents.pending_notifications():
            if self.policy.operating_state == 'shutting_down':
                return
            if self.policy.api_scope != 'full':
                self.incidents.delivered(item, 'unavailable_history_only')
                continue
            ref = self.log.write('decision', situation='Вызов человека', cause=item.reason,
                                 evidence=[item.facts], source='rules', plan=[item.required_action])
            text = f'[{item.severity.upper()}] {item.node_id}: {item.reason}. {item.required_action}'[:900]
            result = await self.gate.submit(Cmd('meeting_01', 'post_message', {'text': text, 'to': 'operator', 'name': AGENT_NAME},
                                               'Вызов оператора', ref, actor='safety', policy_epoch=self.policy.policy_epoch))
            self.incidents.delivered(item, 'accepted' if result.status == 'sent' else 'not_sent' if result.status == 'dry_run' else result.status)

    # ------------------------------------------------------------------ ручные команды Оператора

    async def _manual_step(self) -> None:
        """Забрать результат фонового опроса очереди ручных команд, исполнить их и начать следующий опрос."""
        if self.manual is None:
            return
        task = self._manual_task
        if task is not None and task.done():
            self._manual_task = None
            try:
                fresh, stale = task.result()
            except Exception as exc:  # noqa: BLE001 - БД недоступна: команды подождут в очереди сервиса
                self.log.write("error", where="manual_controls", error=repr(exc))
                fresh, stale = [], []
            for item in stale:
                self._dispatch_event(item, "skipped", f"команда старше {self.manual.max_age_s:.0f} с: состояние могло измениться, не исполнена")
            for item in fresh:
                if item["event"]["event_id"] not in self.manual_done:
                    await self.manual_command(item)
        if self._manual_task is None:
            self._manual_task = asyncio.create_task(asyncio.to_thread(self.manual.poll))

    async def manual_command(self, item: dict[str, Any]) -> GateResult | None:
        """Ручная команда Оператора: решение ``source=operator`` → Gate (те же правила) → Купол → проверка эффекта.

        LLM не участвует. Вызывается из опроса очереди или напрямую панелью; повторно намерение не исполняется (``None``).
        """
        ev = item["event"]
        if ev["event_id"] in self.manual_done:
            return None
        self.manual_done.add(ev["event_id"])
        p, meta = ev["payload"], ev.get("metadata") or {}
        node, action, value = str(p.get("node_id")), str(p.get("action")), p.get("value")
        label = f"{node}.{action}" + (f"({json.dumps(value, ensure_ascii=False, default=str)})" if value is not None else "")
        decision = self.log.write(
            "decision", situation=f"Ручная команда Оператора: {label}", cause=str(meta.get("reason") or "команда из веб-интерфейса"),
            confidence=1.0, evidence=[{"intent_event_id": ev["event_id"], "server_validation": (item.get("validation") or {}).get("status")}],
            plan=[f"{label} через Gate" + (" (подтверждено человеком)" if meta.get("confirmed") is True else "")], source="operator", priority=1, intent_event_id=ev["event_id"], operator=meta.get("operator"),
        )  # fmt: skip
        confirmed = meta.get("confirmed") is True
        reason = 'ручная команда Оператора; все проверки Gate сохраняются'
        res = await self.gate.submit(Cmd(node, action, value, reason, decision, expect_for(node, action, value), confirmed=confirmed,
            actor='operator', policy_epoch=meta.get('policy_epoch', -1), operation_id=ev['event_id']))
        self._dispatch_event(item, res.status, res.reason, decision=decision, result=res)
        return res

    def _dispatch_event(self, item: dict[str, Any], status: str, reason: str, decision: int | None = None, result: GateResult | None = None) -> None:
        """Итог ручной команды для интерфейса: отправлена / dry_run / отклонена Gate / сбой / пропущена."""
        if self.recorder is None:
            return
        ev = item["event"]
        response = (result.response if result else None) or {}
        self.recorder.event(
            "control_dispatch",
            {"intent_event_id": ev["event_id"], "status": status, "reason": reason or None, "decision_seq": decision,
             "action_seq": result.action_seq if result else None, "response": response or None},
            session_id=self.client.session_id, request_id=response.get("request_id"), node_id=(ev.get("payload") or {}).get("node_id"),
            metadata={"intent_event_id": ev["event_id"], "log_run_id": self.log.run_id},
        )  # fmt: skip

    # ------------------------------------------------------------------ модель (только советы и текст)

    def _spawn(self, coro: Any) -> None:
        self._bg.append(asyncio.create_task(coro))

    def context_summary(self) -> dict[str, Any]:
        """Краткое состояние для запросов к модели."""
        st = self.state
        e = assess_energy(st, self.cfg.checks)
        return {
            "SOC_%": e.soc, "режим_инвертора": e.mode, "принтер": st.get("printer_3d_01.state"), "фрезер": st.get("cnc_01.state"),
            "CO2_ppm": {s: st.num(f"{s}.co2_ppm") for s in self.cfg.checks.co2.sensors}, "замечания": self.anomalies()[:6],
        }  # fmt: skip

    async def _answer_operator(self, intent: Intent, decision: int) -> None:
        """Ответ на свободный вопрос: модель в потоке; нет ответа — запасной текст правил."""
        assert self.advisor is not None and intent.llm_question
        epoch = self.policy.policy_epoch
        loop = asyncio.get_running_loop()
        try:
            text = await loop.run_in_executor(None, self.advisor.answer, intent.llm_question, self.context_summary(), decision)
        except Exception as exc:  # noqa: BLE001
            self.log.write("error", where="advisor.answer", error=repr(exc), ref=decision)
            text = None
        final = text or intent.say
        if final and self.llm_enabled and epoch == self.policy.policy_epoch:
            await self.say(final, decision)

    def uncovered(self) -> dict[str, dict[str, Any]]:
        """Ситуации, которые плейбуки не разбирают: тема → контекст (кандидаты на подсказку модели)."""
        st = self.state
        out: dict[str, dict[str, Any]] = {}
        handled = {"IL_CO2", "IL_CONDENSATE", "IL_SMOKE", "IL_LOAD_SHED", "IL_OVERCURRENT"}
        for il, body in (st.get("dome_automation_01.interlocks", {}) or {}).items():
            if isinstance(body, dict) and body.get("state") == "TRIPPED" and il not in handled:
                out[f"интерлок {il} сработал"] = {"интерлок": il, "данные": {k: v for k, v in body.items() if k != "title"}}
        known_holds = {"BED_COLLISION", "FILAMENT_RUNOUT", "UNDERVOLTAGE"} | handled
        for node in ("printer_3d_01", "cnc_01"):
            reason = st.get(f"{node}.hold_reason")
            if reason and reason not in known_holds and not str(reason).startswith("IL_"):
                out[f"{node}: удержание {reason}"] = {"узел": node, "hold_reason": reason, "state": st.get(f"{node}.state")}
        code = st.get("solar_inverter_01.error_code")
        if code and code != "E09":
            out[f"инвертор: ошибка {code}"] = {"код": code, "текст": st.get("solar_inverter_01.error_text"), "температура": st.get("solar_inverter_01.inverter_temp_c")}
        return out

    def _diagnose_uncovered(self, now: float) -> None:
        if self.advisor is None or any(not t.done() for t in self._bg):
            return
        for topic, ctx in self.uncovered().items():
            if now - self._diagnosed.get(topic, -1e9) < 900:
                continue
            self._diagnosed[topic] = now
            self._spawn(self._diagnose(topic, ctx))
            break

    async def _diagnose(self, topic: str, context: dict[str, Any]) -> None:
        assert self.advisor is not None
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, self.advisor.diagnose, topic, {**context, "состояние": self.context_summary()}, None)
        except Exception as exc:  # noqa: BLE001
            self.log.write("error", where="advisor.diagnose", error=repr(exc))
            return
        if result is None:
            return
        refs = self.advisor.references(topic)
        fields: dict[str, Any] = {
            "situation": result.situation or topic, "cause": result.cause, "confidence": result.confidence,
            "evidence": [{"detail": topic, **{k: v for k, v in context.items() if k != "данные"}}],
            "plan": result.plan, "source": "mixed" if refs else "llm", "priority": 4,
        }  # fmt: skip
        if refs:
            fields["rag_refs"] = [r["ref"] for r in refs]
        decision = self.log.write("decision", **fields)
        if result.say and result.confidence >= 0.5:
            await self.say(result.say, decision)

    # ------------------------------------------------------------------ наблюдение

    def anomalies(self) -> list[str]:
        """Что агент считает необычным сейчас (для наблюдений и ответов Оператору)."""
        st, cfg = self.state, self.cfg.checks
        out: list[str] = []
        a = assess_co2(st, cfg)
        for s in sorted(a.suspects):
            out.append(f"{s}.co2_ppm расходится с остальными ({a.values.get(s, 0):.0f} ppm)")
        for s in sorted(a.untrusted):
            out.append(f"{s}.co2_ppm помечен недостоверным")
        for il, body in (st.get("dome_automation_01.interlocks", {}) or {}).items():
            if isinstance(body, dict) and body.get("state") not in (None, "ARMED"):
                out.append(f"интерлок {il}: {body.get('state')}")
        for node in ("printer_3d_01", "cnc_01"):
            if st.get(f"{node}.hold_reason"):
                out.append(f"{node}: удержание {st.get(f'{node}.hold_reason')}")
        p = assess_printer(st)
        if p.webhook_lies:
            out.append("printer_3d_01.webhook_state врёт (COMPLETED при печати)")
        for n in st.frozen_nodes():
            out.append(f"{n}: данные устарели")
        if st.get("solar_inverter_01.error") is True:
            out.append(f"инвертор: ошибка {st.get('solar_inverter_01.error_code')}")
        return out

    def _observe(self, now: float) -> None:
        anomalies = self.anomalies()
        self.mem.setdefault("tasks", {})["issues"] = anomalies[:3]
        changed = frozenset(anomalies) != self._last_anomalies
        if now - self._last_observation < self.cfg.runtime.observation_interval_s and not changed:
            return
        self._last_observation = now
        self._last_anomalies = frozenset(anomalies)
        st = self.state
        e = assess_energy(st, self.cfg.checks)
        co2 = {s: st.num(f"{s}.co2_ppm") for s in self.cfg.checks.co2.sensors}
        metrics: dict[str, Any] = {
            "soc_pct": e.soc, "inverter_mode": e.mode, "pv_w": e.pv_w, "load_w": e.load_w,
            "printer": st.get("printer_3d_01.state"), "printer_progress": st.num("printer_3d_01.progress_pct"),
            "cnc": st.get("cnc_01.state"), "cnc_progress": st.num("cnc_01.progress_pct"),
            "co2_main": co2.get(str(st.get("dome_automation_01.primary_co2_sensor", ""))),
            **{f"co2_{s[-2:]}": v for s, v in co2.items()}, "role": st.role,
        }  # fmt: skip
        summary = (
            f"SOC {e.soc if e.soc is not None else '?'} %, {e.mode or '?'}; принтер {metrics['printer']}, фрезер {metrics['cnc']}; "
            f"CO₂ {', '.join(f'{v:.0f}' if v is not None else '—' for v in co2.values())}"
        )
        self.log.write("observation", summary=summary, metrics=metrics, anomalies=anomalies)

    # ------------------------------------------------------------------ сохранение состояния

    def _save_state(self, force: bool = False) -> bool:
        now = self.clock()
        if not force and now - self._last_save < STATE_SAVE_EVERY_S:
            return True
        self._last_save = now
        payload = {
            "saved_at": now, "mem": self.mem, "last_decision": self.last_decision,
            "seen_msgs": sorted(self.seen_msgs)[-200:], "operator_log": self.operator_log[-40:],
            "failed_until": self.gate.export_failed(), "manual_cursor": self.manual.cursor if self.manual else None,
            "gate_runtime": self.gate.export_runtime(),
        }  # fmt: skip
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False, default=str))
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(self._state_path)
            return True
        except OSError as exc:
            self.log.write("error", where="save_state", error=repr(exc))
            return False

    def _load_state(self) -> None:
        if not self._state_path.is_file():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            self.mem.update({k: v for k, v in (data.get("mem") or {}).items() if isinstance(v, dict)})
            self.last_decision = {k: float(v) for k, v in (data.get("last_decision") or {}).items()}
            self.seen_msgs = {int(x) for x in data.get("seen_msgs") or []}
            self.operator_log = [(float(t), str(x)) for t, x in data.get("operator_log") or []]
            self.gate.import_failed(data.get("failed_until") or {})
            self.gate.import_runtime(data.get("gate_runtime") or {})
            if self.manual is not None and isinstance(data.get("manual_cursor"), int):
                self.manual.cursor = data["manual_cursor"]  # команды, пришедшие за время перезапуска, не теряются
            self.log.write("observation", summary="состояние восстановлено после перезапуска", metrics={"saved_at": data.get("saved_at")}, anomalies=[])
        except (OSError, ValueError, TypeError) as exc:
            self.log.write("error", where="load_state", error=repr(exc))
