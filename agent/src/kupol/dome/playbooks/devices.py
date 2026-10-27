"""Устройства и связь: аварии вентиляции/насоса/охлаждения, замёрзшие узлы, время, канал связи.

Авария неизвестной причины передаётся Оператору. Таймер, сброс флага и просьба об осмотре
не дают допуска на сброс или повторное включение.
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
    """Unknown physical defects need inspection; reset is not a repair."""

    name = "alarms"

    def step(self, ctx: Ctx) -> list[Intent]:
        out = []
        for node, (title, _) in ALARM_NODES.items():
            if ctx.state.get(f'{node}.alarm') is not True:
                continue
            code = ctx.state.get(f'{node}.alarm_code')
            facts = [{'node': node, 'param': 'alarm_code', 'value': code},
                     {'node': node, 'param': 'alarm', 'value': True}]
            action = ('Проверьте фильтр, температуру двигателя и охлаждение; устраните выявленный дефект'
                      if node == 'fume_extraction_01' else
                      'Проверьте питание, кабели, механическую часть и охлаждение; установите и устраните причину')
            out.append(Intent(
                key=f'alarm:inspect:{node}:{code}', situation=f'{title}: авария {code}',
                cause='причина и безопасность повторного включения не подтверждены', confidence=0.0,
                evidence=facts, plan=['сохранить защиту', 'диагностика причины', 'проверка Оператора'],
                human_escalation=dict(node_id=node, incident=f'Авария {code}', severity='yellow',
                    facts=facts, already_done=['Автоматический сброс и повторное включение не выполнялись'],
                    required_action=action + '. Сброс — только по разрешённой процедуре с проверкой результата.'),
                priority=1, cooldown_s=900.0))
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
                steps=[Step("edge_compute_01", "sync_time", None, Expect.custom("часы синхронизированы", lambda s: s.num("edge_compute_01.clock_offset_s") is not None and abs(s.num("edge_compute_01.clock_offset_s")) < 1.0, ["edge_compute_01.clock_offset_s"]), "рассинхрон времени")],
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
