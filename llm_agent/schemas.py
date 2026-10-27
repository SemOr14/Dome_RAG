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
    verify_after_s: float = Field(8.0, ge=1, le=300, allow_inf_nan=False)
    priority: int = Field(1, ge=1, le=9)

    @field_validator("value")
    @classmethod
    def _empty_is_none(cls, v: Any) -> Any:
        # Модель пишет {} / "" вместо «без значения»; сервер и Купол такое значение отклоняют
        return None if v in ({}, [], "") else v


REQUEST_STATUSES = ("answered", "executing", "already_done", "partial", "refused")


class OperatorMessage(BaseModel):
    needed: bool = False
    text: str = ""
    to: str = "operator"
    # Итог требования Оператора по решению модели; partial/refused помечаются «НЕ ВЫПОЛНЕНО» кодом рантайма
    request_status: str = ""

    @field_validator("text")
    @classmethod
    def _limit(cls, v: str) -> str:
        return v[:4000]

    @field_validator("request_status", mode="before")
    @classmethod
    def _status(cls, v: Any) -> str:
        v = str(v or "").strip().lower()
        return v if v in REQUEST_STATUSES else ""


class HumanEscalation(BaseModel):
    node_id: str = Field(min_length=1, max_length=200)
    incident: str = Field(min_length=1, max_length=400)
    severity: Literal['yellow', 'red'] = 'red'
    facts: list[Evidence] = Field(default_factory=list)
    already_done: list[str] = Field(default_factory=list)
    required_action: str = Field(min_length=1, max_length=1200)


class Suspicion(BaseModel):
    """Подозрительное наблюдение «на будущее»: не повод действовать сейчас, а то, к чему стоит вернуться."""
    text: str = Field(min_length=1, max_length=500)
    node_id: str | None = Field(None, max_length=200)
    param: str | None = Field(None, max_length=200)
    why: str = Field("", max_length=500, description="почему это подозрительно")
    check: str = Field("", max_length=300, description="что проверить позже, чтобы подтвердить или снять подозрение")


class AgentDecision(BaseModel):
    situation: str
    cause: str = ""
    severity: Literal["info", "warning", "critical"] = "info"
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    impact_kind: Literal["base", "hidden", "cascade", "false", "unknown"] = "unknown"
    authenticity: Literal["real", "suspected_false", "unknown"] = "unknown"
    causal_chain: list[str] = []
    false_alarm: bool | None = None
    evidence: list[Evidence] = []
    actions: list[ProposedAction] = []
    operator_message: OperatorMessage = OperatorMessage()
    request_human: HumanEscalation | None = None
    suspicion: Suspicion | None = None  # не более одного за решение
    wait_and_watch: list[str] = []
    rag_refs: list[str] = []
    risks: list[str] = []

    @field_validator("impact_kind", "authenticity", mode="before")
    @classmethod
    def _label(cls, v: Any, info: Any) -> str:
        # Неизвестная метка — «не определено», а не повод отбросить всё решение
        allowed = ("base", "hidden", "cascade", "false") if info.field_name == "impact_kind" else ("real", "suspected_false")
        v = str(v or "").strip().lower()
        return v if v in allowed else "unknown"

    @field_validator("suspicion", mode="before")
    @classmethod
    def _one_suspicion(cls, v: Any) -> Any:
        # Не более одного за решение; пустое или негодное подозрение не должно ронять всё решение
        if isinstance(v, (list, tuple)):
            v = v[0] if v else None
        if isinstance(v, str):
            v = {"text": v}
        if not isinstance(v, dict):
            return None
        return v if str(v.get("text") or "").strip() else None

    @field_validator("causal_chain", mode="before")
    @classmethod
    def _chain(cls, v: Any) -> list[str]:
        items = [v] if isinstance(v, str) else list(v or [])
        return [part.strip() for item in items for part in str(item).split("→") if part.strip()]


class ActionVerdict(BaseModel):
    action: ProposedAction
    accepted: bool
    reasons: list[str] = []
    server_check: dict | None = None  # POST /v1/controls/validate: status, reasons, request_id, intent_event_id


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
    decision_id: str | None = None
    sensor_reads: list[dict] = []
    decision_event_id: str | None = None  # событие decision в БД (если подключена)
    classification_issues: list[str] = []  # противоречия в разметке impact_kind/authenticity (validate.check_classification)

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
        "severity": "info",
        "confidence": 0.8,
        "impact_kind": "base | hidden | cascade | false | unknown",
        "authenticity": "real | suspected_false | unknown",
        "causal_chain": ["первопричина", "автоматика или защита", "остановленное оборудование (только подтверждённые звенья)"],
        "false_alarm": None,
        "evidence": [{"node": "climate_sensor_02", "param": "co2_ppm", "value": 781, "note": "почему важно"}],
        "actions": [{
            "node_id": "dome_automation_01", "action": "set_data_trust",
            "value": {"node": "climate_sensor_01", "param": "co2_ppm", "trust": "UNRELIABLE"},
            "reason": "зачем", "expected": "что проверить в телеметрии после команды",
            "verify_after_s": 8, "priority": 1,
        }],
        "operator_message": {"needed": False, "text": "", "to": "operator",
                             "request_status": "только на требование Оператора: answered | executing | already_done | "
                                               "partial | refused; иначе пусто"},
        "request_human": None,
        "suspicion": None,  # или {"text": "что подозрительно", "node_id": "...", "param": "...", "why": "...", "check": "что проверить позже"}
        "wait_and_watch": ["что отслеживать дальше"],
        "rag_refs": ["id использованных документов базы знаний"],
        "risks": ["что может пойти не так"],
    }
