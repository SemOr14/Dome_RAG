"""Воздух: ложный и настоящий CO₂ (``IL_CO2``), ложный и настоящий конденсат (``IL_CONDENSATE``).

Главная идея: интерлок срабатывает через 60 с после начала условия (``pending_s``), поэтому ложный
выброс основного датчика надо опередить — пометить его недостоверным и сменить основной датчик, пока
станки ещё не встали. Если интерлок всё же сработал — снять его после проверки и продолжить станки
(это делает ``ResumePlaybook``). Настоящий рост на нескольких датчиках не «лечим» сбросами.
"""

from __future__ import annotations

import statistics
from typing import Any

from kupol.dome.checks import SENSORS, assess_co2, assess_condensate, is_untrusted
from kupol.dome.gate import Expect
from kupol.dome.playbooks.base import Ctx, Intent, Playbook, Step, interlock_pending_s, interlock_state, trust_step
from kupol.dome.state import DomeState

SAY_REPEAT_S = 900.0


def _fmt(values: dict[str, float]) -> str:
    return ", ".join(f"{s.replace('climate_sensor_', 'д')}={v:.0f}" for s, v in sorted(values.items()))


def pick_primary(st: DomeState, candidates: dict[str, float]) -> str | None:
    """Лучший доверенный датчик для основного: выше ``confidence``, меньше дрейф и задержка."""
    best: tuple[float, str] | None = None
    for s in candidates:
        conf = st.num(f"{s}.confidence") or 0.5
        drift = abs(st.num(f"{s}.drift_co2_ppm") or 0.0)
        delay = st.num(f"{s}.reading_delay_s") or 0.0
        score = conf - drift / 1000.0 - delay / 100000.0
        if best is None or score > best[0]:
            best = (score, s)
    return best[1] if best else None


def paused_by(st: DomeState, il: str) -> list[str]:
    """Станки, стоящие на паузе по этому интерлоку."""
    return [n for n in ("printer_3d_01", "cnc_01") if st.get(f"{n}.hold_reason") == il]


class Co2Playbook(Playbook):
    """CO₂: сверка трёх датчиков, пометка ложного, смена основного, снятие интерлока, вентиляция при реальном росте."""

    name = "co2"

    def step(self, ctx: Ctx) -> list[Intent]:
        st, cfg = ctx.state, ctx.cfg.checks
        c = cfg.co2
        a = assess_co2(st, cfg)
        out: list[Intent] = []
        primary = str(st.get("dome_automation_01.primary_co2_sensor", ""))
        good = a.good()
        il = interlock_state(st, "IL_CO2")
        paused = paused_by(st, "IL_CO2")

        if a.kind == "unknown" and (il == "TRIPPED" or any(v >= c.high_ppm for v in a.values.values())):
            out.append(Intent(
                key="co2:unknown", situation="DIAGNOSE: CO2 требует актуальной независимой сверки",
                cause="недостаточно пригодных текущих измерений; задержанные резервы не опровергают тревогу",
                confidence=0.0, evidence=[{"node": s, "param": "co2_ppm", "value": v} for s, v in a.values.items()],
                plan=["сохранить защиту", "получить актуальные независимые измерения"],
                say="CO2: причину тревоги пока нельзя установить. Защиту сохраняю; нужны актуальные независимые измерения перед продолжением станков.",
                priority=1, cooldown_s=SAY_REPEAT_S,
            ))

        if a.kind == "false_sensor":
            todo = sorted(a.suspects - a.untrusted)
            primary_bad = primary in a.suspects or primary in a.untrusted
            best = pick_primary(st, good) if primary_bad else None
            if todo or (primary_bad and best and best != primary):
                steps: list[Step] = [trust_step(s, "co2_ppm", "UNRELIABLE") for s in todo]
                if primary_bad and best and best != primary:
                    steps.append(Step("dome_automation_01", "set_primary_sensor", best, Expect.equals("dome_automation_01.primary_co2_sensor", best), f"основной датчик CO₂ → {best}"))
                suspect = todo[0] if todo else primary
                state_note = (
                    f"интерлок уже сработал, станки на паузе ({', '.join(paused)})"
                    if il == "TRIPPED"
                    else f"интерлок ещё не сработал (отсчёт {interlock_pending_s(st, 'IL_CO2'):.0f} с) — успеваем"
                    if interlock_pending_s(st, "IL_CO2") > 0
                    else "интерлок не сработал"
                )
                say = (
                    f"Ложный CO₂: {suspect} показывает {a.values.get(suspect, float('nan')):.0f} ppm, "
                    f"остальные — {_fmt(good)}. Помечен ненадёжным"
                    + (f", основной датчик — {best}" if best and best != primary else "")
                    + (". Станки продолжу." if paused else ". Станки не останавливаю.")
                )
                out.append(
                    Intent.from_evidence(
                        a.evidence + [_ev(s, v) for s, v in good.items()],
                        key=f"co2:distrust:{','.join(todo)}" if todo else f"co2:primary:{best}",
                        situation=f"{suspect}: CO₂ {a.values.get(suspect, float('nan')):.0f} ppm при {_fmt(good)}; {state_note}",
                        cause=f"ложное показание {suspect} (сбой датчика, не воздух)",
                        confidence=_conf(a),
                        plan=["пометить датчик недостоверным (автоматика перестаёт им пользоваться)"]
                        + (["сменить основной датчик CO₂"] if primary_bad and best != primary else [])
                        + ["не останавливать станки"],
                        steps=steps,
                        say=say,
                        priority=2,
                        rag_query="ложный датчик CO2 IL_CO2 пометить недостоверным основной датчик",
                    )
                )

        if il == "TRIPPED":
            live = {s: v for s, v in a.values.items() if s not in a.untrusted}
            if len(live) >= 2 and primary in live and max(live.values()) < c.recovered_ppm:
                out.append(
                    Intent(
                        key="co2:reset_interlock",
                        situation=f"IL_CO2 сработал; доверенные датчики в норме: {_fmt(live)}",
                        cause="условие интерлока ушло или оно было ложным",
                        confidence=0.9,
                        evidence=[_ev(s, v).as_log() for s, v in live.items()],
                        plan=["снять IL_CO2", "продолжить станки"],
                        steps=[Step("dome_automation_01", "reset_interlock", "IL_CO2", Expect.equals("dome_automation_01.interlocks.IL_CO2.state", "ARMED"), "снятие IL_CO2 после проверки")],
                        priority=2,
                        rag_query="снять интерлок IL_CO2 продолжить станки",
                    )
                )

        if a.kind == "real":
            out.extend(self._ventilate(ctx, a.reference or statistics.median(a.values.values()), a))

        out.extend(self._restore_trust(ctx, a))
        return out

    def _ventilate(self, ctx: Ctx, level: float, a: Any) -> list[Intent]:
        st = ctx.state
        steps: list[Step] = []
        sv = "supply_ventilation_01"
        if st.has(sv):
            if str(st.get(f"{sv}.state", "")).upper() != "ON":
                steps.append(Step(sv, "turn_on", None, Expect.equals(f"{sv}.state", "ON"), "приток включить"))
            if (st.num(f"{sv}.speed_pct") or 0) < 100:
                steps.append(Step(sv, "set_speed", 100, Expect.equals(f"{sv}.speed_pct", 100), "приток на максимум"))
            if str(st.get(f"{sv}.mode", "")).upper() != "FRESH_AIR":
                steps.append(Step(sv, "set_mode", "FRESH_AIR", Expect.equals(f"{sv}.mode", "FRESH_AIR"), "свежий воздух"))
        fume = "fume_extraction_01"
        if st.has(fume) and st.get(f"{fume}.enabled") is False and st.line(1).get("state") == "ON":
            steps.append(Step(fume, "turn_on", None, Expect.equals(f"{fume}.enabled", True), "вытяжка"))
        paused = paused_by(st, "IL_CO2")
        say = (
            f"CO₂ реально растёт: {_fmt(a.values)} ppm. Включил приток на максимум"
            + (f"; станки на паузе по интерлоку ({', '.join(paused)}), продолжу, когда воздух придёт в норму." if paused else ".")
            + " Если можно, откройте дверь."
        )
        return [
            Intent.from_evidence(
                a.evidence,
                key=f"co2:real:{steps[0].action}" if steps else "co2:real:stable",
                situation=f"CO₂ на нескольких датчиках: {_fmt(a.values)}",
                cause="реальный рост CO₂ (подтверждён независимыми датчиками)",
                confidence=0.9,
                plan=["приток на максимум, свежий воздух", "интерлок не сбрасывать", "продолжить станки после снижения CO₂"],
                steps=steps,
                say=say,
                priority=1,
                cooldown_s=SAY_REPEAT_S,
                rag_query="реальный рост CO2 на всех датчиках приток вентиляция",
            )
        ]

    def _restore_trust(self, ctx: Ctx, a: Any) -> list[Intent]:
        """Вернуть доверие датчику, который устойчиво согласуется с остальными."""
        agree = ctx.mem.setdefault("agree_since", {})
        out: list[Intent] = []
        for s in sorted(a.untrusted):
            v, ref = a.values.get(s), a.reference
            if v is None or ref is None or s in a.spikes or abs(v - ref) > ctx.cfg.checks.co2.agree_diff_ppm:
                agree.pop(s, None)
                continue
            since = agree.setdefault(s, ctx.now)
            if ctx.now - since >= ctx.cfg.checks.co2.trust_back_s:
                out.append(
                    Intent(
                        key=f"co2:trust_back:{s}",
                        situation=f"{s} согласуется с остальными уже {ctx.now - since:.0f} с ({v:.0f} против {ref:.0f} ppm)",
                        cause="сбой датчика прошёл",
                        confidence=0.8,
                        evidence=[_ev(s, v).as_log()],
                        plan=["вернуть доверие датчику"],
                        steps=[trust_step(s, "co2_ppm", "TRUSTED")],
                        priority=6,
                    )
                )
        for s in list(agree):
            if s not in a.untrusted:
                agree.pop(s, None)
        return out


def _ev(sensor: str, value: float) -> Any:
    from kupol.dome.checks import Ev

    return Ev(f"sensor:{sensor}", "false", 0.0, f"{sensor} = {value:.0f} ppm", sensor, "co2_ppm", value)


def _conf(a: Any) -> float:
    from kupol.dome.checks import p_real

    return round(1.0 - p_real(a.evidence), 2)


class CondensatePlaybook(Playbook):
    """Конденсат: один датчик влажности/точки росы врёт — пометить, снять ``IL_CONDENSATE``; настоящий — сообщить."""

    name = "condensate"

    def step(self, ctx: Ctx) -> list[Intent]:
        st, cfg = ctx.state, ctx.cfg.checks
        a = assess_condensate(st, cfg)
        out: list[Intent] = []
        il = interlock_state(st, "IL_CONDENSATE")
        paused = paused_by(st, "IL_CONDENSATE")
        if a.kind == "false_sensor":
            todo = [s for s in sorted(a.bad) if not (is_untrusted(st, s, "humidity_pct") and is_untrusted(st, s, "dew_point_c"))]
            if todo:
                s = todo[0]
                steps = [trust_step(s, p, "UNRELIABLE") for p in ("humidity_pct", "dew_point_c") if not is_untrusted(st, s, p)]
                others = {x: v for x, v in a.rh.items() if x not in a.bad}
                out.append(
                    Intent.from_evidence(
                        a.evidence,
                        key=f"condensate:distrust:{s}:{steps[0].value['param']}",
                        situation=f"{s}: RH {a.rh.get(s, float('nan')):.0f} %, запас до точки росы {a.gap.get(s, float('nan')):.1f} °C; остальные RH {_fmt(others)} %",
                        cause=f"ложное показание влажности/точки росы {s}",
                        confidence=0.85,
                        plan=["пометить влажность и точку росы датчика недостоверными", "снять IL_CONDENSATE, продолжить фрезер"],
                        steps=steps,
                        say=f"Ложный конденсат: {s} показывает RH {a.rh.get(s, float('nan')):.0f} %, два других в норме. Датчик помечен ненадёжным"
                        + ("; фрезер продолжу." if paused else "; фрезер не останавливаю."),
                        priority=2,
                        rag_query="ложный конденсат влажность 99% точка росы IL_CONDENSATE",
                    )
                )
        if il == "TRIPPED":
            live_hot = [s for s in a.rh if a.trigger(s, cfg.condensate.rh_interlock_pct, cfg.condensate.dew_gap_c) and not is_untrusted(st, s, "humidity_pct")]
            if not live_hot and a.rh:
                out.append(
                    Intent(
                        key="condensate:reset_interlock",
                        situation="IL_CONDENSATE сработал, доверенные датчики влажности в норме",
                        cause="ложный конденсат или условие ушло",
                        confidence=0.85,
                        evidence=[{"node": s, "param": "humidity_pct", "value": v} for s, v in a.rh.items()],
                        plan=["снять IL_CONDENSATE", "продолжить фрезер"],
                        steps=[Step("dome_automation_01", "reset_interlock", "IL_CONDENSATE", Expect.equals("dome_automation_01.interlocks.IL_CONDENSATE.state", "ARMED"), "снятие IL_CONDENSATE после проверки")],
                        priority=2,
                    )
                )
        if a.kind == "real":
            out.append(
                Intent.from_evidence(
                    a.evidence,
                    key="condensate:real",
                    situation="высокая влажность на нескольких датчиках: " + _fmt(a.rh),
                    cause="реальный риск конденсата",
                    confidence=0.85,
                    plan=["не сбрасывать интерлок", "сообщить Оператору", "продолжить фрезер после просушки"],
                    say="Влажность реально высокая на нескольких датчиках — риск конденсата, фрезер на паузе. Продолжу, когда влажность снизится.",
                    priority=2,
                    cooldown_s=SAY_REPEAT_S,
                )
            )
        return out


__all__ = ["Co2Playbook", "CondensatePlaybook", "SENSORS", "paused_by", "pick_primary"]
