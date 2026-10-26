'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pretty = value => JSON.stringify(value, null, 2);
const date = value => value ? new Date(value).toLocaleString('ru-RU', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '—';
const labels = {overview:'Обзор системы', charts:'Графики телеметрии', commands:'Управление', agent:'LLM-агент и чат', database:'База данных'};
const state = {page:'overview', runtime:{}, run:'', overview:null, charts:[], dbCursor:0, dbNext:0, dbItems:[], busy:false, commandOperation:null};
$('token').value = sessionStorage.getItem('dome-api-token') || '';

function toast(message, error=false) {
  const el = $('toast'); el.textContent=message; el.classList.toggle('error', error); el.classList.remove('hidden');
  clearTimeout(toast.timer); toast.timer=setTimeout(()=>el.classList.add('hidden'),6500);
}
async function api(path, options={}) {
  const headers = {...options.headers};
  if ($('token').value) headers.Authorization='Bearer '+$('token').value;
  if(options.body !== undefined) { headers['Content-Type']='application/json'; options.body=JSON.stringify(options.body); }
  const response = await fetch(path,{...options,headers,cache:'no-store'});
  let result; try { result=await response.json(); } catch { throw new Error('Сервер вернул неожиданный ответ'); }
  if(!response.ok) {
    if(response.status===401) $('apiState').textContent='Нужен токен сервиса';
    const detail=typeof result.detail==='string'?result.detail:JSON.stringify(result.detail||result);
    throw new Error(`${response.status}: ${detail}`);
  }
  return result;
}
async function action(button, fn) {
  button.disabled=true;
  try { await fn(); } catch(error) { toast(error.message,true); } finally { button.disabled=false; }
}
const params = obj => new URLSearchParams(Object.entries(obj).filter(([,v])=>v!==null&&v!==undefined&&v!=='')).toString();
const empty = text => `<div class="empty">${esc(text)}</div>`;
function badge(value) {
  const names={accepted:'Принято',rejected:'Отклонено',pending:'Ожидается',ambiguous:'Неоднозначно',sent:'Отправлено',dry_run:'Не отправлено',failed:'Ошибка',operator:'Человек',agent:'Агент'};
  const cls=['accepted','sent'].includes(value)?'good':['rejected','failed'].includes(value)?'bad':'wait';
  return `<span class="badge ${cls}">${esc(names[value]||value||'—')}</span>`;
}
function isActive() {return state.run===state.runtime.run_id;}
function selectOptions(select, items, valueFn=x=>x, labelFn=x=>x) {
  const prior=select.value;
  select.innerHTML=items.map(x=>`<option value="${esc(valueFn(x))}">${esc(labelFn(x))}</option>`).join('');
  if(items.some(x=>String(valueFn(x))===prior)) select.value=prior;
}
function nodeName(node) {return state.overview?.catalogue.find(x=>x.node_id===node)?.title||node;}
function flatten(value, path='', output={}) {
  if(value===null || typeof value!=='object') { if(path) output[path]=value; return output; }
  for(const [k,v] of Object.entries(value)) {
    const next=Array.isArray(value)?`${path}[${k}]`:(path?`${path}.${k}`:k);
    if(next.length<140) flatten(v,next,output);
  }
  return output;
}
async function refresh() {
  if(state.busy)return; state.busy=true;
  try {
    const runtime=await api('/control/status'); state.runtime=runtime;
    if(!state.run)state.run=runtime.run_id;
    const runs=await api('/v1/runs?limit=1000');
    if(!runs.items.some(x=>x.run_id===runtime.run_id))runs.items.unshift({run_id:runtime.run_id});
    selectOptions($('runSelect'),runs.items,x=>x.run_id,x=>x.run_id===runtime.run_id?'Текущий · '+x.run_id:x.run_id);
    $('runSelect').value=state.run;
    const data=await api('/control/overview?'+params({run_id:state.run})); state.overview=data;
    $('apiState').textContent='API подключён · SQLite';
    $('connectionBadge').textContent=runtime.connected?'КУПОЛ ПОДКЛЮЧЁН':runtime.connecting?'ПОДКЛЮЧЕНИЕ…':'НЕТ СОЕДИНЕНИЯ';
    $('connectionBadge').className='badge '+(runtime.connected?'good':'muted');
    $('modeBadge').textContent=runtime.control_enabled?'ЖИВОЕ УПРАВЛЕНИЕ':'ТОЛЬКО ЧТЕНИЕ';
    $('modeBadge').className='badge '+(runtime.control_enabled?'wait':'');
    $('connect').disabled=runtime.connecting;
    $('disconnect').disabled=!runtime.connecting;
    $('notice').textContent=!isActive()?'Вы просматриваете другой запуск. Управление доступно только для текущего.':runtime.last_error?'Ошибка соединения: '+runtime.last_error:runtime.control_enabled?'Реальное управление разрешено настройкой сервера. Каждая команда требует проверок и подтверждения.':'Режим только чтения: команды проходят проверки, но в Купол не отправляются. Подключение выполняется отдельно.';
    $('askAgent').disabled=!runtime.llm_configured || !isActive();
    $('buildPrompt').disabled=!isActive();
    $('autoAnalysis').checked=runtime.auto_analysis;
    if(document.activeElement!==$('agentControl'))$('agentControl').checked=!!runtime.agent_control;
    if(runtime.control_enabled&&!runtime.agent_control)$('modeBadge').textContent='АГЕНТ СОВЕТУЕТ';
    if(document.activeElement!==$('autoInterval')&&runtime.review_s)$('autoInterval').value=Math.round(runtime.review_s);
    renderOverview(data); renderControls(data); renderDecisions(data); updateSelectors(data);
    if(state.page==='charts') await refreshCharts();
    $('lastRefresh').textContent='Обновлено '+new Date().toLocaleTimeString('ru-RU');
    $('runtimeDetails').innerHTML=`<p class="tiny muted">Run: <code>${esc(runtime.run_id)}</code><br>Session: <code>${esc(runtime.session_id||'не создана')}</code><br>Роль: ${esc(runtime.role||'не авторизован')}<br>LLM: ${runtime.llm_configured?'ключ настроен':'ключ не настроен'}<br>Решения: ${esc(runtime.brain||'агент не запущен')}</p>`;
    if(state.page==='agent') await loadChat();
  } catch(error) { $('apiState').textContent='API недоступен'; toast(error.message,true); }
  finally {state.busy=false;}
}
function renderOverview(data) {
  const online=data.nodes.filter(x=>x.payload?.online===true&&x.payload?.status==='OK').length;
  const rejected=data.controls.filter(x=>x.validation.status==='rejected').length;
  const cards=[['СОХРАНЕНО СОБЫТИЙ',data.event_count,'В выбранном запуске'],['УЗЛЫ НА СВЯЗИ',`${online} / ${data.nodes.length}`,'online=true · status=OK'],['ПОСЛЕДНИЕ ВОЗДЕЙСТВИЯ',data.controls.length,'Человек и агент, включая отказы'],['ЛОКАЛЬНЫЕ ОТКАЗЫ',rejected,'Среди последних 30 намерений и команд']];
  $('metrics').innerHTML=cards.map(([label,value,note])=>`<div class="metric"><div class="metric-label">${label}</div><div class="metric-value">${value}</div><small>${note}</small></div>`).join('');
  $('observationTime').textContent=data.nodes.length?'Получено '+date(data.nodes.reduce((a,b)=>a.id>b.id?a:b).ingested_at):'Нет телеметрии';
  renderNodes(data.nodes);
  renderTimeline(data);
}
// Keep the actual details/pre elements mounted across polling: open state and scroll survive.
function renderNodes(nodes) {
  const container=$('nodes');
  if(state.nodesRun!==state.run){container.replaceChildren();state.nodesRun=state.run;}
  if(!nodes.length){if(!container.querySelector('.empty'))container.innerHTML=empty('Телеметрии пока нет.');return;}
  container.querySelector('.empty')?.remove();
  const existing=new Map([...container.children].map(el=>[el.dataset.node,el]));
  for(const node of nodes){
    let card=existing.get(node.node_id);
    if(!card){
      card=document.createElement('article');card.className='node-card';card.dataset.node=node.node_id;
      card.innerHTML='<div class="node-heading"><h3></h3><span class="dot"></span></div><div class="node-title"></div><div class="node-values"></div><details><summary></summary><pre tabindex="0"></pre></details>';
      card.querySelector('h3').textContent=node.node_id;container.append(card);
    }
    existing.delete(node.node_id);
    const body=node.payload||{},good=body.online===true&&body.status==='OK';
    card.querySelector('.dot').className='dot '+(good?'good':'bad');
    card.querySelector('.node-title').textContent=nodeName(node.node_id);
    const entries=Object.entries(flatten(body)).filter(([key])=>!['online','updated_at','status'].includes(key)).slice(0,4);
    const values=entries.map(([k,v])=>`<div class="node-value"><span>${esc(k)}</span><b>${esc(typeof v==='number'?Number(v.toFixed(3)):v===null?'null':v)}</b></div>`).join('');
    if(card.querySelector('.node-values').innerHTML!==values)card.querySelector('.node-values').innerHTML=values;
    card.querySelector('summary').textContent='Все поля · '+date(node.ingested_at);
    const pre=card.querySelector('pre'),text=pretty(body);
    if(pre.textContent!==text){const top=pre.scrollTop,left=pre.scrollLeft;pre.textContent=text;pre.scrollTop=top;pre.scrollLeft=left;}
  }
  for(const card of existing.values())card.remove();
}
// Служебные записи вызова модели и дубли решения — во вкладке «База данных»; решение показывает agent_decision
const TIMELINE_HIDDEN=new Set(['llm_request','llm_response','decision','agent_llm_call','control_dispatch','prompt_preview','llm_analysis_result']);
const SOURCES={llm:'LLM-агент',mixed:'LLM-агент + RAG',rules:'Плейбуки',rag:'RAG',operator:'Оператор'};
const value=v=>v===undefined||v===null?'':esc(typeof v==='string'?v:JSON.stringify(v));
function actionStatus(p){
  const r=p.response||{};
  if(p.rejected)return ['bad','Отклонено Gate',r.error];
  if(p.dry_run)return ['wait','Не отправлено · только чтение',''];
  return r.success?['good','Принято Куполом','']:['bad','Ошибка Купола',r.error];
}
// «НЕ ВЫПОЛНЕНО: a; b. Сообщение агента: «…»» (runner.with_outcome) → список невыполненного и сам текст агента
function splitOutcome(text){
  const m=/^НЕ ВЫПОЛНЕНО: ([\s\S]*?)\.(?: Сообщение агента: «([\s\S]*)»)?$/.exec(text||'');
  return m?{missed:m[1].split('; '),say:m[2]||''}:{missed:[],say:text||''};
}
function delivery(p){
  const r=p.response||{};
  if(p.rejected)return ['bad','не отправлено: '+(r.error||'Gate')];
  if(p.dry_run)return ['wait','не отправлено · только чтение'];
  return r.success?['good','доставлено в чат Купола']:['bad','ошибка доставки: '+(r.error||'')];
}
function messageItem(p,time){
  const {missed,say}=splitOutcome(String(p.request.value?.text??p.request.value??'')),[cls,status]=delivery(p);
  const head=missed.length?'<span class="badge bad">НЕ ВЫПОЛНЕНО</span>':'';
  const list=missed.length?`<ul class="plan missed">${missed.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:'';
  return `<div class="event message-event${missed.length?' not-done':''}">${time}<span><span class="badge good">АГЕНТ</span></span><div><b>Сообщение Оператору</b> ${head}${list}${say?`<p class="what">${esc(say)}</p>`:''}<p class="tiny"><span class="dot ${cls==='good'?'good':cls==='bad'?'bad':''}"></span>${esc(status)}</p></div></div>`;
}
function timelineItem(e,all){
  const p=e.payload||{},time=`<span class="muted">${date(e.ingested_at)}</span>`;
  if(e.event_type==='agent_action'&&p.request?.node_id==='meeting_01')return messageItem(p,time);
  if(e.event_type==='agent_action'&&p.request){
    const req=p.request,dec=all.find(x=>x.event_type==='agent_decision'&&x.payload?.seq===p.ref&&x.payload?.run_id===p.run_id)?.payload;
    const operator=dec?.source==='operator',[cls,status,why]=actionStatus(p);
    const what=`<code>${esc(req.node_id)}.${esc(req.action)}</code> ${value(req.value)}`;
    return `<div class="event control ${operator?'by-operator':'by-agent'}">${time}<span><span class="badge ${operator?'wait':'good'}">${operator?'ОПЕРАТОР':'АГЕНТ'}</span></span><div><b>Управляющее воздействие</b> <span class="badge ${cls}">${esc(status)}</span><p class="what">${what}</p><p>${esc(why||p.reason||'')}</p></div></div>`;
  }
  if(e.event_type==='control_intent'&&e.source==='operator'){
    const c=all.__controls?.find(x=>x.event.event_id===e.event_id),v=c?.validation;
    return `<div class="event control by-operator">${time}<span><span class="badge wait">ОПЕРАТОР</span></span><div><b>Команда Оператора из панели</b> ${v?badge(v.status):''}${e.metadata?.confirmed?' <span class="badge wait">ПОДТВЕРЖДЕНА</span>':''}<p class="what"><code>${esc(p.node_id)}.${esc(p.action)}</code> ${value(p.value)}</p><p>${esc((v?.reasons||[]).join(', ')||e.metadata?.reason||'')}</p></div></div>`;
  }
  if(e.event_type==='agent_decision'){
    const impact={base:'базовое',hidden:'скрытое',cascade:'каскадное',false:'вероятно ложное',unknown:'не определено'};
    const classification=p.impact_kind?` · ${impact[p.impact_kind]||p.impact_kind}`:'';
    const conf=typeof p.confidence==='number'?` · уверенность ${Math.round(p.confidence*100)} %`:'';
    const plan=(p.plan||[]).map(x=>`<li>${esc(x)}</li>`).join(''),ev=(p.evidence||[]).slice(0,4).map(x=>`<li>${esc(x.node||x.detail||'')}${x.param?'.'+esc(x.param):''}${x.value!==undefined?' = '+value(x.value):''}${x.note?' — '+esc(x.note):''}</li>`).join('');
    return `<div class="event decision-event">${time}<span><span class="badge">${esc(SOURCES[p.source]||p.source||'агент')}</span></span><div><b>Решение: ${esc(p.situation||'')}</b><p>Причина: ${esc(p.cause||'—')}${esc(conf+classification)}</p>${p.causal_chain?.length?`<p>${esc(p.causal_chain.join(' → '))}</p>`:''}${plan?`<ul class="plan">${plan}</ul>`:''}${ev?`<details><summary>Основания</summary><ul class="plan">${ev}</ul></details>`:''}${p.rag_refs?.length?`<p class="tiny">RAG: ${esc(p.rag_refs.join(', '))}</p>`:''}</div></div>`;
  }
  const who=e.source==='operator'?'Человек':e.source==='agent'?'Агент':e.source;
  const text=p.summary||p.text||p.reason||p.action||p.event||(typeof p.message==='string'?p.message:p.message?.text)||'';
  return `<div class="event">${time}<span>${esc(who)}</span><div><b>${esc(e.event_type)}</b><p>${esc(text)}</p></div></div>`;
}
// Сообщение агента Оператору показывает одна карточка (agent_action); намерение, запрос, ответ Купола и запись
// message о том же сообщении — служебные дубли (они есть во вкладке «База данных»)
function chatDuplicate(e){
  const p=e.payload||{};
  if(e.event_type==='agent_message')return p.direction==='out';
  return e.source!=='operator'&&['control_intent','control','control_response'].includes(e.event_type)&&(p.action==='post_message'||p.node_id==='meeting_01');
}
function renderTimeline(data){
  const all=Object.assign([...data.events],{__controls:data.controls});
  const shown=data.events.filter(e=>!TIMELINE_HIDDEN.has(e.event_type)&&!chatDuplicate(e)).slice(0,30);
  $('timeline').innerHTML=shown.length?shown.map(e=>timelineItem(e,all)).join(''):empty('События ещё не записаны.');
}
function renderControls(data) {
  $('controls').innerHTML=data.controls.length?`<table><thead><tr><th>Время / автор</th><th>Узел / действие</th><th>Проверка БД</th><th>Отправка / Gate</th><th>Ответ Купола</th></tr></thead><tbody>${data.controls.map(c=>{
    const e=c.event,p=e.payload||{};
    const result=data.events.find(x=>x.event_type==='control_dispatch'&&x.payload?.intent_event_id===e.event_id)?.payload;
    return `<tr><td>${date(e.ingested_at)}<br>${badge(e.source)}</td><td><code>${esc(p.node_id)}<br>${esc(p.action)}</code><div class="muted">${esc(JSON.stringify(p.value))}</div><small>${esc(e.direction)}</small></td><td>${badge(c.validation.status)}<div class="muted">${esc(c.validation.reasons.join(', '))}</div></td><td>${result?badge(result.status):e.direction==='outbound'?'Исходящий запрос':'Намерение'}<div class="muted">${esc(result?.reason||'')}</div></td><td>${badge(c.upstream_status)}<div class="muted">Эффект — в verification</div></td></tr>`;
  }).join('')}</tbody></table>`:empty('Управляющие воздействия ещё не записаны.');
}
function updateSelectors(data) {
  const nodes=data.catalogue.length?data.catalogue:data.nodes.map(x=>({node_id:x.node_id,title:x.node_id,controls:[]}));
  const oldNode=$('commandNode').value;
  selectOptions($('commandNode'),nodes,x=>x.node_id,x=>`${x.node_id} · ${x.title||''}`);
  if($('commandNode').value!==oldNode || !$('commandAction').options.length) updateActions();
  const priorChart=$('chartNode').value;
  selectOptions($('chartNode'),data.nodes,x=>x.node_id,x=>`${x.node_id} · ${nodeName(x.node_id)}`);
  if(priorChart!==$('chartNode').value || !$('chartField').options.length)updateFields().catch(e=>toast(e.message,true));
  $('sendCommand').disabled=!isActive()||!data.catalogue.length;
}
function updateActions() {
  const node=state.overview?.catalogue.find(x=>x.node_id===$('commandNode').value);
  selectOptions($('commandAction'),node?.controls||[]);
  $('operatorActions').textContent=node?.operator_controls?.length?'Физические действия (только вручную на месте): '+node.operator_controls.join(', '):'';
}
async function updateFields() {
  const node=$('chartNode').value;
  if(!node){$('chartField').innerHTML='';return;}
  const result=await api('/control/fields?'+params({run_id:state.run,node_id:node}));
  selectOptions($('chartField'),result.items);
}
function renderDecisions(data) {
  const decisions=data.events.filter(x=>x.event_type==='decision');
  $('decisions').innerHTML=decisions.length?decisions.map(e=>{
    const p=e.payload||{};
    return `<article class="panel decision"><div class="panel-heading"><span class="muted tiny">${date(e.ingested_at)} · ${esc(p.decision_id||e.event_id)}</span>${badge(p.severity||'proposed')}</div><h3>${esc(p.summary||p.text||'Решение агента')}</h3><p class="reason">${esc((p.hypotheses||[]).map(x=>x.description).join('; '))}</p>${p.proposed_controls?.length?`<table><thead><tr><th>Предложение</th><th>Проверка LLM-слоя</th><th></th></tr></thead><tbody>${p.proposed_controls.map((a,i)=>`<tr><td><code>${esc(a.node_id)} / ${esc(a.action)}</code><div>${esc(JSON.stringify(a.value))}</div></td><td>${badge(a.accepted?'accepted':'rejected')}<div class="muted">${esc((a.reasons||[]).join('; '))}</div></td><td><button class="small execute-decision" data-event="${esc(e.event_id)}" data-index="${i}" ${a.accepted&&isActive()?'':'disabled'}>Повторно проверить и выполнить</button></td></tr>`).join('')}</tbody></table>`:'<p class="muted tiny">Управляющих действий не предложено.</p>'}<details><summary>Полная структура решения</summary><pre>${esc(pretty(p))}</pre></details></article>`;
  }).join(''):empty('Решений текущего LLM-слоя пока нет. Запросите анализ новых данных.');
  document.querySelectorAll('.execute-decision').forEach(button=>button.onclick=()=>action(button,async()=>{
    if(!confirm('Повторно проверить и выполнить действие агента? В живом режиме команда будет отправлена.'))return;
    button.dataset.operation ||= crypto.randomUUID();
    const result=await api(`/control/decisions/${button.dataset.event}/execute`,{method:'POST',body:{operation_id:button.dataset.operation,action_index:Number(button.dataset.index)}});
    toast(`Результат: ${result.status}. ${result.reason||''}`); await refresh();
  }));
}
let chartRequest=0;
async function refreshCharts() {
  const request=++chartRequest,run=state.run;
  if(!run)return;
  const result=await api('/control/charts?'+params({run_id:run}));
  const extras=await Promise.all(state.charts.filter(c=>!result.items.some(x=>x.node===c.node&&x.field===c.field)).map(async c=>{
    const series=await api('/control/series?'+params({run_id:run,node_id:c.node,field:c.field,source:result.source,session_id:result.session_id,channel:result.channel}));
    return {...c,title:c.node+' · '+c.field,unit:'',points:series.items,default:false};
  }));
  if(request!==chartRequest||run!==state.run)return;
  const items=[...result.items,...extras],root=$('charts');
  const existing=new Map([...root.children].map(el=>[el.dataset.key,el]));
  items.forEach((c,i)=>{
    const key=JSON.stringify([c.node,c.field]);let card=existing.get(key);
    if(!card){
      card=document.createElement('article');card.className='panel chart';card.dataset.key=key;
      card.innerHTML='<div class="panel-heading"><div class="chart-title"></div><button class="secondary small remove-chart">Убрать</button></div><div class="chart-alert tiny"></div><canvas></canvas><div class="chart-note tiny muted"></div>';
    }
    existing.delete(key);
    card.classList.toggle('chart-anomaly',!!c.anomaly);
    card.querySelector('.chart-title').textContent=c.title+(c.unit?' · '+c.unit:'');
    card.querySelector('.chart-alert').textContent=c.anomaly?'⚠ '+c.anomaly.note:'';
    card.querySelector('.chart-alert').hidden=!c.anomaly;
    card.querySelector('canvas').setAttribute('aria-label',c.node+' '+c.field+(c.anomaly?' · аномалия':''));
    const remove=card.querySelector('button');remove.hidden=c.default||!!c.anomaly;
    remove.onclick=()=>{state.charts=state.charts.filter(x=>x.node!==c.node||x.field!==c.field);refreshCharts().catch(e=>toast(e.message,true));};
    if(root.children[i]!==card)root.insertBefore(card,root.children[i]||null);
    drawChart(card.querySelector('canvas'),c.points,card.querySelector('.chart-note'),!!c.anomaly);
  });
  for(const card of existing.values())card.remove();
  $('chartSummary').textContent=`20 основных графиков · аномалий: ${result.anomalies.length} · проверено числовых рядов: ${result.fields_checked} · ${result.source||'данных пока нет'}${result.catching_up?' · обрабатывается накопленная телеметрия':''}`;
  const kind={base:'Базовое',hidden:'Скрытое',cascade:'Каскадное',false:'Вероятно ложное',unknown:'Не определено'};
  $('impactSummary').textContent=result.impacts.map(x=>`${kind[x.kind]||x.kind}: ${x.cause}. Проверка: ${x.check}`).join(' · ');
}
function drawChart(canvas, points, note, anomaly=false) {
  const w=canvas.clientWidth||400,h=240,ratio=window.devicePixelRatio||1;
  canvas.width=w*ratio;canvas.height=h*ratio;
  const ctx=canvas.getContext('2d');ctx.scale(ratio,ratio);
  const values=points.filter(p=>Number.isFinite(p.value));
  note.textContent='Нет числовых измерений';
  if(!values.length){ctx.fillStyle='#8796ab';ctx.font='12px sans-serif';ctx.fillText('Нет числовых измерений',45,110);return;}
  let min=Math.min(...values.map(p=>p.value)),max=Math.max(...values.map(p=>p.value));
  const pad=(max-min)*.12||Math.abs(max)*.05||1;min-=pad;max+=pad;
  const l=60,r=w-18,t=12,b=h-35;
  ctx.font='10px sans-serif';ctx.lineWidth=1;
  for(let i=0;i<5;i++){const y=t+(b-t)*i/4;ctx.strokeStyle='#263244';ctx.beginPath();ctx.moveTo(l,y);ctx.lineTo(r,y);ctx.stroke();ctx.fillStyle='#8796ab';ctx.fillText(Number((max-(max-min)*i/4).toPrecision(5)).toString(),3,y+3);}
  const times=points.map(p=>Date.parse(p.received_at));const first=times[0],last=times[times.length-1];
  const xFor=i=>first===last?(l+r)/2:l+(times[i]-first)/(last-first)*(r-l);
  const yFor=v=>b-(v-min)/(max-min)*(b-t);
  ctx.strokeStyle=anomaly?'#fff000':'#d8ed83';ctx.lineWidth=2;ctx.beginPath();let gap=true;
  points.forEach((p,i)=>{if(!Number.isFinite(p.value)){gap=true;return;}const x=xFor(i),y=yFor(p.value);if(gap)ctx.moveTo(x,y);else ctx.lineTo(x,y);gap=false;});ctx.stroke();
  if(values.length===1){const i=points.indexOf(values[0]);ctx.fillStyle=anomaly?'#fff000':'#d8ed83';ctx.beginPath();ctx.arc(xFor(i),yFor(values[0].value),3,0,2*Math.PI);ctx.fill();}
  ctx.fillStyle='#8796ab';ctx.fillText(new Date(first).toLocaleTimeString('ru-RU'),l,h-10);ctx.textAlign='right';ctx.fillText(new Date(last).toLocaleTimeString('ru-RU'),r,h-10);
  note.textContent=`${points.length} снимков · последнее: ${values[values.length-1].value}`;
  canvas.onmousemove=event=>{const fraction=Math.max(0,Math.min(1,(event.offsetX-l)/(r-l))),target=first+fraction*(last-first);let best=0;times.forEach((time,i)=>{if(Math.abs(time-target)<Math.abs(times[best]-target))best=i;});const p=points[best];canvas.title=`${date(p.received_at)} · ${p.value===null?'null':p.value}\nИсходная метка: ${p.source_timestamp??'не задана'}`;};
}
async function loadEvents(reset=true) {
  if(reset)state.dbCursor=0;
  const result=await api('/v1/events?'+params({run_id:state.run,after_id:state.dbCursor,limit:40,event_type:$('dbType').value,source:$('dbSource').value,node_id:$('dbNode').value}));
  state.dbItems=result.items;state.dbNext=result.next_after_id;
  $('dbNext').disabled=!result.items.length;
  $('dbEvents').innerHTML=result.items.length?`<table><thead><tr><th>ID / время</th><th>Источник</th><th>Тип</th><th>Узел / запрос</th><th>event_id</th></tr></thead><tbody>${result.items.map((e,i)=>`<tr class="clickable db-row" data-index="${i}"><td>${e.id}<br>${date(e.ingested_at)}</td><td>${esc(e.source)}<br><span class="muted">${esc(e.direction)}</span></td><td>${esc(e.event_type)}</td><td>${esc(e.node_id||'—')}<br><code>${esc(JSON.stringify(e.request_id))}</code></td><td><code>${esc(e.event_id)}</code></td></tr>`).join('')}</tbody></table>`:empty('Событий по выбранным фильтрам нет.');
  document.querySelectorAll('.db-row').forEach(row=>row.onclick=()=>{$('dbDetail').textContent=pretty(result.items[Number(row.dataset.index)]);});
}
async function loadChat() {
  const result=await api('/control/chat?'+params({run_id:state.run}));
  const log=$('chatLog'),bottom=log.scrollHeight-log.scrollTop-log.clientHeight<40;
  const label=m=>m.from==='operator'?(m.via==='panel'?'Оператор → агент · требование':'Оператор (чат Купола) → агент'):m.from==='agent'?'Агент → Оператор · рекомендация':`Команда ${m.team??'?'} (${m.name||'чат Купола'})`;
  log.innerHTML=result.items.length?result.items.map(m=>`<div class="chat-msg ${m.from==='operator'?'own':m.from==='agent'?'agent':''} ${m.not_sent?'not-sent':''}"><small>${esc(label(m))} · ${esc(date(m.time))}${m.not_sent?' · не отправлено в Купол (только чтение)':''}</small>${esc(m.text)}</div>`).join(''):empty('Переписки пока нет. Напишите требование агенту.');
  if(bottom)log.scrollTop=log.scrollHeight;
}
async function loadAudit() {
  const result=await api('/v1/http-exchanges?limit=100');
  $('audits').innerHTML=result.items.length?`<table><thead><tr><th>ID</th><th>Время</th><th>Метод</th><th>Путь</th><th>Статус</th></tr></thead><tbody>${result.items.map(e=>`<tr class="clickable audit-row" data-id="${e.id}"><td>${e.id}</td><td>${date(e.started_at)}</td><td>${esc(e.method)}</td><td>${esc(e.path)}</td><td>${e.status}</td></tr>`).join('')}</tbody></table>`:empty('Запросов пока нет.');
  document.querySelectorAll('.audit-row').forEach(row=>row.onclick=async()=>{try{$('auditDetail').textContent=pretty(await api('/v1/http-exchanges/'+row.dataset.id));$('auditDetail').classList.remove('hidden');}catch(e){toast(e.message,true);}});
}
document.querySelectorAll('nav button').forEach(button=>button.onclick=()=>{
  state.page=button.dataset.page;
  document.querySelectorAll('.page').forEach(el=>el.classList.add('hidden'));
  $('page-'+state.page).classList.remove('hidden');
  document.querySelectorAll('nav button').forEach(el=>el.classList.toggle('active',el===button));
  $('heading').textContent=$('pageName').textContent=labels[state.page];
  if(state.page==='charts')refreshCharts().catch(e=>toast(e.message,true));
  if(state.page==='database')loadEvents().catch(e=>toast(e.message,true));
  if(state.page==='agent')loadChat().catch(e=>toast(e.message,true));
});
$('chatForm').onsubmit=event=>{event.preventDefault();action($('chatSend'),async()=>{
  if(!isActive())throw new Error('Выбран другой запуск');
  const text=$('chatText').value.trim();if(!text)return;
  const result=await api('/control/chat',{method:'POST',body:{operation_id:crypto.randomUUID(),text}});
  const ok=result.status==='queued';
  if(ok)$('chatText').value='';
  toast(ok?'Требование передано агенту, ответ появится в ленте':`${result.status}: ${result.reason||''}`,!ok);await loadChat();
});};
$('login').onclick=()=>{sessionStorage.setItem('dome-api-token',$('token').value);refresh();};
$('refresh').onclick=()=>refresh();
$('runSelect').onchange=async()=>{state.run=$('runSelect').value;state.charts=[];await refresh();if(state.page==='database')loadEvents().catch(e=>toast(e.message,true));};
$('connect').onclick=()=>action($('connect'),async()=>{await api('/control/connect',{method:'POST'});await refresh();});
$('disconnect').onclick=()=>action($('disconnect'),async()=>{await api('/control/disconnect',{method:'POST'});await refresh();});
$('commandNode').onchange=updateActions;
$('chartNode').onchange=()=>updateFields().catch(e=>toast(e.message,true));
$('addChart').onclick=async()=>{if(!$('chartNode').value||!$('chartField').value)return;const chart={node:$('chartNode').value,field:$('chartField').value};if(!state.charts.some(c=>c.node===chart.node&&c.field===chart.field))state.charts.push(chart);try{await refreshCharts();}catch(e){toast(e.message,true);}};
$('commandForm').onsubmit=event=>{event.preventDefault();action($('sendCommand'),async()=>{
  if(!$('confirmControl').checked)throw new Error('Подтвердите выполнение команды');
  if(!isActive())throw new Error('Выбран другой запуск');
  const value=$('commandValue').value.trim()?JSON.parse($('commandValue').value):null;
  const content={node_id:$('commandNode').value,action:$('commandAction').value,value,reason:$('commandReason').value};
  const signature=JSON.stringify(content);
  if(!state.commandOperation||state.commandOperation.signature!==signature)state.commandOperation={signature,id:crypto.randomUUID()};
  const result=await api('/control/commands',{method:'POST',body:{...content,confirmed:$('confirmControl').checked,operation_id:state.commandOperation.id}});
  state.commandOperation=null;$('confirmControl').checked=false;
  $('commandResult').textContent=pretty(result);$('commandResult').classList.remove('hidden');
  toast(`Результат: ${result.status}. ${result.reason||''}`);await refresh();
});};
for(const [id,path] of [['buildPrompt','prompt'],['askAgent','decide']])$(id).onclick=()=>action($(id),async()=>{
  const result=await api('/control/'+path,{method:'POST',body:{trigger:$('trigger').value}});
  $('llmOutput').textContent=pretty(result);$('promptDetails').open=true;
  await refresh();
});
$('applyAgentControl').onclick=()=>action($('applyAgentControl'),async()=>{
  const on=$('agentControl').checked;
  if(on&&!confirm('Разрешить агенту отправлять команды в Купол?'))return;
  await api('/control/agent-control',{method:'POST',body:{enabled:on}});toast(on?'Агент управляет Куполом':'Агент только советует');await refresh();
});
$('applyAuto').onclick=()=>action($('applyAuto'),async()=>{await api('/control/auto',{method:'POST',body:{enabled:$('autoAnalysis').checked,interval_s:Number($('autoInterval').value)}});await refresh();});
$('dbFilters').onsubmit=event=>{event.preventDefault();loadEvents().catch(e=>toast(e.message,true));};
$('dbNext').onclick=()=>{state.dbCursor=state.dbNext;loadEvents(false).catch(e=>toast(e.message,true));};
$('find').onclick=()=>action($('find'),async()=>{$('dbDetail').textContent=pretty(await api('/v1/events/'+encodeURIComponent($('findEvent').value.trim())));});
$('loadAudit').onclick=()=>loadAudit().catch(e=>toast(e.message,true));
$('export').onclick=()=>{const link=document.createElement('a');link.href=URL.createObjectURL(new Blob([pretty(state.dbItems)],{type:'application/json'}));link.download='dome-events.json';link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000);};
refresh();
setInterval(()=>{if(!document.hidden)refresh();},4000);
