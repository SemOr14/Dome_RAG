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
    """Keep protection until the cause and safe load are established."""

    name = "overcurrent"

    def step(self, ctx: Ctx) -> list[Intent]:
        out = []
        if interlock_state(ctx.state, 'IL_OVERCURRENT') != 'TRIPPED':
            self._warn_protection(ctx, out)
            return out
        shed = ctx.state.get('dome_automation_01.interlocks.IL_OVERCURRENT.shed_lines', []) or []
        facts = [{'node': 'dome_automation_01', 'param': 'interlocks.IL_OVERCURRENT.shed_lines', 'value': shed}]
        return [Intent(key='overcurrent:inspect', situation=f'IL_OVERCURRENT: отключены линии {shed}',
            cause='причина перегрузки не установлена; ожидание не подтверждает безопасность', confidence=0.0,
            evidence=facts, plan=['сохранить защиту', 'проверить нагрузку и физические причины'],
            human_escalation=dict(node_id='smart_panel_01', incident='IL_OVERCURRENT', severity='yellow',
                facts=facts, already_done=['Автоматическое повторное включение запрещено'],
                required_action='Проверьте нагрузки, кабели и защиту отключённых линий; устраните причину и подтвердите безопасный баланс перед восстановлением по процедуре.'),
            priority=1, cooldown_s=900.0)]

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
