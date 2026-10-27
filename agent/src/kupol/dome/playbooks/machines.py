"""Станки и дым: продолжение работы после остановки автоматикой и реакция на тревогу ``IL_SMOKE``.

``resume`` отправляется один раз, когда причина удержания действительно ушла (интерлок снова
``ARMED``, значения в норме). Три пустые попытки блокируют станок на 20 минут — этого допустить нельзя.
Дым трактуется консервативно: «неуверенно = реально». Квитирование (оно отменяет автопуск
пожаротушения) — только при нескольких независимых свидетельствах ложной тревоги.
"""

from __future__ import annotations

from typing import Any

from kupol.dome.checks import (assess_co2, assess_condensate, assess_smoke, is_untrusted,
                               co2_recovery_safe, condensate_recovery_safe)
from kupol.dome.gate import Expect
from kupol.dome.playbooks.base import Ctx, Intent, Playbook, Step, interlock_pending_s, interlock_state

MACHINES = ("printer_3d_01", "cnc_01")
TITLES = {"printer_3d_01": "принтер", "cnc_01": "фрезер"}
SETTLE_S = 6.0  # причина должна оставаться снятой столько секунд, прежде чем продолжать станок


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
    if reason == 'IL_CO2':
        return co2_recovery_safe(st, cfg), 'CO₂: нужны актуальные независимые согласованные безопасные измерения'
    if reason == 'IL_CONDENSATE':
        return condensate_recovery_safe(st, cfg), 'Конденсат: нужны актуальные независимые безопасные измерения RH и точки росы'
    if reason == 'IL_SMOKE':
        return False, 'После дыма нужна разрешённая процедура проверки текущей зоны и события Оператором'
    if reason == "UNDERVOLTAGE":
        v = st.num("smart_panel_01.bus_voltage_v")
        return (v is not None and v >= 200, f"напряжение шины {v}")
    if reason.startswith("IL_"):
        return False, f"{reason}: ARMED не доказывает устранение причины; нужна разрешённая процедура"
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
    """No automatic cancellation of a fire response from indirect or chat evidence."""

    name = 'smoke'

    def step(self, ctx: Ctx) -> list[Intent]:
        out = []
        for det in ('smoke_detector_01', 'smoke_detector_02'):
            a = assess_smoke(ctx.state, ctx.cfg.checks, det)
            if not a.alarm:
                continue
            zone = ctx.state.get(f'{det}.zone', 'неизвестна')
            facts = [{'node': det, 'param': 'alarm_state', 'value': ctx.state.get(f'{det}.alarm_state')},
                     {'node': det, 'param': 'zone', 'value': zone}] + [e.as_log() for e in a.evidence]
            out.append(Intent(key=f'smoke:inspect:{det}', situation=f'{det}: тревога дыма, зона {zone}',
                cause='пожарная тревога; ложность и безопасность отмены реакции не подтверждены', confidence=0.0,
                evidence=facts, plan=['сохранить штатную пожарную реакцию', 'немедленная проверка зоны Оператором'],
                human_escalation=dict(node_id=det, incident='Тревога дыма', severity='red', facts=facts,
                    already_done=['Квитирование, сброс и отмена пожарной реакции не выполнялись'],
                    required_action=f'Немедленно проверьте зону {zone} и текущее событие по пожарной процедуре. Сохраняйте штатную защиту; общая фраза о ложной тревоге не является допуском.'),
                priority=1, cooldown_s=300.0))
        return out
