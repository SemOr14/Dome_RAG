"""Принтер и бокс: не верим ``webhook_state``, бокс открываем только когда печать действительно завершена и стол остыл.

Журнал 14.10: агент поверил ``webhook_state = COMPLETED`` и разблокировал бокс при идущей печати (33 %).
Правило: завершением считается только согласие ``state``, ``real_state``, прогресса и остатка времени.
"""

from __future__ import annotations

from typing import Any

from kupol.dome.checks import PrinterAssessment, assess_printer
from kupol.dome.gate import Expect
from kupol.dome.playbooks.base import Ctx, Intent, Playbook, Step

BOX = "thermal_insulation_01"


def box_lock(st: Any, index: int = 0) -> str:
    """Состояние замка бокса (``LOCKED`` / ``UNLOCKED`` / пусто)."""
    return str(st.get(f"{BOX}.locks[{index}]", "")).upper()


def unlock_blockers(st: Any, p: PrinterAssessment, cool_c: float) -> str | None:
    """Почему бокс сейчас открывать нельзя (``None`` — можно)."""
    if p.printing or p.webhook_lies:
        left = st.num("printer_3d_01.print_time_left_s")
        tail = f", осталось ≈{left / 60:.0f} мин" if left else ""
        return f"печать ещё идёт ({p.progress or 0:.0f} %{tail})"
    if p.state in ("PAUSED", "ERROR") or p.held:
        return f"принтер в состоянии {p.state}"
    if p.bed_temp is not None and p.bed_temp >= cool_c:
        return f"стол ещё горячий ({p.bed_temp:.0f} °C)"
    return None


def set_lock_step(locked: bool, reason: str) -> Step:
    return Step(
        BOX,
        "set_lock",
        {"lock": 0, "locked": locked},
        Expect.equals(f"{BOX}.locks[0]", "LOCKED" if locked else "UNLOCKED"),
        reason,
    )


class PrinterPlaybook(Playbook):
    """Завершение печати, ложный ``webhook_state``, разблокировка и повторная блокировка бокса."""

    name = "printer"

    def step(self, ctx: Ctx) -> list[Intent]:
        st = ctx.state
        if not st.has("printer_3d_01"):
            return []
        p = assess_printer(st)
        out: list[Intent] = []
        mem = ctx.mem
        name = str(st.get("printer_3d_01.file_name", ""))
        lock = box_lock(st)

        if p.webhook_lies:
            out.append(
                Intent(
                    key="printer:webhook_lie",
                    situation=f"webhook_state = COMPLETED, но state = {p.state}, real_state = {p.real_state}, прогресс {p.progress or 0:.0f} %",
                    cause="webhook врёт при длинной печати; завершением не считаем",
                    confidence=0.95,
                    evidence=[
                        {"node": "printer_3d_01", "param": "webhook_state", "value": p.webhook_state},
                        {"node": "printer_3d_01", "param": "state", "value": p.state},
                        {"node": "printer_3d_01", "param": "progress_pct", "value": p.progress},
                    ],
                    plan=["бокс не открывать", "ориентироваться на state, progress и z_mm"],
                    priority=3,
                    cooldown_s=900.0,
                    rag_query="webhook_state COMPLETED врёт принтер печатает",
                )
            )

        if p.printing:
            mem["printing_file"] = name
            if lock == "UNLOCKED":
                out.append(
                    Intent(
                        key="printer:relock_while_printing",
                        situation=f"бокс разблокирован, а принтер печатает {name}",
                        cause="замок надо закрыть: открывать бокс во время печати нельзя",
                        confidence=0.95,
                        evidence=[{"node": BOX, "param": "locks[0]", "value": lock}],
                        plan=["заблокировать замок 0"],
                        steps=[set_lock_step(True, "печать идёт")],
                        priority=2,
                        cooldown_s=120.0,
                    )
                )
            return out

        finished_key = f"{name}"
        if p.done and mem.get("announced") != finished_key and mem.get("printing_file") == name:
            blocker = unlock_blockers(st, p, ctx.cfg.checks.printer.cool_bed_c)
            if blocker is None:
                mem["announced"] = finished_key
                out.append(
                    Intent(
                        key=f"printer:done:{name}",
                        situation=f"печать {name} завершена (state, real_state и прогресс согласны), стол остыл",
                        cause="печать закончена",
                        confidence=0.95,
                        evidence=[{"node": "printer_3d_01", "param": "state", "value": p.state}, {"node": "printer_3d_01", "param": "bed_temp_c", "value": p.bed_temp}],
                        plan=["разблокировать бокс", "сообщить Оператору"],
                        steps=[set_lock_step(False, "печать завершена, стол остыл")],
                        say=f"Печать {name} готова, стол остыл ({p.bed_temp:.0f} °C). Бокс разблокирован — можно забирать деталь." if p.bed_temp is not None else f"Печать {name} готова. Бокс разблокирован — можно забирать деталь.",
                        priority=3,
                    )
                )
            elif mem.get("cooling_noted") != finished_key:
                mem["cooling_noted"] = finished_key
                out.append(
                    Intent(
                        key=f"printer:cooling:{name}",
                        situation=f"печать {name} завершена, но {blocker}",
                        cause="ждём остывания стола",
                        confidence=0.9,
                        evidence=[{"node": "printer_3d_01", "param": "bed_temp_c", "value": p.bed_temp}],
                        plan=["разблокировать бокс, когда стол остынет"],
                        priority=4,
                    )
                )

        # повторная блокировка: деталь снята (bed_clear), дверь закрыта
        door = str(st.get(f"{BOX}.doors[0]", "")).upper()
        if lock == "UNLOCKED" and door == "CLOSED" and p.bed_clear is True and mem.get("announced"):
            out.append(
                Intent(
                    key="printer:relock",
                    situation="деталь снята (bed_clear), дверь бокса закрыта, замок открыт",
                    cause="работа с боксом закончена",
                    confidence=0.9,
                    evidence=[{"node": "printer_3d_01", "param": "bed_clear", "value": True}, {"node": BOX, "param": "doors[0]", "value": door}],
                    plan=["заблокировать замок 0"],
                    steps=[set_lock_step(True, "деталь снята, дверь закрыта")],
                    priority=4,
                    cooldown_s=120.0,
                )
            )
            mem.pop("announced", None)
        return out
