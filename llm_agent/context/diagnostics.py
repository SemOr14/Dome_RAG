"""Read-only consistency evidence. These are observations, not diagnoses or control permissions."""
from __future__ import annotations

import re
from typing import Any

from ..monitoring import contextual_note, number

BAD_STATES = {'FAULT', 'FAILED', 'FAILURE', 'OVERHEAT', 'ERROR', 'ALARM', 'TRIPPED', 'RCD_TRIP', 'RCD_TRIPPED', 'PUMP_FAULT', 'CONTAMINATED', 'START_FAILURE'}


def state_fields(body: dict, prefix: str = '', depth: int = 0):
    """Current nested state signals; histories/files/messages never become current faults."""
    if depth > 4:
        return
    for field, value in body.items():
        if field in ('events', 'recent_messages', 'files', 'passage_events', 'last_probe'):
            continue
        path = f'{prefix}.{field}' if prefix else field
        if isinstance(value, dict):
            yield from state_fields(value, path, depth + 1)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    yield from state_fields(item, f'{path}[{i}]', depth + 1)
        elif (field in ('state', 'status', 'health') or field.endswith('_state')) and isinstance(value, str):
            state = value.upper()
            if state in BAD_STATES or (not state.startswith(('NO_', 'NOT_')) and
                    any(token in BAD_STATES for token in re.split(r'[_ :/]+', state))):
                yield path, value


def diagnostic_anomalies(nodes: dict[str, Any]) -> list[dict]:
    out = []

    def add(node, param, value, note, kind='conflict', **extra):
        out.append(dict(node=node, param=param, value=value, note=note, kind=kind, **extra))

    for node, body in nodes.items():
        if not isinstance(body, dict) or body.get('online') is False or body.get('status') == 'UNKNOWN':
            continue
        for field, value in state_fields(body):
            details = ' '.join(f'{k}={body[k]}' for k in ('alarm_code', 'error_code') if body.get(k))
            add(node, field, value, f'Отказ устройства: {field} {value} {details}'.strip(), 'alarm')
        if 'dose_rate_usv_h' in body and body.get('alarm_state') in ('WARNING', 'ALARM'):
            add(node, 'alarm_state', body['alarm_state'],
                f'Тревога датчика радиации: dose_rate_usv_h={body.get("dose_rate_usv_h")}, '
                f'warning_threshold={body.get("warning_threshold")}, event_type={body.get("event_type")}, '
                f'sensor_state={body.get("sensor_state")}, confidence={body.get("confidence")}; проверить измерение', 'alarm')
        if body.get('quake_detected') is True:
            add(node, 'quake_detected', True,
                f'Сейсмическая тревога сейсмодатчика quake_detected: amplitude_mm_s={body.get("amplitude_mm_s")}, '
                f'threshold_mm_s={body.get("threshold_mm_s")}; проверить подтверждение и повторяемость', 'alarm')
        if isinstance(body.get('services'), dict) or 'clock_offset_s' in body:
            offset = number(body.get('clock_offset_s'))
            if offset is not None and abs(offset) > 30:
                add(node, 'clock_offset_s', offset,
                    f'Сдвиг времени updated_at: clock_offset_s={offset:g} секунд у edge; проверить синхронизацию часов и меток узлов')
            for service, state in (body.get('services') or {}).items():
                if isinstance(state, str) and (state in BAD_STATES or state == 'STOPPED'):
                    add(node, f'services.{service}', state, f'Сервис edge {service} {state}: проверить сбор телеметрии', 'alarm')
        if any(f in body for f in ('webhook_state', 'nozzle_target_c', 'bed_target_c')):
            webhook, real = body.get('webhook_state'), body.get('real_state') or body.get('state')
            if webhook in ('COMPLETED', 'IDLE') and real in ('PRINTING', 'RUNNING'):
                add(node, 'webhook_state', webhook, f'Webhook сообщает {webhook}, но {"принтер продолжает печатать" if real == "PRINTING" else "устройство продолжает работу"}: real_state {real}, progress_pct={body.get("progress_pct")}. Не считать изделие готовым')
            for field in ('nozzle_temp_c', 'bed_temp_c'):
                value = number(body.get(field))
                if value is not None and (note := contextual_note(node, field, value, body)):
                    add(node, field, value, note)
        if any(f in body for f in ('balancing_state', 'battery_charge_blocked', 'pv_power_w')):
            soc = number(body.get('battery_soc_pct', body.get('soc_pct')))
            balancing = body.get('balancing_state') == 'BALANCING'
            blocked = body.get('battery_charge_blocked') is True or body.get('charge_allowed') is False
            expected_balancing = balancing and soc is not None and soc >= 90
            if (blocked and not expected_balancing) or (balancing and soc is not None and soc < 90):
                add(node, 'balancing_state' if balancing else 'battery_charge_blocked', body.get('balancing_state') if balancing else True,
                    f'BMS: балансировка {body.get("balancing_state")}, battery_charge_blocked={blocked} при SOC={soc} %. Проверить ограничение заряда и согласованность BMS')
            panels = nodes.get('solar_panels_01') or {}
            weather = nodes.get('weather_station_01') or {}
            pv, power, light = number(body.get('pv_power_w')), number(panels.get('power_w')), number(weather.get('illuminance_lux'))
            if (pv is not None and pv > 20 and power is not None and power <= 1 and light is not None and light <= 1
                    and panels.get('online') is not False and panels.get('status') != 'UNKNOWN'
                    and panels.get('updated_at') == body.get('updated_at')):
                add(node, 'pv_power_w', pv,
                    f'Ночная PV-мощность инвертора {pv:g} Вт при нулевой мощности солнечных панелей {power:g} Вт и illuminance_lux={light:g}; несогласованность источников')
        if 'crc_errors' in body:
            missing = [f for f, value in body.items() if value is None and re.search(r'(?:_(?:c|pct|ppm|pa|bar|adc|index)|rpm|readings)$', f)]
            if len(missing) >= 2 and (number(body.get('crc_errors')) or 0) > 0:
                add(node, 'crc_errors', body['crc_errors'], f'Шина/интерфейс опроса: пустые показания {", ".join(missing)}, crc_errors={body["crc_errors"]}, poll_interval_ms={body.get("poll_interval_ms")}. Проверить обмен бокса и замки')
        if isinstance(body.get('lines'), list):
            for i, line in enumerate(body.get('lines') or []):
                if not isinstance(line, dict):
                    continue
                n = line.get('line', i + 1)
                leak = number(line.get('leakage_ma'))
                state = line.get('state')
                if state == 'RCD_TRIP' or line.get('protection_state') == 'RCD_TRIPPED':
                    note = f'УЗО линии {n} ({line.get("name", "")}): leakage_ma={leak}, alarm_code={line.get("alarm_code")}'
                    if leak is not None and leak < 5:
                        note += '; срабатывание без измеренной утечки, требуется проверка причины'
                    add(node, f'lines[{i}].protection_state', state, note, 'alarm')
                elif leak is not None and leak >= 10:
                    add(node, f'lines[{i}].leakage_ma', leak, f'Растущая утечка линии {n} ({line.get("name", "")}): leakage_ma={leak:g} мА, предупреждение 10 мА, УЗО 30 мА', 'alarm')
                power = number(line.get('power_w'))
                # The catalogue explicitly identifies this as a spare line. No such
                # assumption is made for arbitrary loads or for every ON line.
                spare = 'резервная линия' in str(line.get('name', '')).lower()
                if power is not None and power > 20 and (state == 'OFF' or spare):
                    add(node, f'lines[{i}].power_w', power, f'{"Резервная" if spare else "Отключённая"} линия {n}: необъяснённое / фантомное потребление {power:g} Вт при state={state}; проверить наличие нагрузки')
    return out


def group_stale(anomalies: list[dict], nodes: dict) -> list[dict]:
    stale = [a for a in anomalies if 'нет связи' in a['note'] or 'не обновлялись' in a['note']]
    affected = {a['node'] for a in stale}
    if len(affected) < 5:
        return anomalies
    edge_id, edge = next(((k, b) for k, b in nodes.items() if isinstance(b, dict) and
                          ('services' in b or 'clock_offset_s' in b)), ('', {}))
    link_id, link = next(((k, b) for k, b in nodes.items() if isinstance(b, dict) and 'rx_power_dbm' in b), ('', {}))
    panel = next((b for b in nodes.values() if isinstance(b, dict) and 'bus_powered' in b), {})
    offset = number(edge.get('clock_offset_s'))
    if offset is not None and abs(offset) > 30 and all(a.get('param') == 'updated_at' for a in stale):
        return [a for a in anomalies if a not in stale] + [dict(node=edge_id, param='clock_skew',
            value='CLOCK_SKEW', kind='conflict', affected_nodes=sorted(affected),
            note=f'Сдвиг updated_at у {len(affected)} узлов, clock_offset_s={offset:g} секунд. Рассогласованы часы; это не подтверждает массовый отказ связи')]
    evidence = []
    failed = [f'{k}={v}' for k, v in (edge.get('services') or {}).items()
              if isinstance(v, str) and (v in BAD_STATES or v == 'STOPPED')]
    if failed:
        evidence.append('сервис edge ' + ', '.join(failed))
    rx = number(link.get('rx_power_dbm'))
    if rx is not None and rx < -12:
        evidence.append(f'деградация оптического канала SFP: rx_power_dbm={rx}, link_flaps={link.get("link_flaps")}, active_path={link.get("active_path")}')
    if panel.get('bus_powered') is False:
        evidence.append('щиток bus_powered=False')
    note = f'Массовый UNKNOWN: одновременно {len(affected)} узлов недоступны; общая причина — edge, сеть или питание. '
    note += '; '.join(evidence) if evidence else 'Проверить telemetry_collector, сеть и питание; причина пока неизвестна'
    return [a for a in anomalies if a not in stale] + [dict(node=edge_id if failed else link_id or edge_id or sorted(affected)[0],
        param='common_outage', value='UNKNOWN', note=note, kind='stale', affected_nodes=sorted(affected))]
