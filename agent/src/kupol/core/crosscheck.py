"""Сверка источников: отличаем реальное событие от ложного сигнала и сбоя датчика.

Правила возвращают ``Verdict(kind, confidence, explanation)``, где ``kind``:
``real`` (реально), ``false_signal`` (ложный разовый сигнал), ``sensor_fault``
(датчик врёт/дрейфует), ``unknown`` (данных мало). Сверка — чистый код; LLM
получает вердикты на вход и только объясняет их (регламент 7.3: «проверить
достоверность признака с использованием независимых источников»).

Правила:

1. несколько датчиков CO2: выброс одного при согласии остальных;
2. один датчик CO2: скачок при нормальных температуре и влажности;
3. энергобаланс: ``dE_bat/dt ≈ P_pv − P_load`` (расхождение — врёт один из датчиков);
4. статус устройства против его потребления;
5. правдоподобие роста температуры АКБ (малый ток, а температура растёт — дрейф датчика);
6. подтверждение падения генерации балансом АКБ;
7. тепловой баланс: температура растёт при простое оборудования (скрытый источник или дрейф датчика);
8. план против факта: устройство `running`, а потребление не соответствует технологической карте (тихий брак);
9. причина → следствие: после нашей команды ожидается отклик в нескольких каналах (состояние и мощность линии).

Правила дают `Verdict`-ы со свидетельствами; накопление, перепроверка и пересмотр — в `core/verdicts.py`.

Формула баланса (для документации и тестов)::

    dE_bat/dt ≈ P_pv − P_load,   |dE_bat/dt − (P_pv − P_load)| > ε  ⇒  mismatch
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from statistics import median

from kupol.core.config import AppConfig
from kupol.core.models import DeviceStatus, Evidence, SentCommand, Telemetry, Verdict
from kupol.core.state import State


def mean(values: Iterable[float]) -> float:
    """Быстрое среднее (statistics.mean на каждом такте слишком медленно).

    Args:
        values: Числа; не пустой набор.

    Returns:
        Среднее арифметическое.
    """
    items = list(values)
    return sum(items) / len(items)


def balance_residual(state: State, config: AppConfig) -> tuple[float, float] | None:
    """Невязка энергобаланса за окно ``balance_window_s``.

    Args:
        state: Состояние.
        config: Конфигурация (ёмкость АКБ, окно).

    Returns:
        Пара ``(измеренная_мощность_АКБ_Вт, ожидаемая_P_pv−P_load_Вт)`` (заряд — плюс)
        или ``None``, если вычислить нельзя: мало данных, пропадала связь или АКБ упёрлась
        в 0/100 % (изменение заряда уже не отражает баланс).
    """
    minutes = config.crosscheck.balance_window_s / 60.0
    snaps = state.snapshots(minutes)
    if len(snaps) < 3 or any(not s.comms_ok for s in snaps):
        return None
    slope = state.slope("battery_soc_pct", minutes)  # %/мин
    if slope is None:
        return None
    measured = slope / 100.0 * config.energy.battery_capacity_wh * 60.0
    expected = mean(s.pv_power_w - s.load_total_w for s in snaps)
    soc = snaps[-1].battery_soc_pct
    if (soc >= 99.0 and expected > 0) or (soc <= 1.0 and expected < 0):
        return None
    return measured, expected


class CrossChecker:
    """Набор правил сверки источников (держит счётчики устойчивости расхождений)."""

    def __init__(self, config: AppConfig) -> None:
        """Args:
        config: Конфигурация Штаба.
        """
        self._cfg = config
        self._cc = config.crosscheck
        self._since: dict[str, datetime] = {}
        self._running_since: dict[str, datetime] = {}
        self._idle_since: datetime | None = None
        self._cause_done: set[str] = set()

    def run(self, state: State, now: datetime, sent: Sequence[SentCommand] = ()) -> list[Verdict]:
        """Применить все правила к текущему состоянию.

        Args:
            state: Состояние.
            now: Текущее время (для устойчивости расхождений).
            sent: Недавно отправленные команды (для правила «причина → следствие»).

        Returns:
            Вердикты. При потере связи или без данных — пустой список: сверять застывшее нельзя.
        """
        latest = state.latest()
        if latest is None or not latest.comms_ok:
            self._since.clear()
            return []
        out: list[Verdict] = []
        out += self._co2_multi(state, latest, now)
        out += self._co2_single(state, latest, now)
        out += self._co2_rising(state, latest, now)
        out += self._energy_balance(state, now)
        out += self._devices(latest, now)
        out += self._battery_temp(state, now)
        out += self._pv_drop(state, now)
        out += self._thermal(state, latest, now)
        out += self._plan_vs_fact(latest, now)
        out += self._cause_effect(latest, now, sent)
        return out

    # ------------------------------------------------------------------ устойчивость

    def _persist(self, key: str, active: bool, now: datetime, confirm_s: float) -> bool:
        """Держится ли условие дольше ``confirm_s`` секунд."""
        if not active:
            self._since.pop(key, None)
            return False
        first = self._since.setdefault(key, now)
        return (now - first).total_seconds() >= confirm_s

    # ------------------------------------------------------------------ правило 1: датчики CO2

    def _co2_multi(self, state: State, latest: Telemetry, now: datetime) -> list[Verdict]:
        """Несколько датчиков CO2: выброс одного или согласованный реальный рост."""
        sensors = latest.co2_ppm
        if len(sensors) < 2:
            return []
        th = self._cfg.thresholds_for("co2_ppm.x")
        warn = th.warn_high if th and th.warn_high is not None else self._cc.co2_high_ppm
        max_diff = self._cc.co2_sensor_max_diff_ppm
        if len(sensors) == 2:
            a, b = sorted(sensors)
            if abs(sensors[a] - sensors[b]) > max_diff:
                return [
                    Verdict(
                        ts=now,
                        kind="unknown",
                        confidence=0.4,
                        rule="co2_pair_disagreement",
                        fields=[f"co2_ppm.{a}", f"co2_ppm.{b}"],
                        explanation=(
                            f"Датчики CO2 расходятся: {a}={sensors[a]:.0f}, {b}={sensors[b]:.0f} ppm; "
                            "двух датчиков мало, чтобы понять, какой врёт — нужен независимый источник."
                        ),
                    )
                ]
            return self._co2_real(sensors, warn, now)
        med = median(sensors.values())
        outlier, dev = max(((k, abs(v - med)) for k, v in sensors.items()), key=lambda kv: kv[1])
        others = [v for k, v in sensors.items() if k != outlier]
        if dev > max_diff and max(others) - min(others) <= max_diff / 2:
            value = sensors[outlier]
            abrupt = self._was_agreeing(state, outlier, now)
            kind = "false_signal" if abrupt else "sensor_fault"
            how = "резко, после согласованных показаний" if abrupt else "постепенно"
            return [
                Verdict(
                    ts=now,
                    kind=kind,
                    confidence=0.9,
                    rule="co2_outlier",
                    fields=[f"co2_ppm.{outlier}"],
                    explanation=(
                        f"Датчик {outlier} показывает {value:.0f} ppm, остальные {min(others):.0f}–{max(others):.0f} ppm: "
                        f"отклонение {how}; вероятно {'ложный сигнал' if abrupt else 'неисправность датчика'}, "
                        "реального роста CO2 нет (остальные датчики спокойны)."
                    ),
                )
            ]
        return self._co2_real(sensors, warn, now)

    def _was_agreeing(self, state: State, outlier: str, now: datetime) -> bool:
        """Согласовывался ли датчик с остальными ~полторы минуты назад (значит, выброс резкий)."""
        old = state.snapshots(1.5)
        if not old:
            return False
        ref = old[0].co2_ppm
        if outlier not in ref or len(ref) < 3:
            return False
        others = [v for k, v in ref.items() if k != outlier]
        return abs(ref[outlier] - median(others)) <= self._cc.co2_sensor_max_diff_ppm / 2

    def _co2_real(self, sensors: dict[str, float], warn: float, now: datetime) -> list[Verdict]:
        """Все датчики согласны и CO2 повышен — рост реальный."""
        if max(sensors.values()) < warn:
            return []
        lo, hi = min(sensors.values()), max(sensors.values())
        return [
            Verdict(
                ts=now,
                kind="real",
                confidence=0.85,
                rule="co2_consensus_high",
                fields=[f"co2_ppm.{k}" for k in sorted(sensors)],
                explanation=(
                    f"Все датчики CO2 согласны ({lo:.0f}–{hi:.0f} ppm) и выше нормы ({warn:.0f}): рост реальный, "
                    "нужна вентиляция."
                ),
            )
        ]

    def _co2_rising(self, state: State, latest: Telemetry, now: datetime) -> list[Verdict]:
        """Все датчики CO2 растут согласованно: рост реальный (заметен раньше порога)."""
        fields = [f"co2_ppm.{k}" for k in sorted(latest.co2_ppm)]
        if len(fields) < 2:
            return []
        fits = [state.linfit(f, self._cc.co2_rise_window_min) for f in fields]
        if any(f is None or f.r2 < 0.6 or f.slope_per_min < self._cc.co2_rise_ppm_per_min for f in fits):
            return []
        slopes = [f.slope_per_min for f in fits if f is not None]
        return [
            Verdict(
                ts=now,
                kind="real",
                confidence=0.7,
                rule="co2_consensus_rise",
                fields=fields,
                explanation=(
                    f"CO2 растёт согласованно на всех датчиках ({min(slopes):.0f}–{max(slopes):.0f} ppm/мин): "
                    "реальный рост, а не выброс одного датчика."
                ),
            )
        ]

    # ------------------------------------------------------------------ правило 2: один датчик CO2

    def _co2_single(self, state: State, latest: Telemetry, now: datetime) -> list[Verdict]:
        """Единственный датчик CO2: резкий скачок при нормальных температуре и влажности."""
        if len(latest.co2_ppm) != 1:
            return []
        (sensor, value), = latest.co2_ppm.items()
        field = f"co2_ppm.{sensor}"
        prev = state.previous(field)
        if prev is None:
            return []
        lo, hi = self._cc.humidity_normal_range
        calm = (
            latest.temp_c <= self._cc.temp_normal_max_c
            and lo <= latest.humidity_pct <= hi
            and latest.vent_mode == "off"
        )
        if value >= self._cc.co2_high_ppm and value - prev >= self._cc.co2_false_jump_ppm and calm:
            return [
                Verdict(
                    ts=now,
                    kind="false_signal",
                    confidence=0.6,
                    rule="co2_single_context",
                    fields=[field],
                    explanation=(
                        f"CO2 скачком {prev:.0f}→{value:.0f} ppm при нормальных температуре ({latest.temp_c:.1f} °C), "
                        f"влажности ({latest.humidity_pct:.0f} %) и выключенной вытяжке: вероятно ложный сигнал; "
                        "подтвердить вторым источником."
                    ),
                )
            ]
        return []

    # ------------------------------------------------------------------ правило 3: энергобаланс

    def _energy_balance(self, state: State, now: datetime) -> list[Verdict]:
        """dE_bat/dt против P_pv − P_load."""
        res = balance_residual(state, self._cfg)
        mismatch = res is not None and abs(res[0] - res[1]) > self._cc.balance_eps_w
        if not self._persist("balance", mismatch, now, self._cc.balance_confirm_s) or res is None:
            return []
        measured, expected = res
        latest = state.latest()
        lines_sum = sum(line.power_w for line in latest.lines.values()) if latest else 0.0
        consistent = latest is not None and abs(lines_sum - latest.load_total_w) <= self._cc.balance_eps_w / 2
        hidden = consistent and measured < expected  # линии сходятся с общей нагрузкой, а АКБ теряет больше
        direction = "разряжается быстрее" if measured < expected else "разряжается медленнее (заряжается быстрее)"
        if hidden and latest is not None:
            return [
                Verdict(
                    ts=now,
                    kind="real",
                    confidence=0.6,
                    rule="energy_balance",
                    fields=["battery_soc_pct", "pv_power_w", "load_total_w"],
                    explanation=(
                        f"Энергобаланс не сходится: АКБ теряет {measured:+.0f} Вт, а P_pv−P_load = {expected:+.0f} Вт; "
                        f"линии щита ({lines_sum:.0f} Вт) согласуются с общей нагрузкой ({latest.load_total_w:.0f} Вт) — "
                        f"вероятна неучтённая нагрузка ≈{expected - measured:.0f} Вт вне телеметрии."
                    ),
                )
            ]
        return [
            Verdict(
                ts=now,
                kind="sensor_fault",
                confidence=0.6,
                rule="energy_balance",
                fields=["battery_soc_pct", "pv_power_w", "load_total_w"],
                explanation=(
                    f"Энергобаланс не сходится: по изменению заряда АКБ мощность {measured:+.0f} Вт, "
                    f"а P_pv−P_load = {expected:+.0f} Вт (допуск ±{self._cc.balance_eps_w:.0f}); АКБ {direction}, чем "
                    "следует из генерации и нагрузки — врёт один из датчиков (SoC, генерация или нагрузка) "
                    "либо есть неучтённая нагрузка."
                ),
            )
        ]

    # ------------------------------------------------------------------ правило 4: статус и потребление

    def _devices(self, latest: Telemetry, now: datetime) -> list[Verdict]:
        """Статус устройства из телеметрии против его потребления."""
        out: list[Verdict] = []
        for status_field, line in self._cfg.equipment.device_lines.items():
            status = getattr(latest, status_field, DeviceStatus.UNKNOWN)
            spec = latest.lines.get(line)
            if spec is None:
                continue
            power = spec.power_w
            running_low = status is DeviceStatus.RUNNING and power < self._cc.device_running_min_w
            idle_high = status is DeviceStatus.IDLE and power > self._cc.device_idle_max_w
            if self._persist(f"dev-run:{line}", running_low, now, self._cc.device_confirm_s):
                out.append(
                    Verdict(
                        ts=now,
                        kind="sensor_fault",
                        confidence=0.5,
                        rule="device_status_vs_power",
                        fields=[status_field, f"lines.{line}.power_w"],
                        explanation=(
                            f"{status_field}=running, но линия {line} потребляет {power:.0f} Вт: либо устройство "
                            "на самом деле не работает, либо врёт датчик мощности/статуса. Проверить визуально у Оператора."
                        ),
                    )
                )
            if self._persist(f"dev-idle:{line}", idle_high, now, self._cc.device_confirm_s):
                out.append(
                    Verdict(
                        ts=now,
                        kind="sensor_fault",
                        confidence=0.5,
                        rule="device_status_vs_power",
                        fields=[status_field, f"lines.{line}.power_w"],
                        explanation=(
                            f"{status_field}=idle, но линия {line} потребляет {power:.0f} Вт: устройство работает "
                            "без статуса либо статус/датчик мощности врёт. Проверить у Оператора."
                        ),
                    )
                )
        return out

    # ------------------------------------------------------------------ правило 5: температура АКБ

    def _battery_temp(self, state: State, now: datetime) -> list[Verdict]:
        """Рост температуры АКБ против тока АКБ."""
        th = self._cfg.thresholds_for("battery_temp_c")
        if th is None or th.trend_window_min is None or th.max_slope_per_min is None:
            return []
        slope = state.slope("battery_temp_c", th.trend_window_min)
        if slope is None or slope <= th.max_slope_per_min:
            return []
        snaps = state.snapshots(th.trend_window_min)
        net = mean(abs(s.pv_power_w - s.load_total_w) for s in snaps)
        plausible = self._cc.battery_temp_slope_per_kw * net / 1000.0
        if slope > plausible:
            return [
                Verdict(
                    ts=now,
                    kind="sensor_fault",
                    confidence=0.7,
                    rule="battery_temp_plausibility",
                    fields=["battery_temp_c"],
                    explanation=(
                        f"Температура АКБ растёт {slope:+.2f} °C/мин, а при токе АКБ ≈{net:.0f} Вт правдоподобно не более "
                        f"{plausible:.2f} °C/мин: вероятен дрейф датчика. Проверить вторым прибором."
                    ),
                )
            ]
        return [
            Verdict(
                ts=now,
                kind="real",
                confidence=0.5,
                rule="battery_temp_plausibility",
                fields=["battery_temp_c"],
                explanation=(
                    f"Температура АКБ растёт {slope:+.2f} °C/мин при токе ≈{net:.0f} Вт (правдоподобно до {plausible:.2f}): "
                    "рост согласуется с нагрузкой, считать реальным до проверки."
                ),
            )
        ]

    # ------------------------------------------------------------------ правило 6: падение генерации

    def _pv_drop(self, state: State, now: datetime) -> list[Verdict]:
        """Падение генерации подтверждается балансом АКБ."""
        anchor = state.now or now
        recent = state.series("pv_power_w", 0.5)
        before = [v for t, v in state.series("pv_power_w", 2.5) if (anchor - t).total_seconds() >= 60.0]
        if len(recent) < 5 or len(before) < 5:
            return []
        now_pv, was_pv = mean(v for _, v in recent), mean(before)
        if was_pv < 100.0 or now_pv >= was_pv * self._cc.pv_drop_ratio:
            return []
        res = balance_residual(state, self._cfg)
        if res is None or abs(res[0] - res[1]) > self._cc.balance_eps_w:
            return [
                Verdict(
                    ts=now,
                    kind="unknown",
                    confidence=0.4,
                    rule="pv_drop",
                    fields=["pv_power_w"],
                    explanation=(
                        f"Генерация упала с ≈{was_pv:.0f} до ≈{now_pv:.0f} Вт, но баланс АКБ это пока не подтверждает "
                        "(мало данных или расхождение) — проверять."
                    ),
                )
            ]
        return [
            Verdict(
                ts=now,
                kind="real",
                confidence=0.8,
                rule="pv_drop",
                fields=["pv_power_w"],
                explanation=(
                    f"Генерация упала с ≈{was_pv:.0f} до ≈{now_pv:.0f} Вт, и энергобаланс АКБ это подтверждает "
                    f"(АКБ {res[0]:+.0f} Вт при P_pv−P_load={res[1]:+.0f} Вт): падение реальное."
                ),
            )
        ]

    # ------------------------------------------------------------------ правило 7: тепловой баланс

    def _thermal(self, state: State, latest: Telemetry, now: datetime) -> list[Verdict]:
        """Температура растёт при простое оборудования: скрытый источник тепла или дрейф датчика."""
        busy = any(
            getattr(latest, f, DeviceStatus.UNKNOWN) is DeviceStatus.RUNNING for f in self._cfg.equipment.device_lines
        )
        if busy:
            self._idle_since = None
            self._since.pop("thermal", None)
            return []
        if self._idle_since is None:
            self._idle_since = now
        window = self._cc.thermal_window_min
        if (now - self._idle_since).total_seconds() < window * 60.0 or latest.temp_c_2 is None:
            return []
        f1, f2 = state.linfit("temp_c", window), state.linfit("temp_c_2", window)
        limit = self._cc.thermal_idle_slope_per_min
        if f1 is None or f2 is None:
            return []
        r1, r2 = f1.slope_per_min > limit and f1.r2 > 0.5, f2.slope_per_min > limit and f2.r2 > 0.5
        if not self._persist("thermal", r1 or r2, now, 30.0):
            return []
        w = self._cfg.verdicts.weights.get("thermal_balance", 0.6)
        if r1 and r2:
            return [
                Verdict(
                    ts=now,
                    kind="real",
                    confidence=0.6,
                    rule="thermal_balance",
                    fields=["temp_c", "temp_c_2"],
                    explanation=(
                        f"Температура растёт при простое оборудования на обоих датчиках ({f1.slope_per_min:+.2f} и "
                        f"{f2.slope_per_min:+.2f} °C/мин): вероятен скрытый источник тепла."
                    ),
                    evidence=[
                        Evidence(source="temp_c", supports="real", weight=w / 1.4, detail="рост температуры при простое", rule="thermal_balance"),
                        Evidence(source="temp_c_2", supports="real", weight=w / 1.4, detail="рост температуры при простое", rule="thermal_balance"),
                    ],
                )
            ]
        bad, good, fb, fg = ("temp_c", "temp_c_2", f1, f2) if r1 else ("temp_c_2", "temp_c", f2, f1)
        return [
            Verdict(
                ts=now,
                kind="drift",
                confidence=0.6,
                rule="thermal_balance",
                fields=[bad],
                explanation=(
                    f"{bad} растёт {fb.slope_per_min:+.2f} °C/мин при простое оборудования, а {good} — {fg.slope_per_min:+.2f}: "
                    f"вероятен дрейф датчика {bad}."
                ),
                evidence=[Evidence(source=good, supports="false", weight=w, detail=f"{good} не растёт при простое", rule="thermal_balance", kind_hint="drift")],
            )
        ]

    # ------------------------------------------------------------------ правило 8: план против факта

    def _plan_vs_fact(self, latest: Telemetry, now: datetime) -> list[Verdict]:
        """Устройство `running`, а потребление не соответствует технологической карте (тихий брак)."""
        out: list[Verdict] = []
        for status_field, line in self._cfg.equipment.device_lines.items():
            status = getattr(latest, status_field, DeviceStatus.UNKNOWN)
            spec = latest.lines.get(line)
            nominal = self._cfg.equipment.lines[line].nominal_w
            if status is not DeviceStatus.RUNNING or spec is None or nominal <= 0:
                self._running_since.pop(line, None)
                self._since.pop(f"pvf:{line}", None)
                continue
            since = self._running_since.setdefault(line, now)
            low = (now - since).total_seconds() >= self._cc.plan_vs_fact_ramp_s and spec.power_w < self._cc.plan_vs_fact_min_ratio * nominal
            if not self._persist(f"pvf:{line}", low, now, self._cc.plan_vs_fact_confirm_s):
                continue
            w = self._cfg.verdicts.weights.get("plan_vs_fact", 0.7)
            out.append(
                Verdict(
                    ts=now,
                    kind="real",
                    confidence=0.6,
                    rule="plan_vs_fact",
                    fields=[status_field, f"lines.{line}.power_w"],
                    explanation=(
                        f"Тихий брак: {status_field}=running, но линия {line} потребляет {spec.power_w:.0f} Вт при плане ≈{nominal:.0f} Вт "
                        f"({spec.power_w / nominal:.0%}); по технологической карте работа должна идти — проверить у Оператора и по видео."
                    ),
                    evidence=[
                        Evidence(source="power_meter", supports="real", weight=w, detail=f"потребление {spec.power_w:.0f} Вт против плана {nominal:.0f} Вт", rule="plan_vs_fact")
                    ],
                )
            )
        return out

    # ------------------------------------------------------------------ правило 9: причина и следствие

    def _cause_effect(self, latest: Telemetry, now: datetime, sent: Sequence[SentCommand]) -> list[Verdict]:
        """После нашей команды ожидаем отклик в нескольких каналах (состояние и мощность линии)."""
        out: list[Verdict] = []
        for rec in sent:
            cid = rec.command.id
            if cid in self._cause_done or (now - rec.sent_at).total_seconds() < self._cc.cause_effect_window_s:
                continue
            if rec.state_path is None or rec.state_before == rec.state_expected:
                self._cause_done.add(cid)  # менять нечего: отклика ждать не от чего
                continue
            self._cause_done.add(cid)
            state_ok = latest.lookup(rec.state_path) == rec.state_expected
            power_now = latest.lookup(rec.power_path) if rec.power_path else None
            has_power = rec.power_before is not None and power_now is not None
            responded = has_power and abs(float(power_now) - float(rec.power_before)) >= self._cc.cause_effect_min_delta_w  # type: ignore[arg-type]
            if not has_power or (state_ok and responded):
                continue
            w = self._cfg.verdicts.weights.get("cause_effect", 0.7)
            name = f"{rec.command.name} {rec.command.args}"
            subject_state = rec.state_path
            if not state_ok and not responded:
                out.append(
                    Verdict(
                        ts=now,
                        kind="real",
                        confidence=0.6,
                        rule="cause_effect",
                        fields=[f"actuator:{rec.executor}", subject_state],
                        explanation=(
                            f"После команды {name} нет отклика ни в состоянии ({rec.state_path}), ни в мощности линии "
                            f"({rec.power_path}) через {self._cc.cause_effect_window_s:g} с: отказ исполнителя {rec.executor}."
                        ),
                        evidence=[
                            Evidence(source="state", supports="real", weight=w / 1.4, detail="состояние не изменилось", rule="cause_effect"),
                            Evidence(source="power", supports="real", weight=w / 1.4, detail="мощность линии не изменилась", rule="cause_effect"),
                        ],
                    )
                )
            else:
                which = "состояние изменилось, а мощность линии — нет" if state_ok else "мощность линии изменилась, а состояние — нет"
                src = "power" if state_ok else "state"
                out.append(
                    Verdict(
                        ts=now,
                        kind="channel_fault",
                        confidence=0.6,
                        rule="cause_effect",
                        fields=[subject_state],
                        explanation=f"После команды {name}: {which} — один из каналов телеметрии врёт.",
                        evidence=[Evidence(source=src, supports="false", weight=w, detail=which, rule="cause_effect", kind_hint="channel_fault")],
                    )
                )
        return out
