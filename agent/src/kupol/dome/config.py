"""Конфигурация рантайма Dome (``dome.yaml``). Ошибки конфига останавливают запуск до начала смены."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

DEFAULT_CONFIG_PATH = Path("dome.yaml")


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatLimit(_Cfg):
    max_messages: int = Field(default=4, ge=1)
    window_s: float = Field(default=10.0, gt=0)


class RuntimeCfg(_Cfg):
    observation_interval_s: float = Field(default=45.0, gt=0)
    request_timeout_s: float = Field(default=15.0, gt=0)
    request_rate_per_s: float = Field(default=4.0, gt=0, le=5.0)
    chat_limit: ChatLimit = ChatLimit()
    reconnect_min_s: float = Field(default=3.0, ge=1.0)
    reconnect_max_s: float = Field(default=30.0, ge=1.0)
    verify_timeout_s: float = Field(default=20.0, gt=0)
    decision_cooldown_s: float = Field(default=60.0, ge=0)
    state_file: str = "state/agent_state.json"


class LineSwitchLimit(_Cfg):
    max_switches: int = Field(default=2, ge=1)
    window_s: float = Field(default=120.0, gt=0)


class PolicyCfg(_Cfg):
    # узел (или "*") -> действие -> спецификация значения
    allowed: dict[str, dict[str, dict[str, Any]]] = Field(default_factory=dict)
    line_switch_limit: LineSwitchLimit = LineSwitchLimit()
    no_effect_cooldown_s: float = Field(default=120.0, ge=0)
    resume_max_attempts: int = Field(default=1, ge=0)


class Co2Cfg(_Cfg):
    sensors: list[str] = ["climate_sensor_01", "climate_sensor_02", "climate_sensor_03"]
    max_rate_ppm_s: float = 40.0
    outlier_diff_ppm: float = 400.0
    agree_diff_ppm: float = 250.0
    high_ppm: float = 1500.0
    interlock_ppm: float = 2000.0
    recovered_ppm: float = 1200.0
    trust_back_s: float = 600.0


class CondensateCfg(_Cfg):
    rh_interlock_pct: float = 90.0
    dew_gap_c: float = 1.0


class SmokeCfg(_Cfg):
    false_evidence_needed: int = Field(default=2, ge=1)
    co_ok_ppm: float = 5.0
    voc_ok_index: float = 150.0
    obscuration_ok_pct: float = 3.0


class PrinterCfg(_Cfg):
    cool_bed_c: float = 40.0
    stall_timeout_s: float | None = Field(default=None, gt=15)


class EnergyCfg(_Cfg):
    soc_to_grid_pct: float = 35.0
    soc_to_hybrid_pct: float = 70.0
    soc_critical_pct: float = 25.0
    mode_switch_gap_s: float = 600.0
    min_pv_for_hybrid_w: float = 300.0


class PhantomCfg(_Cfg):
    lines: list[int] = [8]
    min_power_w: float = 20.0
    confirm_s: float = 120.0


class InverterCfg(_Cfg):
    reset_min_gap_s: float = 900.0
    reset_max_per_hour: int = 3
    humidity_ok_pct: float = 80.0


class StaleCfg(_Cfg):
    node_age_s: float = 30.0


class NoiseCfg(_Cfg):
    window_s: float = 120.0


class ChecksCfg(_Cfg):
    co2: Co2Cfg = Co2Cfg()
    condensate: CondensateCfg = CondensateCfg()
    smoke: SmokeCfg = SmokeCfg()
    printer: PrinterCfg = PrinterCfg()
    energy: EnergyCfg = EnergyCfg()
    phantom: PhantomCfg = PhantomCfg()
    inverter: InverterCfg = InverterCfg()
    stale: StaleCfg = StaleCfg()
    noise: NoiseCfg = NoiseCfg()


class RagCfg(_Cfg):
    top_k: int = Field(default=4, ge=1)
    chunk_chars: int = Field(default=900, ge=200)


class AdvisorCfg(_Cfg):
    enabled: bool = True
    model: str = "deepseek-v4-flash"
    min_interval_s: float = Field(default=60.0, ge=0)
    max_calls_per_hour: int = Field(default=60, ge=1)
    timeout_s: float = Field(default=20.0, gt=0)


class DomeConfig(_Cfg):
    runtime: RuntimeCfg = RuntimeCfg()
    policy: PolicyCfg = PolicyCfg()
    checks: ChecksCfg = ChecksCfg()
    rag: RagCfg = RagCfg()
    advisor: AdvisorCfg = AdvisorCfg()


class DomeConfigError(Exception):
    """Конфиг не прочитан или не прошёл проверку."""


def load_dome_config(path: str | Path | None = None) -> DomeConfig:
    """Загрузить ``dome.yaml`` (нет файла — значения по умолчанию).

    Args:
        path: Путь к файлу.

    Returns:
        Проверенный конфиг.

    Raises:
        DomeConfigError: Некорректный YAML или значения.
    """
    p = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not p.is_file():
        return DomeConfig()
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return DomeConfig.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as exc:
        raise DomeConfigError(f"{p}: {exc}") from exc
