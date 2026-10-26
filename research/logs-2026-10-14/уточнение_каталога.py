import json
from pathlib import Path

ROOT=Path(__file__).parent
p=ROOT/'Каталог_узлов_по_API.json'
bundle=json.loads(p.read_text(encoding='utf-8'))
nodes=bundle['nodes']
actions={
 'solar_panels_01':'enable_mppt disable_mppt enable_mppt_1 disable_mppt_1 enable_all_mppt disable_all_mppt',
 'solar_inverter_01':'set_mode_hybrid set_mode_grid_only enable_mppt disable_mppt start_cell_balancing cancel_balancing reset_error set_fan',
 'battery_01':'',
 'wind_turbine_01':'turn_on turn_off',
 'dizel_1':'turn_on turn_off preheat',
 'fuel_tank_01':'transfer_fuel',
 'smart_panel_01':'line_on line_off reset_protection reset_line_error',
 'printer_3d_01':'home start_job resume upload_file delete_file set_speed move',
 'cnc_01':'home start_job resume move set_zero probe_z',
 'material_inventory_01':'',
 'equipment_cooling_01':'turn_on turn_off reset_alarm set_speed',
 'thermal_insulation_01':'set_fan_pwm set_lock set_vacuum set_poll_interval',
 'thermal_insulation_02':'set_fan_pwm set_relay_fan_level set_lock',
 'climate_sensor_01':'',
 'climate_sensor_02':'recalibrate',
 'climate_sensor_03':'recalibrate',
 'air_quality_sensor_01':'',
 'fume_extraction_01':'turn_on turn_off reset_alarm',
 'supply_ventilation_01':'turn_on turn_off reset_alarm set_speed set_mode',
 'dome_sealing_01':'open_valves close_valves reset_error seal_zone',
 'smoke_detector_01':'reset_alarm',
 'smoke_detector_02':'reset_alarm',
 'fire_suppression_01':'manual_start_zone stop reset_alarm block_automation unblock_automation',
 'access_control_01':'lock_door unlock_door lockdown reset_alarm',
 'radiation_sensor_01':'reset_alarm set_thresholds',
 'chem_sensor_01':'reset_alarm set_thresholds',
 'weather_station_01':'',
 'seysmo_01':'set_threshold',
 'water_pump_01':'turn_on turn_off reset_alarm set_power_limit',
 'water_tank_01':'',
 'water_filter_01':'switch_to_reserve force_flush reset_warning',
 'network_link_01':'',
 'radio_01':'set_frequency set_protocol',
 'backup_comms_01':'switch_channel set_protocol set_traffic_priority',
 'edge_compute_01':'restart_service limit_load sync_time reboot',
 'dome_automation_01':'set_data_trust set_primary_sensor reset_interlock block_interlock unblock_interlock acknowledge set_ventilation_auto set_diesel_autostart',
 'meeting_01':'post_message'
}
assert set(nodes)==set(actions)
nodes['dome_automation_01']['documented_fields']='mode interlocks data_trust events primary_co2_sensor ventilation_auto diesel_autostart line_current_limits_a autonomy_forecast_h'.split()
extra_values={
 ('solar_panels_01','enable_mppt'):'Номер канала 1..N',('solar_panels_01','disable_mppt'):'Номер канала 1..N',
 ('dizel_1','preheat'):'Без value; подогрев 2 минуты',
 ('fuel_tank_01','transfer_fuel'):'Литры; без value — до полного',
 ('equipment_cooling_01','set_speed'):'Число 0..100, проценты',
 ('climate_sensor_02','recalibrate'):'Без value; обнуление дрейфа',
 ('climate_sensor_03','recalibrate'):'Без value; обнуление дрейфа',
 ('radiation_sensor_01','set_thresholds'):'Объект {warning: число, alarm: число}; разрешено указать одно поле; единица радиационного показателя мкЗв/ч',
 ('chem_sensor_01','set_thresholds'):'Объект {warning: число, alarm: число}; разрешено указать одно поле; единица ppm',
 ('seysmo_01','set_threshold'):'Число 0.01..1000, мм/с',
 ('water_pump_01','set_power_limit'):'Число 20..100, проценты',
 ('radio_01','set_frequency'):'Число 430..440, МГц',
 ('radio_01','set_protocol'):'AX25 | FSK | LORA | DMR',
 ('meeting_01','post_message'):'Строка или {text: строка до 4000 символов, to?: адресат до 64 символов, name?: имя агента}; не больше 10 сообщений за 10 с'
}
sensor_ids='climate_sensor_01 climate_sensor_02 climate_sensor_03 air_quality_sensor_01 smoke_detector_01 smoke_detector_02 radiation_sensor_01 chem_sensor_01 weather_station_01 seysmo_01'.split()
for node_id,commands in actions.items():
 entry=nodes[node_id]
 entry['canonical_controls_documented']=commands.split()
 entry['control_value_specifications']={}
 for command in commands.split():
  matching=next((row for row in entry['action_rows'] if command in row['actions_and_aliases']),None)
  if (node_id,command) in extra_values:
   spec=extra_values[(node_id,command)]
  elif matching:
   spec=matching['value_specification_markdown']
  elif command in ['turn_on','turn_off','reset_alarm','reset_warning','force_flush','switch_to_reserve','recalibrate']:
   spec='Дополнительное значение в разделе API не указано; см. specification_markdown'
  else:
   spec='См. specification_markdown и общий раздел правил API'
  entry['control_value_specifications'][command]=spec
 entry['common_sensor_fields']=['sensor_state','confidence'] if node_id in sensor_ids else []
 entry['queries_documented']=({'list_files':{'validate':True} if node_id=='cnc_01' else {}} if node_id in ['printer_3d_01','cnc_01'] else {'read_messages':{'since_id':'optional id','limit':'1..200; default 50'}} if node_id=='meeting_01' else {})
 entry['runtime_permissions']='Not queried. Require current describe_nodes before control; the list here is static documentation.'

bundle['protocol']={'transport':'WebSocket','host':'Provided by organizers; not inferred from chatbot HTTP endpoint','default_port':8765,'auth_first_message':{'type':'auth','key':'<DOME_KEY>'},'auth_timeout_s':10,'roles':['team','guest'],'telemetry_push_s':2,'simulation_update_approx_s':4,'rate_limits':{'commands_and_queries_per_second_max':5,'meeting_messages_per_10_seconds_max':10},'request_id':'Client correlation id echoed in replies','control_success_semantics':'Accepted by server; execution must be verified using telemetry','authoritative_runtime_catalog_request':{'type':'describe_nodes','request_id':'catalog-1'}}
bundle['global_rules']={'agent_forbidden_production_actions':['pause','abort','arbitrary_gcode'],'resume_failed_attempts_to_alarm':3,'production_alarm_seconds':1200,'printer_job_cancelled_on_resume_lockout':True,'line_max_switches_in_window':3,'line_switch_window_s':120,'line_lockout_s':300,'cnc_probe_parameters':{'distance':{'min':0.1,'max':30,'default':10,'unit':'mm'},'feed':{'min':1,'max':300,'default':50,'unit':'mm/min'},'plate_thickness':{'min':0,'max':30,'default':0,'unit':'mm'},'retract':{'min':0,'max':20,'default':2,'unit':'mm'},'second_pass_feed':{'min':1,'max':100,'unit':'mm/min','required':False},'latch_distance':{'min':0.1,'max':5,'default':1,'unit':'mm'}},'cnc_move':{'axis_travel_mm':{'x':300,'y':180,'z':80},'mode':['relative','absolute'],'feedrate':{'min':10,'max':5000,'default':1000,'unit':'mm/min'}},'printer_move':{'relative_max_abs_mm_per_axis':50,'requires_homed':True,'forbidden_while_printing':True},'printer_speed_pct':{'min':50,'max':200},'printer_upload_max_bytes':16*1024*1024,'hardware_interlocks':'Independent hardware protections must not be bypassed; software PLC actions are distinct permissions, checked at runtime.'}
bundle['interlocks']=[
 {'id':'IL_CO2','condition':'primary CO2 > 2000 ppm','duration_s':60,'effects':['hold printer','hold CNC','supply ventilation 100%']},
 {'id':'IL_CONDENSATE','condition':'RH > 90% OR air temperature minus dew point < 1 C','duration_s':60,'effects':['hold CNC']},
 {'id':'IL_SMOKE','condition':'zone smoke detector in alarm','effects':['FABLAB: hold machines','FABLAB: line 1 off','after 60 s without acknowledgment: fire suppression in zone'],'unacknowledged_delay_s':60},
 {'id':'IL_LOAD_SHED','condition':'SOC < 25% OR autonomy forecast < 1 h OR power deficit','effects':['shed lines in order 8, 1, then 3, 4','diesel autostart']},
 {'id':'IL_STORM','condition':'wind > 20 m/s','duration_s':30,'effects':['wind turbine off','valves closed','recirculation']},
 {'id':'IL_OVERVOLT','condition':'line voltage > 245 V','duration_s':120,'effects':['affected line off']},
 {'id':'IL_INV_OVERLOAD','condition':'inverter error E07','duration_s':120,'effects':['lines 8 and 1 off']},
 {'id':'IL_HEAT','condition':'inverter temperature > 75 C','duration_s':120,'effects':['line 1 off']},
 {'id':'IL_OVERCURRENT','condition':'line current > 90% of configured current limit','duration_s':120,'effects':['affected line off']}
]
history=json.loads((ROOT/'Анализ_исторических_журналов.json').read_text(encoding='utf-8'))
snapshot=json.loads((ROOT/'Купол_состояния_всех_нод.json').read_text(encoding='utf-8'))
for node_id,entry in nodes.items():
 h=history['observed_nodes'].get(node_id)
 entry['historical_field_inventory']={'source':'Logs/2026-10-14/telemetry.jsonl','historical_only':True,**h} if h else None
 current=snapshot['nodes'].get(node_id)
 entry['chatbot_observation']={'source':'chatbot','received_at_utc':current['received_at_utc'],'independently_verified':False,'state':current['state']} if current else None
 entry['current_websocket_telemetry']=None
 entry['current_websocket_telemetry_reason']='No connection to the competition WebSocket server was made.'
bundle['notes']=['Catalog of 37 documented nodes. This is not a live describe_nodes response.','canonical_controls_documented and control_value_specifications describe the documented agent API; operator actions and hardware safety are separate.','specification_markdown preserves original conditions and aliases. General rule sections still apply.','historical_field_inventory contains observed field names/types over 1440 past snapshots; these are not current values.','chatbot_observation contains the earlier sequential 36-node survey; the chatbot is a different interface and its schemas disagree with this API.','current_websocket_telemetry is null for every node; no values or permissions have been fabricated.']
p.write_text(json.dumps(bundle,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
print('Nodes',len(nodes),'Canonical control entries',sum(len(e['canonical_controls_documented']) for e in nodes.values()))
print('Documented field entries',sum(len(e['documented_fields'])+len(e['common_fields']) for e in nodes.values()))
print('Chatbot observations',sum(e['chatbot_observation'] is not None for e in nodes.values()))
