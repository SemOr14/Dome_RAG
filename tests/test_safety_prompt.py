"""Verify mandatory model instructions independent of RAG and diagnostic triggers."""
from test_dispatcher import make
from llm_agent.patterns import DiagnosticPatterns
from test_diagnostic_patterns import bodies, trigger, tick


def test_model_always_receives_safety_policy_even_without_rag(tmp_path):
    d,_,_=make(tmp_path,{'nodes':bodies(),'timestamp':100})
    prompt=d.agent.builder.build('Обычное наблюдение',{'nodes':bodies(),'timestamp':100},[])
    system=prompt.messages[0]['content']
    assert 'Политика безопасного управления' in system
    assert 'Заряд батареи не доказывает' in system
    assert 'не является закрытием инцидента' in system
    assert 'Разные node_id не доказывают независимость' in system
    assert 'источники' in system and prompt.rag_docs == []


def test_stabilized_observations_never_assert_repair_or_incident_closure():
    e=DiagnosticPatterns(hold_s=0)
    tick(e,bodies(),[trigger()],['case'])
    for t in (110,120,130):
        transitions=tick(e,bodies(t),t=t)
    plan=next(iter(e.cases.values())).plan
    assert plan['status']=='stabilized'
    assert plan['incident_closed'] is False and plan['root_cause_status']=='unverified'
    assert plan['residual_risks'] and 'последующих действий' in plan['recovery']
    assert transitions[0]['status']=='stabilized'
