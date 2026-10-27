"""Станки и дым: продолжение работы после остановки автоматикой и реакция на тревогу ``IL_SMOKE``.

``resume`` отправляется один раз, когда причина удержания действительно ушла (интерлок снова
``ARMED``, значения в норме). Три пустые попытки блокируют станок на 20 минут — этого допустить нельзя.
Дым трактуется консервативно: «неуверенно = реально». Квитирование (оно отменяет автопуск
пожаротушения) — только при нескольких независимых свидетельствах ложной тревоги.
"""

from __future__ import annotations

import re
from typing import Any

from kupol.dome.checks import assess_co2, assess_condensate, assess_smoke, is_untrusted
from kupol.dome.gate import Expect
from kupol.dome.playbooks.base import Ctx, Intent, Playbook, Step, interlock_pending_s, interlock_state

MACHINES = ("printer_3d_01", "cnc_01")
TITLES = {"printer_3d_01": "принтер", "cnc_01": "фрезер"}
SETTLE_S = 6.0  # причина должна оставаться снятой столько секунд, прежде чем продолжать станок
FALSE_ALARM = re.compile(r"ложн|дыма нет|нет дыма|не горит|всё норм|все норм", re.IGNORECASE)


def working(node: str) -> Expect:
    """Ожидание: удержание снято и станок работает."""

    def ok(st: Any) -> bool:
        return st.get(f"{node}.hold_reason") is None and str(st.get(f"{node}.state", "")).upper() in ("RUNNING", "PRINTING")

    return Expect.custom(f"{node} работает без удержания", ok, [f"{node}.state", f"{node}.hold_reason", f"{node}.resume_attempts"])


def cause_cleared(ctx: Ctx, reason: str) -> tuple[bool, str]:
    """Ушла ли причина удержания: ``(да/нет, пояснение)``."""
    st, cfg = ctx.state, ctx.cfg.checks
    if reason.startswith("IL_"):
        if interlock_state(st, reason) != "ARMED" or interlock_pending_s(st, reason) > 0:
            return False, f"{reason}: интерлок ещё не взведён заново"
    if reason == "IL_CO2":
        live = {s: v for s, v in assess_co2(st, cfg).values.items() if not is_untrusted(st, s, "co2_ppm")}
        if len(live) < 2:
            return False, "нет двух актуальных доверенных датчиков CO₂"
        if max(live.values()) >= cfg.co2.recovered_ppm:
            return False, f"CO₂ ещё высокий ({max(live.values()):.0f} ppm)"
        return True, "CO₂ в норме на доверенных датчиках"
    if reason == "IL_CONDENSATE":
        a = assess_condensate(st, cfg)
        if len(a.rh) < 2 or a.kind == "unknown":
            return False, "нет актуального подтверждения безопасной влажности"
        hot = [s for s in a.rh if a.trigger(s, cfg.condensate.rh_interlock_pct, cfg.condensate.dew_gap_c) and not is_untrusted(st, s, "humidity_pct")]
        return (not hot, "влажность в норме на доверенных датчиках" if not hot else f"влажность высокая: {', '.join(hot)}")
    if reason == "IL_SMOKE":
        since = ctx.mem.get("smoke_clear_since")
        if since is None or ctx.now - float(since) < 120:
            return False, "после тревоги дыма ещё не прошло 2 минуты тишины"
        return True, "дым не фиксируется 2+ минуты"
    if reason == "UNDERVOLTAGE":
        v = st.num("smart_panel_01.bus_voltage_v")
        return (v is not None and v >= 200, f"напряжение шины {v}")
    if reason.startswith("IL_"):
        return True, f"интерлок {reason} взведён заново"
    return False, f"причина {reason} не распознана"


class ResumePlaybook(Playbook):
    """Продолжить станки после остановки автоматикой, когда причина устранена; сообщить о блокировках."""

    name = "resume"

    def step(self, ctx: Ctx) -> list[Intent]:
        st = ctx.state
        out: list[Intent] = []
        self._track_smoke(ctx)
        clear = ctx.mem.setdefault("clear_since", {})
        for node in MACHINES:
            reason = st.get(f"{node}.hold_reason")
            if not st.has(node) or not reason:
                clear.pop(node, None)
                continue
            title = TITLES[node]
            lock = float(st.get(f"{node}.lockout_remaining_s", 0) or 0)
            if lock > 0 or st.get(f"{node}.hold_level") == "ALARM":
                out.append(
                    Intent(
                        key=f"machine:alarm:{node}",
                        situation=f"{title}: ALARM, причина {reason}, блокировка ещё {lock:.0f} с",
                        cause=f"блокировка сервера после остановки ({reason})",
                        confidence=0.95,
                        evidence=[{"node": node, "param": "hold_reason", "value": reason}, {"node": node, "param": "lockout_remaining_s", "value": lock}],
                        plan=["команды станку отклоняются до конца блокировки", "не отправлять resume"],
                        say=f"{title.capitalize()} заблокирован сервером (ALARM, {reason}) ещё на {lock / 60:.0f} мин. Команды сейчас не принимаются.",
                        priority=1,
                        cooldown_s=1800.0,
                    )
                )
                continue
            if reason in ("BED_COLLISION", "FILAMENT_RUNOUT"):
                text = "замените катушку филамента" if reason == "FILAMENT_RUNOUT" else "стол занят деталью, нужен осмотр"
                out.append(
                    Intent(
                        key=f"machine:operator:{node}:{reason}",
                        situation=f"{title}: пауза {reason}",
                        cause="причину снимает только Оператор",
                        confidence=0.95,
                        evidence=[{"node": node, "param": "hold_reason", "value": reason}],
                        plan=["resume не отправлять", "попросить Оператора"],
                        say=f"{title.capitalize()} встал ({reason}): {text}. После этого скажите — продолжу.",
                        priority=1,
                        cooldown_s=1800.0,
                    )
                )
                continue
            cleared, why = cause_cleared(ctx, str(reason))
            if not cleared:
                clear.pop(node, None)
                continue
            since = clear.setdefault(node, ctx.now)
            if ctx.now - since < SETTLE_S or ctx.gate.is_pending(node, "resume"):
                continue
            tries = ctx.gate.resume_attempts(node, str(reason))
            if tries >= max(1, ctx.cfg.policy.resume_max_attempts):
                continue
            out.append(
                Intent(
                    key=f"resume:{node}:{reason}",
                    situation=f"{title} на паузе ({reason}); {why}",
                    cause="причина остановки устранена",
                    confidence=0.9,
                    evidence=[{"node": node, "param": "hold_reason", "value": reason}, {"detail": why}],
                    plan=[f"один resume {title}у", "проверить, что станок работает"],
                    steps=[Step(node, "resume", None, working(node), f"resume после {reason}")],
                    priority=1,
                    cooldown_s=30.0,
                    rag_query=f"resume станок после {reason}",
                )
            )
        return out

    @staticmethod
    def _track_smoke(ctx: Ctx) -> None:
        a = assess_smoke(ctx.state, ctx.cfg.checks, "smoke_detector_01")
        b = assess_smoke(ctx.state, ctx.cfg.checks, "smoke_detector_02")
        if a.alarm or b.alarm:
            ctx.mem.pop("smoke_clear_since", None)
        else:
            ctx.mem.setdefault("smoke_clear_since", ctx.now)


class SmokePlaybook(Playbook):
    """Тревога дыма: по умолчанию — реальная; квитирование только при убедительных свидетельствах ложной."""

    name = "smoke"

    def step(self, ctx: Ctx) -> list[Intent]:
        st = ctx.state
        out: list[Intent] = []
        for det, zone in (("smoke_detector_01", "FABLAB"), ("smoke_detector_02", "STORAGE")):
            a = assess_smoke(st, ctx.cfg.checks, det)
            if not a.alarm:
                ctx.mem.pop(f"since:{det}", None)
                continue
            since = ctx.mem.setdefault(f"since:{det}", ctx.now)
            told_false = any(FALSE_ALARM.search(t) for t in ctx.operator_texts(since))
            kind = "false" if (a.kind == "false" or (told_false and a.real_sources == 0)) else ("real" if a.kind == "real" else "unknown")
            evidence = [e.as_log() for e in a.evidence]
            if told_false:
                evidence.append({"supports": "false", "weight": 0.9, "detail": "Оператор сообщил, что тревога ложная"})
            if kind == "false":
                event = self._unacked_event(st)
                steps: list[Step] = []
                if event:
                    steps.append(Step("dome_automation_01", "acknowledge", {"event_id": event}, self._acked_expect(event), "отмена автопуска пожаротушения"))
                steps.append(Step(det, "reset_alarm", None, Expect.equals(f"{det}.alarm_state", "NORMAL"), "сброс тревоги датчика"))
                ctx.mem["false_confirmed_at"] = ctx.now
                out.append(
                    Intent(
                        key=f"smoke:false:{det}",
                        situation=f"{det} ({zone}): тревога дыма, но свидетельства ложности: " + "; ".join(e.detail for e in a.evidence if e.supports == "false"),
                        cause="ложная тревога датчика дыма",
                        confidence=0.85,
                        evidence=evidence,
                        plan=["квитировать событие (отменяет пожаротушение)", "сбросить тревогу датчика", "когда датчик успокоится — снять IL_SMOKE, продолжить станки"],
                        steps=steps,
                        say=f"Тревога дыма в {zone} похожа на ложную (CO, VOC, задымлённость в норме) — квитировал, пожаротушение не запускается. Если вы видите дым, напишите.",
                        priority=1,
                        cooldown_s=45.0,
                        rag_query="ложная пожарная тревога датчик дыма CO VOC",
                    )
                )
            else:
                out.append(
                    Intent(
                        key=f"smoke:{kind}:{det}",
                        situation=f"{det} ({zone}): тревога дыма; признаков ложности недостаточно ({a.false_sources} из {a.needs})",
                        cause="возможен реальный дым" if kind == "real" else "причина не установлена",
                        confidence=0.7 if kind == "real" else 0.5,
                        evidence=evidence,
                        plan=["автоматику не блокировать, не квитировать", "сообщить Оператору"],
                        say=f"Тревога дыма в {zone}. Подтверждений ложности нет — ничего не блокирую, пожаротушение сработает штатно. Проверьте визуально; если дыма нет, напишите «ложная тревога».",
                        priority=1,
                        cooldown_s=300.0,
                    )
                )
        quiet = not any(assess_smoke(st, ctx.cfg.checks, d).alarm for d in ("smoke_detector_01", "smoke_detector_02"))
        confirmed = ctx.mem.get("false_confirmed_at")
        if quiet and confirmed is not None and ctx.now - float(confirmed) < 600 and interlock_state(st, "IL_SMOKE") == "TRIPPED":
            out.append(
                Intent(
                    key="smoke:reset_interlock",
                    situation="тревога дыма подтверждена как ложная и снята, IL_SMOKE ещё сработал",
                    cause="ложная тревога дыма прошла",
                    confidence=0.85,
                    evidence=[{"detail": "ложность подтверждена ранее, датчики спокойны"}],
                    plan=["снять IL_SMOKE (вернуть линию 1), затем продолжить станки"],
                    steps=[Step("dome_automation_01", "reset_interlock", "IL_SMOKE", Expect.equals("dome_automation_01.interlocks.IL_SMOKE.state", "ARMED"), "снятие IL_SMOKE")],
                    priority=1,
                )
            )
        return out

    @staticmethod
    def _unacked_event(st: Any) -> str | None:
        events = st.get("dome_automation_01.events", []) or []
        for ev in reversed(events):
            if isinstance(ev, dict) and ev.get("interlock") == "IL_SMOKE" and ev.get("type") == "TRIP" and not ev.get("acknowledged"):
                return str(ev.get("event_id"))
        return None

    @classmethod
    def _acked_expect(cls, event_id: str) -> Expect:
        return Expect.custom(f"событие {event_id} квитировано", lambda st: cls._acked(st, event_id), ["dome_automation_01.events"])

    @staticmethod
    def _acked(st: Any, event_id: str) -> bool:
        for ev in st.get("dome_automation_01.events", []) or []:
            if isinstance(ev, dict) and str(ev.get("event_id")) == event_id:
                return bool(ev.get("acknowledged"))
        return False
