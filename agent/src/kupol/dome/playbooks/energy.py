"""Энергия: режим инвертора по SOC (упреждение ``IL_LOAD_SHED``), ошибки инвертора, MPPT.

По журналу 14.10: при SOC 34–35 % режим переводили в ``GRID_ONLY``, при 77 % и солнце возвращали в
``HYBRID``; E09 требует проверки изоляции, погода не даёт допуска на сброс. Если
дожить до SOC < 25 %, автоматика отключает линии 8, 1, затем 3 и 4 — то есть останавливает работу.
"""

from __future__ import annotations

from typing import Any

from kupol.dome.checks import assess_energy
from kupol.dome.evidence import measurement_usable
from kupol.dome.gate import Expect
from kupol.dome.playbooks.base import Ctx, Intent, Playbook, Step, interlock_state

INV = "solar_inverter_01"
HOUR_S = 3600.0
DAYLIGHT_LUX = 2000.0


def _recent(times: list[float], now: float, window: float) -> list[float]:
    return [t for t in times if now - t <= window]


class EnergyPlaybook(Playbook):
    """Режим инвертора, сброс нагрузки, ошибки инвертора, каналы MPPT."""

    name = "energy"

    def step(self, ctx: Ctx) -> list[Intent]:
        st = ctx.state
        if not measurement_usable(st, INV):
            return []
        out: list[Intent] = []
        out += self._mode(ctx)
        out += self._load_shed(ctx)
        out += self._inverter_error(ctx)
        out += self._mppt(ctx)
        return out

    # ------------------------------------------------------------------ режим по SOC

    def _mode(self, ctx: Ctx) -> list[Intent]:
        cfg = ctx.cfg.checks.energy
        e = assess_energy(ctx.state, ctx.cfg.checks)
        if e.soc is None:
            return []
        last = float(ctx.mem.get("mode_changed_at", 0.0))
        if ctx.now - last < cfg.mode_switch_gap_s:
            return []
        ev: list[dict[str, Any]] = [{"node": INV, "param": "battery_soc_pct", "value": e.soc}, {"node": INV, "param": "mode", "value": e.mode}, {"node": INV, "param": "grid_state", "value": ctx.state.get(f"{INV}.grid_state")}]
        if e.mode == "HYBRID" and e.soc < cfg.soc_to_grid_pct and e.grid_ok:
            ctx.mem["mode_changed_at"] = ctx.now
            return [
                Intent(
                    key="energy:to_grid",
                    situation=f"SOC {e.soc:.0f} % < {cfg.soc_to_grid_pct:.0f} % в режиме HYBRID, сеть доступна; прогноз до {ctx.cfg.checks.energy.soc_critical_pct:.0f} %: "
                    + (f"{e.t_left_h:.1f} ч" if e.t_left_h is not None else "разряд не идёт"),
                    cause="АКБ садится; при SOC < 25 % автоматика отключит линии (IL_LOAD_SHED) и остановит работу",
                    confidence=0.9,
                    evidence=ev,
                    plan=["перевести инвертор в GRID_ONLY (АКБ заряжается от панелей)"],
                    steps=[Step(INV, "set_mode_grid_only", None, Expect.equals(f"{INV}.mode", "GRID_ONLY"), f"SOC {e.soc:.0f} %")],
                    say=f"SOC {e.soc:.0f} % — перевёл нагрузку на сеть (GRID_ONLY), АКБ заряжается от панелей.",
                    priority=2,
                    rag_query="SOC низкий перевести инвертор GRID_ONLY IL_LOAD_SHED",
                )
            ]
        if e.mode == "GRID_ONLY" and e.soc >= cfg.soc_to_hybrid_pct and e.pv_w is not None and e.pv_w >= cfg.min_pv_for_hybrid_w:
            ctx.mem["mode_changed_at"] = ctx.now
            return [
                Intent(
                    key="energy:to_hybrid",
                    situation=f"SOC {e.soc:.0f} % ≥ {cfg.soc_to_hybrid_pct:.0f} % и генерация {e.pv_w:.0f} Вт в режиме GRID_ONLY",
                    cause="АКБ заряжена, солнце есть — можно работать от панелей",
                    confidence=0.85,
                    evidence=ev + [{"node": INV, "param": "pv_power_w", "value": e.pv_w}],
                    plan=["вернуть инвертор в HYBRID"],
                    steps=[Step(INV, "set_mode_hybrid", None, Expect.equals(f"{INV}.mode", "HYBRID"), f"SOC {e.soc:.0f} %")],
                    say=f"SOC {e.soc:.0f} %, солнце есть — вернул инвертор в HYBRID.",
                    priority=4,
                )
            ]
        if e.soc < cfg.soc_to_grid_pct and not e.grid_ok and e.mode == "HYBRID":
            return [
                Intent(
                    key="energy:no_grid_low_soc",
                    situation=f"SOC {e.soc:.0f} %, внешней сети нет",
                    cause="нечем заряжать АКБ, кроме панелей и дизеля",
                    confidence=0.8,
                    evidence=ev,
                    plan=["не отключать линии самим", "сообщить Оператору; дизель запустится автоматикой при IL_LOAD_SHED"],
                    say=f"SOC {e.soc:.0f} %, внешней сети нет: возможна автоматическая разгрузка линий. Лишнюю нагрузку лучше выключить.",
                    priority=2,
                    cooldown_s=1200.0,
                )
            ]
        return []

    # ------------------------------------------------------------------ IL_LOAD_SHED

    def _load_shed(self, ctx: Ctx) -> list[Intent]:
        st = ctx.state
        if interlock_state(st, "IL_LOAD_SHED") != "TRIPPED":
            return []
        e = assess_energy(st, ctx.cfg.checks)
        if e.soc is None or e.soc < ctx.cfg.checks.energy.soc_to_grid_pct:
            return [
                Intent(
                    key="energy:load_shed_active",
                    situation=f"IL_LOAD_SHED сработал, SOC {e.soc if e.soc is not None else float('nan'):.0f} %: линии отключены автоматикой",
                    cause="реальная нехватка энергии",
                    confidence=0.9,
                    evidence=[{"node": INV, "param": "battery_soc_pct", "value": e.soc}],
                    plan=["интерлок не снимать, пока АКБ не зарядится"],
                    say="Сработала разгрузка по энергии (SOC низкий): часть линий отключена автоматикой. Верну их, когда АКБ зарядится.",
                    priority=1,
                    cooldown_s=1200.0,
                )
            ]
        facts = [{'node': INV, 'param': 'battery_soc_pct', 'value': e.soc},
                 {'node': INV, 'param': 'load_power_w', 'value': st.num(f'{INV}.load_power_w')},
                 {'node': 'dome_automation_01', 'param': 'interlocks.IL_LOAD_SHED.shed_lines',
                  'value': st.get('dome_automation_01.interlocks.IL_LOAD_SHED.shed_lines')}]
        return [Intent(key='energy:load_shed_review', situation=f'IL_LOAD_SHED: SOC восстановился до {e.soc:.0f} %',
            cause='заряд восстановился, но допустимость возвращаемой нагрузки не подтверждена', confidence=0.0,
            evidence=facts, plan=['сохранить разгрузку', 'проверить доступную мощность и схему возврата'],
            human_escalation=dict(node_id=INV, incident='IL_LOAD_SHED', severity='yellow', facts=facts,
                already_done=['SOC проверен; интерлок не сбрасывался'],
                required_action='Сверьте доступную мощность, резерв, суммарную и пусковую нагрузку отключённых линий; восстановите их по утверждённой схеме. SOC сам по себе не является допуском.'),
            priority=1, cooldown_s=900.0)]

    # ------------------------------------------------------------------ ошибка инвертора

    def _inverter_error(self, ctx: Ctx) -> list[Intent]:
        st = ctx.state
        if st.get(f"{INV}.error") is not True:
            ctx.mem.pop("error_since", None)
            return []
        code = str(st.get(f"{INV}.error_code", ""))
        text = str(st.get(f"{INV}.error_text", ""))
        ctx.mem.setdefault("error_since", ctx.now)
        ev = [{"node": INV, "param": "error_code", "value": code}, {"node": INV, "param": "error_text", "value": text}]
        if code != "E09":
            return [
                Intent(
                    key=f"energy:error:{code}",
                    situation=f"ошибка инвертора {code} {text}",
                    cause="причина не установлена; сбросом не лечим",
                    confidence=0.6,
                    evidence=ev,
                    plan=["наблюдать", "сообщить Оператору"],
                    say=f"Ошибка инвертора {code} ({text}). Автоматически не сбрасываю — нужна проверка.",
                    priority=2,
                    cooldown_s=1800.0,
                )
            ]
        return [Intent(
            key="energy:e09_wait", situation="E09: нарушение изоляции PV",
            cause="исправность изоляции не подтверждена; погода не доказывает ложность ошибки",
            confidence=0.8, evidence=ev, plan=["сохранить защиту", "проверка изоляции Оператором"],
            say="E09: нужна проверка изоляции PV по установленной процедуре. Автоматический сброс запрещён.",
            priority=2, cooldown_s=1800.0,
        )]

    def _mppt(self, ctx: Ctx) -> list[Intent]:
        st = ctx.state
        lux = st.num("weather_station_01.illuminance_lux")
        if lux is None or lux < DAYLIGHT_LUX or st.get(f"{INV}.error") is True:
            return []
        steps: list[Step] = []
        if st.get("solar_panels_01.all_mppt_enabled") is False:
            steps.append(Step("solar_panels_01", "enable_all_mppt", None, Expect.equals("solar_panels_01.all_mppt_enabled", True), "день, каналы MPPT выключены"))
        if st.get(f"{INV}.mppt_enabled") is False:
            steps.append(Step(INV, "enable_mppt", None, Expect.equals(f"{INV}.mppt_enabled", True), "день, MPPT инвертора выключен"))
        if not steps:
            return []
        return [
            Intent(
                key="energy:mppt_off",
                situation=f"день ({lux:.0f} лк), но MPPT выключен",
                cause="потеря генерации из-за отключённых каналов",
                confidence=0.85,
                evidence=[{"node": "weather_station_01", "param": "illuminance_lux", "value": lux}],
                plan=["включить MPPT"],
                steps=steps,
                priority=2,
                cooldown_s=600.0,
            )
        ]
