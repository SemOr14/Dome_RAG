"""Bounded chart history and analysis for every numeric field, isolated by run/source/session."""
import math
from collections import OrderedDict, deque
from threading import Lock

from data_api.codec import loads
from llm_agent.monitoring import NumericMonitor, leaves

DEFAULT_CHARTS = [
    ('solar_panels_01', 'power_w', 'Солнечная генерация', 'Вт'),
    ('solar_inverter_01', 'load_power_w', 'Суммарная нагрузка', 'Вт'),
    ('battery_01', 'soc_pct', 'Заряд АКБ', '%'),
    ('battery_01', 'voltage_v', 'Напряжение АКБ', 'В'),
    ('battery_01', 'current_a', 'Ток АКБ', 'А'),
    ('battery_01', 'temperature_c', 'Температура АКБ', '°C'),
    ('solar_inverter_01', 'output_voltage_v', 'Выход инвертора', 'В'),
    ('solar_inverter_01', 'inverter_temp_c', 'Температура инвертора', '°C'),
    ('solar_inverter_01', 'grid_power_w', 'Мощность сети', 'Вт'),
    ('solar_inverter_01', 'generator_power_w', 'Мощность генератора', 'Вт'),
    ('climate_sensor_01', 'co2_ppm', 'CO₂ · датчик 1', 'ppm'),
    ('climate_sensor_02', 'co2_ppm', 'CO₂ · датчик 2', 'ppm'),
    ('climate_sensor_03', 'co2_ppm', 'CO₂ · датчик 3', 'ppm'),
    ('climate_sensor_01', 'temperature_c', 'Температура воздуха', '°C'),
    ('climate_sensor_01', 'humidity_pct', 'Влажность воздуха', '%'),
    ('smart_panel_01', 'total_power_w', 'Мощность щитка', 'Вт'),
    ('printer_3d_01', 'progress_pct', 'Прогресс печати', '%'),
    ('printer_3d_01', 'nozzle_temp_c', 'Температура сопла', '°C'),
    ('cnc_01', 'progress_pct', 'Прогресс фрезеровки', '%'),
    ('water_tank_01', 'level_pct', 'Запас воды', '%'),
]


class ChartFeed:
    def __init__(self, store):
        self.store = store
        self.streams = OrderedDict()
        self.lock = Lock()

    def read(self, run_id):
        with self.lock, self.store.connection() as conn:
            latest = conn.execute("SELECT id,session_id,source,channel FROM events WHERE run_id=? AND event_type='telemetry' "
                                  "AND EXISTS (SELECT 1 FROM node_snapshots n WHERE n.event_row_id=events.id) ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
            if latest is None:
                return self._result({}, NumericMonitor(), None, run_id)
            key = (run_id, latest['session_id'], latest['source'], latest['channel'])
            if key not in self.streams:
                self.streams[key] = {'cursor': 0, 'monitor': NumericMonitor(), 'series': {}}
                if len(self.streams) > 8:
                    self.streams.popitem(last=False)
            stream = self.streams[key]
            self.streams.move_to_end(key)
            # Initial warmup is the latest 300 frames. Subsequent polls process every new frame.
            clause = 'AND id>?' if stream['cursor'] else ''
            args = (*key, stream['cursor']) if stream['cursor'] else key
            rows = conn.execute("SELECT id,ingested_at,body_json FROM events WHERE run_id=? AND session_id IS ? AND source=? AND channel=? "
                                f"AND event_type='telemetry' {clause} ORDER BY id {'ASC' if stream['cursor'] else 'DESC'} LIMIT 300", args).fetchall()
            for row in (rows if stream['cursor'] else reversed(rows)):
                event = loads(row['body_json'])
                payload = event['payload']
                data = payload.get('data', payload)
                nodes = data.get('nodes') or {}
                stamp = payload.get('timestamp') or event.get('source_timestamp')
                stream['monitor'].observe(nodes, row['id'])
                series = stream['series']
                for node, body in nodes.items():
                    present = dict(leaves(body))
                    fields = {f for n, f in series if n == node} | set(present)
                    for field in fields:
                        value = present.get(field)
                        # Keep null gaps in established numeric series, ignore never-numeric fields.
                        if type(value) not in (int, float) and (node, field) not in series:
                            continue
                        if type(value) not in (int, float) or not math.isfinite(value):
                            value = None
                        series.setdefault((node, field), deque(maxlen=240)).append(
                            {'id': row['id'], 'received_at': row['ingested_at'], 'source_timestamp': stamp, 'value': value})
                stream['cursor'] = row['id']
            result = self._result(stream['series'], stream['monitor'], key, run_id)
            result['catching_up'] = stream['cursor'] < latest['id']
            return result

    @staticmethod
    def _result(series, monitor, key, run_id):
        anomalies = {(a['node'], a['field']): a for a in monitor.active.values()}
        defaults = {(n, f) for n, f, _, _ in DEFAULT_CHARTS}
        specs = [(*k, f'{k[0]} · {k[1]}', '') for k in sorted(anomalies) if k not in defaults] + DEFAULT_CHARTS
        items = [{'node': n, 'field': f, 'title': title, 'unit': unit, 'points': list(series.get((n, f), [])),
                  'anomaly': anomalies.get((n, f)), 'default': (n, f) in defaults} for n, f, title, unit in specs]
        items.sort(key=lambda x: x['anomaly'] is None)
        return {'items': items, 'anomalies': list(anomalies.values()),
                'run_id': run_id, 'session_id': key[1] if key else None, 'source': key[2] if key else None, 'channel': key[3] if key else None,
                'fields_checked': len(monitor.baselines), 'values_checked': monitor.values_seen}
