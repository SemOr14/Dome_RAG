import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).parent
SOURCE = ROOT / 'источники'
OUTPUT = ROOT / 'Купол_состояния_всех_нод.json'
LOG = SOURCE / 'опрос_полных_состояний.jsonl'
NODE_IDS = '''solar_panels_01 solar_inverter_01 battery_01 wind_turbine_01 dizel_1 fuel_tank_01 smart_panel_01 printer_3d_01 cnc_01 material_inventory_01 equipment_cooling_01 climate_sensor_01 climate_sensor_02 climate_sensor_03 air_quality_sensor_01 smoke_detector_01 fume_extraction_01 supply_ventilation_01 dome_sealing_01 thermal_insulation_01 thermal_insulation_02 weather_station_01 seysmo_01 radiation_sensor_01 chem_sensor_01 smoke_detector_02 water_pump_01 water_tank_01 water_filter_01 fire_suppression_01 access_control_01 radio_01 backup_comms_01 edge_compute_01 network_link_01 dome_automation_01'''.split()

def now():
    return datetime.now(timezone.utc).isoformat()

def parse_reply(reply, node_id):
    text = reply.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        candidates = []
        for pos, char in enumerate(text):
            if char == '{':
                try:
                    obj, _ = decoder.raw_decode(text[pos:])
                    if isinstance(obj, dict):
                        candidates.append(obj)
                except json.JSONDecodeError:
                    pass
        matching = [obj for obj in candidates if obj.get('node_id') == node_id]
        if not matching:
            raise ValueError('Нет корректного JSON с запрошенным node_id')
        obj = matching[0]
    if not isinstance(obj, dict):
        raise ValueError('Ответ не является объектом')
    if obj.get('node_id') != node_id:
        raise ValueError('node_id ответа не совпадает с запросом')
    data = obj.get('data')
    if not isinstance(data, dict) or not data:
        raise ValueError('Поле data отсутствует, пустое или не объект')
    return data

def fields(value, path=''):
    result = []
    if isinstance(value, dict):
        for key, item in value.items():
            part = str(key).replace('~','~0').replace('/','~1')
            pointer = path + '/' + part
            kind = ('null' if item is None else 'boolean' if isinstance(item, bool) else 'object' if isinstance(item, dict) else 'array' if isinstance(item,list) else 'integer' if isinstance(item,int) else 'number' if isinstance(item,float) else 'string')
            result.append({'pointer': pointer, 'observed_type': kind})
            result.extend(fields(item, pointer))
    elif isinstance(value,list):
        for index,item in enumerate(value):
            result.extend(fields(item,path+'/'+str(index)))
    return result

def save(bundle):
    bundle['collection']['last_saved_at_utc'] = now()
    good = sum(n.get('state') is not None for n in bundle['nodes'].values())
    bundle['collection']['nodes_received'] = good
    bundle['collection']['nodes_unavailable'] = [node for node in NODE_IDS if bundle['nodes'].get(node,{}).get('state') is None]
    temp = OUTPUT.with_suffix('.json.tmp')
    temp.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(OUTPUT)

def query(node_id, attempt):
    prompt = ('Только чтение. Вызови get_node_status для '+node_id+' и верни полный результат инструмента в виде компактного корректного JSON. '
              'Сохрани ВСЕ поля, вложенные объекты, массивы, null, исходные имена полей и единицы. Не пересказывай, не сокращай, не добавляй предполагаемые поля. '
              'Формат: {"node_id":"'+node_id+'","data":{...}}. Если полный результат недоступен, честно укажи это. Никаких управляющих действий.')
    if attempt:
        prompt += ' Предыдущий ответ не удалось разобрать. Ответь именно про '+node_id+', только корректным JSON; не про другие узлы.'
    started = now()
    req = Request('http://62.118.24.234:48765/api/chat', data=json.dumps({'message':prompt},ensure_ascii=False).encode('utf-8'), headers={'Content-Type':'application/json'})
    with urlopen(req,timeout=90) as response:
        result = json.load(response)
    record = {'node_id':node_id,'request_started_at_utc':started,'received_at_utc':now(),'question':prompt,'response':result}
    with LOG.open('a',encoding='utf-8') as f:
        f.write(json.dumps(record,ensure_ascii=False)+'\n')
    return record

def main():
    SOURCE.mkdir(parents=True,exist_ok=True)
    bundle = {
        'schema_version':'1.0',
        'source':{'interface':'chatbot_only','page_url':'http://62.118.24.234:48765/static/index.html','state_origin':'JSON reported in chatbot replies; not a direct controller response'},
        'collection':{'started_at_utc':now(),'completed_at_utc':None,'expected_nodes':len(NODE_IDS),'simultaneous_snapshot':False,'control_actions_requested':False},
        'limitations':[
            'Состояния запрошены последовательно, поэтому относятся к разным моментам времени.',
            'Источник — ответы чатбота. Дословность результата внутреннего инструмента и полнота всех полей независимо не проверены.',
            'Исходные имена, значения, единицы и вложенность сохраняются; отсутствующие поля не дополняются предположениями.',
            'Время получения ответа отличается от времени измерения. updated_at и reading_delay_s сохраняются внутри state, если бот их вернул.',
            'field_inventory описывает только реально полученные поля и типы; это не официальная схема и не перечень допустимых значений.'
        ],
        'nodes':{}
    }
    # Reuse the immediately preceding fresh solar-panel response, preserving its original reception time.
    prior = [json.loads(line) for line in (SOURCE/'диалог_справка.jsonl').read_text(encoding='utf-8').splitlines()]
    for record in reversed(prior):
        if 'Формат: {"node_id":"solar_panels_01"' in record.get('question',''):
            try:
                reply=record['response']['reply']; data=parse_reply(reply,'solar_panels_01')
                bundle['collection']['started_at_utc']=record['time']
                bundle['nodes']['solar_panels_01']={'received_at_utc':record['time'],'capture_status':'parsed_chatbot_json','completeness':'not_independently_verified','state':data,'field_inventory':fields(data),'raw_reply':reply}
                break
            except (KeyError,ValueError):
                pass
    save(bundle)
    for index,node_id in enumerate(NODE_IDS,1):
        if node_id in bundle['nodes']:
            print(f'{index}/{len(NODE_IDS)} {node_id}: reused fresh response, {len(bundle["nodes"][node_id]["state"])} fields',flush=True)
            continue
        errors=[]; last=None
        for attempt in range(2):
            try:
                last=query(node_id,attempt)
                reply=last['response'].get('reply','')
                data=parse_reply(reply,node_id)
                bundle['nodes'][node_id]={'request_started_at_utc':last['request_started_at_utc'],'received_at_utc':last['received_at_utc'],'capture_status':'parsed_chatbot_json','completeness':'not_independently_verified','state':data,'field_inventory':fields(data),'raw_reply':reply}
                if errors:bundle['nodes'][node_id]['prior_attempt_errors']=errors
                break
            except Exception as exc:
                errors.append(type(exc).__name__+': '+str(exc))
                print(f'{node_id} attempt {attempt+1}: {errors[-1]}',flush=True)
        else:
            bundle['nodes'][node_id]={'received_at_utc':now(),'capture_status':'unavailable_or_unparseable','state':None,'field_inventory':[],'errors':errors,'raw_reply':last['response'].get('reply') if last else None}
        save(bundle)
        state=bundle['nodes'][node_id].get('state')
        print(f'{index}/{len(NODE_IDS)} {node_id}: '+(f'{len(state)} top-level fields' if state else 'UNAVAILABLE'),flush=True)
    bundle['collection']['completed_at_utc']=now()
    save(bundle)
    print('OUTPUT '+str(OUTPUT.resolve()),flush=True)

if __name__=='__main__':
    main()
