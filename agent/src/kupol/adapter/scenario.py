"""Модели и загрузка YAML-сценариев кризисных воздействий для симулятора.

Все времена — секунды от старта игры. Сценарий — единая шкала времени:
кризисные воздействия и «действия Оператора» (запуск устройств, ручное
включение вытяжки) описываются одинаково, поэтому каскады могут включать и те,
и другие.

Типы воздействий (регламент п. 7.4: прямые, скрытые, ложные, каскадные):

* ``direct``        — реально меняет состояние (генерация, нагрузка, люди);
* ``hidden_drift``  — дрейф показаний датчика без реального изменения;
* ``false_signal``  — ложное показание одного датчика, остальные в норме;
* ``cascade``       — цепочка вторичных эффектов (вложенные события со смещением);
* ``comms_loss``    — пропадание телеметрии (данные застывают);
* ``actuator_fail`` — исполнитель принимает команды, но не исполняет;
* ``device_fault``  — устройство переходит в состояние ``error``;
* ``operator``      — штатное действие Оператора (не кризис).

Скрытые и «коварные» события (дополнение к ТЗ): ``hidden_load``, ``sensor_drift_pair``,
``silent_print_fail``, ``frozen_sensor``, ``noise_loss``, ``stale_timestamp``, ``delayed_actuator``,
а также составные ``false_then_real`` и ``cascade_trap``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ScenarioError(Exception):
    """Сценарий некорректен."""


class _Ev(BaseModel):
    """Базовое событие: время старта и необязательное описание."""

    model_config = ConfigDict(extra="forbid")

    at: float = Field(ge=0)  # секунды от старта (внутри каскада — смещение от его старта)
    note: str = ""


class DirectEvent(_Ev):
    """Прямое воздействие: реально меняет состояние системы.

    Ровно один из ``factor`` / ``extra_w`` / ``delta`` должен соответствовать цели:
    ``pv`` → ``factor`` (множитель генерации), ``load`` → ``extra_w`` (доп. нагрузка, Вт),
    ``occupants`` → ``delta`` (изменение числа людей, источник реального роста CO2),
    ``co2`` → ``extra_ppm_s`` (прямой источник CO2, например пары при пайке).
    """

    type: Literal["direct"] = "direct"
    target: Literal["pv", "load", "occupants", "co2"]
    factor: float | None = Field(default=None, gt=0)
    extra_w: float | None = None
    delta: float | None = None
    extra_ppm_s: float | None = Field(default=None, gt=0)  # цель co2: реальный источник CO2, ppm/с
    duration_s: float | None = Field(default=None, gt=0)  # None — навсегда

    @model_validator(mode="after")
    def _check_target(self) -> DirectEvent:
        """Для цели задан соответствующий параметр."""
        need = {"pv": self.factor, "load": self.extra_w, "occupants": self.delta, "co2": self.extra_ppm_s}[self.target]
        if need is None:
            names = {"pv": "factor", "load": "extra_w", "occupants": "delta", "co2": "extra_ppm_s"}
            raise ValueError(f"для direct/{self.target} нужен параметр {names[self.target]}")
        return self


class HiddenDriftEvent(_Ev):
    """Скрытый дрейф показаний: ``sensor`` (плоское имя поля) смещается на ``rate_per_min`` в минуту."""

    type: Literal["hidden_drift"] = "hidden_drift"
    sensor: str
    rate_per_min: float
    duration_s: float | None = Field(default=None, gt=0)


class FalseSignalEvent(_Ev):
    """Ложный сигнал: датчик ``sensor`` показывает ``value`` в течение ``duration_s``."""

    type: Literal["false_signal"] = "false_signal"
    sensor: str
    value: float
    duration_s: float = Field(gt=0)


class CommsLossEvent(_Ev):
    """Потеря связи: данные застывают (режим ``frozen``) или запросы падают по таймауту."""

    type: Literal["comms_loss"] = "comms_loss"
    duration_s: float = Field(gt=0)
    mode: Literal["frozen", "timeout"] = "frozen"


class ActuatorFailEvent(_Ev):
    """Отказ исполнителя: команда принимается, состояние не меняется."""

    type: Literal["actuator_fail"] = "actuator_fail"
    target: Literal["vent", "humidifier", "line", "power", "all"]
    duration_s: float | None = Field(default=None, gt=0)


class DeviceFaultEvent(_Ev):
    """Неисправность устройства: статус ``error`` (останов, засор и т.п.)."""

    type: Literal["device_fault"] = "device_fault"
    device: Literal["cnc", "printer", "solder"]
    duration_s: float | None = Field(default=None, gt=0)


class OperatorEvent(_Ev):
    """Действие Оператора (штатное): запуск/остановка устройств и ручные режимы."""

    type: Literal["operator"] = "operator"
    action: Literal["start_device", "stop_device", "set_vent", "set_humidifier", "set_line"]
    device: Literal["cnc", "printer", "solder"] | None = None
    line: str | None = None
    mode: str | None = None
    enabled: bool | None = None
    duration_s: float | None = Field(default=None, gt=0)  # для start_device: сколько работать

    @model_validator(mode="after")
    def _check_action(self) -> OperatorEvent:
        """Для действия заданы нужные параметры."""
        a = self.action
        if a in ("start_device", "stop_device") and self.device is None:
            raise ValueError(f"operator/{a} требует device")
        if a == "start_device" and self.duration_s is None:
            raise ValueError("operator/start_device требует duration_s")
        if a in ("set_vent", "set_humidifier") and self.mode is None:
            raise ValueError(f"operator/{a} требует mode")
        if a == "set_line" and (self.line is None or self.enabled is None):
            raise ValueError("operator/set_line требует line и enabled")
        return self


class HiddenLoadEvent(_Ev):
    """Неучтённая нагрузка: заряжает/разряжает АКБ, но отсутствует в телеметрии потребления."""

    type: Literal["hidden_load"] = "hidden_load"
    watts: float = Field(gt=0)
    duration_s: float | None = Field(default=None, gt=0)


class SensorDriftPairEvent(_Ev):
    """Медленный дрейф одного из двух датчиков одной величины (физика не меняется)."""

    type: Literal["sensor_drift_pair"] = "sensor_drift_pair"
    sensor: str
    rate_per_min: float
    duration_s: float | None = Field(default=None, gt=0)


class SilentPrintFailEvent(_Ev):
    """Тихий брак: устройство в статусе ``running``, но работа не идёт (потребление ниже плана)."""

    type: Literal["silent_print_fail"] = "silent_print_fail"
    device: Literal["cnc", "printer", "solder"] = "printer"
    power_factor: float = Field(default=0.35, ge=0.0, lt=1.0)  # доля номинального потребления
    duration_s: float | None = Field(default=None, gt=0)


class FrozenSensorEvent(_Ev):
    """Залипание датчика: показание застывает на последнем значении, шум пропадает."""

    type: Literal["frozen_sensor"] = "frozen_sensor"
    sensor: str
    duration_s: float | None = Field(default=None, gt=0)


class NoiseLossEvent(_Ev):
    """Потеря шума: показание следует за физикой, но шум почти исчезает."""

    type: Literal["noise_loss"] = "noise_loss"
    sensor: str
    noise_factor: float = Field(default=0.02, ge=0.0, lt=1.0)  # во сколько раз остаётся шум
    duration_s: float | None = Field(default=None, gt=0)


class StaleTimestampEvent(_Ev):
    """Метка времени снимков перестаёт идти (``freeze``) или откатывается (``rollback``)."""

    type: Literal["stale_timestamp"] = "stale_timestamp"
    mode: Literal["freeze", "rollback"] = "freeze"
    rollback_s: float = Field(default=30.0, gt=0)
    duration_s: float = Field(gt=0)


class DelayedActuatorEvent(_Ev):
    """Исполнитель срабатывает с большой задержкой (команда принята, эффект через ``delay_s``)."""

    type: Literal["delayed_actuator"] = "delayed_actuator"
    target: Literal["vent", "humidifier", "line", "power", "all"]
    delay_s: float = Field(default=40.0, gt=0)
    duration_s: float | None = Field(default=None, gt=0)


LeafEvent = Annotated[
    DirectEvent
    | HiddenDriftEvent
    | FalseSignalEvent
    | CommsLossEvent
    | ActuatorFailEvent
    | DeviceFaultEvent
    | OperatorEvent
    | HiddenLoadEvent
    | SensorDriftPairEvent
    | SilentPrintFailEvent
    | FrozenSensorEvent
    | NoiseLossEvent
    | StaleTimestampEvent
    | DelayedActuatorEvent,
    Field(discriminator="type"),
]


class CascadeEvent(_Ev):
    """Каскад: ``steps`` — вложенные события, ``at`` которых отсчитывается от старта каскада."""

    type: Literal["cascade"] = "cascade"
    steps: list[LeafEvent] = Field(min_length=1)


class FalseThenRealEvent(_Ev):
    """Ложный сигнал CO2, а через ``real_after_s`` — настоящий рост CO2 (например, пайка).

    Проверяет, что вердикт «ложный» не закрывается навсегда: когда приходят подтверждающие
    свидетельства реального роста, вердикт пересматривается.
    """

    type: Literal["false_then_real"] = "false_then_real"
    sensor: str = "co2_ppm.co2_b"
    false_value: float = 5000.0
    false_duration_s: float = Field(default=40.0, gt=0)
    real_after_s: float = Field(default=120.0, gt=0)
    real_ppm_s: float = Field(default=1.5, gt=0)  # скорость реального роста CO2, ppm/с
    real_duration_s: float = Field(default=600.0, gt=0)
    solder_s: float | None = 600.0  # пайка в момент реального роста (контекст работ); None — без пайки


class CascadeTrapEvent(_Ev):
    """Ложный сигнал, «естественная» реакция на который (вытяжка на максимум) вредна.

    ``harmful`` — действия, которые считаются каскадной ошибкой (``команда:режим``);
    метрика «каскады» считает их выполнение в течение ``window_s`` после начала события.
    """

    type: Literal["cascade_trap"] = "cascade_trap"
    sensor: str = "co2_ppm.co2_b"
    value: float = 5000.0
    duration_s: float = Field(default=90.0, gt=0)
    harmful: list[str] = Field(default_factory=lambda: ["set_vent_mode:high", "vent_enable:high"])
    window_s: float = Field(default=600.0, gt=0)


ScenarioEvent = Annotated[
    DirectEvent
    | HiddenDriftEvent
    | FalseSignalEvent
    | CommsLossEvent
    | ActuatorFailEvent
    | DeviceFaultEvent
    | OperatorEvent
    | HiddenLoadEvent
    | SensorDriftPairEvent
    | SilentPrintFailEvent
    | FrozenSensorEvent
    | NoiseLossEvent
    | StaleTimestampEvent
    | DelayedActuatorEvent
    | CascadeEvent
    | FalseThenRealEvent
    | CascadeTrapEvent,
    Field(discriminator="type"),
]


class Scenario(BaseModel):
    """Сценарий симулятора.

    Attributes:
        name: Имя сценария.
        description: Что проверяет сценарий.
        duration_s: Длительность игры, с.
        initial: Переопределения ``simulator:`` из ``config.yaml`` для этого сценария.
        events: Единая шкала времени событий.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    duration_s: float = Field(default=5400.0, gt=0)
    initial: dict[str, Any] = Field(default_factory=dict)
    events: list[ScenarioEvent] = Field(default_factory=list)

    def flattened(self) -> list[tuple[float, Any, str]]:
        """Развернуть каскады в плоский список ``(время, событие, источник)``.

        Сам каскад тоже попадает в список (как маркер начала для журнала правды).

        Returns:
            События по возрастанию времени. ``источник`` — ``cascade:<n>`` для
            вложенных событий или ``top`` для верхнего уровня.
        """
        out: list[tuple[float, Any, str]] = []
        for i, ev in enumerate(self.events):
            if isinstance(ev, CascadeEvent):
                for step in ev.steps:
                    out.append((ev.at + step.at, step, f"cascade:{i}"))
                out.append((ev.at, ev, "top"))
            elif isinstance(ev, FalseThenRealEvent):
                src = f"false_then_real:{i}"
                real_at = ev.at + ev.real_after_s
                out.append((ev.at, ev, "top"))
                out.append((ev.at, FalseSignalEvent(at=0, sensor=ev.sensor, value=ev.false_value, duration_s=ev.false_duration_s, note=ev.note), src))
                if ev.solder_s is not None:
                    out.append((real_at, OperatorEvent(at=0, action="start_device", device="solder", duration_s=ev.solder_s), src + ":ctx"))
                out.append((real_at, DirectEvent(at=0, target="co2", extra_ppm_s=ev.real_ppm_s, duration_s=ev.real_duration_s, note=ev.note), src + ":real"))
            elif isinstance(ev, CascadeTrapEvent):
                out.append((ev.at, ev, "top"))
                out.append((ev.at, FalseSignalEvent(at=0, sensor=ev.sensor, value=ev.value, duration_s=ev.duration_s, note=ev.note), f"cascade_trap:{i}"))
            else:
                out.append((ev.at, ev, "top"))
        out.sort(key=lambda x: x[0])
        return out


def load_scenario(path: str | Path) -> Scenario:
    """Загрузить сценарий из YAML.

    Args:
        path: Путь к файлу сценария.

    Returns:
        Проверенный сценарий.

    Raises:
        ScenarioError: Файл не найден, YAML некорректен или значения не прошли проверку.
    """
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScenarioError(f"Сценарий не найден: {p}") from exc
    except yaml.YAMLError as exc:
        raise ScenarioError(f"Некорректный YAML сценария {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ScenarioError(f"{p}: корень сценария должен быть словарём")
    try:
        return Scenario.model_validate(raw)
    except ValidationError as exc:
        lines = [f"Ошибки в сценарии {p}:"]
        for err in exc.errors():
            loc = ".".join(str(x) for x in err["loc"]) or "<корень>"
            lines.append(f"  - {loc}: {err['msg']}")
        raise ScenarioError("\n".join(lines)) from exc
