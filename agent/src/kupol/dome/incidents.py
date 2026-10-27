"""Durable incident projection over the existing event journal, independent of the LLM."""
from __future__ import annotations

from dataclasses import asdict, fields
from uuid import uuid4
import time

from kupol.dome.control import Incident, CRITICAL_NODES


class IncidentCenter:
    def __init__(self, emit=lambda *a, **k: None, events=(), clock=time.time):
        self.emit, self.clock = emit, clock
        self.items: dict[str, Incident] = {}
        self.active: dict[str, str] = {}
        self.notified: dict[str, str] = {}
        names = {f.name for f in fields(Incident)}
        for event in events:
            p = event.get('payload') or {}
            if p.get('incident_id') and p.get('dedup_key'):
                item = Incident(**{k: v for k, v in p.items() if k in names})
                item.current = False  # persisted facts are historical until seen in live telemetry
                self.items[item.incident_id] = item
                if item.status != 'resolved':
                    self.active[item.dedup_key] = item.incident_id
                if p.get('notification_token'):
                    self.notified[item.incident_id] = p['notification_token']

    def _save(self, item):
        item.updated_at = self.clock()
        self.emit('incident_changed', {**asdict(item), 'notification_token': self.notified.get(item.incident_id)},
                  source='system', node_id=item.node_id, metadata={'actor': 'safety', 'contract_version': 1})

    def open(self, key, severity, node, reason, required_action, facts=None):
        item = self.items.get(self.active.get(key))
        new = item is None
        if new:
            item = Incident(str(uuid4()), key, severity, reason, node, required_action, facts or {})
            self.items[item.incident_id] = item
            self.active[key] = item.incident_id
        old = asdict(item)
        # A model or lesser subsequent rule cannot downgrade an unresolved critical risk.
        if severity == 'red':
            item.severity = 'red'
        item.reason, item.required_action, item.facts, item.current = reason, required_action, facts or {}, True
        if new or old != asdict(item):
            self._save(item)
        return item

    def observe_recovery(self, key):
        """Record symptom recovery while keeping cause/risk/follow-up review open."""
        item = self.items.get(self.active.get(key))
        if item is not None and not item.facts.get('recovery_observed'):
            item.facts.update(recovery_observed=True, root_cause_status='unverified',
                              closure_requires='Проверка причины, зависимостей, остаточных рисков и последующих действий')
            self._save(item)

    def resolve(self, key, *, review=None):
        """Only a documented review can close the incident; telemetry is insufficient."""
        if not isinstance(review, dict) or not all(review.get(k) is True for k in
                ('state_verified', 'dependencies_verified', 'cause_resolved')):
            return False
        if not isinstance(review.get('evidence'), list) or not review['evidence']:
            return False
        if not all(isinstance(review.get(k), list) for k in ('residual_risks', 'follow_up')):
            return False
        if not isinstance(review.get('reviewed_by'), str) or not review['reviewed_by'].strip():
            return False
        item = self.items.get(self.active.get(key))
        if item is not None:
            item.facts['closure_review'] = review
            item.status = 'resolved'
            item.current = True
            self.active.pop(key, None)
            self._save(item)
            return True
        return False

    def acknowledge(self, incident_id):
        item = self.items[incident_id]
        if item.status == 'open':
            item.status = 'acknowledged'
            self._save(item)
        return asdict(item)

    def public(self):
        return [asdict(i) for i in sorted(self.items.values(), key=lambda i: i.updated_at, reverse=True)]

    def pending_notifications(self):
        for item in list(self.items.values()):
            token = item.severity + ':' + str(item.facts.get('danger_token', ''))
            if item.current and item.status != 'resolved' and self.notified.get(item.incident_id) != token:
                # Claim before awaiting network; repeated ticks do not enqueue another send.
                self.notified[item.incident_id] = token
                item.delivery = 'requested'
                self._save(item)
                yield item

    def delivered(self, item, status):
        item.delivery = status
        self._save(item)


def classify_live(st, cfg, center: IncidentCenter, now):
    """Use configured/source interlock conditions; absent thresholds are never invented."""
    if not st.wall or now - st.wall > 20:
        center.open('telemetry:stale', 'yellow', 'telemetry', 'Нет свежей телеметрии',
                    'Проверьте подключение; управление по старым данным заблокировано', {'received_at': st.wall})
        return
    center.observe_recovery('telemetry:stale')
    for node, body in st.nodes.items():
        key = 'availability:' + node
        if st.is_frozen(node) or body.get('online') is False:
            age = now - st._progress_wall.get(node, now)
            active = str(body.get('state', '')).upper() in ('PRINTING', 'RUNNING', 'PAUSED', 'HOMING', 'BUSY')
            critical = node in CRITICAL_NODES or (node in ('printer_3d_01', 'cnc_01') and active)
            severity = 'red' if critical and age >= cfg.checks.stale.node_age_s else 'yellow'
            center.open(key, severity, node, 'Узел недоступен или показания не обновляются',
                        'Проверьте узел и связь; результат задания неизвестен' if active else 'Проверьте доступность узла',
                        {'state': body.get('state'), 'measured_at': body.get('updated_at'), 'active_job': active})
            continue
        center.observe_recovery(key)
        if node.startswith('smoke_detector_'):
            key = 'smoke:' + node
            alarm = body.get('smoke_detected') is True or str(body.get('alarm_state', '')).upper() in ('ALARM', 'SMOKE')
            if alarm:
                center.open(key, 'red', node, 'Сработал датчик дыма',
                            'Проверьте зону; не обходите пожарную защиту', {'alarm_state': body.get('alarm_state'), 'danger_token': 'smoke'})
            elif body.get('smoke_detected') is False:
                center.observe_recovery(key)
        if node in ('printer_3d_01', 'cnc_01'):
            reason = body.get('hold_reason')
            key = 'machine:' + node
            if reason in ('FILAMENT_RUNOUT', 'BED_COLLISION') or body.get('hold_level') == 'ALARM':
                center.open(key, 'red', node, str(reason or 'ALARM'),
                            'Замените филамент' if reason == 'FILAMENT_RUNOUT' else 'Осмотрите станок и устраните физическую причину',
                            {'hold_reason': reason, 'resume_attempts': body.get('resume_attempts'), 'danger_token': reason})
            elif reason:
                center.open(key, 'yellow', node, str(reason), 'Устранить причину и проверить новый кадр перед resume',
                            {'hold_reason': reason, 'measured_at': body.get('updated_at')})
            elif str(body.get('state', '')).upper() in ('IDLE', 'PRINTING', 'RUNNING', 'COMPLETED'):
                center.observe_recovery(key)
