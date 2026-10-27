/* DOM smoke with isolated responses; no browser/network/equipment/model is used. */
const {JSDOM}=require('jsdom');
const fs=require('fs'),assert=require('assert');
let runtime={run_id:'ui-test',session_id:null,agent_mode:'recommendation',algorithms_enabled:true,profile:'competition',api_scope:'history',policy_epoch:1};
const incident={incident_id:'one',dedup_key:'one',severity:'red',reason:'Нужна замена филамента',node_id:'printer_3d_01',required_action:'Замените катушку',status:'open',current:true,facts:{},delivery:'not_sent',updated_at:1};
const prod={run_id:'ui-test',history:true,items:['printer_3d_01','cnc_01'].map(node_id=>({node_id,telemetry:{},fresh:false,capabilities:{},job:null,limitations:['Недоступно текущим подключением']}))};
const html=fs.readFileSync('integration/web/index.html','utf8').replace(/<script[^>]*>[^]*?<\/script>/g,'');
const dom=new JSDOM(html,{url:'http://localhost/ui/',runScripts:'outside-only'});
const w=dom.window,errors=[];
w.addEventListener('error',e=>errors.push(e.message));
w.fetch=async(url,options={})=>{
 let data={items:[]};const path=new URL(url,'http://localhost');
 if(path.pathname==='/control/status')data={...runtime};
 if(path.pathname==='/v1/runs')data={items:[{run_id:'ui-test'}]};
 if(path.pathname==='/control/overview')data={runtime:{...runtime},nodes:[],events:[],controls:[],catalogue:[],event_count:0};
 if(path.pathname==='/control/incidents')data={items:[incident]};
 if(path.pathname==='/control/incidents/one/ack'){incident.status='acknowledged';data=incident;}
 if(path.pathname==='/control/production')data=prod;
 if(path.pathname==='/control/mode'){runtime={...runtime,...JSON.parse(options.body),policy_epoch:runtime.policy_epoch+1};data={...runtime};}
 return {ok:true,status:200,json:async()=>data};
};
const wait=async(fn)=>{for(let i=0;i<50;i++){if(fn())return;await new Promise(r=>setTimeout(r,10));}throw Error('DOM timeout');};
(async()=>{
 w.eval(['integration/web/ui-state.js','integration/web/app.js','integration/web/nodes.js','integration/web/agent-modes.js','integration/web/incident-center.js','integration/web/production.js','integration/web/safety-controls.js','integration/web/production-control.js'].map(file=>fs.readFileSync(file,'utf8')).join('\n'));
 await wait(()=>w.document.getElementById('policyStatus').textContent.includes('Рекомендательный'));
 w.document.querySelector('nav button[data-page=production]').click();
 await wait(()=>w.document.querySelectorAll('.production-card').length===2);
 assert(w.document.querySelector('.production-card').textContent.includes('3D-принтер'));
 assert([...w.document.querySelectorAll('.production-start')].every(x=>x.disabled));
 const menu=w.document.getElementById('offMenu');
 menu.dispatchEvent(new w.MouseEvent('mouseenter'));assert(menu.open);
 menu.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));assert(!menu.open);
 menu.dispatchEvent(new w.MouseEvent('mouseenter'));
 w.document.getElementById('allOff').click();
 await wait(()=>!runtime.algorithms_enabled);
 await wait(()=>w.document.getElementById('policyStatus').textContent.includes('Алгоритмы: выкл'));
 assert(w.document.querySelector('.has-red'));
 w.document.querySelector('.incident-ack').click();
 await wait(()=>incident.status==='acknowledged');
 assert(incident.severity==='red');
 w.document.getElementById('autonomousMode').click();
 await wait(()=>runtime.agent_mode==='autonomous'&&runtime.algorithms_enabled);
 assert.deepStrictEqual(errors,[]);
 console.log('UI smoke passed: two cards, disabled history controls, menu hover/Escape/click, mode switches, red incident ack.');
 dom.window.close();
})().catch(e=>{console.error(e);dom.window.close();process.exitCode=1;});
