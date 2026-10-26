"""Мозг агента: цикл наблюдения, решений, действий и проверки результата."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any

from agent_log import AgentLog
from config import Config
from dome import DomeClient, DomeError
from llm import LLM
from model import Snap
from rag import Rag
from rules import Action, Decision, Rules

_FILE = re.compile(r"[\w\-.]+\.(?:gcode|gco|nc|ngc|tap|txt)", re.IGNORECASE)
_PRINT_WORDS = ("напечата", "печат", "принтер", "корпус")
_CNC_WORDS = ("фрез", "плат", "чпу", "гравир")


class Brain:
    def __init__(self, cfg: Config, log: AgentLog, dome: DomeClient, llm: LLM, rag: Rag) -> None:
        self.cfg = cfg
        self.log = log
        self.dome = dome
        self.llm = llm
        self.rag = rag
        self.rules = Rules(self._load_state())
        self.snap = Snap({"nodes": {}, "game_time": {}})
        self.task: dict[str, str] = self.rules.state.get("task") or {}
        self._cooldown: dict[str, float] = {}
        self._last_obs = 0.0
        self._last_obs_key = ""
        self._busy = False
        self._stop = asyncio.Event()
        self._catalog: dict[str, set[str]] = {}
        self._last_save = 0.0
        self._inbox_id = int(self.rules.state.get("inbox_id") or 0)
        self._reconciled = False
        self._node_seen: dict[str, tuple] = {}
        self._last_nodata = 0.0
        self._task_ack: dict[str, float] = {}

    # ------------------------------------------------------------------ состояние
    def _load_state(self) -> dict:
        path = self.cfg.state_path
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}

    def save_state(self) -> None:
        self.rules.state["task"] = self.task
        self.rules.state["inbox_id"] = self._inbox_id
        path = self.cfg.state_path
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.rules.state, fh, ensure_ascii=False, default=str)
            os.replace(tmp, path)
        except OSError as exc:
            self.log.write("error", where="brain.save_state", error=str(exc))

    # ------------------------------------------------------------------ события
    async def on_telemetry(self, data: dict) -> None:
        now = time.time()
        nodes = data.get("nodes") or {}
        for nid, node in nodes.items():
            ts = (node or {}).get("updated_at")
            prev = self._node_seen.get(nid)
            if prev is None or prev[0] != ts:
                self._node_seen[nid] = (ts, now)
        ages = {nid: now - seen[1] for nid, seen in self._node_seen.items()}
        self.snap = Snap(data, now, ages=ages)
        game = data.get("game_time") or {}
        hour = game.get("hour")
        if isinstance(hour, (int, float)):
            self.log.set_dome_time(f"{int(hour) % 24:02d}:{int(round((hour % 1) * 60)) % 60:02d}")
        else:
            self.log.set_dome_time(None)

    async def on_message(self, message: dict) -> None:
        sender = message.get("sender") or {}
        text = (message.get("text") or "").strip()
        self._inbox_id = max(self._inbox_id, int(message.get("id") or 0))
        role = sender.get("role")
        self.log.write("message", direction="in", sender={"role": role, "team": sender.get("team"),
                                                          "name": sender.get("name")},
                       text=text[:4000], message_id=message.get("id"))
        if role != "operator" or not text:
            return
        recipient = message.get("to")
        if recipient:
            own = {self.cfg.operator_name, f"team{self.dome.team}", "operator", "all"}
            if str(recipient).lower() not in own:
                return  # сообщение адресовано другой команде
        if self.dome.role != "team":
            return  # вне своей смены задания не берём
        await self._handle_task(text)

    async def _handle_task(self, text: str) -> None:
        low = text.lower()
        files = _FILE.findall(text)
        chosen = None
        for name in files:
            if name.lower().endswith((".gcode", ".gco")) and any(w in low for w in _PRINT_WORDS):
                chosen = ("print_file", name)
                break
            if name.lower().endswith((".nc", ".ngc", ".tap")) and any(w in low for w in _CNC_WORDS):
                chosen = ("cnc_file", name)
                break
        if chosen is None and files:
            name = files[0]
            chosen = ("print_file", name) if name.lower().endswith((".gcode", ".gco")) else ("cnc_file", name)
        if chosen is None:
            # задание словами: ищем файлы в списке принтера
            if any(w in low for w in _PRINT_WORDS):
                files_resp = await self._printer_files()
                names = [f.get("name") if isinstance(f, dict) else str(f) for f in files_resp]
                if names:
                    chosen = ("print_file", names[0])
        if chosen is None:
            return
        key, name = chosen
        self.task[key] = name
        self.rules.state["started"].pop(key.split("_")[0], None)
        self.save_state()
        if time.time() - self._task_ack.get(name, 0) > 120:
            self._task_ack[name] = time.time()
            ref = self.log.write("decision", situation=f"Оператор поставил задачу: {text[:200]}",
                                 cause="сообщение Оператора", confidence=0.95,
                                 evidence=[{"node": "meeting_01", "param": "text", "value": text[:200]}],
                                 plan=[f"взять в работу {name}"], source="rules")
            try:
                await self.dome.say(f"Принято: {name}. Запущу, когда станок будет свободен.", ref=ref)
            except DomeError as exc:
                self.log.write("error", where="brain.ack", error=str(exc))

    async def _printer_files(self) -> list:
        try:
            resp = await self.dome.query("printer_3d_01", "list_files")
            result = resp.get("result") or []
            return [f if isinstance(f, dict) else {"name": str(f)} for f in result]
        except DomeError:
            return []

    # ------------------------------------------------------------------ цикл
    async def run(self) -> None:
        await asyncio.sleep(2)
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.log.write("error", where="brain.tick", error=f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(self.cfg.tick_s)

    async def stop(self) -> None:
        self._stop.set()
        self.save_state()

    async def _tick(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            await self._observe()
            if self.dome.role != "team":
                return
            if not self.dome.connected.is_set():
                return
            age = time.time() - self.dome.data_ts
            if age > self.cfg.data_stale_s:
                if time.time() - self._last_nodata > 30:
                    self._last_nodata = time.time()
                    self.log.write("observation",
                                   summary=f"нет свежей телеметрии {age:.0f} с — решения не принимаю",
                                   metrics={"telemetry_age_s": round(age, 1), "role": self.dome.role},
                                   anomalies=["потеря телеметрии"])
                return
            self._reconcile_once()
            self._close_finished_task()
            self._close_finished_cnc()
            decisions = self.rules.evaluate(self.snap, {
                "task": dict(self.task),
                "stale_after": self.cfg.stale_after_s,
            })
            for decision in decisions:
                if self._stop.is_set():
                    break
                if self._cooldown.get(decision.key, 0) > time.time():
                    continue
                self._cooldown[decision.key] = time.time() + max(decision.cooldown, 15)
                await self._apply(decision)
        finally:
            self._busy = False
            if time.time() - self._last_save > 20:
                self._last_save = time.time()
                self.save_state()

    def _reconcile_once(self) -> None:
        """Один раз после запуска сверяет сохранённую задачу с состоянием станков."""
        if self._reconciled or not self.snap.nodes:
            return
        self._reconciled = True
        changed = False
        pr = self.snap.node("printer_3d_01")
        if self.task.get("print_file") and self.rules.state["started"].get("print") == self.task["print_file"]:
            if pr.get("state") in ("IDLE", "COMPLETED") and not pr.get("file_name"):
                self.task.pop("print_file", None)
                changed = True
        cnc = self.snap.node("cnc_01")
        if self.task.get("cnc_file") and self.rules.state["started"].get("cnc") == self.task["cnc_file"]:
            if cnc.get("state") == "IDLE" and not cnc.get("file_name"):
                self.task.pop("cnc_file", None)
                changed = True
        if changed:
            self.save_state()
            self.log.write("observation", summary="задача сверена с состоянием станков после запуска",
                           metrics={"task": dict(self.task)})

    def _close_finished_task(self) -> None:
        """Задание считается выполненным, когда печать дошла до COMPLETED."""
        if not self.task.get("print_file"):
            return
        pr = self.snap.node("printer_3d_01")
        if pr.get("state") == "COMPLETED":
            self.task.pop("print_file", None)
            self.save_state()
            self.log.write("observation", summary="задание на печать выполнено, задача снята",
                           metrics={"printer": "COMPLETED"})

    def _close_finished_cnc(self) -> None:
        if not self.task.get("cnc_file") or not self.rules.state["started"].get("cnc"):
            return
        cnc = self.snap.node("cnc_01")
        if cnc.get("state") == "IDLE" and not cnc.get("file_name"):
            self.task.pop("cnc_file", None)
            self.rules.state["started"].pop("cnc", None)
            self.save_state()
            self.log.write("observation", summary="задание на фрезеровку выполнено, задача снята",
                           metrics={"cnc": "IDLE"})

    # ------------------------------------------------------------------ наблюдаемость
    async def _observe(self) -> None:
        now = time.time()
        key = self._anomaly_key()
        if now - self._last_obs < self.cfg.observation_every_s and key == self._last_obs_key:
            return
        self._last_obs = now
        self._last_obs_key = key
        s = self.snap
        print_state = s.machine("printer_3d_01")
        cnc_state = s.machine("cnc_01")
        co2 = s.co2_values()
        inter = {name: data.get("state") for name, data in (s.interlocks() or {}).items()
                 if isinstance(data, dict) and data.get("state") not in (None, "ARMED")}
        metrics = {
            "soc_pct": s.soc(),
            "autonomy_h": s.autonomy_h(),
            "co2_primary": co2.get(s.primary_co2_sensor()),
            "co2_02": co2.get("climate_sensor_02"),
            "co2_03": co2.get("climate_sensor_03"),
            "printer": print_state.get("state"),
            "printer_pct": print_state.get("progress"),
            "printer_hold": print_state.get("reason"),
            "cnc": cnc_state.get("state"),
            "cnc_hold": cnc_state.get("reason"),
            "diesel": s.get("dizel_1", "state"),
            "lines_off": [i + 1 for i, ln in enumerate(s.lines()) if ln.get("state") != "ON"],
            "interlocks": inter,
            "role": self.dome.role,
        }
        anomalies = []
        if co2.get(s.primary_co2_sensor()) and co2.get(s.primary_co2_sensor()) > 1200:
            anomalies.append("CO2 основного датчика выше 1200 ppm")
        if print_state.get("hold"):
            anomalies.append(f"принтер удержан: {print_state.get('reason')}")
        if cnc_state.get("hold"):
            anomalies.append(f"фрезер удержан: {cnc_state.get('reason')}")
        if inter:
            anomalies.append("интерлоки: " + ", ".join(inter))
        if (s.get("printer_3d_01", "webhook_state") == "COMPLETED"
                and s.get("printer_3d_01", "state") == "PRINTING"):
            anomalies.append("webhook_state врёт: печать ещё идёт")
        stale = [n for n in ("printer_3d_01", "cnc_01", "smart_panel_01") if s.stale(n, self.cfg.stale_after_s)]
        if stale:
            anomalies.append("нет свежих данных: " + ", ".join(stale))
        self.log.write("observation", summary=self._summary(metrics), metrics=metrics, anomalies=anomalies)

    def _anomaly_key(self) -> str:
        s = self.snap
        return "|".join(str(s.get(n, "hold_reason")) for n in ("printer_3d_01", "cnc_01"))

    @staticmethod
    def _summary(m: dict) -> str:
        return (f"роль {m.get('role')}; SOC {m.get('soc_pct')}; CO2 {m.get('co2_primary')}; "
                f"принтер {m.get('printer')} {m.get('printer_pct')}; фрезер {m.get('cnc')}; "
                f"дизель {m.get('diesel')}; линии выкл {m.get('lines_off')}")

    # ------------------------------------------------------------------ исполнение
    async def _apply(self, d: Decision) -> None:
        llm_used = None
        if d.severity >= 4 or (d.confidence < 0.6 and d.severity >= 2):
            llm_used = await self._consult(d)
        source = "mixed" if llm_used and d.source == "rules" else d.source
        if llm_used:
            d.cause = llm_used.get("cause") or d.cause
            d.plan = list(d.plan) + [f"LLM: {p}" for p in (llm_used.get("plan") or [])][:3]
            d.confidence = max(d.confidence, float(llm_used.get("confidence") or 0))
            # сообщение модели используем только там, где правило не справилось само
            if not d.message and llm_used.get("operator_message") and (d.severity >= 4 or not d.actions):
                d.message = str(llm_used["operator_message"])[:400]
        decision_seq = self.log.write(
            "decision",
            situation=d.situation,
            cause=d.cause,
            confidence=round(d.confidence, 2),
            evidence=d.evidence,
            plan=d.plan,
            source=source,
            rag_refs=[h["id"] for h in (llm_used or {}).get("_rag", [])] or None,
        )
        if d.message and self.cfg.dry_run:
            pass
        for action in d.actions:
            if self.cfg.dry_run:
                self.log.write("action", request={"type": "control", "node_id": action.node,
                                                  "action": action.action, "value": action.value},
                               response={"success": None, "error": "dry_run"}, ref=decision_seq)
                continue
            ok = await self._execute(action, decision_seq)
            if not ok and action.required:
                self.log.write("observation", summary=f"действие не подтвердилось: {action.expect or action.action}",
                               metrics={"node": action.node, "action": action.action},
                               anomalies=["цепочка действий остановлена"])
                if action.node == "cnc_01" and not self.cfg.dry_run:
                    try:
                        await self.dome.say(
                            f"Фрезер: не удалось выполнить «{action.action}». Проверьте заготовку и пробник на месте.",
                            ref=decision_seq)
                    except DomeError as exc:
                        self.log.write("error", where="brain.cnc_help", error=str(exc))
                break
        if d.message and d.message_if is not None and not d.message_if(self.snap):
            d.message = None
        if d.message:
            if self.cfg.dry_run:
                return
            try:
                await self.dome.say(d.message, to=d.message_to, ref=decision_seq)
            except DomeError as exc:
                self.log.write("error", where="brain.say", error=str(exc))

    def _allowed(self, node: str, action: str) -> bool:
        if not self._catalog:
            return True
        allowed = self._catalog.get(node)
        if allowed is None:
            return False
        return action.lower() in allowed

    async def refresh_catalog(self) -> None:
        try:
            resp = await self.dome.describe()
        except DomeError as exc:
            self.log.write("error", where="brain.describe", error=str(exc))
            return
        catalog: dict[str, set[str]] = {}
        for node in resp.get("nodes") or []:
            name = node.get("node_id")
            actions = {str(a).lower() for a in (node.get("controls") or [])}
            for alias, names in (node.get("control_aliases") or {}).items():
                actions.add(str(alias).lower())
                for extra in names or []:
                    actions.add(str(extra).lower())
            actions |= {str(a).lower() for a in (node.get("service_actions") or [])}
            actions |= {str(a).lower() for a in (node.get("queries") or [])}
            catalog[name] = actions
            catalog[name].add("post_message")
        self._catalog = catalog
        self.log.write("observation", summary=f"каталог прав получен: {len(catalog)} узлов",
                       metrics={"nodes": len(catalog)})

    async def on_role(self, role: str, team: int | None) -> None:
        if role == "team":
            asyncio.create_task(self.refresh_catalog())

    async def _execute(self, action: Action, decision_seq: int) -> bool:
        if not self._allowed(action.node, action.action):
            self.log.write("error", where="brain.policy",
                           error=f"действие {action.action} для {action.node} не разрешено каталогом")
            return False
        try:
            resp = await self.dome.control(action.node, action.action, action.value, ref=decision_seq)
        except DomeError as exc:
            self.log.write("error", where="brain.execute", error=str(exc),
                           request={"node": action.node, "action": action.action})
            return False
        ok = bool(resp.get("success"))
        if not ok:
            return False
        if action.check is None:
            return True
        deadline = time.time() + action.timeout
        observed = None
        while time.time() < deadline:
            await asyncio.sleep(1.5)
            try:
                if action.check(self.snap):
                    observed = self._observed(action)
                    self.log.write("verification", ref=self.dome.last_action_seq,
                                   expected=action.expect, observed=observed, ok=True)
                    return True
            except Exception:  # noqa: BLE001
                continue
        observed = self._observed(action)
        self.log.write("verification", ref=self.dome.last_action_seq,
                       expected=action.expect, observed=observed, ok=False)
        return False

    def _observed(self, action: Action) -> Any:
        if action.node in ("printer_3d_01", "cnc_01"):
            return {k: self.snap.get(action.node, k) for k in
                    ("state", "hold_level", "hold_reason", "progress_pct", "bed_clear")}
        if action.node == "smart_panel_01":
            value = action.value if isinstance(action.value, int) else None
            if value:
                return {k: self.snap.line(value).get(k) for k in ("state", "current_a", "power_w")}
        if action.node == "thermal_insulation_01":
            return {"locks": self.snap.raw("thermal_insulation_01", "locks"),
                    "doors": self.snap.raw("thermal_insulation_01", "doors")}
        if action.node == "dome_automation_01":
            return {"interlocks": {k: v.get("state") for k, v in (self.snap.interlocks() or {}).items()
                                   if isinstance(v, dict)}}
        return {k: self.snap.get(action.node, k) for k in
                ("state", "enabled", "speed_pct", "active_channel", "power_w")}

    # ------------------------------------------------------------------ LLM
    async def _consult(self, d: Decision) -> dict | None:
        if not self.llm.available():
            return None
        state_text = self._state_text()
        hits = self.rag.search(f"{d.situation} {d.cause}", k=3) if self.rag.ready else []
        result = await self.llm.ask(purpose=d.key[:60], state_text=state_text, rag_hits=hits)
        if result:
            result["_rag"] = hits
        return result

    def _state_text(self) -> str:
        s = self.snap
        lines = []
        for i in range(1, 9):
            ln = s.line(i)
            if ln:
                lines.append(f"L{i} {ln.get('name')}: {ln.get('state')} {ln.get('power_w')} Вт")
        return "\n".join([
            f"Время купола: {s.dome_clock()}",
            f"Энергия: солнце {s.get('solar_panels_01', 'power_w')} Вт, АКБ SOC {s.soc()} %, "
            f"дизель {s.get('dizel_1', 'state')}, дефицит {s.get('solar_inverter_01', 'power_deficit_w')} Вт",
            f"CO2: 01={s.get('climate_sensor_01', 'co2_ppm')}, 02={s.get('climate_sensor_02', 'co2_ppm')}, "
            f"03={s.get('climate_sensor_03', 'co2_ppm')} (основной {s.primary_co2_sensor()})",
            f"Принтер: {s.get('printer_3d_01', 'state')} {s.get('printer_3d_01', 'progress_pct')} % "
            f"hold={s.get('printer_3d_01', 'hold_level')}/{s.get('printer_3d_01', 'hold_reason')} "
            f"bed_clear={s.get('printer_3d_01', 'bed_clear')}",
            f"Фрезер: {s.get('cnc_01', 'state')} {s.get('cnc_01', 'progress_pct')} % "
            f"hold={s.get('cnc_01', 'hold_level')}/{s.get('cnc_01', 'hold_reason')}",
            "Интерлоки: " + ", ".join(f"{k}={v.get('state')}" for k, v in (s.interlocks() or {}).items()
                                      if isinstance(v, dict)),
            "Линии: " + "; ".join(lines),
        ])
