"""Главный цикл Штаба: опрос → состояние → детекторы → сверка → агенты → предохранитель → журнал.

Цикл устойчив к исключениям: сбой одного шага (адаптер, детектор, агент) пишется в журнал
как ошибка, и цикл идёт дальше. Каждый такт:

1. получить телеметрию;
2. обновить состояние, записать снимок для ``replay``;
3. прогнать детекторы, сверку источников, энергетику, проверку эффекта команд;
4. дедуплицировать тревоги и записать новые в журнал;
5. собрать ``Situation`` и отдать агентам: детерминированные правила (``react``) — каждый
   такт, вызов модели (``run``) — по изменению входа, в потоках, чтобы медленная модель не
   останавливала опрос;
6. рекомендации агентов идут на доску, команды — только через ``Guard``;
7. продвинуть время (только симулятор).

Движок не знает, симулятор перед ним или реальный Комплекс: только ``KupolAdapter``.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from kupol.adapter.base import AdapterError, KupolAdapter
from kupol.agents.base import Agent, AgentResult, Situation
from kupol.agents.board import RecommendationBoard
from kupol.agents.diagnost import Diagnost
from kupol.agents.energy_planner import EnergyPlannerAgent
from kupol.agents.llm import LLMClient
from kupol.agents.monitor import Monitor
from kupol.core.config import AppConfig
from kupol.core.baseline import Baseline, ResidualSeries
from kupol.core.charts import ChartRecorder
from kupol.core.crosscheck import CrossChecker
from kupol.core.detectors import AlertTracker, DetectorSuite
from kupol.core.energy import (
    EnergyPlanner,
    EnergyStatus,
    PlanItem,
    ProcessTracker,
    SheddingAdvice,
    energy_alert,
    energy_status,
)
from kupol.core.guard import Guard
from kupol.core.probes import ProbeManager
from kupol.core.response import ResponseDecision, ResponsePolicy
from kupol.core.round import RoundChecklist, RoundItem
from kupol.core.verdicts import BookEvent, VerdictBook, context_evidence
from kupol.core.journal import Journal, TelemetryLog
from kupol.core.models import (
    Alert,
    AlertLevel,
    Command,
    Evidence,
    JournalEntry,
    Probe,
    Recommendation,
    Telemetry,
    Verdict,
)
from kupol.core.state import State

log = logging.getLogger("kupol.engine")


def _field_matches(pattern: str, field: str) -> bool:
    """Совпадает ли поле разметки (допускает шаблон ``co2_ppm.*``) с утверждением."""
    import fnmatch

    return fnmatch.fnmatchcase(field, pattern) or fnmatch.fnmatchcase(pattern, field)


ERROR_LOG_REPEAT_S = 30.0  # повторять запись одной и той же ошибки шага не чаще
BASELINE_LOG_REPEAT_S = 600.0  # как часто писать обновлённую калибровку в журнал


@dataclass
class View:
    """Снимок состояния для панели (заменяется целиком в конце каждого такта).

    Attributes:
        ts: Время последнего такта.
        elapsed_s: Секунд с начала игры.
        duration_s: Контрольное время игры.
        telemetry: Последний снимок.
        comms_ok: Есть ли связь.
        energy: Энергостатус.
        alerts: Активные тревоги (тяжёлые первыми).
        verdicts: Актуальные вердикты сверки.
        plan: План операций.
        running: Идёт ли цикл.
        llm_mode: Режим LLM.
        counters: Счётчики прогона.
        forecasts: Прогноз «время до критического порога» по полям.
        probe_runs: Выполняющиеся и недавние пробы (для кнопок ответа Оператора).
        round_items: Пункты текущего обхода.
        round_next_in_s: Секунд до следующего обхода.
        round_no: Номер текущего обхода.
        responses: Уровни реакции по полям.
        notices: Последние уведомления о действиях человека.
    """

    ts: datetime | None = None
    elapsed_s: float = 0.0
    duration_s: float = 0.0
    telemetry: Telemetry | None = None
    comms_ok: bool = True
    energy: EnergyStatus | None = None
    alerts: list[Alert] = field(default_factory=list)
    verdicts: list[Verdict] = field(default_factory=list)
    plan: list[PlanItem] = field(default_factory=list)
    running: bool = False
    llm_mode: str = "mock"
    counters: dict[str, int] = field(default_factory=dict)
    forecasts: list[dict[str, Any]] = field(default_factory=list)
    probe_runs: list[dict[str, Any]] = field(default_factory=list)
    round_items: list[RoundItem] = field(default_factory=list)
    round_next_in_s: float = 0.0
    round_no: int = 0
    responses: dict[str, ResponseDecision] = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)


class Engine:
    """Главный цикл Штаба."""

    def __init__(
        self,
        config: AppConfig,
        adapter: KupolAdapter,
        run_dir: str | Path,
        llm: LLMClient | None = None,
        auto_approve: bool = False,
        sim_operator: bool = False,
        agent_workers: int = 0,
        duration_s: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """Собрать все компоненты.

        Args:
            config: Конфигурация.
            adapter: Адаптер Комплекса.
            run_dir: Каталог прогона (журнал, телеметрия, итог).
            llm: Клиент модели; по умолчанию режим из конфига.
            sim_operator: Симулированный Оператор отвечает на вопросы проб по эталонной разметке симулятора
                (только для симуляции; в журнале актёр ``sim-operator``).
            auto_approve: Автоматически подтверждать рекомендации человека. ТОЛЬКО для
                headless-симуляции без людей; в журнале актёр ``sim-auto-approve``.
            agent_workers: Потоков для вызовов моделей; 0 — выполнять вызовы агентов
                синхронно (детерминированно, для симуляции с mock).
            duration_s: Длительность игры, с (по умолчанию ``game.duration_min``).
            meta: Метаданные прогона для ``meta.json`` (сценарий, seed, скорость).
        """
        self.config = config
        self.adapter = adapter
        self.run_dir = Path(run_dir)
        self.auto_approve = auto_approve
        self.sim_operator = sim_operator
        self.meta = dict(meta or {})
        self.duration_s = duration_s if duration_s is not None else config.game.duration_min * 60.0

        self.journal = Journal(self.run_dir, clock=adapter.now)
        self.telemetry_log = TelemetryLog(self.run_dir)
        self.state = State(config.game.history_minutes)
        self.baseline = Baseline(config)
        self.residuals = ResidualSeries(config)
        self.suite = DetectorSuite(config, self.baseline)
        self.charts = ChartRecorder(config, self.baseline)  # история для графиков панели
        self.crosscheck = CrossChecker(config)
        self.book = VerdictBook(config)
        self.alerts = AlertTracker(config.game.alert_dedup_s)
        self.guard = Guard(config.guard, adapter, self.journal, self.state, response=config.response)
        self.guard.cascade_check = self._cascade_check
        self.guard.on_downgrade = self._on_downgrade
        self.policy = ResponsePolicy(config.response)
        self.probes: ProbeManager | None = None
        self._last_level: dict[str, str] = {}
        self._safety_queue: list[tuple[str, str, str]] = []
        self._actions: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self.notices: list[str] = []
        self.round = RoundChecklist(config.round, adapter.now())
        self.planner = EnergyPlanner(config)
        self.process = ProcessTracker(config)
        self.probes = ProbeManager(config, self.guard, self.journal, self.planner, self.baseline)
        self.board = RecommendationBoard(self.guard, self.journal, adapter.now, config.agents.recommendation_ttl_s)
        self.llm = llm or LLMClient(config.llm, self.journal)
        self.llm.set_journal(self.journal)  # вызовы модели пишутся в журнал этого прогона
        enabled = config.agents.enabled
        self.agents: list[Agent] = [
            a
            for a in (
                Monitor(config, self.llm, self.journal),
                Diagnost(config, self.llm, self.journal),
                EnergyPlannerAgent(config, self.llm, self.journal),
            )
            if enabled.get(a.name, True)
        ]

        self._pool = ThreadPoolExecutor(max_workers=agent_workers) if agent_workers > 0 else None
        self._futures: list[Future[AgentResult]] = []
        self._error_logged: dict[str, datetime] = {}
        self._baseline_logged: datetime | None = None
        self._last_residual: float | None = None
        self._start: datetime = adapter.now()
        self._stop = threading.Event()
        self._closed = False
        self.counters: dict[str, int] = {
            "ticks": 0,
            "alerts_info": 0,
            "alerts_warning": 0,
            "alerts_critical": 0,
            "verdicts": 0,
            "verdict_revisions": 0,
            "responses": 0,
            "downgrades": 0,
            "probes": 0,
            "rounds": 0,
            "round_mismatches": 0,
            "safety_events": 0,
            "recommendations": 0,
            "agent_runs": 0,
            "agent_fallbacks": 0,
            "step_errors": 0,
            "poll_errors": 0,
        }
        self.view = View(duration_s=self.duration_s, llm_mode=self.llm.mode)

    # ------------------------------------------------------------------ управление

    def stop(self) -> None:
        """Попросить цикл остановиться после текущего такта (потокобезопасно)."""
        self._stop.set()

    @property
    def start_time(self) -> datetime:
        """Время старта игры (по часам адаптера)."""
        return self._start

    @property
    def elapsed_s(self) -> float:
        """Секунд с начала игры (по времени адаптера)."""
        return (self.adapter.now() - self._start).total_seconds()

    def run(self, speed: float = 1.0, until_s: float | None = None) -> dict[str, Any]:
        """Выполнять такты до конца игры или остановки.

        Args:
            speed: Ускорение относительно реального времени; 0 — без пауз (максимально быстро).
            until_s: Остановиться при этом времени игры (по умолчанию ``duration_s``).

        Returns:
            Итог прогона (то же, что пишется в ``summary.json``).
        """
        limit = self.duration_s if until_s is None else until_s
        tick = self.config.game.tick_s
        self.view.running = True
        try:
            while not self._stop.is_set() and self.elapsed_s < limit:
                t0 = time.monotonic()
                self.step()
                if speed > 0:
                    spare = tick / speed - (time.monotonic() - t0)
                    if spare > 0:
                        self._stop.wait(spare)
        except KeyboardInterrupt:
            self.journal.log(actor="engine", event="остановка по Ctrl+C", status="stopped", kind="engine")
        finally:
            self.view.running = False
            summary = self.close()
        return summary

    # ------------------------------------------------------------------ один такт

    def step(self) -> None:
        """Выполнить один такт цикла. Исключения шагов перехватываются и журналируются."""
        self.counters["ticks"] += 1
        tel = self._poll()
        now = self.adapter.now()
        self._safe("safety", lambda: self._process_safety_queue(now))
        self._safe("actions", lambda: self._process_actions(now))
        alerts: list[Alert] = []
        verdicts: list[Verdict] = []
        status: EnergyStatus | None = None
        if tel is not None:
            self._safe("state", lambda: self.state.update(tel))
            self._safe("telemetry_log", lambda: self.telemetry_log.write(tel))
            self._safe("process", lambda: self.process.update(tel))
        residual = self._safe("residual", lambda: self.residuals.update(self.state))
        if residual is not None:
            self._last_residual = residual
        self._safe("baseline", lambda: self._observe_baseline(now, residual))
        alerts += self._safe("detectors", lambda: self.suite.run(self.state, now, residual)) or []
        alerts += self._safe("guard", self.guard.check_effects) or []
        rule_verdicts = self._safe("crosscheck", lambda: self.crosscheck.run(self.state, now, self.guard.sent_commands())) or []
        status = self._safe("energy", lambda: energy_status(self.state, self.config))
        if status is not None and self.state.latest() is not None and self.state.latest().comms_ok:  # type: ignore[union-attr]
            ea = energy_alert(status, now, self.config.energy.deficit_info_w)
            if ea is not None:
                alerts.append(ea)
        for a in alerts:
            if a.kind == "actuator_delayed" and a.field:  # запаздывание, а не отказ: снимаем «отказ исполнителя»
                self.alerts.clear_key(f"actuator_no_effect:{a.field}")
                self.book.retract(f"actuator:{a.field}", rule="cause_effect", supports="real")
        upd = self._safe("verdicts", lambda: self._update_book(now, rule_verdicts, alerts))
        verdicts = upd.verdicts if upd is not None else []
        alerts += upd.alerts if upd is not None else []
        self._annotate(alerts, verdicts)
        new_alerts = self.alerts.filter(alerts, now)
        self._log_alerts(new_alerts, verdicts, now)
        if upd is not None:
            self._log_book_events(upd.events, now)
        active = self.alerts.active(now)
        responses = self._safe("response", lambda: self._decide_responses(now, active, verdicts)) or {}
        self._safe("probes", lambda: self._run_probes(now, responses, status))
        self._safe("round", lambda: self._tick_round(now))
        sit = self._situation(now, active, new_alerts, verdicts, status)
        sit.responses = responses
        sit.residual_w = self._last_residual
        sit.available_probes = self._safe("probe_list", lambda: self._available_probes(now, active, status)) or []
        self._safe("agents", lambda: self._run_agents(sit))
        self._safe("collect", self._collect_futures)
        self._safe("board", self._board_housekeeping)
        self._safe("charts", lambda: self._record_charts(now, tel, status, residual, responses))
        self._publish_view(sit)
        # Время идёт после обработки такта: первый снимок — момент старта, а решения
        # такта k относятся к данным момента k (в реальном адаптере advance ничего не делает).
        self._safe("advance", lambda: self.adapter.advance(self.config.game.tick_s))

    # ------------------------------------------------------------------ части такта

    def _observe_baseline(self, now: datetime, residual: float | None) -> None:
        """Калибровка: данные из периода тревоги в неё не попадают; оценки пишутся в журнал."""
        blocked: set[str] = set()
        for a in self.alerts.active(now):
            if a.level.rank < AlertLevel.WARNING.rank or a.kind == "time_to_threshold":
                continue  # прогноз и справочные тревоги калибровку не блокируют
            blocked.update(a.field.split("|") if a.field else ["*"])
            if a.kind in ("cusum", "energy_t_left") and a.field == "energy_balance":
                blocked.add("energy_balance")
        was_fitted = self.baseline.fitted
        changed = self.baseline.observe(self.state, blocked, residual)
        due = self._baseline_logged is None or (now - self._baseline_logged).total_seconds() >= BASELINE_LOG_REPEAT_S
        if changed and (not was_fitted or due):
            self._baseline_logged = now
            snap = self.baseline.snapshot()
            self.journal.log(
                JournalEntry(
                    ts=now,
                    actor="baseline",
                    event="калибровка набрана" if not was_fitted else "калибровка обновлена",
                    cross_check="шум датчиков, разности пар, σ невязки баланса (абсолютные уровни не калибруются)",
                    status="fitted",
                    kind="baseline",
                    data={"baseline": snap.model_dump(mode="json")},
                )
            )

    def _poll(self) -> Telemetry | None:
        """Получить телеметрию; сбой не останавливает цикл."""
        try:
            return self.adapter.get_telemetry()
        except NotImplementedError:
            raise  # адаптер не доделан: это ошибка разработки, а не сбой Комплекса — цикл не маскирует её
        except AdapterError as exc:
            self.counters["poll_errors"] += 1
            self._log_error("poll", f"телеметрия недоступна: {exc}")
        except Exception as exc:  # noqa: BLE001 - любой сбой адаптера не должен ронять цикл
            self.counters["poll_errors"] += 1
            self._log_error("poll", f"неожиданная ошибка адаптера: {exc!r}")
        return None

    def _safe(self, name: str, fn: Callable[[], Any]) -> Any:
        """Выполнить шаг, перехватив исключение (оно пишется в журнал, цикл продолжается)."""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - устойчивость цикла важнее
            self.counters["step_errors"] += 1
            self._log_error(name, f"сбой шага '{name}': {exc!r}")
            return None

    def _log_error(self, name: str, text: str) -> None:
        """Записать ошибку (не чаще раза в ``ERROR_LOG_REPEAT_S`` на тип)."""
        now = self.adapter.now()
        last = self._error_logged.get(name)
        if last is not None and (now - last).total_seconds() < ERROR_LOG_REPEAT_S:
            return
        self._error_logged[name] = now
        log.warning(text)
        try:
            self.journal.log(actor="engine", event=text, status="error", kind="error", ts=now)
        except Exception:  # noqa: BLE001 - журнал недоступен: остаётся только logging
            log.exception("не удалось записать ошибку в журнал")

    @staticmethod
    def _annotate(alerts: list[Alert], verdicts: list[Verdict]) -> None:
        """Дописать к тревогам итог сверки (уровень НЕ меняем: безопасность важнее удобства)."""
        for a in alerts:
            if a.field is None or a.kind not in ("threshold", "jump", "trend", "freeze"):
                continue
            related = [v for v in verdicts if a.field in v.fields]
            if not related:
                continue
            v = related[0]
            a.details["verdict"] = v.kind
            a.message += f" [сверка: {v.kind}, {v.confidence:.0%}]"

    # ------------------------------------------------------------------ политика реакции, каскад, безопасность

    def _decide_responses(self, now: datetime, active: list[Alert], verdicts: list[Verdict]) -> dict[str, ResponseDecision]:
        """Уровень реакции по каждому полю с активной тревогой; смены уровня пишутся в журнал."""
        by_field: dict[str, list[Alert]] = {}
        for a in active:
            if a.field and a.kind not in ("safety_event", "verdict_revised"):
                by_field.setdefault(a.field, []).append(a)
        out: dict[str, ResponseDecision] = {}
        for fld, items in by_field.items():
            top = max(items, key=lambda a: a.level.rank).level
            verdict = next((v for v in verdicts if fld in v.fields and v.kind != "unknown"), None) or next(
                (v for v in verdicts if fld in v.fields), None
            )
            forecast = all(a.kind == "time_to_threshold" for a in items)
            decision = self.policy.decide(fld, verdict, top, forecast_only=forecast)
            out[fld] = decision
            if self._last_level.get(fld) != decision.level.value:
                self._last_level[fld] = decision.level.value
                self._log_response(now, decision, verdict)
        for fld in [f for f in self._last_level if f not in by_field and self._last_level[f] != "observe"]:
            self._last_level[fld] = "observe"  # тревога снята: уровень вернулся к наблюдению
        return out

    def _log_response(self, now: datetime, d: ResponseDecision, verdict: Verdict | None) -> None:
        """Запись о реакции: уровень, свидетельства и причина выбора (п. 5 политики реакции)."""
        self.counters["responses"] += 1
        self.journal.log(
            JournalEntry(
                ts=now,
                actor="response",
                event=f"реакция {d.level.value} по {d.subject}",
                cross_check="; ".join(f"{e.source}:{e.rule}" for e in (verdict.evidence if verdict else [])[:4]),
                hypothesis=verdict.kind if verdict else "",
                impact=f"тревога {d.alert_level.value}",
                action=d.level.value,
                result=d.reason,
                status=d.level.value,
                kind="response",
                data={
                    "level": d.level.value,
                    "subject": d.subject,
                    "confidence": d.confidence,
                    "sources": d.sources,
                    "alert_level": d.alert_level.value,
                    "safety_event": d.safety_event,
                    "verdict_id": d.verdict_id,
                    "reason": d.reason,
                },
            )
        )

    def _cascade_check(self, cmd: Any) -> str | None:
        """Что-если для дорогой команды: риск каскада (вызывается предохранителем)."""
        try:
            return self.planner.command_risk(cmd, self.state.latest(), energy_status(self.state, self.config))
        except Exception as exc:  # noqa: BLE001 - сбой проверки не должен блокировать безопасность
            self._log_error("cascade", f"сбой проверки каскада: {exc!r}")
            return None

    def _on_downgrade(self, cmd: Any, who: str, risk: str) -> None:
        """Команда с риском каскада превращается в рекомендацию человеку."""
        self.counters["downgrades"] += 1
        self._add(
            Recommendation(
                agent=who,
                title=f"Не выполнено автоматически (риск каскада): {cmd.name} {cmd.args}",
                rationale=(
                    f"{risk}.\nКоманда не отправлена. Если действие всё же необходимо, подайте её вручную "
                    "(панель: «Ручная команда») после оценки последствий для энергии и производства."
                ),
                priority=2,
                needs_human=True,
            )
        )

    def report_safety_event(self, event: str, detail: str = "", who: str = "human") -> None:
        """Сообщить о событии безопасности (дым, перегрев паяльника, «Стоп» Оператора).

        Реакция немедленная: правило двух и проверка каскада не применяются, но белый список и
        диапазоны команд действуют (это не обход блокировок, а ускорение безопасного ответа).
        Потокобезопасно: событие обрабатывается в начале следующего такта.

        Args:
            event: Имя события из `response.safety_override_events`.
            detail: Пояснение человека.
            who: Кто сообщил.
        """
        self._safety_queue.append((event, detail, who))

    def _process_safety_queue(self, now: datetime) -> None:
        """Обработать сообщения о событиях безопасности."""
        queue, self._safety_queue = self._safety_queue, []
        for event, detail, who in queue:
            self.counters["safety_events"] += 1
            alert = Alert(
                ts=now,
                level=AlertLevel.CRITICAL,
                kind="safety_event",
                field=event,
                message=f"Событие безопасности «{event}» от {who}: {detail or 'без пояснения'}",
                detector="human",
            )
            self.alerts.filter([alert], now)
            self.journal.log(
                JournalEntry(
                    ts=now, actor=who, event=alert.message, status="critical", kind="alert", data={"alert": alert.model_dump(mode="json")}
                )
            )
            decision = self.policy.decide(event, None, AlertLevel.CRITICAL, safety_event=event)
            self._last_level[event] = decision.level.value
            self._log_response(now, decision, None)
            self._add(
                Recommendation(
                    agent="safety",
                    title=f"Событие безопасности «{event}»: прекратить опасную операцию",
                    rationale=(
                        f"{detail or event}. Безопасность важнее производства (регламент п. 1.5): Оператору остановить опасную "
                        "операцию и предупредить находящихся рядом, включить вытяжку, уведомить ответственного за безопасность "
                        "(регламент п. 9.3). Аппаратные блокировки не трогаем."
                    ),
                    priority=1,
                    needs_human=True,
                    commands=[
                        Command(name="vent_enable", args={"mode": "low"}, reason=f"событие безопасности: {event}", safety_event=event)
                    ],
                )
            )

    # ------------------------------------------------------------------ действия человека из панели

    def _notice(self, text: str) -> None:
        """Уведомление для панели (последние 8)."""
        self.notices = (self.notices + [text])[-8:]

    def _process_actions(self, now: datetime) -> None:
        """Выполнить действия, поставленные панелью в очередь (всё остальное в цикле однопоточно)."""
        while True:
            try:
                action = self._actions.get_nowait()
            except queue.Empty:
                return
            try:
                action(now)
            except Exception as exc:  # noqa: BLE001 - ошибка действия не должна ронять цикл
                self.counters["step_errors"] += 1
                self._notice(f"Ошибка действия: {exc!r}")

    def request_probe(self, verdict_id: str, who: str = "human", probe_id: str | None = None) -> None:
        """Запустить безопасную пробу по вердикту (кнопка «выполнить пробу»). Потокобезопасно."""
        self._actions.put(lambda now: self._do_probe(now, verdict_id, who, probe_id))

    def close_verdict(self, verdict_id: str, who: str = "human") -> None:
        """Закрыть вердикт вручную (кнопка «закрыть вручную»). Новые свидетельства откроют его заново. Потокобезопасно."""
        self._actions.put(lambda now: self._do_close(now, verdict_id, who))

    def answer_probe(self, run_id: str, supports: str, who: str = "human") -> None:
        """Ответ Оператора на вопрос пробы: ``real`` или ``false``. Потокобезопасно."""
        self._actions.put(lambda now: self._do_answer(now, run_id, supports, who))

    def round_mark(self, item_id: str, ok: bool, who: str = "human", note: str = "") -> None:
        """Отметить пункт обхода: «проверил, расхождений нет» или «расхождение». Потокобезопасно."""
        self._actions.put(lambda now: self._do_round_mark(now, item_id, ok, who, note))

    def _do_probe(self, now: datetime, verdict_id: str, who: str, probe_id: str | None) -> None:
        """Запуск пробы по вердикту по просьбе человека (ограничения проб действуют и здесь)."""
        verdict = self.book.by_id(verdict_id)
        tel = self.state.latest()
        if verdict is None or self.probes is None or tel is None:
            self._notice("Вердикт не найден или нет данных: проба не запущена")
            return
        subject = verdict.fields[0]
        status = energy_status(self.state, self.config)
        cands = self.probes.candidates(subject, tel, status, now)
        probe = next((p for p in cands if p.id == probe_id), None) if probe_id else self.probes.select(cands, subject, self.probes.ran_auto(subject))
        if probe is None:
            reasons = {p.id: self.probes.reject_reason(p, subject, tel, status, now) for p in self.probes.catalog}
            why = "; ".join(f"{k}: {v}" for k, v in reasons.items() if v)
            self._notice(f"Допустимых проб по {subject} нет ({why or 'каталог пуст'})")
            return
        run = self.probes.start(probe, subject, tel, self.state, now)
        self.counters["probes"] += 1
        self._notice(f"{who} запустил пробу {probe.id} по {subject}: {run.state}")
        if probe.human_action is not None:
            self._add(Recommendation(agent="probes", title=f"Вопрос Оператору (проба {probe.id}) по {subject}", rationale=run.message, priority=3, needs_human=False))

    def _do_close(self, now: datetime, verdict_id: str, who: str) -> None:
        """Закрытие вердикта человеком."""
        ev = self.book.close_manual(verdict_id, who, now)
        if ev is None:
            self._notice("Вердикт уже закрыт или не найден")
            return
        self._log_book_events([ev], now)
        self._notice(f"{who} закрыл вердикт по {', '.join(ev.verdict.fields[:2])}")

    def _do_answer(self, now: datetime, run_id: str, supports: str, who: str) -> None:
        """Ответ человека на вопрос пробы."""
        ok = self.probes is not None and self.probes.answer(run_id, supports, who)
        self._notice(f"{who} ответил на пробу: {'реально' if supports == 'real' else 'ложно'}" if ok else "Ответ не принят: проба уже завершена")

    def _do_round_mark(self, now: datetime, item_id: str, ok: bool, who: str, note: str) -> None:
        """Отметка пункта обхода; расхождение создаёт тревогу."""
        item = self.round.mark(item_id, ok, who, note)
        if item is None:
            self._notice("Пункт обхода уже отмечен или не найден")
            return
        self.journal.log(
            JournalEntry(
                ts=now,
                actor=who,
                event=f"обход №{self.round.round_no}: {item.label} — {'расхождений нет' if ok else 'РАСХОЖДЕНИЕ'}",
                cross_check=item.hint,
                result=note,
                status=item.status,
                kind="round",
                data={"item": item.id, "fields": item.fields, "status": item.status},
            )
        )
        if not ok:
            self.counters["round_mismatches"] += 1
            alert = Alert(
                ts=now,
                level=AlertLevel.WARNING,
                kind="round_mismatch",
                field=item.fields[0],
                message=f"Обход: расхождение в паре «{item.label}» ({item.hint})" + (f"; {note}" if note else ""),
                detector="round",
                details={"item": item.id, "who": who},
            )
            self.alerts.filter([alert], now)
            self.book.add_evidence(
                item.fields[0],
                Evidence(source="operator", supports="false", weight=self.config.verdicts.weights.get("operator", 0.9) * 0.6, detail=alert.message, rule="round", kind_hint="sensor_fault", ts=now),
                now,
                item.fields,
            )
            self._notice(f"Расхождение по «{item.label}» занесено как тревога")
        else:
            self._notice(f"Обход: «{item.label}» — расхождений нет")

    def _tick_round(self, now: datetime) -> None:
        """Открыть очередной обход по таймеру."""
        if not self.round.due(now):
            return
        if self.round.is_open:
            self.journal.log(actor="round", event=f"обход №{self.round.round_no} не завершён, открывается новый", status="skipped", kind="round")
        items = self.round.open_round(now, self.state.latest(), self._last_residual)
        self.counters["rounds"] += 1
        self.journal.log(
            JournalEntry(
                ts=now,
                actor="round",
                event=f"открыт обход №{self.round.round_no}: {len(items)} пунктов для сверки",
                status="open",
                kind="round",
                data={"items": [{"id": i.id, "label": i.label, "hint": i.hint} for i in items]},
            )
        )
        self._add(
            Recommendation(
                agent="round",
                title=f"Обход №{self.round.round_no}: сверьте пары источников",
                rationale="\n".join(f"• {i.label}: {i.hint}" for i in items),
                priority=4,
                needs_human=False,
            )
        )

    def _forecasts(self, active: list[Alert]) -> list[dict[str, Any]]:
        """Прогноз «время до критического порога» по активным тревогам-прогнозам."""
        out = [
            {
                "field": a.field,
                "eta_s": a.details.get("eta_s"),
                "limit": a.expected,
                "slope_per_min": a.details.get("slope_per_min"),
                "r2": a.details.get("r2"),
            }
            for a in active
            if a.kind == "time_to_threshold" and a.field
        ]
        return sorted(out, key=lambda f: f["eta_s"] or 0.0)

    # ------------------------------------------------------------------ пробы

    def _run_probes(self, now: datetime, responses: dict[str, ResponseDecision], status: EnergyStatus | None) -> None:
        """Завершить пробы и при необходимости запустить новую (одну за такт)."""
        probes = self.probes
        tel = self.state.latest()
        if probes is None or tel is None or not tel.comms_ok:
            return
        if self.sim_operator:
            self._sim_operator_answers(now)
        for run, ev in probes.tick(self.state, now):
            if ev is not None:
                self.book.add_evidence(run.subject, ev, now, [run.subject])
        for subject, decision in responses.items():
            if decision.level.value != "prepare" or decision.forecast_only or probes.has_recent(subject, now):
                continue
            cands = probes.candidates(subject, tel, status, now)
            probe = probes.select(cands, subject, probes.ran_auto(subject))
            if probe is None:
                continue
            run = probes.start(probe, subject, tel, self.state, now)
            self.counters["probes"] += 1
            if run.probe.human_action is not None:
                self._add(
                    Recommendation(
                        agent="probes",
                        title=f"Вопрос Оператору (проба {probe.id}) по {subject}",
                        rationale=run.message,
                        priority=3,
                        needs_human=False,
                    )
                )
            break

    def _available_probes(self, now: datetime, active: list[Alert], status: EnergyStatus | None) -> list[Probe]:
        """Допустимые пробы для утверждений с активными тревогами (для Диагноста)."""
        tel = self.state.latest()
        if self.probes is None or tel is None or not tel.comms_ok:
            return []
        out: dict[str, Probe] = {}
        for a in active:
            if a.level.rank >= AlertLevel.WARNING.rank and a.field and a.kind != "time_to_threshold":
                for p in self.probes.candidates(a.field, tel, status, now):
                    out.setdefault(p.id, p)
        return list(out.values())

    def _sim_operator_answers(self, now: datetime) -> None:
        """Симулированный Оператор: отвечает по эталонной разметке симулятора (только для симуляции)."""
        truth = getattr(self.adapter, "ground_truth", None)
        probes = self.probes
        if probes is None or truth is None:
            return
        false_types = {"false_signal", "hidden_drift", "sensor_drift_pair", "frozen_sensor", "noise_loss", "stale_timestamp"}
        t = (now - self._start).total_seconds()
        for run in probes.running():
            if run.probe.human_action is None or run.human_answer is not None:
                continue
            active = [
                g for g in truth
                if g["t_start"] <= t and (g["t_end"] is None or t <= g["t_end"] + 120.0) and _field_matches(g["field"], run.subject)
            ]
            supports = "false" if any(g["type"] in false_types for g in active) else "real"
            probes.answer(run.id, supports, "sim-operator")

    def _update_book(self, now: datetime, rule_verdicts: list[Verdict], alerts: list[Alert]) -> Any:
        """Накопить свидетельства и пересчитать вердикты (с контекстом работ)."""
        latest = self.state.latest()
        subjects = [v.fields[0] for v in self.book.active() if v.fields]
        extra = context_evidence(latest, subjects, self.config.verdicts.weights.get("context_work", 0.0))
        return self.book.update(now, latest, rule_verdicts, alerts, extra)

    def _log_book_events(self, events: list[BookEvent], now: datetime) -> None:
        """События жизненного цикла вердиктов — в журнал (создан, подтверждён, пересмотрен…)."""
        for ev in events:
            v = ev.verdict
            if ev.event == "created":
                self.counters["verdicts"] += 1
            if ev.event == "revised":
                self.counters["verdict_revisions"] += 1
            self.journal.log(
                JournalEntry(
                    ts=now,
                    actor="crosscheck",
                    event=f"вердикт {ev.event}: {v.kind} по {', '.join(v.fields[:2])}",
                    cross_check=v.explanation,
                    hypothesis=v.kind,
                    result=ev.reason,
                    status=f"{v.status}/{v.confidence:.2f}",
                    kind="verdict",
                    data={
                        "verdict": v.model_dump(mode="json"),
                        "event": ev.event,
                        "previous": ev.previous.model_dump(mode="json") if ev.previous else None,
                    },
                )
            )

    def _log_alerts(self, new_alerts: list[Alert], verdicts: list[Verdict], now: datetime) -> None:
        """Записать новые тревоги и вердикты в журнал."""
        for a in new_alerts:
            self.counters[f"alerts_{a.level.value}"] += 1
            related = [v for v in verdicts if a.field is not None and a.field in v.fields]
            self.journal.log(
                JournalEntry(
                    ts=now,
                    actor="detector",
                    event=a.message,
                    cross_check=related[0].explanation if related else "",
                    status=a.level.value,
                    kind="alert",
                    data={"alert": a.model_dump(mode="json")},
                )
            )

    def _situation(
        self,
        now: datetime,
        active: list[Alert],
        new_alerts: list[Alert],
        verdicts: list[Verdict],
        status: EnergyStatus | None,
    ) -> Situation:
        """Собрать срез для агентов."""
        tel = self.state.latest()
        vent_ok = not any(a.kind == "actuator_no_effect" and a.field == "vent" for a in active)
        plan: list[PlanItem] = []
        advice: list[SheddingAdvice] = []
        if tel is not None and status is not None and tel.comms_ok:
            plan = self._safe("plan", lambda: self.planner.order(self.process, tel, status, vent_ok)) or []
            advice = self._safe("advice", lambda: self.planner.shedding_advice(tel, status, self.process)) or []
        return Situation(
            ts=now,
            telemetry=tel,
            alerts=active,
            new_alerts=new_alerts,
            verdicts=verdicts,
            energy=status,
            plan=plan,
            advice=advice,
            journal_tail=self.journal.tail(10, exclude_kinds=("llm_call", "alert")),
            vent_ok=vent_ok,
            elapsed_s=self.elapsed_s,
        )

    def _run_agents(self, sit: Situation) -> None:
        """Правила кода — каждый такт; вызовы моделей — по изменению входа."""
        for agent in self.agents:
            for rec in agent.react(sit):
                self._add(rec)
            if sit.comms_ok and agent.try_claim(sit):
                self.counters["agent_runs"] += 1
                if self._pool is None:
                    self._finish(self._run_one(agent, sit))
                else:
                    self._futures.append(self._pool.submit(self._run_one, agent, sit))

    def _run_one(self, agent: Agent, sit: Situation) -> AgentResult:
        """Запуск агента (в потоке или синхронно). Сбой агента → результат пуст, ошибка в журнале."""
        try:
            return agent.run(sit)
        except Exception as exc:  # noqa: BLE001 - агент не должен ронять Штаб
            self.counters["step_errors"] += 1
            self._log_error(f"agent:{agent.name}", f"сбой агента {agent.name}: {exc!r}")
            return AgentResult([])

    def _collect_futures(self) -> None:
        """Забрать результаты завершённых вызовов агентов."""
        pending: list[Future[AgentResult]] = []
        for fut in self._futures:
            if fut.done():
                self._finish(fut.result())
            else:
                pending.append(fut)
        self._futures = pending

    def _finish(self, res: AgentResult) -> None:
        """Передать рекомендации агента на доску."""
        if res.used_fallback:
            self.counters["agent_fallbacks"] += 1
        for rec in res.recommendations:
            self._add(rec)

    def _add(self, rec: Recommendation) -> None:
        """Добавить рекомендацию на доску (команды пойдут через Guard)."""
        rec.ts = self.adapter.now()
        if self.board.add(rec) is not None:
            self.counters["recommendations"] += 1

    def _board_housekeeping(self) -> None:
        """Истечение сроков и автоподтверждение (только headless-симуляция)."""
        self.board.expire()
        if self.auto_approve and self.board.pending():
            self.board.accept_all("sim-auto-approve")

    def _record_charts(
        self, now: datetime, tel: Telemetry | None, status: EnergyStatus | None, residual: float | None, responses: dict[str, ResponseDecision]
    ) -> None:
        """Записать в историю графиков то, что такт уже вычислил."""
        self.charts.record(
            (now - self._start).total_seconds(),
            tel,
            residual_w=residual,
            t_left_min=None if status is None else status.t_left_min,
            cusum=self.suite.cusum.sums(),
            p_real=self.book.probabilities(),
            response_rank=max((d.level.rank for d in responses.values()), default=0),
        )

    def _probe_runs(self) -> list[dict[str, Any]]:
        """Недавние пробы для панели."""
        if self.probes is None:
            return []
        return [
            {
                "id": r.id,
                "probe": r.probe.id,
                "subject": r.subject,
                "state": r.state,
                "by_human": r.probe.human_action is not None,
                "message": r.message,
                "result": None if r.result is None else f"{r.result.supports}: {r.result.detail}",
                "expected_if_real": r.probe.expected_if_real,
                "expected_if_false": r.probe.expected_if_false,
                "deadline": r.deadline,
            }
            for r in self.probes.runs[-8:]
        ]

    def _publish_view(self, sit: Situation) -> None:
        """Обновить снимок для панели."""
        tel = sit.telemetry
        self.view = View(
            ts=sit.ts,
            elapsed_s=sit.elapsed_s,
            duration_s=self.duration_s,
            telemetry=tel,
            comms_ok=bool(tel and tel.comms_ok),
            energy=sit.energy,
            alerts=list(sit.alerts),
            verdicts=list(sit.verdicts),
            plan=list(sit.plan),
            running=self.view.running,
            llm_mode=self.llm.mode,
            counters=dict(self.counters),
            forecasts=self._forecasts(sit.alerts),
            probe_runs=self._probe_runs(),
            round_items=[RoundItem(**vars(i)) for i in self.round.items],
            round_next_in_s=self.round.next_in_s(sit.ts),
            round_no=self.round.round_no,
            responses=dict(sit.responses),
            notices=list(self.notices),
        )

    # ------------------------------------------------------------------ завершение

    def close(self) -> dict[str, Any]:
        """Дождаться агентов, сохранить журнал и итог. Повторный вызов безопасен.

        Returns:
            Итог прогона.
        """
        if self._closed:
            return self._summary()
        self._closed = True
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._safe("collect", self._collect_futures)
        summary = self._summary()
        try:
            self._write_artifacts(summary)
        finally:
            self.journal.close()
            self.telemetry_log.close()
        return summary

    def _summary(self) -> dict[str, Any]:
        """Итог прогона для ``summary.json`` и консоли."""
        return {
            "elapsed_s": round(self.elapsed_s, 1),
            "duration_s": self.duration_s,
            "llm_mode": self.llm.mode,
            "llm_model": self.llm.model,
            "counters": dict(self.counters),
            "run_dir": str(self.run_dir),
        }

    def _write_artifacts(self, summary: dict[str, Any]) -> None:
        """Сохранить экспорт журнала, метаданные, итог и «правду» симулятора."""
        self.journal.log(actor="engine", event="прогон завершён", result=json.dumps(summary["counters"]), status="finished", kind="engine")
        self.journal.export_csv()
        self.journal.export_markdown()
        (self.run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.run_dir / "meta.json").write_text(
            json.dumps({**self.meta, "llm_mode": self.llm.mode, "llm_model": self.llm.model}, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        for attr, filename in (("event_log", "sim_events.jsonl"), ("ground_truth", "ground_truth.jsonl")):
            records = getattr(self.adapter, attr, None)  # есть только у симулятора
            if records:
                with (self.run_dir / filename).open("w", encoding="utf-8") as fh:
                    for rec in records:
                        fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self.adapter.close()
