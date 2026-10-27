"""LLM-основа агента: решения принимает LLM-агент Купола (``llm_agent/`` в корне репозитория).

Поводы вызвать модель находит диспетчер ``llm_agent.dispatcher`` (события и контрольные таймеры,
``rag/AGENT_TRIGGERS.md``); промпт собирается из живого ``DomeState``, каталога ``describe_nodes``, графа
влияний, RAG и журнала этого агента. Решение модели проверяет ``llm_agent.validate`` (и сервис БД, если он
подключён), затем оно превращается в ``Intent`` и исполняется рантаймом как раньше: шаги только через
``Gate`` (политика ``dome.yaml``, защитные правила, режим «только чтение»), проверка эффекта по телеметрии.

Модель вызывается в отдельном потоке: такт, Gate и проверка эффекта её не ждут. Когда модель недоступна
(нет ключа, сбои подряд), рантайм работает на плейбуках.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kupol.dome.agentlog import AgentLog
from kupol.dome.datalog import DataApiRecorder
from kupol.dome.env import Settings
from kupol.dome.expects import expect_for
from kupol.dome.playbooks.base import Intent, Step
from kupol.dome.state import DomeState
from kupol.dome.control import DECISION_POLICY_EPOCH

REPO_ROOT = Path(__file__).resolve().parents[4]
FAILURES_TO_FALLBACK = 3  # сбоев модели подряд, после которых решения временно отдаются плейбукам
FALLBACK_S = 300.0
AGENT_FAILURE_WINDOW_S = 600.0  # столько секунд считаем сбои собственных команд модели
AGENT_FAILURES_BEFORE_HUMAN = 3  # больше сбоев за окно — вызов человека вместо нового разбора моделью
SEVERITY_PRIORITY = {"critical": 1, "warning": 3, "info": 5}
REQUEST_MISSED = {"refused": "агент отказался выполнять", "partial": "выполнено только частично"}
JOURNAL_KINDS = ("decision", "action", "verification", "message")
ADVISORY = (
    "\n\nРежим: управление агента выключено — твои команды в Купол не отправятся. Предлагай действия как обычно "
    "(они записываются), а в operator_message пиши Оператору рекомендацию, что сделать вручную через панель."
)

try:
    import llm_agent  # noqa: F401
except ImportError:  # запуск из agent/: пакет LLM-слоя лежит в корне репозитория
    sys.path.insert(0, str(REPO_ROOT))

from llm_agent import DomeLLMAgent  # noqa: E402
from llm_agent import Settings as LlmSettings  # noqa: E402
from llm_agent.agent import make_retriever  # noqa: E402
from llm_agent.context.catalog import NodeCatalog  # noqa: E402
from llm_agent.dispatcher import Dispatcher, Fired  # noqa: E402
from llm_agent.prompt_builder import ALGORITHMS_MARK  # noqa: E402
from llm_agent.triggers import (  # noqa: E402
    Trigger,
    gate_action_trigger,
    gate_rejection_trigger,
    gate_verification_trigger,
    playbook_failure_trigger,
    telemetry_age,
)
from llm_agent.schemas import AgentResponse, LLMCallInfo, ProposedAction  # noqa: E402
from llm_agent.storage.data_api import ApiControlChecker, DataApiClient, journal_record  # noqa: E402

ALGORITHMS_HEADER = (
    "\n\n" + ALGORITHMS_MARK + " работают параллельно с тобой и действуют по своим узлам первыми; твои команды по "
    "узлам, занятым алгоритмом, будут отложены. Что они делают сейчас:\n"
)

Execute = Callable[[Intent], Awaitable[None]]


class StateTelemetry:
    """Снимок для промпта из ``DomeState`` рантайма; ``timestamp`` — время кадра Купола."""

    def __init__(self, state: DomeState, frame: Callable[[], dict[str, Any] | None], clock: Callable[[], float] = time.time) -> None:
        self.state = state
        self.frame = frame
        self.clock = clock

    def latest(self) -> dict[str, Any]:
        msg = self.frame() or {}
        observed = datetime.fromtimestamp(self.state.wall, timezone.utc).isoformat() if self.state.wall else None
        return {"timestamp": msg.get("timestamp"), "game_time": dict(self.state.game_time or {}),
                "nodes": dict(self.state.nodes), "observed_at": observed,
                "telemetry_age_s": max(0.0, self.clock() - self.state.wall) if self.state.wall else None}

    def history(self, node_id: str, param: str, limit: int = 20) -> list[dict[str, Any]]:
        return []


class LogJournal:
    """Журнал решений для промпта: записи ``AgentLog`` этого агента (решения, команды, проверки, чат)."""

    def __init__(self, keep: int = 200) -> None:
        self._records: deque[dict[str, Any]] = deque(maxlen=keep)
        self._lock = threading.Lock()

    def on_log(self, kind: str, rec: dict[str, Any]) -> None:
        if kind not in JOURNAL_KINDS:
            return
        ev = {"event_type": f"agent_{kind}", "payload": rec, "source": "agent", "ingested_at": rec.get("ts"),
              "metadata": {"dome_time": rec.get("dome_time")}}
        with self._lock:
            self._records.append(journal_record(ev))

    def recent(self, limit: int = 15) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._records)[-limit:]

    def append(self, record: dict[str, Any]) -> None:
        # Успешные решения попадают сюда из лога (Runner.execute); здесь — сбои модели и проверки результата
        if record.get("kind") == "decision" and record.get("status") != "failed":
            return
        with self._lock:
            self._records.append(record)


class _Sink:
    """События LLM-слоя (llm_request/llm_response/decision/verification) — в БД через рекордер агента."""

    def __init__(self, recorder: DataApiRecorder | None, session: Callable[[], str | None], journal: LogJournal, policy=lambda: None) -> None:
        self.recorder = recorder
        self.session = session
        self.journal = journal
        self.policy = policy

    def emit(self, event_type: str, payload: Any, **kwargs: Any) -> str:
        policy = self.policy()
        if policy is not None:
            kwargs['metadata'] = {**kwargs.get('metadata', {}), 'policy_epoch': DECISION_POLICY_EPOCH.get() if DECISION_POLICY_EPOCH.get() is not None else policy.policy_epoch, 'agent_mode': policy.agent_mode}
        if event_type == "verification":
            self.journal.append({"actor": "agent", "kind": "verification", **payload})
        if self.recorder is None:
            return str(uuid.uuid4())  # без БД: id нужен только для ссылок внутри процесса
        return self.recorder.event(event_type, payload, session_id=self.session(), **kwargs)


class _Catalog:
    def __init__(self, nodes: list[dict[str, Any]]) -> None:
        self._nodes = nodes

    def nodes(self) -> list[dict[str, Any]]:
        return self._nodes


class _SessionChecker:
    """Предпроверка команд сервисом БД в текущей WS-сессии (она меняется при переподключении)."""

    def __init__(self, checker: ApiControlChecker, session: Callable[[], str | None]) -> None:
        self.checker = checker
        self.session = session

    def check(self, node_id: str, action: str, value: Any, metadata: dict[str, Any]) -> dict[str, Any]:
        session = self.session()
        if not session:
            return {"status": "unavailable", "reasons": ["нет WS-сессии"], "request_id": None, "intent_event_id": None}
        self.checker.session_id = session
        return self.checker.check(node_id, action, value, metadata)


def _value(v: Any) -> str:
    return "" if v is None else json.dumps(v, ensure_ascii=False, default=str)[:80]


def to_intent(response: AgentResponse, fired: Fired, stale_s: float = 30.0) -> Intent:
    """Решение модели → намерение рантайма. Сообщение Оператору уходит, только если все шаги приняты Gate."""
    d = response.decision
    assert d is not None
    steps: list[Step] = []
    plan: list[str] = []
    blocked = False
    deferred: list[str] = []
    for v in response.verdicts:
        a: ProposedAction = v.action
        if a.node_id == "meeting_01" and a.action == "post_message":
            continue
        if d.request_human is not None and d.request_human.severity == 'red':
            plan.append(f'Заблокировано до человека: {a.node_id}.{a.action}')
            continue
        label = f"{a.node_id}.{a.action}({_value(a.value)})"
        if v.accepted and not blocked and not steps:
            exp = expect_for(a.node_id, a.action, a.value)
            exp.timeout_s = max(exp.timeout_s or 0, a.verify_after_s)
            steps.append(Step(a.node_id, a.action, a.value, exp, (a.reason or d.cause)[:200]))
            plan.append(f"{label}: {a.reason}" + (f" → ожидаем {a.expected}" if a.expected else ""))
        elif v.accepted:
            plan.append(f"отложить до нового решения по телеметрии: {label}")
            deferred.append(label)
        else:
            blocked = True
            plan.append(f"не делать {label}: {'; '.join(v.reasons)}")
    plan += [f"наблюдать: {w}" for w in d.wait_and_watch]
    say = d.operator_message.text.strip() if d.operator_message.needed and d.operator_message.text.strip() else None
    if say and not any(v.accepted for v in response.verdicts if v.action.node_id == "meeting_01"):
        say = None  # сообщение не прошло проверку LLM-слоя (длина, пустота)
    refused = [f"{v.action.node_id}.{v.action.action} — {'; '.join(v.reasons)}" for v in response.verdicts
               if not v.accepted and v.action.node_id != "meeting_01"]
    operator = any(t.kind == "operator" for t in fired.triggers)
    if operator:
        status = d.operator_message.request_status
        if status in REQUEST_MISSED:
            refused.insert(0, f"требование Оператора — {REQUEST_MISSED[status]}")
        elif status == "executing" and not steps and not refused:
            refused.insert(0, "требование Оператора — агент не предложил команд для него")
        elif status == "already_done" and (age := telemetry_age(fired.snapshot or {}, stale_s)) is not None:
            # «Уже выполнено» по старому кадру — догадка, а не факт
            refused.insert(0, f"требование Оператора — выполнение не подтверждено: телеметрия устарела ({age:.0f} с)")
    return Intent(
        key=f"llm:{response.decision_id}", situation=d.situation, cause=d.cause or "не установлена", confidence=d.confidence,
        evidence=[e.model_dump(exclude_none=True) for e in d.evidence] or [{"trigger": t.text[:300]} for t in fired.triggers],
        plan=plan or ["наблюдать"], steps=steps, say=say, priority=SEVERITY_PRIORITY.get(d.severity, 5),
        impact_kind=d.impact_kind, authenticity=d.authenticity, causal_chain=list(d.causal_chain),
        classification_issues=list(response.classification_issues),
        cooldown_s=0.0, source="mixed" if d.rag_refs else "llm", rag_refs=list(d.rag_refs),
        say_always=operator, refused=refused,
        human_escalation=d.request_human.model_dump(mode='json') if d.request_human else None, deferred=deferred,
    )  # fmt: skip


class LlmBrain:
    """Диспетчер поводов + LLM-агент Купола, подключённые к рантайму агента."""

    def __init__(
        self,
        settings: Settings,
        log: AgentLog,
        state: DomeState,
        frame: Callable[[], dict[str, Any] | None],
        session: Callable[[], str | None],
        recorder: DataApiRecorder | None = None,
        llm: Any = None,
        llm_settings: LlmSettings | None = None,
        clock: Callable[[], float] = time.time,
        data_client: DataApiClient | None = None,
    ) -> None:
        s = llm_settings or LlmSettings.from_env()
        if settings.deepseek_key:
            s.deepseek_api_key = settings.deepseek_key
        if settings.deepseek_model:
            s.model = settings.deepseek_model
        s.data_api_url = None  # телеметрия и журнал — из этого процесса, в БД пишет рекордер агента
        s.state_dir = Path(settings.state_dir)
        self.settings = s
        self.log = log
        self.clock = clock
        self.journal = LogJournal()
        checker = None
        if settings.data_api_url and recorder is not None and recorder.run_id and s.data_api_check_controls:
            client = data_client or DataApiClient(settings.data_api_url, settings.data_api_token or None, s.data_api_timeout_s)
            checker = _SessionChecker(ApiControlChecker(client, recorder.run_id, ""), session)
        self.agent = DomeLLMAgent(
            s, StateTelemetry(state, frame, clock), self.journal, _Catalog([]), retriever=make_retriever(s), llm=llm,
            events=_Sink(recorder, session, self.journal, lambda: self.policy), control_checker=checker,
        )  # fmt: skip
        self.dispatcher = Dispatcher(self.agent, clock=clock)
        self.dispatcher.external_verification = True
        self.task: asyncio.Task[None] | None = None
        self.failures = 0
        self.down_until = 0.0
        self.calls = 0
        self.policy: Any = None  # рантайм подставляет ControlPolicy
        self.no_effect_cooldown_s = 120.0  # рантайм подставляет policy.no_effect_cooldown_s
        self._actions: dict[int, dict[str, Any]] = {}  # seq записи action → запись (для поводов по verification)
        self._timed_out: set[int] = set()
        self._feedback_cases: dict[Any, Trigger] = {}
        self._continue: dict[str, list[str]] = {}  # «узел.действие» выполненного шага -> отложенные шаги того же решения
        self._agent_failures: deque[float] = deque(maxlen=20)
        self.on_human: Callable[[Any], None] = lambda trigger: None

    @property
    def model(self) -> str:
        return self.settings.model

    @property
    def configured(self) -> bool:
        return bool(self.settings.deepseek_api_key) or self.agent._llm is not None

    @property
    def healthy(self) -> bool:
        """Решения принимает модель; ``False`` — рантайм работает на плейбуках."""
        return self.configured and self.clock() >= self.down_until

    def set_catalog(self, nodes: list[dict[str, Any]]) -> None:
        self.agent.catalog = NodeCatalog(nodes)
        self.agent.builder.catalog = self.agent.catalog

    def on_log(self, kind: str, rec: dict[str, Any]) -> None:
        self.journal.on_log(kind, rec)
        trigger = None
        if kind == 'action' and isinstance(rec.get('seq'), int):
            self._actions[rec['seq']] = rec
            if len(self._actions) > 500:
                first = next(iter(self._actions))
                self._actions.pop(first)
                self._timed_out.discard(first)
            trigger = gate_action_trigger(rec) or gate_rejection_trigger(rec)
            if trigger is not None and '|timeout|' in trigger.key:
                self._timed_out.add(rec['seq'])
        elif kind == 'verification':
            action = self._actions.get(rec.get('ref'), {})
            trigger = gate_verification_trigger(rec, action, rec.get('ref') in self._timed_out, self.no_effect_cooldown_s)
            self._timed_out.discard(rec.get('ref'))
            trigger = trigger or self._continuation(rec, action)
            rec = {**action, **rec, 'request': action.get('request', {})}
        if trigger is not None:
            req = rec.get('request') or {}
            if req.get('node_id') == 'meeting_01':
                return  # delivery errors must never create recursive chat/LLM traffic
            trigger.meta.update(node_id=req.get('node_id'), action=req.get('action'), actor=rec.get('actor', 'algorithm'),
                                policy_epoch=rec.get('policy_epoch'), pipeline_feedback=True)
            self.review_failure(trigger)

    def review_failure(self, trigger):
        policy = getattr(self, 'policy', None)
        epoch = policy.policy_epoch if policy else None
        if policy and (policy.operating_state == 'shutting_down' or trigger.meta.get('policy_epoch') not in (None, epoch)):
            return
        trigger.meta.setdefault('policy_epoch', epoch)
        identity = (epoch, trigger.key)
        if identity in self._feedback_cases:
            return
        self._feedback_cases[identity] = trigger
        if len(self._feedback_cases) > 1000:
            self._feedback_cases.pop(next(iter(self._feedback_cases)))
        can_analyze = self.healthy and (not policy or policy.agent_mode != 'off')
        if trigger.meta.get('actor') == 'agent' and trigger.kind != 'verification':
            # Ошибку собственной команды модель разбирает сама, но не по кругу: частые сбои идут человеку
            now = self.clock()
            self._agent_failures.append(now)
            if sum(1 for t in self._agent_failures if now - t < AGENT_FAILURE_WINDOW_S) > AGENT_FAILURES_BEFORE_HUMAN:
                can_analyze = False
        if trigger.kind == 'verification':
            if can_analyze:
                self.dispatcher.submit(trigger)
        elif can_analyze:
            self.dispatcher.submit(trigger)
        else:
            self.on_human(trigger)

    def _continuation(self, rec: dict[str, Any], action: dict[str, Any]) -> Trigger | None:
        """Шаг модели подтверждён Gate по телеметрии: вернуть её к отложенным шагам решения (новое решение по свежим данным)."""
        req = action.get('request') or {}
        if rec.get('ok') is not True or action.get('actor') != 'agent':
            return None
        if req.get('node_id') == 'meeting_01':
            return None
        rest = self._continue.pop(f"{req.get('node_id')}.{req.get('action')}", [])
        node = req.get('node_id')
        later = f" Отложено в этом решении: {'; '.join(rest)}." if rest else ""
        return Trigger("verification", f"command|continue|{rec.get('run_id')}|{rec.get('ref')}",
                       f"Проверка результата: шаг {req.get('node_id')}.{req.get('action')} подтверждён по телеметрии "
                       f"({rec.get('observed')}).{later} Реши по свежим данным, нужен ли следующий шаг",
                       {node} if node else set(), meta={"action_seq": rec.get("ref")})

    def playbook_failed(self, name, error):
        self.review_failure(playbook_failure_trigger(name, error))

    def playbook_recovered(self, name):
        for identity, trigger in list(self._feedback_cases.items()):
            if trigger.meta.get('playbook') == name:
                self.dispatcher.pending.pop(trigger.key, None)
                self._feedback_cases.pop(identity)

    def operator_request(self, text: str, event_id: str | None = None) -> None:
        """Требование Оператора из веб-панели: срочный повод для модели, ответ — рекомендация Оператору."""
        self.dispatcher.submit(Trigger(
            "operator", f"panel|{event_id or uuid.uuid4()}",
            f"Требование Оператора (веб-панель): «{text}». Выполни, если это допускают права и правила, и ответь "
            "Оператору в operator_message: что сделано, что нельзя и почему, что рекомендуешь дальше",
            {"meeting_01"}, [event_id] if event_id else [],
        ))  # fmt: skip

    def mark_seen(self, messages: list[dict[str, Any]]) -> None:
        """Сообщения чата, обработанные плейбуками (запасной режим): модель не отвечает на них повторно."""
        ids = [m["id"] for m in messages if isinstance(m, dict) and isinstance(m.get("id"), int)]
        if ids:
            self.dispatcher.operator.last_id = max(self.dispatcher.operator.last_id, *ids)

    def step(self, chat_new: list[dict[str, Any]], execute: Execute, *, algorithms: list[str] | None = None,
             handled_operator_ids: set[int] | None = None, source_events: list[dict] | None = None) -> None:
        """Такт: найти поводы; если пора — запустить вызов модели в фоне (результат исполнит ``execute``).

        Поводы по узлам, которые ведут плейбуки, не отбрасываются: модель видит всё, а ``algorithms`` — строки
        «что сделал или ждёт алгоритм» — добавляются к запросу отдельным разделом.
        """
        fired = self.dispatcher.poll(chat_new, handled_operator_ids=handled_operator_ids, source_events=source_events)
        if fired is not None:
            if algorithms:
                fired.text += ALGORITHMS_HEADER + "\n".join(f"- {line}" for line in algorithms)
            self.task = asyncio.create_task(self._call(fired, execute))

    async def _call(self, fired: Fired, execute: Execute) -> None:
        started = time.monotonic()
        policy = self.policy
        advisory = policy is not None and policy.agent_mode != "autonomous"
        if advisory:
            fired.text += ADVISORY
        token = DECISION_POLICY_EPOCH.set(policy.policy_epoch if policy is not None else None)
        try:
            response = await asyncio.to_thread(self.dispatcher.decide, fired)
        except Exception as exc:  # noqa: BLE001 - сбой модели — это «нет решения», не остановка агента
            response = AgentResponse(ok=False, llm_call=LLMCallInfo(model=self.model, purpose=fired.purpose, ok=False,
                                                                    error=repr(exc)[:300],
                                                                    latency_ms=int((time.monotonic() - started) * 1000)))
        finally:
            DECISION_POLICY_EPOCH.reset(token)
        self.dispatcher.finish(fired, response)
        # Таймер проверки модели не нужен: каждую отправленную команду проверяет Gate по телеметрии, а отказ, таймаут,
        # отсутствие эффекта и итог после таймаута приходят поводами из on_log. Таймер срабатывал раньше Gate.
        self.dispatcher.checks = [c for c in self.dispatcher.checks if c.decision_id != response.decision_id]
        self.calls += 1
        info = response.llm_call
        self.log.write(
            "llm_call", model=info.model, purpose=info.purpose, prompt_tokens=info.prompt_tokens, completion_tokens=info.completion_tokens,
            latency_ms=info.latency_ms, ok=response.ok, error=info.error, attempts=info.attempts,
            summary=(response.decision.situation[:200] if response.decision else None), triggers=[t.public() for t in fired.triggers],
            response=(response.raw_text or "")[:4000] or None,
        )  # fmt: skip
        if not response.ok or response.decision is None:
            for trigger in fired.triggers:
                if trigger.kind != 'verification' and (trigger.meta.get('pipeline_feedback') or trigger.meta.get('playbook')):
                    self.dispatcher.pending.pop(trigger.key, None)
                    self.on_human(trigger)
            self.failures += 1
            if self.failures >= FAILURES_TO_FALLBACK:
                self.down_until = self.clock() + FALLBACK_S
                self.failures = 0
                self.log.write("error", where="llm_brain", error=f"модель не отвечает {FAILURES_TO_FALLBACK} раза подряд: решения на плейбуках {FALLBACK_S:.0f} с")
            return
        self.failures = 0
        try:
            intent = to_intent(response, fired, self.settings.trigger_stale_s)
            if intent.steps:
                self._continue[f"{intent.steps[0].node}.{intent.steps[0].action}"] = intent.deferred
            if (not intent.steps or advisory) and not intent.human_escalation:
                for trigger in fired.triggers:
                    # Локальный отказ Gate Купола не касался: оценка модели его закрывает. Остальные сбои без нового шага — человеку
                    local = trigger.meta.get('local_rejection')
                    if trigger.kind != 'verification' and not local and (trigger.meta.get('pipeline_feedback') or trigger.meta.get('playbook')):
                        self.on_human(trigger)
            await execute(intent)
        except Exception as exc:  # noqa: BLE001 - фоновая задача: ошибка исполнения не должна потеряться
            self.log.write("error", where="llm_brain.execute", error=repr(exc))
            for trigger in fired.triggers:
                if trigger.kind != 'verification' and (trigger.meta.get('pipeline_feedback') or trigger.meta.get('playbook')):
                    self.on_human(trigger)

    async def close(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
