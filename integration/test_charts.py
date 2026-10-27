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


def test_charts_do_not_classify_impacts_in_code(tmp_path):
    # Разделение воздействий делает LLM-агент; графики поднимают только числовые предупреждения
    app = create_app(tmp_path / 'impacts.sqlite3', token='')
    nodes = {f'climate_sensor_0{i}': {'online': True, 'status': 'OK', 'updated_at': 1, 'co2_ppm': v}
             for i, v in enumerate((1600, 800, 810), 1)}
    app.state.store.append([envelope('run', 'telemetry', {'timestamp': 1, 'data': {'nodes': nodes}},
                            source='dome', session_id='test', channel='websocket', direction='inbound')])
    with TestClient(app) as client:
        data = client.get('/control/charts?run_id=run').json()
        assert 'impacts' not in data
        assert {(c['node'], c['field']) for c in data['items'] if c['anomaly']} == {('climate_sensor_01', 'co2_ppm')}


def test_array_anomalies_collapse_into_one_chart():
    from integration.charts import ChartFeed
    from llm_agent.monitoring import NumericMonitor
    monitor = NumericMonitor()
    monitor.active = {("box", f"fans_rpm[{i}]"): {"node": "box", "field": f"fans_rpm[{i}]", "value": None, "note": "нет", "kind": "missing", "frame": 1}
                      for i in range(6)}
    result = ChartFeed._result({}, monitor, None, "run")
    flagged = [i for i in result["items"] if i["anomaly"]]
    assert len(flagged) == 1 and "элементов: 6" in flagged[0]["anomaly"]["note"]
