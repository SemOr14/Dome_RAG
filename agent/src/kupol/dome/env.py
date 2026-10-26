"""Переменные окружения и файл ключей.

Приоритет: окружение процесса → ``.env`` → файл ключей ``KEYS_FILE`` (строки ``team: ...`` и
``deepseek: ...``). Значения ключей нигде не печатаются; в лог попадает только источник.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_KEYS_FILE = "Dome_RAG/Keys/team_1.txt"
DEFAULT_DOME_URL = "ws://192.168.8.10:8765"


def parse_env_file(path: Path) -> dict[str, str]:
    """Разобрать ``.env`` (``KEY=value``, комментарии ``#``, кавычки). Нет файла — пустой словарь."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip("'\"")
        if value:
            out[key.strip()] = value
    return out


def parse_keys_file(path: Path) -> dict[str, str]:
    """Разобрать файл ключей: ``team: <ключ купола>`` и ``deepseek: <ключ DeepSeek>``."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        name, sep, value = raw.partition(":")
        if sep and value.strip():
            out[name.strip().lower()] = value.strip()
    return out


@dataclass
class Settings:
    """Настройки запуска из окружения. ``sources`` — откуда взят каждый секрет (без значений)."""

    dome_url: str
    dome_key: str
    deepseek_key: str
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str | None = None
    log_dir: str = "logs"
    rag_dir: str = "Dome_RAG"
    dry_run: bool = False
    allow_control: bool = False
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def can_control(self) -> bool:
        """Можно ли отправлять ``control``: только явное ``DOME_ALLOW_CONTROL=1`` и не dry-run."""
        return self.allow_control and not self.dry_run


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def load_settings(env: Mapping[str, str] | None = None, root: Path | None = None) -> Settings:
    """Собрать настройки.

    Args:
        env: Окружение процесса (по умолчанию ``os.environ``).
        root: Каталог проекта для ``.env`` и относительных путей (по умолчанию текущий).

    Returns:
        Настройки; отсутствующие секреты — пустые строки (проверяет вызывающий код).
    """
    base = Path.cwd() if root is None else root
    proc: Mapping[str, str] = os.environ if env is None else env
    dotenv = parse_env_file(base / ".env")
    keys_path = Path(proc.get("KEYS_FILE") or dotenv.get("KEYS_FILE") or DEFAULT_KEYS_FILE)
    if not keys_path.is_absolute():
        keys_path = base / keys_path
    keys = parse_keys_file(keys_path)
    sources: dict[str, str] = {}

    def pick(name: str, file_key: str | None = None, default: str = "") -> str:
        if proc.get(name):
            sources[name] = "окружение"
            return proc[name]
        if dotenv.get(name):
            sources[name] = ".env"
            return dotenv[name]
        if file_key and keys.get(file_key):
            sources[name] = "файл ключей"
            return keys[file_key]
        if default:
            sources[name] = "по умолчанию"
        return default

    return Settings(
        dome_url=pick("DOME_URL", default=DEFAULT_DOME_URL),
        dome_key=pick("DOME_KEY", "team"),
        deepseek_key=pick("DEEPSEEK_API_KEY", "deepseek"),
        deepseek_base_url=pick("DEEPSEEK_BASE_URL", default="https://api.deepseek.com"),
        deepseek_model=pick("DEEPSEEK_MODEL") or None,
        log_dir=pick("AGENT_LOG_DIR", default="logs"),
        rag_dir=pick("DOME_RAG_DIR", default="Dome_RAG"),
        dry_run=_flag(pick("AGENT_DRY_RUN")),
        allow_control=_flag(pick("DOME_ALLOW_CONTROL")),
        sources=sources,
    )


def export_to_environ(settings: Settings, environ: MutableMapping[str, str] | None = None) -> None:
    """Положить ключ DeepSeek в окружение процесса (для кода, читающего его из ``os.environ``)."""
    target = os.environ if environ is None else environ
    if settings.deepseek_key:
        target.setdefault("DEEPSEEK_API_KEY", settings.deepseek_key)
