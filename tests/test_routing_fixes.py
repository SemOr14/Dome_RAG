"""Offline regressions for single-file commands and explicit algorithm/LLM/human handoff."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from integration.server import create_app  # installs project import paths
from kupol.dome.agentlog import AgentLog
from kupol.dome.brain import LlmBrain
from kupol.dome.config import load_dome_config
from kupol.dome.control import ControlPolicy
from kupol.dome.env import Settings
from kupol.dome.playbooks.base import Ctx
from kupol.dome.playbooks.tasks import TasksPlaybook
from kupol.dome.routing import route_playbooks
from kupol.dome.state import DomeState
from llm_agent import Settings as LlmSettings
from llm_agent.llm_client import LLMResult
from llm_agent.schemas import AgentDecision, AgentResponse, ActionVerdict, ProposedAction, LLMCallInfo
from test_production_control import frame


def op(mid,text):return {'id':mid,'sender':{'role':'operator'},'text':text}
def commands(out):return [(s.node,s.action,s.value) for i in out for s in i.steps]


def task_ctx(mem=None, chat=None):
    st=DomeState();frame(st,100,state='IDLE')
    st.nodes['cnc_01'].update(homed=True,files=['a.nc','b.nc'],progress_pct=0)
    st.nodes['printer_3d_01'].update(files=['a.gcode','b.gcode'])
    return Ctx(st,load_dome_config('agent/dome.yaml'),SimpleNamespace(is_pending=lambda *a:False),
               {} if mem is None else mem,100,chat_new=chat or [])


def test_busy_request_is_refused_and_never_starts_later():
    ctx=task_ctx(chat=[op(1,'Запусти a.nc')]);ctx.state.nodes['cnc_01']['state']='RUNNING'
    out=TasksPlaybook().step(ctx)
    assert not commands(out) and 'не поставлено в очередь' in out[0].say
    ctx.chat_new=[];ctx.state.nodes['cnc_01']['state']='IDLE'
    assert not commands(TasksPlaybook().step(ctx))
    ctx.chat_new=[op(2,'Заготовка закреплена, запускай')]
    assert not commands(TasksPlaybook().step(ctx))
    ctx.chat_new=[op(3,'Запусти a.nc')]
    assert commands(TasksPlaybook().step(ctx))==[('cnc_01','start_job','a.nc')]


def test_occupied_bed_and_missing_home_require_new_named_request():
    for field,value in [('bed_clear',False),('homed',False)]:
        ctx=task_ctx(chat=[op(1,'Напечатай a.gcode')]);ctx.state.nodes['printer_3d_01'][field]=value
        assert not commands(TasksPlaybook().step(ctx))
        ctx.state.nodes['printer_3d_01'][field]=True;ctx.chat_new=[]
        assert not commands(TasksPlaybook().step(ctx))
        ctx.chat_new=[op(2,'Напечатай a.gcode')]
        assert commands(TasksPlaybook().step(ctx))==[('printer_3d_01','start_job','a.gcode')]


def test_legacy_pending_jobs_are_discarded_without_replay():
    ctx=task_ctx(mem={'jobs':[{'machine':'cnc_01','file':'a.nc','state':'queued'},
        {'machine':'printer_3d_01','file':'a.gcode','state':'waiting_confirm'}]})
    assert not commands(TasksPlaybook().step(ctx)) and 'jobs' not in ctx.mem
    assert not ctx.mem['active_jobs']
    ctx.chat_new=[op(2,'Заготовку закрепил, запускай')]
    assert not commands(TasksPlaybook().step(ctx))


def test_multiple_files_and_burst_do_not_create_an_automatic_next_job():
    ctx=task_ctx(chat=[op(1,'Запусти a.nc и b.nc')])
    assert not commands(TasksPlaybook().step(ctx)) and not ctx.mem['active_jobs']
    ctx.chat_new=[op(2,'Запусти a.nc'),op(3,'Запусти b.nc')]
    assert commands(TasksPlaybook().step(ctx))==[('cnc_01','start_job','a.nc')]
    ctx.chat_new=[];ctx.state.nodes['cnc_01'].update(file_name='a.nc',state='IDLE',progress_pct=100)
    assert not commands(TasksPlaybook().step(ctx))


class FakeLLM:
    def __init__(self,answer=None):self.calls=0;self.answer=answer
    def chat(self,*a,**kw):
        self.calls+=1
        if self.answer=='invalid':return LLMResult(ok=True,text='invalid JSON',model='fake',attempts=1)
        return LLMResult(ok=True,text=json.dumps(self.answer or {'situation':'Нет допустимого исправления','actions':[]}),model='fake',attempts=1)


@pytest.fixture
def brain(tmp_path):
    st=DomeState();frame(st,100,state='IDLE')
    log=AgentLog(tmp_path/'logs');llm=FakeLLM()
    settings=Settings('ws://127.0.0.1:1','offline','',state_dir=str(tmp_path))
    b=LlmBrain(settings,log,st,lambda:{'timestamp':100},lambda:'offline',llm=llm,
        llm_settings=LlmSettings(deepseek_api_key=None,rag_backend='none',state_dir=tmp_path/'brain',trigger_review_s=0,trigger_min_interval_s=0),clock=lambda:100)
    b.policy=ControlPolicy('autonomous',True,'service','full',1)
    b.humans=[];b.on_human=b.humans.append
    yield b,llm,st
    log.close()


def action(seq=1,actor='algorithm',error='Купол отказал'):
    return {'seq':seq,'run_id':'offline','actor':actor,'policy_epoch':1,'ref':1,
            'request':{'node_id':'cnc_01','action':'start_job','value':'a.nc'},'response':{'success':False,'error':error}}


@pytest.mark.parametrize('error',['Купол отказал','control: нет ответа за 15 с'])
def test_gate_outcome_is_explicit_llm_trigger_despite_covered_node(brain,error):
    b,llm,st=brain
    b.on_log('action',action(error=error))
    fired=b.dispatcher.poll(covered_nodes={'cnc_01'})
    assert fired and any(t.kind=='command' for t in fired.triggers) and llm.calls==0
    async def execute(intent):pass
    asyncio.run(b._call(fired,execute))
    assert llm.calls==1 and b.humans and not b.dispatcher.checks
    b.on_log('action',action(error=error))
    assert not b.dispatcher.pending  # same outcome is not retried every tick


def test_gate_local_rejection_is_reviewed_once_not_once_per_log_seq(brain):
    b,llm,_=brain
    for seq in (1,2,3):
        rec=action(seq=seq,error='Gate: нет подтверждённых координат');rec['rejected']=True
        b.on_log('action',rec)
    assert len(b.dispatcher.pending)==1


def test_gate_no_effect_and_late_timeout_resolution_are_forwarded(brain):
    b,_,_=brain
    b.on_log('action',action(error='нет ответа'))
    b.dispatcher.pending.clear()
    b.on_log('verification',{'ref':1,'ok':False,'run_id':'offline','expected':'RUNNING','observed':'IDLE'})
    assert any('no_effect' in k for k in b.dispatcher.pending)
    b.on_log('action',action(seq=2,error='нет ответа'))
    b.on_log('verification',{'ref':2,'ok':True,'run_id':'offline','observed':'RUNNING'})
    assert any('resolved' in k for k in b.dispatcher.pending)


@pytest.mark.parametrize('mode,actor',[('off','algorithm'),('autonomous','agent')])
def test_ai_off_or_failed_ai_action_goes_to_human_without_llm_loop(brain,mode,actor):
    b,llm,_=brain;b.policy.agent_mode=mode
    b.on_log('action',action(actor=actor))
    assert len(b.humans)==1 and not b.dispatcher.pending and llm.calls==0


def test_stale_epoch_and_chat_failures_do_not_enqueue_analysis(brain):
    b,_,_=brain
    rec=action();rec['policy_epoch']=0;b.on_log('action',rec)
    rec=action(seq=2);rec['request']['node_id']='meeting_01';b.on_log('action',rec)
    assert not b.dispatcher.pending and not b.humans


def test_invalid_analysis_response_escalates_once_without_model_retry(brain):
    b,llm,_=brain;llm.answer='invalid'
    b.playbook_failed('broken','ValueError')
    b.playbook_failed('broken','ValueError')
    fired=b.dispatcher.poll()
    async def execute(intent):pass
    asyncio.run(b._call(fired,execute))
    assert b.humans and not b.dispatcher.pending


def test_playbook_exception_is_returned_for_handoff(brain):
    b,_,st=brain
    class Broken:
        name='broken'
        def step(self,ctx):raise ValueError('bad state')
    r=SimpleNamespace(state=st,policy=b.policy,playbooks=[Broken()],cfg=load_dome_config('agent/dome.yaml'),
        gate=SimpleNamespace(),mem={},_operator_texts=lambda *a:[],log=b.log)
    routed=route_playbooks(r,100,[])
    assert routed.failures==[{'playbook':'broken','error':'ValueError'}]
    for f in routed.failures:b.playbook_failed(f['playbook'],f['error'])
    assert b.dispatcher.pending


def test_handled_message_id_does_not_hide_earlier_unhandled_request(brain):
    b,llm,st=brain
    messages=[op(1,'Почему упал нестандартный датчик?'),op(2,'Статус систем?')]
    st.nodes['meeting_01']={'online':True,'recent_messages':messages}
    fired=b.dispatcher.poll(messages,handled_operator_ids={2})
    assert fired
    ids=[t.key for t in fired.triggers if t.kind=='operator']
    assert ids==['operator|1'] and b.dispatcher.operator.last_id==2


def test_runner_gate_owns_verification_not_the_model_timer(brain):
    from llm_agent.dispatcher import Fired
    b,_,_=brain
    response=AgentResponse(ok=True,decision=AgentDecision(situation='Предложение'),decision_id='one',
        verdicts=[ActionVerdict(action=ProposedAction(node_id='cnc_01',action='start_job',value='a.nc'),accepted=True)],
        llm_call=LLMCallInfo(model='fake',ok=True))
    b.dispatcher.finish(Fired([], 'offline', snapshot={}),response)
    assert not b.dispatcher.checks


def test_confirmed_timeout_resolution_does_not_call_human_again(brain):
    b,llm,_=brain
    b.on_log('action',action(error='нет ответа'))
    b.dispatcher.pending.clear()
    b.on_log('verification',{'ref':1,'ok':True,'run_id':'offline','observed':'RUNNING'})
    fired=b.dispatcher.poll()
    assert fired and all(t.kind=='verification' for t in fired.triggers)
    async def execute(intent):pass
    asyncio.run(b._call(fired,execute))
    assert llm.calls==1 and not b.humans


def test_restart_history_is_not_replayed_but_new_live_message_is_kept(brain,tmp_path):
    from kupol.dome.runner import Runner
    b,_,st=brain
    cfg=load_dome_config('agent/dome.yaml');cfg.runtime.state_file=str(tmp_path/'runner.json')
    async def send(*a):raise AssertionError('No equipment commands in chat collection')
    client=SimpleNamespace(recorder=None,control=send,chat=asyncio.Queue(),session_id='offline')
    r=Runner(Settings('ws://127.0.0.1:1','offline',''),cfg,b.log,client=client,
        playbooks=[],brain=b,clock=lambda:100,policy=b.policy)
    old,new=op(1,'Запусти a.nc'),op(2,'Запусти b.nc')
    r.state.nodes['meeting_01']={'recent_messages':[old,new]}
    st.nodes['meeting_01']={'recent_messages':[old,new]}
    client.chat.put_nowait({'message':new})
    messages=r._collect_chat(100)
    assert messages==[new] and r._history_message_ids=={1}
    assert r._collect_chat(101)==[]
    fired=b.dispatcher.poll(messages,handled_operator_ids=r._history_message_ids)
    assert [t.key for t in fired.triggers if t.kind=='operator']==['operator|2']


def test_runner_human_handoff_uses_incident_and_ignores_old_epoch(brain,tmp_path):
    from kupol.dome.runner import Runner
    b,_,_=brain
    cfg=load_dome_config('agent/dome.yaml');cfg.runtime.state_file=str(tmp_path/'runner.json')
    async def send(*a):raise AssertionError('No equipment commands in handoff')
    client=SimpleNamespace(recorder=None,control=send,chat=asyncio.Queue(),session_id='offline')
    r=Runner(Settings('ws://127.0.0.1:1','offline',''),cfg,b.log,client=client,
        playbooks=[],brain=b,clock=lambda:100,policy=b.policy)
    b.on_log('action',action(actor='agent'))
    item=r.incidents.items[r.incidents.active['action:cnc_01:start_job']]
    assert item.severity=='red' and item.required_action and not b.dispatcher.pending
    r.policy.policy_epoch+=1
    b.on_human(SimpleNamespace(key='old',nodes={'cnc_01'},text='stale',
        meta={'policy_epoch':1,'node_id':'cnc_01','action':'resume'}))
    assert 'action:cnc_01:resume' not in r.incidents.active


def test_recommendation_after_algorithm_failure_requires_human(brain):
    b,_,_=brain;b.policy.agent_mode='recommendation'
    b.playbook_failed('broken','ValueError')
    fired=b.dispatcher.poll()
    response=AgentResponse(ok=True,decision=AgentDecision(situation='Предложение'),decision_id='one',
        verdicts=[ActionVerdict(action=ProposedAction(node_id='cnc_01',action='start_job',value='a.nc'),accepted=True)],
        llm_call=LLMCallInfo(model='fake',ok=True))
    b.dispatcher.decide=lambda _:response
    proposals=[]
    async def execute(intent):proposals.append(intent)
    asyncio.run(b._call(fired,execute))
    assert proposals[0].steps and b.humans and not b.dispatcher.pending
