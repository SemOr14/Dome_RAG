"""Правила агента: детерминированные проверки безопасности и производства.

Правила не «долбят» командами: на каждый эпизод — одно действие, затем проверка
результата по телеметрии. LLM здесь не используется, только факты.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from model import Snap

MACHINES = {"printer_3d_01": "3D-принтер", "cnc_01": "ЧПУ-фрезер"}
CRITICAL_NODES = (
    "printer_3d_01",
    "cnc_01",
    "smart_panel_01",
    "dome_automation_01",
    "climate_sensor_01",
    "supply_ventilation_01",
)


@dataclass
class Action:
    node: str
    action: str
    value: Any = None
    expect: str = ""
    check: Callable[[Snap], bool] | None = None
    timeout: float = 20.0
    required: bool = True


@dataclass
class Decision:
    key: str
    situation: str
    cause: str
    confidence: float
    evidence: list = field(default_factory=list)
    plan: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    message: str | None = None
    message_if: Callable[[Snap], bool] | None = None
    message_to: str = "operator"
    source: str = "rules"
    cooldown: float = 60.0
    severity: int = 1


def ev(node: str, param: str, value: Any = None) -> dict:
    return {"node": node, "param": param, "value": value}


class Rules:
    def __init__(self, state: dict | None = None) -> None:
        self.state = state if state is not None else {}
        for key in ("resume", "line_resets", "messages", "started"):
            self.state.setdefault(key, {})

    # ------------------------------------------------------------------ публичный вход
    def evaluate(self, s: Snap, ctx: dict) -> list[Decision]:
        out: list[Decision] = []
        for fn in (
            self._stale,
            self._lines,
            self._smoke,
            self._holds,
            self._bed,
            self._start_print,
            self._start_cnc,
            self._restore_lines,
            self._energy,
            self._comms,
            self._vent,
        ):
            try:
                result = fn(s, ctx)
            except Exception as exc:  # noqa: BLE001 — правило не должно ронять цикл
                ctx.setdefault("errors", []).append(f"{fn.__name__}: {exc}")
                continue
            if result is None:
                continue
            out.extend(result if isinstance(result, list) else [result])
        return out

    # ------------------------------------------------------------------ связь
    def _stale(self, s: Snap, ctx: dict) -> Decision | None:
        max_age = ctx.get("stale_after", 30.0)
        for nid in CRITICAL_NODES:
            if not s.node(nid):
                continue
            if s.stale(nid, max_age):
                return Decision(
                    key=f"stale:{nid}",
                    situation=f"нет свежих данных по {nid} (status={s.get(nid, 'status')}, возраст {s.age(nid):.0f} с)",
                    cause="потеря связи с узлом или зависание телеметрии",
                    confidence=0.7,
                    evidence=[ev(nid, "updated_at", s.node(nid).get("updated_at")), ev(nid, "status", s.get(nid, "status"))],
                    plan=[f"переподключить узел {nid}", "проверить, что данные обновились"],
                    actions=[Action(nid, "reconnect", None, "status != UNKNOWN и свежие данные",
                                    lambda snap, n=nid: not snap.stale(n, max_age), 30)],
                    cooldown=150,
                    severity=2,
                )
        return None

    # ------------------------------------------------------------------ щиток
    def _lines(self, s: Snap, ctx: dict) -> list[Decision] | None:
        out = []
        for i in range(1, 9):
            ln = s.line(i)
            if not ln:
                continue
            state = ln.get("state")
            prot = ln.get("protection_state")
            if state in ("TRIPPED", "RCD_TRIP") or prot in ("TRIPPED", "RCD_TRIPPED"):
                if (ln.get("lockout_remaining_s") or 0) > 0:
                    continue
                if self._reset_count(i) >= 2:
                    continue
                name = ln.get("name") or f"линия {i}"
                out.append(Decision(
                    key=f"line:{i}:trip",
                    situation=f"сработала защита: {name} ({state or prot})",
                    cause="перегрузка или УЗО; включение без сброса не сработает",
                    confidence=0.9,
                    evidence=[ev("smart_panel_01", f"lines[{i-1}].state", state),
                              ev("smart_panel_01", f"lines[{i-1}].current_a", ln.get("current_a"))],
                    plan=[f"взвести защиту линии {i}", "проверить состояние и ток"],
                    actions=[Action("smart_panel_01", "reset_protection", i, "line.state == ON",
                                    lambda snap, n=i: snap.line(n).get("state") == "ON", 15)],
                    cooldown=300,
                    severity=2,
                ))
                self._note_reset(i)
        return out or None

    def _reset_count(self, line: int) -> int:
        now = time.time()
        items = [t for t in self.state["line_resets"].get(str(line), []) if now - t < 900]
        self.state["line_resets"][str(line)] = items
        return len(items)

    def _note_reset(self, line: int) -> None:
        self.state["line_resets"].setdefault(str(line), []).append(time.time())

    # ------------------------------------------------------------------ дым
    def _smoke(self, s: Snap, ctx: dict) -> Decision | None:
        for sid in ("smoke_detector_01", "smoke_detector_02"):
            d = s.node(sid)
            if not d or not d.get("smoke_detected"):
                continue
            false_suspected = bool(d.get("false_alarm_suspected"))
            other = "smoke_detector_02" if sid == "smoke_detector_01" else "smoke_detector_01"
            if false_suspected or not s.get(other, "smoke_detected"):
                return Decision(
                    key=f"smoke:{sid}",
                    situation=f"{sid}: дым, но второй датчик молчит (false_alarm_suspected={false_suspected})",
                    cause="ложное срабатывание датчика дыма",
                    confidence=0.65,
                    evidence=[ev(sid, "smoke_detected", True), ev(sid, "obscuration_pct", d.get("obscuration_pct")),
                              ev(other, "smoke_detected", s.get(other, "smoke_detected"))],
                    plan=["квитировать событие автоматики", "сбросить тревогу датчика", "не останавливать производство"],
                    actions=[
                        Action("dome_automation_01", "acknowledge", "all", "событие квитировано", None, 10),
                        Action(sid, "reset_alarm", None, "smoke_detected == false",
                               lambda snap, n=sid: not snap.get(n, "smoke_detected"), 15),
                    ],
                    message="Датчик дыма дал ложную тревогу, производство не останавливаю. Проверьте зону, если есть запах.",
                    cooldown=180,
                    severity=3,
                )
            return Decision(
                key=f"smoke:{sid}:real",
                situation=f"{sid}: дым подтверждён",
                cause="возможное возгорание",
                confidence=0.6,
                evidence=[ev(sid, "smoke_detected", True), ev(sid, "obscuration_pct", d.get("obscuration_pct"))],
                plan=["не сбрасывать тревогу", "включить вытяжку", "сообщить Оператору"],
                actions=[Action("fume_extraction_01", "turn_on", None, "вытяжка включена",
                                lambda snap: snap.get("fume_extraction_01", "enabled") is True, 10)],
                message="ВНИМАНИЕ: датчик дыма показывает дым. Проверьте зону, при подтверждении — аварийная остановка.",
                cooldown=120,
                severity=5,
            )
        return None

    # ------------------------------------------------------------------ удержания станков
    def _holds(self, s: Snap, ctx: dict) -> list[Decision] | None:
        out = []
        for nid, title in MACHINES.items():
            d = s.node(nid)
            if not d:
                continue
            hold = d.get("hold_level")
            if not hold:
                self.state["resume"][nid] = {"reason": None, "count": 0}
                continue
            if (d.get("lockout_remaining_s") or 0) > 0:
                continue
            reason = d.get("hold_reason")
            decision = self._hold_decision(s, nid, title, hold, reason, d)
            if decision:
                out.append(decision)
        return out or None

    def _hold_decision(self, s: Snap, nid: str, title: str, hold: str, reason: str | None, d: dict) -> Decision | None:
        prep: list[Action] = []
        cleared = False
        cause = f"удержание {reason or 'без кода'}"
        confidence = 0.5
        evidence = [ev(nid, "hold_level", hold), ev(nid, "hold_reason", reason),
                    ev(nid, "resume_attempts", d.get("resume_attempts"))]
        message = None

        if reason == "IL_CO2":
            verdict, primary, values, why = self._co2_verdict(s)
            evidence += [ev(k, "co2_ppm", v) for k, v in values.items()]
            evidence.append(ev("dome_automation_01", "primary_co2_sensor", primary))
            if verdict == "false":
                best = max(((k, v) for k, v in values.items() if k != primary and v is not None),
                           key=lambda kv: kv[1], default=(None, None))[0]
                cleared = True
                cause = f"ложное показание {primary}: {values.get(primary)} ppm против {why} у остальных"
                confidence = 0.85
                # второй станок не должен повторять ту же настройку автоматики
                now_ts = time.time()
                recent_fix = (now_ts - float(self.state.get("co2_fixed_at") or 0)) < 90
                if s.interlock("IL_CO2").get("state") != "TRIPPED" or recent_fix:
                    prep = []
                else:
                    self.state["co2_fixed_at"] = now_ts
                    prep = [
                        Action("dome_automation_01", "set_data_trust",
                               {"node": primary, "param": "co2_ppm", "trust": "UNRELIABLE"},
                               f"{primary}.co2_ppm помечен UNRELIABLE", None, 10),
                        Action("dome_automation_01", "set_primary_sensor", best,
                               f"основной датчик CO2 = {best}", None, 10),
                        Action("dome_automation_01", "reset_interlock", "IL_CO2",
                               "IL_CO2 снят", lambda snap: snap.interlock("IL_CO2").get("state") != "TRIPPED", 15),
                    ]
            elif verdict == "real":
                cleared = False
                cause = "реальный рост CO2: все датчики выше нормы"
                confidence = 0.8
                prep = [
                    Action("supply_ventilation_01", "set_speed", 100, "приток 100%", None, 10),
                    Action("supply_ventilation_01", "set_mode", "FRESH_AIR", "режим FRESH_AIR", None, 10),
                ]
                message = (f"Духота: CO2 {values.get('climate_sensor_01'):.0f} ppm на всех датчиках. "
                           f"Приток выведен на 100 %. Проверьте, не перекрыт ли воздуховод.")
            else:
                return Decision(
                    key=f"hold:{nid}:{reason}:unknown",
                    situation=f"{title} удержан по {reason}, датчики CO2 расходятся",
                    cause="недостаточно данных, чтобы отличить ложную тревогу от реальной",
                    confidence=0.3,
                    evidence=evidence,
                    plan=["собрать ещё данные", "не снимать удержание"],
                    cooldown=90,
                    severity=2,
                )

        elif reason == "IL_CONDENSATE":
            temp = s.raw("climate_sensor_01", "temperature_c")
            dew = s.raw("climate_sensor_01", "dew_point_c")
            hum = s.raw("climate_sensor_01", "humidity_pct")
            if isinstance(temp, (int, float)) and isinstance(dew, (int, float)) and isinstance(hum, (int, float)):
                evidence += [ev("climate_sensor_01", "temperature_c", temp), ev("climate_sensor_01", "dew_point_c", dew)]
                if hum < 88 and (temp - dew) > 2.0:
                    cleared = True
                    confidence = 0.7
                    cause = f"конденсат: RH {hum:.0f} %, T-Td {temp - dew:.1f} °C — условие ушло"
                    prep = [Action("dome_automation_01", "reset_interlock", "IL_CONDENSATE",
                                   "IL_CONDENSATE снят",
                                   lambda snap: snap.interlock("IL_CONDENSATE").get("state") != "TRIPPED", 15)]
            else:
                return self._wait_hold(nid, title, reason, evidence, "нет данных о точке росы")

        elif reason == "IL_SMOKE":
            smoke = s.get("smoke_detector_01", "smoke_detected") or s.get("smoke_detector_02", "smoke_detected")
            if not smoke:
                cleared = True
                confidence = 0.7
                cause = "ложное срабатывание IL_SMOKE: датчики дыма чисты"
                prep = [
                    Action("dome_automation_01", "acknowledge", "all", "событие квитировано", None, 10),
                    Action("dome_automation_01", "reset_interlock", "IL_SMOKE", "IL_SMOKE снят",
                           lambda snap: snap.interlock("IL_SMOKE").get("state") != "TRIPPED", 15),
                ]
                if s.line(1).get("state") == "OFF":
                    prep.append(Action("smart_panel_01", "line_on", 1, "линия 1 включена",
                                       lambda snap: snap.line(1).get("state") == "ON", 15))
            else:
                return Decision(
                    key=f"hold:{nid}:smoke",
                    situation=f"{title} удержан по IL_SMOKE, датчик показывает дым",
                    cause="возможное возгорание",
                    confidence=0.6, evidence=evidence,
                    plan=["не снимать удержание", "сообщить Оператору"],
                    message="Станки удержаны по дыму. Проверьте зону ФабЛаб.",
                    cooldown=120, severity=5,
                )

        elif reason == "UNDERVOLTAGE":
            bus = s.raw("smart_panel_01", "bus_voltage_v")
            soc = s.soc()
            evidence += [ev("smart_panel_01", "bus_voltage_v", bus), ev("battery_01", "soc_pct", soc)]
            if isinstance(bus, (int, float)) and bus > 210 and (soc is None or soc > 30):
                cleared = True
                confidence = 0.7
                cause = f"просадка напряжения ушла: шина {bus:.0f} В, SOC {soc}"
            else:
                return self._wait_hold(nid, title, reason, evidence, f"напряжение шины {bus}, SOC {soc}")

        elif reason == "BED_COLLISION":
            return Decision(
                key=f"hold:{nid}:bed",
                situation=f"{title} в ALARM из-за столкновения с деталью на столе",
                cause="на столе принтера осталась деталь",
                confidence=0.9, evidence=evidence,
                plan=["дождаться, пока Оператор уберёт деталь", "не пытаться продолжать"],
                message="Принтер заблокирован: на столе деталь. Уберите её из бокса — я продолжу печать после сброса.",
                cooldown=300, severity=3,
            )

        elif reason == "FILAMENT_RUNOUT":
            return Decision(
                key=f"hold:{nid}:filament",
                situation=f"{title}: закончился филамент",
                cause="катушка пуста",
                confidence=0.95, evidence=evidence,
                plan=["ждать замены катушки Оператором", "resume до замены бесполезен"],
                message="Печать стоит: закончился филамент. Замените катушку, я продолжу печать.",
                cooldown=300, severity=3,
            )

        else:
            return self._wait_hold(nid, title, reason, evidence, "причина неизвестна")

        # ---- resume, но не больше одной попытки на эпизод
        episode = self.state["resume"].get(nid) or {}
        attempts = int(episode.get("count") or 0) if episode.get("reason") == reason else 0
        server_attempts = int(d.get("resume_attempts") or 0)
        if attempts >= 1 or server_attempts >= 2:
            return Decision(
                key=f"hold:{nid}:{reason}:exhausted",
                situation=f"{title}: удержание {reason} не снялось после попытки продолжения",
                cause=cause,
                confidence=max(confidence, 0.5),
                evidence=evidence,
                plan=["не повторять resume", "разобраться в причине", "при необходимости позвать Оператора"],
                message=f"{title} не вышел из удержания {reason}. Нужна проверка на месте.",
                cooldown=240,
                severity=3,
            )

        actions = list(prep)
        if cleared:
            actions.append(Action(nid, "resume", None, f"{nid}: hold снят, работа продолжена",
                                  lambda snap, n=nid: snap.node(n).get("hold_level") is None, 25))
            self.state["resume"][nid] = {"reason": reason, "count": attempts + 1}

        plan = [a.expect or a.action for a in actions]
        return Decision(
            key=f"hold:{nid}:{reason}",
            situation=f"{title} удержан: {hold} / {reason}",
            cause=cause,
            confidence=confidence,
            evidence=evidence,
            plan=plan,
            actions=actions,
            message=message,
            cooldown=45,
            severity=3 if cleared else 2,
        )

    def _wait_hold(self, nid: str, title: str, reason: str | None, evidence: list, why: str) -> Decision:
        return Decision(
            key=f"hold:{nid}:{reason}:wait",
            situation=f"{title} удержан по {reason or 'без кода'}",
            cause=why,
            confidence=0.4,
            evidence=evidence,
            plan=["ждать, пока условие удержания уйдёт", "не пытаться продолжать вслепую"],
            cooldown=120,
            severity=2,
        )

    @staticmethod
    def _co2_verdict(s: Snap) -> tuple[str, str, dict, str]:
        values = s.co2_values()
        primary = s.primary_co2_sensor()
        known = {k: v for k, v in values.items() if v is not None}
        if len(known) < 2:
            return "unknown", primary, values, "мало датчиков"
        prim = known.get(primary)
        others = {k: v for k, v in known.items() if k != primary}
        if prim is not None and others:
            others_max = max(others.values())
            others_min = min(others.values())
            if prim >= 2000 and others_max < 1200 and (prim - others_max) >= 800:
                return "false", primary, values, f"{others_min:.0f}..{others_max:.0f} ppm"
        if all(v >= 1200 for v in known.values()) and (max(known.values()) - min(known.values())) < 900:
            return "real", primary, values, "все датчики выше 1200"
        return "unknown", primary, values, "показания расходятся"

    # ------------------------------------------------------------------ деталь на столе
    def _bed(self, s: Snap, ctx: dict) -> Decision | None:
        pr = s.node("printer_3d_01")
        if not pr:
            return None
        state = pr.get("state")
        bed_clear = pr.get("bed_clear")
        if state != "COMPLETED" and not (state == "IDLE" and bed_clear is False):
            return None
        locks = s.raw("thermal_insulation_01", "locks") or []
        lock_state = locks[0] if locks else None
        actions = []
        if lock_state == "LOCKED":
            actions.append(Action("thermal_insulation_01", "set_lock", {"lock": 0, "locked": False},
                                  "замок бокса открыт или деталь уже забрали",
                                  lambda snap: (snap.raw("thermal_insulation_01", "locks") or [None])[0] == "UNLOCKED"
                                  or snap.get("printer_3d_01", "bed_clear") is True,
                                  15))
        return Decision(
            key="bed:part_ready",
            situation=f"печать завершена ({pr.get('file_name') or 'файл'}), деталь на столе принтера",
            cause="принтер закончил задание",
            confidence=0.95,
            evidence=[ev("printer_3d_01", "state", state), ev("printer_3d_01", "bed_clear", bed_clear),
                      ev("thermal_insulation_01", "locks[0]", lock_state)],
            plan=["открыть замок бокса", "сообщить Оператору, что деталь готова"],
            actions=actions,
            message="Деталь напечатана, замок бокса открыт. Заберите деталь — без этого следующая печать невозможна.",
            message_if=lambda snap: snap.get("printer_3d_01", "bed_clear") is not True,
            cooldown=240,
            severity=3,
        )

    # ------------------------------------------------------------------ запуск печати
    def _start_print(self, s: Snap, ctx: dict) -> Decision | None:
        task = (ctx.get("task") or {})
        file_name = task.get("print_file")
        if not file_name:
            return None
        pr = s.node("printer_3d_01")
        state = pr.get("state")
        if state in ("PRINTING", "PAUSED"):
            if pr.get("file_name") == file_name:
                return None
        if state != "IDLE" or pr.get("bed_clear") is not True:
            return None
        if self.state["started"].get("print") == file_name and state != "IDLE":
            return None
        actions = []
        if not pr.get("homed"):
            actions.append(Action("printer_3d_01", "home", None, "принтер homed",
                                  lambda snap: snap.get("printer_3d_01", "homed") is True, 60))
        actions.append(Action("printer_3d_01", "start_job", file_name, f"печать {file_name} запущена",
                              lambda snap: snap.get("printer_3d_01", "state") in ("PRINTING", "PAUSED"), 45))
        self.state["started"]["print"] = file_name
        return Decision(
            key=f"print:{file_name}",
            situation=f"получено задание на печать {file_name}, принтер свободен",
            cause="задание Оператора",
            confidence=0.95,
            evidence=[ev("printer_3d_01", "state", state), ev("printer_3d_01", "bed_clear", pr.get("bed_clear"))],
            plan=[f"запустить печать {file_name}"],
            actions=actions,
            cooldown=30,
            severity=3,
        )

    # ------------------------------------------------------------------ запуск фрезеровки
    def _start_cnc(self, s: Snap, ctx: dict) -> Decision | None:
        task = ctx.get("task") or {}
        file_name = task.get("cnc_file")
        if not file_name:
            return None
        d = s.node("cnc_01")
        state = d.get("state")
        if state not in ("IDLE",) or d.get("file_name"):
            return None
        if d.get("alarm") or d.get("hold_level"):
            return None
        probe_ok = bool((d.get("last_probe") or {}).get("success"))
        actions: list[Action] = []
        if not d.get("homed"):
            actions.append(Action("cnc_01", "home", None, "фрезер homed",
                                  lambda snap: snap.get("cnc_01", "homed") is True, 90))
        if not probe_ok:
            actions.append(Action("cnc_01", "probe_z", {"distance": 10, "feed": 50},
                                  "пробинг заготовки выполнен",
                                  lambda snap: bool((snap.get("cnc_01", "last_probe") or {}).get("success")), 60))
            actions.append(Action("cnc_01", "set_zero", "xy", "ноль XY выставлен", None, 10))
        actions.append(Action("cnc_01", "start_job", file_name, f"фрезеровка {file_name} запущена",
                              lambda snap: snap.get("cnc_01", "state") in ("RUNNING", "PAUSED"), 45))
        self.state["started"]["cnc"] = file_name
        return Decision(
            key=f"cnc:{file_name}",
            situation=f"задание на фрезеровку {file_name}, станок свободен",
            cause="задание Оператора",
            confidence=0.9,
            evidence=[ev("cnc_01", "state", state), ev("cnc_01", "homed", d.get("homed")),
                      ev("cnc_01", "last_probe", d.get("last_probe"))],
            plan=[a.expect or a.action for a in actions],
            actions=actions,
            message=f"Плата: выставляю ноль по пробнику и запускаю {file_name}."
            if not probe_ok else f"Плата: запускаю фрезеровку {file_name}.",
            cooldown=40,
            severity=3,
        )

    # ------------------------------------------------------------------ возврат линий
    def _restore_lines(self, s: Snap, ctx: dict) -> Decision | None:
        """После сброса нагрузки автоматикой возвращает производственные линии."""
        if s.interlock("IL_LOAD_SHED").get("state") != "ARMED":
            self.state["shed_seen"] = True
            return None
        if not self.state.get("shed_seen"):
            return None
        soc = s.soc()
        autonomy = s.autonomy_h() or 0
        if soc is None or soc < 45 or autonomy < 3:
            return None
        for i in (1, 3, 4):
            ln = s.line(i)
            if ln.get("state") != "OFF" or (ln.get("lockout_remaining_s") or 0) > 0:
                continue
            self.state["shed_seen"] = False
            return Decision(
                key=f"restore:line{i}",
                situation=f"линия {i} ({ln.get('name')}) отключена после сброса нагрузки, энергия восстановлена",
                cause="автоматика снимала нагрузку при низком SOC",
                confidence=0.75,
                evidence=[ev("battery_01", "soc_pct", soc), ev("dome_automation_01", "autonomy_forecast_h", autonomy),
                          ev("smart_panel_01", f"lines[{i-1}].state", ln.get("state"))],
                plan=[f"включить линию {i}"],
                actions=[Action("smart_panel_01", "line_on", i, f"линия {i} включена",
                                lambda snap, n=i: snap.line(n).get("state") == "ON", 15)],
                cooldown=300,
                severity=2,
            )
        return None

    # ------------------------------------------------------------------ энергия
    def _energy(self, s: Snap, ctx: dict) -> Decision | None:
        soc = s.soc()
        autonomy = s.autonomy_h()
        deficit = s.raw("solar_inverter_01", "power_deficit_w") or 0
        diz = s.node("dizel_1")
        state = diz.get("state")
        fuel = s.raw("fuel_tank_01", "level_pct")
        if state == "ON":
            if soc is not None and soc > 85 and deficit <= 0 and (autonomy is None or autonomy > 6):
                return Decision(
                    key="energy:diesel_off",
                    situation=f"SOC {soc:.0f} %, дефицита нет — дизель больше не нужен",
                    cause="энергобаланс восстановлен",
                    confidence=0.8,
                    evidence=[ev("battery_01", "soc_pct", soc), ev("solar_inverter_01", "power_deficit_w", deficit)],
                    plan=["выключить дизель"],
                    actions=[Action("dizel_1", "turn_off", None, "дизель выключен",
                                    lambda snap: snap.get("dizel_1", "state") != "ON", 20)],
                    cooldown=300, severity=2,
                )
            return None
        trigger = (soc is not None and soc < 30) or (autonomy is not None and autonomy < 1.0) or deficit > 400
        if not trigger:
            return None
        if isinstance(fuel, (int, float)) and fuel < 10:
            return Decision(
                key="energy:no_fuel",
                situation=f"нужен дизель, но топлива {fuel:.0f} %",
                cause="низкий запас топлива",
                confidence=0.7,
                evidence=[ev("fuel_tank_01", "level_pct", fuel)],
                plan=["сообщить Оператору", "снизить нагрузку"],
                message=f"Энергии мало (SOC {soc}), а в баке {fuel:.0f} % — нужна заправка или снижение нагрузки.",
                cooldown=300, severity=4,
            )
        return Decision(
            key="energy:diesel_on",
            situation=f"энергии мало: SOC {soc}, автономность {autonomy} ч, дефицит {deficit} Вт",
            cause="разряд АКБ / дефицит генерации",
            confidence=0.8,
            evidence=[ev("battery_01", "soc_pct", soc), ev("dome_automation_01", "autonomy_forecast_h", autonomy),
                      ev("solar_inverter_01", "power_deficit_w", deficit)],
            plan=["запустить дизель-генератор", "проверить, что он вышел на мощность"],
            actions=[
                Action("dizel_1", "preheat", None, "предпусковой подогрев", None, 5),
                Action("dizel_1", "turn_on", None, "дизель работает",
                       lambda snap: snap.get("dizel_1", "state") == "ON" or (snap.get("dizel_1", "power_w") or 0) > 0, 40),
            ],
            cooldown=240, severity=3,
        )

    # ------------------------------------------------------------------ связь
    def _comms(self, s: Snap, ctx: dict) -> Decision | None:
        link = s.node("network_link_01")
        if not link:
            return None
        state = link.get("link_state")
        if state in ("OK", "UP", None):
            return None
        backup = s.node("backup_comms_01")
        actions = []
        if backup.get("available"):
            actions.append(Action("backup_comms_01", "switch_channel", "LORA", "резервный канал LORA",
                                  lambda snap: snap.get("backup_comms_01", "active_channel") == "LORA", 20))
        return Decision(
            key="comms:link",
            situation=f"основной канал связи {state}, потери {link.get('packet_loss_pct')} %",
            cause="деградация канала или патчкорда",
            confidence=0.7,
            evidence=[ev("network_link_01", "link_state", state),
                      ev("network_link_01", "packet_loss_pct", link.get("packet_loss_pct"))],
            plan=["перевести связь на резервный канал", "попросить Оператора проверить патчкорд"],
            actions=actions,
            message="Основной канал связи деградировал, перешёл на резервный. Проверьте патчкорд на сетевом узле.",
            cooldown=300, severity=3,
        )

    # ------------------------------------------------------------------ вентиляция
    def _vent(self, s: Snap, ctx: dict) -> Decision | None:
        co2 = s.raw("climate_sensor_01", "co2_ppm")
        if not isinstance(co2, (int, float)) or co2 < 1200:
            return None
        if s.interlock("IL_CO2").get("state") == "TRIPPED":
            return None  # автоматика уже вывела приток на 100 %
        supply = s.node("supply_ventilation_01")
        speed = supply.get("speed_pct") or 0
        if supply.get("state") != "ON" or speed >= 80:
            return None
        return Decision(
            key="vent:co2",
            situation=f"CO2 {co2:.0f} ppm, приток {speed:.0f} %",
            cause="недостаточный воздухообмен",
            confidence=0.65,
            evidence=[ev("climate_sensor_01", "co2_ppm", co2), ev("supply_ventilation_01", "speed_pct", speed)],
            plan=["увеличить приток до 80 %"],
            actions=[Action("supply_ventilation_01", "set_speed", 80, "приток 80 %",
                            lambda snap: (snap.get("supply_ventilation_01", "speed_pct") or 0) >= 75, 20)],
            cooldown=300, severity=2,
        )
