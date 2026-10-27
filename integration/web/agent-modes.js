'use strict';
(() => {
  const modeName = {off:'Выключен',recommendation:'Рекомендательный',autonomous:'Автономный'};
  const menu = $('offMenu');
  menu.addEventListener('mouseenter',()=>{menu.open=true;});
  menu.addEventListener('mouseleave',()=>{if(!menu.contains(document.activeElement))menu.open=false;});
  menu.addEventListener('focusin',()=>{if(!menu.dataset.escaping)menu.open=true;});
  menu.addEventListener('focusout',e=>{if(!menu.contains(e.relatedTarget))menu.open=false;});
  menu.addEventListener('keydown',e=>{if(e.key==='Escape'){menu.dataset.escaping='1';menu.open=false;menu.querySelector('summary').focus();delete menu.dataset.escaping;}if(e.key==='ArrowDown'){e.preventDefault();menu.open=true;menu.querySelector('button').focus();}});
  async function changeMode(button,agent_mode,algorithms_enabled) {
    await action(button,async()=>{await api('/control/mode',{method:'POST',body:{agent_mode,algorithms_enabled}});menu.open=false;await refresh();});
  }
  $('recommendationMode').onclick=()=>changeMode($('recommendationMode'),'recommendation',true);
  $('autonomousMode').onclick=()=>changeMode($('autonomousMode'),'autonomous',true);
  $('aiOff').onclick=()=>changeMode($('aiOff'),'off',true);
  $('allOff').onclick=()=>changeMode($('allOff'),'off',false);
  window.refreshAgentMode=()=>{
    const r=state.runtime;
    $('policyStatus').textContent=`ИИ: ${modeName[r.agent_mode]||r.agent_mode} · Алгоритмы: ${r.algorithms_enabled?'вкл':'выкл'} · ${r.profile} · API: ${r.api_scope}`;
    $('recommendationMode').classList.toggle('selected-mode',r.agent_mode==='recommendation');
    $('autonomousMode').classList.toggle('selected-mode',r.agent_mode==='autonomous');
  };

})();
