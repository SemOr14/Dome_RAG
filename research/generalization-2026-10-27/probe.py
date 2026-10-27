"""Fixed synthetic regression/transfer probes. Not an estimate of field recall.
Run: python research/generalization-2026-10-27/probe.py [repository-root]
"""
import json
import random
import sys
from pathlib import Path

repo = Path(sys.argv[1]).resolve() if __name__ == '__main__' and len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo))
from llm_agent.triggers import TelemetryWatch


def cases(seed):
    rng = random.Random(seed)
    pressure = rng.uniform(1, 5)
    speed = rng.uniform(1000, 5000)
    node = f'extension_{seed}_not_in_catalog'
    def simple(before, after):
        return lambda i: {node: dict(before if i < 20 else after)}
    drive = {'state': 'RUNNING', 'motor_target_rpm': speed, 'motor_rpm': speed}
    rows = [
        ('nested_fault', True, simple({'parts': [{'driver_state': 'OK'}]}, {'parts': [{'driver_state': 'OVERHEAT'}]})),
        ('pressure_spike', True, simple({'pressure_bar': pressure}, {'pressure_bar': pressure * 4})),
        ('lost_reading', True, simple({'pressure_bar': pressure}, {'pressure_bar': None})),
        ('motor_zero_response', True, simple(drive, dict(drive, motor_rpm=0))),
        ('motor_partial_response', True, simple(drive, dict(drive, motor_rpm=speed * .5))),
        ('motor_wrong_direction', True, simple(drive, dict(drive, motor_rpm=-speed))),
        ('slow_drift', True, lambda i: {node: {'zone': 'process', 'pressure_bar': pressure + max(0, i - 19) * pressure / 80},
                                      'ref_A': {'zone': 'process', 'pressure_bar': pressure}, 'ref_B': {'zone': 'process', 'pressure_bar': pressure}}),
        ('heater_bad_at_start', True, simple({'state': 'RUNNING', 'heater_target_c': 60, 'heater_temp_c': 100, 'heater_power_pct': 25},
                                           {'state': 'RUNNING', 'heater_target_c': 60, 'heater_temp_c': 100, 'heater_power_pct': 25})),
        ('nested_error_code', True, simple({'driver': {'error_code': None}}, {'driver': {'error_code': 'E_BEARING_FAILURE'}})),
        ('frozen_sensor', True, lambda i: {node: {'zone': 'room', 'temperature_c': 20}, 'ref_A': {'zone': 'room', 'temperature_c': 20 if i < 20 else 42 + i % 7 * 2}}),
        ('stale_timestamp', True, simple({'pressure_bar': pressure}, {'pressure_bar': pressure})),
        ('progress_stall', True, lambda i: {node: {'state': 'MOVING', 'progress_pct': min(i, 19), 'file_name': 'part'}}),
        ('normal_steady', False, simple({'pressure_bar': pressure}, {'pressure_bar': pressure})),
        ('normal_startup', False, lambda i: {node: {'state': 'IDLE' if i < 20 else 'RUNNING', 'motor_target_rpm': 0 if i < 20 else speed,
                                                  'motor_rpm': 0 if i < 20 else min((i - 20) * speed / 3, speed)}}),
        ('normal_mode_change', False, simple({'state': 'RUNNING', 'mode': 'LOW', 'pressure_target_bar': pressure, 'pressure_bar': pressure},
                                            {'state': 'RUNNING', 'mode': 'HIGH', 'pressure_target_bar': pressure * 3, 'pressure_bar': pressure * 3})),
        ('normal_regulated', False, simple({'zone': 'isolated', 'temperature_c': 20}, {'zone': 'isolated', 'temperature_c': 20})),
        ('normal_power_cycle', False, lambda i: {node: {'power_w': 1000 if i % 2 else 0}}),
        ('normal_angle_wrap', False, lambda i: {node: {'wind_speed_ms': 3, 'wind_direction_deg': 359 if i % 2 else 1}}),
    ]
    return node, rows


def evaluate(seed):
    node, specs = cases(seed)
    rows = []
    for name, anomalous, build in specs:
        watch = TelemetryWatch(30, 180)
        hits = []
        for i in range(60):
            stamp = 1 + i * 60
            nodes = {k: dict(v, updated_at=(1 + 19 * 60 if name == 'stale_timestamp' and k == node and i >= 20 else stamp),
                             online=True, status='OK') for k, v in build(i).items()}
            nodes['heartbeat'] = {'updated_at': stamp, 'online': True, 'status': 'OK'}
            for t in watch.observe({'timestamp': stamp, 'observed_at': str(i), 'nodes': nodes}, {}, i * 60):
                if (i >= 20 or name == 'heater_bad_at_start') and node in t.nodes:
                    hits.append((i, t.kind, t.key))
        rows.append(dict(seed=seed, case=name, anomaly=anomalous, detected=bool(hits), first=hits[:1]))
    return rows


def main():
    rows = [row for seed in range(5) for row in evaluate(seed)]
    positive = [r for r in rows if r['anomaly']]
    negative = [r for r in rows if not r['anomaly']]
    print(json.dumps(dict(anomalies=len(positive), detected=sum(r['detected'] for r in positive),
                         normal=len(negative), false_triggers=sum(r['detected'] for r in negative),
                         cases=rows), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
