"""Вызовы DeepSeek: диагностика сложных ситуаций и текст для Оператора.

Модель — совещательный контур: она не командует напрямую, а предлагает диагноз и
план. Исполняет план детерминированный слой (policy/whitelist) в brain.py.
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from agent_log import AgentLog
from config import Config

SYSTEM = (
    "Ты — дежурный инженер автономного купола. Твоя задача — чтобы работа Оператора "
    "не прерывалась, а сам Оператор отвлекался только на физические действия, которые "
    "нельзя сделать удалённо.\n\n"
    "Правила:\n"
    "- Безопасность выше производства. Духота (CO2), дым, электрика — приоритет.\n"
    "- Один датчик против двух — сначала проверь, не сбой ли это. Застывшие данные — не факт.\n"
    "- Не предлагай повторять одну и ту же команду: 'принято' не значит 'выполнено'.\n"
    "- Не предлагай resume станка, пока причина удержания не устранена: три попытки дают "
    "ALARM на 20 минут.\n"
    "- Не предлагай запуск печати, если bed_clear = false.\n"
    "- Оператора проси только о физических действиях и коротко: что сделать, где, зачем.\n\n"
    "Ответ — строго JSON без пояснений:\n"
    '{"situation": "...", "cause": "...", "confidence": 0.0-1.0, '
    '"plan": ["..."], "operator_message": "..." или null, "severity": 1-5}'
)

_NAME = re.compile(r"[\w\-]{2,32}")


class LLM:
    def __init__(self, cfg: Config, log: AgentLog) -> None:
        self.cfg = cfg
        self.log = log
        self.calls = 0
        self.failures = 0
        self.disabled_until = 0.0
        self.last_call = 0.0
        self._client = None
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    # ------------------------------------------------------------------ служебное
    def _client_or_none(self):
        if self._client is not None:
            return self._client
        if not (self.cfg.llm_enabled and self.cfg.deepseek_api_key):
            return None
        try:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self.cfg.deepseek_api_key,
                base_url=self.cfg.deepseek_base_url,
                timeout=self.cfg.llm_timeout_s,
                max_retries=1,
            )
        except Exception as exc:  # noqa: BLE001
            self.log.write("error", where="llm.init", error=str(exc))
            return None
        return self._client

    def available(self) -> bool:
        return (
            self.cfg.llm_enabled
            and bool(self.cfg.deepseek_api_key)
            and time.time() >= self.disabled_until
            and self.calls < self.cfg.llm_budget_calls
            and (time.time() - self.last_call) >= self.cfg.llm_min_interval_s
        )

    # ------------------------------------------------------------------ вызов
    async def ask(self, purpose: str, state_text: str, rag_hits: list[dict] | None = None) -> dict | None:
        if not self.available():
            return None
        client = self._client_or_none()
        if client is None:
            return None
        context = ""
        if rag_hits:
            context = "\n\nПохожие случаи из журнала и базы знаний:\n" + "\n".join(
                f"- [{h['source']}] {h['text'][:600]}" for h in rag_hits[:3]
            )
        user = f"Текущее состояние купола:\n{state_text}{context}\n\nЧто происходит и что делать?"
        started = time.monotonic()
        self.last_call = time.time()
        self.calls += 1
        try:
            response = await asyncio.to_thread(self._call, client, user)
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            if self.failures >= 3:
                self.disabled_until = time.time() + 600
                self.failures = 0
            self.log.write("llm_call", model=self.cfg.model, purpose=purpose, ok=False,
                           error=f"{type(exc).__name__}: {exc}", latency_ms=int((time.monotonic() - started) * 1000))
            return None
        text, usage = response
        parsed = self._parse(text)
        self.log.write(
            "llm_call",
            model=self.cfg.model,
            purpose=purpose,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            latency_ms=int((time.monotonic() - started) * 1000),
            ok=parsed is not None,
            summary=(parsed or {}).get("cause") or text[:300],
        )
        self.total_prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
        self.total_completion_tokens += getattr(usage, "completion_tokens", 0) or 0
        return parsed

    def _call(self, client, user: str):
        response = client.chat.completions.create(
            model=self.cfg.model,
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            temperature=0.2,
            max_tokens=600,
        )
        return response.choices[0].message.content or "", response.usage

    @staticmethod
    def _parse(text: str) -> dict | None:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.IGNORECASE)
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        plan = data.get("plan")
        data["plan"] = [str(p)[:200] for p in plan][:6] if isinstance(plan, list) else []
        try:
            data["confidence"] = float(data.get("confidence", 0.5))
        except (TypeError, ValueError):
            data["confidence"] = 0.5
        try:
            data["severity"] = int(data.get("severity", 2))
        except (TypeError, ValueError):
            data["severity"] = 2
        return data
