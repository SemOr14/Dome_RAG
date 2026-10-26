"""Gate — единственный путь команд в купол.

Порядок проверок: роль команды → каталог ``describe_nodes`` → политика ``dome.yaml`` (что агенту вообще
разрешено) → значение → защитные правила по состоянию (``resume`` только после снятия причины, запуск
печати на свободный стол, анти-дребезг линий, запрет повтора без проверки) → режим (по умолчанию
«только чтение»: команда пишется в лог как ``dry_run`` и в сеть не уходит) → отправка → отложенная
проверка эффекта по телеметрии (``verification``).

Каждая команда обязана ссылаться на решение (``ref``): так лог остаётся цепочкой
``decision → action → verification``.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from kupol.dome.agentlog import AgentLog
from kupol.dome.catalog import Catalog
from kupol.dome.client import DomeError
from kupol.dome.config import DomeConfig
from kupol.dome.evidence import finite_number, measurement_usable
from kupol.dome.state import DomeState

OPERATOR_NAME = "Агент Штаба"
MAX_CHAT_CHARS = 1000  # короткие сообщения: Оператор не должен читать простыни
MACHINES = ("printer_3d_01", "cnc_01")
LINE_ACTIONS = ("line_on", "line_off")
CLIMATE_PARAMS = ("co2_ppm", "humidity_pct", "dew_point_c", "temperature_c")

GateStatus = Literal["sent", "dry_run", "rejected", "failed"]
SendFn = Callable[[str, str, Any], Awaitable[dict[str, Any]]]


@dataclass
class Expect:
    """Ожидаемый эффект команды в телеметрии."""

    description: str
    check: Callable[[DomeState], bool]
    observe: Callable[[DomeState], str]
    timeout_s: float | None = None

    @classmethod
    def equals(cls, path: str, expected: Any, timeout_s: float | None = None) -> Expect:
        """``path`` принимает значение ``expected`` (строки без учёта регистра, числа с допуском)."""

        def same(value: Any) -> bool:
            if isinstance(expected, str):
                return isinstance(value, str) and value.lower() == expected.lower()
            if isinstance(expected, bool) or isinstance(value, bool):
                return value is expected
            if isinstance(expected, (int, float)) and isinstance(value, (int, float)):
                return abs(float(value) - float(expected)) < 1e-6
            return bool(value == expected)

        return cls(f"{path} = {expected}", lambda st: same(st.get(path)), lambda st: f"{path} = {st.get(path)}", timeout_s)

    @classmethod
    def custom(cls, description: str, check: Callable[[DomeState], bool], paths: list[str], timeout_s: float | None = None) -> Expect:
        """Произвольное условие; ``paths`` — что показать в ``observed``."""
        return cls(description, check, lambda st: "; ".join(f"{p} = {st.get(p)}" for p in paths), timeout_s)


@dataclass
class Cmd:
    """Команда куполу."""

    node_id: str
    action: str
    value: Any = None
    reason: str = ""
    ref: int | None = None  # seq записи decision
    expect: Expect | None = None
    confirmed: bool = False  # человек подтвердил ручную команду: только роль и каталог, без политики и защитных правил

    @property
    def key(self) -> str:
        return f"{self.node_id}:{self.action}:{self.value!r}"


@dataclass
class GateResult:
    """Итог обработки команды."""

    status: GateStatus
    reason: str = ""
    response: dict[str, Any] | None = None
    action_seq: int | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("sent", "dry_run")


@dataclass
class Pending:
    """Отправленная команда, эффект которой ещё проверяется."""

    cmd: Cmd
    action_seq: int
    deadline: float
    sent_at: float
    updates: int = 0
    node_updated_at: Any = None
    verified: bool | None = None
    job_file: str | None = None
    job_progress: float | None = None


def continued_job(node: str, original: Expect, file_name: Any, progress: Any) -> Expect:
    def check(st: DomeState) -> bool:
        current = st.num(f"{node}.progress_pct")
        return (original.check(st) and st.get(f"{node}.file_name") == file_name
                and finite_number(progress) and current is not None and current > progress)
    return Expect.custom(f"{node}: то же задание работает, прогресс увеличился", check,
                         [f"{node}.state", f"{node}.file_name", f"{node}.progress_pct"],
                         max(original.timeout_s or 0, 45.0))


@dataclass
class _Spec:
    kind: str
    arg: Any = None


def _line_number(value: Any) -> int | None:
    if isinstance(value, dict):
        value = value.get("line")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def check_value(spec: dict[str, Any], value: Any) -> str | None:
    """Проверить значение по спецификации политики. Возвращает причину отказа или ``None``."""
    if not spec:
        return None if value is None else "значение не ожидается"
    if spec.get("any"):
        return None
    if spec.get("type") == "boolean":
        return None if isinstance(value, bool) else "нужен bool"
    if spec.get("type") == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        return "нужно целое число"
    if "enum" in spec:
        allowed = [str(v) for v in spec["enum"]]
        return None if str(value) in allowed else f"допустимо только {allowed}"
    if "range" in spec:
        lo, hi = spec["range"]
        if not finite_number(value) or not lo <= value <= hi:
            return f"допустим диапазон {lo}..{hi}"
        return None
    if spec.get("line"):
        n = _line_number(value)
        return None if n is not None and 1 <= n <= 8 else "нужен номер линии 1..8"
    if "object" in spec:
        if not isinstance(value, dict) or any(k not in value for k in spec["object"]):
            return f"нужен объект с ключами {spec['object']}"
        for key, rule in spec.get("properties", {}).items():
            if key in value and (bad := check_value(rule, value[key])):
                return f"{key}: {bad}"
        return None
    return "неизвестная спецификация значения"


class Gate:
    """Проверка, отправка и контроль эффекта команд."""

    def __init__(
        self,
        cfg: DomeConfig,
        state: DomeState,
        catalog: Callable[[], Catalog],
        log: AgentLog,
        send: SendFn,
        *,
        can_control: bool,
        dry_run: bool = False,
        clock: Callable[[], float] = time.time,
        agent_name: str = OPERATOR_NAME,
    ) -> None:
        """Создать Gate.

        Args:
            cfg: Конфигурация (политика, лимиты).
            state: Состояние купола (для защитных правил и проверки эффекта).
            catalog: Источник текущего каталога узлов.
            log: Лог агента.
            send: Отправка ``control`` (``client.control``).
            can_control: Разрешена ли реальная отправка (``DOME_ALLOW_CONTROL=1``).
            dry_run: Явный режим «только анализ» (приоритетнее ``can_control``).
            clock: Секунды (Unix-время: состояние сохраняется между запусками).
            agent_name: Имя агента в чате.
        """
        self._cfg = cfg
        self._state = state
        self._catalog = catalog
        self._log = log
        self._send = send
        self.can_control = can_control and not dry_run
        self.dry_run = dry_run
        self._clock = clock
        self.agent_name = agent_name
        self.pending: list[Pending] = []
        self.max_telemetry_age_s: float | None = None  # команды не отправляем, если телеметрия старше (None — не проверять)
        self._line_switches: dict[int, deque[float]] = {}
        self._chat_times: deque[float] = deque()
        self._failed_until: dict[str, float] = {}
        self._resume_attempts: dict[str, int] = {}
        self.stats = {"sent": 0, "dry_run": 0, "rejected": 0, "failed": 0, "verified": 0, "no_effect": 0}
        self.checkpoint: Callable[[], bool] | None = None

    def export_runtime(self) -> dict[str, Any]:
        return {"resume_attempts": dict(self._resume_attempts),
                "line_switches": {str(k): list(v) for k, v in self._line_switches.items()},
                "pending": [{"node": p.cmd.node_id, "action": p.cmd.action, "value": p.cmd.value,
                             "ref": p.cmd.ref, "seq": p.action_seq, "deadline": p.deadline,
                             "sent_at": p.sent_at, "job_file": p.job_file,
                             "job_progress": p.job_progress} for p in self.pending]}

    def import_runtime(self, data: dict[str, Any]) -> None:
        from kupol.dome.expects import expect_for
        self._resume_attempts = {str(k): int(v) for k, v in data.get("resume_attempts", {}).items()}
        self._line_switches = {int(k): deque(float(t) for t in v) for k, v in data.get("line_switches", {}).items()}
        for p in data.get("pending", []):
            ref = self._log.write("decision", situation="RECONCILE: неизвестен итог команды до перезапуска",
                                  cause="перезапуск", confidence=0.0, evidence=[{"previous_action_seq": p["seq"]}],
                                  plan=["проверить эффект без повторной отправки"], source="rules")
            cmd = Cmd(p["node"], p["action"], p.get("value"), ref=ref,
                      expect=expect_for(p["node"], p["action"], p.get("value")))
            if cmd.action == "resume":
                cmd.expect = continued_job(cmd.node_id, cmd.expect, p.get("job_file"), p.get("job_progress"))
            seq = self._log.write("action", ref=ref, request=self._request(cmd, cmd.action),
                                  response={"success": None, "delivery": "unknown_after_restart"})
            self.pending.append(Pending(cmd, seq, float(p["deadline"]), float(p["sent_at"]),
                                        job_file=p.get("job_file"), job_progress=p.get("job_progress")))

    # ------------------------------------------------------------------ публичный API

    def recently_failed(self, cmd_key: str) -> bool:
        """Недавно ли такая же команда не дала эффекта (пауза ``no_effect_cooldown_s``)."""
        return self._failed_until.get(cmd_key, 0.0) > self._clock()

    def export_failed(self) -> dict[str, float]:
        """Паузы после «нет эффекта» (для сохранения между запусками)."""
        now = self._clock()
        return {k: v for k, v in self._failed_until.items() if v > now}

    def import_failed(self, data: dict[str, Any]) -> None:
        """Восстановить паузы после «нет эффекта»."""
        self._failed_until = {str(k): float(v) for k, v in data.items()}

    def is_pending(self, node_id: str, action: str | None = None) -> bool:
        """Есть ли у узла (и действия) непроверенная команда."""
        return any(p.cmd.node_id == node_id and (action is None or p.cmd.action == action) for p in self.pending)

    def resume_attempts(self, node_id: str, reason: str | None) -> int:
        """Сколько раз агент уже делал ``resume`` узлу при этой причине удержания."""
        return self._resume_attempts.get(f"{node_id}:{reason}", 0)

    async def submit(self, cmd: Cmd) -> GateResult:
        """Проверить и (если разрешено) отправить команду."""
        canonical = self._catalog().resolve(cmd.node_id, cmd.action)
        if canonical is not None:
            cmd.action = canonical  # aliases share locks, retry budgets and reconciliation
        reason = self._reject_reason(cmd)
        if reason is not None:
            return self._reject(cmd, reason)
        canonical = self._catalog().resolve(cmd.node_id, cmd.action) or cmd.action
        if canonical == "resume" and cmd.expect is not None:
            cmd.expect = continued_job(cmd.node_id, cmd.expect, self._state.get(f"{cmd.node_id}.file_name"),
                                       self._state.num(f"{cmd.node_id}.progress_pct"))
        request = self._request(cmd, canonical)
        if not self.can_control:
            self.stats["dry_run"] += 1
            self._note_attempt(cmd, canonical)
            seq = self._log.write(
                "action", ref=cmd.ref, request=request, response={"success": None, "skipped": "dry_run" if self.dry_run else "управление выключено"},
                dry_run=True, reason=cmd.reason,
            )
            return GateResult("dry_run", "в сеть не отправлено", None, seq)
        # Persist intent and retry budgets BEFORE any network side effect.
        seq = self._log.write("observation", ref=cmd.ref, summary="намерение сохранено перед отправкой",
                              metrics={"request": request, "delivery": "prepared"}, anomalies=[])
        self._note_attempt(cmd, canonical)
        self._arm(cmd, seq)
        if self.checkpoint is not None and not self.checkpoint():
            self.pending = [p for p in self.pending if p.action_seq != seq]
            return self._reject(cmd, "не удалось сохранить намерение: управление приостановлено")
        try:
            response = await self._send(cmd.node_id, canonical, cmd.value)
        except DomeError as exc:
            self.stats["failed"] += 1
            seq = self._log.write("action", ref=cmd.ref, request=request, response={"success": False, "error": str(exc)}, reason=cmd.reason)
            # Состояние неизвестно: повтор вслепую запрещён — ставим на проверку, если есть чем.
            self._failed_until[cmd.key] = self._clock() + self._cfg.policy.no_effect_cooldown_s
            for p in self.pending:
                if p.cmd is cmd:
                    p.action_seq = seq
            return GateResult("failed", str(exc), None, seq)
        ok = bool(response.get("success"))
        seq = self._log.write(
            "action", ref=cmd.ref, request=request,
            response={"success": ok, "error": response.get("error"), "mode": response.get("mode"), "request_id": response.get("request_id")},
            reason=cmd.reason,
        )  # fmt: skip
        for p in self.pending:
            if p.cmd is cmd:
                p.action_seq = seq
        if not ok:
            self.pending = [p for p in self.pending if p.cmd is not cmd]
            self.stats["failed"] += 1
            return GateResult("failed", str(response.get("error", "")), response, seq)
        self.stats["sent"] += 1
        return GateResult("sent", "", response, seq)

    def check_pending(self) -> list[Pending]:
        """Проверить эффект отправленных команд; результат пишется в лог. Возвращает завершённые проверки."""
        done: list[Pending] = []
        now = self._clock()
        for node in MACHINES:  # станок снова работает — следующая остановка считается новым эпизодом
            points = self._state.series(f"{node}.progress_pct", 60.0)
            progressed = len(points) >= 2 and points[-1][1] > points[0][1]
            if (progressed and not self.is_pending(node, "resume") and self._state.get(f"{node}.hold_reason") is None
                    and str(self._state.get(f"{node}.state", "")).upper() in ("PRINTING", "RUNNING")):
                for key in [k for k in self._resume_attempts if k.startswith(f"{node}:")]:
                    del self._resume_attempts[key]
        for p in list(self.pending):
            exp = p.cmd.expect
            if exp is None:
                self.pending.remove(p)
                continue
            stamp = self._state.node(p.cmd.node_id).get("updated_at")
            fresh = (self._state.updates > p.updates and finite_number(stamp)
                     and stamp != p.node_updated_at and not self._state.is_frozen(p.cmd.node_id))
            if fresh and exp.check(self._state):
                ok = True
            elif now >= p.deadline:
                ok = False
            else:
                continue
            self.pending.remove(p)
            p.verified = ok
            done.append(p)
            self.stats["verified" if ok else "no_effect"] += 1
            if not ok:
                self._failed_until[p.cmd.key] = now + self._cfg.policy.no_effect_cooldown_s
            self._log.write(
                "verification", ref=p.action_seq, expected=exp.description, observed=exp.observe(self._state), ok=ok,
                waited_s=round(now - p.sent_at, 1),
            )  # fmt: skip
        return done

    # ------------------------------------------------------------------ проверки

    def _reject(self, cmd: Cmd, reason: str) -> GateResult:
        self.stats["rejected"] += 1
        seq = self._log.write(
            "action", ref=cmd.ref, request=self._request(cmd, cmd.action), response={"success": False, "error": f"Gate: {reason}"},
            rejected=True, reason=cmd.reason,
        )  # fmt: skip
        return GateResult("rejected", reason, None, seq)

    @staticmethod
    def _request(cmd: Cmd, action: str) -> dict[str, Any]:
        req: dict[str, Any] = {"type": "control", "node_id": cmd.node_id, "action": action}
        if cmd.value is not None:
            req["value"] = cmd.value
        return req

    def _reject_reason(self, cmd: Cmd) -> str | None:
        if cmd.ref is None:
            return "команда без решения (нет ref на decision)"
        if self._state.role != "team":
            return f"роль соединения '{self._state.role}': управление недоступно"
        if not cmd.confirmed and self.max_telemetry_age_s is not None and self._state.wall and self._clock() - self._state.wall > self.max_telemetry_age_s:
            return f"телеметрия устарела (> {self.max_telemetry_age_s:g} с): решаем по неизвестному состоянию нельзя"
        catalog = self._catalog()
        if not catalog:
            return "каталог узлов (describe_nodes) ещё не получен"
        canonical = catalog.resolve(cmd.node_id, cmd.action)
        if canonical is None:
            if catalog.is_operator_only(cmd.node_id, cmd.action):
                return "физическое действие Оператора: агент может только попросить"
            return f"действие '{cmd.action}' недоступно узлу {cmd.node_id} по describe_nodes"
        if cmd.confirmed:
            return None  # ответственность за команду на человеке, подтвердившем её в панели
        allowed = self._cfg.policy.allowed
        spec = (allowed.get(cmd.node_id) or {}).get(canonical)
        if spec is None:
            spec = (allowed.get("*") or {}).get(canonical)
        if spec is None:
            return f"действие {cmd.node_id}.{canonical} не разрешено политикой агента"
        bad = check_value(spec, cmd.value)
        if bad is not None:
            return f"значение {cmd.value!r}: {bad}"
        if cmd.node_id == "meeting_01":
            return self._chat_reason(cmd)
        if self.is_pending(cmd.node_id, canonical) and any(p.cmd.key == cmd.key for p in self.pending):
            return "такая же команда ещё ожидает проверки результата"
        if self.is_pending(cmd.node_id):
            return "узел ожидает проверки предыдущей команды; требуется новый кадр"
        if self.recently_failed(cmd.key):
            return "такая же команда недавно не дала эффекта: повтор вслепую запрещён"
        if canonical in LINE_ACTIONS:
            bad = self._line_reason(cmd.value)
            if bad:
                return bad
        return self._state_reason(cmd, canonical)

    def _chat_reason(self, cmd: Cmd) -> str | None:
        value = cmd.value if isinstance(cmd.value, dict) else {}
        text = str(value.get("text", ""))
        if not text.strip():
            return "пустое сообщение"
        if len(text) > MAX_CHAT_CHARS:
            return f"сообщение длиннее {MAX_CHAT_CHARS} символов"
        to = value.get("to")
        if to not in (None, "operator"):
            return "писать можно только Оператору (не другим командам)"
        lim = self._cfg.runtime.chat_limit
        now = self._clock()
        while self._chat_times and now - self._chat_times[0] > lim.window_s:
            self._chat_times.popleft()
        if len(self._chat_times) >= lim.max_messages:
            return f"лимит чата: не больше {lim.max_messages} сообщений за {lim.window_s:g} с"
        return None

    def _line_reason(self, value: Any) -> str | None:
        n = _line_number(value)
        if n is None:
            return None
        lim = self._cfg.policy.line_switch_limit
        now = self._clock()
        dq = self._line_switches.setdefault(n, deque())
        while dq and now - dq[0] > lim.window_s:
            dq.popleft()
        if len(dq) >= lim.max_switches:
            return f"анти-дребезг: линия {n} уже переключалась {len(dq)} раз за {lim.window_s:g} с (сервер блокирует после 3)"
        line = self._state.line(n)
        if float(line.get("lockout_remaining_s") or 0) > 0:
            return f"линия {n} заблокирована сервером ({line.get('lockout_reason')})"
        return None

    def _note_attempt(self, cmd: Cmd, canonical: str) -> None:
        now = self._clock()
        if canonical in LINE_ACTIONS:
            n = _line_number(cmd.value)
            if n is not None:
                self._line_switches.setdefault(n, deque()).append(now)
        if canonical == "resume":
            hold = self._state.get(f"{cmd.node_id}.hold_reason")
            key = f"{cmd.node_id}:{hold}"
            self._resume_attempts[key] = self._resume_attempts.get(key, 0) + 1
        if cmd.node_id == "meeting_01":
            self._chat_times.append(now)

    def _arm(self, cmd: Cmd, seq: int) -> None:
        if cmd.expect is None:
            return
        now = self._clock()
        timeout = cmd.expect.timeout_s or self._cfg.runtime.verify_timeout_s
        self.pending.append(Pending(cmd, seq, now + timeout, now, self._state.updates,
                                    self._state.node(cmd.node_id).get("updated_at"),
                                    job_file=self._state.get(f"{cmd.node_id}.file_name"),
                                    job_progress=self._state.num(f"{cmd.node_id}.progress_pct")))

    # ------------------------------------------------------------------ защитные правила по состоянию

    def _state_reason(self, cmd: Cmd, canonical: str) -> str | None:
        st = self._state
        node, value = cmd.node_id, cmd.value
        if node != "meeting_01" and canonical != "reconnect" and (not st.has(node) or st.is_frozen(node) or st.get(f"{node}.online") is False):
            return "нет актуального состояния целевого узла"
        if node != "meeting_01" and canonical != "reconnect" and not finite_number(st.get(f"{node}.updated_at")):
            return "неизвестно время измерения целевого узла"
        if node == "solar_inverter_01" and canonical == "reset_error" and st.get(f"{node}.error_code") == "E09":
            return "E09: исправность изоляции не подтверждается текущим API; нужна проверка Оператора"
        if node == "dome_automation_01" and canonical == "reset_interlock" and value == "IL_OVERCURRENT":
            return "устранение перегрузки не подтверждено; таймер не даёт допуска на повторное включение"
        if node == "dome_automation_01" and canonical == "acknowledge":
            event_id = value.get("event_id") if isinstance(value, dict) else value
            events = st.get("dome_automation_01.events", []) or []
            event = next((e for e in events if isinstance(e, dict) and str(e.get("event_id")) == str(event_id)), None)
            if not event or event.get("interlock") == "IL_SMOKE":
                return "нет проверенного подтверждения текущего события и зоны; отменять пожарную реакцию нельзя"
        if node in ("smoke_detector_01", "smoke_detector_02") and canonical == "reset_alarm":
            if not measurement_usable(st, node) or st.get(f"{node}.smoke_detected") is not False:
                return "отсутствие дыма не подтверждено актуальным измерением"
        if node == "dome_automation_01" and canonical == "reset_interlock" and value in ("IL_CO2", "IL_CONDENSATE"):
            from kupol.dome.checks import assess_co2, assess_condensate
            if value == "IL_CO2":
                a = assess_co2(st, self._cfg.checks)
                good = a.good()
                primary = st.get("dome_automation_01.primary_co2_sensor")
                if len(good) < 2 or primary not in good or max(good.values()) >= self._cfg.checks.co2.recovered_ppm:
                    return "IL_CO2: нет двух актуальных согласованных безопасных источников"
            else:
                from kupol.dome.checks import is_untrusted
                a = assess_condensate(st, self._cfg.checks)
                live = [s for s in a.rh if not is_untrusted(st, s, "humidity_pct")]
                if len(live) < 2 or any(s not in a.gap or a.trigger(s, self._cfg.checks.condensate.rh_interlock_pct,
                                                                self._cfg.checks.condensate.dew_gap_c) for s in live):
                    return "IL_CONDENSATE: безопасные условия не подтверждены актуальными источниками"
        if node in MACHINES and canonical == "resume":
            return self._resume_reason(node)
        if node in MACHINES and canonical == "start_job":
            return self._start_job_reason(node, value)
        if node == "thermal_insulation_01" and canonical == "set_lock":
            if isinstance(value, dict) and value.get("locked") is False:
                printer_state = str(st.get("printer_3d_01.state", "")).upper()
                if printer_state in ("PRINTING", "PAUSED") or str(st.get("printer_3d_01.real_state", "")).upper() == "PRINTING":
                    return "принтер печатает: бокс открывать нельзя"
                bed = st.num("printer_3d_01.bed_temp_c")
                if bed is None or printer_state not in ("IDLE", "COMPLETED"):
                    return "не подтверждены завершение работы и безопасная температура стола"
                if bed is not None and bed >= self._cfg.checks.printer.cool_bed_c:
                    return f"стол ещё горячий ({bed:.0f} °C)"
            return None
        if node == "dome_automation_01" and canonical == "reset_interlock" and value == "IL_SMOKE":
            for det in ("smoke_detector_01", "smoke_detector_02"):
                if (not measurement_usable(st, det) or st.get(f"{det}.smoke_detected") is not False
                        or str(st.get(f"{det}.alarm_state", "")).upper() != "NORMAL"):
                    return f"{det} ещё в тревоге: IL_SMOKE не снимаем"
            return None
        if node == "dome_automation_01" and canonical == "set_data_trust":
            return self._trust_reason(value)
        if node == "dome_automation_01" and canonical == "set_primary_sensor":
            trust = st.get("dome_automation_01.data_trust", {}) or {}
            if str(trust.get(f"{value}.co2_ppm", "")).upper() in ("UNRELIABLE", "FAILED"):
                return f"датчик {value} помечен недостоверным"
            if not measurement_usable(st, str(value)):
                return f"данные {value} устарели, задержаны или недостоверны"
        return None

    def _resume_reason(self, node: str) -> str | None:
        st = self._state
        level = st.get(f"{node}.hold_level")
        reason = st.get(f"{node}.hold_reason")
        if float(st.get(f"{node}.lockout_remaining_s", 0) or 0) > 0 or level == "ALARM":
            return "станок заблокирован (ALARM): команды отклоняются сервером"
        if str(st.get(f"{node}.state", "")).upper() not in ("PAUSED", "ALARM") and not st.get(f"{node}.paused"):
            return "станок не на паузе"
        if not isinstance(st.get(f"{node}.file_name"), str) or not st.get(f"{node}.file_name"):
            return "не установлено активное задание: нужна сверка, resume запрещён"
        if st.get(f"{node}.homed") is not True:
            return "не подтверждены координаты станка после остановки"
        if st.num(f"{node}.progress_pct") is None:
            return "неизвестен прогресс активного задания"
        if reason in ("BED_COLLISION", "FILAMENT_RUNOUT"):
            return f"причина {reason} устраняется Оператором, resume бесполезен"
        if reason is not None:
            if str(reason).startswith("IL_"):
                il = st.get(f"dome_automation_01.interlocks.{reason}.state")
                if il != "ARMED":
                    return f"интерлок {reason} в состоянии {il}: причина не снята"
                if reason == "IL_CO2":
                    from kupol.dome.checks import assess_co2
                    a = assess_co2(st, self._cfg.checks)
                    good = a.good()
                    if len(good) < 2 or max(good.values()) >= self._cfg.checks.co2.recovered_ppm:
                        return "CO2: актуальные безопасные измерения не подтверждены"
            elif reason == "UNDERVOLTAGE":
                v = st.num("smart_panel_01.bus_voltage_v")
                if v is None or v < 200:
                    return "напряжение шины не восстановилось"
            else:
                return f"причина удержания {reason} не распознана: resume не отправляем"
            if self.resume_attempts(node, str(reason)) >= max(1, self._cfg.policy.resume_max_attempts):
                return f"resume по причине {reason} уже пробовали {self.resume_attempts(node, str(reason))} раз"
        if int(st.get(f"{node}.resume_attempts", 0) or 0) >= 2:
            return "сервер уже насчитал 2 попытки resume: третья даст ALARM на 20 минут"
        return None

    def _start_job_reason(self, node: str, value: Any) -> str | None:
        st = self._state
        name = value.get("file_name") if isinstance(value, dict) else value
        if not isinstance(name, str) or not name:
            return "не указано имя файла"
        files = st.get(f"{node}.files", []) or []
        if name not in files:
            return f"файла {name} нет в списке станка"
        if st.get(f"{node}.hold_level") or float(st.get(f"{node}.lockout_remaining_s", 0) or 0) > 0:
            return "станок удержан сервером"
        state = str(st.get(f"{node}.state", "")).upper()
        if state not in ("IDLE", "COMPLETED"):
            return f"станок занят (state = {state})"
        if st.get(f"{node}.homed") is not True:
            return "координаты станка неизвестны: сначала разрешённая процедура home и проверка"
        if node == "printer_3d_01" and st.get("printer_3d_01.bed_clear") is not True:
            return "стол принтера занят деталью (bed_clear != true): будет BED_COLLISION"
        return None

    def _trust_reason(self, value: Any) -> str | None:
        if not isinstance(value, dict):
            return None
        trust = str(value.get("trust", "")).upper()
        node, param = str(value.get("node", "")), str(value.get("param", ""))
        if trust not in ("UNRELIABLE", "FAILED", "TRUSTED"):
            return "trust: UNRELIABLE, FAILED или TRUSTED"
        if param == "*":
            return "помечать целый узел недоверенным нельзя"
        if trust != "TRUSTED" and node in self._cfg.checks.co2.sensors and param in CLIMATE_PARAMS:
            same = [s for s in self._cfg.checks.co2.sensors if s != node]
            data_trust = self._state.get("dome_automation_01.data_trust", {}) or {}
            bad = [s for s in same if str(data_trust.get(f"{s}.{param}", "")).upper() in ("UNRELIABLE", "FAILED")
                   or not measurement_usable(self._state, s) or self._state.num(f"{s}.{param}") is None]
            if len(bad) >= len(same):
                return f"это последний доверенный датчик ({param}): интерлок остался бы без источника"
        return None
