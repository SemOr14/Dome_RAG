"""Versioned local control contracts; these names are never sent as Dome API actions."""
from __future__ import annotations

import os
from uuid import uuid4
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

AgentMode = Literal['off', 'recommendation', 'autonomous']
DeploymentProfile = Literal['competition', 'service']
OperatingState = Literal['normal', 'manual_after_yellow', 'shutting_down']
Actor = Literal['operator', 'algorithm', 'agent', 'safety']
CONTRACT_VERSION = 1
ADVISORY_REASON = 'LLM не исполняет воздействия в этом режиме'
SEND_GUARD: ContextVar[Any] = ContextVar('dome_send_guard', default=None)
COMMAND_ACTOR: ContextVar[str] = ContextVar('dome_command_actor', default='algorithm')
DECISION_POLICY_EPOCH: ContextVar[Any] = ContextVar('dome_decision_policy_epoch', default=None)


@dataclass
class ControlPolicy:
    agent_mode: AgentMode = 'recommendation'
    algorithms_enabled: bool = True
    profile: DeploymentProfile = 'competition'
    api_scope: str = 'history'
    policy_epoch: int = 0
    operating_state: OperatingState = 'normal'

    @classmethod
    def from_env(cls):
        mode = os.getenv('AGENT_MODE', 'recommendation')
        profile = os.getenv('DEPLOYMENT_PROFILE', 'competition')
        scope = os.getenv('DOME_API_SCOPE', 'history')
        if mode not in ('off', 'recommendation', 'autonomous') or profile not in ('competition', 'service') or scope not in ('history', 'full'):
            raise ValueError('Invalid AGENT_MODE, DEPLOYMENT_PROFILE or DOME_API_SCOPE')
        return cls(mode, os.getenv('ALGORITHMS_ENABLED', '1') == '1', profile, scope, uuid4().int % (2**48))

    def public(self):
        return {'contract_version': CONTRACT_VERSION, **asdict(self)}

    def change(self, mode: AgentMode, algorithms: bool):
        if self.operating_state == 'shutting_down':
            raise ValueError('Приложение завершается')
        if mode not in ('off', 'recommendation', 'autonomous'):
            raise ValueError('Неизвестный режим агента')
        self.agent_mode, self.algorithms_enabled = mode, algorithms
        self.policy_epoch += 1
        self.operating_state = 'normal'

    def reject(self, actor: Actor, epoch: int | None, node: str, action: str) -> str | None:
        if self.operating_state == 'shutting_down':
            return 'Приложение завершается'
        if epoch != self.policy_epoch:
            return 'Решение отменено сменой policy_epoch'
        if self.api_scope != 'full':
            return 'Подключение предоставляет только историю; управление недоступно'
        if actor == 'operator' and self.profile != 'service':
            return 'competition: прямые ручные команды запрещены; используйте чат агента'
        if actor == 'agent' and self.agent_mode != 'autonomous':
            return ADVISORY_REASON
        if actor == 'algorithm' and not self.algorithms_enabled:
            return 'Алгоритмы выключены'
        if actor == 'safety' and self.profile != 'service' and node != 'meeting_01':
            return 'Ручной сценарий питания доступен только в service'
        return None


@dataclass
class EquipmentCapabilities:
    live_read: bool = False
    history_read: bool = True
    upload: bool = False
    start: bool = False
    pause: bool = False
    resume: bool = False
    stop: bool = False
    execution_confirmation: bool = False

    @classmethod
    def for_node(cls, catalog, node: str, live: bool):
        info = catalog.nodes.get(node) if catalog else None
        if not live or info is None:
            return cls()
        controls = info.controls | info.service_actions
        # Competition API §7.1 forbids pause/cancel regardless of stale local copies.
        return cls(True, True, node == 'printer_3d_01' and 'upload_file' in controls,
                   'start_job' in controls, False, 'resume' in controls, False, True)


@dataclass
class ProductionJob:
    job_id: str
    device: str
    generation: int = 1
    state: str = 'unknown'
    file_identity: dict[str, Any] = field(default_factory=dict)
    completed_work: float | None = None
    elapsed: float | None = None
    eta: float | None = None
    eta_source: str | None = None
    measured_at: Any = None
    pause_origin: str | None = None
    reason: str | None = None


@dataclass
class Incident:
    incident_id: str
    dedup_key: str
    severity: str
    reason: str
    node_id: str
    required_action: str
    facts: dict[str, Any] = field(default_factory=dict)
    status: str = 'open'
    delivery: str = 'pending'
    current: bool = True
    updated_at: float = 0


CRITICAL_NODES = frozenset(('solar_inverter_01', 'battery_01', 'smart_panel_01',
    'supply_ventilation_01', 'dome_sealing_01', 'dome_automation_01', 'smoke_detector_01',
    'smoke_detector_02', 'fire_suppression_01', 'air_quality_sensor_01', 'chem_sensor_01',
    'radiation_sensor_01', 'network_link_01', 'backup_comms_01', 'radio_01', 'edge_compute_01',
    'access_control_01', 'water_pump_01', 'water_tank_01', 'water_filter_01'))
SECONDARY_LINES = (1, 2, 3, 4, 5, 6, 8)
