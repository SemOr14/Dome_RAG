"""Single-job production console. No file queues, CAM, streaming or controller transport."""
from __future__ import annotations

import hashlib
import math
import os
import re
import time
from collections import deque
from dataclasses import asdict
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from kupol.dome.control import EquipmentCapabilities, ProductionJob

DEVICES = ('printer_3d_01', 'cnc_01')
ACTIVE = ('PRINTING', 'RUNNING', 'PAUSED', 'BUSY', 'HOMING')


def number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def safe_name(name):
    if not isinstance(name, str) or not name or len(name) > 200 or name in ('.', '..') or re.search(r'[/\\\x00-\x1f\x7f]', name):
        raise HTTPException(422, 'Недопустимое имя файла')
    return name


def validate_upload(name, content, active_file=None):
    safe_name(name)
    if not name.lower().endswith(('.gcode', '.gco', '.gc')):
        raise HTTPException(422, 'Нужен готовый G-code; STL/STEP/DXF не поддерживаются')
    try:
        data = content.encode('utf-8', errors='strict')
    except UnicodeError:
        raise HTTPException(422, 'Нужен текст UTF-8')
    if not data or len(data) > 16 * 1024 * 1024 or '\x00' in content:
        raise HTTPException(422, 'Пустой/бинарный файл или превышен предел 16 МБ')
    if name == active_file:
        raise HTTPException(409, 'Нельзя перезаписать выполняемый файл')
    return hashlib.sha256(data).hexdigest(), len(data)


class ProductionTracker:
    def __init__(self, emit=lambda *a, **k: None):
        self.emit = emit
        self.jobs = {}
        self.samples = {node: deque(maxlen=8) for node in DEVICES}
        self.stamps = {}
        self.previous = {}

    def observe(self, st, now):
        for node in DEVICES:
            body = st.node(node)
            stamp = body.get('updated_at')
            if not body or stamp == self.stamps.get(node) or st.is_frozen(node):
                continue
            self.stamps[node] = stamp
            state = str(body.get('state', 'unknown')).upper()
            name = body.get('file_name')
            elapsed_key = 'print_elapsed_s' if node == DEVICES[0] else 'job_elapsed_s'
            elapsed = number(body.get(elapsed_key))
            job = self.jobs.get(node)
            previous = self.previous.get(node, {})
            new_job = name and (job is None or job.file_identity.get('name') != name or
                       (state in ACTIVE and previous.get('state') in ('IDLE', 'COMPLETED', 'ABORTED')) or
                       (elapsed is not None and previous.get('elapsed') is not None and elapsed < previous['elapsed'] and state != 'PAUSED'))
            if new_job:
                job = ProductionJob(str(uuid4()), node, (job.generation + 1) if job else 1, file_identity={'name': name})
                self.jobs[node] = job
                self.samples[node].clear()
            self.previous[node] = {'state': state, 'elapsed': elapsed}
            if job is None:
                continue
            files = body.get('files') or []
            info = next((f for f in files if isinstance(f, dict) and f.get('name') == name), {})
            job.file_identity = {'name': name, **{k: info[k] for k in ('sha256', 'size_bytes') if k in info}}
            job.state, job.elapsed, job.measured_at = state, elapsed, stamp
            job.completed_work = number(body.get('progress_pct'))
            job.reason = body.get('hold_reason') or body.get('error_code')
            job.pause_origin = body.get('pause_origin') or ('manual_or_unknown' if state == 'PAUSED' and not body.get('hold_reason') else 'server_hold' if body.get('hold_reason') else None)
            left = number(body.get('print_time_left_s' if node == DEVICES[0] else 'job_time_left_s'))
            job.eta = left if left is not None and left >= 0 else None
            job.eta_source = 'telemetry' if job.eta is not None else None
            if state not in ('PRINTING', 'RUNNING'):
                self.samples[node].clear()
            elif job.completed_work is not None:
                samples = self.samples[node]
                if samples and job.completed_work < samples[-1][1]:
                    samples.clear()
                samples.append((now, job.completed_work))
                if job.eta is None and len(samples) >= 3:
                    rates = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(samples, list(samples)[1:]) if b[0] > a[0]]
                    if rates and min(rates) > 0 and max(rates) <= min(rates) * 2 and samples[-1][0] - samples[0][0] >= 24:
                        job.eta = max(0, (100 - job.completed_work) / (sum(rates) / len(rates)))
                        job.eta_source = 'estimate'
            # A webhook alone never changes job.state into COMPLETED.
            self.emit('production_job', asdict(job), source='system', node_id=node, metadata={'actor': 'algorithm'})


class ProductionService:
    def __init__(self, hub):
        self.hub = hub
        self.tracker = ProductionTracker(hub.emit)
        self.results = {}

    def require_live(self, run_id):
        h = self.hub
        if run_id != h.run_id:
            raise HTTPException(409, 'Исторический запуск доступен только для просмотра')
        if h.policy.operating_state == 'shutting_down':
            raise HTTPException(409, 'Приложение завершается')
        if h.policy.api_scope != 'full' or not h.runner or not h.client.connected:
            raise HTTPException(409, 'Не поддерживается текущим подключением: нет живого API')

    def snapshot(self, run_id):
        h = self.hub
        records = h.store.latest_telemetry(run_id)['items']
        nodes = {e['node_id']: e for e in records}
        live = bool(run_id == h.run_id and h.runner and h.client.connected and h.policy.api_scope == 'full')
        rows = []
        for node in DEVICES:
            rec = nodes.get(node, {})
            body = h.runner.state.node(node) if live else rec.get('payload', {})
            fresh = bool(live and h.runner.state.wall and time.time() - h.runner.state.wall <= 20 and not h.runner.state.is_frozen(node))
            caps = EquipmentCapabilities.for_node(h.runner.catalog if live else None, node, live and fresh)
            approved = bool(os.getenv('PRODUCTION_APPROVED_SHA256', '').strip())
            caps.upload = caps.upload and approved
            job = self.tracker.jobs.get(node) if live else None
            if job is None:
                events = h.store.events(run_id=run_id, node_id=node, event_type='production_job', limit=1000)['items']
                stored_job = events[-1]['payload'] if events else None
            else:
                stored_job = asdict(job)
            last = h.store.events(run_id=run_id, node_id=node, event_type='control_dispatch', limit=1000)['items']
            rows.append({'node_id': node, 'telemetry': body, 'fresh': fresh, 'received_at': rec.get('ingested_at'),
                         'capabilities': asdict(caps), 'job': stored_job, 'last_action': last[-1]['payload'] if last else None,
                         'manual_allowed': fresh and h.policy.profile == 'service',
                         'limitations': ['Пауза и отмена недоступны API §7.1', 'Загрузка ЧПУ отсутствует' if node == DEVICES[1] else
                                         'Загрузка только файлов организаторов из серверного списка разрешённых SHA-256']})
        return {'run_id': run_id, 'history': not live, 'items': rows}

    async def files(self, node, run_id):
        if node not in DEVICES:
            raise HTTPException(404, 'Неизвестный станок')
        self.require_live(run_id)
        info = self.hub.runner.catalog.nodes.get(node)
        if not info or 'list_files' not in info.queries:
            raise HTTPException(409, 'list_files отсутствует в текущем каталоге')
        return await self.hub.client.query(node, 'list_files', {'validate': True} if node == DEVICES[1] else {})

    async def operate(self, body):
        h = self.hub
        self.require_live(body.run_id)
        if h.policy.profile != 'service':
            raise HTTPException(403, 'competition: задачу передают агенту через чат')
        node, action = body.node_id, body.action
        if node not in DEVICES or action not in ('start_job', 'resume', 'upload_file'):
            raise HTTPException(422, 'Действие не поддерживается API производства')
        caps = EquipmentCapabilities.for_node(h.runner.catalog, node, True)
        if not getattr(caps, {'start_job': 'start', 'resume': 'resume', 'upload_file': 'upload'}[action]):
            raise HTTPException(409, 'Не поддерживается текущим подключением')
        value, identity = None, None
        if action == 'start_job':
            value = safe_name(body.file_name)
        if action == 'upload_file':
            digest, size = validate_upload(body.file_name, body.content or '',
                h.runner.state.get(f'{node}.file_name') if h.runner.state.get(f'{node}.state') in ACTIVE else None)
            approved = {s.strip().lower() for s in os.getenv('PRODUCTION_APPROVED_SHA256', '').split(',') if s.strip()}
            if digest not in approved:
                raise HTTPException(403, 'Хеш файла отсутствует в разрешённых файлах организаторов')
            identity = {'name': body.file_name, 'sha256': digest, 'size_bytes': size}
            value = {'file_name': body.file_name, 'content': body.content}
        signature = {'run_id': body.run_id, 'node_id': node, 'action': action, 'file_name': body.file_name, 'file_identity': identity}
        op = str(body.operation_id)
        reservation = str(uuid5(NAMESPACE_URL, 'production:' + op))
        previous = h.store.event(reservation)
        if previous:
            if previous['payload'] != signature:
                raise HTTPException(409, 'operation_id уже относится к другой операции')
            return self.results.get(op, {'status': 'ambiguous', 'operation_id': op,
                'reason': 'Операция уже зарегистрирована; сверка по control_dispatch/verification, без повторной отправки'})
        h.emit('production_operation', signature, event_id=reservation, node_id=node,
               metadata={'actor': 'operator', 'policy_epoch': h.policy.policy_epoch, 'operation_id': op})
        result = await h.command(operation_id=op, node_id=node, action=action, value=value, reason='Ручная команда из раздела Производство')
        self.results[op] = result
        return result


class ProductionAction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation_id: UUID
    run_id: str = Field(min_length=1, max_length=200)
    node_id: str
    action: str
    file_name: str | None = None
    content: str | None = None


def install_production_routes(app, hub):
    @app.get('/control/production')
    def production(run_id: str | None = None):
        return hub.production.snapshot(run_id or hub.run_id)

    @app.get('/control/production/{node}/files')
    async def files(node: str, run_id: str):
        return await hub.production.files(node, run_id)

    @app.post('/control/production/actions')
    async def operate(body: ProductionAction):
        return await hub.production.operate(body)
