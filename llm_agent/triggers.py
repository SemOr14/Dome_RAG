"""Поводы вызвать модель (rag/AGENT_TRIGGERS.md): пороги, смены состояния и чат — обычным кодом.

Детекторы ничего не вызывают сами: они превращают телеметрию и события БД в Trigger, а диспетчер
(dispatcher.py) объединяет их, ограничивает частоту и решает, когда звать DeepSeek.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from .context.telemetry import compact_telemetry
from .monitoring import NumericMonitor

# Порядок важен: при объединении поводов первыми идут более срочные
PRIORITY = {"operator": 0, "alarm": 0, "command": 0, "conflict": 1, "stale": 1, "operation": 1,
            "verification": 1, "numeric": 2, "review": 2}
JOB_NODES = {"printer_3d_01": ("PRINTING",), "cnc_01": ("RUNNING",)}
DONE_STATES = ("COMPLETED", "IDLE")


@dataclass
class Trigger:
    kind: str  # alarm | conflict | stale | operation | command | operator | verification | numeric | review
    key: str  # ключ ситуации: повторы с тем же ключом объединяются и не вызывают модель чаще паузы
    text: str
    nodes: set[str] = field(default_factory=set)
    event_ids: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def priority(self) -> int:
        return PRIORITY.get(self.kind, 1)

    def public(self) -> dict:
        return {"kind": self.kind, "key": self.key, "text": self.text, "nodes": sorted(self.nodes),
                "event_ids": self.event_ids, **({"meta": self.meta} if self.meta else {})}


def telemetry_age(snapshot: dict, stale_s: float) -> float | None:
    """Возраст последнего кадра, если он старше stale_s; None — данные свежие или возраст неизвестен."""
    age = snapshot.get("telemetry_age_s")
    return float(age) if isinstance(age, (int, float)) and not isinstance(age, bool) and age > stale_s else None


def _anomaly_kind(a: dict) -> str:
    if a.get("kind"):
        return a["kind"]
    note = a["note"]
    if "расходятся" in note:
        return "conflict"
    if "нет связи" in note or "не обновлялись" in note or "пропало" in note:
        return "stale"
    return "alarm"


def _anomaly_key(a: dict) -> str:
    # Значение входит в ключ только у состояний: TRIPPED → BLOCKED — новая ситуация, 3001 → 3005 ppm — нет
    value = a.get("value")
    state = value if isinstance(value, (str, bool)) else ""
    if a.get("kind"):
        return f"diagnostic|{a['node']}|{a.get('param') or ''}|{state}"
    return f"{a['node']}|{a.get('param') or ''}|{a['note']}|{state}"


def _num(value: Any) -> float | None:
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


REPEAT_QUIET_S = 1800.0  # повтор того же несрочного повода раньше этого срока не отдаётся модели
QUIET_KINDS = ('numeric', 'conflict')


class TelemetryWatch:
    """Смены телеметрии между кадрами: новые аномалии, противоречия, застывшие операции и кадры."""

    def __init__(self, stale_s: float, stall_s: float):
        self.stale_s = stale_s
        self.stall_s = stall_s
        self.numeric = NumericMonitor()
        self.numeric_active: set[str] = set()
        self.active: set[str] = set()  # ключи аномалий, уже отданных в работу; исчезнувшие снимаются
        self.frame: tuple | None = None
        self.frame_since: float | None = None
        self.frame_stale = False
        self.progress: dict[str, tuple[float, float]] = {}  # node -> (progress_pct, с какого момента)
        self.stalled: set[str] = set()
        self.done_progress: dict[str, tuple[str, str | None, float]] = {}
        self.target_wait: dict[tuple[str, str], dict] = {}
        self.target_active: set[tuple[str, str]] = set()
        self.last_emitted: dict[str, float] = {}

    @staticmethod
    def frame_mark(snapshot: dict) -> tuple:
        return snapshot.get("timestamp"), snapshot.get("observed_at")

    def observe(self, snapshot: dict, catalog: dict[str, dict], now: float) -> list[Trigger]:
        out: list[Trigger] = []
        nodes = snapshot.get("nodes") or {}
        out += self._frames(snapshot, now)
        if not nodes:
            return out

        current = {}
        for a in compact_telemetry(snapshot, catalog).anomalies:
            current.setdefault(_anomaly_key(a), a)
        for key, a in current.items():
            if key in self.active:
                continue
            value = a.get("value")
            where = f"{a['node']}.{a['param']}" if a.get("param") else a["node"]
            nodes_hit = {a["node"]} | set(a.get("affected_nodes") or ()) | ({str(value)} if isinstance(value, str) and value.startswith("IL_") else set())
            out.append(Trigger(_anomaly_kind(a), key, f"{where} = {value!r}: {a['note']}", nodes_hit,
                               meta={"evidence": {"node": a['node'], "field": a.get('param'), "kind": _anomaly_kind(a)},
                                     **({"affected_nodes": a['affected_nodes']} if a.get('affected_nodes') else {})}))
        self.active = set(current)
        numeric = self.numeric.observe(nodes, self.frame_mark(snapshot))
        numeric_keys = {f"numeric|{a['node']}|{re.sub(r'\[\d+\]', '', a['field'])}" for a in numeric}
        flagged = set()
        for a in numeric:
            key = f"numeric|{a['node']}|{re.sub(r'\[\d+\]', '', a['field'])}"
            if key not in self.numeric_active and key not in flagged and not any(x['node'] == a['node'] and x.get('param') == a['field'] for x in current.values()):
                # Статистика и рабочие пределы — гипотеза, а не авария: идёт после срочных поводов, как обзор
                kind = 'conflict' if a['kind'] in ('stuck', 'conflict') else 'numeric'
                text = f"{a['node']}.{a['field']}={a['value']}: {a['note']} (гипотеза для проверки)"
                flagged.add(key)  # элементы одного массива (fans_rpm[0..5]) — один повод
                out.append(Trigger(kind, key, text, {a['node']}, meta={'evidence': {'node':a['node'], 'field':a['field'], 'kind':a['kind']}}))
        self.numeric_active = numeric_keys

        for node, data in nodes.items():
            if not isinstance(data, dict) or data.get('online') is False or data.get('status') == 'UNKNOWN':
                self.progress.pop(node, None)
                self.done_progress.pop(node, None)
                for key in [k for k in self.target_wait if k[0] == node]:
                    self.target_wait.pop(key, None)
                    self.target_active.discard(key)
                continue
            if isinstance(data, dict) and ('progress_pct' in data or node in self.progress or node in self.done_progress):
                running = JOB_NODES.get(node, ('PRINTING', 'RUNNING', 'PROCESSING', 'MOVING'))
                out += self._job(node, data, running, now)
            out += self._targets(node, data, self.frame_mark(snapshot), now)
        return self._quiet(out, now)

    def _quiet(self, triggers: list[Trigger], now: float) -> list[Trigger]:
        """Несрочный повод (числовая гипотеза, расхождение) с тем же ключом не повторяется чаще REPEAT_QUIET_S.

        Аварии, операции, устаревшие данные и всё остальное проходят без задержки.
        """
        out = []
        for t in triggers:
            if t.kind in QUIET_KINDS:
                last = self.last_emitted.get(t.key)
                if last is not None and 0 <= now - last < REPEAT_QUIET_S:
                    continue
                self.last_emitted[t.key] = now
            out.append(t)
        return out

    def _targets(self, node: str, data: dict, frame, now: float) -> list[Trigger]:
        """No response to an active setpoint: unit-independent, with a startup grace period."""
        out = []
        live = set()
        running = data.get('state') in ('RUNNING', 'PRINTING', 'PROCESSING', 'MOVING', 'ACTIVE', 'ON')
        for field, raw in data.items():
            match = re.fullmatch(r'(.+)_target_(.+)', field)
            target = _num(raw)
            if not match or target is None or target == 0 or not running:
                continue
            prefix, unit = match.groups()
            actual_field = next((f for f in (f'{prefix}_{unit}', f'{prefix}_actual_{unit}', f'{prefix}_temp_{unit}')
                                 if _num(data.get(f)) is not None), None)
            if actual_field is None:
                continue
            actual = _num(data[actual_field])
            tolerance = max(abs(target) * .2, .02)
            if unit == 'c':
                tolerance = max(10, abs(target) * .15)
            error = abs(target - actual)
            key = (node, actual_field)
            if error <= tolerance:
                continue
            live.add(key)
            mark = data.get('updated_at', frame)
            regime = (target, data.get('state'), data.get('mode'), data.get('file_name'))
            before = self.target_wait.get(key)
            if (before is None or before['regime'] != regime
                    or (mark != before['mark'] and now - before['last_seen'] > max(self.stale_s * 2, 120))):
                self.target_wait[key] = dict(regime=regime, since=now, mark=mark, count=1,
                                            best=error, last_seen=now)
                self.target_active.discard(key)
                continue
            fresh_update = mark != before['mark']
            if fresh_update:
                before['count'] += 1
                before['mark'] = mark
                before['last_seen'] = now
                if error < before['best'] - tolerance * .25:
                    # A ramp that is converging to its target gets more settling time.
                    before.update(best=error, since=now, count=1)
                    self.target_active.discard(key)
            if fresh_update and now - before['since'] >= self.stall_s and before['count'] >= 4 and key not in self.target_active:
                self.target_active.add(key)
                out.append(Trigger('conflict', f'tracking|{node}|{actual_field}',
                    f'{node}: задано {field}={target:g}, но {actual_field}={actual:g} при {data.get("state")} '
                    f'устойчиво расходится с уставкой {now - before["since"]:.0f} с по {before["count"]} новым измерениям; '
                    'проверить режим, привод, датчик и эффект команды', {node}))
        for key in [k for k in self.target_wait if k[0] == node and k not in live]:
            self.target_wait.pop(key, None)
            self.target_active.discard(key)
        return out

    def _frames(self, snapshot: dict, now: float) -> list[Trigger]:
        """Отсутствие новых кадров — отдельный повод, а не «всё спокойно»."""
        mark = self.frame_mark(snapshot)
        if mark != self.frame:
            self.frame, self.frame_since, self.frame_stale = mark, now, False
            return []
        if self.frame_stale or self.frame_since is None or now - self.frame_since < self.stale_s:
            return []
        self.frame_stale = True
        return [Trigger("stale", "telemetry|no_frames",
                        f"Новых кадров телеметрии нет {now - self.frame_since:.0f} с: состояние купола устарело, "
                        "результаты действий проверить нельзя")]

    def _job(self, node: str, data: dict, running: tuple, now: float) -> list[Trigger]:
        out = []
        state, progress = data.get("state"), _num(data.get("progress_pct"))
        # Завершение при продолжающемся росте прогресса — противоречие, а не готовое изделие
        if state in DONE_STATES and progress is not None:
            before = self.done_progress.get(node)
            if before is not None and before[:2] == (state, data.get('file_name')) and progress > before[2]:
                out.append(Trigger("conflict", f"{node}|done_but_progress",
                                   f"{node}: состояние {state}, но progress_pct растёт ({before[2]} → {progress}). "
                                   "Не считать изделие готовым, проверить фактическое состояние устройства", {node}))
            self.done_progress[node] = (state, data.get('file_name'), progress)
        else:
            self.done_progress.pop(node, None)

        if state not in running or progress is None:
            self.progress.pop(node, None)
            self.stalled.discard(node)
            return out
        last = self.progress.get(node)
        if last is None or progress != last[0]:
            self.progress[node] = (progress, now)
            self.stalled.discard(node)
        elif node not in self.stalled and now - last[1] >= self.stall_s:
            self.stalled.add(node)
            out.append(Trigger("operation", f"{node}|stall|{data.get('file_name')}",
                               f"{node}: {state} {data.get('file_name') or ''}, progress_pct={progress} "
                               f"не меняется {now - last[1]:.0f} с — операция превысила ожидаемое время", {node}))
        return out


class OperatorWatch:
    """Новые сообщения Оператора в meeting_01: из телеметрии (recent_messages) и событий meeting_message."""

    def __init__(self, last_id: int = 0):
        self.last_id = last_id

    def messages(self, messages: list[dict], event_ids: dict[int, str] | None = None, *, handled_ids=None) -> list[Trigger]:
        out = []
        for msg in sorted((m for m in messages if isinstance(m, dict) and isinstance(m.get("id"), int)),
                          key=lambda m: m["id"]):
            if msg["id"] <= self.last_id:
                continue
            self.last_id = msg["id"]
            if msg["id"] in (handled_ids or set()):
                continue
            sender = msg.get("sender") if isinstance(msg.get("sender"), dict) else {}
            if sender.get("role") != "operator":
                continue  # свои и чужие агенты — не задача для нас
            ev = (event_ids or {}).get(msg["id"])
            out.append(Trigger("operator", f"operator|{msg['id']}",
                               f"Оператор в чате (сообщение {msg['id']}): «{msg.get('text', '')}»",
                               {"meeting_01"}, [ev] if ev else []))
        return out

    def from_snapshot(self, snapshot: dict, *, handled_ids=None) -> list[Trigger]:
        meeting = (snapshot.get("nodes") or {}).get("meeting_01") or {}
        return self.messages(meeting.get("recent_messages") or [], handled_ids=handled_ids)


def command_triggers(events: list[dict]) -> list[Trigger]:
    """События БД об исполнении команд: отказ Купола, таймаут, неподтверждённый эффект."""
    out = []
    for ev in events:
        p = ev["payload"] if isinstance(ev.get("payload"), dict) else {}
        kind, rid = ev["event_type"], ev.get("request_id")
        req = ev.get("request") or {}
        what = f"{req.get('node_id') or p.get('node_id') or ev.get('node_id') or '?'}." \
               f"{req.get('action') or p.get('action') or p.get('type') or '?'}"
        node = req.get("node_id") or p.get("node_id") or ev.get("node_id")
        if kind == "control_response" and p.get("success") is False:
            text = f"Купол отклонил команду {what} (request_id {rid}): {p.get('error') or 'без пояснения'}"
        elif kind == "request_timeout":
            text = f"Нет ответа Купола на {what} (request_id {rid}): результат неизвестен"
        elif kind == "agent_verification" and p.get("ok") is False:
            text = f"Агентная система не подтвердила эффект: ожидалось {p.get('expected')}, " \
                   f"наблюдается {p.get('observed')}"
            node = node or p.get("node_id")
        else:
            continue
        out.append(Trigger("command", f"command|{kind}|{rid or ev['event_id']}", text,
                           {node} if node else set(), [ev["event_id"]]))
    return out


NO_ANSWER = ("нет ответа", "нет соединения", "соединение закрыто")


def gate_action_trigger(rec: dict) -> Trigger | None:
    """Запись action журнала агента: отказ Купола или отсутствие ответа — срочный повод.

    Отказы Gate (rejected) и неотправленные команды (dry_run) Купол не видел — не повод.
    """
    resp = rec.get("response") if isinstance(rec.get("response"), dict) else {}
    if rec.get("rejected") or rec.get("dry_run") or resp.get("success") is not False:
        return None
    req = rec.get("request") if isinstance(rec.get("request"), dict) else {}
    node, action = req.get("node_id"), req.get("action")
    what = _what(req)
    error = str(resp.get("error") or "без пояснения")
    if any(x in error for x in NO_ANSWER):
        text = (f"Купол не подтвердил команду {what}: {error}. Результат неизвестен, эффект проверяется по телеметрии. "
                "Не повторяй команду, пока не ясен итог; оцени ситуацию и сообщи Оператору")
        kind = "timeout"
    else:
        text = (f"Купол отклонил команду {what}: {error}. Повтор вслепую запрещён: оцени причину, предложи другое "
                "действие или попроси Оператора")
        kind = "refused"
    return Trigger("command", f"command|{kind}|{rec.get('run_id')}|{rec.get('seq')}", text, {node} if node else set(),
                   meta={"action_seq": rec.get("seq"), "decision_seq": rec.get("ref"), "error": error})


def _what(req: dict) -> str:
    value = req.get("value")
    if isinstance(value, dict) and "content" in value:
        value = {k: v for k, v in value.items() if k != "content"}
    label = json.dumps(value, ensure_ascii=False, default=str)[:240] if value is not None else ""
    return f"{req.get('node_id')}.{req.get('action')}" + (f"({label})" if label else "")


def gate_verification_trigger(rec: dict, action: dict | None, timed_out: bool, cooldown_s: float) -> Trigger | None:
    """Запись verification журнала агента (проверка эффекта Gate по телеметрии) — повод для модели.

    ok=false (no_effect): Купол принял команду, а эффекта нет — срочно. ok=true — только как итог команды,
    на которую Купол не ответил (модели сказали «результат неизвестен»).
    """
    req = (action or {}).get("request") if isinstance((action or {}).get("request"), dict) else {}
    what = _what(req) if req else f"действие seq {rec.get('ref')}"
    node = req.get("node_id")
    key = f"{rec.get('run_id')}|{rec.get('ref')}"
    if rec.get("no_data"):
        return Trigger(
            "stale", f"command|no_data|{key}",
            f"Команда {what} отправлена, но новых кадров телеметрии за {rec.get('waited_s', '?')} с не было: результат "
            "не проверен. Не считай её выполненной и не повторяй вслепую", {node} if node else set(),
            meta={"action_seq": rec.get("ref")},
        )  # fmt: skip
    if rec.get("ok") is False:
        return Trigger(
            "command", f"command|no_effect|{key}",
            f"Команда {what} отправлена, но эффект за {rec.get('waited_s', '?')} с не подтвердился: ожидалось "
            f"{rec.get('expected')}, наблюдается {rec.get('observed')}. Такая же команда заблокирована Gate на "
            f"{cooldown_s:.0f} с — не повторяй её: найди причину по телеметрии, предложи другое действие или попроси "
            "Оператора", {node} if node else set(), meta={"action_seq": rec.get("ref")},
        )  # fmt: skip
    if rec.get("ok") is True and timed_out:
        return Trigger(
            "verification", f"command|resolved|{key}",
            f"Итог команды {what}, на которую Купол не ответил вовремя: эффект подтверждён ({rec.get('observed')}). "
            "Повторять её не нужно", {node} if node else set(), meta={"action_seq": rec.get("ref")},
        )  # fmt: skip
    return None


def gate_rejection_trigger(rec: dict) -> Trigger | None:
    """One review of a deterministic proposal rejected locally; not permission to bypass Gate."""
    if not rec.get("rejected") or rec.get("dry_run"):
        return None
    req = rec.get("request") or {}
    error = str((rec.get("response") or {}).get("error") or "отказ Gate")
    if "ожидает проверки" in error:
        return None  # плейбук повторяет шаг, пока ждёт эффекта: штатная дедупликация, не повод для модели
    node = req.get("node_id")
    return Trigger("command", "gate_rejected|" + _what(req) + "|" + error,
        f"Алгоритм предложил {_what(req)}, Gate отклонил: {error}. Не обходи запрет и не повторяй эту команду. "
        "Разбери причину; предложи допустимый иной шаг или структурированный request_human.",
        {node} if node else set(), meta={"action_seq": rec.get("seq"), "error": error, "local_rejection": True})


def playbook_failure_trigger(name: str, error: str) -> Trigger:
    return Trigger("command", f"playbook_failure|{name}|{error}",
        f"Детерминированный алгоритм {name} завершился ошибкой {error}. Его действие не выполнено. "
        "Разбери ситуацию по свежим данным; не повторяй ошибочный сценарий. Если нет допустимого решения — request_human.",
        meta={"playbook": name, "error": error})
