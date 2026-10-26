from __future__ import annotations
from .common import log_event
from .context import build_context, local_answer
from .llm import DeepSeekError, generate
from .search import SearchEngine


class RAG:
    def __init__(self, semantic=True):
        self.engine = SearchEngine(semantic=semantic)

    def answer(self, question, *, observation=None, use_llm=False, max_chars=None):
        context, results = build_context(self.engine, question, observation, max_chars)
        answer = local_answer(context, results)
        answer['context_budget'] = context['budget']
        answer['semantic'] = dict(enabled=self.engine.semantic,
                                  fallback_error=self.engine.last_semantic_error)
        if use_llm and answer['answer_status'] not in {'refused','insufficient_evidence'}:
            try:
                answer['llm'] = generate(context)
                answer['mode'] = 'rag_with_deepseek_proposal'
            except DeepSeekError as exc:
                answer['llm'] = dict(status='unavailable_local_fallback', error=str(exc))
        log_event('rag_answer',mode=answer['mode'],answer_status=answer['answer_status'],
                  used_record_ids=answer['used_record_ids'],context_chars=context['budget']['used_chars'],
                  equipment_commands_sent=0)
        return answer
