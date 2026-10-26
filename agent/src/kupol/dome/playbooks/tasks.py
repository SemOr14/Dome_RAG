"""Задания Оператора из чата ``meeting_01``: запуск файлов, бокс принтера, ответы на вопросы.

Агент сам запускает печать и фрезеровку (``start_job``). Оператор пишет задания обычным языком:
«Следующее задание: sensor_case.gcode», «Нужен pcb_power_board.nc», «Печать закончилась, заберу
деталь — разблокируй бокс», «Сняла pcb_sensor_hub.nc, стол фрезера свободен». Разбор — правилами;
непонятное уходит в ответ «уточните». Ответы короткие: что сделал и что ждёт от Оператора.
"""

from __future__ import annotations

import re
from typing import Any

from kupol.dome.checks import assess_energy, assess_printer
from kupol.dome.gate import Expect
from kupol.dome.playbooks.base import Ctx, Intent, Playbook, Step
from kupol.dome.playbooks.production import BOX, box_lock, set_lock_step, unlock_blockers

FILE_RE = re.compile(r"[\w\-]+\.(?:gcode|nc)", re.IGNORECASE)
TAKEN_RE = re.compile(r"\bсня(?:л|ла|ли)\b|\bзабра(?:л|ла|ли)\b|стол\b.{0,30}свободен|дверь закры", re.IGNORECASE)
UNLOCK_RE = re.compile(r"разблокир|открой\w*\s+бокс|открыть\s+бокс|иду за деталью|заберу деталь|забрать деталь|открой\w* замок", re.IGNORECASE)
CONFIRM_RE = re.compile(r"закреп|заготовк\w*\s+(?:на месте|готов)|можно запуска|запускай|давай запуск", re.IGNORECASE)
STATUS_RE = re.compile(r"что (?:по|с) систем|что случилось|что происходит|как дела|статус|что у нас|что с ", re.IGNORECASE)
MACHINE_BY_EXT = {"gcode": "printer_3d_01", "nc": "cnc_01"}
TITLE = {"printer_3d_01": "принтер", "cnc_01": "фрезер"}
FREE_STATES = {"printer_3d_01": ("IDLE", "COMPLETED"), "cnc_01": ("IDLE",)}
MIN_RUN_S = 30.0  # раньше этого после старта «завершение» не засчитываем


def operator_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Сообщения от Оператора (не от команд и не от нас)."""
    return [m for m in messages if (m.get("sender") or {}).get("role") == "operator"]


def unanswered_operator_messages(recent: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Сообщения Оператора без нашего ответа после них (по последним сообщениям чата).

    Нужны после перезапуска агента: работа, которую просил Оператор, не должна потеряться.
    """
    out: list[dict[str, Any]] = []
    for m in sorted(recent, key=lambda x: int(x.get("id", 0))):
        role = (m.get("sender") or {}).get("role")
        if role == "operator":
            out.append(m)
        elif role == "team":
            out.clear()  # мы ответили позже — предыдущее обработано
    return out


def find_file(st: Any, machine: str, name: str) -> str | None:
    """Имя файла из списка станка (точное, затем без учёта регистра)."""
    files = [str(f) for f in (st.get(f"{machine}.files", []) or [])]
    if name in files:
        return name
    for f in files:
        if f.lower() == name.lower():
            return f
    return None


class TasksPlaybook(Playbook):
    """Разбор чата Оператора и ведение очереди заданий."""

    name = "tasks"

    def step(self, ctx: Ctx) -> list[Intent]:
        out: list[Intent] = []
        jobs: list[dict[str, Any]] = ctx.mem.setdefault("jobs", [])
        for msg in operator_messages(ctx.chat_new):
            out += self._handle(ctx, msg, jobs)
        out += self._advance(ctx, jobs)
        ctx.mem["jobs"] = [j for j in jobs if j["state"] not in ("done", "failed", "dropped")][-8:]
        return out

    # ------------------------------------------------------------------ разбор сообщения

    def _handle(self, ctx: Ctx, msg: dict[str, Any], jobs: list[dict[str, Any]]) -> list[Intent]:
        text = str(msg.get("text", "")).strip()
        mid = msg.get("id")
        base = dict(situation=f"Оператор написал: «{text[:160]}»", source="rules", priority=1, cooldown_s=0.0)
        files = FILE_RE.findall(text)
        confirm = bool(CONFIRM_RE.search(text))

        if TAKEN_RE.search(text) and not UNLOCK_RE.search(text):
            return self._taken(ctx, text, mid, files)
        if UNLOCK_RE.search(text):
            return self._unlock(ctx, mid, text)
        if files:
            return self._new_job(ctx, jobs, files[0], confirm, mid, text)
        if confirm:
            ready = [j for j in jobs if j["state"] == "waiting_confirm"]
            for j in ready:
                j["state"] = "queued"
                j["confirmed"] = True
            if ready:
                return [Intent(key=f"chat:{mid}", cause="Оператор подтвердил, что заготовка готова", confidence=0.9, evidence=[{"detail": text[:120]}], plan=["запустить отложенное задание"], **base)]  # type: ignore[arg-type]
        if STATUS_RE.search(text):
            return [Intent(key=f"chat:{mid}", cause="вопрос Оператора о состоянии", confidence=0.9, evidence=[], plan=["коротко ответить по данным"], say=self.status_text(ctx), **base)]  # type: ignore[arg-type]
        if len(text) >= 8 and "?" in text:
            fallback = Intent(key=f"chat:{mid}", cause="вопрос, который правила не разбирают", confidence=0.4, evidence=[], plan=["ответить по данным (модель) или попросить уточнить"], **base)  # type: ignore[arg-type]
            fallback.say = "Не понял запрос. Напишите имя файла или что именно сделать."
            fallback.llm_question = text
            return [fallback]
        return []

    def _taken(self, ctx: Ctx, text: str, mid: Any, files: list[str]) -> list[Intent]:
        st = ctx.state
        for f in files:  # «Сняла X.nc» — завершение фрезеровки подтверждено Оператором
            for j in ctx.mem.get("jobs", []):
                if j["file"].lower() == f.lower() and j["state"] in ("started", "done"):
                    j["state"] = "done"
        door = str(st.get(f"{BOX}.doors[0]", "")).upper()
        if box_lock(st) == "UNLOCKED" and door == "CLOSED" and not files:
            return [
                Intent(
                    key=f"chat:{mid}",
                    situation=f"Оператор написал: «{text[:160]}»",
                    cause="деталь снята, дверь бокса закрыта",
                    confidence=0.9,
                    evidence=[{"node": BOX, "param": "doors[0]", "value": door}],
                    plan=["заблокировать замок бокса"],
                    steps=[set_lock_step(True, "Оператор забрал деталь")],
                    priority=3,
                    cooldown_s=0.0,
                )
            ]
        return []

    def _unlock(self, ctx: Ctx, mid: Any, text: str) -> list[Intent]:
        st = ctx.state
        p = assess_printer(st)
        blocker = unlock_blockers(st, p, ctx.cfg.checks.printer.cool_bed_c)
        common = dict(situation=f"Оператор просит открыть бокс: «{text[:160]}»", priority=1, cooldown_s=0.0, source="rules")
        ev: list[dict[str, Any]] = [{"node": "printer_3d_01", "param": "state", "value": p.state}, {"node": "printer_3d_01", "param": "progress_pct", "value": p.progress}, {"node": "printer_3d_01", "param": "bed_temp_c", "value": p.bed_temp}]
        if blocker is not None:
            return [Intent(key=f"chat:{mid}", cause="бокс сейчас открывать нельзя", confidence=0.95, evidence=ev, plan=["не открывать", "объяснить Оператору"], say=f"Бокс пока не открываю: {blocker}. Сообщу, когда можно.", **common)]  # type: ignore[arg-type]
        if box_lock(st) == "UNLOCKED":
            return [Intent(key=f"chat:{mid}", cause="бокс уже разблокирован", confidence=0.95, evidence=ev, plan=["сообщить"], say="Бокс уже разблокирован.", **common)]  # type: ignore[arg-type]
        return [
            Intent(
                key=f"chat:{mid}",
                cause="печать завершена и стол остыл — открыть можно",
                confidence=0.95,
                evidence=ev,
                plan=["разблокировать замок бокса"],
                steps=[set_lock_step(False, "просьба Оператора, печать завершена")],
                say="Бокс разблокирован (замок 0), стол остыл.",
                **common,  # type: ignore[arg-type]
            )
        ]

    def _new_job(self, ctx: Ctx, jobs: list[dict[str, Any]], raw: str, confirm: bool, mid: Any, text: str) -> list[Intent]:
        st = ctx.state
        ext = raw.rsplit(".", 1)[-1].lower()
        machine = MACHINE_BY_EXT[ext]
        title = TITLE[machine]
        name = find_file(st, machine, raw)
        common = dict(situation=f"Оператор просит запустить «{raw}»: «{text[:160]}»", priority=1, cooldown_s=0.0, source="rules")
        if name is None:
            files = ", ".join(str(f) for f in (st.get(f"{machine}.files", []) or [])[:6]) or "список пуст"
            return [Intent(key=f"chat:{mid}", cause=f"файла нет в списке {title}а", confidence=0.95, evidence=[{"node": machine, "param": "files", "value": files}], plan=["не запускать", "сообщить доступные файлы"], say=f"Файла {raw} нет на {title}е. Доступны: {files}.", **common)]  # type: ignore[arg-type]
        if any(j["file"] == name and j["state"] in ("queued", "waiting_confirm", "starting", "started") for j in jobs):
            return []
        job = {"file": name, "machine": machine, "state": "queued", "msg": mid, "confirmed": confirm, "at": ctx.now}
        jobs.append(job)
        # Оператор попросил явно: станок свободен — запускаем сразу. Подтверждение «заготовка закреплена»
        # нужно только когда фрезер был занят и задание ждёт освобождения.
        free, why = self._free(ctx, machine)
        if not free:
            job["state"] = "waiting_confirm" if machine == "cnc_01" else "queued"
            tail = " Запущу, когда освободится и напишете, что заготовка закреплена." if machine == "cnc_01" else " Запущу сразу, как станок освободится."
            return [Intent(key=f"chat:{mid}", cause=f"{title} сейчас не может принять задание: {why}", confidence=0.95, evidence=[{"node": machine, "param": "state", "value": st.get(f"{machine}.state")}], plan=["поставить в очередь", "сообщить Оператору"], say=f"{title.capitalize()} занят ({why}).{tail}", **common)]  # type: ignore[arg-type]
        return []  # запуск сделает _advance в этом же такте

    # ------------------------------------------------------------------ очередь и завершение

    @staticmethod
    def _free(ctx: Ctx, machine: str) -> tuple[bool, str]:
        st = ctx.state
        state = str(st.get(f"{machine}.state", "")).upper()
        if st.get(f"{machine}.hold_level") or float(st.get(f"{machine}.lockout_remaining_s", 0) or 0) > 0:
            return False, f"удержан сервером ({st.get(f'{machine}.hold_reason')})"
        if state not in FREE_STATES[machine]:
            prog = st.num(f"{machine}.progress_pct")
            return False, f"{state.lower()}" + (f", {prog:.0f} %" if prog is not None else "")
        if machine == "printer_3d_01" and st.get("printer_3d_01.bed_clear") is not True:
            return False, "на столе деталь — заберите её (бокс разблокирую)"
        return True, ""

    def _advance(self, ctx: Ctx, jobs: list[dict[str, Any]]) -> list[Intent]:
        out: list[Intent] = []
        st = ctx.state
        for job in jobs:
            machine, name, title = job["machine"], job["file"], TITLE[job["machine"]]
            if job["state"] == "queued":
                free, _why = self._free(ctx, machine)
                if not free or ctx.gate.is_pending(machine):
                    continue
                if st.get(f"{machine}.homed") is False:
                    out.append(
                        Intent(
                            key=f"job:home:{machine}",
                            situation=f"перед запуском {name} {title} не отъюстирован (homed = false)",
                            cause="нужен home перед стартом",
                            confidence=0.85,
                            evidence=[{"node": machine, "param": "homed", "value": False}],
                            plan=["home", f"затем запустить {name}"],
                            steps=[Step(machine, "home", None, Expect.equals(f"{machine}.homed", True, timeout_s=90.0), "перед запуском задания")],
                            priority=1,
                            cooldown_s=120.0,
                        )
                    )
                    continue
                job["state"] = "starting"
                job["started_at"] = ctx.now
                eta = st.num(f"{machine}.estimated_time_s")
                say = f"Запустил {name} на {title}е, слежу." if machine == "cnc_01" else f"Запустил {name} на принтере."
                out.append(
                    Intent(
                        key=f"job:start:{machine}:{name}",
                        situation=f"запуск задания Оператора: {name} на {title}е (станок свободен, удержаний нет)" + (f", оценка {eta / 60:.0f} мин" if eta else ""),
                        cause="задание от Оператора",
                        confidence=0.95,
                        evidence=[{"node": machine, "param": "state", "value": st.get(f"{machine}.state")}, {"node": machine, "param": "files", "value": name}]
                        + ([{"node": machine, "param": "bed_clear", "value": st.get(f"{machine}.bed_clear")}] if machine == "printer_3d_01" else []),
                        plan=[f"start_job {name}", "проверить, что станок перешёл в работу"],
                        steps=[Step(machine, "start_job", name, self._running(machine, name), "задание Оператора")],
                        say=say,
                        priority=1,
                        cooldown_s=60.0,
                        rag_query=f"запуск {name} {title}",
                    )
                )
            elif job["state"] in ("starting", "started"):
                state = str(st.get(f"{machine}.state", "")).upper()
                running = state in ("RUNNING", "PRINTING", "HOMING", "BUSY") and str(st.get(f"{machine}.file_name", "")) == name
                if running:
                    job["state"] = "started"
                    job["seen_running"] = True
                    continue
                age = ctx.now - float(job.get("started_at", ctx.now))
                if job["state"] == "starting" and age > 90 and not job.get("seen_running"):
                    job["state"] = "failed"
                    out.append(Intent(key=f"job:failed:{machine}:{name}", situation=f"{name}: станок не перешёл в работу за {age:.0f} с", cause="запуск не подтвердился", confidence=0.8, evidence=[{"node": machine, "param": "state", "value": state}], plan=["не повторять вслепую", "сообщить Оператору"], say=f"{name}: {title} не начал работу после команды (состояние {state}). Повторно не отправляю — проверьте станок.", priority=1, cooldown_s=0.0))
                    continue
                if job.get("seen_running") and age > MIN_RUN_S and self._finished(ctx, machine, name):
                    job["state"] = "done"
                    if machine == "cnc_01":
                        out.append(Intent(key=f"job:done:{machine}:{name}", situation=f"фрезеровка {name} завершена", cause="задание выполнено", confidence=0.9, evidence=[{"node": machine, "param": "state", "value": state}, {"node": machine, "param": "progress_pct", "value": st.num(f"{machine}.progress_pct")}], plan=["сообщить Оператору"], say=f"Фрезеровка {name} завершена — можно снимать заготовку.", priority=3, cooldown_s=0.0))
        return out

    @staticmethod
    def _running(machine: str, name: str) -> Expect:
        def ok(st: Any) -> bool:
            return str(st.get(f"{machine}.state", "")).upper() in ("RUNNING", "PRINTING", "HOMING", "BUSY") and str(st.get(f"{machine}.file_name", "")) == name

        return Expect.custom(f"{machine} работает над {name}", ok, [f"{machine}.state", f"{machine}.file_name", f"{machine}.progress_pct"], timeout_s=60.0)

    @staticmethod
    def _finished(ctx: Ctx, machine: str, name: str) -> bool:
        st = ctx.state
        if machine == "printer_3d_01":
            return assess_printer(st).done
        state = str(st.get(f"{machine}.state", "")).upper()
        prog = st.num(f"{machine}.progress_pct")
        return state == "IDLE" and prog is not None and prog >= 99.0

    # ------------------------------------------------------------------ ответ о состоянии

    @staticmethod
    def status_text(ctx: Ctx) -> str:
        st = ctx.state
        e = assess_energy(st, ctx.cfg.checks)
        parts = []
        if e.soc is not None:
            parts.append(f"АКБ {e.soc:.0f} %, инвертор {e.mode or '?'}")
        for node in ("printer_3d_01", "cnc_01"):
            state = str(st.get(f"{node}.state", "?")).upper()
            reason = st.get(f"{node}.hold_reason")
            fname = st.get(f"{node}.file_name")
            prog = st.num(f"{node}.progress_pct")
            if state in ("PRINTING", "RUNNING") and fname:
                parts.append(f"{TITLE[node]}: {fname} {prog:.0f} %" if prog is not None else f"{TITLE[node]}: {fname}")
            elif reason:
                parts.append(f"{TITLE[node]} на паузе ({reason})")
            else:
                parts.append(f"{TITLE[node]} свободен" if state in ("IDLE", "COMPLETED") else f"{TITLE[node]}: {state.lower()}")
        issues = [str(i) for i in ctx.mem.get("issues", [])][:2]
        return ". ".join(parts) + ("." if parts else "") + (" Сейчас разбираюсь: " + "; ".join(issues) + "." if issues else "")
