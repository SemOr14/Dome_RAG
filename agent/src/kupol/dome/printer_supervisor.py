"""Server-side printer supervision over already received telemetry, once per 10–15 s."""
from __future__ import annotations

import os

from kupol.dome.playbooks.base import Ctx, Intent, Step
from kupol.dome.playbooks.machines import cause_cleared, working

NODE = 'printer_3d_01'
SOFTWARE_HOLDS = ('IL_CO2', 'IL_CONDENSATE')
PHYSICAL = ('FILAMENT_RUNOUT', 'BED_COLLISION', 'IL_SMOKE', 'UNDERVOLTAGE')


class PrinterSupervisor:
    def __init__(self, runner, incidents=None, tracker=None, period=None):
        self.runner, self.incidents, self.tracker = runner, incidents, tracker
        self.period = float(period if period is not None else os.getenv('PRINTER_SUPERVISION_S', '12'))
        if not 10 <= self.period <= 15:
            raise ValueError('PRINTER_SUPERVISION_S must be 10..15')
        self.last = None
        self.last_stamp = None
        self.clear_stamp = None
        self.clear_reason = None

    def human(self, reason, facts, severity='red'):
        if self.incidents is not None:
            self.incidents.open('printer-supervisor:' + reason, severity, NODE, reason,
                                'Осмотрите принтер и устраните причину; автоматический повтор не выполняется', facts)

    def step(self, now):
        if self.last is not None and now - self.last < self.period:
            return []
        self.last = now
        r, st = self.runner, self.runner.state
        if r.policy.operating_state == 'shutting_down':
            return []
        mem = r.mem.setdefault('_printer_supervisor', {})
        body = st.node(NODE)
        stamp = body.get('updated_at')
        if not st.wall or now - st.wall > 20 or not body or st.is_frozen(NODE):
            self.human('Нет свежего состояния принтера', {'last_stamp': stamp}, 'yellow')
            return []
        if stamp is None or stamp == self.last_stamp:
            return []
        self.last_stamp = stamp
        if self.tracker is not None:
            self.tracker.observe(st, now)
        state = str(body.get('state', '')).upper()
        name = body.get('file_name')
        reason = body.get('hold_reason')
        progress = body.get('progress_pct')
        elapsed = body.get('print_elapsed_s')
        running = state == 'PRINTING' and str(body.get('real_state', '')).upper() == 'PRINTING' and not body.get('paused')
        powered = body.get('online') is True and st.line(3).get('state') == 'ON'
        if not powered or state in ('ABORTED', 'IDLE') or body.get('homed') is not True:
            if mem.get('active_file'):
                mem['unsafe_restart'] = True
                self.human('Потеря питания, задания или координат', {'state': state, 'file_name': name, 'homed': body.get('homed')})
            return []
        if running:
            # A distinct explicit job or increasing elapsed/progress establishes a new episode.
            new_job = name != mem.get('active_file') or (isinstance(elapsed, (int, float)) and isinstance(mem.get('elapsed'), (int, float)) and elapsed < mem['elapsed'])
            if new_job:
                mem.clear()
            old_progress = mem.get('progress')
            if name and isinstance(progress, (int, float)):
                if old_progress is None or progress > old_progress:
                    mem['progress_at'] = now
                mem.update(active_file=name, progress=progress, elapsed=elapsed, running_stamp=stamp)
            # An unchanged percentage for one supervision tick is never a stall.
            stall = r.cfg.checks.printer.stall_timeout_s
            phase = str(body.get('phase', body.get('gcode_state', ''))).upper()
            if stall and phase not in ('HEATING', 'HOMING', 'PREHEATING') and now - mem.get('progress_at', now) > stall:
                self.human('Прогресс не меняется дольше настроенного stall timeout', {'progress_pct': progress, 'phase': state}, 'yellow')
            self.clear_reason, self.clear_stamp = None, None
            return []
        if state == 'COMPLETED':
            # Completion/unlocking remains owned by assess_printer/PrinterPlaybook, never webhook.
            return []
        if state != 'PAUSED' and not body.get('paused'):
            return []
        if not reason:
            self.clear_reason, self.clear_stamp = None, None
            self.human('Ручная или неизвестная пауза', {'pause_origin': body.get('pause_origin')}, 'yellow')
            return []
        if reason in PHYSICAL or reason not in SOFTWARE_HOLDS or body.get('hold_level') == 'ALARM':
            self.human(str(reason), {'resume_attempts': body.get('resume_attempts'), 'hold_level': body.get('hold_level')})
            return []
        if not r.policy.algorithms_enabled or mem.get('unsafe_restart'):
            return []
        if not name or name != mem.get('active_file') or not mem.get('running_stamp'):
            self.human('Не подтверждено то же сохранённое задание', {'file_name': name})
            return []
        if body.get('pause_origin') in ('operator', 'manual', 'emergency'):
            return []
        if mem.get('resume_sent') or r.gate.is_pending(NODE, 'resume') or (body.get('resume_attempts') or 0) > 0:
            return []
        ctx = Ctx(st, r.cfg, r.gate, mem, now)
        cleared, why = cause_cleared(ctx, reason)
        if not cleared:
            self.clear_reason, self.clear_stamp = None, None
            return []
        if self.clear_reason != reason:
            self.clear_reason, self.clear_stamp = reason, stamp
            return []
        if self.clear_stamp == stamp:
            return []
        # Persist a single attempt before yielding it to Runner/Gate. Failed/ambiguous never retries.
        mem['resume_sent'] = {'reason': reason, 'file_name': name, 'stamp': stamp, 'policy_epoch': r.policy.policy_epoch}
        return [Intent(key='supervisor:resume:' + name + ':' + str(mem.get('running_stamp')),
            situation='Программная причина удержания принтера устранена и проверена на новых данных',
            cause=why, confidence=1.0, evidence=[{'node': NODE, 'param': 'file_name', 'value': name}],
            plan=['один resume', 'проверить фактический рост прогресса того же задания'],
            steps=[Step(NODE, 'resume', None, working(NODE), why)], cooldown_s=0, priority=2)]

    def verification(self, pending):
        if pending.cmd.node_id == NODE and pending.cmd.action == 'resume' and not pending.verified:
            self.human('Возобновление не подтверждено', {'action_seq': pending.action_seq, 'status': 'ambiguous'})
