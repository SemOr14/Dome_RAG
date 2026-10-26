import collections
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT=Path(__file__).parents[2]/'Logs'/'2026-10-14'
OUT=Path(__file__).parent
stats=collections.defaultdict(dict)
stream_counts=collections.Counter()
entities={}; relations={}; source_files={}

def typ(v):
 return 'null' if v is None else 'boolean' if isinstance(v,bool) else 'object' if isinstance(v,dict) else 'array' if isinstance(v,list) else 'integer' if isinstance(v,int) else 'number' if isinstance(v,float) else 'string'

def entity(id,kind,label,**attrs):
 if id not in entities:entities[id]={'id':id,'kind':kind,'label':label,**attrs}
 return id

def edge(a,r,b,proof,**attrs):
 key=(a,r,b)
 if key not in relations:relations[key]={'source':a,'relation':r,'target':b,'evidence_count':0,'evidence_samples':[],**attrs}
 e=relations[key];e['evidence_count']+=1
 if len(e['evidence_samples'])<3:e['evidence_samples'].append(proof)

def walk(v,path=''):
 yield path, v
 if isinstance(v,dict):
  for k,item in v.items():yield from walk(item,path+'/'+str(k).replace('~','~0').replace('/','~1'))
 elif isinstance(v,list):
  for item in v:yield from walk(item,path+'/*')

def observe(stream,value,ts,proof):
 stream_counts[stream]+=1
 present=set()
 for pointer,item in walk(value):
  if not pointer:continue
  if pointer not in stats[stream]:
   stats[stream][pointer]={'path':pointer,'observed_types':collections.Counter(),'occurrences':0,'snapshots_present':0,'null_count':0,'first_ts':ts,'last_ts':ts,'examples':[],'observed_string_values':set(),'string_values_truncated':False,'evidence':proof}
  s=stats[stream][pointer];t=typ(item);s['observed_types'][t]+=1;s['occurrences']+=1;s['last_ts']=ts;present.add(pointer)
  if item is None:s['null_count']+=1
  elif isinstance(item,(str,bool,int,float)):
   if len(s['examples'])<4 and not any(type(x)==type(item) and x==item for x in s['examples']):s['examples'].append(item)
   s['last_scalar_value']=item
  if isinstance(item,str):
   if len(s['observed_string_values'])<24:s['observed_string_values'].add(item)
   elif item not in s['observed_string_values']:s['string_values_truncated']=True
  if isinstance(item,(int,float)) and not isinstance(item,bool):
   s['observed_min']=min(s.get('observed_min',item),item);s['observed_max']=max(s.get('observed_max',item),item)
  if isinstance(item,list):
   s['array_length_min']=min(s.get('array_length_min',len(item)),len(item));s['array_length_max']=max(s.get('array_length_max',len(item)),len(item))
 for pointer in present:stats[stream][pointer]['snapshots_present']+=1

for name in ['telemetry.jsonl','events.jsonl','logbook.jsonl']:
 p=ROOT/name;source_files[name]={'path':str(p.resolve()),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'records':0}
 with p.open(encoding='utf-8-sig') as f:
  for lineno,line in enumerate(f,1):
   if not line.strip():continue
   row=json.loads(line);source_files[name]['records']+=1
   proof={'file':name,'line':lineno,'ts':row.get('ts')};ts=row.get('ts');kind=row['kind']
   if kind=='telemetry':
    observe('record:telemetry',{k:v for k,v in row.items() if k!='nodes'},ts,proof)
    for node,data in row['nodes'].items():
     entity(node,'device',node);observe('telemetry:'+node,data,ts,{**proof,'json_pointer':'/nodes/'+node})
     if node=='smart_panel_01':
      for l in data.get('lines',[]):
       lid=entity('line:'+str(l['line']),'power_line',str(l['line'])+'. '+l['name'])
       edge(node,'CONTAINS_LINE',lid,{**proof,'json_pointer':'/nodes/'+node+'/lines'},evidence_kind='structured_record')
     if node=='dome_automation_01':
      for key,interlock in data.get('interlocks',{}).items():
       entity(key,'software_interlock',interlock.get('title',key))
       edge(node,'CONTAINS_INTERLOCK',key,{**proof,'json_pointer':'/nodes/'+node+'/interlocks/'+key},evidence_kind='structured_configuration')
      primary=data.get('primary_co2_sensor')
      if primary:
       entity(primary,'device',primary);edge(node,'SELECTED_CO2_SOURCE',primary,{**proof,'json_pointer':'/nodes/'+node+'/primary_co2_sensor'},evidence_kind='configuration_observation')
      for k,trust in data.get('data_trust',{}).items():
       n,_,param=k.partition('.')
       field=entity('field:'+n+'/'+param,'telemetry_field',n+'.'+param,node_id=n,path='/'+param)
       edge(node,'MARKED_DATA_'+str(trust),field,{**proof,'json_pointer':'/nodes/'+node+'/data_trust'},evidence_kind='configuration_observation')
     if data.get('hold_reason') in ['IL_CO2']:
      edge(node,'REPORTED_HOLD_REASON',data['hold_reason'],{**proof,'json_pointer':'/nodes/'+node+'/hold_reason'},evidence_kind='state_observation')
   else:
    observe('record:'+kind,row,ts,proof)
    ev=entity('record:'+name+':'+str(lineno),'logged_'+kind,kind+' '+str(ts),record=row,evidence=proof)
    target=row.get('node_id')
    if target:
     entity(target,'device',target);edge(ev,'REFERS_TO_NODE',target,proof,evidence_kind='structured_record')
    if kind=='action':
     actor=entity('actor:'+row['actor'],'actor',row['actor'])
     edge(actor,'ISSUED_RECORDED_ACTION',ev,proof,evidence_kind='recorded_request')
     observe('action:'+row['actor']+':'+target+':'+row['action'],{'value':row.get('value'),'success':row.get('success')},ts,proof)
    elif kind=='message':
     sender=row['sender'];role='agent' if sender.get('role')=='team' else sender.get('role','unknown')
     a=entity('actor:'+role,'actor',role)
     recipient=row.get('to')
     edge(a,'SENT_RECORDED_MESSAGE',ev,proof,evidence_kind='message_metadata')
     if recipient in ['operator','agent']:
      b=entity('actor:'+recipient,'actor',recipient);edge(ev,'ADDRESSED_TO',b,proof,evidence_kind='message_metadata')
    elif kind=='automation':
     e=row['event'];il=entity(e['interlock'],'software_interlock',e['interlock'])
     edge(ev,'REPORTS_INTERLOCK_'+e['type'],il,proof,evidence_kind='automation_report')
     for n in e.get('paused',[]):
      entity(n,'device',n);edge(il,'EVENT_REPORTS_PAUSE_OF',n,proof,evidence_kind='automation_report',note='Reported by event; not an independent physical measurement')
    elif kind=='change' and target:
     param=row['param'];parts=re.sub(r'\[\d+\]','.*',param).split('.')
     pointer='/'+ '/'.join(parts)
     field=entity('field:'+target+pointer,'telemetry_field',target+pointer,node_id=target,path=pointer)
     edge(ev,'REPORTS_CHANGE_OF',field,proof,evidence_kind='change_record')

data_nodes={};record_types={};actions={};flat=[]
for stream,rows in stats.items():
 result=[]
 for path,s in sorted(rows.items()):
  types=sorted(s['observed_types'])
  s['type']=' | '.join(types)
  s['observed_types']=dict(s['observed_types']);s['observed_string_values']=sorted(s['observed_string_values'])
  s['nullable_observed']=s['null_count']>0;s['non_null_type_known']=any(t!='null' for t in types)
  s['records_in_stream']=stream_counts[stream]
  s['meaning_of_range']='Observed historical min/max only; not a permitted or safe range'
  if 'array' in types:
   items=rows.get(path+'/*');s['observed_item_types']=sorted(items['observed_types']) if items else []
  result.append(s);flat.append({'stream':stream,**s})
 if stream.startswith('telemetry:'):
  node=stream.split(':',1)[1];data_nodes[node]={'snapshots':stream_counts[stream],'fields':result}
  for s in result:
   field=entity('field:'+node+s['path'],'telemetry_field',node+s['path'],node_id=node,path=s['path'],observed_types=list(s['observed_types']))
   edge(node,'HAS_OBSERVED_FIELD',field,s['evidence'],evidence_kind='field_observation')
   for t in s['observed_types']:
    type_id=entity('json_type:'+t,'json_type',t);edge(field,'HAS_OBSERVED_TYPE',type_id,s['evidence'],evidence_kind='json_value_type')
 elif stream.startswith('record:'):record_types[stream[7:]]={'records':stream_counts[stream],'fields':result}
 else:actions[stream[7:]]={'records':stream_counts[stream],'fields':result}

meta={'generated_at_utc':datetime.now(timezone.utc).isoformat(),'sources':source_files,'source_scope':'Only the three local logs dated 2026-10-14. No docs, chatbot replies or server calls used.','live_state':None,'timestamp_timezone':'Log ts values have no UTC offset; timezone not inferred.','limitations':['Observed types and values are not the complete API schema or current permissions.','A null-only field has unknown non-null type.','Observed numeric extrema are not permitted ranges.','Actions with success=true are recorded as accepted; physical execution is not assumed.','Narrative text is retained as testimony, not promoted automatically to confirmed causal facts.','Graph edges are structured-record observations; graph contains no inferred electrical/hydraulic wiring.']}
dictionary={'metadata':meta,'telemetry_nodes':data_nodes,'record_types':record_types,'observed_action_parameters':actions}
graph={'metadata':meta,'nodes':list(entities.values()),'edges':list(relations.values()),'relation_meaning':{'CONTAINS_LINE':'Line object present in panel telemetry','SELECTED_CO2_SOURCE':'Identifier selected in configuration','EVENT_REPORTS_PAUSE_OF':'Pause asserted by automation event','REPORTED_HOLD_REASON':'Hold reason recorded by device','ISSUED_RECORDED_ACTION':'Request recorded, not independent proof of execution','HAS_OBSERVED_TYPE':'JSON type actually present in a log value'}}
for name,obj in [('Словарь_данных_по_логам.json',dictionary),('Граф_знаний_по_логам.json',graph)]:
 (OUT/name).write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')

lines=['# Поля и типы данных из журналов 14.10.2026','Источник: только локальные telemetry.jsonl, events.jsonl, logbook.jsonl. Текущего состояния сервера здесь нет.','Типы — наблюдавшиеся в файлах JSON. `null` без других типов не позволяет определить ожидаемый тип. `*` обозначает элемент массива. Наблюдавшиеся значения и диапазоны не являются разрешёнными значениями API.']
for node,item in sorted(data_nodes.items()):
 lines += ['\n## '+node,f'Снимков: {item["snapshots"]}.','| Путь поля | Тип по журналу | Наличие в снимках | Наблюдавшиеся значения / диапазон |','|---|---|---|---|']
 for s in item['fields']:
  values=''
  if 'observed_min' in s:values=str(s['observed_min'])+' … '+str(s['observed_max'])
  elif s['observed_string_values'] and len(s['observed_string_values'])<=8:values=', '.join(s['observed_string_values'])
  elif not s['non_null_type_known']:values='Ненулевой тип неизвестен'
  values=values.replace('|','\\|').replace('\n',' ')[:180]
  lines.append('| `'+s['path']+'` | '+s['type'].replace('|','\\|')+' | '+str(s['snapshots_present'])+'/'+str(item['snapshots'])+' | '+values+' |')
lines+=['\n## Схемы событий и параметры фактически записанных команд','Полные поля action, change, automation, journal, message, logbook и метаданных telemetry, а также типы value каждой записанной команды находятся в Словарь_данных_по_логам.json.']
(OUT/'Словарь_данных_по_логам.md').write_text('\n\n'.join(lines[:3])+'\n'+'\n'.join(lines[3:]),encoding='utf-8')

NS='http://graphml.graphdrawing.org/xmlns';ET.register_namespace('',NS)
root=ET.Element('{'+NS+'}graphml')
for key in ['label','kind','relation','metadata']:
 ET.SubElement(root,'{'+NS+'}key',{'id':key,'for':'all','attr.name':key,'attr.type':'string'})
g=ET.SubElement(root,'{'+NS+'}graph',{'id':'kupol-logs','edgedefault':'directed'})
for n in graph['nodes']:
 el=ET.SubElement(g,'{'+NS+'}node',{'id':n['id']})
 for key in ['label','kind']:ET.SubElement(el,'{'+NS+'}data',{'key':key}).text=n[key]
for i,e in enumerate(graph['edges']):
 el=ET.SubElement(g,'{'+NS+'}edge',{'id':'e'+str(i),'source':e['source'],'target':e['target']})
 ET.SubElement(el,'{'+NS+'}data',{'key':'relation'}).text=e['relation']
 ET.SubElement(el,'{'+NS+'}data',{'key':'metadata'}).text=json.dumps({k:v for k,v in e.items() if k not in ['source','target','relation']},ensure_ascii=False)
ET.ElementTree(root).write(OUT/'Граф_знаний_по_логам.graphml',encoding='utf-8',xml_declaration=True)
assert all(e['source'] in entities and e['target'] in entities for e in relations.values())
assert len(data_nodes)==36 and all(n['snapshots']==1440 for n in data_nodes.values())
summary={'telemetry_nodes':len(data_nodes),'telemetry_field_paths':sum(len(n['fields']) for n in data_nodes.values()),'record_types':len(record_types),'observed_action_groups':len(actions),'graph_vertices':len(entities),'graph_edges':len(relations),'null_only_telemetry_fields':sum(not s['non_null_type_known'] for n in data_nodes.values() for s in n['fields'])}
print(json.dumps(summary,ensure_ascii=False,indent=2))
