"""Обёртка модели DeepSeek V4 Flash с фолбэком, строгим JSON и журналом.

Регламент п. 10.6: в зачётной игре допустима ТОЛЬКО модель DeepSeek V4 Flash.
Поэтому в режиме ``competition`` любое другое имя модели вызывает
``ForbiddenModelError`` (проверка и в конструкторе, и перед каждым вызовом).

Режимы (``llm.mode`` в ``config.yaml``):

* ``mock`` — без сети и без ключа; ответы даёт вызывающий код (детерминированные
  заглушки), чтобы проект работал и тестировался офлайн;
* ``dev`` — реальная модель на подготовке (любое имя модели из конфига);
* ``competition`` — реальная модель, только разрешённая.

Надёжность: таймаут, ОДИН повтор запроса, строгий JSON по pydantic-схеме
(при ошибке разбора — один повтор с просьбой исправить формат), затем фолбэк:
вызывающий код получает ``used_fallback=True`` и обязан применить правило в коде.
Каждый вызов пишется в журнал (п. 10.2–10.3): хэш промпта, входные данные,
сырой ответ, разобранное решение, признак фолбэка.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from kupol.core.config import LLMConfig
from kupol.core.journal import Journal

T = TypeVar("T", bound=BaseModel)

PROMPTS_DIR = Path(__file__).parent / "prompts"
JSON_FORMAT_REMINDER = (
    "Ответ не прошёл проверку формата: {error}. Верни ТОЛЬКО корректный JSON по заданной схеме, без пояснений и без markdown."
)


class LLMError(Exception):
    """Базовая ошибка слоя LLM."""


class LLMConfigError(LLMError):
    """Некорректная конфигурация LLM (например, нет ключа в боевом режиме)."""


class ForbiddenModelError(LLMError):
    """Запрошена модель, не разрешённая регламентом в режиме competition (п. 10.6)."""


class LLMTransportError(LLMError):
    """Сетевая ошибка, таймаут или плохой HTTP-статус."""


def load_prompt(name: str) -> str:
    """Прочитать промпт из ``agents/prompts/<name>.md``.

    Args:
        name: Имя файла без расширения.

    Returns:
        Текст промпта.

    Raises:
        FileNotFoundError: Файла нет.
    """
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def load_examples(name: str) -> str:
    """Few-shot примеры агента из ``agents/prompts/examples/<name>_*.md`` (по алфавиту).

    Args:
        name: Имя агента (префикс файлов примеров).

    Returns:
        Склеенный текст примеров (пустая строка, если примеров нет).
    """
    files = sorted((PROMPTS_DIR / "examples").glob(f"{name}_*.md"))
    return "\n\n".join(f.read_text(encoding="utf-8").strip() for f in files)


def prompt_hash(text: str) -> str:
    """Короткий хэш текста промпта для журнала.

    Args:
        text: Текст.

    Returns:
        Первые 16 hex-символов SHA-256.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class LLMResult(Generic[T]):
    """Итог вызова модели.

    Attributes:
        parsed: Проверенный ответ по схеме или ``None`` при фолбэке.
        raw: Сырой текст ответа (пустой при таймауте).
        used_fallback: Если ``True`` — вызывающий обязан применить резервный сценарий.
        error: Причина фолбэка (для журнала и панели).
        prompt_hash: Хэш системного промпта.
        duration_s: Длительность вызова, с.
        attempts: Сколько запросов к модели сделано.
    """

    parsed: T | None
    raw: str
    used_fallback: bool
    error: str | None
    prompt_hash: str
    duration_s: float
    attempts: int


FailureMode = Literal["timeout", "bad_json"]


class LLMClient:
    """Клиент DeepSeek (OpenAI-совместимый HTTP API) с режимом mock."""

    def __init__(
        self,
        config: LLMConfig,
        journal: Journal | None = None,
        http_client: httpx.Client | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        """Создать клиент.

        Args:
            config: Раздел ``llm`` конфигурации.
            journal: Журнал для записи каждого вызова.
            http_client: HTTP-клиент (в тестах — с ``httpx.MockTransport``).
            env: Переменные окружения (по умолчанию ``os.environ``); ключ берётся только оттуда.

        Raises:
            ForbiddenModelError: Режим competition и модель не разрешена.
            LLMConfigError: Режим dev/competition без ключа API.
        """
        self._cfg = config
        self._journal = journal
        self._env = os.environ if env is None else env
        self._http = http_client
        self.simulate_failure: FailureMode | None = None  # только mock: имитация отказа модели
        self._guard_model()
        self._api_key = self._env.get(config.api_key_env, "")
        if config.mode != "mock" and not self._api_key:
            raise LLMConfigError(
                f"режим '{config.mode}' требует ключ API в переменной окружения {config.api_key_env} (см. .env.example)"
            )

    # ------------------------------------------------------------------ публичный API

    def set_journal(self, journal: Journal | None) -> None:
        """Подключить журнал, в который пишется каждый вызов модели.

        Args:
            journal: Журнал прогона или ``None``.
        """
        self._journal = journal

    @property
    def mode(self) -> str:
        """Текущий режим (``mock`` / ``dev`` / ``competition``)."""
        return self._cfg.mode

    @property
    def model(self) -> str:
        """Имя используемой модели."""
        return self._cfg.model

    def call(
        self,
        agent: str,
        system_prompt: str,
        payload: Mapping[str, Any],
        schema: type[T],
        mock: Callable[[], T] | None = None,
        inputs_summary: str | Mapping[str, Any] | None = None,
    ) -> LLMResult[T]:
        """Вызвать модель и вернуть проверенный ответ либо признак фолбэка.

        Args:
            agent: Имя агента (в журнал).
            system_prompt: Текст системного промпта (файл из ``agents/prompts``).
            payload: Входные данные для модели (сериализуются в JSON; идут в журнал целиком).
            schema: Pydantic-схема строгого JSON-ответа.
            mock: Генератор детерминированного ответа для режима ``mock``.
            inputs_summary: Краткая сводка входа для журнала (по умолчанию ключи payload).

        Returns:
            ``LLMResult``; при ``used_fallback=True`` поле ``parsed`` равно ``None``.

        Raises:
            ForbiddenModelError: В режиме competition модель не разрешена (никакого фолбэка:
                это нарушение регламента, а не сбой).
        """
        self._guard_model()
        started = time.monotonic()
        p_hash = prompt_hash(system_prompt)
        full_system = self._with_schema(system_prompt, schema)
        user_text = json.dumps(payload, ensure_ascii=False, default=str)
        if self._cfg.mode == "mock":
            result = self._call_mock(schema, mock, p_hash)
        else:
            result = self._call_remote(full_system, user_text, schema, p_hash)
        result.duration_s = time.monotonic() - started
        self._log(agent, result, payload, inputs_summary)
        return result

    # ------------------------------------------------------------------ проверки

    def _guard_model(self) -> None:
        """В режиме competition допускается только разрешённая модель (регламент п. 10.6)."""
        if self._cfg.mode == "competition" and self._cfg.model not in self._cfg.allowed_models:
            raise ForbiddenModelError(
                f"В зачётной игре допустимы только модели {', '.join(self._cfg.allowed_models)} (регламент п. 10.6), "
                f"запрошена '{self._cfg.model}'"
            )

    # ------------------------------------------------------------------ mock

    def _call_mock(self, schema: type[T], mock: Callable[[], T] | None, p_hash: str) -> LLMResult[T]:
        """Офлайн-режим: детерминированный ответ-заглушка или имитация отказа."""
        if self.simulate_failure == "timeout":
            return LLMResult(None, "", True, "имитация: таймаут модели", p_hash, 0.0, 0)
        if self.simulate_failure == "bad_json":
            return LLMResult(None, "это не JSON", True, "имитация: ответ не прошёл проверку формата", p_hash, 0.0, 1)
        if mock is None:
            return LLMResult(None, "", True, "в режиме mock не задана заглушка ответа", p_hash, 0.0, 0)
        parsed = mock()
        return LLMResult(parsed, parsed.model_dump_json(), False, None, p_hash, 0.0, 0)

    # ------------------------------------------------------------------ сеть

    @staticmethod
    def _with_schema(system_prompt: str, schema: type[BaseModel]) -> str:
        """Добавить к промпту JSON-схему ответа."""
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        return f"{system_prompt.rstrip()}\n\nОтвечай ТОЛЬКО JSON-объектом по схеме:\n{schema_json}"

    def _call_remote(self, system: str, user_text: str, schema: type[T], p_hash: str) -> LLMResult[T]:
        """Реальный вызов: запрос (+1 повтор при сбое сети) и разбор (+1 повтор при плохом формате)."""
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]
        attempts = 0
        raw = ""
        error: str | None = None
        format_retry_used = False
        transport_retry_used = False
        while True:
            attempts += 1
            try:
                raw = self._post(messages)
            except LLMTransportError as exc:
                error = f"сбой связи с моделью: {exc}"
                if transport_retry_used:
                    return LLMResult(None, raw, True, error, p_hash, 0.0, attempts)
                transport_retry_used = True
                continue
            try:
                parsed = schema.model_validate_json(self._extract_json(raw))
            except (ValidationError, ValueError) as exc:
                error = f"ответ не прошёл проверку формата: {_short(str(exc))}"
                if format_retry_used:
                    return LLMResult(None, raw, True, error, p_hash, 0.0, attempts)
                format_retry_used = True
                messages = [
                    *messages[:2],
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": JSON_FORMAT_REMINDER.format(error=_short(str(exc)))},
                ]
                continue
            return LLMResult(parsed, raw, False, None, p_hash, 0.0, attempts)

    def _post(self, messages: list[dict[str, str]]) -> str:
        """Один запрос chat/completions. Возвращает текст ответа модели."""
        url = self._cfg.base_url.rstrip("/") + "/chat/completions"
        body = {
            "model": self._cfg.model,
            "messages": messages,
            "temperature": self._cfg.temperature,
            "max_tokens": self._cfg.max_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        client = self._http or httpx.Client()
        try:
            resp = client.post(url, json=body, headers=headers, timeout=self._cfg.timeout_s)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except httpx.TimeoutException as exc:
            raise LLMTransportError(f"таймаут {self._cfg.timeout_s:g} с") from exc
        except httpx.HTTPStatusError as exc:
            raise LLMTransportError(f"HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise LLMTransportError(f"сеть: {exc.__class__.__name__}") from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMTransportError(f"неожиданный формат ответа API: {exc.__class__.__name__}") from exc
        finally:
            if self._http is None:
                client.close()
        if not isinstance(content, str):
            raise LLMTransportError("в ответе API нет текста")
        return content

    @staticmethod
    def _extract_json(raw: str) -> str:
        """Вынуть JSON из ответа (модель может обернуть его в ```json ... ```)."""
        text = raw.strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()
        return text

    # ------------------------------------------------------------------ журнал

    def _log(
        self,
        agent: str,
        result: LLMResult[T],
        payload: Mapping[str, Any],
        inputs_summary: str | Mapping[str, Any] | None,
    ) -> None:
        """Записать вызов модели в журнал (п. 10.2–10.3)."""
        if self._journal is None:
            return
        summary: str | dict[str, Any]
        if inputs_summary is None:
            summary = {"keys": sorted(payload)}
        elif isinstance(inputs_summary, str):
            summary = inputs_summary
        else:
            summary = dict(inputs_summary)
        self._journal.log_llm_call(
            agent=agent,
            prompt_hash=result.prompt_hash,
            inputs_summary=summary,
            raw_output=result.raw,
            parsed_decision=result.parsed.model_dump(mode="json") if result.parsed is not None else None,
            used_fallback=result.used_fallback,
            model=self._cfg.model,
            mode=self._cfg.mode,
            duration_s=round(result.duration_s, 3),
            attempts=result.attempts,
            error=result.error,
            inputs=dict(payload),
        )


def _short(text: str, limit: int = 300) -> str:
    """Обрезать длинное сообщение об ошибке."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
