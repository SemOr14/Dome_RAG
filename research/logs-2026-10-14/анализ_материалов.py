import collections
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).parents[2]
OUT=Path(__file__).parent

def write(name,obj):
    (OUT/name).write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')

def json_type(value):
    return 'null' if value is None else 'boolean' if isinstance(value,bool) else 'object' if isinstance(value,dict) else 'array' if isinstance(value,list) else 'integer' if isinstance(value,int) else 'number' if isinstance(value,float) else 'string'

def walk(value,path=''):
    if isinstance(value,dict):
        for key,item in value.items():
            p=path+'/'+str(key).replace('~','~0').replace('/','~1')
            yield p,json_type(item)
            yield from walk(item,p)
    elif isinstance(value,list):
        for item in value:
            yield from walk(item,path+'/*')

api=(ROOT/'01_API.md').read_text(encoding='utf-8-sig')
start=api.index('## 8. Справочник узлов')
end=api.index('## 9. Пример клиента')
segment=api[start:end]
blocks=list(re.finditer(r'^\*\*`[^\n]+',segment,re.M))
catalog={}
for i,match in enumerate(blocks):
    body=segment[match.start():blocks[i+1].start() if i+1<len(blocks) else len(segment)]
    ids=re.findall(r'`([a-z][a-z0-9_]*_\d+)`',body.splitlines()[0])
    source_line=api[:start+match.start()].count('\n')+1
    field_text=body.split('Поля:',1)[1] if 'Поля:' in body else ''
    field_text=re.split(r'\n\n|Действия?:|Запрос',field_text,maxsplit=1)[0]
    names=re.findall(r'`([a-z][a-z0-9_]*(?:\[\d+\])?)`',field_text)
    names=list(dict.fromkeys(names))
    tables=[]
    for line in body.splitlines():
        if line.startswith('|') and not line.startswith('|---'):
            columns=re.split(r'(?<!\\)\|',line)[1:-1]
            if len(columns)>=2 and 'Действие' not in columns[0]:
                tables.append({'actions_and_aliases':re.findall(r'`([^`]+)`',columns[0]),'value_specification_markdown':columns[1].strip()})
    for node_id in ids:
        catalog[node_id]={'source':{'file':'01_API.md','line':source_line},'documented_fields':names.copy(),'common_fields':['online','updated_at','status'],'action_rows':tables.copy(),'specification_markdown':body.strip(),'runtime_permissions':'Not queried; describe_nodes is authoritative'}

base=catalog['climate_sensor_01']['documented_fields']
for node in ['climate_sensor_02','climate_sensor_03']:
    catalog[node]['documented_fields']=list(dict.fromkeys(base+catalog[node]['documented_fields']))
catalog['meeting_01']['documented_fields']=['message_count','last_message_id','recent_messages']
catalog['meeting_01']['common_fields']=['online']
catalog['meeting_01']['state_available_from_chatbot']=False
write('Каталог_узлов_по_API.json',{'generated_at_utc':datetime.now(timezone.utc).isoformat(),'source':str(ROOT/'01_API.md'),'notes':['Static documentation, not live permissions or current telemetry.','Commands in action_rows are not the whole list: prose-only commands are preserved verbatim in specification_markdown.','Common fields and documented fields are separated; field lists are an extraction and may include nested keys.'],'node_count':len(catalog),'nodes':catalog})

report={'source':str(ROOT/'Logs'),'historical_only':True,'files':{},'observed_nodes':{},'event_counts':{},'actions':{},'automation':{},'logbook':[]}
schemas=collections.defaultdict(lambda:collections.defaultdict(collections.Counter))
top=collections.defaultdict(set)
snapshots=collections.Counter()
event_counts=collections.Counter();actions=collections.Counter();auto=collections.Counter()
for path in sorted((ROOT/'Logs').rglob('*.jsonl')):
    info={'records':0,'parse_errors':[],'kinds':collections.Counter(),'first_ts':None,'last_ts':None}
    with path.open(encoding='utf-8-sig') as f:
        for lineno,line in enumerate(f,1):
            if not line.strip():continue
            try:obj=json.loads(line)
            except Exception as e:
                info['parse_errors'].append({'line':lineno,'error':str(e)});continue
            info['records']+=1;kind=obj.get('kind','unknown');info['kinds'][kind]+=1
            ts=obj.get('ts');info['first_ts']=info['first_ts'] or ts;info['last_ts']=ts
            if kind=='telemetry':
                for node,data in obj.get('nodes',{}).items():
                    snapshots[node]+=1
                    if isinstance(data,dict):top[node].update(data)
                    for pointer,typ in walk(data):schemas[node][pointer][typ]+=1
            else:
                event_counts[kind]+=1
                if kind=='action':actions[(str(obj.get('actor')),str(obj.get('node_id')),str(obj.get('action')),str(obj.get('success')))]+=1
                if kind=='automation':
                    event=obj.get('event',{})
                    auto[(str(event.get('interlock')),str(event.get('type')))]+=1
                if kind=='logbook':report['logbook'].append(obj)
    info['kinds']=dict(info['kinds'])
    report['files'][str(path.relative_to(ROOT))]=info
for node in sorted(schemas):
    report['observed_nodes'][node]={'snapshot_count':snapshots[node],'top_level_fields':sorted(top[node]),'field_types':{p:dict(count) for p,count in sorted(schemas[node].items())}}
report['event_counts']=dict(event_counts)
report['actions']=[{'actor':a,'node_id':n,'action':act,'success':s,'count':count} for (a,n,act,s),count in actions.most_common()]
report['automation']=[{'interlock':a,'event_type':t,'count':count} for (a,t),count in auto.most_common()]
write('Анализ_исторических_журналов.json',report)

snapshot_path=OUT/'Купол_состояния_всех_нод.json'
if snapshot_path.exists():
    bundle=json.loads(snapshot_path.read_text(encoding='utf-8'))
    bundle['documentation_comparison']={'source':str(ROOT/'01_API.md'),'catalog_nodes':len(catalog),'scope_warning':'Chatbot and documented WebSocket system may be different interfaces/versions; do not treat chatbot replies as complete WebSocket telemetry.','not_polled_nodes':sorted(set(catalog)-set(bundle['nodes']))}
    for node,entry in bundle['nodes'].items():
        if node in catalog and isinstance(entry.get('state'),dict):
            documented={re.sub(r'\[\d+\]$','',x) for x in catalog[node]['documented_fields']+catalog[node]['common_fields']}
            reported=set(entry['state'])
            entry['documentation_comparison']={'documented_fields_not_returned':sorted(documented-reported),'reported_fields_not_in_documented_list':sorted(reported-documented),'notes':'Different field names may be aliases or a different version, not necessarily an error. No values synthesized.'}
    snapshot_path.write_text(json.dumps(bundle,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')

print('DOCUMENTED NODES',len(catalog),sorted(catalog))
print('LOG FILES',json.dumps(report['files'],ensure_ascii=False))
print('HISTORICAL NODES',len(schemas))
print('EVENT COUNTS',dict(event_counts))
print('AUTOMATION',json.dumps(report['automation'],ensure_ascii=False))
print('ACTIONS',json.dumps(report['actions'],ensure_ascii=False))
print('LOGBOOK',json.dumps(report['logbook'],ensure_ascii=False))
