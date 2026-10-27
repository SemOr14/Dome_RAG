"""Настройки LLM-слоя агента. Всё, что меняется между средами, — через переменные окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Минимальный загрузчик .env: не перезаписывает уже заданные переменные."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip())


def _key_from_file(path: Path, label: str) -> str | None:
    """Читает строку вида `<label>: <ключ>` из файла ключей команды."""
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() == label:
            return value.strip() or None
    return None


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).lower() not in ("0", "false", "no")


def _path(name: str, default: str) -> Path:
    value = Path(os.getenv(name, default))
    return value if value.is_absolute() else ROOT / value


@dataclass
class Settings:
    deepseek_api_key: str | None
    deepseek_base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    timeout_s: float = 30.0
    max_retries: int = 2
    temperature: float = 0.2
    max_tokens: int = 4000
    prompt_dir: Path = field(default_factory=lambda: ROOT / "prompts")
    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    journal_limit: int = 15
    rag_k: int = 3
    graph_depth: int = 1
    rag_backend: str = "auto"  # auto | dome | keyword | none
    rag_dir: Path = field(default_factory=lambda: ROOT / "rag")
    rag_semantic: bool = True
    data_api_url: str | None = None  # сервис БД/API (services/data_api); пусто — файловые заглушки data/
    data_api_token: str | None = None
    data_api_run_id: str | None = None  # пусто — последний запуск в БД
    data_api_session_id: str | None = None  # пусто — сессия последнего auth_response запуска
    data_api_timeout_s: float = 5.0
    data_api_check_controls: bool = True
    state_dir: Path = field(default_factory=lambda: ROOT / "state")
    # Диспетчер вызовов (rag/AGENT_TRIGGERS.md): значения предварительные, их нужно подобрать испытаниями
    trigger_poll_s: float = 2.0  # опрос телеметрии и событий
    trigger_min_interval_s: float = 10.0  # не чаще одного вызова модели за это время
    trigger_cooldown_s: float = 120.0  # пауза повтора одной и той же ситуации
    trigger_review_s: float = 180.0  # плановый обзор при затишье; 0 — выключен
    trigger_stale_s: float = 30.0  # нет новых кадров телеметрии
    trigger_stall_s: float = 180.0  # прогресс печати/фрезеровки не меняется
    trigger_verify_min_s: float = 5.0  # проверка результата не раньше
    trigger_verify_retries: int = 2  # сколько раз ждать новых данных для проверки
    trigger_llm_retries: int = 2  # повторы повода после сбоя модели

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv(ROOT / ".env")
        key = os.getenv("DEEPSEEK_API_KEY") or _key_from_file(
            _path("KEYS_FILE", "Keys/team_1.txt"), "deepseek")
        return cls(
            deepseek_api_key=key,
            deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            model=os.getenv("LLM_MODEL", "deepseek-chat"),
            timeout_s=float(os.getenv("LLM_TIMEOUT_S", "30")),
            max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
            temperature=float(os.getenv("LLM_TEMPERATURE", "0.2")),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "4000")),
            prompt_dir=_path("PROMPT_TEMPLATE_DIR", "prompts"),
            data_dir=_path("DATA_DIR", "data"),
            journal_limit=int(os.getenv("PROMPT_JOURNAL_LIMIT", "15")),
            rag_k=int(os.getenv("PROMPT_RAG_K", "3")),
            graph_depth=int(os.getenv("PROMPT_GRAPH_DEPTH", "1")),
            rag_backend=os.getenv("RAG_BACKEND", "auto").lower(),
            rag_dir=_path("RAG_DIR", "rag"),
            rag_semantic=_flag("RAG_SEMANTIC"),
            data_api_url=os.getenv("DATA_API_URL") or None,
            data_api_token=os.getenv("DATA_API_TOKEN") or None,
            data_api_run_id=os.getenv("DATA_API_RUN_ID") or None,
            data_api_session_id=os.getenv("DATA_API_SESSION_ID") or None,
            data_api_timeout_s=float(os.getenv("DATA_API_TIMEOUT_S", "5")),
            data_api_check_controls=_flag("DATA_API_CHECK_CONTROLS"),
            state_dir=_path("STATE_DIR", "state"),
            trigger_poll_s=float(os.getenv("TRIGGER_POLL_S", "2")),
            trigger_min_interval_s=float(os.getenv("TRIGGER_MIN_INTERVAL_S", "10")),
            trigger_cooldown_s=float(os.getenv("TRIGGER_COOLDOWN_S", "120")),
            trigger_review_s=float(os.getenv("TRIGGER_REVIEW_S", "180")),
            trigger_stale_s=float(os.getenv("TRIGGER_STALE_S", "30")),
            trigger_stall_s=float(os.getenv("TRIGGER_STALL_S", "180")),
            trigger_verify_min_s=float(os.getenv("TRIGGER_VERIFY_MIN_S", "5")),
            trigger_verify_retries=int(os.getenv("TRIGGER_VERIFY_RETRIES", "2")),
            trigger_llm_retries=int(os.getenv("TRIGGER_LLM_RETRIES", "2")),
        )

    def public(self) -> dict:
        """Настройки без ключей — для логов."""
        return {
            "deepseek_base_url": self.deepseek_base_url, "model": self.model,
            "timeout_s": self.timeout_s, "max_retries": self.max_retries,
            "prompt_dir": str(self.prompt_dir), "data_dir": str(self.data_dir),
            "journal_limit": self.journal_limit, "rag_k": self.rag_k, "graph_depth": self.graph_depth,
            "rag_backend": self.rag_backend, "rag_dir": str(self.rag_dir), "rag_semantic": self.rag_semantic,
            "data_api_url": self.data_api_url, "data_api_run_id": self.data_api_run_id,
            "data_api_session_id": self.data_api_session_id, "data_api_check_controls": self.data_api_check_controls,
            "trigger_min_interval_s": self.trigger_min_interval_s, "trigger_cooldown_s": self.trigger_cooldown_s,
            "trigger_review_s": self.trigger_review_s,
            "api_key_set": bool(self.deepseek_api_key), "data_api_token_set": bool(self.data_api_token),
        }
