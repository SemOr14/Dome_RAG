"""DomeLLMAgent: собрать промпт → вызвать DeepSeek → разобрать JSON → проверить действия."""

from __future__ import annotations

import json
import re
import sys
import uuid

from pydantic import ValidationError

from .config import Settings
from .context.catalog import NodeCatalog
from .context.graph import InfluenceGraph
from .llm_client import DeepSeekClient
from .prompt_builder import BuiltPrompt, PromptBuilder
from .rag.base import NullRetriever, Retriever
from .rag.dome import DomeRagRetriever
from .rag.keyword import KeywordRetriever
from .schemas import AgentDecision, AgentResponse, LLMCallInfo, ProposedAction
from .storage.base import (CatalogRepo, ControlChecker, DecisionJournalRepo, EventSink, NullEventSink,
                           TelemetryRepo)
from .storage.data_api import (ApiCatalogRepo, ApiControlChecker, ApiDecisionJournalRepo, ApiEventSink,
                               ApiTelemetryRepo, DataApiClient)
from .storage.file_store import FileCatalogRepo, FileDecisionJournalRepo, FileTelemetryRepo
from .validate import validate_actions

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_decision(text: str) -> AgentDecision:
    """JSON из ответа модели; терпим markdown-обёртку и текст вокруг объекта."""
    cleaned = _FENCE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("в ответе нет JSON-объекта")
    return AgentDecision.model_validate(json.loads(cleaned[start:end + 1]))


def make_retriever(settings: Settings) -> Retriever:
    """RAG Купола из rag/; если его нет или индекс не собирается — запасной BM25 по data/rag/."""
    backend = settings.rag_backend
    if backend in ("auto", "dome") and (settings.rag_dir / "index" / "metadata.json").is_file():
        try:
            return DomeRagRetriever(settings.rag_dir, semantic=settings.rag_semantic)
        except (ImportError, OSError, ValueError, KeyError) as exc:
            if backend == "dome":
                raise
            print(f"RAG Купола недоступен ({type(exc).__name__}: {exc}), используется data/rag", file=sys.stderr)
    elif backend == "dome":
        raise FileNotFoundError(f"нет индекса RAG: {settings.rag_dir / 'index'}")
    keyword_dir = settings.data_dir / "rag"
    if backend in ("auto", "keyword") and keyword_dir.is_dir():
        return KeywordRetriever(keyword_dir)
    return NullRetriever()


class DomeLLMAgent:
    def __init__(self, settings: Settings, telemetry: TelemetryRepo, journal: DecisionJournalRepo,
                 catalog_repo: CatalogRepo, retriever: Retriever | None = None, llm: DeepSeekClient | None = None,
                 events: EventSink | None = None, control_checker: ControlChecker | None = None):
        self.settings = settings
        self.telemetry = telemetry
        self.journal = journal
        self.events = events or NullEventSink()
        self.control_checker = control_checker
        self.catalog = NodeCatalog(catalog_repo.nodes())
        self.hard_rules = PromptBuilder.load_rules(settings.data_dir / "hard_rules.yaml")
        self.builder = PromptBuilder(
            settings.prompt_dir, self.catalog, InfluenceGraph.load(settings.data_dir / "influence_graph.yaml"),
            self.hard_rules, retriever or NullRetriever(), journal_limit=settings.journal_limit,
            rag_k=settings.rag_k, graph_depth=settings.graph_depth)
        self._llm = llm

    @classmethod
    def from_files(cls, settings: Settings, snapshot_file=None, journal_file=None) -> "DomeLLMAgent":
        """Сборка на файловых заглушках из data/ (пока нет БД)."""
        data = settings.data_dir
        return cls(
            settings,
            telemetry=FileTelemetryRepo(snapshot_file or data / "samples" / "telemetry_latest.json"),
            journal=FileDecisionJournalRepo(journal_file or data / "samples" / "decision_journal.jsonl"),
            catalog_repo=FileCatalogRepo(data / "catalog_static.json"),
            retriever=make_retriever(settings))

    @classmethod
    def from_data_api(cls, settings: Settings, client: DataApiClient | None = None) -> "DomeLLMAgent":
        """Сборка на сервисе БД/API: телеметрия, каталог и журнал запуска; весь обмен с LLM пишется в БД."""
        client = client or DataApiClient(settings.data_api_url, settings.data_api_token,
                                         settings.data_api_timeout_s, settings.state_dir)
        run_id = settings.data_api_run_id or client.latest_run_id()
        if not run_id:
            raise RuntimeError("В БД нет ни одного запуска: задайте DATA_API_RUN_ID или запишите телеметрию")
        session_id = settings.data_api_session_id
        if session_id is None:
            auth = client.last_event(run_id, "auth_response")
            session_id = auth.get("session_id") if auth else None
        sink = ApiEventSink(client, run_id, session_id)
        checker = (ApiControlChecker(client, run_id, session_id)
                   if settings.data_api_check_controls and session_id else None)
        return cls(
            settings,
            telemetry=ApiTelemetryRepo(client, run_id),
            journal=ApiDecisionJournalRepo(client, run_id, sink),
            catalog_repo=ApiCatalogRepo(client, run_id,
                                        fallback=FileCatalogRepo(settings.data_dir / "catalog_static.json")),
            retriever=make_retriever(settings), events=sink, control_checker=checker)

    @classmethod
    def from_env(cls, settings: Settings) -> "DomeLLMAgent":
        """БД/API, если задан DATA_API_URL, иначе файловые заглушки data/."""
        return cls.from_data_api(settings) if settings.data_api_url else cls.from_files(settings)

    @property
    def llm(self) -> DeepSeekClient:
        if self._llm is None:
            self._llm = DeepSeekClient(self.settings)
        return self._llm

    def build_prompt(self, trigger: str, focus_nodes: set[str] | None = None) -> BuiltPrompt:
        return self.builder.build(trigger, self.telemetry.latest(), self.journal.recent(self.settings.journal_limit),
                                  focus_nodes)

    def decide(self, trigger: str, focus_nodes: set[str] | None = None, purpose: str = "") -> AgentResponse:
        prompt = self.build_prompt(trigger, focus_nodes)
        messages = list(prompt.messages)
        info = LLMCallInfo(model=self.settings.model, purpose=purpose or trigger[:120], ok=False)
        raw = None
        prompt_tokens = completion_tokens = latency = 0

        call_id = f"llm-{uuid.uuid4().hex[:12]}"
        for attempt in range(2):  # вторая попытка — только если модель вернула невалидный JSON
            request_id = f"{call_id}-{attempt + 1}"
            self.events.emit("llm_request", {
                "messages": messages, "model": self.settings.model,
                "parameters": {"temperature": self.settings.temperature, "max_tokens": self.settings.max_tokens,
                               "response_format": "json_object"},
                "response_schema": "AgentDecision", "purpose": info.purpose},
                channel="llm", direction="outbound", request_id=request_id)
            result = self.llm.chat(messages)
            info.attempts += result.attempts
            prompt_tokens += result.prompt_tokens or 0
            completion_tokens += result.completion_tokens or 0
            latency += result.latency_ms or 0
            info.model = result.model
            usage = {"prompt_tokens": result.prompt_tokens, "completion_tokens": result.completion_tokens,
                     "latency_ms": result.latency_ms, "attempts": result.attempts}
            if not result.ok:
                info.error = result.error
                self._llm_response(request_id, result.model, None, None, usage, result.error)
                break
            raw = result.text or ""
            try:
                decision = parse_decision(raw)
            except (ValueError, ValidationError) as exc:
                info.error = f"невалидный ответ: {exc}"[:500]
                self._llm_response(request_id, result.model, raw, None, usage, f"parse_error: {info.error}")
                messages += [{"role": "assistant", "content": raw},
                             {"role": "user", "content": f"Ответ не прошёл проверку: {info.error}. "
                                                         "Верни только один JSON-объект строго по формату."}]
                continue
            self._llm_response(request_id, result.model, raw, decision.model_dump(mode="json"), usage, None)
            info.ok, info.error = True, None
            info.prompt_tokens, info.completion_tokens, info.latency_ms = prompt_tokens, completion_tokens, latency
            verdicts = self._validate(decision, prompt)
            response = AgentResponse(ok=True, decision=decision, verdicts=verdicts, llm_call=info,
                                     raw_text=raw, rag_used=[d.id for d in prompt.rag_docs],
                                     decision_id=f"decision-{call_id}")
            response.decision_event_id = self.events.emit(
                "decision", self.decision_payload(response, prompt.dome_time),
                metadata={"llm_request_id": request_id})
            self._server_check(response)
            return response

        info.prompt_tokens, info.completion_tokens, info.latency_ms = prompt_tokens, completion_tokens, latency
        return AgentResponse(ok=False, llm_call=info, raw_text=raw, rag_used=[d.id for d in prompt.rag_docs])

    def _llm_response(self, request_id: str, model: str, raw, parsed, usage: dict, error) -> None:
        self.events.emit("llm_response", {"model": model, "raw_response": raw, "parsed_output": parsed,
                                          "usage": usage, "error": error},
                         source="llm", channel="llm", direction="inbound", request_id=request_id)

    def _server_check(self, response: AgentResponse) -> None:
        """Предпроверка сервиса БД (каталог сессии, свежесть состояния); её отказ отклоняет действие."""
        if self.control_checker is None:
            return
        meta = {"decision_id": response.decision_id, "decision_event_id": response.decision_event_id}
        for v in response.verdicts:
            v.server_check = self.control_checker.check(v.action.node_id, v.action.action, v.action.value, meta)
            if v.server_check["status"] == "rejected":
                v.accepted = False
                v.reasons += [f"БД/API: {r}" for r in v.server_check["reasons"]]

    def _validate(self, decision: AgentDecision, prompt: BuiltPrompt):
        actions = [a for a in decision.actions if not (a.node_id == "meeting_01" and a.action == "post_message")]
        msg = decision.operator_message
        if msg.needed and msg.text.strip():
            # Сообщение Оператору проходит ту же проверку, что и команды
            actions.append(ProposedAction(node_id="meeting_01", action="post_message",
                                          value={"text": msg.text, "to": msg.to}, reason="сообщение Оператору",
                                          expected="сообщение появилось в meeting_01", verify_after_s=2,
                                          priority=max([a.priority for a in actions], default=0) + 1))
        snapshot_nodes = self.telemetry.latest().get("nodes") or {}
        return validate_actions(actions, self.catalog, snapshot_nodes, prompt.journal, self.hard_rules)

    def decision_payload(self, response: AgentResponse, dome_time: str | None = None) -> dict:
        """Решение в формате services/data_api/AGENT_INTERFACE.md плюс text для журнала промпта."""
        d = response.decision
        assert d is not None
        accepted = [f"{a.node_id}.{a.action}" for a in response.accepted_actions]
        return {
            "actor": "agent", "kind": "decision", "dome_time": dome_time, "source": "llm",
            "text": f"{d.situation}; причина: {d.cause}; план: {', '.join(accepted) or 'наблюдать'}",
            "decision_id": response.decision_id,
            "observation_event_ids": list(getattr(self.telemetry, "event_ids", [])),
            "summary": d.situation,
            "hypotheses": [{"description": d.cause, "confidence": d.confidence}] if d.cause else [],
            "impact_kind": d.impact_kind, "authenticity": d.authenticity, "causal_chain": d.causal_chain,
            "false_alarm": d.false_alarm, "severity": d.severity, "confidence": d.confidence,
            "proposed_controls": [{"node_id": v.action.node_id, "action": v.action.action, "value": v.action.value,
                                   "accepted": v.accepted, "reasons": v.reasons} for v in response.verdicts],
            "needs_operator": d.operator_message.needed,
            "verification_plan": [{"node_id": a.node_id, "expected": a.expected, "timeout_s": a.verify_after_s}
                                  for a in response.accepted_actions if a.expected],
            "rag_refs": d.rag_refs,
            "status": "proposed",
        }

    def record(self, response: AgentResponse, dome_time: str | None = None) -> None:
        """Кладёт решение в журнал решений, чтобы следующий промпт его видел."""
        if not response.ok or not response.decision:
            self.journal.append({"actor": "agent", "kind": "decision", "dome_time": dome_time, "status": "failed",
                                 "text": f"LLM недоступна: {response.llm_call.error}"})
            return
        self.journal.append({**self.decision_payload(response, dome_time),
                             "decision_event_id": response.decision_event_id})
