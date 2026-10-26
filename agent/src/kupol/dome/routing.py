"""Single-event arbitration: deterministic analysis, permitted playbooks, LLM, human."""
from dataclasses import dataclass, field
import traceback

from kupol.dome.playbooks.base import Ctx


@dataclass
class RouteResult:
    intents: list = field(default_factory=list)
    covered_nodes: set = field(default_factory=set)
    summary: list = field(default_factory=list)


def route_playbooks(runner, now, chat_new):
    result = RouteResult()
    if runner.state.role != 'team':
        return result
    for pb in runner.playbooks:
        # Pure evaluation is allowed when disabled; no ordinary automatic action is returned.
        if not runner.policy.algorithms_enabled:
            continue
        ctx = Ctx(runner.state, runner.cfg, runner.gate, runner.mem.setdefault(pb.name, {}),
                  now, runner._operator_texts, chat_new)
        try:
            intents = pb.step(ctx)
        except Exception as exc:
            runner.log.write('error', where='playbook:' + pb.name, error=repr(exc), traceback=traceback.format_exc()[-1000:])
            result.summary.append({'playbook': pb.name, 'status': 'failed', 'reason': type(exc).__name__})
            continue
        for intent in intents:
            nodes = {e.get('node') for e in intent.evidence if e.get('node')}
            nodes |= {s.node for s in intent.steps}
            result.covered_nodes |= nodes
            result.summary.append({'playbook': pb.name, 'situation': intent.situation, 'nodes': sorted(nodes),
                                   'actions': [s.action for s in intent.steps], 'priority': intent.priority})
            result.intents.append(intent)
    result.intents.sort(key=lambda i: i.priority)
    return result
