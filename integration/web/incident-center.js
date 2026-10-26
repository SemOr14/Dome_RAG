'use strict';
(() => {
  async function incidents() {
    const list=(await api('/control/incidents')).items;
    const active=list.filter(x=>x.status!=='resolved'&&x.current);
    const red=active.filter(x=>x.severity==='red').length;
    $('incidentSummary').textContent=`Уведомления · ${active.length}${red?' · КРИТИЧЕСКИХ: '+red:''}`;
    $('incidentCenter').classList.toggle('has-red',red>0);
    $('incidentCenter').classList.toggle('has-yellow',!red&&active.length>0);
    $('incidentList').innerHTML=list.length?list.slice(0,60).map(x=>`<article class="incident ${x.severity}"><div><b>${esc(x.node_id)} · ${esc(x.reason)}</b><p>${esc(x.required_action)}</p><small>${x.current?'Текущие данные':'Историческое событие, текущий риск не подтверждён'} · ${esc(x.status)} · чат: ${esc(x.delivery)} · ${new Date(x.updated_at*1000).toLocaleString('ru-RU')}</small><details><summary>Факты</summary><pre>${esc(pretty(x.facts))}</pre></details></div><button class="secondary incident-ack" data-id="${esc(x.incident_id)}" ${x.status==='open'?'':'disabled'}>${x.status==='open'?'Принял':x.status==='resolved'?'Устранён':'Принято человеком'}</button></article>`).join(''):empty('Уведомлений пока нет.');
    document.querySelectorAll('.incident-ack').forEach(b=>b.onclick=()=>action(b,async()=>{await api(`/control/incidents/${b.dataset.id}/ack`,{method:'POST'});await incidents();}));
  }
  window.refreshIncidents=incidents;
})();
