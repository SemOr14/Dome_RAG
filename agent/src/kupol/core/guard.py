"""Предохранитель команд: единственный путь от агентов и UI к адаптеру.

Любая команда проходит только через ``Guard.submit(cmd, requested_by)``:

1. явный список запретов (аппаратные блокировки, регламент п. 9.2 и 12.1) —
   проверяется ПЕРВЫМ, раньше белого списка и диапазонов;
2. белый список и допустимые диапазоны аргументов из ``config.yaml``;
3. ограничение частоты на исполнителя;
4. **правило двух и защита от каскада** для дорогих действий (``costly``): без двух независимых
   свидетельств команда отклоняется; если действие разрядило бы АКБ ниже безопасного уровня или
   остановило неприрываемую операцию, оно не уходит автоматически, а превращается в рекомендацию.
   Исключение безопасности (``safety_event``: дым, перегрев паяльника, «Стоп» Оператора) обходит правило
   двух и проверку каскада, но НЕ белый список и диапазоны; команды человека (``human…``) — на его
   ответственности;
5. класс команды: ``auto`` выполняется сразу, ``needs_human`` ждёт подтверждения
   (``approve`` / ``reject``) — команда человека (``requested_by`` начинается с
   ``human``) считается подтверждённой самим автором;
6. после отправки проверяется эффект: за ``verify_timeout_s`` состояние должно
   измениться как ожидается, иначе создаётся тревога ``actuator_no_effect`` (запоздалый эффект
   засчитывается как ``actuator_delayed``);
7. всё пишется в журнал.

Адаптер напрямую вызывать запрещено — этот модуль единственный, кто держит ссылку на него
для отправки команд.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from kupol.adapter.base import AdapterError, AdapterTimeout, KupolAdapter
from kupol.core.config import ArgRule, CommandRule, GuardConfig, ResponseConfig
from kupol.core.journal import Journal
from kupol.core.models import (
    Alert,
    AlertLevel,
    Command,
    CommandResult,
    CommandStatus,
    JournalEntry,
    SentCommand,
    Telemetry,
)
from kupol.core.response import ResponsePolicy
from kupol.core.state import State

HUMAN_PREFIX = "human"


def _normalize(text: str) -> str:
    """Оставить только буквы и цифры в нижнем регистре (против обхода запрета дефисами и пробелами)."""
    return re.sub(r"[\W_]+", "", text.lower())


@dataclass
class PendingCommand:
    """Команда, ожидающая подтверждения человека.

    Attributes:
        command: Сама команда.
        requested_by: Кто предложил (агент).
        queued_at: Время постановки в очередь.
    """

    command: Command
    requested_by: str
    queued_at: datetime


@dataclass
class _Awaiting:
    """Отправленная команда, эффект которой ещё не подтверждён."""

    command: Command
    requested_by: str
    executor: str
    rule: CommandRule
    sent_at: datetime
    deadline: datetime
    record: SentCommand | None = None


class Guard:
    """Предохранитель команд (потокобезопасный)."""

    def __init__(
        self,
        config: GuardConfig,
        adapter: KupolAdapter,
        journal: Journal,
        state: State,
        clock: Callable[[], datetime] | None = None,
        response: ResponseConfig | None = None,
    ) -> None:
        """Создать предохранитель.

        Args:
            config: Раздел ``guard`` конфигурации.
            adapter: Адаптер Комплекса (команды уходят только через Guard).
            journal: Журнал решений.
            state: Состояние (для проверки эффекта команд).
            clock: Источник времени; по умолчанию ``adapter.now``.
            response: Раздел ``response`` (правило двух, события безопасности).
        """
        self._cfg = config
        self._adapter = adapter
        self._journal = journal
        self._state = state
        self._clock = clock or adapter.now
        self._lock = threading.RLock()
        self._response = response or ResponseConfig()
        self._policy = ResponsePolicy(self._response)
        # Что-если для дорогих команд: возвращает описание риска каскада или None (задаёт движок).
        self.cascade_check: Callable[[Command], str | None] | None = None
        # Обратная связь для «превращения в рекомендацию»: (команда, автор, риск).
        self.on_downgrade: Callable[[Command, str, str], None] | None = None
        self._pending: dict[str, PendingCommand] = {}
        self._awaiting: dict[str, _Awaiting] = {}
        self._sent: dict[str, deque[datetime]] = {}
        self._alerts: list[Alert] = []
        self._sent_log: deque[SentCommand] = deque(maxlen=40)
        self._late: dict[str, tuple[_Awaiting, datetime]] = {}
        self._forbidden = [(f.lower(), _normalize(f)) for f in config.forbidden if f.strip()]

    # ------------------------------------------------------------------ публичный API

    def submit(self, cmd: Command, requested_by: str) -> CommandResult:
        """Принять команду от агента или человека.

        Args:
            cmd: Команда.
            requested_by: Автор (``monitor``, ``energy_planner``, ``human:диагност`` и т.п.).

        Returns:
            Результат: ``REJECTED`` (отклонена предохранителем), ``PENDING`` (ждёт человека),
            ``ACCEPTED`` (принята адаптером) или ``FAILED`` (адаптер не принял).
        """
        with self._lock:
            problem = self._validate(cmd)
            if problem is not None:
                return self._finish(cmd, requested_by, CommandStatus.REJECTED, problem)
            rule = self._cfg.commands[cmd.name]
            gated = self._costly_gate(cmd, requested_by, rule)
            if gated is not None:
                return gated
            if rule.klass == "needs_human" and not requested_by.startswith(HUMAN_PREFIX):
                if any(p.command.name == cmd.name and p.command.args == cmd.args for p in self._pending.values()):
                    return self._finish(
                        cmd, requested_by, CommandStatus.REJECTED, "такая команда уже ожидает подтверждения"
                    )
                self._pending[cmd.id] = PendingCommand(cmd, requested_by, self._clock())
                return self._finish(
                    cmd,
                    requested_by,
                    CommandStatus.PENDING,
                    "требуется подтверждение человека",
                )
            return self._dispatch(cmd, requested_by, rule)

    def approve(self, command_id: str, approver: str = "human") -> CommandResult:
        """Подтвердить команду из очереди и отправить её.

        Команда проверяется заново: за время ожидания правила не должны нарушаться.

        Args:
            command_id: Идентификатор команды в очереди.
            approver: Кто подтвердил (попадает в журнал).

        Returns:
            Результат отправки либо ``REJECTED``, если команды нет или она больше не проходит проверки.
        """
        with self._lock:
            item = self._pending.pop(command_id, None)
            if item is None:
                return CommandResult(
                    command_id=command_id,
                    status=CommandStatus.REJECTED,
                    message="команда не найдена в очереди подтверждения",
                    ts=self._clock(),
                )
            cmd = item.command
            problem = self._validate(cmd)
            if problem is not None:
                return self._finish(cmd, item.requested_by, CommandStatus.REJECTED, f"после подтверждения: {problem}")
            self._journal_event(
                actor=approver,
                cmd=cmd,
                event=f"подтверждена команда {cmd.name} (предложил {item.requested_by})",
                result="подтверждено человеком",
                status="approved",
            )
            return self._dispatch(cmd, f"{HUMAN_PREFIX}:{approver}", self._cfg.commands[cmd.name], proposed_by=item.requested_by)

    def reject(self, command_id: str, approver: str = "human", reason: str = "") -> CommandResult:
        """Отклонить команду из очереди.

        Args:
            command_id: Идентификатор команды.
            approver: Кто отклонил.
            reason: Причина (в журнал).

        Returns:
            ``DENIED``, либо ``REJECTED``, если команды нет в очереди.
        """
        with self._lock:
            item = self._pending.pop(command_id, None)
            if item is None:
                return CommandResult(
                    command_id=command_id,
                    status=CommandStatus.REJECTED,
                    message="команда не найдена в очереди подтверждения",
                    ts=self._clock(),
                )
            msg = f"отклонено человеком ({approver})" + (f": {reason}" if reason else "")
            return self._finish(item.command, approver, CommandStatus.DENIED, msg, proposed_by=item.requested_by)

    def pending(self) -> list[PendingCommand]:
        """Команды, ожидающие подтверждения (старые первыми)."""
        with self._lock:
            return sorted(self._pending.values(), key=lambda p: p.queued_at)

    def is_pending(self, command_id: str) -> bool:
        """Ждёт ли команда подтверждения человека (не истекла и не обработана)."""
        with self._lock:
            return command_id in self._pending

    def sent_commands(self) -> list[SentCommand]:
        """Недавно отправленные команды с ожидаемым откликом (для сверки «причина → следствие»)."""
        with self._lock:
            return [s.model_copy() for s in self._sent_log]

    def awaiting_verification(self) -> int:
        """Сколько отправленных команд ещё ждут подтверждения эффекта."""
        with self._lock:
            return len(self._awaiting)

    def check_effects(self) -> list[Alert]:
        """Проверить эффект отправленных команд и сроки очереди.

        Вызывается главным циклом каждый такт после обновления состояния.

        Returns:
            Новые тревоги ``actuator_no_effect`` (и накопленные тревоги предохранителя).
        """
        with self._lock:
            now = self._clock()
            latest = self._state.latest()
            for cid, aw in list(self._awaiting.items()):
                if latest is None or not latest.comms_ok:
                    # Нет достоверных данных: срок проверки не идёт (иначе ложный no_effect).
                    aw.deadline = now + timedelta(seconds=self._cfg.verify_timeout_s)
                    continue
                if latest.ts <= aw.sent_at:
                    continue  # ещё нет снимка после отправки
                ok, expected, actual = self._effect(aw, latest)
                if ok:
                    del self._awaiting[cid]
                    self._mark(aw, "verified", now)
                    self._journal_event(
                        actor="guard",
                        cmd=aw.command,
                        event=f"эффект команды {aw.command.name} подтверждён",
                        result=f"ожидалось {expected!r}, наблюдается {actual!r}",
                        status=CommandStatus.VERIFIED.value,
                        executor=aw.executor,
                    )
                elif now >= aw.deadline:
                    del self._awaiting[cid]
                    self._mark(aw, "no_effect", now)
                    if self._cfg.late_effect_window_s > 0:
                        self._late[cid] = (aw, now + timedelta(seconds=self._cfg.late_effect_window_s))
                    self._journal_event(
                        actor="guard",
                        cmd=aw.command,
                        event=f"команда {aw.command.name} принята, но состояние не изменилось",
                        hypothesis="отказ исполнителя или ложная телеметрия",
                        result=f"ожидалось {expected!r}, наблюдается {actual!r}",
                        status=CommandStatus.NO_EFFECT.value,
                        executor=aw.executor,
                    )
                    self._alerts.append(
                        Alert(
                            ts=now,
                            level=AlertLevel.WARNING,
                            kind="actuator_no_effect",
                            field=aw.executor,
                            message=(
                                f"Команда {aw.command.name} {aw.command.args} принята, но за "
                                f"{self._cfg.verify_timeout_s:g} с эффекта нет (ожидалось {expected!r}, сейчас {actual!r})"
                            ),
                            details={"command_id": aw.command.id, "expected": expected, "actual": actual},
                        )
                    )
            self._check_late(now, latest)
            for cid, item in list(self._pending.items()):
                if (now - item.queued_at).total_seconds() >= self._cfg.pending_ttl_s:
                    del self._pending[cid]
                    self._finish(
                        item.command,
                        "guard",
                        CommandStatus.EXPIRED,
                        "подтверждение не получено вовремя",
                        proposed_by=item.requested_by,
                    )
            out, self._alerts = self._alerts, []
            return out

    def _mark(self, aw: _Awaiting, outcome: str, now: datetime) -> None:
        """Записать исход проверки эффекта в журнал отправленных команд."""
        if aw.record is not None:
            aw.record.outcome = outcome
            aw.record.delay_s = (now - aw.sent_at).total_seconds()

    def _check_late(self, now: datetime, latest: Telemetry | None) -> None:
        """Запоздалый эффект: команда «без эффекта» вдруг сработала (проверка результата с учётом задержки)."""
        if latest is None or not latest.comms_ok:
            return
        for cid, (aw, until) in list(self._late.items()):
            ok, expected, actual = self._effect(aw, latest)
            if ok:
                del self._late[cid]
                delay = (now - aw.sent_at).total_seconds()
                self._mark(aw, "late_verified", now)
                self._journal_event(
                    actor="guard",
                    cmd=aw.command,
                    event=f"команда {aw.command.name} сработала с опозданием {delay:.0f} с",
                    hypothesis="запаздывающий исполнитель, а не отказ",
                    result=f"ожидалось {expected!r}, наблюдается {actual!r}",
                    status="late_verified",
                    executor=aw.executor,
                )
                self._alerts.append(
                    Alert(
                        ts=now,
                        level=AlertLevel.WARNING,
                        kind="actuator_delayed",
                        field=aw.executor,
                        message=(
                            f"Исполнитель {aw.executor} выполнил {aw.command.name} {aw.command.args} с задержкой "
                            f"{delay:.0f} с (ожидалось не более {self._cfg.verify_timeout_s:g} с): это запаздывание, не отказ"
                        ),
                        value=delay,
                        detector="guard",
                        details={"command_id": cid, "delay_s": delay},
                    )
                )
            elif now >= until:
                del self._late[cid]

    # ------------------------------------------------------------------ правило двух и каскад

    def _is_costly(self, cmd: Command, rule: CommandRule) -> bool:
        """Дорогое или необратимое действие: флаг команды или правила из конфига."""
        return (cmd.costly or rule.costly) and "costly" in self._response.require_two_sources_for

    def _confirmations(self, cmd: Command) -> set[str]:
        """Независимые источники, подтверждающие реальность угрозы (по свидетельствам команды)."""
        return {e.source for e in cmd.evidence if e.supports == "real" and e.weight > 0}

    def _costly_gate(self, cmd: Command, requested_by: str, rule: CommandRule) -> CommandResult | None:
        """Правило двух и защита от каскада для дорогой команды; `None` — можно продолжать."""
        if not self._is_costly(cmd, rule) or requested_by.startswith(HUMAN_PREFIX):
            return None  # команды человека — на его ответственности (журнал отметит автора)
        safety = cmd.safety_event if cmd.safety_event in self._response.safety_override_events else None
        if safety is not None:
            return None  # исключение безопасности: белый список и диапазоны уже проверены в _validate
        ok, why = self._policy.enough_for_costly(self._confirmations(cmd), None)
        if not ok:
            return self._finish(
                cmd,
                requested_by,
                CommandStatus.REJECTED,
                f"ОТКЛОНЕНО правилом двух: дорогое действие требует независимых подтверждений — {why}",
            )
        if self.cascade_check is not None:
            risk = self.cascade_check(cmd)
            if risk:
                if self.on_downgrade is not None:
                    self.on_downgrade(cmd, requested_by, risk)
                return self._finish(
                    cmd,
                    requested_by,
                    CommandStatus.DOWNGRADED,
                    f"не отправлено автоматически, превращено в рекомендацию (риск каскада): {risk}",
                )
        return None

    # ------------------------------------------------------------------ проверки

    def _validate(self, cmd: Command) -> str | None:
        """Все статические проверки. Возвращает причину отказа или ``None``."""
        forbidden = self._check_forbidden(cmd)
        if forbidden is not None:  # СТРОГО первой: до белого списка и диапазонов
            return forbidden
        rule = self._cfg.commands.get(cmd.name)
        if rule is None:
            return f"команда '{cmd.name}' не входит в белый список"
        arg_problem = self._check_args(cmd, rule)
        if arg_problem is not None:
            return arg_problem
        return self._check_rate(cmd, rule)

    def _check_forbidden(self, cmd: Command) -> str | None:
        """Явный запрет: любое упоминание блокировок и аналогов в имени или аргументах."""
        parts = [cmd.name] + [str(k) for k in cmd.args] + [str(v) for v in cmd.args.values()]
        raw = " ".join(parts).lower()
        norm = _normalize(raw)
        for word, word_norm in self._forbidden:
            if word in raw or (word_norm and word_norm in norm):
                return f"ЗАПРЕЩЕНО: команда затрагивает защитные механизмы ('{word}'), регламент п. 9.2/12.1"
        return None

    @staticmethod
    def _check_args(cmd: Command, rule: CommandRule) -> str | None:
        """Набор и допустимые значения аргументов."""
        extra = set(cmd.args) - set(rule.args)
        if extra:
            return f"неизвестные аргументы: {sorted(extra)}"
        for name, arg_rule in rule.args.items():
            if name not in cmd.args:
                return f"не хватает аргумента '{name}'"
            problem = Guard._check_value(name, cmd.args[name], arg_rule)
            if problem is not None:
                return problem
        return None

    @staticmethod
    def _check_value(name: str, value: Any, rule: ArgRule) -> str | None:
        """Проверка одного значения по правилу."""
        if rule.type == "enum":
            if value not in rule.values or isinstance(value, bool) != any(isinstance(v, bool) for v in rule.values):
                return f"аргумент '{name}'={value!r} вне допустимого набора {rule.values}"
        elif rule.type == "bool":
            if not isinstance(value, bool):
                return f"аргумент '{name}' должен быть bool, получено {value!r}"
        else:  # number
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"аргумент '{name}' должен быть числом, получено {value!r}"
            if (rule.min is not None and value < rule.min) or (rule.max is not None and value > rule.max):
                return f"аргумент '{name}'={value} вне диапазона [{rule.min}, {rule.max}]"
        return None

    def _executor(self, cmd: Command, rule: CommandRule) -> str:
        """Ключ исполнителя для ограничения частоты."""
        try:
            return rule.executor.format(**cmd.args)
        except (KeyError, IndexError):  # pragma: no cover - аргументы уже проверены
            return rule.executor

    def _check_rate(self, cmd: Command, rule: CommandRule) -> str | None:
        """Не больше N отправленных команд исполнителю за окно T."""
        limit = rule.rate_limit or self._cfg.default_rate_limit
        executor = self._executor(cmd, rule)
        now = self._clock()
        window = self._sent.get(executor, deque())
        recent = [t for t in window if (now - t).total_seconds() < limit.window_s]
        if len(recent) >= limit.max_commands:
            return (
                f"превышена частота команд исполнителю '{executor}': "
                f"{limit.max_commands} за {limit.window_s:g} с"
            )
        return None

    # ------------------------------------------------------------------ отправка и журнал

    def _dispatch(
        self, cmd: Command, requested_by: str, rule: CommandRule, proposed_by: str | None = None
    ) -> CommandResult:
        """Отправить проверенную команду адаптеру."""
        executor = self._executor(cmd, rule)
        now = self._clock()
        try:
            res = self._adapter.send_command(cmd)
        except AdapterTimeout as exc:
            return self._finish(cmd, requested_by, CommandStatus.FAILED, f"таймаут адаптера: {exc}", proposed_by)
        except AdapterError as exc:
            return self._finish(cmd, requested_by, CommandStatus.FAILED, f"ошибка адаптера: {exc}", proposed_by)
        self._sent.setdefault(executor, deque()).append(now)
        if res.status is not CommandStatus.ACCEPTED:
            return self._finish(cmd, requested_by, CommandStatus.FAILED, res.message or "адаптер не принял команду", proposed_by)
        latest = self._state.latest()
        record = SentCommand(
            command=cmd,
            sent_at=now,
            executor=executor,
            state_path=rule.verify.path.format(**cmd.args) if rule.verify else None,
            state_expected=(cmd.args[rule.verify.equals_arg] if rule.verify.equals_arg else rule.verify.equals) if rule.verify else None,
            state_before=latest.lookup(rule.verify.path.format(**cmd.args)) if latest and rule.verify else None,
            power_path=rule.power_path.format(**cmd.args) if rule.power_path else None,
            power_before=(
                float(latest.lookup(rule.power_path.format(**cmd.args)))
                if latest and rule.power_path and latest.lookup(rule.power_path.format(**cmd.args)) is not None
                else None
            ),
        )
        self._sent_log.append(record)
        if rule.verify is not None:
            self._awaiting[cmd.id] = _Awaiting(
                command=cmd,
                requested_by=requested_by,
                executor=executor,
                rule=rule,
                sent_at=now,
                deadline=now + timedelta(seconds=self._cfg.verify_timeout_s),
                record=record,
            )
        return self._finish(cmd, requested_by, CommandStatus.ACCEPTED, res.message or "принята адаптером", proposed_by)

    def _effect(self, aw: _Awaiting, latest: Telemetry) -> tuple[bool, Any, Any]:
        """Сравнить ожидаемое и наблюдаемое состояние. Возвращает (ок, ожидалось, наблюдается)."""
        verify = aw.rule.verify
        assert verify is not None
        path = verify.path.format(**aw.command.args)
        expected = aw.command.args[verify.equals_arg] if verify.equals_arg else verify.equals
        actual = latest.lookup(path)
        return actual == expected, expected, actual

    def _finish(
        self,
        cmd: Command,
        requested_by: str,
        status: CommandStatus,
        message: str,
        proposed_by: str | None = None,
    ) -> CommandResult:
        """Записать итог обработки в журнал и вернуть результат."""
        rule = self._cfg.commands.get(cmd.name)
        executor = self._executor(cmd, rule) if rule is not None and self._args_formattable(cmd, rule) else ""
        by = f"{requested_by} (предложил {proposed_by})" if proposed_by and proposed_by != requested_by else requested_by
        self._journal_event(
            actor=requested_by,
            cmd=cmd,
            event=f"команда {cmd.name} {cmd.args} от {by}",
            result=message,
            status=status.value,
            executor=executor,
            impact=cmd.reason,
        )
        if status is CommandStatus.REJECTED and message.startswith("ЗАПРЕЩЕНО"):
            self._alerts.append(
                Alert(
                    ts=self._clock(),
                    level=AlertLevel.CRITICAL,
                    kind="forbidden_command",
                    message=f"Попытка запрещённой команды от {requested_by}: {cmd.name} {cmd.args}",
                    details={"command_id": cmd.id},
                )
            )
        return CommandResult(command_id=cmd.id, status=status, message=message, ts=self._clock())

    @staticmethod
    def _args_formattable(cmd: Command, rule: CommandRule) -> bool:
        """Можно ли вычислить исполнителя (аргументы не повреждены)."""
        try:
            rule.executor.format(**cmd.args)
        except (KeyError, IndexError):
            return False
        return True

    def _journal_event(
        self,
        actor: str,
        cmd: Command,
        event: str,
        result: str,
        status: str,
        executor: str = "",
        hypothesis: str = "",
        impact: str = "",
    ) -> None:
        """Запись в журнал по форме п. 7.3 регламента."""
        self._journal.log(
            JournalEntry(
                ts=self._clock(),
                actor=actor,
                event=event,
                hypothesis=hypothesis,
                impact=impact,
                action=f"{cmd.name} {cmd.args}",
                executor=executor,
                result=result,
                status=status,
                kind="command",
                data={
                    "command_id": cmd.id,
                    "command": cmd.name,
                    "args": cmd.args,
                    "costly": cmd.costly or (self._cfg.commands[cmd.name].costly if cmd.name in self._cfg.commands else False),
                    "evidence_sources": sorted(self._confirmations(cmd)),
                    "safety_event": cmd.safety_event,
                },
            )
        )
