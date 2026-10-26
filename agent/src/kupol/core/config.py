"""Конфигурация Штаба: pydantic-модели и загрузка `config.yaml`.

Ошибка в конфиге должна останавливать запуск до начала игры, поэтому все
проверки согласованности (ссылки на линии, аргументы команд, режим LLM)
выполняются здесь при загрузке и сообщают человекочитаемую причину.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ConfigError(Exception):
    """Конфигурация некорректна: понятное сообщение для оператора."""


class _Cfg(BaseModel):
    """Базовая модель конфига: неизвестные ключи запрещены (ловим опечатки)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class GameConfig(_Cfg):
    """Параметры игры и главного цикла."""

    duration_min: float = Field(default=90.0, gt=0)  # контрольное время игры, регламент п. 6.6
    tick_s: float = Field(default=1.0, gt=0)  # период опроса телеметрии
    history_minutes: float = Field(default=15.0, gt=0)  # глубина кольцевого буфера состояния
    alert_dedup_s: float = Field(default=60.0, ge=0)  # не повторять ту же тревогу чаще


class FieldThreshold(_Cfg):
    """Пороги и параметры детекторов для одного поля телеметрии.

    Любой параметр можно опустить: соответствующий детектор для поля выключен.
    """

    warn_low: float | None = None
    warn_high: float | None = None
    crit_low: float | None = None
    crit_high: float | None = None
    max_jump: float | None = None  # макс. изменение между соседними снимками (абс.)
    max_jump_rel: float | None = None  # то же, в долях от предыдущего значения
    rel_floor: float = 50.0  # относительный скачок считается, если прежнее значение больше
    trend_window_min: float | None = None  # окно тренда, минут
    max_slope_per_min: float | None = None  # допустимый |наклон| за минуту
    freeze_s: float | None = None  # сколько секунд значение может не меняться
    freeze_ignore_ge: float | None = None  # при значении не ниже этого «замерзание» не считается (SoC=100%)

    @model_validator(mode="after")
    def _check_order(self) -> FieldThreshold:
        """Критический порог не мягче предупредительного."""
        if self.crit_high is not None and self.warn_high is not None and self.crit_high < self.warn_high:
            raise ValueError("crit_high не может быть меньше warn_high")
        if self.crit_low is not None and self.warn_low is not None and self.crit_low > self.warn_low:
            raise ValueError("crit_low не может быть больше warn_low")
        if (self.max_slope_per_min is None) != (self.trend_window_min is None):
            raise ValueError("trend_window_min и max_slope_per_min задаются только вместе")
        return self


class CusumConfig(_Cfg):
    """CUSUM по невязкам (только разность пар и баланс; к абсолютным значениям не применяется)."""

    tolerance_sigma: float = Field(default=0.5, gt=0)  # допуск k, в сигмах
    outlier_sigma: float = Field(default=8.0, gt=0)  # |z| больше — грубый выброс: сумма сбрасывается (ловят другие детекторы)
    threshold_sigma: float = Field(default=14.0, gt=0)  # порог h, в сигмах (ASSUMPTION: при 1 Гц порог 5σ слишком чувствителен)


class TimeToThresholdConfig(_Cfg):
    """Прогноз времени до критического порога по линейному тренду."""

    horizon_s: float = Field(default=900.0, gt=0)  # предупреждать, если порог будет достигнут раньше
    min_r2: float = Field(default=0.7, ge=0, le=1)  # минимальное качество тренда
    window_min: float = Field(default=5.0, gt=0)  # окно тренда, минут
    min_points: int = Field(default=60, ge=3)  # минимум точек в окне


class PairDiffConfig(_Cfg):
    """Разность пар датчиков одной величины."""

    groups: list[list[str]] = Field(default_factory=list)  # группы плоских имён полей
    corridor_k: float = Field(default=4.0, gt=0)  # коридор pair_offset ± k·σ
    max_abs_diff: dict[str, float] = Field(default_factory=dict)  # жёсткий предел разности по группам
    max_diff_slope_per_min: dict[str, float] = Field(default_factory=dict)  # предел дрейфа разности
    slope_window_min: float = Field(default=3.0, gt=0)
    confirm_ticks: int = Field(default=3, ge=1)  # сколько тактов подряд нарушение должно держаться


class NoiseLossConfig(_Cfg):
    """Потеря шума датчика."""

    min_noise_ratio: float = Field(default=0.1, gt=0, lt=1)  # σ окна ниже этой доли baseline — тревога
    window_s: float = Field(default=60.0, gt=0)


class DetectorsConfig(_Cfg):
    """Общие параметры детекторов."""

    stale_warn_s: float = 5.0  # возраст данных для предупреждения
    stale_crit_s: float = 15.0  # возраст данных для критической тревоги
    comms_loss_s: float = 10.0  # без связи/свежих данных дольше — потеря связи
    cusum: CusumConfig = Field(default_factory=CusumConfig)
    time_to_threshold: TimeToThresholdConfig = Field(default_factory=TimeToThresholdConfig)
    pair_diff: PairDiffConfig = Field(default_factory=PairDiffConfig)
    noise_loss: NoiseLossConfig = Field(default_factory=NoiseLossConfig)
    freeze_s: dict[str, float] = Field(default_factory=dict)  # залипание: секунды по полю или группе (co2_ppm)
    freeze_flat_ratio: float = Field(default=0.5, gt=0)  # «не меняется» = размах окна ≤ доля·noise_level
    max_rate_per_s: dict[str, float] = Field(default_factory=dict)  # физический предел скорости по полю/группе
    ts_bad_ticks: int = Field(default=3, ge=1)  # сколько подряд плохих меток времени — тревога
    rate_noise_k: float = Field(default=3.0, ge=0)  # допуск на шум датчика при оценке скорости изменения


class BaselineConfig(_Cfg):
    """Калибровка на начальном отрезке: ТОЛЬКО шум, разность пар и шум невязки баланса."""

    window_s: float = Field(default=300.0, gt=0)  # длина начального отрезка (п. 6.5 регламента)
    update_rate: float = Field(default=0.01, ge=0, le=1)  # скорость скользящего обновления оценок
    subwindow_s: float = Field(default=30.0, gt=0)  # подокно для оценки шума после вычитания тренда
    noise_fields: list[str] = Field(
        default_factory=lambda: ["co2_ppm.*", "temp_c", "temp_c_2", "humidity_pct", "battery_temp_c", "battery_voltage_v"]
    )  # поля, для которых оценивается шум (шаблоны fnmatch)
    residual_window_s: float = Field(default=60.0, gt=0)  # окно невязки энергобаланса
    outlier_ratio: float = Field(default=3.0, gt=1)  # при обновлении отбрасывать оценки шума больше ratio·текущей
    min_subwindows: int = Field(default=3, ge=1)  # минимум подокон для оценки
    sigma_safety: float = Field(default=1.25, ge=1.0)  # запас на оценку σ невязки по малой выборке
    min_residuals: int = Field(default=3, ge=2)  # минимум окон невязки для оценки σ
    balance_sigma_floor_w: float = Field(default=25.0, ge=0)  # нижняя граница σ невязки (защита от гиперчувствительности)


class VerdictsConfig(_Cfg):
    """Накопление свидетельств и жизненный цикл вердиктов."""

    class Aggregation(_Cfg):
        bias: float = 0.0
        scale: float = Field(default=2.5, gt=0)  # ASSUMPTION: масштаб логистической суммы весов

    recheck_after_s: float = Field(default=60.0, gt=0)
    watch_after_false_s: float = Field(default=300.0, gt=0)
    aggregation: Aggregation = Field(default_factory=Aggregation)
    real_above: float = Field(default=0.6, gt=0.5, lt=1)  # p(real) выше — вердикт «реальный»
    false_below: float = Field(default=0.4, gt=0, lt=0.5)  # p(real) ниже — вердикт «ложный» того или иного вида
    evidence_half_life_s: float = Field(default=180.0, gt=0)  # затухание свидетельств со временем
    min_weight: float = Field(default=0.05, ge=0)  # свидетельство слабее этого отбрасывается
    max_confidence: float = Field(default=0.99, gt=0.5, le=1)  # потолок уверенности (абсолютной уверенности не бывает)
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "co2_outlier": 0.9,
            "co2_single_context": 0.6,
            "co2_pair_disagreement": 0.2,
            "co2_consensus_high": 0.8,
            "co2_consensus_rise": 0.6,
            "energy_balance": 0.6,
            "battery_temp_plausibility": 0.7,
            "device_status_vs_power": 0.5,
            "pv_drop": 0.8,
            "rate_limit": 0.7,
            "pair_diff": 0.6,
            "cusum": 0.6,
            "noise_loss": 0.6,
            "freeze": 0.6,
            "time_monotonic": 0.7,
            "thermal_balance": 0.6,
            "plan_vs_fact": 0.7,
            "cause_effect": 0.7,
            "context_work": 0.2,
            "probe": 0.8,
            "operator": 0.9,
            "video": 0.8,
        }
    )  # вес свидетельства по правилу/источнику (ASSUMPTION)

    @model_validator(mode="after")
    def _check(self) -> VerdictsConfig:
        """Веса в [0, 1], пороги согласованы."""
        for name, w in self.weights.items():
            if not 0.0 <= w <= 1.0:
                raise ValueError(f"вес '{name}' должен быть от 0 до 1")
        if self.false_below >= self.real_above:
            raise ValueError("false_below должен быть меньше real_above")
        return self


class ResponseConfig(_Cfg):
    """Политика реакции и защита от каскада."""

    prepare_confidence: float = Field(default=0.5, ge=0, le=1)  # от этой уверенности (или одного источника) — prepare
    reversible_confidence: float = Field(default=0.75, ge=0, le=1)  # act_reversible (или два независимых источника)
    full_confidence: float = Field(default=0.9, ge=0, le=1)  # act_full (и не менее двух независимых источников)
    min_sources_full: int = Field(default=2, ge=1)
    require_two_sources_for: list[str] = Field(default_factory=lambda: ["costly"])  # правило двух
    safety_override_events: list[str] = Field(default_factory=lambda: ["smoke", "solder_overheat", "operator_stop"])
    cascade_min_t_left_min: float = Field(default=20.0, ge=0)  # после действия запас энергии не должен быть меньше
    max_level_by_alert: dict[str, str] = Field(
        default_factory=lambda: {"info": "observe", "warning": "act_reversible", "critical": "act_full"}
    )  # потолок реакции по уровню тревоги


class ProbeCommandSpec(_Cfg):
    """Команда пробы (проходит только через Guard)."""

    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class ProbeSpec(_Cfg):
    """Проба из каталога конфига."""

    id: str
    description: str
    cost_wh: float = Field(default=0.0, ge=0)
    cost_s: float = Field(default=0.0, ge=0)
    reversible: bool = True
    risk: Literal["none", "low", "medium", "high"] = "none"
    expected_if_real: str
    expected_if_false: str
    duration_s: float = Field(default=0.0, ge=0)
    command: ProbeCommandSpec | None = None
    human_action: str | None = None
    applies_to: list[str] = Field(default_factory=list)
    evaluator: str = "none"  # имя оценщика результата в core/probes.py
    info_gain: float = Field(default=0.5, ge=0, le=1)  # насколько проба информативна (для выбора лучшей)

    @model_validator(mode="after")
    def _check(self) -> ProbeSpec:
        """У пробы есть действие или оценщик-сверка."""
        if self.command is None and self.human_action is None and self.evaluator == "none":
            raise ValueError("проба должна содержать command, human_action или evaluator")
        return self


class ProbesConfig(_Cfg):
    """Библиотека безопасных проб."""

    max_per_10min: int = Field(default=3, ge=0)
    energy_budget_wh: float = Field(default=5.0, ge=0)
    min_gap_s: float = Field(default=60.0, ge=0)
    catalog: list[ProbeSpec] = Field(default_factory=list)


class RoundConfig(_Cfg):
    """«Обход»: чек-лист сверки пар источников по таймеру."""

    interval_s: float = Field(default=420.0, gt=0)
    pairs: list[list[str]] = Field(default_factory=list)


class QualityConfig(_Cfg):
    """Допустимые показатели качества (проверяются тестами и benchmark)."""

    max_quiet_warnings_per_hour: float = Field(default=1.0, ge=0)


class EnergyConfig(_Cfg):
    """Параметры энергосистемы."""

    deficit_info_w: float = Field(default=400.0, ge=0)  # разряд АКБ мощнее — информационная тревога energy_deficit
    battery_capacity_wh: float = Field(gt=0)
    soc_min_pct: float = Field(ge=0, le=100)  # ниже этого заряда разряжать нельзя
    inverter_limit_w: float = Field(gt=0)
    t_left_warn_min: float = 30.0  # запас времени для предупреждения
    t_left_crit_min: float = 12.0  # запас времени для критической тревоги
    start_margin: float = Field(default=1.15, ge=1.0)  # запас энергии при проверке «можно ли начать операцию»
    eco_saving_fraction: float = Field(default=0.5, ge=0, le=1)  # ASSUMPTION: доля экономии eco-режима на освещении и розетках
    avg_window_s: float = Field(default=60.0, gt=0)  # окно усреднения генерации и нагрузки для расчётов
    charge_efficiency: float = Field(default=0.97, gt=0, le=1)  # ASSUMPTION: КПД заряда АКБ (для ожидаемой невязки)
    discharge_efficiency: float = Field(default=0.97, gt=0, le=1)  # ASSUMPTION: КПД разряда
    balance_bias_w: float = 0.0  # ASSUMPTION: постоянное смещение баланса (собственное потребление BMS и т.п.)


class LineSpec(_Cfg):
    """Линия распределительного щита."""

    nominal_w: float = Field(ge=0)  # номинальная мощность при работе
    description: str = ""


class EquipmentConfig(_Cfg):
    """Мощности и привязки оборудования."""

    lines: dict[str, LineSpec]
    device_lines: dict[str, str]  # поле статуса устройства -> имя линии (cnc_status -> cnc)
    vent_power_w: dict[str, float]  # режим вытяжки -> потребляемая мощность
    humidifier_power_w: dict[str, float]  # режим увлажнителя -> мощность

    @model_validator(mode="after")
    def _check_refs(self) -> EquipmentConfig:
        """Привязки устройств указывают на существующие линии."""
        for status_field, line in self.device_lines.items():
            if line not in self.lines:
                raise ValueError(f"device_lines[{status_field}] ссылается на неизвестную линию '{line}'")
        return self


class Operation(_Cfg):
    """Этап технологической карты."""

    name: str
    duration_min: float = Field(gt=0)
    power_w: float = Field(ge=0)  # мощность этапа поверх базовой нагрузки
    interruptible: bool = True  # можно ли прервать без потери результата
    needs_vent: bool = False  # требуется ли вытяжка (пайка: обязательно, регламент п. 9.1)
    device_line: str | None = None  # линия оборудования этапа
    depends_on: list[str] = Field(default_factory=list)


class ProcessConfig(_Cfg):
    """Технологическая карта изделия."""

    operations: list[Operation]
    vent_mode_for_vent_ops: str = "low"  # режим вытяжки, который планировщик закладывает в расчёт

    @model_validator(mode="after")
    def _check_deps(self) -> ProcessConfig:
        """Зависимости этапов ссылаются на существующие этапы."""
        names = {op.name for op in self.operations}
        if len(names) != len(self.operations):
            raise ValueError("имена этапов должны быть уникальны")
        for op in self.operations:
            for dep in op.depends_on:
                if dep not in names:
                    raise ValueError(f"этап '{op.name}' зависит от неизвестного '{dep}'")
        return self


class ArgRule(_Cfg):
    """Правило допустимости аргумента команды."""

    type: Literal["enum", "bool", "number"]
    values: list[Any] = Field(default_factory=list)  # для enum
    min: float | None = None  # для number
    max: float | None = None  # для number

    @model_validator(mode="after")
    def _check(self) -> ArgRule:
        """enum требует значений, number — корректный диапазон."""
        if self.type == "enum" and not self.values:
            raise ValueError("для enum нужен непустой список values")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min больше max")
        return self


class VerifyRule(_Cfg):
    """Как проверить, что команда подействовала.

    `path` — путь в телеметрии (`Telemetry.lookup`), в котором допустимы
    подстановки `{имя_аргумента}`; ожидаемое значение берётся из аргумента
    `equals_arg` или задаётся константой `equals`.
    """

    path: str
    equals_arg: str | None = None
    equals: Any = None


class RateLimit(_Cfg):
    """Ограничение частоты команд одному исполнителю."""

    max_commands: int = Field(default=3, ge=1)
    window_s: float = Field(default=60.0, gt=0)


class CommandRule(_Cfg):
    """Правило для команды из белого списка."""

    klass: Literal["auto", "needs_human"] = Field(alias="class")
    executor: str  # ключ исполнителя для ограничения частоты, допускает {аргументы}
    args: dict[str, ArgRule] = Field(default_factory=dict)
    verify: VerifyRule | None = None
    rate_limit: RateLimit | None = None
    description: str = ""
    costly: bool = False  # дорогое/необратимое действие: нужны два независимых свидетельства
    power_path: str | None = None  # путь мощности линии исполнителя: второй канал отклика после команды

    @model_validator(mode="after")
    def _check_verify(self) -> CommandRule:
        """`equals_arg` должен быть аргументом команды."""
        if self.verify and self.verify.equals_arg and self.verify.equals_arg not in self.args:
            raise ValueError(f"verify.equals_arg='{self.verify.equals_arg}' не объявлен в args")
        return self


class GuardConfig(_Cfg):
    """Настройки предохранителя команд."""

    forbidden: list[str]  # подстроки (без регистра) в имени/аргументах команды: всегда запрет
    commands: dict[str, CommandRule]  # белый список
    default_rate_limit: RateLimit = Field(default_factory=RateLimit)
    verify_timeout_s: float = Field(default=10.0, gt=0)
    pending_ttl_s: float = Field(default=300.0, gt=0)  # сколько ждать подтверждения человека
    late_effect_window_s: float = Field(default=120.0, ge=0)  # сколько ещё ждать запоздалый эффект после no_effect


class CrosscheckConfig(_Cfg):
    """Параметры сверки источников."""

    co2_sensor_max_diff_ppm: float = 300.0  # расхождение датчиков CO2 → подозрение на сбой одного
    co2_false_jump_ppm: float = 1000.0  # скачок за один такт, характерный для ложного сигнала
    co2_high_ppm: float = 1500.0  # «высокий» CO2 для контекстных правил
    temp_normal_max_c: float = 30.0  # выше — температура не «в норме»
    humidity_normal_range: tuple[float, float] = (25.0, 75.0)
    balance_window_s: float = 120.0  # окно сравнения изменения заряда АКБ с P_pv - P_load
    balance_eps_w: float = 200.0  # допуск расхождения энергобаланса, Вт
    balance_confirm_s: float = 45.0  # сколько секунд расхождение должно держаться до вердикта
    device_running_min_w: float = 30.0  # «running» с меньшим потреблением — несоответствие
    device_idle_max_w: float = 40.0  # «idle» с большим потреблением — несоответствие
    device_confirm_s: float = 20.0  # сколько секунд несоответствие должно держаться
    battery_temp_slope_per_kw: float = 0.3  # правдоподобный рост температуры АКБ, °C/мин на каждый кВт тока
    pv_drop_ratio: float = 0.6  # генерация ниже этой доли от прежней — «падение»
    plan_vs_fact_min_ratio: float = Field(default=0.6, gt=0, lt=1)  # потребление ниже доли плана при running — тихий брак
    plan_vs_fact_confirm_s: float = Field(default=30.0, gt=0)
    plan_vs_fact_ramp_s: float = Field(default=15.0, ge=0)  # после старта устройства потребление выходит на режим
    thermal_idle_slope_per_min: float = Field(default=0.15, gt=0)  # рост температуры при простое — подозрительно
    thermal_window_min: float = Field(default=3.0, gt=0)
    co2_rise_ppm_per_min: float = Field(default=30.0, gt=0)  # согласованный рост на всех датчиках CO2 быстрее — реальный рост
    co2_rise_window_min: float = Field(default=2.0, gt=0)
    cause_effect_window_s: float = Field(default=30.0, gt=0)  # сколько ждать отклика в каналах после команды
    cause_effect_min_delta_w: float = Field(default=20.0, ge=0)  # ожидаемое изменение мощности линии после команды


class LLMConfig(_Cfg):
    """Настройки LLM (в игре только DeepSeek; список моделей — от организаторов, см. ``allowed_models``)."""

    mode: Literal["mock", "dev", "competition"] = "mock"
    model: str = "deepseek-v4-flash"  # ASSUMPTION: фактический id модели сверить с документацией API
    # модели, допустимые в competition (Dome_RAG/03_Требования: deepseek-chat и deepseek-reasoner; v4-flash оставлена из регламента)
    allowed_models: list[str] = Field(default_factory=lambda: ["deepseek-chat", "deepseek-reasoner", "deepseek-v4-flash"])
    base_url: str = "https://api.deepseek.com"  # ASSUMPTION: OpenAI-совместимый endpoint
    api_key_env: str = "DEEPSEEK_API_KEY"
    timeout_s: float = Field(default=20.0, gt=0)
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_tokens: int = Field(default=900, ge=16)

    @model_validator(mode="after")
    def _check_competition(self) -> LLMConfig:
        """В боевом режиме чужая модель недопустима уже на этапе загрузки конфига."""
        if self.mode == "competition" and self.model not in self.allowed_models:
            raise ValueError(
                f"режим competition допускает только модели {', '.join(self.allowed_models)}, задано '{self.model}'"
            )
        return self


class AgentsConfig(_Cfg):
    """Общие настройки агентов."""

    min_interval_s: float = Field(default=30.0, ge=0)  # не чаще вызывать LLM одним агентом
    intervals: dict[str, float] = Field(default_factory=lambda: {"diagnost": 90.0})  # индивидуальные интервалы, с
    enabled: dict[str, bool] = Field(
        default_factory=lambda: {"monitor": True, "diagnost": True, "energy_planner": True}
    )
    recommendation_ttl_s: float = Field(default=600.0, gt=0)
    plan_interval_s: float = Field(default=300.0, gt=0)  # как часто Энергопланировщик обновляет план без тревог


class PathsConfig(_Cfg):
    """Пути к данным."""

    logs_dir: str = "logs"


class RealAdapterConfig(_Cfg):
    """Параметры реального адаптера (`adapter/real.py`). Все значения — ASSUMPTION: утром заменить."""

    base_url: str = "http://kupol.local/api"  # ASSUMPTION: адрес API Комплекса
    timeout_s: float = Field(default=3.0, gt=0)  # таймаут одного запроса
    retries: int = Field(default=2, ge=0)  # повторов после первой попытки
    telemetry_path: str = "/telemetry"  # ASSUMPTION
    history_path: str = "/history"  # ASSUMPTION
    command_path: str = "/command"  # ASSUMPTION
    token_env: str = "KUPOL_API_TOKEN"  # переменная окружения с токеном доступа (если нужен)


class SimulatorConfig(_Cfg):
    """Параметры симулятора (используются только им, на реальном Комплексе не нужны)."""

    pv_peak_w: float = 2000.0  # пик генерации при безоблачном небе
    cloud_cover: float = Field(default=0.1, ge=0, le=1)  # 0 — ясно, 1 — сплошная облачность
    initial_soc_pct: float = 80.0
    occupants: int = 1  # людей в помещении (источник CO2)
    co2_outdoor_ppm: float = 420.0
    co2_per_person_ppm_s: float = 0.133  # прирост CO2 на человека
    co2_leak_rate_s: float = 0.0003  # естественный воздухообмен
    co2_vent_rate_s: dict[str, float] = Field(
        default_factory=lambda: {"off": 0.0, "low": 0.0008, "high": 0.002, "auto": 0.0008}
    )
    humidity_base_pct: float = 45.0
    humidifier_gain_pct_s: dict[str, float] = Field(
        default_factory=lambda: {"off": 0.0, "low": 0.004, "high": 0.01, "auto": 0.004}
    )
    humidity_relax_s: float = 0.0004  # возврат влажности к базовой
    temp_base_c: float = 22.0
    temp_relax_s: float = 0.0006
    heat_w_per_deg_s: dict[str, float] = Field(
        default_factory=lambda: {"cnc": 0.0010, "printer": 0.0006, "solder": 0.0008}
    )
    battery_temp_base_c: float = 25.0
    noise: bool = True
    actuator_delay_s: float = 3.0  # задержка исполнения команды
    actuator_fail_prob: float = Field(default=0.0, ge=0, le=1)  # случайный отказ исполнителя
    device_start_delay_s: float = 5.0  # время выхода устройства на режим


class AppConfig(_Cfg):
    """Корневая конфигурация Штаба."""

    game: GameConfig = Field(default_factory=GameConfig)
    thresholds: dict[str, FieldThreshold] = Field(default_factory=dict)
    detectors: DetectorsConfig = Field(default_factory=DetectorsConfig)
    energy: EnergyConfig
    equipment: EquipmentConfig
    process: ProcessConfig
    guard: GuardConfig
    crosscheck: CrosscheckConfig = Field(default_factory=CrosscheckConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    simulator: SimulatorConfig = Field(default_factory=SimulatorConfig)
    real: RealAdapterConfig = Field(default_factory=RealAdapterConfig)
    baseline: BaselineConfig = Field(default_factory=BaselineConfig)
    verdicts: VerdictsConfig = Field(default_factory=VerdictsConfig)
    response: ResponseConfig = Field(default_factory=ResponseConfig)
    probes: ProbesConfig = Field(default_factory=ProbesConfig)
    round: RoundConfig = Field(default_factory=RoundConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)

    @model_validator(mode="after")
    def _check_cross_refs(self) -> AppConfig:
        """Проверки согласованности между разделами."""
        for op in self.process.operations:
            if op.device_line and op.device_line not in self.equipment.lines:
                raise ValueError(f"этап '{op.name}' использует неизвестную линию '{op.device_line}'")
        return self

    def thresholds_for(self, field: str) -> FieldThreshold | None:
        """Найти пороги для поля: точное имя, затем шаблон (`co2_ppm.*`).

        Из нескольких подходящих шаблонов выбирается самый длинный (конкретный).

        Args:
            field: Плоское имя поля телеметрии.

        Returns:
            Пороги или `None`, если поле не настроено.
        """
        if field in self.thresholds:
            return self.thresholds[field]
        matches = [p for p in self.thresholds if fnmatch.fnmatchcase(field, p)]
        if not matches:
            return None
        return self.thresholds[max(matches, key=len)]


def load_config(path: str | Path) -> AppConfig:
    """Загрузить и проверить конфигурацию.

    Args:
        path: Путь к `config.yaml`.

    Returns:
        Проверенная конфигурация.

    Raises:
        ConfigError: Файл не найден, YAML некорректен или значения не прошли проверку.
            Сообщение перечисляет все проблемы с путями к ключам.
    """
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Файл конфигурации не найден: {p}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Некорректный YAML в {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{p}: корень конфигурации должен быть словарём")
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        lines = [f"Ошибки в конфигурации {p}:"]
        for err in exc.errors():
            loc = ".".join(str(x) for x in err["loc"]) or "<корень>"
            lines.append(f"  - {loc}: {err['msg']}")
        raise ConfigError("\n".join(lines)) from exc
