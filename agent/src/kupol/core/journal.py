"""Журнал решений: JSONL (машинный), CSV и Markdown (для судей).

Поля записи соответствуют п. 7.3 регламента. Каждый вызов модели пишется
отдельно (`log_llm_call`), чтобы можно было восстановить, какие данные
использовал агент, что вывел, что предложил или сделал и каков результат
(регламент п. 10.2–10.3).

Запись атомарна на уровне строки: после каждой строки выполняется flush и
fsync, поэтому при падении процесса теряется не более одной незавершённой
строки, которую читатель молча пропускает.
"""

from __future__ import annotations

import csv
import json
import os
import threading
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from kupol.core.models import JournalEntry, Telemetry, utcnow

JOURNAL_FILE = "journal.jsonl"
TELEMETRY_FILE = "telemetry.jsonl"

# Колонки таблицы для судей: порядок соответствует п. 7.3 регламента.
TABLE_COLUMNS = (
    "ts",
    "actor",
    "event",
    "cross_check",
    "hypothesis",
    "impact",
    "action",
    "executor",
    "result",
    "status",
)


def new_run_dir(logs_dir: str | Path, label: str = "run") -> Path:
    """Создать каталог прогона `logs/<время>-<метка>/`.

    Имя строится по системным часам (а не виртуальным), чтобы прогоны не
    перезаписывали друг друга.

    Args:
        logs_dir: Корневой каталог журналов.
        label: Короткая метка прогона (имя сценария и т.п.).

    Returns:
        Путь к созданному каталогу.
    """
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label) or "run"
    base = Path(logs_dir) / f"{stamp}-{safe}"
    path = base
    n = 1
    while path.exists():
        n += 1
        path = Path(f"{base}-{n}")
    path.mkdir(parents=True)
    return path


class _JsonlWriter:
    """Потокобезопасная построчная запись JSONL с принудительной синхронизацией."""

    def __init__(self, path: Path) -> None:
        """Открыть файл на дозапись (создаёт каталоги).

        Args:
            path: Путь к файлу.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh: IO[str] | None = open(path, "a", encoding="utf-8", newline="\n")  # noqa: SIM115
        self._lock = threading.Lock()

    def write(self, line: str) -> None:
        """Записать одну строку и сбросить на диск.

        Args:
            line: Строка без перевода строки.
        """
        with self._lock:
            if self._fh is None:
                raise ValueError("журнал закрыт")
            self._fh.write(line + "\n")
            self._fh.flush()
            try:
                os.fsync(self._fh.fileno())
            except OSError:  # pragma: no cover - fsync может быть недоступен на экзотических ФС
                pass

    def close(self) -> None:
        """Закрыть файл (повторный вызов безопасен)."""
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None


def read_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    """Читать JSONL, пропуская пустые и оборванные строки.

    Args:
        path: Путь к файлу.

    Yields:
        Разобранные объекты. Файла нет — итератор пуст.
    """
    p = Path(path)
    if not p.exists():
        return
    with p.open(encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue  # оборванная при падении последняя строка
            if isinstance(obj, dict):
                yield obj


def _cell(value: object) -> str:
    """Подготовить значение для ячейки Markdown-таблицы."""
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


class Journal:
    """Журнал решений одного прогона.

    Attributes:
        run_dir: Каталог прогона.
        path: Путь к `journal.jsonl`.
    """

    def __init__(self, run_dir: str | Path, clock: Callable[[], datetime] | None = None) -> None:
        """Открыть журнал в каталоге прогона.

        Args:
            run_dir: Каталог прогона (создаётся при необходимости).
            clock: Источник времени для записей без явного `ts`. В симуляции
                передаётся виртуальное время адаптера.
        """
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / JOURNAL_FILE
        self._clock = clock or utcnow
        self._cache: list[JournalEntry] = self._load_existing()
        self._cache_lock = threading.Lock()
        self._writer = _JsonlWriter(self.path)

    def _load_existing(self) -> list[JournalEntry]:
        """Прочитать записи, уже лежащие в файле (журнал продолжается после перезапуска)."""
        out: list[JournalEntry] = []
        for obj in read_jsonl(self.path):
            try:
                out.append(JournalEntry.model_validate(obj))
            except ValueError:
                continue
        return out

    def set_clock(self, clock: Callable[[], datetime]) -> None:
        """Заменить источник времени (например, на время адаптера).

        Args:
            clock: Функция без аргументов, возвращающая время в UTC.
        """
        self._clock = clock

    def log(self, entry: JournalEntry | None = None, **fields: Any) -> JournalEntry:
        """Добавить запись в журнал.

        Можно передать готовый `JournalEntry` или поля именованными аргументами
        (`actor`, `event`, `cross_check`, `hypothesis`, `impact`, `action`,
        `executor`, `result`, `status`, `kind`, `data`). Если `ts` не задан,
        берётся время журнала.

        Args:
            entry: Готовая запись.
            **fields: Поля новой записи, если `entry` не передан.

        Returns:
            Записанная запись.

        Raises:
            ValueError: Журнал закрыт.
            pydantic.ValidationError: Поля записи некорректны.
        """
        if entry is None:
            fields.setdefault("ts", self._clock())
            entry = JournalEntry(**fields)
        self._writer.write(entry.model_dump_json())
        with self._cache_lock:
            self._cache.append(entry)
        return entry

    def log_llm_call(
        self,
        agent: str,
        prompt_hash: str,
        inputs_summary: str | dict[str, Any],
        raw_output: str,
        parsed_decision: dict[str, Any] | None,
        used_fallback: bool,
        **extra: Any,
    ) -> JournalEntry:
        """Записать вызов модели (регламент п. 10.2–10.3).

        Args:
            agent: Имя агента.
            prompt_hash: Хэш текста промпта (сами промпты версионируются в git).
            inputs_summary: Краткое описание/сводка входных данных вызова.
            raw_output: Сырой ответ модели (пустой при таймауте).
            parsed_decision: Разобранное решение или `None`, если разбор не удался.
            used_fallback: Применён ли резервный сценарий.
            **extra: Дополнительные данные (модель, режим, длительность, ошибка).

        Returns:
            Записанная запись типа `llm_call`.
        """
        data: dict[str, Any] = {
            "agent": agent,
            "prompt_hash": prompt_hash,
            "inputs_summary": inputs_summary,
            "raw_output": raw_output,
            "parsed_decision": parsed_decision,
            "used_fallback": used_fallback,
            **extra,
        }
        return self.log(
            actor=agent,
            event="вызов модели",
            action="резервный сценарий" if used_fallback else "ответ модели принят",
            status="fallback" if used_fallback else "ok",
            kind="llm_call",
            data=data,
        )

    def entries(self, kinds: Iterable[str] | None = None) -> list[JournalEntry]:
        """Записи журнала в порядке добавления (из памяти; файл остаётся источником правды).

        Args:
            kinds: Оставить только записи этих типов; `None` — все.

        Returns:
            Копия списка записей.
        """
        wanted = set(kinds) if kinds is not None else None
        with self._cache_lock:
            items = list(self._cache)
        return [e for e in items if wanted is None or e.kind in wanted]

    def tail(self, n: int = 20, exclude_kinds: Iterable[str] = ("llm_call",)) -> list[JournalEntry]:
        """Последние записи журнала для панели.

        Args:
            n: Сколько записей вернуть.
            exclude_kinds: Типы записей, которые не показывать.

        Returns:
            До `n` последних записей, старые первыми.
        """
        skip = set(exclude_kinds)
        return [e for e in self.entries() if e.kind not in skip][-n:]

    def export_csv(self, path: str | Path | None = None) -> Path:
        """Экспортировать решения в CSV (без вызовов модели).

        Args:
            path: Куда писать; по умолчанию `journal.csv` в каталоге прогона.

        Returns:
            Путь к файлу.
        """
        target = Path(path) if path else self.run_dir / "journal.csv"
        with target.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(TABLE_COLUMNS)
            for e in self.entries():
                if e.kind == "llm_call":
                    continue
                dumped = e.model_dump(mode="json")
                writer.writerow([dumped[c] for c in TABLE_COLUMNS])
        return target

    def export_markdown(self, path: str | Path | None = None) -> Path:
        """Экспортировать решения в читаемую Markdown-таблицу для судей.

        Args:
            path: Куда писать; по умолчанию `journal.md` в каталоге прогона.

        Returns:
            Путь к файлу.
        """
        target = Path(path) if path else self.run_dir / "journal.md"
        lines = [
            "# Журнал решений",
            "",
            "| " + " | ".join(TABLE_COLUMNS) + " |",
            "|" + "|".join("---" for _ in TABLE_COLUMNS) + "|",
        ]
        for e in self.entries():
            if e.kind == "llm_call":
                continue
            dumped = e.model_dump(mode="json")
            lines.append("| " + " | ".join(_cell(dumped[c]) for c in TABLE_COLUMNS) + " |")
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return target

    def close(self) -> None:
        """Закрыть журнал (повторный вызов безопасен)."""
        self._writer.close()


class TelemetryLog:
    """Запись снимков телеметрии прогона для `replay` и отчёта."""

    def __init__(self, run_dir: str | Path) -> None:
        """Открыть `telemetry.jsonl` в каталоге прогона.

        Args:
            run_dir: Каталог прогона.
        """
        self.path = Path(run_dir) / TELEMETRY_FILE
        self._writer = _JsonlWriter(self.path)

    def write(self, telemetry: Telemetry) -> None:
        """Записать снимок.

        Args:
            telemetry: Снимок телеметрии.
        """
        self._writer.write(telemetry.model_dump_json())

    def close(self) -> None:
        """Закрыть файл."""
        self._writer.close()


def read_telemetry(run_dir: str | Path) -> list[Telemetry]:
    """Прочитать сохранённую телеметрию прогона.

    Args:
        run_dir: Каталог прогона.

    Returns:
        Снимки в порядке записи; повреждённые строки пропускаются.
    """
    out: list[Telemetry] = []
    for obj in read_jsonl(Path(run_dir) / TELEMETRY_FILE):
        try:
            out.append(Telemetry.model_validate(obj))
        except ValueError:
            continue
    return out
