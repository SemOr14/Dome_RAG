'use strict';
(() => {
  let exiting=false;
  $('yellowStop').onclick=()=>action($('yellowStop'),async()=>{
    const result=await api('/control/safety/yellow',{method:'POST',body:{operation_id:crypto.randomUUID()}});
    toast(`Жёлтый: ${result.status}. ${result.reason||'Результаты отключения — в журнале и уведомлениях'}`);await refresh();
  });
  $('redStop').onclick=async()=>{
    if(exiting)return;
    exiting=true;state.exiting=true;clearInterval(window.panelRefreshTimer);
    $('redStop').disabled=true;
    try {
      const response=await api('/control/safety/red',{method:'POST'});
      exiting=true;clearInterval(window.panelRefreshTimer);
      document.querySelectorAll('button,input,select,textarea,summary').forEach(el=>{if(el.tagName==='SUMMARY')el.tabIndex=-1;else el.disabled=true;});
      $('notice').textContent=response.message||'Приложение завершается. Для повторного подключения запустите его обычной командой.';
      $('connectionBadge').textContent='ПРИЛОЖЕНИЕ ЗАВЕРШЕНО';
    }catch(e){
      if(/^\d{3}:/.test(e.message)){
        exiting=false;state.exiting=false;$('redStop').disabled=false;
        window.panelRefreshTimer=setInterval(()=>{if(!document.hidden)refresh();},4000);toast(e.message,true);
      }else{$('notice').textContent='Соединение потеряно после запроса завершения. Опрос остановлен; повторный запуск приложения — обычной командой.';}
    }
  };

})();
