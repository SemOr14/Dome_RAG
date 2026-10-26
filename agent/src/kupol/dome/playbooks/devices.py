"""Устройства и связь: аварии вентиляции/насоса/охлаждения, замёрзшие узлы, время, канал связи.

Сброс аварии — не чаще раза в 5 минут и не более ``reset_max_per_hour`` в час на устройство; после
сброса при необходимости включаем устройство. Вытяжку при перегреве двигателя сначала чистит Оператор.
"""

from __future__ import annotations

from kupol.dome.gate import Expect
from kupol.dome.playbooks.base import Ctx, Intent, Playbook, Step

ALARM_NODES = {
    "supply_ventilation_01": ("приточная вентиляция", "state"),
    "fume_extraction_01": ("вытяжка FabLab", "enabled"),
    "equipment_cooling_01": ("охлаждение станков", "state"),
    "water_pump_01": ("насосная станция", "state"),
}
RESET_GAP_S = 300.0
HOUR_S = 3600.0
RECONNECT_GAP_S = 300.0
RECONNECT_MAX_PER_30MIN = 3


def _on_expect(node: str, field: str) -> Expect:
    return Expect.custom(f"{node} включён", lambda s: s.get(f"{node}.{field}") in (True, "ON"), [f"{node}.{field}"])


def _fresh_expect(node: str) -> Expect:
    return Expect.custom(f"{node}: данные обновляются", lambda s: not s.is_frozen(node), [f"{node}.status", f"{node}.updated_at"])


def _is_on(ctx: Ctx, node: str, field: str) -> bool:
    v = ctx.state.get(f"{node}.{field}")
    return v is True or str(v).upper() == "ON"


class AlarmPlaybook(Playbook):
    """Авария устройства: сброс с ограничениями, затем включение."""

    name = "alarms"

    def step(self, ctx: Ctx) -> list[Intent]:
        st = ctx.state
        out: list[Intent] = []
        for node, (title, field) in ALARM_NODES.items():
            if not st.has(node):
                continue
            history = [t for t in ctx.mem.get(node, []) if ctx.now - t <= HOUR_S]
            ctx.mem[node] = history
            code = st.get(f"{node}.alarm_code")
            if st.get(f"{node}.alarm") is True:
                if history and ctx.now - history[-1] < RESET_GAP_S:
                    continue
                ev = [{"node": node, "param": "alarm_code", "value": code}]
                if len(history) >= ctx.cfg.checks.inverter.reset_max_per_hour:
                    out.append(
                        Intent(
                            key=f"alarm:giveup:{node}",
                            situation=f"{title}: авария {code} повторяется ({len(history)} сброса за час)",
                            cause="сброс не помогает",
                            confidence=0.8,
                            evidence=ev,
                            plan=["больше не сбрасывать", "сообщить Оператору"],
                            say=f"{title.capitalize()}: авария {code} возвращается после сброса — нужна проверка на месте.",
                            priority=2,
                            cooldown_s=3600.0,
                        )
                    )
                    continue
                if node == "fume_extraction_01" and str(code).upper().startswith("MOTOR") and not history:
                    history.append(ctx.now)  # первая реакция — просьба почистить фильтр, потом сброс
                    out.append(
                        Intent(
                            key="alarm:fume_filter",
                            situation=f"вытяжка: авария {code}",
                            cause="перегрев двигателя (засорён фильтр), линия 1 может быть заблокирована тепловой защитой",
                            confidence=0.8,
                            evidence=ev,
                            plan=["попросить Оператора почистить фильтр", "после остывания сбросить аварию"],
                            say="Вытяжка отключилась от перегрева двигателя — продуйте/почистите фильтр, потом скажите. Работу с пайкой лучше приостановить.",
                            priority=1,
                            cooldown_s=900.0,
                        )
                    )
                    continue
                history.append(ctx.now)
                out.append(
                    Intent(
                        key=f"alarm:reset:{node}",
                        situation=f"{title}: авария {code}",
                        cause="сбой устройства; пробуем штатный сброс (с проверкой результата)",
                        confidence=0.7,
                        evidence=ev,
                        plan=["reset_alarm", "включить устройство, если оно выключено"],
                        steps=[Step(node, "reset_alarm", None, Expect.equals(f"{node}.alarm", False), f"авария {code}")],
                        priority=2,
                        cooldown_s=RESET_GAP_S,
                        rag_query=f"{title} авария {code} сброс",
                    )
                )
            elif history and ctx.now - history[-1] < 120 and not _is_on(ctx, node, field):
                out.append(
                    Intent(
                        key=f"alarm:turn_on:{node}",
                        situation=f"{title}: авария сброшена, устройство выключено",
                        cause="после сброса аварии устройство нужно включить",
                        confidence=0.8,
                        evidence=[{"node": node, "param": field, "value": st.get(f"{node}.{field}")}],
                        plan=["turn_on"],
                        steps=[Step(node, "turn_on", None, _on_expect(node, field), "после сброса аварии")],
                        priority=2,
                        cooldown_s=120.0,
                    )
                )
        return out


class StalePlaybook(Playbook):
    """Узлы с устаревшими данными: ограниченное число попыток ``reconnect``; в логе — как «данным не верим»."""

    name = "stale"

    def step(self, ctx: Ctx) -> list[Intent]:
        st = ctx.state
        out: list[Intent] = []
        for node in st.frozen_nodes():
            times = [t for t in ctx.mem.get(node, []) if ctx.now - t <= 1800.0]
            ctx.mem[node] = times
            if len(times) >= RECONNECT_MAX_PER_30MIN or (times and ctx.now - times[-1] < RECONNECT_GAP_S):
                continue
            times.append(ctx.now)
            out.append(
                Intent(
                    key=f"stale:{node}",
                    situation=f"{node}: данные устарели (status {st.get(f'{node}.status')}, отставание {st.age_s(node) or 0:.0f} с)",
                    cause="потеряна связь с узлом; показания заморожены и недостоверны",
                    confidence=0.85,
                    evidence=[{"node": node, "param": "status", "value": st.get(f"{node}.status")}, {"node": node, "param": "updated_at", "value": st.get(f"{node}.updated_at")}],
                    plan=["не принимать решений по замороженным показаниям", "одна попытка reconnect"],
                    steps=[Step(node, "reconnect", None, _fresh_expect(node), "узел замёрз")],
                    priority=3,
                    cooldown_s=RECONNECT_GAP_S,
                )
            )
        return out


class ClockPlaybook(Playbook):
    """Рассинхрон часов вычислительного узла."""

    name = "clock"

    def step(self, ctx: Ctx) -> list[Intent]:
        off = ctx.state.num("edge_compute_01.clock_offset_s")
        if off is None or abs(off) < 2.0:
            return []
        return [
            Intent(
                key="clock:sync",
                situation=f"clock_offset_s = {off:.1f} с на edge_compute_01",
                cause="рассинхрон времени: метки в журналах недостоверны",
                confidence=0.85,
                evidence=[{"node": "edge_compute_01", "param": "clock_offset_s", "value": off}],
                plan=["sync_time"],
                steps=[Step("edge_compute_01", "sync_time", None, Expect.custom("часы синхронизированы", lambda s: abs(s.num("edge_compute_01.clock_offset_s") or 0.0) < 1.0, ["edge_compute_01.clock_offset_s"]), "рассинхрон времени")],
                priority=4,
                cooldown_s=600.0,
            )
        ]


class NetworkPlaybook(Playbook):
    """Основной канал связи деградирует: переключение канала — физическое действие Оператора."""

    name = "network"

    def step(self, ctx: Ctx) -> list[Intent]:
        st = ctx.state
        if not st.has("network_link_01"):
            return []
        link = str(st.get("network_link_01.link_state", "UP")).upper()
        loss = st.num("network_link_01.packet_loss_pct") or 0.0
        bad = link != "UP" or loss >= 30.0
        if not bad:
            ctx.mem.pop("bad_since", None)
            return []
        since = ctx.mem.setdefault("bad_since", ctx.now)
        if ctx.now - since < 30 or st.get("network_link_01.patchcord_replacing") is True:
            return []
        return [
            Intent(
                key="network:degraded",
                situation=f"основной канал: link_state {link}, потери пакетов {loss:.0f} %, flaps {st.get('network_link_01.link_flaps')}",
                cause="деградация основного канала связи; резервный канал переключает Оператор",
                confidence=0.8,
                evidence=[{"node": "network_link_01", "param": "link_state", "value": link}, {"node": "network_link_01", "param": "packet_loss_pct", "value": loss}],
                plan=["сообщить Оператору", "команды не повторять вслепую"],
                say=f"Основной канал связи деградирует (состояние {link}, потери {loss:.0f} %). Если команды перестанут проходить — переключите на резерв (патчкорд).",
                priority=2,
                cooldown_s=1800.0,
            )
        ]
