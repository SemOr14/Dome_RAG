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
    max_tokens: int = 2000
    prompt_dir: Path = field(default_factory=lambda: ROOT / "prompts")
    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    journal_limit: int = 15
    rag_k: int = 3
    graph_depth: int = 1

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
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "2000")),
            prompt_dir=_path("PROMPT_TEMPLATE_DIR", "prompts"),
            data_dir=_path("DATA_DIR", "data"),
            journal_limit=int(os.getenv("PROMPT_JOURNAL_LIMIT", "15")),
            rag_k=int(os.getenv("PROMPT_RAG_K", "3")),
            graph_depth=int(os.getenv("PROMPT_GRAPH_DEPTH", "1")),
        )

    def public(self) -> dict:
        """Настройки без ключей — для логов."""
        return {
            "deepseek_base_url": self.deepseek_base_url, "model": self.model,
            "timeout_s": self.timeout_s, "max_retries": self.max_retries,
            "prompt_dir": str(self.prompt_dir), "data_dir": str(self.data_dir),
            "journal_limit": self.journal_limit, "rag_k": self.rag_k, "graph_depth": self.graph_depth,
            "api_key_set": bool(self.deepseek_api_key),
        }
