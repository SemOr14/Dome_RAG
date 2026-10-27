"""Сколько подсвечивает числовой монитор графиков на логах недели. Только чтение, без агента и LLM.

Запуск из корня репозитория: python research/chart-highlighting-2026-10-27/noise.py
"""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from llm_agent.monitoring import NumericMonitor  # noqa: E402

DAYS = ('12', '13', '14', '15', '16', '17', '18')
# Поля эпизодов: подсвечиваются ли и когда впервые за день
EPISODES = {
    ('13', 'smart_panel_01', 'lines[3].leakage_ma'): 'S31 УЗО 10:31',
    ('17', 'smart_panel_01', 'lines[3].leakage_ma'): 'S31 утечка 10:14',
    ('13', 'climate_sensor_01', 'co2_ppm'): 'S08 рост CO2',
    ('16', 'climate_sensor_01', 'co2_ppm'): 'S41 заниженный CO2',
    ('15', 'thermal_insulation_01', 'temperature_c[1]'): 'S36 залип 68 °C',
    ('15', 'battery_01', 'soc_pct'): 'S38 SOC 20 %',
    ('18', 'smoke_detector_02', 'obscuration_pct'): 'S50 пожар',
    ('13', 'weather_station_01', 'temperature_c'): 'S29 +50 °C',
    ('17', 'printer_3d_01', 'nozzle_temp_c'): 'S44 сопло 280 °C',
    ('16', 'solar_inverter_01', 'fan_current_a'): 'S42 вентилятор',
    ('16', 'solar_inverter_01', 'inverter_temp_c'): 'S42 температура',
    ('16', 'fuel_tank_01', 'water_content_ppm'): 'S47 вода в топливе',
    ('17', 'network_link_01', 'rx_power_dbm'): 'S45 SFP',
    ('18', 'battery_01', 'cell_voltage_delta_mv'): 'S48 балансировка',
    ('15', 'weather_station_01', 'wind_direction_deg'): 'S37 флюгер',
}


def main() -> None:
    starts, frames_on, kinds = Counter(), Counter(), Counter()
    for day in DAYS:
        monitor, previous, new, busy, first = NumericMonitor(), set(), 0, 0, {}
        for line in (ROOT / f'rag/data/sources/Logs/2026-10-{day}/telemetry.jsonl').open(encoding='utf-8'):
            row = json.loads(line)
            nodes = row['nodes']
            stamp = max(b['updated_at'] for b in nodes.values() if isinstance(b, dict) and isinstance(b.get('updated_at'), (int, float)))
            active = {(a['node'], a['field']): a for a in monitor.observe(nodes, stamp)}
            for key in active.keys() - previous:
                starts[key] += 1
                new += 1
                note = active[key]['note']
                kinds['предел' if note.startswith('Вне') else 'пропуск' if note.startswith('Измерение') else 'фон'] += 1
            for key in active:
                frames_on[key] += 1
                first.setdefault((day, *key), row['ts'][11:16])
            busy += bool(active)
            previous = set(active)
        print(f'{day}.10: новых предупреждений {new}, кадров с подсветкой {busy} из 1440')
        for (d, node, field), what in EPISODES.items():
            if d == day:
                print(f'    {what}: {node}.{field} — первая подсветка {first.get((d, node, field), "нет")}')
    print('по типу:', dict(kinds), '| всего:', sum(starts.values()), 'по рядам:', len(starts))
    for (node, field), count in starts.most_common(15):
        print(f'{count:5d} {frames_on[(node, field)]:6d}  {node}.{field}')


if __name__ == '__main__':
    main()
