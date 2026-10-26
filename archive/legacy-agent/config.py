"""Конфигурация агента.

Всё, что отличается между тренировочным и реальным куполом, задаётся переменными
окружения (см. .env.example). Ключи в коде и в логах не хранятся.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

def _load_dotenv(path: str = ".env") -> None:
    """Подхватывает .env рядом с агентом, не перетирая уже заданные переменные."""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_dotenv()

AGENT_NAME = "kupol-agent"
VERSION = os.getenv("AGENT_VERSION", "1.0.0")


def _s(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None or not value.strip() else value.strip()


def _b(name: str, default: bool = False) -> bool:
    value = _s(name).lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on", "y", "да")


def _i(name: str, default: int) -> int:
    try:
        return int(_s(name) or default)
    except ValueError:
        return default


def _first_dir(*candidates: str) -> str:
    for path in candidates:
        if path and os.path.isdir(path):
            return path
    return ""


def _f(name: str, default: float) -> float:
    try:
        return float(_s(name) or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    # --- купол ---
    dome_url: str = field(default_factory=lambda: _s("DOME_URL", "ws://127.0.0.1:8765"))
    dome_key: str = field(default_factory=lambda: _s("DOME_KEY"))

    # --- языковая модель ---
    deepseek_api_key: str = field(default_factory=lambda: _s("DEEPSEEK_API_KEY"))
    deepseek_base_url: str = field(default_factory=lambda: _s("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    model: str = field(default_factory=lambda: _s("AGENT_MODEL", "deepseek-chat"))
    llm_enabled: bool = field(default_factory=lambda: _b("AGENT_LLM_ENABLED", True))
    llm_min_interval_s: float = field(default_factory=lambda: _f("AGENT_LLM_MIN_INTERVAL_S", 75.0))
    llm_budget_calls: int = field(default_factory=lambda: _i("AGENT_LLM_BUDGET_CALLS", 400))
    llm_timeout_s: float = field(default_factory=lambda: _f("AGENT_LLM_TIMEOUT_S", 45.0))

    # --- работа ---
    log_dir: str = field(default_factory=lambda: _s("AGENT_LOG_DIR", "logs"))
    state_path: str = field(default_factory=lambda: _s("AGENT_STATE_PATH", "state/agent_state.json"))
    dry_run: bool = field(default_factory=lambda: _b("AGENT_DRY_RUN", False))
    tick_s: float = field(default_factory=lambda: _f("AGENT_TICK_S", 1.0))
    observation_every_s: float = field(default_factory=lambda: _f("AGENT_OBSERVATION_EVERY_S", 45.0))
    stale_after_s: float = field(default_factory=lambda: _f("AGENT_STALE_AFTER_S", 30.0))
    data_stale_s: float = field(default_factory=lambda: _f("AGENT_DATA_STALE_S", 12.0))
    max_cmds_per_s: int = field(default_factory=lambda: _i("AGENT_MAX_CMDS_PER_S", 4))
    rag_enabled: bool = field(default_factory=lambda: _b("AGENT_RAG_ENABLED", True))
    knowledge_dir: str = field(default_factory=lambda: _s("AGENT_KNOWLEDGE_DIR") or _first_dir(
        "knowledge", "../Dome_RAG-main/Dome_RAG-main/knowledge", "Dome_RAG-main/knowledge"))
    journal_dir: str = field(default_factory=lambda: _s("AGENT_JOURNAL_DIR") or _first_dir(
        "Logs", "../Dome_RAG-main/Dome_RAG-main/Logs", "Dome_RAG-main/Logs"))
    operator_name: str = field(default_factory=lambda: _s("AGENT_OPERATOR_NAME", "kupol-agent"))

    def public(self) -> dict:
        """Настройки для записи start без секретов."""
        return {
            "dome_url": self.dome_url,
            "model": self.model if self.llm_enabled else None,
            "dry_run": self.dry_run,
            "tick_s": self.tick_s,
            "rag_enabled": self.rag_enabled,
            "stale_after_s": self.stale_after_s,
            "max_cmds_per_s": self.max_cmds_per_s,
        }

    def check(self) -> list[str]:
        problems = []
        if not self.dome_key:
            problems.append("DOME_KEY не задан")
        if not self.dome_url.startswith(("ws://", "wss://")):
            problems.append("DOME_URL должен начинаться с ws:// или wss://")
        if self.llm_enabled and not self.deepseek_api_key:
            problems.append("DEEPSEEK_API_KEY не задан (агент продолжит без LLM)")
        return problems
