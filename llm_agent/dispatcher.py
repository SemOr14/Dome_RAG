"""Диспетчер вызовов модели по событиям и контрольным таймерам (rag/AGENT_TRIGGERS.md).

1. Событие или таймер → свежие данные (telemetry.latest, лента событий БД).
2-3. Поводы одного цикла объединяются в один вызов decide(): RAG и решение — в DomeLLMAgent.
4. Команды не исполняются: предложенные действия проверяют validate.py и сервер БД, отправляет Gate агентной системы.
5. После решения с действиями ставится таймер проверки; без новых данных результат остаётся непроверенным.
6. Повторная оценка — новым вызовом с результатом проверки; автоматического повтора команды нет.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from .agent import DomeLLMAgent
from .prompt_builder import _dome_time
from .schemas import AgentResponse
from .triggers import OperatorWatch, TelemetryWatch, Trigger, command_triggers, telemetry_age

MAX_LISTED = 12  # поводов в тексте одного вызова; остальные — списком узлов


@dataclass
class _Pending:
    trigger: Trigger
    failures: int = 0
    not_before: float = 0.0


@dataclass
class _Check:
    """Контрольный таймер проверки результата решения."""
    decision_id: str
    decision_event_id: str | None
    actions: list[dict]
    frame: tuple  # кадр телеметрии на момент решения
    due: float
    delay: float
    nodes: set[str] = field(default_factory=set)
    waits: int = 0


@dataclass
class Fired:
    """Один вызов модели: какие поводы объединены и что вышло."""
    triggers: list[Trigger]
    text: str
    response: AgentResponse | None = None
    nodes: set[str] = field(default_factory=set)
    purpose: str = ""
    snapshot: dict = field(default_factory=dict, repr=False)
    batch: list[_Pending] = field(default_factory=list, repr=False)


class Dispatcher:
    def __init__(self, agent: DomeLLMAgent, feed=None, *, clock: Callable[[], float] = time.monotonic,
                 dry_run: bool = False):
        s = agent.settings
        self.agent = agent
        self.settings = s
        self.feed = feed
        self.clock = clock
        self.dry_run = dry_run  # только находить поводы, модель не вызывать
        self.state_file = s.state_dir / "dispatcher_state.json"
        saved = self._load_state()
        self.telemetry = TelemetryWatch(s.trigger_stale_s, s.trigger_stall_s)
        self.operator = OperatorWatch(saved.get("operator_last_id", 0))
        self.pending: dict[str, _Pending] = {}
        self.fired_at: dict[str, float] = {}  # ключ ситуации -> когда по ней звали модель
        self.checks: list[_Check] = []
        self.last_call: float | None = None
        self.external_verification = False  # Runner/Gate owns actual command verification
        self.busy = False  # вызов модели идёт: новые поводы копятся, второй вызов не начинается
        self.last_review = clock()
        self.stats = {"calls": 0, "llm_failures": 0, "suppressed": 0, "unverified": 0, "triggers": 0}

    # --- состояние между перезапусками: не отвечать дважды на одно сообщение Оператора ---
    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        state = {"operator_last_id": self.operator.last_id}
        if self.feed is not None:
            state["feed_cursors"] = getattr(self.feed, "cursors", {})
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(state), encoding="utf-8")
        except OSError as exc:
            print(f"состояние диспетчера не сохранено: {exc}", file=sys.stderr)

    @property
    def saved_cursors(self) -> dict:
        return self._load_state().get("feed_cursors") or {}

    # --- цикл ---
    def run(self, stop: Callable[[], bool] = lambda: False, on_fire: Callable[[Fired], None] | None = None) -> None:
        while not stop():
            started = self.clock()
            for fired in self.tick():
                if on_fire:
                    on_fire(fired)
            time.sleep(max(0.0, self.settings.trigger_poll_s - (self.clock() - started)))

    def tick(self) -> list[Fired]:
        """Один цикл: собрать поводы, при необходимости вызвать модель. Возвращает сделанные вызовы."""
        fired = self.poll()
        if fired is None:
            return []
        if not self.dry_run:
            self.finish(fired, self.decide(fired))
        return [fired]

    def poll(self, chat: list[dict] | None = None, *, handled_operator_ids=None) -> Fired | None:
        """Собрать поводы; если пора звать модель — вернуть объединённый вызов (модель здесь не вызывается).

        chat — сообщения meeting_message.message, полученные вызывающим кодом напрямую из WebSocket.
        """
        now = self.clock()
        snapshot = self.agent.telemetry.latest()
        found = self.telemetry.observe(snapshot, self.agent.catalog.by_id, now)
        found += self.operator.from_snapshot(snapshot, handled_ids=handled_operator_ids)
        found += self.operator.messages(chat or [], handled_ids=handled_operator_ids)
        if self.feed is not None:
            try:
                events = self.feed.poll()
            except Exception as exc:  # noqa: BLE001 - без ленты событий работаем по телеметрии
                print(f"события БД не прочитаны ({type(exc).__name__})", file=sys.stderr)
                events = []
            found += command_triggers(events)
            msgs = {}
            for ev in events:
                if ev["event_type"] == "meeting_message" and isinstance(ev["payload"], dict):
                    msg = ev["payload"].get("message")
                    if isinstance(msg, dict) and isinstance(msg.get("id"), int):
                        msgs[msg["id"]] = (msg, ev["event_id"])
            found += self.operator.messages([m for m, _ in msgs.values()], {i: e for i, (_, e) in msgs.items()}, handled_ids=handled_operator_ids)
        if not self.external_verification:
            found += self._due_checks(snapshot, now)
        if self.settings.trigger_review_s and now - self.last_review >= self.settings.trigger_review_s \
                and not self.pending:
            found.append(Trigger("review", "review", "Плановый обзор: оцени состояние купола и риски на ближайшее "
                                                     "время. Если всё штатно — действий не нужно"))
        for trig in found:
            self._add(trig, now)
        self._save_state()

        if self.busy:
            return None
        batch = self._ready(now, snapshot)
        if not batch:
            return None
        return self._begin(batch, snapshot, now)

    def submit(self, trig: Trigger) -> None:
        """Повод извне (например, требование Оператора из веб-панели) — в очередь, как найденный детектором."""
        self._add(trig, self.clock())

    def _add(self, trig: Trigger, now: float) -> None:
        self.stats["triggers"] += 1
        last = self.fired_at.get(trig.key)
        if last is not None and now - last < self.settings.trigger_cooldown_s and trig.kind != "verification":
            self.stats["suppressed"] += 1  # та же ситуация недавно уже оценивалась
            return
        if trig.key in self.pending:  # повтор той же ситуации до вызова — объединяем
            old = self.pending[trig.key].trigger
            old.text = trig.text
            old.nodes |= trig.nodes
            old.event_ids += [e for e in trig.event_ids if e not in old.event_ids]
            return
        self.pending[trig.key] = _Pending(trig)

    def _ready(self, now: float, snapshot: dict) -> list[_Pending]:
        if self.last_call is not None and now - self.last_call < self.settings.trigger_min_interval_s:
            return []
        ready = [p for p in self.pending.values() if p.not_before <= now]
        if telemetry_age(snapshot, self.settings.trigger_stale_s) is not None:
            # Проверять результат по устаревшему кадру нельзя: проверка ждёт свежих данных в очереди
            ready = [p for p in ready if p.trigger.kind != "verification"]
        return sorted(ready, key=lambda p: p.trigger.priority)

    def _begin(self, batch: list[_Pending], snapshot: dict, now: float) -> Fired:
        triggers = [p.trigger for p in batch]
        text = self.compose(triggers)
        kinds = sorted({t.kind for t in triggers})
        fired = Fired(triggers, text, nodes=set().union(*(t.nodes for t in triggers)),
                      purpose=f"диспетчер: {', '.join(kinds)}", snapshot=snapshot, batch=batch)
        self.last_call = now
        self.last_review = now
        if self.dry_run:
            for p in batch:
                self._done(p.trigger, now)
            return fired
        self.busy = True
        call_id = f"dispatch-{uuid.uuid4().hex[:12]}"
        self.agent.events.emit("llm_trigger", {"call_id": call_id, "triggers": [t.public() for t in triggers],
                                               "text": text})
        return fired

    def decide(self, fired: Fired) -> AgentResponse:
        """Вызов модели по объединённому поводу. Блокирующий: в асинхронном коде — в отдельном потоке."""
        return self.agent.decide(fired.text, focus_nodes=fired.nodes, purpose=fired.purpose)

    def finish(self, fired: Fired, response: AgentResponse) -> None:
        """Учесть ответ модели: снять или отложить поводы, записать решение, поставить проверку результата."""
        now = self.clock()
        batch, snapshot, triggers = fired.batch, fired.snapshot, fired.triggers
        fired.response = response
        self.busy = False
        self.stats["calls"] += 1

        if not response.ok:
            # Пропущенный ответ модели — не проверка и не решение: повод остаётся в очереди с паузой
            self.stats["llm_failures"] += 1
            for p in batch:
                p.failures += 1
                if p.failures > self.settings.trigger_llm_retries:
                    self._done(p.trigger, now)
                else:
                    p.not_before = now + self.settings.trigger_min_interval_s * 2 ** p.failures
            self.agent.record(response, _dome_time(snapshot.get("game_time")))
            return

        for p in batch:
            self._done(p.trigger, now)
        self.agent.record(response, _dome_time(snapshot.get("game_time")))
        for trig in triggers:
            if trig.kind == "verification":
                self._verification_event(trig.meta, "reassessed", "результат оценён моделью по новым данным",
                                         response)
        if not self.external_verification:
            self._schedule_check(response, snapshot, now)

    def _done(self, trig: Trigger, now: float) -> None:
        self.pending.pop(trig.key, None)
        self.fired_at[trig.key] = now

    @staticmethod
    def compose(triggers: list[Trigger]) -> str:
        if len(triggers) == 1:
            return triggers[0].text
        lines = [f"Поводов для анализа: {len(triggers)} (по срочности)."]
        lines += [f"{i}. [{t.kind}] {t.text}" for i, t in enumerate(triggers[:MAX_LISTED], 1)]
        if len(triggers) > MAX_LISTED:
            rest = triggers[MAX_LISTED:]
            lines.append(f"…и ещё {len(rest)}: " + ", ".join(sorted({n for t in rest for n in t.nodes} or {"—"})))
        return "\n".join(lines)

    # --- проверка результата ---
    def _schedule_check(self, response: AgentResponse, snapshot: dict, now: float) -> None:
        accepted = response.accepted_actions
        if not accepted:
            return
        delay = max([self.settings.trigger_verify_min_s] + [a.verify_after_s for a in accepted])
        actions = [{"node_id": v.action.node_id, "action": v.action.action, "value": v.action.value,
                    "expected": v.action.expected,
                    "request_id": (v.server_check or {}).get("request_id")}
                   for v in response.verdicts if v.accepted]
        self.checks.append(_Check(response.decision_id, response.decision_event_id, actions,
                                  TelemetryWatch.frame_mark(snapshot), now + delay, delay,
                                  {a.node_id for a in accepted}))

    def _due_checks(self, snapshot: dict, now: float) -> list[Trigger]:
        out = []
        for check in [c for c in self.checks if c.due <= now]:
            meta = {"decision_id": check.decision_id, "decision_event_id": check.decision_event_id,
                    "actions": check.actions}
            if TelemetryWatch.frame_mark(snapshot) == check.frame:
                # Новых данных нет — нечем проверять; это не успех
                check.waits += 1
                if check.waits <= self.settings.trigger_verify_retries:
                    check.due = now + check.delay
                    continue
                self.checks.remove(check)
                self.stats["unverified"] += 1
                self._verification_event(meta, "unverified", "после действия не пришло новых данных телеметрии")
                out.append(Trigger("stale", f"verify-nodata|{check.decision_id}",
                                   f"Результат решения {check.decision_id} не проверен: новых данных телеметрии нет. "
                                   "Считать результат неподтверждённым", check.nodes, meta=meta))
                continue
            self.checks.remove(check)
            expected = "; ".join(f"{a['node_id']}.{a['action']} → {a['expected'] or 'не указано'}"
                                 for a in check.actions)
            out.append(Trigger(
                "verification", f"verify|{check.decision_id}",
                f"Проверка результата решения {check.decision_id}: ожидалось {expected}. По журналу определи, "
                "отправлялись ли команды и что ответил Купол, а по телеметрии — виден ли эффект. Если эффект не "
                "виден или команда не отправлялась, результат не подтверждён; не повторяй команду без проверки "
                "допустимости",
                check.nodes, [check.decision_event_id] if check.decision_event_id else [], meta))
        return out

    def _verification_event(self, meta: dict, status: str, reason: str, response: AgentResponse | None = None):
        payload = {"decision_id": meta.get("decision_id"), "decision_event_id": meta.get("decision_event_id"),
                   "status": status, "outcome": f"{status}: {reason}", "actions": meta.get("actions", []),
                   "observation_event_ids": list(getattr(self.agent.telemetry, "event_ids", []))}
        if response is not None:
            payload["followup_decision_id"] = response.decision_id
            payload["text"] = response.decision.situation if response.decision else None
        else:
            payload["text"] = f"Проверка решения {meta.get('decision_id')}"
        if self.agent.events.emit("verification", payload) is None:  # без БД — в файловый журнал
            self.agent.journal.append({"actor": "agent", "kind": "verification", **payload})
