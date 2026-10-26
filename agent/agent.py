"""DomeLLMAgent: собрать промпт → вызвать DeepSeek → разобрать JSON → проверить действия."""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from .config import Settings
from .context.catalog import NodeCatalog
from .context.graph import InfluenceGraph
from .llm_client import DeepSeekClient
from .prompt_builder import BuiltPrompt, PromptBuilder
from .rag.base import NullRetriever, Retriever
from .rag.keyword import KeywordRetriever
from .schemas import AgentDecision, AgentResponse, LLMCallInfo, ProposedAction
from .storage.base import CatalogRepo, DecisionJournalRepo, TelemetryRepo
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


class DomeLLMAgent:
    def __init__(self, settings: Settings, telemetry: TelemetryRepo, journal: DecisionJournalRepo,
                 catalog_repo: CatalogRepo, retriever: Retriever | None = None, llm: DeepSeekClient | None = None):
        self.settings = settings
        self.telemetry = telemetry
        self.journal = journal
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
        rag_dir = data / "rag"
        return cls(
            settings,
            telemetry=FileTelemetryRepo(snapshot_file or data / "samples" / "telemetry_latest.json"),
            journal=FileDecisionJournalRepo(journal_file or data / "samples" / "decision_journal.jsonl"),
            catalog_repo=FileCatalogRepo(data / "catalog_static.json"),
            retriever=KeywordRetriever(rag_dir) if rag_dir.is_dir() else NullRetriever())

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

        for attempt in range(2):  # вторая попытка — только если модель вернула невалидный JSON
            result = self.llm.chat(messages)
            info.attempts += result.attempts
            prompt_tokens += result.prompt_tokens or 0
            completion_tokens += result.completion_tokens or 0
            latency += result.latency_ms or 0
            info.model = result.model
            if not result.ok:
                info.error = result.error
                break
            raw = result.text or ""
            try:
                decision = parse_decision(raw)
            except (ValueError, ValidationError) as exc:
                info.error = f"невалидный ответ: {exc}"[:500]
                messages += [{"role": "assistant", "content": raw},
                             {"role": "user", "content": f"Ответ не прошёл проверку: {info.error}. "
                                                         "Верни только один JSON-объект строго по формату."}]
                continue
            info.ok, info.error = True, None
            info.prompt_tokens, info.completion_tokens, info.latency_ms = prompt_tokens, completion_tokens, latency
            verdicts = self._validate(decision, prompt)
            return AgentResponse(ok=True, decision=decision, verdicts=verdicts, llm_call=info,
                                 raw_text=raw, rag_used=[d.id for d in prompt.rag_docs])

        info.prompt_tokens, info.completion_tokens, info.latency_ms = prompt_tokens, completion_tokens, latency
        return AgentResponse(ok=False, llm_call=info, raw_text=raw, rag_used=[d.id for d in prompt.rag_docs])

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

    def record(self, response: AgentResponse, dome_time: str | None = None) -> None:
        """Кладёт решение в журнал решений, чтобы следующий промпт его видел."""
        if not response.ok or not response.decision:
            self.journal.append({"actor": "agent", "kind": "decision", "dome_time": dome_time,
                                 "text": f"LLM недоступна: {response.llm_call.error}"})
            return
        d = response.decision
        accepted = [f"{a.node_id}.{a.action}" for a in response.accepted_actions]
        self.journal.append({"actor": "agent", "kind": "decision", "dome_time": dome_time,
                             "text": f"{d.situation}; причина: {d.cause}; план: {', '.join(accepted) or 'наблюдать'}",
                             "confidence": d.confidence, "source": "llm"})
