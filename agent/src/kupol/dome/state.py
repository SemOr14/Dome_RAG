"""Состояние купола: последние значения узлов и короткая история числовых полей.

Любое поле может быть ``None``, узел может пропасть: доступ ``get`` ничего не бросает.
Время для кулдаунов и таймаутов — реальное (физика купола идёт в реальном времени, сутки окружения
ускорены), «возраст узла» считается по ``updated_at`` относительно самого свежего узла.
"""

from __future__ import annotations

import re
import math
from collections import deque
from collections.abc import Iterator
from typing import Any

from kupol.dome.forecast import Forecaster

SKIP_KEYS = {"recent_messages", "passage_events", "events", "files", "services", "last_probe"}
MAX_DEPTH = 4
_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def split_path(path: str) -> list[str | int]:
    """Разобрать путь ``node.field[2].sub`` в список ключей и индексов."""
    out: list[str | int] = []
    for name, index in _PATH_TOKEN.findall(path):
        out.append(int(index) if index else name)
    return out


def flatten_numbers(value: Any, prefix: str = "", depth: int = 0) -> Iterator[tuple[str, float]]:
    """Все числовые листья (кроме bool) с путями ``a.b[2].c``; служебные ключи пропускаются."""
    if depth > MAX_DEPTH:
        return
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)) and math.isfinite(value):
        yield prefix, float(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in SKIP_KEYS:
                continue
            yield from flatten_numbers(item, f"{prefix}.{key}" if prefix else str(key), depth + 1)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from flatten_numbers(item, f"{prefix}[{i}]", depth + 1)


class DomeState:
    """Последний снимок телеметрии и ряды чисел."""

    def __init__(self, history_len: int = 900, stale_age_s: float = 30.0) -> None:
        """Создать пустое состояние.

        Args:
            history_len: Максимум точек на ряд (при 2 с — около 30 минут).
            stale_age_s: После скольких секунд отставания ``updated_at`` узел считается замёрзшим.
        """
        self.nodes: dict[str, dict[str, Any]] = {}
        self.game_time: dict[str, Any] = {}
        self.role: str = ""
        self.wall: float = 0.0
        self.updates: int = 0
        self._history_len = history_len
        self._stale_age_s = stale_age_s
        self._series: dict[str, deque[tuple[float, float]]] = {}
        self._progress_wall: dict[str, float] = {}
        self.forecaster = Forecaster()  # прогноз времени до порогов (заряд АКБ, топливо, климат)

    # ------------------------------------------------------------------ запись

    def update(self, data: dict[str, Any], role: str, wall: float) -> None:
        """Принять ``data`` сообщения ``telemetry``.

        Args:
            data: Поле ``data`` телеметрии (``nodes``, ``game_time``).
            role: Роль соединения (``team`` / ``guest``).
            wall: Реальное время получения (секунды, монотонные или Unix — главное, единые).
        """
        nodes = data.get("nodes")
        if isinstance(nodes, dict):
            # Обновляем узлы, пришедшие в снимке; пропавший узел остаётся с прежним временем.
            for node_id, body in nodes.items():
                if isinstance(body, dict):
                    previous = self.nodes.get(node_id)
                    if previous is None or body.get("updated_at") != previous.get("updated_at"):
                        self._progress_wall[node_id] = wall
                    self.nodes[node_id] = body
                    for path, num in flatten_numbers(body, node_id):
                        dq = self._series.get(path)
                        if dq is None:
                            dq = self._series[path] = deque(maxlen=self._history_len)
                        dq.append((wall, num))
        if isinstance(nodes, dict):
            self.forecaster.observe(nodes, wall)
        gt = data.get("game_time")
        if isinstance(gt, dict):
            self.game_time = gt
        self.role = role
        self.wall = wall
        self.updates += 1

    # ------------------------------------------------------------------ чтение

    def node(self, node_id: str) -> dict[str, Any]:
        """Узел целиком (пустой словарь, если узла нет)."""
        return self.nodes.get(node_id) or {}

    def has(self, node_id: str) -> bool:
        """Есть ли узел в телеметрии."""
        return node_id in self.nodes

    def get(self, path: str, default: Any = None) -> Any:
        """Значение по пути ``node.field[idx].sub``; ``default``, если чего-то на пути нет или там ``None``."""
        current: Any = self.nodes
        for part in split_path(path):
            if isinstance(part, int):
                if isinstance(current, list) and 0 <= part < len(current):
                    current = current[part]
                else:
                    return default
            elif isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return default
            if current is None:
                return default
        return current

    def num(self, path: str) -> float | None:
        """Числовое значение по пути (``None`` для не числа и ``bool``)."""
        value = self.get(path)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return None
        return float(value)

    def line(self, number: int) -> dict[str, Any]:
        """Линия щитка по номеру 1..8 (пустой словарь, если нет)."""
        for item in self.get("smart_panel_01.lines", []) or []:
            if isinstance(item, dict) and item.get("line") == number:
                return item
        return {}

    def series(self, path: str, window_s: float | None = None) -> list[tuple[float, float]]:
        """История числового поля ``(wall, значение)``, при необходимости — за последние ``window_s``."""
        dq = self._series.get(path)
        if not dq:
            return []
        if window_s is None:
            return list(dq)
        cutoff = self.wall - window_s
        return [(t, v) for t, v in dq if t >= cutoff]

    # ------------------------------------------------------------------ актуальность

    def newest_update(self) -> float | None:
        """Самое свежее ``updated_at`` среди узлов (время часов купола)."""
        values = [
            float(n["updated_at"])
            for node_id, n in self.nodes.items()
            if node_id != "meeting_01" and isinstance(n.get("updated_at"), (int, float))
            and not isinstance(n["updated_at"], bool) and math.isfinite(n["updated_at"])
        ]
        return max(values) if values else None

    def age_s(self, node_id: str) -> float | None:
        """На сколько секунд часов купола отстаёт узел от самого свежего (``None`` — неизвестно)."""
        node = self.nodes.get(node_id)
        newest = self.newest_update()
        if (not node or newest is None or isinstance(node.get("updated_at"), bool)
                or not isinstance(node.get("updated_at"), (int, float)) or not math.isfinite(node["updated_at"])):
            return None
        return max(0.0, newest - float(node["updated_at"]))

    def is_frozen(self, node_id: str) -> bool:
        """Данные узла устарели: ``status == UNKNOWN`` или он отстаёт больше ``stale_age_s``."""
        node = self.nodes.get(node_id)
        if not node:
            return False
        if str(node.get("status", "")).upper() == "UNKNOWN" or node.get("online") is False:
            return True
        progressed = self._progress_wall.get(node_id)
        if progressed is not None and self.wall - progressed > self._stale_age_s:
            return True
        age = self.age_s(node_id)
        return age is not None and age > self._stale_age_s

    def frozen_nodes(self) -> list[str]:
        """Все узлы с устаревшими данными (кроме чата)."""
        return [n for n in self.nodes if n != "meeting_01" and self.is_frozen(n)]
