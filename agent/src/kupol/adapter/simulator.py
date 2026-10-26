"""Симулятор Комплекса «Купол».

Позволяет разрабатывать и тренироваться без доступа к настоящему Комплексу.
Моделирует энергобаланс, микроклимат, производственные устройства (конечные
автоматы), исполнители с задержкой и отказами, и вбрасывает события сценария.

Принципы:

* Время виртуальное: ``advance(dt)`` продвигает мир шагами по 1 с; ``now()`` —
  стартовое время плюс прошедшее. Ускорение (``--speed``) — забота главного цикла.
* Детерминизм: единственный источник случайности — ``random.Random(seed)``;
  шум считается один раз за шаг в ``advance``, а не при чтении телеметрии, поэтому
  результат не зависит от числа вызовов ``get_telemetry``.
* «Правда» и «показания» разделены: события ``hidden_drift`` и ``false_signal``
  портят только показания, физическое состояние остаётся прежним
  (проверяется через ``truth()``).

Все физические константы условны (ASSUMPTION) и лежат в ``config.yaml``
(раздел ``simulator``) и в сценарии (``initial``).
"""

from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from kupol.adapter.base import AdapterError, AdapterTimeout, KupolAdapter
from kupol.adapter.scenario import (
    ActuatorFailEvent,
    CascadeEvent,
    CascadeTrapEvent,
    CommsLossEvent,
    DelayedActuatorEvent,
    DeviceFaultEvent,
    DirectEvent,
    FalseSignalEvent,
    FalseThenRealEvent,
    FrozenSensorEvent,
    HiddenDriftEvent,
    HiddenLoadEvent,
    NoiseLossEvent,
    OperatorEvent,
    Scenario,
    ScenarioError,
    SensorDriftPairEvent,
    SilentPrintFailEvent,
    StaleTimestampEvent,
)
from kupol.core.config import AppConfig, SimulatorConfig
from kupol.core.models import (
    Command,
    CommandResult,
    CommandStatus,
    DeviceStatus,
    LineState,
    Telemetry,
)

SIM_START = datetime(2026, 10, 27, 10, 0, 0, tzinfo=UTC)
SIM_STEP_S = 1.0
STANDBY_W = 8.0  # потребление включённого, но простаивающего устройства
CO2_SENSORS = {"co2_a": 0.0, "co2_b": 8.0, "co2_c": -6.0}  # датчик -> систематический сдвиг, ppm
TEMP2_BIAS_C = 0.15  # систематический сдвиг второго датчика температуры
DEVICES = ("cnc", "printer", "solder")
HISTORY_MINUTES = 60

# Какому исполнителю принадлежит команда (для actuator_fail).
COMMAND_TARGET = {
    "vent_enable": "vent",
    "set_vent_mode": "vent",
    "set_humidifier_mode": "humidifier",
    "set_line": "line",
    "set_power_mode": "power",
}


@dataclass
class _Timed:
    """Числовой эффект с необязательным временем окончания."""

    value: float
    end: float | None


@dataclass
class _Drift:
    """Активный дрейф показаний датчика."""

    sensor: str
    rate_per_min: float
    start: float
    end: float | None


@dataclass
class _FalseSignal:
    """Активное ложное показание датчика."""

    sensor: str
    value: float
    end: float


@dataclass
class _Frozen:
    """Залипший датчик."""

    sensor: str
    end: float | None
    value: float | None = None


@dataclass
class _NoiseLoss:
    """Датчик с пропавшим шумом."""

    sensor: str
    factor: float
    end: float | None


@dataclass
class _StaleTs:
    """Нарушение метки времени снимков."""

    mode: str
    rollback_s: float
    start: float
    end: float


@dataclass
class _Delayed:
    """Исполнитель с большой задержкой."""

    target: str
    delay_s: float
    end: float | None


@dataclass
class _SilentFail:
    """Тихий брак устройства."""

    device: str
    factor: float
    end: float | None


@dataclass
class _Pending:
    """Принятая, но ещё не исполненная команда."""

    apply_at: float
    cmd: Command


@dataclass
class _Device:
    """Конечный автомат производственного устройства."""

    name: str
    state: DeviceStatus = DeviceStatus.IDLE
    remaining_s: float = 0.0
    run_s: float = 0.0


@dataclass
class _World:
    """Физическое состояние («правда»)."""

    soc: float
    battery_temp: float
    co2: float
    humidity: float
    temp: float
    vent_mode: str = "off"
    humidifier_mode: str = "off"
    power_mode: str = "normal"
    lines_on: dict[str, bool] = dc_field(default_factory=dict)


class SimulatorAdapter(KupolAdapter):
    """Симулятор Комплекса с единым интерфейсом ``KupolAdapter``."""

    def __init__(
        self,
        config: AppConfig,
        scenario: Scenario | None = None,
        seed: int = 42,
        start: datetime = SIM_START,
    ) -> None:
        """Создать симулятор.

        Args:
            config: Конфигурация Штаба (мощности, ёмкость АКБ, параметры симулятора).
            scenario: Сценарий событий; ``None`` — тихий мир без событий.
            seed: Начальное значение генератора случайных чисел (детерминизм).
            start: Стартовое виртуальное время (UTC).

        Raises:
            ScenarioError: В сценарии указан неизвестный датчик, линия или параметр.
        """
        self.config = config
        self.scenario = scenario or Scenario(name="empty")
        self.seed = seed
        self._start = start
        self._rng = random.Random(seed)
        self.t = 0.0
        try:
            merged = {**config.simulator.model_dump(), **self.scenario.initial}
            self.sim: SimulatorConfig = SimulatorConfig.model_validate(merged)
        except ValueError as exc:
            raise ScenarioError(f"Некорректный раздел initial сценария: {exc}") from exc

        lines = config.equipment.lines
        self._w = _World(
            soc=self.sim.initial_soc_pct,
            battery_temp=self.sim.battery_temp_base_c,
            co2=self.sim.co2_outdoor_ppm + 430.0,
            humidity=self.sim.humidity_base_pct,
            temp=self.sim.temp_base_c,
            lines_on={name: True for name in lines},
        )
        self._devices = {name: _Device(name) for name in DEVICES}
        self._events = self.scenario.flattened()
        self._next_event = 0

        self._pv_factors: list[_Timed] = []
        self._extra_loads: list[_Timed] = []
        self._occupant_deltas: list[_Timed] = []
        self._drifts: list[_Drift] = []
        self._false: list[_FalseSignal] = []
        self._act_fail: list[tuple[str, float | None]] = []
        self._faults: dict[str, float | None] = {}
        self._comms_until: float | None = None
        self._comms_mode = "frozen"
        self._pending: list[_Pending] = []
        self._hidden_loads: list[_Timed] = []
        self._co2_sources: list[_Timed] = []
        self._frozen: list[_Frozen] = []
        self._noise_loss: list[_NoiseLoss] = []
        self._stale_ts: _StaleTs | None = None
        self._delayed: list[_Delayed] = []
        self._silent_fail: list[_SilentFail] = []
        self._below_min_logged = False
        self._validate_scenario()

        self.ground_truth: list[dict[str, Any]] = []
        self.event_log: list[dict[str, Any]] = []
        self._history: deque[tuple[datetime, dict[str, float]]] = deque()
        self._last_good: Telemetry | None = None
        self._last_good_t = 0.0
        self._reported = self._build_snapshot()
        self._last_good = self._reported
        self._record_history(self._reported)

    # ------------------------------------------------------------------ интерфейс

    @property
    def duration_s(self) -> float:
        """Длительность игры по сценарию, с."""
        return self.scenario.duration_s

    def now(self) -> datetime:
        """Виртуальное время (UTC)."""
        return self._start + timedelta(seconds=self.t)

    def advance(self, dt_s: float) -> None:
        """Продвинуть симуляцию на ``dt_s`` секунд шагами не более 1 с.

        Args:
            dt_s: Длительность, секунд (неположительная игнорируется).
        """
        remaining = dt_s
        while remaining > 1e-9:
            step = min(SIM_STEP_S, remaining)
            self._step(step)
            remaining -= step

    def get_telemetry(self) -> Telemetry:
        """Вернуть текущие показания (не продвигает время).

        Returns:
            Снимок; при потере связи в режиме ``frozen`` — застывшие данные с
            ``comms_ok=False`` и растущим ``data_age_s``.

        Raises:
            AdapterTimeout: Потеря связи в режиме ``timeout``.
        """
        if self._comms_active() and self._comms_mode == "timeout":
            raise AdapterTimeout("симулятор: связь потеряна (режим timeout)")
        return self._reported

    def get_history(self, field: str, minutes: int) -> list[tuple[datetime, float]]:
        """Вернуть историю показаний поля за последние ``minutes`` минут.

        Args:
            field: Плоское имя поля (``Telemetry.flat``).
            minutes: Глубина, минут.

        Returns:
            Пары ``(время, значение)`` по возрастанию времени.

        Raises:
            AdapterTimeout: Потеря связи в режиме ``timeout``.
            AdapterError: Неизвестное поле.
        """
        if self._comms_active() and self._comms_mode == "timeout":
            raise AdapterTimeout("симулятор: связь потеряна (режим timeout)")
        if field not in self._reported.flat():
            raise AdapterError(f"неизвестное поле телеметрии: {field}")
        cutoff = self.now() - timedelta(minutes=minutes)
        return [(ts, flat[field]) for ts, flat in self._history if ts >= cutoff and field in flat]

    def send_command(self, cmd: Command) -> CommandResult:
        """Принять команду; исполнение — с задержкой и, возможно, с отказом исполнителя.

        Симулятор НЕ заменяет предохранитель: диапазоны и белый список проверяет
        ``Guard``. Здесь проверяется только, что команда вообще понятна «протоколу».

        Args:
            cmd: Команда.

        Returns:
            ``ACCEPTED`` — принята (это не значит, что исполнена).

        Raises:
            AdapterTimeout: Связь потеряна.
            AdapterError: Неизвестная команда или некорректные аргументы.
        """
        if self._comms_active():
            raise AdapterTimeout("симулятор: связь потеряна, команда не доставлена")
        if cmd.name not in COMMAND_TARGET:
            raise AdapterError(f"неизвестная команда: {cmd.name}")
        self._check_args(cmd)
        delay = self._command_delay(COMMAND_TARGET[cmd.name])
        self._pending.append(_Pending(self.t + delay, cmd))
        self._truth("command_accepted", f"принята {cmd.name} {cmd.args}", cmd_id=cmd.id)
        return CommandResult(
            command_id=cmd.id,
            status=CommandStatus.ACCEPTED,
            message=f"принята, исполнение через {delay:g} с",
            ts=self.now(),
        )

    def _command_delay(self, target: str) -> float:
        """Задержка исполнения команды: штатная или увеличенная событием ``delayed_actuator``."""
        for d in self._delayed:
            if d.target in (target, "all") and (d.end is None or self.t < d.end):
                return d.delay_s
        return self.sim.actuator_delay_s

    def is_connected(self) -> bool:
        """Есть ли связь (``False`` во время ``comms_loss``)."""
        return not self._comms_active()

    # ------------------------------------------------------------------ для тестов и отчётов

    def truth(self) -> dict[str, float]:
        """Физические («истинные») значения без шума, дрейфа и ложных сигналов.

        Returns:
            Словарь плоских имён полей; нужен тестам, чтобы отличать реальное от показаний.
        """
        flat = self._true_flat()
        flat["occupants"] = self._occupants()
        return flat

    # ------------------------------------------------------------------ внутреннее: проверки

    def _validate_scenario(self) -> None:
        """Проверить сценарий на опечатки до старта (имена датчиков, линий)."""
        known = set(self._true_flat())
        lines = set(self.config.equipment.lines)
        for _, ev, _src in self._events:
            if isinstance(ev, (HiddenDriftEvent, FalseSignalEvent, SensorDriftPairEvent, FrozenSensorEvent, NoiseLossEvent)) and ev.sensor not in known:
                raise ScenarioError(f"неизвестный датчик в сценарии: {ev.sensor}")
            if isinstance(ev, OperatorEvent):
                if ev.line is not None and ev.line not in lines:
                    raise ScenarioError(f"неизвестная линия в сценарии: {ev.line}")
                if ev.action == "set_vent" and ev.mode not in self.sim.co2_vent_rate_s:
                    raise ScenarioError(f"неизвестный режим вытяжки в сценарии: {ev.mode}")
                if ev.action == "set_humidifier" and ev.mode not in self.sim.humidifier_gain_pct_s:
                    raise ScenarioError(f"неизвестный режим увлажнителя в сценарии: {ev.mode}")

    def _check_args(self, cmd: Command) -> None:
        """Минимальная проверка аргументов на уровне «протокола» Комплекса."""
        args = cmd.args
        if cmd.name in ("vent_enable", "set_vent_mode"):
            if args.get("mode") not in self.sim.co2_vent_rate_s:
                raise AdapterError(f"{cmd.name}: недопустимый mode={args.get('mode')!r}")
        elif cmd.name == "set_humidifier_mode":
            if args.get("mode") not in self.sim.humidifier_gain_pct_s:
                raise AdapterError(f"{cmd.name}: недопустимый mode={args.get('mode')!r}")
        elif cmd.name == "set_line":
            if args.get("line") not in self._w.lines_on or not isinstance(args.get("enabled"), bool):
                raise AdapterError(f"{cmd.name}: нужны существующая line и bool enabled")
        elif cmd.name == "set_power_mode" and args.get("mode") not in ("eco", "normal"):
            raise AdapterError(f"{cmd.name}: недопустимый mode={args.get('mode')!r}")

    # ------------------------------------------------------------------ внутреннее: шаг мира

    def _step(self, dt: float) -> None:
        """Один шаг физики: события, команды, устройства, энергия, климат, показания."""
        self._process_events()
        self._apply_pending()
        self._expire_effects()
        self._update_devices(dt)
        self._update_energy(dt)
        self._update_climate(dt)
        self.t += dt
        self._publish()

    def _process_events(self) -> None:
        """Запустить события сценария, время которых наступило."""
        while self._next_event < len(self._events) and self._events[self._next_event][0] <= self.t + 1e-9:
            _, ev, src = self._events[self._next_event]
            self._next_event += 1
            self._start_event(ev, src)

    def _end(self, duration: float | None) -> float | None:
        """Абсолютное время окончания эффекта."""
        return None if duration is None else self.t + duration

    def _gt(self, category: str, ev: Any, field: str, duration: float | None, src: str, **extra: Any) -> None:
        """Эталонная разметка: что было реальным, ложным или скрытым и когда началось."""
        self.ground_truth.append(
            {
                "id": f"gt{len(self.ground_truth) + 1}",
                "category": category,
                "type": ev.type,
                "field": field,
                "t_start": self.t,
                "t_end": None if duration is None else self.t + duration,
                "source": src,
                "note": getattr(ev, "note", ""),
                **extra,
            }
        )

    def _start_event(self, ev: Any, src: str) -> None:
        """Применить одно событие сценария."""
        label = f"{ev.type}" + (f" ({src})" if src != "top" else "")
        if isinstance(ev, CascadeEvent):
            self._truth("cascade", f"начало каскада из {len(ev.steps)} шагов: {ev.note}")
        elif isinstance(ev, FalseThenRealEvent):
            self._truth("false_then_real", f"ложный сигнал {ev.sensor}, через {ev.real_after_s:g} с реальный рост CO2 {ev.note}")
        elif isinstance(ev, CascadeTrapEvent):
            self._truth("cascade_trap", f"ложный сигнал {ev.sensor}; вредные реакции: {ev.harmful} {ev.note}")
            self._gt("trap", ev, ev.sensor, ev.window_s, src, harmful=list(ev.harmful), window_s=ev.window_s)
        elif isinstance(ev, DirectEvent):
            if ev.target == "pv":
                assert ev.factor is not None
                self._pv_factors.append(_Timed(ev.factor, self._end(ev.duration_s)))
            elif ev.target == "load":
                assert ev.extra_w is not None
                self._extra_loads.append(_Timed(ev.extra_w, self._end(ev.duration_s)))
            elif ev.target == "co2":
                assert ev.extra_ppm_s is not None
                self._co2_sources.append(_Timed(ev.extra_ppm_s, self._end(ev.duration_s)))
            else:
                assert ev.delta is not None
                self._occupant_deltas.append(_Timed(ev.delta, self._end(ev.duration_s)))
            self._truth("direct", f"{label}: {ev.target} {ev.note}", target=ev.target)
            fld = {"pv": "pv_power_w", "load": "load_total_w", "occupants": "co2_ppm.*", "co2": "co2_ppm.*"}[ev.target]
            self._gt("real", ev, fld, ev.duration_s, src, target=ev.target)
        elif isinstance(ev, HiddenDriftEvent):
            self._drifts.append(_Drift(ev.sensor, ev.rate_per_min, self.t, self._end(ev.duration_s)))
            self._truth("hidden_drift", f"{label}: {ev.sensor} {ev.rate_per_min:+g}/мин {ev.note}", sensor=ev.sensor)
            self._gt("hidden", ev, ev.sensor, ev.duration_s, src, rate_per_min=ev.rate_per_min)
        elif isinstance(ev, SensorDriftPairEvent):
            self._drifts.append(_Drift(ev.sensor, ev.rate_per_min, self.t, self._end(ev.duration_s)))
            self._truth("sensor_drift_pair", f"{label}: {ev.sensor} {ev.rate_per_min:+g}/мин {ev.note}", sensor=ev.sensor)
            self._gt("hidden", ev, ev.sensor, ev.duration_s, src, rate_per_min=ev.rate_per_min)
        elif isinstance(ev, FalseSignalEvent):
            self._false.append(_FalseSignal(ev.sensor, ev.value, self.t + ev.duration_s))
            self._truth("false_signal", f"{label}: {ev.sensor}={ev.value:g} {ev.note}", sensor=ev.sensor)
            self._gt("false", ev, ev.sensor, ev.duration_s, src, value=ev.value)
        elif isinstance(ev, HiddenLoadEvent):
            self._hidden_loads.append(_Timed(ev.watts, self._end(ev.duration_s)))
            self._truth("hidden_load", f"{label}: {ev.watts:g} Вт вне телеметрии {ev.note}", watts=ev.watts)
            self._gt("hidden", ev, "battery_soc_pct", ev.duration_s, src, watts=ev.watts)
        elif isinstance(ev, SilentPrintFailEvent):
            self._silent_fail.append(_SilentFail(ev.device, ev.power_factor, self._end(ev.duration_s)))
            self._truth("silent_print_fail", f"{label}: {ev.device} running, потребление {ev.power_factor:.0%} {ev.note}", device=ev.device)
            self._gt("hidden", ev, f"lines.{ev.device}.power_w", ev.duration_s, src, device=ev.device)
        elif isinstance(ev, FrozenSensorEvent):
            self._frozen.append(_Frozen(ev.sensor, self._end(ev.duration_s)))
            self._truth("frozen_sensor", f"{label}: {ev.sensor} залип {ev.note}", sensor=ev.sensor)
            self._gt("hidden", ev, ev.sensor, ev.duration_s, src)
        elif isinstance(ev, NoiseLossEvent):
            self._noise_loss.append(_NoiseLoss(ev.sensor, ev.noise_factor, self._end(ev.duration_s)))
            self._truth("noise_loss", f"{label}: {ev.sensor} шум ×{ev.noise_factor:g} {ev.note}", sensor=ev.sensor)
            self._gt("hidden", ev, ev.sensor, ev.duration_s, src)
        elif isinstance(ev, StaleTimestampEvent):
            self._stale_ts = _StaleTs(ev.mode, ev.rollback_s, self.t, self.t + ev.duration_s)
            self._truth("stale_timestamp", f"{label}: метка времени {ev.mode} {ev.note}")
            self._gt("hidden", ev, "ts", ev.duration_s, src, mode=ev.mode)
        elif isinstance(ev, DelayedActuatorEvent):
            self._delayed.append(_Delayed(ev.target, ev.delay_s, self._end(ev.duration_s)))
            self._truth("delayed_actuator", f"{label}: {ev.target} задержка {ev.delay_s:g} с {ev.note}", target=ev.target)
            self._gt("hidden", ev, ev.target, ev.duration_s, src, delay_s=ev.delay_s)
        elif isinstance(ev, CommsLossEvent):
            self._comms_until = self.t + ev.duration_s
            self._comms_mode = ev.mode
            self._truth("comms_loss", f"{label}: {ev.duration_s:g} с ({ev.mode}) {ev.note}")
            self._gt("real", ev, "comms_ok", ev.duration_s, src)
        elif isinstance(ev, ActuatorFailEvent):
            self._act_fail.append((ev.target, self._end(ev.duration_s)))
            self._truth("actuator_fail", f"{label}: {ev.target} {ev.note}", target=ev.target)
            self._gt("real", ev, ev.target, ev.duration_s, src)
        elif isinstance(ev, DeviceFaultEvent):
            self._faults[ev.device] = self._end(ev.duration_s)
            self._truth("device_fault", f"{label}: {ev.device} {ev.note}", device=ev.device)
            self._gt("real", ev, f"{ev.device}_status", ev.duration_s, src)
        elif isinstance(ev, OperatorEvent):
            self._operator_action(ev)

    def _operator_action(self, ev: OperatorEvent) -> None:
        """Действие Оператора (не через предохранитель: человек в Комплексе)."""
        if ev.action == "start_device" and ev.device:
            dev = self._devices[ev.device]
            line = self._device_line(ev.device)
            if dev.state is DeviceStatus.ERROR or not self._w.lines_on.get(line, False):
                self._truth("operator", f"запуск {ev.device} невозможен (авария или линия отключена)")
                return
            dev.state = DeviceStatus.RUNNING
            dev.remaining_s = ev.duration_s or 0.0
            dev.run_s = 0.0
            self._truth("operator", f"Оператор запустил {ev.device} на {dev.remaining_s:g} с", device=ev.device)
        elif ev.action == "stop_device" and ev.device:
            dev = self._devices[ev.device]
            if dev.state is DeviceStatus.RUNNING:
                dev.state = DeviceStatus.IDLE
            self._truth("operator", f"Оператор остановил {ev.device}", device=ev.device)
        elif ev.action == "set_vent" and ev.mode:
            self._w.vent_mode = ev.mode
            self._truth("operator", f"Оператор: вытяжка {ev.mode}")
        elif ev.action == "set_humidifier" and ev.mode:
            self._w.humidifier_mode = ev.mode
            self._truth("operator", f"Оператор: увлажнитель {ev.mode}")
        elif ev.action == "set_line" and ev.line is not None and ev.enabled is not None:
            self._w.lines_on[ev.line] = ev.enabled
            self._truth("operator", f"Оператор: линия {ev.line} {'вкл' if ev.enabled else 'откл'}")

    def _apply_pending(self) -> None:
        """Исполнить команды, у которых истекла задержка исполнителя."""
        due = [p for p in self._pending if p.apply_at <= self.t + 1e-9]
        self._pending = [p for p in self._pending if p.apply_at > self.t + 1e-9]
        for p in due:
            cmd = p.cmd
            target = COMMAND_TARGET[cmd.name]
            if self._actuator_failing(target):
                self._truth("command_ignored", f"исполнитель {target} не отреагировал на {cmd.name}", cmd_id=cmd.id)
                continue
            if self.sim.actuator_fail_prob > 0 and self._rng.random() < self.sim.actuator_fail_prob:
                self._truth("command_ignored", f"случайный отказ исполнителя на {cmd.name}", cmd_id=cmd.id)
                continue
            self._execute(cmd)

    def _execute(self, cmd: Command) -> None:
        """Изменить физическое состояние по команде."""
        a = cmd.args
        if cmd.name in ("vent_enable", "set_vent_mode"):
            self._w.vent_mode = str(a["mode"])
        elif cmd.name == "set_humidifier_mode":
            self._w.humidifier_mode = str(a["mode"])
        elif cmd.name == "set_line":
            self._w.lines_on[str(a["line"])] = bool(a["enabled"])
        elif cmd.name == "set_power_mode":
            self._w.power_mode = str(a["mode"])
        self._truth("command_applied", f"исполнена {cmd.name} {cmd.args}", cmd_id=cmd.id)

    def _actuator_failing(self, target: str) -> bool:
        """Отказывает ли сейчас исполнитель ``target``."""
        return any(t in (target, "all") and (end is None or self.t < end) for t, end in self._act_fail)

    def _expire_effects(self) -> None:
        """Убрать эффекты с истёкшим временем."""

        def alive(end: float | None) -> bool:
            return end is None or self.t < end

        self._pv_factors = [e for e in self._pv_factors if alive(e.end)]
        self._extra_loads = [e for e in self._extra_loads if alive(e.end)]
        self._occupant_deltas = [e for e in self._occupant_deltas if alive(e.end)]
        self._drifts = [d for d in self._drifts if alive(d.end)]
        self._false = [f for f in self._false if self.t < f.end]
        self._act_fail = [(t, e) for t, e in self._act_fail if alive(e)]
        self._faults = {d: e for d, e in self._faults.items() if alive(e)}
        self._hidden_loads = [e for e in self._hidden_loads if alive(e.end)]
        self._co2_sources = [e for e in self._co2_sources if alive(e.end)]
        self._frozen = [f for f in self._frozen if alive(f.end)]
        self._noise_loss = [n for n in self._noise_loss if alive(n.end)]
        self._delayed = [d for d in self._delayed if alive(d.end)]
        self._silent_fail = [f for f in self._silent_fail if alive(f.end)]
        if self._stale_ts is not None and self.t >= self._stale_ts.end:
            self._stale_ts = None
        if self._comms_until is not None and self.t >= self._comms_until:
            self._comms_until = None

    def _comms_active(self) -> bool:
        """Идёт ли сейчас потеря связи."""
        return self._comms_until is not None and self.t < self._comms_until

    def _device_line(self, device: str) -> str:
        """Линия щита устройства (из ``equipment.device_lines``)."""
        return self.config.equipment.device_lines[f"{device}_status"]

    def _update_devices(self, dt: float) -> None:
        """Шаг конечных автоматов устройств."""
        for name, dev in self._devices.items():
            line_on = self._w.lines_on.get(self._device_line(name), False)
            if name in self._faults:
                dev.state = DeviceStatus.ERROR
                dev.remaining_s = 0.0
                continue
            if dev.state is DeviceStatus.ERROR:
                dev.state = DeviceStatus.IDLE  # авария закончилась
            if dev.state is DeviceStatus.RUNNING:
                if not line_on:
                    dev.state = DeviceStatus.IDLE  # питание снято: задание прервано
                    self._truth("device_aborted", f"{name}: питание снято, работа прервана", device=name)
                    continue
                dev.remaining_s -= dt
                dev.run_s += dt
                if dev.remaining_s <= 0:
                    dev.state = DeviceStatus.IDLE
                    self._truth("device_done", f"{name}: работа завершена", device=name)

    # ------------------------------------------------------------------ внутреннее: физика

    def _occupants(self) -> float:
        """Текущее число людей в помещении."""
        return max(0.0, self.sim.occupants + sum(e.value for e in self._occupant_deltas))

    def _line_powers(self) -> dict[str, float]:
        """Истинная мощность по каждой линии, Вт."""
        eq = self.config.equipment
        out: dict[str, float] = {}
        eco = 0.5 if self._w.power_mode == "eco" else 1.0
        for name, spec in eq.lines.items():
            if not self._w.lines_on.get(name, False):
                out[name] = 0.0
            elif name in self._devices:
                dev = self._devices[name]
                if dev.state is DeviceStatus.RUNNING:
                    ramp = min(1.0, dev.run_s / self.sim.device_start_delay_s) if self.sim.device_start_delay_s > 0 else 1.0
                    factor = 1.0
                    for sf in self._silent_fail:
                        if sf.device == name:
                            factor = sf.factor
                    out[name] = max(STANDBY_W, spec.nominal_w * ramp * factor)
                else:
                    out[name] = STANDBY_W
            elif name == "vent":
                out[name] = eq.vent_power_w.get(self._w.vent_mode, 0.0)
            elif name == "humidifier":
                out[name] = eq.humidifier_power_w.get(self._w.humidifier_mode, 0.0)
            elif name in ("lighting", "aux"):
                out[name] = spec.nominal_w * eco
            else:
                out[name] = spec.nominal_w
        return out

    def _pv(self) -> float:
        """Истинная генерация панелей, Вт."""
        sun = 0.95 + 0.05 * math.sin(2 * math.pi * self.t / 1800.0)
        cloud = min(1.0, max(0.0, self.sim.cloud_cover + 0.04 * math.sin(2 * math.pi * self.t / 700.0 + 1.0)))
        pv = self.sim.pv_peak_w * (1.0 - cloud) * sun
        for f in self._pv_factors:
            pv *= f.value
        return pv

    def _load_total(self) -> float:
        """Истинная суммарная нагрузка, Вт."""
        return sum(self._line_powers().values()) + sum(e.value for e in self._extra_loads)

    def _update_energy(self, dt: float) -> None:
        """Заряд/разряд АКБ и её температура."""
        pv, load = self._pv(), self._load_total() + sum(e.value for e in self._hidden_loads)
        net = pv - load
        eff = 0.97  # КПД заряда/разряда (условный)
        d_wh = net * eff * dt / 3600.0 if net > 0 else net / eff * dt / 3600.0
        cap = self.config.energy.battery_capacity_wh
        self._w.soc = min(100.0, max(0.0, self._w.soc + d_wh / cap * 100.0))
        target = self.sim.battery_temp_base_c + 0.0015 * abs(net)
        self._w.battery_temp += (target - self._w.battery_temp) * dt / 900.0
        if self._w.soc < self.config.energy.soc_min_pct and not self._below_min_logged:
            self._below_min_logged = True
            self._truth("battery_below_min", f"заряд АКБ {self._w.soc:.1f}% ниже минимального {self.config.energy.soc_min_pct:g}%")

    def _update_climate(self, dt: float) -> None:
        """CO2, влажность и температура помещения."""
        s, w = self.sim, self._w
        vent_rate = s.co2_vent_rate_s.get(w.vent_mode, 0.0) if w.lines_on.get("vent", False) else 0.0
        gen = self._occupants() * s.co2_per_person_ppm_s + sum(e.value for e in self._co2_sources)
        w.co2 += (gen - (s.co2_leak_rate_s + vent_rate) * (w.co2 - s.co2_outdoor_ppm)) * dt
        gain = s.humidifier_gain_pct_s.get(w.humidifier_mode, 0.0) if w.lines_on.get("humidifier", False) else 0.0
        w.humidity += (gain - s.humidity_relax_s * (w.humidity - s.humidity_base_pct)) * dt
        w.humidity = min(100.0, max(0.0, w.humidity))
        heat = sum(
            s.heat_w_per_deg_s.get(name, 0.0)
            for name, dev in self._devices.items()
            if dev.state is DeviceStatus.RUNNING
        )
        w.temp += (heat - s.temp_relax_s * (w.temp - s.temp_base_c)) * dt

    def _true_flat(self) -> dict[str, float]:
        """Истинные значения в плоских именах телеметрии (без шума)."""
        powers = self._line_powers()
        load = sum(powers.values()) + sum(e.value for e in self._extra_loads)
        w = self._w
        flat: dict[str, float] = {
            "pv_power_w": self._pv(),
            "battery_soc_pct": w.soc,
            "battery_voltage_v": 44.0 + 0.1 * w.soc - 0.0008 * load,
            "battery_temp_c": w.battery_temp,
            "load_total_w": load,
            "temp_c": w.temp,
            "temp_c_2": w.temp + TEMP2_BIAS_C,
            "humidity_pct": w.humidity,
            "data_age_s": 0.0,
        }
        for sensor, bias in CO2_SENSORS.items():
            flat[f"co2_ppm.{sensor}"] = w.co2 + bias
        for name, p in powers.items():
            flat[f"lines.{name}.power_w"] = p
        return flat

    # ------------------------------------------------------------------ внутреннее: показания

    _SIGMA_ABS: ClassVar[dict[str, float]] = {
        "battery_soc_pct": 0.02,
        "battery_voltage_v": 0.02,
        "battery_temp_c": 0.05,
        "temp_c": 0.03,
        "temp_c_2": 0.03,
        "humidity_pct": 0.15,
    }

    def _noise(self, sigma: float) -> float:
        """Гауссов шум показаний (0, если шум отключён)."""
        return self._rng.gauss(0.0, sigma) if self.sim.noise and sigma > 0 else 0.0

    def _noise_scale(self, name: str) -> float:
        """Множитель шума датчика (``noise_loss`` уменьшает его)."""
        scale = 1.0
        for n in self._noise_loss:
            if n.sensor == name:
                scale = n.factor
        return scale

    def _reported_flat(self) -> dict[str, float]:
        """Показания датчиков: истина + шум + дрейф + ложные сигналы + залипание."""
        flat = self._true_flat()
        rep: dict[str, float] = {}
        for name, value in flat.items():
            k = self._noise_scale(name)
            if name == "data_age_s":
                rep[name] = value
            elif name in self._SIGMA_ABS:
                rep[name] = value + self._noise(self._SIGMA_ABS[name] * k)
            elif name.startswith("co2_ppm."):
                rep[name] = value + self._noise(4.0 * k)
            elif name == "pv_power_w" or name == "load_total_w" or name.endswith(".power_w"):
                rep[name] = value + self._noise((0.005 * abs(value) + 0.2) * k)
            else:  # pragma: no cover - все поля перечислены выше
                rep[name] = value
        for d in self._drifts:
            stop = self.t if d.end is None else min(self.t, d.end)
            rep[d.sensor] += d.rate_per_min * (stop - d.start) / 60.0
        for f in self._false:
            rep[f.sensor] = f.value
        for fr in self._frozen:
            if fr.value is None:
                fr.value = rep[fr.sensor]  # залипает на последнем показании
            rep[fr.sensor] = fr.value
        return rep

    def _build_snapshot(self) -> Telemetry:
        """Собрать снимок телеметрии из показаний."""
        rep = self._reported_flat()
        w = self._w
        lines = {
            name: LineState(on=w.lines_on[name], power_w=max(0.0, rep[f"lines.{name}.power_w"]))
            for name in self.config.equipment.lines
        }
        return Telemetry(
            ts=self._snapshot_ts(),
            pv_power_w=max(0.0, rep["pv_power_w"]),
            battery_soc_pct=min(100.0, max(0.0, rep["battery_soc_pct"])),
            battery_voltage_v=rep["battery_voltage_v"],
            battery_temp_c=rep["battery_temp_c"],
            load_total_w=max(0.0, rep["load_total_w"]),
            lines=lines,
            power_mode=w.power_mode,
            temp_c=rep["temp_c"],
            temp_c_2=rep["temp_c_2"],
            humidity_pct=min(100.0, max(0.0, rep["humidity_pct"])),
            co2_ppm={s: max(0.0, rep[f"co2_ppm.{s}"]) for s in CO2_SENSORS},
            vent_mode=w.vent_mode,
            humidifier_mode=w.humidifier_mode,
            cnc_status=self._devices["cnc"].state,
            printer_status=self._devices["printer"].state,
            solder_status=self._devices["solder"].state,
            comms_ok=True,
            data_age_s=rep["data_age_s"],
        )

    def _snapshot_ts(self) -> datetime:
        """Метка времени снимка (``stale_timestamp`` замораживает или откатывает её)."""
        st = self._stale_ts
        if st is None or self.t < st.start:
            return self.now()
        if st.mode == "freeze":
            return self._start + timedelta(seconds=st.start)
        return self.now() - timedelta(seconds=st.rollback_s)

    def _publish(self) -> None:
        """Обновить публикуемый снимок после шага (шум считается здесь, один раз за шаг)."""
        if self._comms_active() and self._last_good is not None:
            self._reported = self._last_good.model_copy(
                update={
                    "ts": self.now(),
                    "comms_ok": False,
                    "data_age_s": self.t - self._last_good_t,
                }
            )
            return
        snap = self._build_snapshot()
        self._reported = snap
        self._last_good = snap
        self._last_good_t = self.t
        self._record_history(snap)

    def _record_history(self, snap: Telemetry) -> None:
        """Добавить снимок в историю (для ``get_history``)."""
        self._history.append((snap.ts, snap.flat()))
        cutoff = snap.ts - timedelta(minutes=HISTORY_MINUTES)
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

    def _truth(self, kind: str, text: str, **data: Any) -> None:
        """Записать событие в журнал «правды» симулятора (для оценки качества и отчётов)."""
        self.event_log.append({"t": self.t, "ts": self.now().isoformat(), "kind": kind, "text": text, **data})
