"""Reusable diagnostic policies. Read-only evidence routing; never equipment controls."""
from __future__ import annotations

import copy
import re
from collections import OrderedDict
from dataclasses import dataclass, field

from .monitoring import leaves
from kupol.dome.monitoring import number, unit_floor

MARK = 'Диагностические стратегии'
END_MARK = 'Конец диагностических стратегий'
POLICIES = {
    'sensor_conflict': ['Сопоставить свежие независимые измерения и их обычные смещения',
        'Отделить ошибку источника от процесса; два расходящихся датчика без третьего или независимого факта не позволяют выбрать ошибочный',
        'Использовать пригодный резерв для анализа; смену источника автоматики проводить через Gate'],
    'source_missing': ['Проверить время, питание, связь и доверие источнику',
        'Найти свежий сопоставимый источник вне той же отказавшей инфраструктуры',
        'При отсутствии резерва сохранить неизвестное значение; не подставлять ноль или последнее как актуальное'],
    'common_failure': ['Проверить общие питание, сеть, шину, сервис сбора и часы',
        'Не считать несколько зависимых датчиков независимым подтверждением',
        'Сначала восстановить наблюдаемость; не перезапускать все устройства по отдельности вслепую'],
    'actuator_mismatch': ['Проверить уставку, режим, интерлоки, питание и независимую обратную связь',
        'Дождаться установления; проверить фактический эффект по новым данным',
        'Не повторять ту же команду без новой гипотезы и допустимости через Gate'],
    'command_uncertain': ['ACK подтверждает приём, а не физический результат',
        'Сопоставить запрос, ответ и новую независимую телеметрию',
        'До выяснения результата не повторять воздействие; при неуспехе обратиться к человеку'],
    'device_fault': ['Сопоставить код ошибки, состояние, условия работы и зависимые узлы',
        'Проверить доступные независимые признаки причины',
        'Выбрать разрешённый шаг, проверить эффект; при неизвестной причине запросить проверку человека'],
    'coherent_change': ['Проверить общую динамику процесса и общую причину смещения измерений',
        'Согласованный рост у нескольких датчиков нельзя автоматически объявлять ложными показаниями',
        'Сопоставить с технологическим режимом, независимой физикой и пределами; общая ошибка датчиков всё ещё возможна'],
    'measurement_candidate': ['Проверить режим, уставку, свежесть и рабочий диапазон',
        'Сопоставить с независимыми измерениями и динамикой процесса',
        'Числовой выброс сам по себе не подтверждает неисправность'],
}


@dataclass
class Case:
    key: str
    pattern: str
    nodes: set
    fields: dict
    trigger_keys: set = field(default_factory=set)
    status: str = 'observing'
    healthy: int = 0
    flaps: int = 0
    mark: tuple | None = None
    recovery_marks: tuple | None = None
    selected: dict = field(default_factory=dict)
    selected_at: float = 0
    plan: dict = field(default_factory=dict)


class DiagnosticPatterns:
    def __init__(self, stale_s=30, hold_s=60, capacity=256):
        self.stale_s, self.hold_s, self.capacity = stale_s, hold_s, capacity
        self.cases: OrderedDict[str, Case] = OrderedDict()

    @staticmethod
    def classify(trigger):
        evidence = trigger.meta.get('evidence') or {}
        if trigger.meta.get('affected_nodes'):
            return 'common_failure'
        if trigger.kind == 'command':
            return 'command_uncertain'
        if trigger.key.startswith('tracking|') or trigger.kind == 'operation':
            return 'actuator_mismatch'
        if trigger.kind == 'stale' or evidence.get('kind') == 'missing':
            return 'source_missing'
        if trigger.kind == 'conflict':
            return 'sensor_conflict'
        if trigger.kind == 'numeric':
            return 'measurement_candidate'
        return 'device_fault'

    def observe(self, snapshot, triggers, active, catalog, now):
        nodes = snapshot.get('nodes') or {}
        newest = number(snapshot.get('timestamp'))
        if newest is None:
            newest = max((number(b.get('updated_at')) or 0 for b in nodes.values() if isinstance(b, dict)), default=0)
        newest += max(0, number(snapshot.get('telemetry_age_s')) or 0)
        touched = set()
        for t in triggers:
            if t.kind in ('operator', 'review', 'verification'):
                continue
            pattern = self.classify(t)
            evidence = t.meta.get('evidence') or {}
            targets = set(t.nodes) & set(nodes)
            if evidence.get('node') and not t.meta.get('affected_nodes'):
                targets = {evidence['node']}
            if not targets and t.key == 'telemetry|no_frames':
                targets, pattern = set(nodes), 'common_failure'
            if not targets:
                continue
            # Different detectors of the same node/field share a diagnostic case.
            param = evidence.get('field') or ''
            if pattern == 'measurement_candidate' and len(targets) == 1:
                coherent = self._coherent(next(iter(targets)), param, nodes, catalog, newest)
                if coherent:
                    pattern, targets = 'coherent_change', coherent
            key = f'{pattern}|{",".join(sorted(targets))}|{param}'
            c = self.cases.get(key)
            if c is None:
                fields = {n: self._fields(nodes.get(n, {}), param) for n in targets}
                c = Case(key, pattern, targets, fields)
                self.cases[key] = c
            self.cases.move_to_end(key)
            c.trigger_keys.add(t.key)
            if c.status in ('recovering', 'stabilized'):
                c.flaps += 1
            c.status, c.healthy = 'observing', 0
            touched.add(key)
        transitions = []
        trust = {k: str(v).upper() for b in nodes.values() if isinstance(b, dict)
                 and isinstance(b.get('data_trust'), dict) for k, v in b['data_trust'].items()}
        blocked = {n for c in self.cases.values() if c.status != 'stabilized' and c.pattern in ('sensor_conflict', 'source_missing', 'device_fault', 'common_failure') for n in c.nodes}
        for key, c in list(self.cases.items()):
            if c.status == 'stabilized' and key not in touched and not c.trigger_keys & active:
                continue
            mark = tuple((n, (nodes.get(n) or {}).get('updated_at', snapshot.get('observed_at', snapshot.get('timestamp')))) for n in sorted(c.nodes))
            available = all(n in nodes and self._healthy(n, nodes[n], newest, nodes)
                            and all(number(dict(leaves(nodes[n])).get(f)) is not None
                                    and trust.get(f'{n}.{f}', trust.get(f'{n}.*', 'TRUSTED')) == 'TRUSTED'
                                    for f in c.fields[n]) for n in c.nodes)
            if key in touched or c.trigger_keys & active:
                c.healthy = 0
                c.recovery_marks = mark
                c.status = 'observing'
            elif available and (c.recovery_marks is None or all(a != b for a, b in zip(mark, c.recovery_marks))):
                c.healthy += 1
                c.recovery_marks = mark
                c.status = 'stabilized' if c.healthy >= min(6, 3 + c.flaps) and (not c.selected or now - c.selected_at >= min(600, self.hold_s * (1 + c.flaps))) else 'recovering'
            elif not available:
                c.status = 'observing'
                c.healthy = 0
                c.recovery_marks = mark
            c.mark = mark
            old = (c.plan.get('status'), c.selected.get('node'), c.selected.get('field'))
            alternatives = []
            if c.pattern in ('sensor_conflict', 'source_missing', 'measurement_candidate'):
                for n in sorted(c.nodes):
                    for f in c.fields[n]:
                        alternatives += self._alternatives(n, f, nodes, catalog, newest, blocked)
            chosen = next((a for a in alternatives if a['node'] == c.selected.get('node') and a['field'] == c.selected.get('field')), None)
            if chosen is None and alternatives:
                chosen = alternatives[0]
                c.selected_at = now
            if c.status == 'stabilized' and now - c.selected_at >= min(600, self.hold_s * (1 + c.flaps)):
                chosen = None
            c.selected = chosen or {}
            c.plan = dict(case_id=key, pattern=c.pattern, status=c.status,
                nodes=sorted(c.nodes), fields=c.fields, flaps=c.flaps, checks=POLICIES[c.pattern] +
                (['Повторные сбои: удерживать резерв дольше, проверить общую причину нестабильности; не повторять сбросы вслепую'] if c.flaps >= 2 else []),
                alternatives=alternatives[:8], selected_observation=copy.deepcopy(c.selected) or None,
                uncovered_fields={n:[f for f in c.fields[n] if not any(a['field']==f for a in alternatives)] for n in c.nodes},
                source_use='analysis_only', incident_closed=False, root_cause_status='unverified',
                residual_risks=['Стабилизация измерений не подтверждает устранение физического дефекта'],
                escalation='Запросить человека, если нет независимого подтверждения, наблюдаемости или допустимого проверяемого действия',
                recovery='Не менее трёх новых здоровых обновлений подтверждают лишь стабилизацию наблюдений; закрытие инцидента требует отдельной проверки причины, зависимостей, остаточных рисков и последующих действий')
            if old != (c.status, c.selected.get('node'), c.selected.get('field')):
                transitions.append(copy.deepcopy(c.plan))
        while len(self.cases) > self.capacity:
            self.cases.popitem(last=False)
        return transitions

    @staticmethod
    def _fields(body, param):
        candidates = param.split(',') if param and re.search(r'_(?:c|ppm|pct|bar|pa|v|a|w|dbm|rpm)(?:\[\d+\])?$', param) else list(dict(leaves(body)))
        return [f for f in candidates if re.search(r'_(?:c|ppm|pct|bar|pa|v|a|w|dbm|rpm)(?:\[\d+\])?$', f)
                and not any(s in f for s in ('target', 'progress', 'limit', 'remaining', 'count'))][:6]

    def _healthy(self, node, body, newest, nodes):
        if body.get('online') is False or body.get('status') == 'UNKNOWN' or body.get('sensor_state') not in (None, 'OK'):
            return False
        confidence = number(body.get('confidence'))
        if confidence is not None and confidence < .7:
            return False
        stamp = number(body.get('updated_at'))
        if stamp is None or abs(newest - stamp + (number(body.get('reading_delay_s')) or 0)) > self.stale_s:
            return False
        from .context.diagnostics import state_fields
        if any(state_fields(body)):
            return False
        return True

    def _alternatives(self, source, field, nodes, catalog, newest, blocked):
        body = nodes.get(source) or {}
        meta = catalog.get(source) or {}
        group = body.get('sensor_group') or meta.get('redundancy_group') or body.get('zone')
        if not group:
            return []
        domain = body.get('failure_domain') or meta.get('failure_domain')
        units = (body.get('units') or {}).get(field)
        trust = {}
        for b in nodes.values():
            if isinstance(b, dict) and isinstance(b.get('data_trust'), dict):
                trust.update(b['data_trust'])
        out = []
        for peer, other in sorted(nodes.items()):
            pm = catalog.get(peer) or {}
            if (peer == source or peer in blocked or not isinstance(other, dict)
                    or (other.get('sensor_group') or pm.get('redundancy_group') or other.get('zone')) != group
                    or not self._healthy(peer, other, newest, nodes)):
                continue
            value = dict(leaves(other)).get(field)
            if number(value) is None or trust.get(f'{peer}.{field}', 'TRUSTED') not in ('TRUSTED', None):
                continue
            peer_units = (other.get('units') or {}).get(field)
            if units != peer_units:
                continue
            pd = other.get('failure_domain') or pm.get('failure_domain')
            if domain is not None and pd == domain:
                continue
            out.append(dict(node=peer, field=field, value=value, failure_domain=pd, updated_at=other.get('updated_at'),
                independence='separate_domain' if domain is not None and pd is not None else 'unverified',
                confidence='single_source_unconfirmed', units=peer_units))
        return out

    def _coherent(self, source, field, nodes, catalog, newest):
        body = nodes.get(source) or {}
        group = body.get('sensor_group') or (catalog.get(source) or {}).get('redundancy_group') or body.get('zone')
        if not group or not field:
            return set()
        peers = {}
        units = (body.get('units') or {}).get(field)
        for n, b in nodes.items():
            if not isinstance(b, dict):
                continue
            g = b.get('sensor_group') or (catalog.get(n) or {}).get('redundancy_group') or b.get('zone')
            value = dict(leaves(b)).get(field)
            if g == group and number(value) is not None and self._healthy(n, b, newest, nodes) and (b.get('units') or {}).get(field) == units:
                peers[n] = value
        if len(peers) < 3:
            return set()
        center = sum(peers.values()) / len(peers)
        return set(peers) if max(peers.values()) - min(peers.values()) <= max(3 * unit_floor(field), abs(center) * .05) else set()

    def plans(self):
        priorities = {'common_failure':0, 'command_uncertain':1, 'source_missing':2, 'actuator_mismatch':3}
        cases = sorted((c for c in self.cases.values() if c.status != 'stabilized'), key=lambda c:priorities.get(c.pattern, 4))
        return copy.deepcopy([c.plan for c in cases][:4])
