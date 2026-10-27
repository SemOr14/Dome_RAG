'use strict';
(() => {
  const files = {}, selections = {}, uploads = {}, results = {};
  const seconds = n => Number.isFinite(n) ? `${Math.floor(n/3600)}:${String(Math.floor(n/60)%60).padStart(2,'0')}:${String(Math.floor(n)%60).padStart(2,'0')}` : 'нет данных';
  const fact = (label,value) => `<div class="production-value"><span>${esc(label)}</span><b>${esc(value ?? 'нет данных')}</b></div>`;
  async function production() {
    const data=await api('/control/production?'+params({run_id:state.run}));
    const focus=document.activeElement?.closest('.production-card');
    if(focus && ['INPUT','SELECT'].includes(document.activeElement.tagName))return;
    $('productionCards').innerHTML=data.items.map(item=>{
      const n=item.node_id,p=item.telemetry||{},c=item.capabilities,j=item.job||{};
      const list=Array.isArray(files[n]||p.files)?(files[n]||p.files):[];
      const values=list.map(x=>typeof x==='string'?{name:x}:x);
      const options=values.map(x=>`<option value="${esc(x.name)}" ${selections[n]===x.name?'selected':''} ${x.valid===false?'disabled':''}>${esc(x.name)}${x.size_bytes?' · '+x.size_bytes+' байт':''}${x.valid===false?' · '+esc(x.error||'невалидный G-code'):''}</option>`).join('');
      const busy=['PRINTING','RUNNING','PAUSED','BUSY','HOMING'].includes(p.state);
      const manual=item.manual_allowed&&isActive();
      const progress=Number.isFinite(p.progress_pct)?Math.min(100,Math.max(0,p.progress_pct)):null;
      const elapsed=n==='printer_3d_01'?p.print_elapsed_s:p.job_elapsed_s;
      const eta=n==='printer_3d_01'?p.print_time_left_s:p.job_time_left_s;
      const measured=j.measured_at ? new Date(j.measured_at*1000).toLocaleTimeString('ru-RU') : 'нет данных';
      return `<article class="panel production-card" data-node="${n}"><div class="panel-heading"><h2>${n==='printer_3d_01'?'3D-принтер':'ЧПУ-фрезер'}</h2><span class="badge ${item.fresh?'good':'wait'}">${item.fresh?'АКТУАЛЬНО':'ИСТОРИЯ / НЕТ СВЕЖИХ ДАННЫХ'}</span></div><p class="tiny muted">${n} · ${date(item.received_at)}</p><div class="production-values">${fact('Состояние',p.state)}${fact('Фактическое состояние',p.real_state)}${fact('Файл',p.file_name)}${fact('Удержание / ошибка',p.hold_reason||p.error_code||p.alarm_code)}${fact('Прогресс',progress===null?null:progress.toFixed(1)+' %')}${fact('Прошло',seconds(elapsed))}${fact('Осталось',seconds(Number.isFinite(eta)?eta:j.eta))}${fact('Источник ETA',Number.isFinite(eta)?'телеметрия':j.eta_source==='estimate'?'оценка по устойчивому прогрессу':j.eta_source||'нет данных')}${fact('Время измерения ETA',measured)}${n==='printer_3d_01'?fact('Сопло / стол, °C',`${p.nozzle_temp_c??'?'} / ${p.bed_temp_c??'?'}`)+fact('Стол свободен',p.bed_clear===true?'Да':p.bed_clear===false?'Нет':'неизвестно'):fact('Рабочие координаты X/Y/Z, мм',`${p.x_mm??'?'} / ${p.y_mm??'?'} / ${p.z_mm??'?'}`)+fact('Машинные координаты, мм',Array.isArray(p.mpos_mm)?p.mpos_mm.join(' / '):null)}</div>${progress!==null?`<progress max="100" value="${progress}" aria-label="Прогресс задания"></progress>`:''}<label>Серверный файл<select class="production-file"><option value="">Выберите один файл</option>${options}</select></label><div class="form-row wrap"><button class="secondary production-list" ${c.live_read?'':'disabled'}>Обновить список</button><button class="production-start" ${manual&&c.start&&!busy?'':'disabled'}>Запустить выбранный</button><button class="secondary production-resume" ${manual&&c.resume&&(p.paused||p.state==='PAUSED')?'':'disabled'}>Продолжить</button><button disabled title="Не предоставляется текущим API Купола">Пауза недоступна</button><button disabled title="Не предоставляется текущим API Купола">Остановка недоступна</button></div><label>Готовый G-code<input class="production-upload" type="file" accept=".gcode,.gco,.gc" ${manual&&c.upload?'':'disabled'}></label>${uploads[n]?`<p class="tiny">Выбран: ${esc(uploads[n].file_name)}</p><button class="production-upload-send" ${manual&&c.upload?'':'disabled'}>Загрузить файл</button>`:''}<p class="tiny muted">${item.limitations.map(esc).join('. ')}${!manual?' · Ручное управление недоступно в текущем профиле/запуске/подключении.':''}</p><details><summary>Последнее действие и результат</summary><pre>${esc(pretty(results[n]||item.last_action||'Нет данных'))}</pre></details></article>`;
    }).join('');
    document.querySelectorAll('.production-card').forEach(card=>{
      const n=card.dataset.node;
      card.querySelector('.production-file').onchange=e=>selections[n]=e.target.value;
      card.querySelector('.production-list').onclick=e=>action(e.target,async()=>{const r=await api(`/control/production/${n}/files?`+params({run_id:state.run}));if(!r.success)throw new Error(r.error||'Список не получен');files[n]=r.result;await production();});
      async function operate(button,actionName,extra={}) {
        const contentHash=extra.content?Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(extra.content)))).map(x=>x.toString(16).padStart(2,'0')).join(''):'';
        const signature=[state.run,n,actionName,extra.file_name||'',state.runtime.session_id,contentHash,data.items.find(x=>x.node_id===n)?.job?.job_id||''].join('|');
        const key='dome-production-operation:'+n+':'+actionName;
        let saved;try{saved=JSON.parse(sessionStorage.getItem(key));}catch{}
        if(saved?.signature!==signature)saved={signature,id:crypto.randomUUID()};
        sessionStorage.setItem(key,JSON.stringify(saved));
        await action(button,async()=>{const result=await api('/control/production/actions',{method:'POST',body:{operation_id:saved.id,run_id:state.run,node_id:n,action:actionName,...extra}});results[n]=result;toast(`${n}: ${result.status}. ${result.reason||'Ответ не заменяет проверку эффекта'}`);await production();});
      }
      card.querySelector('.production-start').onclick=e=>{const name=card.querySelector('.production-file').value;if(!name){toast('Выберите серверный файл',true);return;}operate(e.target,'start_job',{file_name:name});};
      card.querySelector('.production-resume').onclick=e=>operate(e.target,'resume');
      card.querySelector('.production-upload').onchange=async e=>{try{const f=e.target.files[0];if(!f)return;if(f.size>16*1024*1024)throw new Error('Файл больше 16 МБ');uploads[n]={file_name:f.name,content:new TextDecoder('utf-8',{fatal:true}).decode(await f.arrayBuffer())};e.target.blur();await production();}catch(err){toast(err.message,true);}};
      const upload=card.querySelector('.production-upload-send');if(upload)upload.onclick=e=>operate(e.target,'upload_file',uploads[n]);
    });
  }
  window.refreshProduction=production;
})();
