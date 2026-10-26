from fastapi.testclient import TestClient

from integration.server import create_app
from llm_agent.storage.data_api import envelope


def append(store, run, value, stamp, session='s1', node='mystery', source='dome'):
    store.append([envelope(run, 'telemetry', {'type': 'telemetry', 'timestamp': stamp,
                 'data': {'nodes': {node: {'updated_at': stamp, 'pressure': value}}}},
                 session_id=session, source=source, channel='websocket', direction='inbound')])


def test_twenty_defaults_all_numeric_anomalies_and_stream_isolation(tmp_path):
    app = create_app(tmp_path / 'charts.sqlite3', token='test-token')
    with TestClient(app) as client:
        assert client.get('/control/charts?run_id=r1').status_code == 401
        client.headers['Authorization'] = 'Bearer test-token'
        empty = client.get('/control/charts?run_id=r1').json()
        assert len(empty['items']) == 20 and not empty['anomalies']
        for i in range(12):
            append(app.state.store, 'r1', 10, i)
        append(app.state.store, 'r1', 90, 12)
        data = client.get('/control/charts?run_id=r1').json()
        assert len(data['items']) == 21
        assert data['items'][0]['node'] == 'mystery' and data['items'][0]['anomaly']
        assert len(data['items'][0]['points']) == 13
        assert client.get('/control/charts?run_id=r1').json() == data  # polling does not train
        assert not client.get('/control/charts?run_id=r2').json()['anomalies']
        append(app.state.store, 'r1', 10, 13, session='s2')
        new = client.get('/control/charts?run_id=r1').json()
        assert new['session_id'] == 's2' and not new['anomalies']
        assert new['values_checked'] == 1
        append(app.state.store, 'r1', 999, 14, session='s2', source='import')
        historical = client.get('/control/charts?run_id=r1').json()
        assert historical['source'] == 'import' and historical['values_checked'] == 1


def test_backlog_does_not_skip_anomalous_frames_and_history_is_bounded(tmp_path):
    app = create_app(tmp_path / 'backlog.sqlite3', token='')
    with TestClient(app) as client:
        for i in range(12):
            append(app.state.store, 'run', 10, i)
        client.get('/control/charts?run_id=run')
        for i in range(12, 330):
            append(app.state.store, 'run', 20 if i >= 326 else 10, i)
        first = client.get('/control/charts?run_id=run').json()
        assert first['catching_up'] is True and first['values_checked'] == 312
        second = client.get('/control/charts?run_id=run').json()
        assert second['catching_up'] is False and second['values_checked'] == 330
        assert second['items'][0]['anomaly'] and len(second['items'][0]['points']) == 240


def test_custom_series_can_select_exact_source_and_session(tmp_path):
    app = create_app(tmp_path / 'sources.sqlite3', token='')
    append(app.state.store, 'run', 10, 1, session='s1')
    append(app.state.store, 'run', 20, 2, session='s2')
    append(app.state.store, 'run', 30, 3, session='s2', source='import')
    with TestClient(app) as client:
        path = '/control/series?run_id=run&node_id=mystery&field=pressure'
        assert [p['value'] for p in client.get(path).json()['items']] == [10, 20, 30]
        assert [p['value'] for p in client.get(path+'&source=dome&session_id=s2&channel=websocket').json()['items']] == [20]


def test_pair_conflict_and_cascade_promote_relevant_charts_before_calibration(tmp_path):
    app = create_app(tmp_path / 'impacts.sqlite3', token='')
    nodes = {f'climate_sensor_0{i}': {'online': True, 'status': 'OK', 'updated_at': 1, 'co2_ppm': v}
             for i, v in enumerate((1250, 800, 810), 1)}
    nodes['dome_automation_01'] = {'interlocks': {'IL_CO2': {'state': 'TRIPPED'}}}
    nodes['cnc_01'] = {'hold_reason': 'IL_CO2', 'progress_pct': 30}
    app.state.store.append([envelope('run', 'telemetry', {'timestamp': 1, 'data': {'nodes': nodes}},
                            source='dome', session_id='test', channel='websocket', direction='inbound')])
    with TestClient(app) as client:
        data = client.get('/control/charts?run_id=run').json()
        flagged = {(c['node'], c['field']) for c in data['items'] if c['anomaly']}
        assert ('climate_sensor_01', 'co2_ppm') in flagged
        assert ('cnc_01', 'progress_pct') in flagged
        assert {x['kind'] for x in data['impacts']} == {'false', 'cascade'}
