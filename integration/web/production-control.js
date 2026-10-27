'use strict';
window.refreshProductionControl=async()=>{
  if(state.exiting)return;
  window.refreshAgentMode();
  await window.refreshIncidents();
  if(state.page==='production')await window.refreshProduction();
};
