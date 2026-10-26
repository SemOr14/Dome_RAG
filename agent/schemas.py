"""Структурированный выход агента. Поля совместимы с записью `decision` из 03_Требования_к_агенту.md."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class Evidence(BaseModel):
    node: str
    param: str | None = None
    value: Any = None
    note: str | None = None


class ProposedAction(BaseModel):
    """Команда куполу, которую предлагает модель. Исполняется только после validate.py."""
    node_id: str
    action: str
    value: Any = None
    reason: str = ""
    expected: str = Field("", description="что должно измениться в телеметрии")
    verify_after_s: float = 8.0
    priority: int = 1


class OperatorMessage(BaseModel):
    needed: bool = False
    text: str = ""
    to: str = "operator"

    @field_validator("text")
    @classmethod
    def _limit(cls, v: str) -> str:
        return v[:4000]


class AgentDecision(BaseModel):
    situation: str
    cause: str = ""
    severity: Literal["info", "warning", "critical"] = "info"
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    false_alarm: bool | None = None
    evidence: list[Evidence] = []
    actions: list[ProposedAction] = []
    operator_message: OperatorMessage = OperatorMessage()
    wait_and_watch: list[str] = []
    rag_refs: list[str] = []
    risks: list[str] = []


class ActionVerdict(BaseModel):
    action: ProposedAction
    accepted: bool
    reasons: list[str] = []


class LLMCallInfo(BaseModel):
    """Метаданные вызова — для записи `llm_call` в лог агента."""
    model: str
    purpose: str = ""
    ok: bool
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    attempts: int = 0
    error: str | None = None


class AgentResponse(BaseModel):
    ok: bool
    decision: AgentDecision | None = None
    verdicts: list[ActionVerdict] = []
    llm_call: LLMCallInfo
    raw_text: str | None = None
    rag_used: list[str] = []

    @property
    def accepted_actions(self) -> list[ProposedAction]:
        return [v.action for v in self.verdicts if v.accepted]

    @property
    def rejected_actions(self) -> list[ActionVerdict]:
        return [v for v in self.verdicts if not v.accepted]


def output_schema_example() -> dict:
    """Пример ответа, который подставляется в шаблон промпта как образец формата."""
    return {
        "situation": "кратко: что происходит",
        "cause": "предполагаемая причина",
        "severity": "info | warning | critical",
        "confidence": 0.8,
        "false_alarm": "true | false | null (если не про тревогу)",
        "evidence": [{"node": "climate_sensor_02", "param": "co2_ppm", "value": 781, "note": "почему важно"}],
        "actions": [{
            "node_id": "dome_automation_01", "action": "set_data_trust",
            "value": {"node": "climate_sensor_01", "param": "co2_ppm", "trust": "UNRELIABLE"},
            "reason": "зачем", "expected": "что проверить в телеметрии после команды",
            "verify_after_s": 8, "priority": 1,
        }],
        "operator_message": {"needed": False, "text": "", "to": "operator"},
        "wait_and_watch": ["что отслеживать дальше"],
        "rag_refs": ["id использованных документов базы знаний"],
        "risks": ["что может пойти не так"],
    }
