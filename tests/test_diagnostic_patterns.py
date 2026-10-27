"""Source routing and episode stability on arbitrary device names; no equipment actions."""
from llm_agent.patterns import DiagnosticPatterns
from llm_agent.triggers import Trigger


def bodies(t=100):
    return {n: {'zone':'room','temperature_c':v,'updated_at':t,'online':True,'status':'OK','failure_domain':d}
            for n,v,d in [('primary',30,'bus_a'),('spare1',21,'bus_b'),('spare2',21.1,'bus_c')]}

def trigger(node='primary',kind='conflict',key='case',field='temperature_c'):
    return Trigger(kind,key,'Показания требуют проверки',{node},meta={'evidence':{'node':node,'field':field,'kind':kind}})

def tick(engine,nodes,trigs=(),active=(),t=100):
    return engine.observe({'timestamp':t,'nodes':nodes},trigs,set(active),{},t)


def test_suspect_sensor_routes_to_a_fresh_peer_and_keeps_raw_values():
    e=DiagnosticPatterns();ns=bodies(); tick(e,ns,[trigger()],['case'])
    p=e.plans()[0]
    assert p['pattern']=='sensor_conflict'
    assert p['selected_observation']['node']=='spare1'
    assert p['source_use']=='analysis_only' and ns['primary']['temperature_c']==30
    assert not any('actions' in plan for plan in e.plans())


def test_missing_source_uses_same_measurement_from_other_source():
    e=DiagnosticPatterns();ns=bodies();ns['primary']['online']=False
    tick(e,ns,[trigger(kind='stale',field='status')],['case'])
    assert e.plans()[0]['pattern']=='source_missing'
    assert e.plans()[0]['selected_observation']['node']=='spare1'


def test_bad_stale_delayed_degraded_same_bus_and_untrusted_spares_are_rejected():
    changes=[{'updated_at':0},{'reading_delay_s':90},{'sensor_state':'DEGRADED'},
             {'failure_domain':'bus_a'},{'units':{'temperature_c':'kelvin'}}, {'alarm':True}]
    for change in changes:
        e=DiagnosticPatterns(); ns=bodies();ns['spare1'].update(change);ns.pop('spare2')
        tick(e,ns,[trigger()],['case']);assert not e.plans()[0]['alternatives'],change
    e=DiagnosticPatterns();ns=bodies();ns.pop('spare2');ns['plc']={'data_trust':{'spare1.temperature_c':'UNRELIABLE'}}
    tick(e,ns,[trigger()],['case']);assert not e.plans()[0]['alternatives']


def test_no_zone_or_other_units_are_not_a_reason_to_guess_a_fallback():
    e=DiagnosticPatterns();ns=bodies();ns['primary'].pop('zone')
    tick(e,ns,[trigger()],['case']);assert e.plans()[0]['selected_observation'] is None


def test_fallback_is_sticky_then_fails_over_when_it_is_no_longer_usable():
    e=DiagnosticPatterns();ns=bodies();tick(e,ns,[trigger()],['case'])
    ns=bodies(110);ns['spare2']['updated_at']=111
    tick(e,ns,active=['case'],t=111)
    assert e.plans()[0]['selected_observation']['node']=='spare1'
    ns['spare1']['online']=False
    tick(e,ns,active=['case'],t=112)
    assert e.plans()[0]['selected_observation']['node']=='spare2'


def test_recovery_requires_three_own_source_updates_and_no_active_detector():
    e=DiagnosticPatterns(hold_s=0);ns=bodies();tick(e,ns,[trigger()],['case'])
    for t in (110,120,130):tick(e,ns,t=t)
    assert e.plans()[0]['status']=='observing'  # repeated polling cannot confirm recovery
    for t in (140,150,160):tick(e,bodies(t),t=t)
    assert not e.plans()


def test_disappearing_data_never_closes_an_episode():
    e=DiagnosticPatterns();ns=bodies();tick(e,ns,[trigger()],['case'])
    for t in (200,300,400):tick(e,{},t=t)
    assert e.plans() and e.plans()[0]['status']!='recovered'


def test_mass_failure_checks_common_infrastructure_instead_of_individual_resets():
    e=DiagnosticPatterns();ns=bodies();t=trigger(kind='stale')
    t.meta['affected_nodes']=list(ns)
    tick(e,ns,[t],['case'])
    assert e.plans()[0]['pattern']=='common_failure'
    assert not e.plans()[0]['selected_observation']


def test_consistent_multiple_measurements_get_process_change_strategy():
    e=DiagnosticPatterns();ns=bodies();ns['primary']['temperature_c']=21.05
    tick(e,ns,[trigger(kind='numeric')],['case'])
    assert e.plans()[0]['pattern']=='coherent_change'
    assert len(e.plans()[0]['nodes'])==3


def test_ack_failure_and_actuator_stall_have_distinct_check_orders():
    for t,expected in [(trigger(kind='command'),'command_uncertain'),(trigger(kind='operation'),'actuator_mismatch')]:
        e=DiagnosticPatterns();tick(e,bodies(),[t],[t.key]);assert e.plans()[0]['pattern']==expected


def test_transition_journal_does_not_repeat_on_each_poll_or_new_spare_value():
    e=DiagnosticPatterns();ns=bodies();assert tick(e,ns,[trigger()],['case'])
    assert not tick(e,ns,active=['case'])
    ns=bodies(110);ns['spare1']['temperature_c']=21.2
    assert not tick(e,ns,active=['case'],t=110)
    assert e.plans()[0]['selected_observation']['value']==21.2


def test_plans_are_snapshots_and_capacity_is_bounded():
    e=DiagnosticPatterns(capacity=2);ns=bodies();tick(e,ns,[trigger()],['case']);p=e.plans()
    p[0]['checks'].clear();assert e.plans()[0]['checks']
    for i in range(5):tick(e,ns,[trigger(key=str(i),field=f'other_{i}_c')],[str(i)])
    assert len(e.cases)==2


def test_whole_frame_age_prevents_a_false_fresh_backup():
    e=DiagnosticPatterns();ns=bodies()
    e.observe({'timestamp':100,'telemetry_age_s':95,'nodes':ns},[trigger()],{'case'},{},100)
    assert not e.plans()[0]['alternatives']


def test_asynchronous_sources_need_three_updates_of_each_source_for_group_recovery():
    e=DiagnosticPatterns(hold_s=0);ns=bodies();t=trigger(kind='stale')
    t.nodes=set(ns);t.meta['affected_nodes']=list(ns)
    tick(e,ns,[t],['case'])
    for i in range(1,4):
        for n in ns:
            ns[n]['updated_at']=100+i
            tick(e,ns,t=100+i)
    assert not e.plans()


def test_plan_reaches_model_and_raw_measurement_is_not_replaced(tmp_path):
    from test_dispatcher import make
    ns=bodies();ns['primary']['online']=False
    d,llm,clock=make(tmp_path,{'timestamp':100,'nodes':ns})
    result=d.tick()
    assert result and 'Диагностические стратегии' in llm.calls[0]
    assert 'selected_observation' in llm.calls[0] and 'analysis_only' in llm.calls[0]
    assert ns['primary']['temperature_c']==30
    assert not result[0].response.accepted_actions


def test_patterns_do_not_pollute_retrieval_queries(tmp_path):
    from test_dispatcher import make
    from llm_agent.patterns import MARK
    from llm_agent.context.telemetry import compact_telemetry
    d,_,_=make(tmp_path,{'timestamp':100,'nodes':bodies()})
    query=d.agent.builder.rag_query('Подозрительное показание\n\n'+MARK+'\nsecret_strategy_marker',compact_telemetry({'nodes':bodies()},{}))
    assert 'secret_strategy_marker' not in query


def test_repeated_failure_requires_longer_recovery_without_source_ping_pong():
    e=DiagnosticPatterns(hold_s=0);tick(e,bodies(),[trigger()],['case'])
    tick(e,bodies(110),t=110)
    tick(e,bodies(120),[trigger()],['case'],t=120)
    assert e.plans()[0]['flaps']==1
    for t in (130,140,150):tick(e,bodies(t),t=t)
    assert e.plans()  # three updates are no longer sufficient after a flap
    tick(e,bodies(160),t=160)
    assert not e.plans()


def test_three_coherent_candidates_share_a_single_case():
    e=DiagnosticPatterns();ns=bodies();ns['primary']['temperature_c']=21.05
    events=tick(e,ns,[trigger(node=n,kind='numeric',key=n) for n in ns],set(ns))
    assert len(e.cases)==1 and len(events)==1


def test_future_timestamp_is_not_a_valid_backup():
    e=DiagnosticPatterns();ns=bodies();ns['spare1']['updated_at']=10000;ns.pop('spare2')
    tick(e,ns,[trigger()],['case'],t=100)
    assert not e.plans()[0]['alternatives']
