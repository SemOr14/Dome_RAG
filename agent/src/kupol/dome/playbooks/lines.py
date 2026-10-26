"""Линии щитка: фантомная нагрузка на резервной линии, повторные перегрузки (``IL_OVERCURRENT``), сработавшая защита.

Журнал 14.10: линия 8 с 23:30 потребляла ≈53 Вт без нагрузки (окисленный клеммник). Отключаем её и
просим Оператора осмотреть. Сработавшую защиту (``TRIPPED``, ``RCD_TRIP``) сами не взводим: причина
неизвестна, сообщаем Оператору.
"""

from __future__ import annotations

from kupol.dome.gate import Expect
from kupol.dome.playbooks.base import Ctx, Intent, Playbook, Step, interlock_state

PANEL = "smart_panel_01"
OVERCURRENT_WAIT_S = 180.0
OVERCURRENT_RETRY_S = 900.0


class PhantomLinePlaybook(Playbook):
    """Резервная линия потребляет без нагрузки — отключить и попросить осмотреть."""

    name = "phantom"

    def step(self, ctx: Ctx) -> list[Intent]:
        st, cfg = ctx.state, ctx.cfg.checks.phantom
        out: list[Intent] = []
        for n in cfg.lines:
            line = st.line(n)
            power = float(line.get("power_w") or 0.0)
            key = f"since:{n}"
            if not line or line.get("state") != "ON" or power < cfg.min_power_w:
                ctx.mem.pop(key, None)
                continue
            since = ctx.mem.setdefault(key, ctx.now)
            if ctx.now - since < cfg.confirm_s:
                continue
            out.append(
                Intent(
                    key=f"phantom:line{n}",
                    situation=f"линия {n} ({line.get('name')}): включена и потребляет {power:.0f} Вт уже {ctx.now - since:.0f} с, нагрузки на ней быть не должно",
                    cause="фантомное потребление (утечка/окисленный контакт), реальной нагрузки нет",
                    confidence=0.85,
                    evidence=[
                        {"node": PANEL, "param": f"lines[{n - 1}].power_w", "value": power},
                        {"node": PANEL, "param": f"lines[{n - 1}].leakage_ma", "value": line.get("leakage_ma")},
                    ],
                    plan=["отключить резервную линию", "попросить Оператора осмотреть клеммник"],
                    steps=[Step(PANEL, "line_off", n, Expect.equals(f"{PANEL}.lines[{n - 1}].state", "OFF"), f"фантомная нагрузка {power:.0f} Вт")],
                    say=f"Резервная линия {n} потребляет ≈{power:.0f} Вт без нагрузки — отключил. Осмотрите её клеммник (окисление/влага), потом скажите — включу.",
                    priority=3,
                    cooldown_s=3600.0,
                    rag_query="резервная линия 8 фантомное потребление клеммник окислен",
                )
            )
        return out


class OvercurrentPlaybook(Playbook):
    """``IL_OVERCURRENT`` отключил линию: один осторожный возврат, при повторе — только Оператору."""

    name = "overcurrent"

    def step(self, ctx: Ctx) -> list[Intent]:
        st = ctx.state
        out: list[Intent] = []
        il = interlock_state(st, "IL_OVERCURRENT")
        if il != "TRIPPED":
            ctx.mem.pop("tripped_since", None)
            self._warn_protection(ctx, out)
            return out
        shed = st.get("dome_automation_01.interlocks.IL_OVERCURRENT.shed_lines", []) or []
        since = ctx.mem.setdefault("tripped_since", ctx.now)
        last = float(ctx.mem.get("last_reset", 0.0))
        lines = ", ".join(str(x) for x in shed) or "?"
        if ctx.now - last < OVERCURRENT_RETRY_S and last > 0:
            out.append(
                Intent(
                    key="overcurrent:repeat",
                    situation=f"IL_OVERCURRENT сработал повторно после нашего сброса, линии: {lines}",
                    cause="реальная перегрузка линии",
                    confidence=0.85,
                    evidence=[{"node": "dome_automation_01", "param": "interlocks.IL_OVERCURRENT.shed_lines", "value": shed}],
                    plan=["больше не сбрасывать", "Оператору проверить нагрузку на линии"],
                    say=f"Линия {lines} снова отключилась по перегрузке. Не возвращаю: проверьте, что подключено на этой линии.",
                    priority=2,
                    cooldown_s=1800.0,
                )
            )
            return out
        if ctx.now - since >= OVERCURRENT_WAIT_S:
            ctx.mem["last_reset"] = ctx.now
            out.append(
                Intent(
                    key="overcurrent:reset",
                    situation=f"IL_OVERCURRENT отключил линии {lines} {ctx.now - since:.0f} с назад",
                    cause="кратковременная перегрузка (пуск оборудования); попробуем вернуть один раз",
                    confidence=0.7,
                    evidence=[{"node": "dome_automation_01", "param": "interlocks.IL_OVERCURRENT.shed_lines", "value": shed}],
                    plan=["снять IL_OVERCURRENT один раз", "при повторе — сообщить Оператору"],
                    steps=[Step("dome_automation_01", "reset_interlock", "IL_OVERCURRENT", Expect.equals("dome_automation_01.interlocks.IL_OVERCURRENT.state", "ARMED"), "возврат линий после перегрузки")],
                    say=f"Линия {lines} отключалась по перегрузке — вернул один раз. Если повторится, проверьте нагрузку.",
                    priority=2,
                    rag_query="IL_OVERCURRENT линия отключена перегрузка вернуть",
                )
            )
        return out

    @staticmethod
    def _warn_protection(ctx: Ctx, out: list[Intent]) -> None:
        for line in ctx.state.get(f"{PANEL}.lines", []) or []:
            if not isinstance(line, dict):
                continue
            if line.get("state") in ("TRIPPED", "RCD_TRIP") or line.get("protection_state") in ("TRIPPED", "RCD_TRIPPED"):
                n = line.get("line")
                out.append(
                    Intent(
                        key=f"line:protection:{n}",
                        situation=f"линия {n} ({line.get('name')}): защита сработала ({line.get('state')}/{line.get('protection_state')})",
                        cause="причина срабатывания защиты не установлена; сбрасывать вслепую нельзя",
                        confidence=0.8,
                        evidence=[{"node": PANEL, "param": f"lines[{int(n) - 1 if isinstance(n, int) else 0}].state", "value": line.get("state")}],
                        plan=["защиту не сбрасывать", "сообщить Оператору"],
                        say=f"На линии {n} сработала защита ({line.get('state')}). Сам не сбрасываю — проверьте, что подключено к линии.",
                        priority=2,
                        cooldown_s=1800.0,
                    )
                )
