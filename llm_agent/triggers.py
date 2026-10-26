"""Поводы вызвать модель (rag/AGENT_TRIGGERS.md): пороги, смены состояния и чат — обычным кодом.

Детекторы ничего не вызывают сами: они превращают телеметрию и события БД в Trigger, а диспетчер
(dispatcher.py) объединяет их, ограничивает частоту и решает, когда звать DeepSeek.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .context.telemetry import compact_telemetry

# Порядок важен: при объединении поводов первыми идут более срочные
PRIORITY = {"operator": 0, "alarm": 0, "command": 0, "conflict": 1, "stale": 1, "operation": 1,
            "verification": 1, "review": 2}
JOB_NODES = {"printer_3d_01": ("PRINTING",), "cnc_01": ("RUNNING",)}
DONE_STATES = ("COMPLETED", "IDLE")


@dataclass
class Trigger:
    kind: str  # alarm | conflict | stale | operation | command | operator | verification | review
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


def _anomaly_kind(a: dict) -> str:
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
    return f"{a['node']}|{a.get('param') or ''}|{a['note']}|{state}"


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class TelemetryWatch:
    """Смены телеметрии между кадрами: новые аномалии, противоречия, застывшие операции и кадры."""

    def __init__(self, stale_s: float, stall_s: float):
        self.stale_s = stale_s
        self.stall_s = stall_s
        self.active: set[str] = set()  # ключи аномалий, уже отданных в работу; исчезнувшие снимаются
        self.frame: tuple | None = None
        self.frame_since: float | None = None
        self.frame_stale = False
        self.progress: dict[str, tuple[float, float]] = {}  # node -> (progress_pct, с какого момента)
        self.stalled: set[str] = set()
        self.done_progress: dict[str, float] = {}

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
        for key in current.keys() - self.active:
            a = current[key]
            value = a.get("value")
            where = f"{a['node']}.{a['param']}" if a.get("param") else a["node"]
            nodes_hit = {a["node"]} | ({str(value)} if isinstance(value, str) and value.startswith("IL_") else set())
            out.append(Trigger(_anomaly_kind(a), key, f"{where} = {value!r}: {a['note']}", nodes_hit))
        self.active = set(current)

        for node, running in JOB_NODES.items():
            data = nodes.get(node) or {}
            out += self._job(node, data, running, now)
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
            if before is not None and progress > before:
                out.append(Trigger("conflict", f"{node}|done_but_progress",
                                   f"{node}: состояние {state}, но progress_pct растёт ({before} → {progress}). "
                                   "Не считать изделие готовым, проверить данные принтера", {node}))
            self.done_progress[node] = progress
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

    def messages(self, messages: list[dict], event_ids: dict[int, str] | None = None) -> list[Trigger]:
        out = []
        for msg in sorted((m for m in messages if isinstance(m, dict) and isinstance(m.get("id"), int)),
                          key=lambda m: m["id"]):
            if msg["id"] <= self.last_id:
                continue
            self.last_id = msg["id"]
            sender = msg.get("sender") if isinstance(msg.get("sender"), dict) else {}
            if sender.get("role") != "operator":
                continue  # свои и чужие агенты — не задача для нас
            ev = (event_ids or {}).get(msg["id"])
            out.append(Trigger("operator", f"operator|{msg['id']}",
                               f"Оператор в чате (сообщение {msg['id']}): «{msg.get('text', '')}»",
                               {"meeting_01"}, [ev] if ev else []))
        return out

    def from_snapshot(self, snapshot: dict) -> list[Trigger]:
        meeting = (snapshot.get("nodes") or {}).get("meeting_01") or {}
        return self.messages(meeting.get("recent_messages") or [])


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
