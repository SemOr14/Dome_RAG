"""Энергобаланс и планировщик операций.

Формула запаса времени до минимального заряда (``P_load > P_pv``)::

    t_left = E_bat * (SoC − SoC_min) / (P_load − P_pv)

где ``E_bat`` — ёмкость АКБ, ``SoC`` — текущий заряд в долях. Генерация и нагрузка
усредняются по окну (``energy.avg_window_s``), чтобы шум датчиков не прыгал в оценке.

Приоритеты (что защищаем при нехватке энергии): сначала безопасность (вытяжка при
пайке, регламент п. 9.1), затем неприрываемые операции, затем всё остальное;
откладываем и отключаем в обратном порядке.

Модуль — чистый код: агент ``energy_planner`` получает его выводы и лишь формулирует
рекомендации; резервный сценарий агента — прямой вызов этих функций.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from kupol.core.config import AppConfig, Operation
from kupol.core.models import Alert, AlertLevel, Command, DeviceStatus, Telemetry
from kupol.core.state import State

PRIORITY_SAFETY = 0
PRIORITY_UNINTERRUPTIBLE = 1
PRIORITY_OTHER = 2


def priority_class(op: Operation) -> int:
    """Класс приоритета операции: 0 — безопасность, 1 — неприрываемая, 2 — остальное.

    Args:
        op: Этап технологической карты.

    Returns:
        Число; чем меньше, тем важнее защитить операцию при нехватке энергии.
    """
    if op.needs_vent:
        return PRIORITY_SAFETY
    if not op.interruptible:
        return PRIORITY_UNINTERRUPTIBLE
    return PRIORITY_OTHER


@dataclass(frozen=True)
class EnergyStatus:
    """Усреднённое энергетическое состояние.

    Attributes:
        soc_pct: Заряд АКБ, %.
        pv_w: Генерация (среднее за окно), Вт.
        load_w: Нагрузка (среднее за окно), Вт.
        net_w: ``pv_w − load_w`` (отрицательная — АКБ разряжается), Вт.
        available_wh: Энергия до минимального заряда, Вт·ч.
        t_left_min: Запас времени до минимального заряда, минут; ``None`` — АКБ не разряжается.
        level: ``ok`` / ``warning`` / ``critical`` по порогам ``t_left_*``.
    """

    soc_pct: float
    pv_w: float
    load_w: float
    net_w: float
    available_wh: float
    t_left_min: float | None
    level: AlertLevel | None


def t_left_minutes(available_wh: float, load_w: float, pv_w: float) -> float | None:
    """Запас времени до минимального заряда по формуле из модуля.

    Args:
        available_wh: Энергия выше минимального заряда, Вт·ч.
        load_w: Нагрузка, Вт.
        pv_w: Генерация, Вт.

    Returns:
        Минуты; ``0.0``, если энергии уже нет; ``None``, если нагрузка не превышает генерацию.
    """
    deficit = load_w - pv_w
    if deficit <= 0:
        return None
    if available_wh <= 0:
        return 0.0
    return available_wh / deficit * 60.0


def energy_status(state: State, config: AppConfig) -> EnergyStatus | None:
    """Рассчитать усреднённое состояние энергосистемы.

    Args:
        state: Состояние.
        config: Конфигурация.

    Returns:
        Статус или ``None``, если данных ещё нет.
    """
    latest = state.latest()
    if latest is None:
        return None
    window_min = config.energy.avg_window_s / 60.0
    snaps = state.snapshots(window_min) or [latest]
    pv = sum(s.pv_power_w for s in snaps) / len(snaps)
    load = sum(s.load_total_w for s in snaps) / len(snaps)
    soc = latest.battery_soc_pct
    available = config.energy.battery_capacity_wh * (soc - config.energy.soc_min_pct) / 100.0
    t_left = t_left_minutes(max(available, 0.0), load, pv)
    level: AlertLevel | None = None
    if t_left is not None:
        if t_left <= config.energy.t_left_crit_min:
            level = AlertLevel.CRITICAL
        elif t_left <= config.energy.t_left_warn_min:
            level = AlertLevel.WARNING
    return EnergyStatus(
        soc_pct=soc, pv_w=pv, load_w=load, net_w=pv - load, available_wh=available, t_left_min=t_left, level=level
    )


def energy_alert(status: EnergyStatus | None, now: datetime, deficit_info_w: float = 600.0) -> Alert | None:
    """Тревога по запасу времени (или информационная о заметном разряде).

    Args:
        status: Статус энергосистемы.
        now: Текущее время.
        deficit_info_w: Порог разряда АКБ для информационной тревоги ``energy_deficit``.

    Returns:
        ``energy_t_left`` (warning/critical), ``energy_deficit`` (info) или ``None``.
    """
    if status is None:
        return None
    if status.level is not None and status.t_left_min is not None:
        return Alert(
            ts=now,
            level=status.level,
            kind="energy_t_left",
            field="battery_soc_pct",
            message=(
                f"Запас энергии до минимального заряда ≈{status.t_left_min:.0f} мин "
                f"(SoC {status.soc_pct:.0f} %, генерация {status.pv_w:.0f} Вт, нагрузка {status.load_w:.0f} Вт)"
            ),
            value=status.t_left_min,
            details={"net_w": status.net_w, "available_wh": status.available_wh},
        )
    if status.net_w < -deficit_info_w:
        return Alert(
            ts=now,
            level=AlertLevel.INFO,
            kind="energy_deficit",
            field="battery_soc_pct",
            message=(
                f"АКБ разряжается со скоростью ≈{-status.net_w:.0f} Вт (генерация {status.pv_w:.0f} Вт, "
                f"нагрузка {status.load_w:.0f} Вт); запас ≈{status.t_left_min or 0:.0f} мин"
            ),
            value=status.net_w,
            details={"net_w": status.net_w},
        )
    return None


class OpStatus(StrEnum):
    """Состояние этапа технологической карты."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    BLOCKED = "blocked"  # устройство неисправно


@dataclass
class ProcessTracker:
    """Отслеживает выполнение этапов по статусам устройств.

    Этап с ``device_line`` считается идущим, когда статус соответствующего устройства
    ``running``, и выполненным, когда оно остановилось после ≥ 80 % плановой
    длительности (иначе вернулся в очередь как прерванный). Этапы без статуса в
    телеметрии (сборка, измерения) отмечает человек: ``mark_done``.
    """

    config: AppConfig
    status: dict[str, OpStatus] = field(default_factory=dict)
    started: dict[str, datetime] = field(default_factory=dict)
    interrupted: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Все этапы изначально ожидают."""
        for op in self.config.process.operations:
            self.status.setdefault(op.name, OpStatus.PENDING)

    def update(self, tel: Telemetry) -> None:
        """Обновить состояние этапов по свежему снимку.

        Args:
            tel: Снимок телеметрии.
        """
        if not tel.comms_ok:
            return  # застывшие данные не двигают процесс
        for op in self.config.process.operations:
            if op.device_line is None:
                continue
            dev: DeviceStatus | None = getattr(tel, f"{op.device_line}_status", None)
            if dev is None:
                continue
            cur = self.status[op.name]
            if dev is DeviceStatus.RUNNING and cur is OpStatus.PENDING and self._is_next_for_line(op):
                self.status[op.name] = OpStatus.RUNNING
                self.started[op.name] = tel.ts
            elif cur is OpStatus.RUNNING and dev is not DeviceStatus.RUNNING:
                elapsed = (tel.ts - self.started[op.name]).total_seconds() / 60.0
                if dev is DeviceStatus.ERROR:
                    self.status[op.name] = OpStatus.BLOCKED
                elif elapsed >= 0.8 * op.duration_min:
                    self.status[op.name] = OpStatus.DONE
                else:
                    self.status[op.name] = OpStatus.PENDING
                    self.interrupted[op.name] = self.interrupted.get(op.name, 0) + 1
            elif cur is OpStatus.BLOCKED and dev is not DeviceStatus.ERROR:
                self.status[op.name] = OpStatus.PENDING

    def _is_next_for_line(self, op: Operation) -> bool:
        """Первый ожидающий этап на этой линии (по порядку карты) — именно он сейчас идёт."""
        for other in self.config.process.operations:
            if other.device_line == op.device_line and self.status[other.name] is OpStatus.PENDING:
                return other.name == op.name
        return False

    def mark_done(self, name: str) -> None:
        """Отметить этап выполненным вручную.

        Args:
            name: Имя этапа.

        Raises:
            KeyError: Нет такого этапа.
        """
        if name not in self.status:
            raise KeyError(name)
        self.status[name] = OpStatus.DONE

    def mark_pending(self, name: str) -> None:
        """Вернуть этап в очередь (например, ошибочная отметка).

        Args:
            name: Имя этапа.

        Raises:
            KeyError: Нет такого этапа.
        """
        if name not in self.status:
            raise KeyError(name)
        self.status[name] = OpStatus.PENDING

    def operations(self, *statuses: OpStatus) -> list[Operation]:
        """Этапы в заданных статусах в порядке технологической карты.

        Args:
            *statuses: Статусы для отбора.

        Returns:
            Список этапов.
        """
        return [op for op in self.config.process.operations if self.status[op.name] in statuses]

    def ready(self) -> list[Operation]:
        """Ожидающие этапы, у которых выполнены все зависимости."""
        return [
            op
            for op in self.operations(OpStatus.PENDING)
            if all(self.status[d] is OpStatus.DONE for d in op.depends_on)
        ]


@dataclass(frozen=True)
class PlanItem:
    """Элемент плана операций.

    Attributes:
        operation: Этап.
        priority: Класс приоритета (0 — безопасность, 1 — неприрываемая, 2 — остальное).
        can_start: Хватит ли энергии начать сейчас.
        reason: Пояснение с числами.
    """

    operation: Operation
    priority: int
    can_start: bool
    reason: str


@dataclass(frozen=True)
class SheddingAdvice:
    """Совет по снижению нагрузки при нехватке энергии.

    Attributes:
        text: Что сделать и почему.
        saving_w: Ожидаемая экономия, Вт.
        command: Команда (проходит через Guard) или ``None``, если это указание Оператору.
        t_left_after_min: Оценка запаса времени после применения всех предыдущих советов и этого.
    """

    text: str
    saving_w: float
    command: Command | None
    t_left_after_min: float | None


class EnergyPlanner:
    """Детерминированный планировщик: «что если», порядок операций, что отложить."""

    def __init__(self, config: AppConfig) -> None:
        """Args:
        config: Конфигурация Штаба.
        """
        self._cfg = config

    def _vent_extra_w(self, op: Operation, tel: Telemetry) -> float:
        """Доп. мощность вытяжки, которую придётся включить для этапа с вытяжкой."""
        if not op.needs_vent:
            return 0.0
        eq = self._cfg.equipment
        needed = eq.vent_power_w.get(self._cfg.process.vent_mode_for_vent_ops, 0.0)
        current = tel.lines["vent"].power_w if "vent" in tel.lines else 0.0
        return max(0.0, needed - current)

    def can_start(
        self, op: Operation, tel: Telemetry, status: EnergyStatus, vent_ok: bool = True
    ) -> tuple[bool, str]:
        """Режим «что если»: хватит ли энергии, если начать операцию сейчас.

        Args:
            op: Этап.
            tel: Последний снимок.
            status: Усреднённый энергостатус.
            vent_ok: Исправна ли вытяжка (ложь, если ранее была тревога ``actuator_no_effect``).

        Returns:
            ``(можно, причина)``; причина всегда содержит числа расчёта.
        """
        if not tel.comms_ok:
            return False, "нет достоверных данных телеметрии — оценка невозможна"
        if op.needs_vent and not vent_ok:
            return False, "этап требует вытяжку, а вытяжка не отвечает на команды — начинать нельзя (безопасность)"
        extra = self._vent_extra_w(op, tel)
        added = op.power_w + extra
        load_new = status.load_w + added
        limit = self._cfg.energy.inverter_limit_w
        if load_new > limit:
            return False, f"нагрузка вырастет до {load_new:.0f} Вт и превысит предел инвертора {limit:.0f} Вт"
        deficit = max(load_new - status.pv_w, 0.0)
        need_wh = deficit * op.duration_min / 60.0 * self._cfg.energy.start_margin
        if need_wh > status.available_wh:
            return False, (
                f"не хватит энергии: нужно ≈{need_wh:.0f} Вт·ч (нагрузка {load_new:.0f} Вт при генерации {status.pv_w:.0f} Вт "
                f"на {op.duration_min:g} мин с запасом {self._cfg.energy.start_margin:g}), доступно {status.available_wh:.0f} Вт·ч"
            )
        vent_note = f", включая вытяжку +{extra:.0f} Вт" if extra else ""
        return True, (
            f"энергии хватит: нужно ≈{need_wh:.0f} Вт·ч, доступно {status.available_wh:.0f} Вт·ч "
            f"(нагрузка {load_new:.0f} Вт{vent_note})"
        )

    def command_risk(self, cmd: Command, tel: Telemetry | None, status: EnergyStatus | None) -> str | None:
        """Что-если для дорогой команды: не вызовет ли она каскад (разряд АКБ или остановку неприрываемой операции).

        Args:
            cmd: Команда.
            tel: Последний снимок.
            status: Усреднённый энергостатус.

        Returns:
            Описание риска или `None`, если действие безопасно для энергобаланса и производства.
        """
        if tel is None or status is None:
            return None
        eq = self._cfg.equipment
        min_t = self._cfg.response.cascade_min_t_left_min
        delta = 0.0
        if cmd.name in ("vent_enable", "set_vent_mode"):
            current = tel.lines["vent"].power_w if "vent" in tel.lines else 0.0
            delta = eq.vent_power_w.get(str(cmd.args.get("mode")), 0.0) - current
        elif cmd.name == "set_humidifier_mode":
            current = tel.lines["humidifier"].power_w if "humidifier" in tel.lines else 0.0
            delta = eq.humidifier_power_w.get(str(cmd.args.get("mode")), 0.0) - current
        elif cmd.name == "set_line" and cmd.args.get("enabled") is True:
            delta = eq.lines[str(cmd.args.get("line"))].nominal_w if str(cmd.args.get("line")) in eq.lines else 0.0
        elif cmd.name == "set_power_mode" and cmd.args.get("mode") == "normal" and tel.power_mode == "eco":
            saving = self._cfg.energy.eco_saving_fraction
            eco_now = sum(tel.lines[n].power_w for n in ("lighting", "aux") if n in tel.lines)
            delta = eco_now * saving / (1.0 - saving) if saving < 1.0 else 0.0  # возврат из eco возвращает сэкономленное
        if delta > 0:
            t_after = t_left_minutes(max(status.available_wh, 0.0), status.load_w + delta, status.pv_w)
            if t_after is not None and t_after < min_t:
                now_txt = "∞" if status.t_left_min is None else f"{status.t_left_min:.0f}"
                return (
                    f"{cmd.name} {cmd.args} добавит ≈{delta:.0f} Вт нагрузки: запас до минимального заряда упадёт с "
                    f"{now_txt} до ≈{t_after:.0f} мин (безопасный минимум {min_t:g} мин)"
                )
        if cmd.name == "set_line" and cmd.args.get("enabled") is False:
            line = str(cmd.args.get("line"))
            for status_field, device_line in eq.device_lines.items():
                if device_line != line or getattr(tel, status_field, None) != DeviceStatus.RUNNING:
                    continue
                for op in self._cfg.process.operations:
                    if op.device_line == line and not op.interruptible:
                        return f"отключение линии {line} остановит неприрываемую операцию «{op.name}»"
        return None

    def order(self, tracker: ProcessTracker, tel: Telemetry, status: EnergyStatus, vent_ok: bool = True) -> list[PlanItem]:
        """Порядок оставшихся операций с проверкой «можно ли начать сейчас».

        Сортировка: сначала готовые (зависимости выполнены), затем по классу приоритета
        (безопасность → неприрываемые → остальное), затем по порядку карты.

        Args:
            tracker: Отслеживание этапов.
            tel: Последний снимок.
            status: Энергостатус.
            vent_ok: Исправна ли вытяжка.

        Returns:
            План для ожидающих этапов.
        """
        ready_names = {op.name for op in tracker.ready()}
        index = {op.name: i for i, op in enumerate(self._cfg.process.operations)}
        pending = tracker.operations(OpStatus.PENDING)
        pending.sort(key=lambda o: (o.name not in ready_names, priority_class(o), index[o.name]))
        items: list[PlanItem] = []
        for op in pending:
            ok, reason = self.can_start(op, tel, status, vent_ok)
            if op.name not in ready_names:
                ok, reason = False, f"ждёт завершения: {', '.join(op.depends_on)}"
            items.append(PlanItem(op, priority_class(op), ok, reason))
        return items

    def shedding_advice(self, tel: Telemetry, status: EnergyStatus, tracker: ProcessTracker | None = None) -> list[SheddingAdvice]:
        """Что отложить или отключить при нехватке энергии, с объяснением.

        Порядок: малорисковые команды (увлажнитель, eco-режим, некритичные линии), затем
        указания Оператору отложить прерываемые операции низшего приоритета. Безопасность
        (вытяжка при пайке) и неприрываемые операции не трогаем никогда.

        Args:
            tel: Последний снимок.
            status: Энергостатус.
            tracker: Отслеживание этапов (для указаний по операциям); необязателен.

        Returns:
            Советы по порядку применения; пусто, если энергии достаточно.
        """
        if status.t_left_min is None or status.t_left_min > self._cfg.energy.t_left_warn_min:
            return []
        advice: list[SheddingAdvice] = []
        saved = 0.0

        def push(text: str, saving: float, cmd: Command | None) -> None:
            nonlocal saved
            saved += saving
            t_after = t_left_minutes(status.available_wh, status.load_w - saved, status.pv_w)
            advice.append(SheddingAdvice(text, saving, cmd, t_after))

        hum = tel.lines.get("humidifier")
        if hum is not None and tel.humidifier_mode != "off" and hum.power_w > 1:
            push(
                f"Выключить увлажнитель: экономия ≈{hum.power_w:.0f} Вт (малорисковый режим, влажность {tel.humidity_pct:.0f} % в норме)"
                if 25 <= tel.humidity_pct <= 75
                else f"Выключить увлажнитель: экономия ≈{hum.power_w:.0f} Вт",
                hum.power_w,
                Command(name="set_humidifier_mode", args={"mode": "off"}, reason="нехватка энергии: экономия"),
            )
        light = sum(tel.lines[n].power_w for n in ("lighting", "aux") if n in tel.lines)
        if light > 1 and tel.power_mode != "eco" and not self._done(advice, status):
            saving = light * self._cfg.energy.eco_saving_fraction
            push(
                f"Перейти в режим eco: экономия ≈{saving:.0f} Вт на освещении и розетках",
                saving,
                Command(name="set_power_mode", args={"mode": "eco"}, reason="нехватка энергии: экономия"),
            )
        for name in ("aux", "lighting"):
            line = tel.lines.get(name)
            if line is not None and line.on and line.power_w > 1 and not self._done(advice, status):
                push(
                    f"Отключить линию {name}: экономия ≈{line.power_w:.0f} Вт",
                    line.power_w,
                    Command(name="set_line", args={"line": name, "enabled": False}, reason="нехватка энергии: экономия"),
                )
        if tracker is not None and not self._done(advice, status):
            candidates = [op for op in tracker.operations(OpStatus.PENDING) if priority_class(op) == PRIORITY_OTHER]
            for op in sorted(candidates, key=lambda o: -o.power_w):
                if self._done(advice, status):
                    break
                push(
                    f"Попросить Оператора отложить этап «{op.name}» (≈{op.power_w:.0f} Вт): прерываемый, не связан с безопасностью",
                    op.power_w,
                    None,
                )
        return advice

    def _done(self, advice: list[SheddingAdvice], status: EnergyStatus) -> bool:
        """Достигнут ли целевой запас времени после уже выданных советов."""
        if not advice:
            return False
        last = advice[-1].t_left_after_min
        return last is None or last > self._cfg.energy.t_left_warn_min
