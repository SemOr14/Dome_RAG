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

REPO_ROOT = Path(__file__).resolve().parents[4]
FAILURES_TO_FALLBACK = 3  # сбоев модели подряд, после которых решения временно отдаются плейбукам
FALLBACK_S = 300.0
SEVERITY_PRIORITY = {"critical": 1, "warning": 3, "info": 5}
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
from llm_agent.triggers import Trigger, gate_action_trigger, gate_verification_trigger  # noqa: E402
from llm_agent.schemas import AgentResponse, LLMCallInfo, ProposedAction  # noqa: E402
from llm_agent.storage.data_api import ApiControlChecker, DataApiClient, journal_record  # noqa: E402

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

    def __init__(self, recorder: DataApiRecorder | None, session: Callable[[], str | None], journal: LogJournal) -> None:
        self.recorder = recorder
        self.session = session
        self.journal = journal

    def emit(self, event_type: str, payload: Any, **kwargs: Any) -> str:
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


def to_intent(response: AgentResponse, fired: Fired) -> Intent:
    """Решение модели → намерение рантайма. Сообщение Оператору уходит, только если все шаги приняты Gate."""
    d = response.decision
    assert d is not None
    steps: list[Step] = []
    plan: list[str] = []
    for v in response.verdicts:
        a: ProposedAction = v.action
        if a.node_id == "meeting_01" and a.action == "post_message":
            continue
        label = f"{a.node_id}.{a.action}({_value(a.value)})"
        if v.accepted:
            steps.append(Step(a.node_id, a.action, a.value, expect_for(a.node_id, a.action, a.value), (a.reason or d.cause)[:200]))
            plan.append(f"{label}: {a.reason}" + (f" → ожидаем {a.expected}" if a.expected else ""))
        else:
            plan.append(f"не делать {label}: {'; '.join(v.reasons)}")
    plan += [f"наблюдать: {w}" for w in d.wait_and_watch]
    say = d.operator_message.text.strip() if d.operator_message.needed and d.operator_message.text.strip() else None
    if say and not any(v.accepted for v in response.verdicts if v.action.node_id == "meeting_01"):
        say = None  # сообщение не прошло проверку LLM-слоя (длина, пустота)
    refused = [f"{v.action.node_id}.{v.action.action} — {'; '.join(v.reasons)}" for v in response.verdicts
               if not v.accepted and v.action.node_id != "meeting_01"]
    return Intent(
        key=f"llm:{response.decision_id}", situation=d.situation, cause=d.cause or "не установлена", confidence=d.confidence,
        evidence=[e.model_dump(exclude_none=True) for e in d.evidence] or [{"trigger": t.text[:300]} for t in fired.triggers],
        plan=plan or ["наблюдать"], steps=steps, say=say, priority=SEVERITY_PRIORITY.get(d.severity, 5),
        impact_kind=d.impact_kind, authenticity=d.authenticity, causal_chain=list(d.causal_chain),
        cooldown_s=0.0, source="mixed" if d.rag_refs else "llm", rag_refs=list(d.rag_refs),
        say_always=any(t.kind == "operator" for t in fired.triggers), refused=refused,
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
            events=_Sink(recorder, session, self.journal), control_checker=checker,
        )  # fmt: skip
        self.dispatcher = Dispatcher(self.agent, clock=clock)
        self.task: asyncio.Task[None] | None = None
        self.failures = 0
        self.down_until = 0.0
        self.calls = 0
        self.agent_control: Callable[[], bool] = lambda: True  # рантайм подставляет флаг Gate
        self.no_effect_cooldown_s = 120.0  # рантайм подставляет policy.no_effect_cooldown_s
        self._actions: dict[int, dict[str, Any]] = {}  # seq записи action → запись (для поводов по verification)
        self._timed_out: set[int] = set()

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
        if kind == "action" and isinstance(rec.get("seq"), int):
            self._actions[rec["seq"]] = rec
            if len(self._actions) > 500:
                self._actions.pop(next(iter(self._actions)))
            if (trig := gate_action_trigger(rec)) is not None:  # отказ Купола или нет ответа — модель оценивает сразу
                if "|timeout|" in trig.key:
                    self._timed_out.add(rec["seq"])
                self.dispatcher.submit(trig)
        elif kind == "verification":
            ref = rec.get("ref")
            if not isinstance(ref, int):
                return
            trig = gate_verification_trigger(rec, self._actions.get(ref), ref in self._timed_out, self.no_effect_cooldown_s)
            self._timed_out.discard(ref)
            if trig is not None:  # нет эффекта или итог после таймаута — по факту проверки Gate
                self.dispatcher.submit(trig)

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

    def step(self, chat_new: list[dict[str, Any]], execute: Execute) -> None:
        """Такт: найти поводы; если пора — запустить вызов модели в фоне (результат исполнит ``execute``)."""
        fired = self.dispatcher.poll(chat_new)
        if fired is not None:
            self.task = asyncio.create_task(self._call(fired, execute))

    async def _call(self, fired: Fired, execute: Execute) -> None:
        started = time.monotonic()
        advisory = not self.agent_control()
        if advisory:
            fired.text += ADVISORY
        try:
            response = await asyncio.to_thread(self.dispatcher.decide, fired)
        except Exception as exc:  # noqa: BLE001 - сбой модели — это «нет решения», не остановка агента
            response = AgentResponse(ok=False, llm_call=LLMCallInfo(model=self.model, purpose=fired.purpose, ok=False,
                                                                    error=repr(exc)[:300],
                                                                    latency_ms=int((time.monotonic() - started) * 1000)))
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
            self.failures += 1
            if self.failures >= FAILURES_TO_FALLBACK:
                self.down_until = self.clock() + FALLBACK_S
                self.failures = 0
                self.log.write("error", where="llm_brain", error=f"модель не отвечает {FAILURES_TO_FALLBACK} раза подряд: решения на плейбуках {FALLBACK_S:.0f} с")
            return
        self.failures = 0
        try:
            await execute(to_intent(response, fired))
        except Exception as exc:  # noqa: BLE001 - фоновая задача: ошибка исполнения не должна потеряться
            self.log.write("error", where="llm_brain.execute", error=repr(exc))

    async def close(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
