"""Безопасные активные проверки (пробы) для различения гипотез «реально» и «ложно».

Проба — дешёвое обратимое действие или вопрос, у которого есть *ожидаемый результат при реальном событии* и
*при ложном*. После выполнения система сравнивает наблюдение с ожиданием и добавляет `Evidence` в вердикт.

Ограничения (все — код, а не модель):

* допустимы только пробы с `risk` не выше `low` и `reversible=True`;
* стоимость по энергии укладывается в бюджет `probes.energy_budget_wh`, а команда пробы проходит проверку
  «что если» (`EnergyPlanner.command_risk`): проба не должна разряжать АКБ ниже безопасного уровня;
* проба не мешает безопасным процессам: вытяжку включаем, но никогда не выключаем;
* частота ограничена (`max_per_10min`, `min_gap_s`), чтобы не засорять эфир и не тратить энергию;
* любая проба с командой идёт **только через `Guard`** (класс `needs_human` для всего, что меняет состояние
  оборудования, если иное не разрешено конфигом); пробы с просьбой к Оператору оформляются текстовой командой
  (формат ниже) и пишутся в журнал;
* автоматические пробы предпочтительнее: человеку задаём вопрос, если автоматической нет или она не дала ответа.

Формат текстовой команды Оператору («язык команд», допущение — формат в регламенте не задан)::

    ШТАБ→ОПЕРАТОР [проба <id>] <действие>. Если реально: <ожидание>. Если ложно: <ожидание>. Ответ: «реально» или «ложно».
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

from kupol.core.baseline import Baseline
from kupol.core.config import AppConfig, ProbeSpec
from kupol.core.energy import EnergyPlanner, EnergyStatus
from kupol.core.guard import Guard
from kupol.core.journal import Journal
from kupol.core.models import (
    Command,
    CommandStatus,
    Evidence,
    JournalEntry,
    Probe,
    ProbeRisk,
    Telemetry,
    new_id,
)
from kupol.core.state import State

ALLOWED_RISKS = (ProbeRisk.NONE, ProbeRisk.LOW)
PROBE_ACTOR = "probes"


def to_probe(spec: ProbeSpec) -> Probe:
    """Превратить описание пробы из конфига в модель `Probe`.

    Args:
        spec: Описание из `config.yaml`.

    Returns:
        Проба; при `risk` выше `low` — `ValueError` (такие пробы недопустимы по регламенту безопасности).
    """
    if spec.risk not in ("none", "low"):
        raise ValueError(f"проба '{spec.id}' имеет риск '{spec.risk}', допустимы только none/low")
    return Probe(
        id=spec.id,
        description=spec.description,
        cost_wh=spec.cost_wh,
        cost_s=spec.cost_s,
        reversible=spec.reversible,
        risk=ProbeRisk(spec.risk),
        expected_if_real=spec.expected_if_real,
        expected_if_false=spec.expected_if_false,
        duration_s=spec.duration_s,
        command=Command(name=spec.command.name, args=dict(spec.command.args)) if spec.command else None,
        human_action=spec.human_action,
        applies_to=list(spec.applies_to),
    )


def operator_message(probe: Probe, deadline_s: float) -> str:
    """Текстовая команда Оператору по формату «язык команд»."""
    return (
        f"ШТАБ→ОПЕРАТОР [проба {probe.id}] {probe.human_action or probe.description}. "
        f"Если реально: {probe.expected_if_real}. Если ложно: {probe.expected_if_false}. "
        f"Ответ: «реально» или «ложно» в течение {deadline_s:.0f} с."
    )


@dataclass
class ProbeResult:
    """Итог оценки пробы."""

    supports: str  # real | false | neutral
    weight: float
    detail: str
    kind_hint: str = ""


@dataclass
class ProbeRun:
    """Запущенная проба."""

    id: str
    probe: Probe
    subject: str
    started: datetime
    deadline: datetime
    state: str = "running"  # running | done | blocked | expired
    before: dict[str, float] = field(default_factory=dict)
    vent_mode_before: str = ""
    human_answer: tuple[str, str] | None = None  # (real|false, кто)
    result: ProbeResult | None = None
    message: str = ""


Evaluator = Callable[["ProbeManager", ProbeRun, Telemetry], ProbeResult]


class ProbeManager:
    """Выбор, запуск и оценка проб (потокобезопасность обеспечивает вызывающий движок)."""

    def __init__(
        self,
        config: AppConfig,
        guard: Guard,
        journal: Journal,
        planner: EnergyPlanner,
        baseline: Baseline | None = None,
    ) -> None:
        """Args:
        config: Конфигурация (раздел `probes`, пары датчиков).
        guard: Предохранитель (команды проб — только через него).
        journal: Журнал решений.
        planner: Планировщик энергии (проверка «что если» для команды пробы).
        baseline: Калибровка шума (допуск при сравнении соседних датчиков).
        """
        self._cfg = config
        self._pcfg = config.probes
        self._guard = guard
        self._journal = journal
        self._planner = planner
        self._baseline = baseline
        self.catalog: list[Probe] = [to_probe(s) for s in self._pcfg.catalog]
        self._evaluator_names = {s.id: s.evaluator for s in self._pcfg.catalog}
        self._info_gain = {s.id: s.info_gain for s in self._pcfg.catalog}
        self.runs: list[ProbeRun] = []
        self._started: list[datetime] = []
        self._evaluators: dict[str, Evaluator] = {
            "neighbors_agree": ProbeManager._eval_neighbors,
            "vent_response": ProbeManager._eval_vent,
            "operator_report": ProbeManager._eval_operator,
        }

    # ------------------------------------------------------------------ выбор

    def candidates(
        self,
        subject: str,
        tel: Telemetry,
        status: EnergyStatus | None,
        now: datetime,
        exclude: Iterable[str] = (),
    ) -> list[Probe]:
        """Допустимые пробы для утверждения (недопустимые отфильтрованы).

        Args:
            subject: Утверждение (поле).
            tel: Последний снимок.
            status: Энергостатус (для проверки бюджета и каскада).
            now: Текущее время.
            exclude: Идентификаторы проб, которые не предлагать.

        Returns:
            Пробы, удовлетворяющие всем ограничениям.
        """
        skip = set(exclude)
        return [p for p in self.catalog if p.id not in skip and self.reject_reason(p, subject, tel, status, now) is None]

    def reject_reason(self, probe: Probe, subject: str, tel: Telemetry, status: EnergyStatus | None, now: datetime) -> str | None:
        """Почему пробу нельзя запускать сейчас (`None` — можно)."""
        if probe.risk not in ALLOWED_RISKS:
            return f"риск '{probe.risk.value}' выше low"
        if not probe.reversible:
            return "проба необратима"
        if probe.applies_to and not any(fnmatch.fnmatchcase(subject, pat) for pat in probe.applies_to):
            return "проба неприменима к этому полю"
        if probe.cost_wh > self._pcfg.energy_budget_wh:
            return f"стоимость {probe.cost_wh:g} Вт·ч выше бюджета {self._pcfg.energy_budget_wh:g}"
        if len([t for t in self._started if (now - t).total_seconds() < 600.0]) >= self._pcfg.max_per_10min:
            return "исчерпан лимит проб за 10 минут"
        if self._started and (now - self._started[-1]).total_seconds() < self._pcfg.min_gap_s:
            return "слишком рано после предыдущей пробы"
        if any(r.state == "running" and r.probe.id == probe.id for r in self.runs):
            return "эта проба уже выполняется"
        cmd = probe.command
        if cmd is not None:
            if cmd.name in ("vent_enable", "set_vent_mode") and tel.vent_mode != "off":
                return "вытяжка уже включена: проба неинформативна и могла бы помешать (никогда не выключаем вытяжку)"
            risk = self._planner.command_risk(cmd, tel, status)
            if risk:
                return f"риск каскада: {risk}"
        return None

    def select(self, candidates: list[Probe], subject: str, ran_auto_before: bool = False) -> Probe | None:
        """Выбрать максимально информативную пробу из допустимых.

        Автоматические пробы предпочтительнее; к человеку идём, если автоматических нет или одна уже была,
        а вердикт не прояснился.

        Args:
            candidates: Допустимые пробы.
            subject: Утверждение.
            ran_auto_before: Была ли по этому утверждению автоматическая проба.

        Returns:
            Лучшая проба или `None`.
        """
        auto = [p for p in candidates if p.human_action is None]
        human = [p for p in candidates if p.human_action is not None]
        pool = auto if (auto and not ran_auto_before) else (human or auto)
        if not pool:
            return None

        def score(p: Probe) -> float:
            return self._info_gain.get(p.id, 0.5) - 0.002 * p.cost_s - 0.05 * p.cost_wh

        return max(pool, key=score)

    # ------------------------------------------------------------------ запуск

    def start(self, probe: Probe, subject: str, tel: Telemetry, state: State, now: datetime) -> ProbeRun:
        """Запустить пробу.

        Args:
            probe: Проба из каталога.
            subject: Утверждение, которое проверяем.
            tel: Последний снимок («до»).
            state: Состояние.
            now: Текущее время.

        Returns:
            Запись о запуске. Если команду пробы предохранитель не принял, `state == "blocked"`.
        """
        deadline_s = max(probe.duration_s, 1.0) if probe.human_action is None else max(probe.cost_s, probe.duration_s, 30.0)
        run = ProbeRun(
            id=new_id("prb"),
            probe=probe,
            subject=subject,
            started=now,
            deadline=now + timedelta(seconds=deadline_s),
            before=tel.flat(),
            vent_mode_before=tel.vent_mode,
        )
        self._started.append(now)
        self.runs.append(run)
        if probe.command is not None:
            cmd = probe.command.model_copy(update={"id": new_id("cmd"), "ts": now, "reason": f"проба {probe.id} по {subject}"})
            res = self._guard.submit(cmd, PROBE_ACTOR)
            if res.status not in (CommandStatus.ACCEPTED, CommandStatus.PENDING):
                run.state, run.message = "blocked", f"предохранитель: {res.status.value}: {res.message}"
            elif res.status is CommandStatus.PENDING:
                run.message = "команда пробы ждёт подтверждения человека"
        if probe.human_action is not None:
            run.message = operator_message(probe, deadline_s)
        self._log(now, run, "запущена" if run.state != "blocked" else "не запущена", run.message)
        return run

    def answer(self, run_id: str, supports: str, who: str) -> bool:
        """Ответ человека на вопрос пробы (`real` или `false`).

        Args:
            run_id: Идентификатор запуска.
            supports: `real` или `false`.
            who: Кто ответил.

        Returns:
            `True`, если ответ принят.
        """
        for run in self.runs:
            if run.id == run_id and run.state == "running" and run.probe.human_action is not None and supports in ("real", "false"):
                run.human_answer = (supports, who)
                return True
        return False

    def running(self) -> list[ProbeRun]:
        """Выполняющиеся пробы."""
        return [r for r in self.runs if r.state == "running"]

    def ran_auto(self, subject: str) -> bool:
        """Была ли по утверждению автоматическая проба."""
        return any(r.subject == subject and r.probe.human_action is None and r.state in ("done", "running") for r in self.runs)

    def has_recent(self, subject: str, now: datetime, within_s: float = 300.0) -> bool:
        """Была ли по утверждению проба недавно (не запускать повторно сразу)."""
        return any(r.subject == subject and (now - r.started).total_seconds() < within_s for r in self.runs)

    # ------------------------------------------------------------------ завершение и оценка

    def tick(self, state: State, now: datetime) -> list[tuple[ProbeRun, Evidence | None]]:
        """Завершить пробы, чей срок вышел (или на которые ответил человек).

        Args:
            state: Состояние (наблюдение «после»).
            now: Текущее время.

        Returns:
            Завершённые запуски и свидетельство для вердикта (`None`, если вывода нет).
        """
        latest = state.latest()
        done: list[tuple[ProbeRun, Evidence | None]] = []
        for run in self.running():
            asked_human = run.probe.human_action is not None
            if asked_human and run.human_answer is None:
                if now >= run.deadline:
                    run.state = "expired"
                    self._log(now, run, "истекла", "Оператор не ответил вовремя")
                    done.append((run, None))
                continue
            if not asked_human and now < run.deadline:
                continue
            if latest is None or not latest.comms_ok:
                continue
            evaluator = self._evaluators.get(self._evaluator_names.get(run.probe.id, "none"))
            if evaluator is None:
                run.state = "done"
                done.append((run, None))
                continue
            run.result = evaluator(self, run, latest)
            run.state = "done"
            ev = self._evidence(run, now)
            self._log(now, run, "завершена", f"{run.result.supports}: {run.result.detail}")
            done.append((run, ev))
        return done

    def _evidence(self, run: ProbeRun, now: datetime) -> Evidence | None:
        """Свидетельство из результата пробы (нейтральный результат свидетельством не является)."""
        r = run.result
        if r is None or r.supports == "neutral":
            return None
        source = "operator" if run.probe.human_action else f"probe:{run.probe.id}"
        return Evidence(
            source=source,
            supports=r.supports,
            weight=min(1.0, r.weight),
            detail=f"проба {run.probe.id}: {r.detail}",
            rule="probe",
            kind_hint=r.kind_hint,
            ts=now,
        )

    # ------------------------------------------------------------------ оценщики

    def _group_of(self, subject: str) -> list[str]:
        """Группа одноимённых датчиков, к которой относится поле."""
        for g in self._cfg.detectors.pair_diff.groups:
            if subject in g:
                return list(g)
        return []

    def _tolerance(self, a: str, b: str, key: str) -> float:
        """Допуск расхождения датчиков: коридор по σ baseline, иначе жёсткий предел из конфига."""
        pd = self._cfg.detectors.pair_diff
        hard: float = pd.max_abs_diff.get(key, float("inf"))
        if self._baseline is not None:
            sa, sb = self._baseline.noise_level(a), self._baseline.noise_level(b)
            if sa is not None and sb is not None:
                return float(min(hard, pd.corridor_k * (sa**2 + sb**2) ** 0.5 + abs(self._baseline.pair_offset(a, b) or 0.0)))
        return hard

    def _eval_neighbors(self, run: ProbeRun, now: Telemetry) -> ProbeResult:
        """Повторное измерение у соседних датчиков: согласны ли они с подозрительным."""
        group = self._group_of(run.subject)
        flat = now.flat()
        others = [f for f in group if f != run.subject and f in flat]
        if run.subject not in flat or not others:
            return ProbeResult("neutral", 0.0, "нет соседних датчиков для сверки")
        med = median(flat[f] for f in others)
        diff = abs(flat[run.subject] - med)
        key = group[0].split(".", 1)[0]
        tol = max(self._tolerance(run.subject, others[0], key), 1e-9)
        w = self._cfg.verdicts.weights.get("probe", 0.8)
        if diff <= tol:
            return ProbeResult("real", w * 0.75, f"{run.subject} согласуется с соседями (расхождение {diff:.3g} ≤ {tol:.3g})")
        return ProbeResult("false", w, f"{run.subject} расходится с соседями на {diff:.3g} (допуск {tol:.3g})", "false_signal")

    def _eval_vent(self, run: ProbeRun, now: Telemetry) -> ProbeResult:
        """Вытяжка на минимум: реагирует ли подозрительный датчик CO2 так же, как реальный рост."""
        w = self._cfg.verdicts.weights.get("probe", 0.8)
        if now.vent_mode == run.vent_mode_before:
            return ProbeResult("neutral", 0.0, "вытяжка не включилась: проба недействительна (возможен отказ исполнителя)")
        flat = now.flat()
        before = run.before.get(run.subject)
        after = flat.get(run.subject)
        if before is None or after is None:
            return ProbeResult("neutral", 0.0, "нет значений до/после")
        sigma = self._baseline.noise_level(run.subject) if self._baseline else None
        react = (sigma or 0.0) * 3.0 + 1e-9
        if after - before <= -react:
            return ProbeResult("real", w, f"{run.subject} снижается {before:.0f}→{after:.0f} ppm при включённой вытяжке: поведение реального CO2")
        return ProbeResult("false", w * 0.9, f"{run.subject} не реагирует на вытяжку ({before:.0f}→{after:.0f} ppm)", "false_signal")

    def _eval_operator(self, run: ProbeRun, now: Telemetry) -> ProbeResult:
        """Ответ Оператора: слово человека — самое весомое свидетельство."""
        assert run.human_answer is not None
        supports, who = run.human_answer
        w = self._cfg.verdicts.weights.get("operator", 0.9)
        hint = "" if supports == "real" else "false_signal"
        return ProbeResult(supports, w, f"{who} подтверждает: {'реально' if supports == 'real' else 'ложно'}", hint)

    # ------------------------------------------------------------------ журнал

    def _log(self, now: datetime, run: ProbeRun, what: str, detail: str) -> None:
        """Запись о пробе в журнал (п. 10.3 регламента): что, зачем, чем кончилось."""
        p = run.probe
        self._journal.log(
            JournalEntry(
                ts=now,
                actor=PROBE_ACTOR,
                event=f"проба {p.id} по {run.subject}: {what}",
                cross_check=f"ожидание при реальном: {p.expected_if_real}; при ложном: {p.expected_if_false}",
                hypothesis=run.subject,
                impact=f"стоимость {p.cost_wh:g} Вт·ч, {p.cost_s:g} с, риск {p.risk.value}",
                action=(f"{p.command.name} {p.command.args}" if p.command else (p.human_action or "сверка")),
                executor="Оператор" if p.human_action else "Штаб",
                result=detail,
                status=run.state,
                kind="probe",
                data={"run_id": run.id, "probe": p.id, "subject": run.subject, "state": run.state, "result": run.result.supports if run.result else None},
            )
        )
