"""Focused offline acceptance checks. No organizer connection or model is used."""
import asyncio
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from integration.server import create_app
from integration.production import ProductionAction, ProductionTracker, validate_upload
from kupol.dome.control import ControlPolicy
from kupol.dome.catalog import Catalog
from kupol.dome.config import load_dome_config
from kupol.dome.agentlog import AgentLog
from kupol.dome.gate import Cmd, Gate, Expect
from kupol.dome.state import DomeState
from kupol.dome.incidents import IncidentCenter
from kupol.dome.printer_supervisor import PrinterSupervisor
from kupol.dome.routing import route_playbooks
from kupol.dome.playbooks.base import Intent


def run(coro):
    return asyncio.run(coro)


def frame(st, stamp, state='PRINTING', reason=None, progress=20, elapsed=100, power='ON'):
    nodes = {
        'printer_3d_01': {'online': True, 'status': 'OK', 'updated_at': stamp, 'state': state, 'real_state': state,
            'paused': state == 'PAUSED', 'file_name': 'part.gcode', 'files': ['part.gcode'], 'homed': True,
            'progress_pct': progress, 'print_elapsed_s': elapsed, 'nozzle_temp_c': 200, 'bed_temp_c': 60,
            'hold_reason': reason, 'hold_level': 'WARNING' if reason else None, 'resume_attempts': 0, 'bed_clear': True},
        'cnc_01': {'online': True, 'updated_at': stamp, 'state': 'IDLE', 'spindle_rpm': 0},
        'smart_panel_01': {'online': True, 'updated_at': stamp, 'lines': [{'line': 3, 'state': power}]},
        'dome_automation_01': {'online': True, 'updated_at': stamp, 'interlocks': {'IL_CO2': {'state': 'ARMED', 'pending_s': 0}}},
    }
    for i in (1, 2, 3):
        nodes[f'climate_sensor_0{i}'] = {'online': True, 'status': 'OK', 'updated_at': stamp, 'co2_ppm': 800, 'reading_delay_s': 0}
    st.update({'nodes': nodes}, 'team', stamp)


@pytest.fixture
def setup_gate(tmp_path):
    cfg = load_dome_config('agent/dome.yaml')
    st = DomeState(); frame(st, 100)
    messages = []
    async def send(*args):
        messages.append(args); return {'success': True}
    log = AgentLog(tmp_path / 'logs')
    catalog = Catalog.from_response({'nodes': [{'node_id': 'printer_3d_01', 'controls': ['resume', 'start_job'], 'queries': ['list_files']},
        {'node_id': 'smart_panel_01', 'controls': ['line_on', 'line_off']}]})
    gate = Gate(cfg, st, lambda: catalog, log, send, can_control=True, clock=lambda: 100)
    gate.max_telemetry_age_s = 20
    gate.policy = ControlPolicy('autonomous', True, 'service', 'full', 1)
    yield gate, st, cfg, messages
    log.close()


def test_modes_profile_and_epoch_are_independent():
    p = ControlPolicy('autonomous', True, 'service', 'full')
    p.change('off', True)
    assert p.reject('agent', p.policy_epoch, 'printer_3d_01', 'start_job')
    assert p.reject('algorithm', p.policy_epoch, 'printer_3d_01', 'start_job') is None
    p.change('off', False)
    assert p.reject('algorithm', p.policy_epoch, '', '')
    p.operating_state = 'manual_after_yellow'
    assert p.reject('operator', p.policy_epoch, 'smart_panel_01', 'line_on') is None
    p.change('recommendation', True)
    assert p.operating_state == 'normal'
    assert p.reject('operator', p.policy_epoch-1, '', '')


def test_confirmed_command_cannot_bypass_stale_gate(setup_gate):
    gate, st, _, sent = setup_gate
    st.wall = 1
    result = run(gate.submit(Cmd('smart_panel_01', 'line_off', 3, ref=1, confirmed=True, actor='operator')))
    assert result.status == 'rejected' and not sent


def test_gate_rechecks_policy_after_wait_before_socket_send(setup_gate):
    gate, _, _, sent = setup_gate
    from kupol.dome.control import SEND_GUARD
    from kupol.dome.client import PolicyRejected
    async def send(*args):
        gate.policy.change('off', False)
        bad = SEND_GUARD.get()()
        assert bad
        raise PolicyRejected(bad)
    gate._send = send
    result = run(gate.submit(Cmd('smart_panel_01', 'line_off', 3, ref=1)))
    assert result.status == 'rejected' and not gate.pending and not sent


def test_automatic_manual_pause_and_non_supervisor_resume_are_blocked(setup_gate):
    gate, st, _, sent = setup_gate
    frame(st, 100, state='PAUSED')
    assert run(gate.submit(Cmd('printer_3d_01', 'resume', ref=1))).status == 'rejected'
    frame(st, 100, state='PAUSED', reason='IL_CO2')
    assert run(gate.submit(Cmd('printer_3d_01', 'resume', ref=1))).status == 'rejected'
    assert not sent


def test_incident_dedup_upgrade_ack_and_replay():
    events=[]
    def emit(kind,payload,**kw): events.append({'event_type':kind,'payload':payload.copy()})
    c=IncidentCenter(emit)
    x=c.open('printer/episode', 'yellow', 'printer_3d_01', 'pause', 'inspect')
    assert len(list(c.pending_notifications()))==1
    c.open('printer/episode', 'yellow', 'printer_3d_01', 'pause', 'inspect')
    assert not list(c.pending_notifications())
    c.open('printer/episode', 'red', 'printer_3d_01', 'collision', 'inspect')
    assert len(list(c.pending_notifications()))==1
    c.acknowledge(x.incident_id)
    assert x.status=='acknowledged' and x.severity=='red'
    c.open('printer/episode', 'yellow', 'printer_3d_01', 'pause', 'inspect')
    assert x.severity=='red'
    replay=IncidentCenter(events=events)
    assert replay.items[x.incident_id].status=='acknowledged'
    assert not replay.items[x.incident_id].current


def test_server_modes_shared_by_two_clients_and_competition_denies_manual(tmp_path):
    app=create_app(tmp_path/'db.sqlite',token='')
    with TestClient(app) as a, TestClient(app) as b:
        old=b.get('/control/status').json()['policy_epoch']
        assert a.post('/control/mode',json={'agent_mode':'off','algorithms_enabled':False}).status_code==200
        status=b.get('/control/status').json()
        assert not status['algorithms_enabled'] and status['policy_epoch']>old
        assert b.post('/control/commands',json={'operation_id':str(uuid4()),'node_id':'smart_panel_01','action':'line_on','value':3,'confirmed':True}).status_code==403
        assert not app.state.hub.client
        assert a.post('/control/connect').status_code==409


def test_incident_rest_endpoint_persists_ack(tmp_path):
    path=tmp_path/'db.sqlite'
    app=create_app(path,token='')
    x=app.state.hub.incidents.open('smoke/1','red','smoke_detector_01','дым','проверить зону')
    with TestClient(app) as c:
        assert c.post(f'/control/incidents/{x.incident_id}/ack').json()['status']=='acknowledged'
    app2=create_app(path,token='')
    assert app2.state.hub.incidents.items[x.incident_id].status=='acknowledged'


@pytest.mark.parametrize('name', ['../x.gcode','dir\\x.gcode','x.stl','a\x00.gcode'])
def test_upload_rejects_paths_and_wrong_formats(name):
    with pytest.raises(HTTPException): validate_upload(name,'G28')


def test_upload_identity_and_active_file():
    digest,size=validate_upload('x.gcode','G28\n')
    assert len(digest)==64 and size==4
    with pytest.raises(HTTPException):validate_upload('x.gcode','G28','x.gcode')


def test_eta_and_false_webhook():
    st=DomeState(); tracker=ProductionTracker()
    for stamp, progress in [(100,10),(112,20),(124,30)]:
        frame(st,stamp,progress=progress,elapsed=stamp)
        st.nodes['printer_3d_01']['webhook_state']='COMPLETED'
        tracker.observe(st,stamp)
    job=tracker.jobs['printer_3d_01']
    assert job.state=='PRINTING' and job.eta_source=='estimate' and job.eta==pytest.approx(84)
    old=job.measured_at
    tracker.observe(st,136)
    assert job.measured_at==old


def supervisor_setup():
    st=DomeState()
    gate=SimpleNamespace(is_pending=lambda *a:False)
    runner=SimpleNamespace(state=st,cfg=load_dome_config('agent/dome.yaml'),mem={},gate=gate,
        policy=ControlPolicy('off',True,'service','full'))
    return st,runner,PrinterSupervisor(runner,IncidentCenter())


def test_supervisor_single_resume_after_new_clear_evidence():
    st,r,s=supervisor_setup()
    frame(st,100); assert s.step(100)==[]
    frame(st,112,state='PAUSED',reason='IL_CO2'); assert s.step(112)==[]
    frame(st,124,state='PAUSED',reason='IL_CO2'); out=s.step(124)
    assert len(out)==1 and out[0].steps[0].action=='resume'
    frame(st,136,state='PAUSED',reason='IL_CO2'); assert s.step(136)==[]


def test_supervisor_never_resumes_manual_or_power_loss():
    st,r,s=supervisor_setup()
    frame(st,100); s.step(100)
    frame(st,112,state='PAUSED'); assert not s.step(112)
    frame(st,124,state='PAUSED',reason='IL_CO2',power='OFF'); assert not s.step(124)
    frame(st,136,state='PAUSED',reason='IL_CO2'); assert not s.step(136)
    frame(st,148,state='PAUSED',reason='IL_CO2'); assert not s.step(148)
    assert r.mem['_printer_supervisor']['unsafe_restart']


def test_algorithms_off_prevents_playbooks():
    calls=[]
    pb=SimpleNamespace(name='test',step=lambda ctx:calls.append(True) or [])
    r=SimpleNamespace(state=SimpleNamespace(role='team'),policy=ControlPolicy('off',False),playbooks=[pb])
    assert route_playbooks(r,100,[]).intents==[] and not calls


def test_history_production_capabilities_are_disabled(tmp_path):
    with TestClient(create_app(tmp_path/'db.sqlite',token='')) as c:
        rows=c.get('/control/production?run_id=historical').json()['items']
        assert len(rows)==2 and all(not x['capabilities']['start'] for x in rows)
        assert c.get('/control/production/cnc_01/files?run_id=historical').status_code==409


def test_production_request_is_reserved_before_await_and_not_replayed(tmp_path):
    h=create_app(tmp_path/'db.sqlite',token='').state.hub
    h.policy=ControlPolicy('off',False,'service','full')
    catalog=Catalog.from_response({'nodes':[{'node_id':'printer_3d_01','controls':['start_job']}]})
    h.runner=SimpleNamespace(client=SimpleNamespace(connected=True,session_id='offline-test'),catalog=catalog)
    calls=[]
    async def command(**kw):calls.append(kw);return {'status':'sent'}
    h.command=command
    body=ProductionAction(operation_id=uuid4(),run_id=h.run_id,node_id='printer_3d_01',action='start_job',file_name='part.gcode')
    run(h.production.operate(body));run(h.production.operate(body))
    assert len(calls)==1
    body.file_name='other.gcode'
    with pytest.raises(HTTPException):run(h.production.operate(body))


def test_structured_human_request_is_backward_compatible():
    from llm_agent.schemas import AgentDecision
    old=AgentDecision(situation='норма')
    assert old.request_human is None
    new=AgentDecision(situation='филамент',request_human={'node_id':'printer_3d_01','incident':'FILAMENT_RUNOUT','required_action':'заменить катушку'})
    assert new.request_human.severity=='red'


def test_late_model_callback_cannot_act_after_mode_change(tmp_path):
    from kupol.dome.runner import Runner
    from kupol.dome.env import Settings
    cfg=load_dome_config('agent/dome.yaml');cfg.runtime.state_file=str(tmp_path/'state.json')
    frame_state=DomeState();frame(frame_state,100)
    sent=[]
    async def send(*a):sent.append(a);return {'success':True}
    class Brain:
        healthy=True;task=None
        dispatcher=SimpleNamespace(busy=False,pending={})
        settings=SimpleNamespace(trigger_review_s=120)
        def on_log(self,*a):pass
        def set_catalog(self,*a):pass
        def mark_seen(self,*a):pass
        def step(self,chat,execute,**kw):self.callback=execute
    brain=Brain()
    client=SimpleNamespace(recorder=None,control=send,telemetry={'data':{'nodes':frame_state.nodes}},role='team',
        session_id='offline-session',catalog_message={'nodes':[{'node_id':'smart_panel_01','controls':['line_off']}]},
        chat=asyncio.Queue(),connected=True)
    log=AgentLog(tmp_path/'logs')
    r=Runner(Settings('ws://127.0.0.1:1','offline','',allow_control=True),cfg,log,client=client,playbooks=[],brain=brain,
        clock=lambda:100,policy=ControlPolicy('autonomous',True,'service','full'))
    async def scenario():
        await r.tick()
        r.set_mode('off',False)
        await brain.callback(Intent('late','late','late',1,steps=[SimpleNamespace(node='smart_panel_01',action='line_off',value=3,reason='',expect=None)],source='llm'))
        for task in r._bg:task.cancel()
    run(scenario());log.close()
    assert not sent


def test_yellow_is_not_latched_and_keeps_mixed_dependencies(tmp_path):
    h=create_app(tmp_path/'db.sqlite',token='').state.hub
    h.policy=ControlPolicy('autonomous',True,'service','history')
    st=DomeState();frame(st,time.time())
    h.runner=None
    async def scenario():
        result=h.safety.yellow(str(uuid4()))
        assert result['status']=='not_supported'
        assert h.policy.operating_state=='manual_after_yellow' and not h.policy.algorithms_enabled
        h.set_mode('autonomous',True)
        assert h.policy.operating_state=='normal'
    run(scenario())
    h.runner=SimpleNamespace(state=st,cfg=load_dome_config('agent/dome.yaml'))
    assert h.safety.blockers(7) and h.safety.blockers(1) and h.safety.blockers(3)
    assert h.safety.blockers(4) is None


def test_red_barrier_is_immediate_and_does_not_persist_latch(tmp_path):
    h=create_app(tmp_path/'db.sqlite',token='').state.hub
    calls=[];h.safety.force_exit=lambda code:calls.append(code)
    async def scenario():
        result=h.safety.red()
        h.safety.shutdown_timer.cancel()
        assert result['status']=='shutting_down'
        assert h.policy.reject('operator',h.policy.policy_epoch,'smart_panel_01','line_on')
        with pytest.raises(ValueError):h.set_mode('autonomous',True)
        await asyncio.sleep(.02)
    run(scenario())
    new=create_app(tmp_path/'db.sqlite',token='').state.hub
    assert new.policy.operating_state=='normal' and new.client is None


def test_resume_prepared_budget_allows_one_send_and_requires_new_progress(setup_gate):
    from kupol.dome.control import SEND_GUARD
    gate,st,_,sent=setup_gate
    frame(st,100,state='PAUSED',reason='IL_CO2')
    async def send(*args):
        assert SEND_GUARD.get()() is None
        sent.append(args);return {'success':True}
    gate._send=send
    cmd=Cmd('printer_3d_01','resume',ref=1,expect=Expect.custom('работает',lambda s:s.get('printer_3d_01.state')=='PRINTING',['printer_3d_01.state']),recovery_owner='printer_supervisor')
    result=run(gate.submit(cmd))
    assert result.status=='sent' and len(sent)==1
    frame(st,101,progress=20);assert not gate.check_pending()
    frame(st,102,progress=21);done=gate.check_pending()
    assert len(done)==1 and done[0].verified


def test_smoke_cannot_be_overridden_by_confirmed_power_on(setup_gate):
    gate,st,_,sent=setup_gate
    st.nodes['smoke_detector_01']={'smoke_detected':True,'updated_at':100,'online':True}
    result=run(gate.submit(Cmd('smart_panel_01','line_on',3,ref=1,actor='operator',confirmed=True)))
    assert result.status=='rejected' and not sent


def test_new_routes_inherit_service_auth(tmp_path):
    with TestClient(create_app(tmp_path/'db.sqlite',token='offline-test-token')) as c:
        assert c.get('/control/production').status_code==401
        assert c.post('/control/safety/red',json={}).status_code==401
        assert c.get('/control/incidents',headers={'Authorization':'Bearer offline-test-token'}).status_code==200


def test_explicit_operator_request_to_agent_is_not_automatic_pause_recovery(setup_gate):
    gate,st,_,sent=setup_gate
    frame(st,100,state='PAUSED')
    cmd=Cmd('printer_3d_01','resume',ref=1,actor='agent',operator_requested=True)
    assert run(gate.submit(cmd)).status=='sent'
    assert len(sent)==1


def test_yellow_plan_verifies_cnc_line_and_reports_partial_mixed_line(tmp_path,monkeypatch):
    from kupol.dome.gate import GateResult
    monkeypatch.setenv('YELLOW_PLAN_TIMEOUT_S','0.01')
    h=create_app(tmp_path/'db.sqlite',token='').state.hub
    h.policy=ControlPolicy('off',False,'service','full',1,'manual_after_yellow')
    st=DomeState();frame(st,time.time())
    sent=[]
    gate=SimpleNamespace(pending=[])
    async def submit(cmd):
        sent.append(cmd.value);seq=len(sent)
        gate.pending.append(SimpleNamespace(action_seq=seq,verified=True))
        return GateResult('sent',action_seq=seq)
    gate.submit=submit
    h.runner=SimpleNamespace(state=st,cfg=load_dome_config('agent/dome.yaml'),gate=gate,
        log=SimpleNamespace(write=lambda *a,**k:1),client=SimpleNamespace(session_id='offline'))
    h.safety.yellow_result={'status':'requested','lines':{}}
    run(h.safety._yellow_plan(1))
    result=h.safety.yellow_result
    assert 4 in sent and 7 not in sent and 1 not in sent
    assert result['status']=='partial' and result['lines']['4']['status']=='verified'
    assert result['lines']['1']['status']=='preserved'


def test_job_identity_survives_restart_without_replaying_a_command(tmp_path):
    path=tmp_path/'db.sqlite'
    h=create_app(path,token='').state.hub
    st=DomeState();frame(st,100,elapsed=10)
    h.production.tracker.observe(st,100)
    original=h.production.tracker.jobs['printer_3d_01'].job_id
    restarted=create_app(path,token='').state.hub
    assert restarted.production.tracker.jobs['printer_3d_01'].job_id==original
    frame(st,112,elapsed=22,progress=21)
    restarted.production.tracker.observe(st,112)
    assert restarted.production.tracker.jobs['printer_3d_01'].job_id==original
    assert not restarted.store.events(event_type='control')['items']
