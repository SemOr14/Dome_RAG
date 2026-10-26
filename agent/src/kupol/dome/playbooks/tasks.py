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
    raw = st.get(f"{machine}.files", []) or []
    files = [f if isinstance(f, str) else f.get("name") for f in raw if isinstance(f, (str, dict))]
    files = [f for f in files if isinstance(f, str)]
    if name in files:
        return name
    for f in files:
        if f.lower() == name.lower():
            return f
    return None


class TasksPlaybook(Playbook):
    """Одна явная команда — один немедленный запуск; сохраняется только наблюдение начатого."""

    name = "tasks"

    def step(self, ctx: Ctx) -> list[Intent]:
        active = ctx.mem.setdefault("active_jobs", {})
        # Migrate observation of already-started jobs; queued work is never replayed.
        for job in ctx.mem.pop("jobs", []):
            if isinstance(job, dict) and job.get("state") in ("starting", "started") and job.get("machine") in TITLE:
                active.setdefault(job["machine"], job)
        jobs = list(active.values())
        out = self._observe_active(ctx, jobs)
        claimed = set()
        for msg in operator_messages(ctx.chat_new):
            text = str(msg.get("text", ""))
            files = FILE_RE.findall(text)
            known = bool(files or CONFIRM_RE.search(text) or STATUS_RE.search(text) or TAKEN_RE.search(text) or UNLOCK_RE.search(text))
            if known and isinstance(msg.get("id"), int):
                ctx.handled_message_ids.add(msg["id"])
            machine = MACHINE_BY_EXT.get(files[0].rsplit(".", 1)[-1].lower()) if len(files) == 1 else None
            if machine in claimed and not TAKEN_RE.search(text) and not UNLOCK_RE.search(text):
                out.append(Intent(key=f"chat:{msg.get('id')}", situation="Повторный запуск в одном кадре",
                    cause="для станка уже выбран один запуск", confidence=1, plan=["не создавать очередь"],
                    say="Для этого станка уже обрабатывается один запуск. Повторите запрос после проверки его результата.", cooldown_s=0))
                continue
            fresh = self._handle(ctx, msg, jobs)
            claimed.update(step.node for intent in fresh for step in intent.steps if step.action == "start_job")
            out += fresh
        ctx.mem["active_jobs"] = {j["machine"]: j for j in jobs if j.get("state") in ("starting", "started")}
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
        if len(files) > 1:
            return [Intent(key=f"chat:{mid}", cause="несколько файлов в одной команде", confidence=1.0,
                evidence=[], plan=["не запускать и не создавать очередь"],
                say="Укажите один файл для отдельного запуска. Очередь файлов не поддерживается.", **base)]
        if files:
            return self._new_job(ctx, jobs, files[0], confirm, mid, text)
        if confirm:
            return [Intent(key=f"chat:{mid}", cause="нет отложенного задания", confidence=1.0, evidence=[],
                plan=["попросить один файл для отдельного запуска"],
                say="Отложенных заданий нет. Для запуска укажите одно имя серверного файла новой командой.", **base)]
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
            for j in ctx.mem.get("active_jobs", {}).values():
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
        free, why = self._free(ctx, machine)
        if ctx.gate.is_pending(machine):
            free, why = False, "ожидается проверка предыдущей команды"
        if st.get(f"{machine}.homed") is not True:
            free, why = False, "координаты не подтверждены: сначала home и проверка"
        if not free:
            return [Intent(key=f"chat:{mid}", cause=f"{title} сейчас не может принять задание: {why}",
                confidence=0.95, evidence=[{"node": machine, "param": "state", "value": st.get(f"{machine}.state")}],
                plan=["не сохранять запрос на будущий запуск"],
                say=f"{title.capitalize()} занят или не готов ({why}). Задание не поставлено в очередь; после устранения причины повторите команду с именем файла.", **common)]
        jobs[:] = [j for j in jobs if j.get("machine") != machine]
        jobs.append({"file": name, "machine": machine, "state": "starting", "msg": mid, "started_at": ctx.now})
        return [Intent(key=f"job:start:{machine}:{name}:{mid}",
            situation=f"Одиночный запуск {name} на {title}е", cause="новая явная команда Оператора",
            confidence=0.95, evidence=[{"node": machine, "param": "state", "value": st.get(f"{machine}.state")}],
            plan=[f"start_job {name}", "проверить фактический запуск"],
            steps=[Step(machine, "start_job", name, self._running(machine, name), "задание Оператора")],
            say=f"Запустил {name} на принтере." if machine == "printer_3d_01" else f"Запустил {name} на фрезере, слежу.",
            priority=1, cooldown_s=0, rag_query=f"запуск {name} {title}")]

    # ------------------------------------------------------------------ наблюдение начатого задания

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

    def _observe_active(self, ctx: Ctx, jobs: list[dict[str, Any]]) -> list[Intent]:
        out: list[Intent] = []
        st = ctx.state
        for job in jobs:
            machine, name, title = job["machine"], job["file"], TITLE[job["machine"]]
            if job["state"] in ("starting", "started"):
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
