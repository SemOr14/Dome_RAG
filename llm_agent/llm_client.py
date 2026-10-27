"""Клиент DeepSeek (OpenAI-совместимый API): отправка промпта, повторы, замер расхода."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from openai import OpenAI

from .config import Settings


@dataclass
class LLMResult:
    ok: bool
    text: str | None
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    attempts: int = 0
    error: str | None = None
    tool_calls: list[dict] = field(default_factory=list)


class DeepSeekClient:
    def __init__(self, settings: Settings):
        if not settings.deepseek_api_key:
            raise RuntimeError("Не задан ключ DeepSeek: DEEPSEEK_API_KEY или строка 'deepseek:' в KEYS_FILE")
        self.settings = settings
        # Повторы делаем сами, чтобы контролировать паузы и считать попытки
        self._client = OpenAI(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url,
                              timeout=settings.timeout_s, max_retries=0)

    def chat(self, messages: list[dict], *, json_mode: bool = True, model: str | None = None,
             tools: list[dict] | None = None) -> LLMResult:
        model = model or self.settings.model
        kwargs: dict = {"model": model, "messages": messages, "max_tokens": self.settings.max_tokens}
        if model != "deepseek-reasoner":  # reasoner не поддерживает temperature и json_object
            kwargs["temperature"] = self.settings.temperature
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        error = None
        attempts = self.settings.max_retries + 1
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                resp = self._client.chat.completions.create(**kwargs)
            except Exception as exc:  # таймаут, сеть, лимит — агент должен жить дальше
                error = f"{type(exc).__name__}: {exc}"
                if attempt < attempts:
                    time.sleep(min(2 ** attempt, 8))
                continue
            usage = resp.usage
            return LLMResult(
                ok=True, text=resp.choices[0].message.content, model=model,
                tool_calls=[call.model_dump(exclude_none=True) for call in (resp.choices[0].message.tool_calls or [])],
                prompt_tokens=usage.prompt_tokens if usage else None,
                completion_tokens=usage.completion_tokens if usage else None,
                latency_ms=int((time.monotonic() - started) * 1000), attempts=attempt)
        return LLMResult(ok=False, text=None, model=model, attempts=attempts, error=error)
