"""One-shot service yellow plan and a bounded, process-wide red shutdown."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from uuid import UUID, NAMESPACE_URL, uuid5

from fastapi import HTTPException
from pydantic import BaseModel

from kupol.dome.control import CRITICAL_NODES, SECONDARY_LINES
from kupol.dome.gate import Cmd, Expect

RED_EXIT = 77


class SafetyController:
    def __init__(self, hub, force_exit=os._exit):
        self.hub, self.force_exit = hub, force_exit
        self.yellow_task = None
        self.cancelled_lines = set()
        self.shutdown_timer = None
        self.yellow_result = None

    def cancel(self):
        if self.yellow_task is not None:
            self.yellow_task.cancel()
        self.yellow_task = None

    def cancel_line(self, line):
        self.cancelled_lines.add(line)

    def blockers(self, line):
        r = self.hub.runner
        if r is None or not r.state.wall or time.time() - r.state.wall > 20:
            return 'Нет свежей телеметрии'
        st = r.state
        if line == 7:
            return 'Аварийное освещение'
        declared = json.loads(os.getenv('CRITICAL_LINE_MAP', '{}'))
        if not isinstance(declared, dict) or any(type(n) is not int or not 1 <= n <= 8 for n in declared.values()):
            return 'Неизвестна критическая схема питания: проверьте CRITICAL_LINE_MAP'
        if line in declared.values():
            return 'Подтверждённая критическая нагрузка из CRITICAL_LINE_MAP'
        for node in CRITICAL_NODES:
            n = st.node(node).get('power_line', declared.get(node))
            if n == line:
                return 'Критическая нагрузка: ' + node
        loads = st.line(line).get('nodes') or st.line(line).get('loads') or []
        if any((x.get('node_id') if isinstance(x, dict) else x) in CRITICAL_NODES for x in loads):
            return 'Каталог линии содержит критическую нагрузку'
        printer = st.node('printer_3d_01')
        cnc = st.node('cnc_01')
        nozzle, bed = printer.get('nozzle_temp_c'), printer.get('bed_temp_c')
        hot = not (type(nozzle) in (int, float) and type(bed) in (int, float) and
            nozzle < float(os.getenv('YELLOW_NOZZLE_SAFE_C', '50')) and bed < r.cfg.checks.printer.cool_bed_c)
        spindle = cnc.get('spindle_rpm')
        rotating = type(spindle) not in (int, float) or spindle > 0
        if line in (2, 3) and hot:
            return 'Сохраняется питание охлаждения горячего принтера'
        if line == 4 and rotating:
            return 'Не подтверждён останов шпинделя; требуется оператор'
        if line == 1:
            fumes = st.node('fume_extraction_01')
            if hot or rotating or fumes.get('purge_required') is not False:
                return 'Смешанная линия: вытяжка ещё нужна или остаточные пары неизвестны; выключите пайку физически'
        if line == 5 and st.node('thermal_insulation_02').get('purge_required') is not False:
            return 'Химлаб: не подтверждено окончание удаления остаточных паров'
        return None

    def yellow(self, operation_id):
        h = self.hub
        identity = str(uuid5(NAMESPACE_URL, 'yellow:' + operation_id))
        previous = h.store.event(identity)
        if previous:
            return self.yellow_result or {'status': 'already_requested', 'operation_id': operation_id}
        h.set_mode('off', False)
        h.policy.operating_state = 'manual_after_yellow'
        epoch = h.policy.policy_epoch
        h.emit('yellow_requested', {'operation_id': operation_id, 'policy_epoch': epoch}, event_id=identity,
               metadata={'actor': 'operator'})
        h.incidents.open('yellow:' + operation_id, 'yellow', 'application', 'Переход в ручное управление',
                         'Примите управление; проверьте частичный результат отключения и сохранённые зависимости')
        self.cancelled_lines.clear()
        self.yellow_result = {'status': 'requested', 'operation_id': operation_id, 'policy_epoch': epoch, 'lines': {}}
        if h.policy.profile != 'service':
            self.yellow_result.update(status='manual_power_unavailable', reason='competition: ручное питание запрещено')
        elif h.policy.api_scope != 'full' or not h.runner or not h.client.connected:
            self.yellow_result.update(status='not_supported', reason='Нет живого управляющего API; оборудование не отключено')
        else:
            self.yellow_task = asyncio.create_task(self._yellow_plan(epoch))
        h.emit('yellow_result', self.yellow_result, metadata={'actor': 'safety'})
        return self.yellow_result

    async def _yellow_plan(self, epoch):
        h, result = self.hub, self.yellow_result
        remaining = set(SECONDARY_LINES)
        deadline = time.monotonic() + float(os.getenv('YELLOW_PLAN_TIMEOUT_S', '180'))
        try:
            while remaining and time.monotonic() < deadline:
                if epoch != h.policy.policy_epoch or h.policy.operating_state == 'shutting_down':
                    result['status'] = 'cancelled'
                    return
                for line in sorted(remaining):
                    if line in self.cancelled_lines:
                        result['lines'][str(line)] = {'status': 'cancelled_by_operator'}
                        remaining.remove(line)
                        continue
                    blocker = self.blockers(line)
                    if blocker:
                        result['lines'][str(line)] = {'status': 'preserved', 'reason': blocker}
                        continue
                    r = h.runner
                    ref = r.log.write('decision', situation='Жёлтый разовый план', cause='ручное отключение второстепенной линии',
                                      source='operator', plan=[f'line_off {line}'], policy_epoch=epoch)
                    cmd = Cmd('smart_panel_01', 'line_off', line, 'Жёлтый: явное отключение питания', ref,
                        Expect.custom(f'линия {line} выключена', lambda st, n=line: st.line(n).get('state') == 'OFF', ['smart_panel_01.lines']),
                        actor='safety', policy_epoch=epoch)
                    res = await r.gate.submit(cmd)
                    row = {'status': res.status, 'reason': res.reason, 'action_seq': res.action_seq}
                    result['lines'][str(line)] = row
                    remaining.remove(line)  # one attempt, never replay the same step
                    if res.status == 'sent':
                        row['status'] = 'accepted'
                        pending = next((p for p in r.gate.pending if p.action_seq == res.action_seq), None)
                        while pending and pending.verified is None and time.monotonic() < deadline:
                            if epoch != h.policy.policy_epoch or line in self.cancelled_lines:
                                return
                            await asyncio.sleep(.1)
                        row['status'] = 'verified' if pending and pending.verified else 'ambiguous'
                    if row['status'] in ('failed', 'ambiguous', 'rejected'):
                        h.incidents.open(f'yellow:line:{line}:{epoch}', 'red', 'smart_panel_01', f'Отключение линии {line} не подтверждено',
                                         'Проверьте линию и зависимые нагрузки', row)
                    h.emit('yellow_result', result, metadata={'actor': 'safety', 'policy_epoch': epoch})
                if remaining:
                    await asyncio.sleep(1)
            result['status'] = 'verified' if all(x['status'] == 'verified' for x in result['lines'].values()) and not remaining else 'partial'
        except asyncio.CancelledError:
            result['status'] = 'cancelled'
        finally:
            if h.policy.operating_state != 'shutting_down':
                h.emit('yellow_result', result, metadata={'actor': 'safety', 'policy_epoch': epoch})

    def red(self):
        h = self.hub
        if h.policy.operating_state == 'shutting_down':
            return {'status': 'shutting_down'}
        if self.force_exit is os._exit and os.getenv('DOME_MANAGED_LAUNCH') != '1':
            raise HTTPException(503, 'Полное завершение дерева процессов требует запуска python run_panel.py')
        h.policy.operating_state = 'shutting_down'
        h.policy.policy_epoch += 1
        h.policy.agent_mode, h.policy.algorithms_enabled = 'off', False
        self.cancel()
        if h.runner:
            h.runner._plans.clear()
            h.runner.stop()
            if h.runner.brain and h.runner.brain.task:
                h.runner.brain.task.cancel()
        # A daemon timer is independent of a stuck loop, database, network and executor thread.
        self.shutdown_timer = threading.Timer(.75, self._exit_owned)
        self.shutdown_timer.daemon = True
        self.shutdown_timer.start()
        asyncio.create_task(self._flush())
        return {'status': 'shutting_down', 'message': 'Приложение завершается. Состояние оборудования определяет Купол.'}

    def _exit_owned(self):
        if self.force_exit is os._exit:
            # Also clean descendants from the worker side if its launcher exited unexpectedly.
            import psutil
            try:
                for p in psutil.Process().children(recursive=True):
                    try:
                        p.kill()
                    except psutil.Error:
                        pass
            except psutil.Error:
                pass
        self.force_exit(RED_EXIT)

    async def _flush(self):
        try:
            await asyncio.wait_for(asyncio.to_thread(self.hub.emit, 'application_shutdown',
                                   {'reason': 'red', 'policy_epoch': self.hub.policy.policy_epoch}), .2)
        except Exception:
            pass


class SafetyIn(BaseModel):
    operation_id: UUID


def install_safety_routes(app, hub):
    @app.post('/control/safety/yellow')
    async def yellow(body: SafetyIn):
        return hub.safety.yellow(str(body.operation_id))

    @app.post('/control/safety/red')
    async def red():
        return hub.safety.red()
