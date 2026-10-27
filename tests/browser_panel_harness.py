"""Страница для проверки веб-панели в настоящем браузере (Chrome/Edge headless).

Берётся настоящий integration/web/index.html с его скриптами; сеть заменена заглушкой fetch с настоящим
фрагментом каталога describe_nodes (tests/fixtures/describe_nodes_real_excerpt.json, ответ Купола 26.10.2026).
Сценарий кликает по вкладкам «События» и «Узлы», раскрывает блоки, обновляет панель и проверяет, что раскрытое
не свернулось, а воздействия отправляются так же, как на вкладке «Управление». Результат лежит в #result.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "integration" / "web"
CATALOGUE = ROOT / "tests" / "fixtures" / "describe_nodes_real_excerpt.json"

STUB = """
<script>
window.__posts = [];
window.__profile = __PROFILE__;
const CATALOGUE = __CATALOGUE__;
const nowIso = new Date().toISOString();
const NODES = [
  {id: 1, node_id: 'printer_3d_01', ingested_at: nowIso, payload: {online: true, status: 'OK', state: 'IDLE', files: ['a.gcode', 'b.gcode'], nozzle_temp_c: 24.5, bed_temp_c: 23, progress_pct: 0, updated_at: 1}},
  {id: 2, node_id: 'supply_ventilation_01', ingested_at: nowIso, payload: {online: true, status: 'OK', state: 'ON', speed_pct: 40, mode: 'MIXED', alarm: false, airflow_m3_h: 243.2}},
  {id: 3, node_id: 'climate_sensor_01', ingested_at: nowIso, payload: {online: true, status: 'OK', co2_ppm: 640, temperature_c: 22.1}},
  {id: 4, node_id: 'smart_panel_01', ingested_at: nowIso, payload: {online: true, status: 'OK', total_power_w: 120, lines: [{state: 'ON'}, {state: 'OFF'}]}},
  {id: 5, node_id: 'battery_01', ingested_at: nowIso, payload: {online: false, status: 'UNKNOWN', soc_pct: 55}},
  {id: 6, node_id: 'dome_automation_01', ingested_at: nowIso, payload: {online: true, status: 'OK', mode: 'AUTO'}},
];
const EVENTS = [
  {id: 10, event_id: 'ev-dec-1', event_type: 'agent_decision', source: 'agent', ingested_at: nowIso, payload: {seq: 1, run_id: 'r', situation: 'Ложный CO2', cause: 'расхождение', confidence: 0.9, source: 'llm', plan: ['пометить датчик'], evidence: [{node: 'climate_sensor_01', param: 'co2_ppm', value: 3000, note: 'x'}]}},
  {id: 11, event_id: 'ev-act-1', event_type: 'agent_action', source: 'agent', ingested_at: nowIso, payload: {ref: 1, run_id: 'r', request: {node_id: 'dome_automation_01', action: 'set_data_trust', value: 1}, response: {success: true}}},
  {id: 12, event_id: 'ev-msg-1', event_type: 'agent_action', source: 'agent', ingested_at: nowIso, payload: {ref: 1, run_id: 'r', request: {node_id: 'meeting_01', action: 'post_message', value: {text: 'Привет'}}, response: {success: true}}},
  {id: 13, event_id: 'ev-x-1', event_type: 'connection', source: 'dome', ingested_at: nowIso, payload: {summary: 'connected'}},
];
let revision = 0;
window.fetch = async (url, options = {}) => {
  const path = new URL(url, 'http://localhost');
  let data = {items: []};
  if (options.method === 'POST') window.__posts.push({path: path.pathname, body: JSON.parse(options.body || 'null')});
  if (path.pathname === '/control/status') data = {run_id: 'r', profile: window.__profile, agent_mode: 'autonomous', algorithms_enabled: true, api_scope: 'full', policy_epoch: 1, control_enabled: true, connected: true, llm_configured: true, review_s: 180};
  if (path.pathname === '/v1/runs') data = {items: [{run_id: 'r'}]};
  if (path.pathname === '/control/overview') {
    revision++;
    data = {runtime: {}, run_id: 'r', event_count: 4, nodes: NODES, events: EVENTS, controls: [], catalogue: Object.values(CATALOGUE)};
  }
  if (path.pathname === '/control/commands') data = {status: 'sent', reason: '', intent_event_id: 'x'};
  if (path.pathname === '/control/incidents') data = {items: []};
  if (path.pathname === '/control/production') data = {run_id: 'r', history: false, items: []};
  return {ok: true, status: 200, json: async () => data};
};
</script>
"""

DRIVER = """
<script>
(async () => {
  const log = [], fail = [];
  const check = (name, ok) => { (ok ? log : fail).push(name); };
  const wait = async (fn, ms = 4000) => { const end = Date.now() + ms; while (Date.now() < end) { try { if (fn()) return true; } catch (_) { /* скрипты ещё грузятся */ } await new Promise(r => setTimeout(r, 25)); } return false; };
  const q = s => document.querySelector(s), qa = s => [...document.querySelectorAll(s)];
  try {
    window.addEventListener('error', e => fail.push('JS-ошибка: ' + e.message));
    await wait(() => state.overview);
    check('обзор загрузился', !!state.overview);
    check('на обзоре 5 последних событий максимум', qa('#timelineMini [data-key]').length <= 5 && qa('#timelineMini [data-key]').length > 0);

    // ---- вкладка «События»
    q('nav button[data-page=events]').click();
    check('вкладка событий открыта', !q('#page-events').classList.contains('hidden'));
    await wait(() => qa('#timeline [data-key]').length > 0);
    check('события отрисованы (решение, воздействие, сообщение, прочее)', qa('#timeline [data-key]').length === 4);
    const det = q('#timeline details'); det.open = true;
    q('#eventSearch').value = ''; await refresh(); await refresh();
    check('раскрытый блок события не свернулся после обновлений', q('#timeline details').open === true);
    q('#eventKind').value = 'message'; q('#eventKind').dispatchEvent(new Event('input'));
    check('фильтр по сообщениям оставляет одно событие', qa('#timeline [data-key]').length === 1);
    q('#eventKind').value = 'all'; q('#eventKind').dispatchEvent(new Event('input'));
    q('#eventSearch').value = 'set_data_trust'; q('#eventSearch').dispatchEvent(new Event('input'));
    check('поиск по тексту находит воздействие', qa('#timeline [data-key]').length === 1);
    q('#eventSearch').value = ''; q('#eventSearch').dispatchEvent(new Event('input'));

    // ---- вкладка «Узлы»
    q('nav button[data-page=nodes]').click();
    await wait(() => qa('#nodeGroups .node-card').length >= 6);
    check('все 6 узлов показаны', qa('#nodeGroups .node-card').length === 6);
    const groupNames = qa('#nodeGroups > details > summary').map(s => s.textContent.trim());
    check('узлы разбиты на группы: ' + groupNames.join(' | '), groupNames.length >= 4);
    const printer = q('#nodeGroups .node-card[data-node=printer_3d_01]');
    check('у принтера видно состояние из телеметрии', printer.querySelector('.node-values').textContent.includes('IDLE'));
    check('воздействия принтера из каталога: ' + qa('#nodeGroups .node-card[data-node=printer_3d_01] .ctl-row').length, qa('#nodeGroups .node-card[data-node=printer_3d_01] .ctl-row').length === 7);
    check('операторские действия только перечислены', printer.textContent.includes('remove_part') && !printer.querySelector('.ctl-row[data-action=remove_part]'));
    const startRow = printer.querySelector('.ctl-row[data-action=start_job]');
    check('start_job предлагает файлы из телеметрии', [...startRow.querySelectorAll('option')].map(o => o.value).join() === 'a.gcode,b.gcode');
    check('датчик без воздействий не показывает панель', q('#nodeGroups .node-card[data-node=climate_sensor_01] .node-controls').classList.contains('hidden'));
    check('узел offline помечен проблемным', q('#nodeGroups .node-card[data-node=battery_01]').dataset.bad === '1' && q('#nodeGroups .node-card[data-node=battery_01] .dot').classList.contains('bad'));

    // раскрытые блоки не сворачиваются
    printer.querySelector('.node-controls').open = true;
    printer.querySelector('.node-fields').open = true;
    q('#nodeGroups > details').open = false;
    const closedGroup = q('#nodeGroups > details');
    printer.querySelector('.ctl-reason').value = 'моя причина';
    await refresh(); await refresh();
    check('блок воздействий остался раскрыт', printer.querySelector('.node-controls').open === true);
    check('блок всех полей остался раскрыт', printer.querySelector('.node-fields').open === true);
    check('свёрнутая группа осталась свёрнутой', closedGroup.open === false);
    check('введённая причина не потерялась', printer.querySelector('.ctl-reason').value === 'моя причина');
    check('карточка не пересоздавалась', q('#nodeGroups .node-card[data-node=printer_3d_01]') === printer);

    // фильтры
    q('#nodeSearch').value = 'вентил'; q('#nodeSearch').dispatchEvent(new Event('input'));
    check('поиск «вентил» оставляет 1 узел', qa('#nodeGroups .node-card:not(.hidden)').length === 1);
    q('#nodeSearch').value = ''; q('#nodeProblems').checked = true; q('#nodeProblems').dispatchEvent(new Event('input'));
    check('«только с проблемами» оставляет battery_01', qa('#nodeGroups .node-card:not(.hidden)').map(c => c.dataset.node).join() === 'battery_01');
    q('#nodeProblems').checked = false; q('#nodeProblems').dispatchEvent(new Event('input'));
    q('#nodeWithControls').checked = true; q('#nodeWithControls').dispatchEvent(new Event('input'));
    check('«только с воздействиями» скрывает датчик', q('#nodeGroups .node-card[data-node=climate_sensor_01]').classList.contains('hidden'));
    q('#nodeWithControls').checked = false; q('#nodeWithControls').dispatchEvent(new Event('input'));
    q('#nodeGroupsOpen').click();
    check('«Раскрыть группы» раскрывает все', qa('#nodeGroups > details').every(d => d.open));

    // отправка воздействия
    const vent = q('#nodeGroups .node-card[data-node=supply_ventilation_01]');
    const speed = vent.querySelector('.ctl-row[data-action=set_speed]');
    const runBtn = speed.querySelector('.ctl-run');
    if (window.__profile === 'service') {
      check('кнопки доступны в service', !runBtn.disabled);
      speed.querySelector('.ctl-value').value = '55';
      runBtn.click();
      await new Promise(r => setTimeout(r, 150));
      check('без подтверждения команда не ушла', !window.__posts.some(p => p.path === '/control/commands'));
      vent.querySelector('.ctl-confirm').checked = true;
      runBtn.click();
      await wait(() => window.__posts.some(p => p.path === '/control/commands'));
      const sent = window.__posts.find(p => p.path === '/control/commands');
      check('команда ушла с нужными полями: ' + JSON.stringify(sent && sent.body), sent && sent.body.node_id === 'supply_ventilation_01' && sent.body.action === 'set_speed' && sent.body.value === 55 && sent.body.confirmed === true && !!sent.body.operation_id);
      const mode = vent.querySelector('.ctl-row[data-action=set_mode]');
      mode.querySelector('.ctl-value').value = 'FRESH_AIR';
      vent.querySelector('.ctl-confirm').checked = true;
      mode.querySelector('.ctl-run').click();
      await wait(() => window.__posts.filter(p => p.path === '/control/commands').length === 2);
      check('set_mode отправил строку', window.__posts.filter(p => p.path === '/control/commands')[1].body.value === 'FRESH_AIR');
      const off = vent.querySelector('.ctl-row[data-action=turn_off]');
      vent.querySelector('.ctl-confirm').checked = true; off.querySelector('.ctl-run').click();
      await wait(() => window.__posts.filter(p => p.path === '/control/commands').length === 3);
      check('turn_off без значения', window.__posts.filter(p => p.path === '/control/commands')[2].body.value === null);
      const line = q('#nodeGroups .node-card[data-node=smart_panel_01] .ctl-row[data-action=line_off]');
      line.querySelector('.ctl-value').value = '8';
      q('#nodeGroups .node-card[data-node=smart_panel_01] .ctl-confirm').checked = true; line.querySelector('.ctl-run').click();
      await wait(() => window.__posts.filter(p => p.path === '/control/commands').length === 4);
      check('line_off отправил число 8', window.__posts.filter(p => p.path === '/control/commands')[3].body.value === 8);
    } else {
      check('кнопки заблокированы вне service', runBtn.disabled);
      check('поясняющая заметка про competition', q('#nodesNote').textContent.includes('сервисном профиле'));
    }
    await refresh();
    check('переключение вкладки назад на обзор не ломает', (q('nav button[data-page=overview]').click(), true));
  } catch (e) { fail.push('исключение: ' + e.message + ' ' + (e.stack || '').slice(0, 300)); }
  document.title = fail.length ? 'FAIL' : 'PASS';
  const pre = document.createElement('pre'); pre.id = 'result';
  pre.textContent = JSON.stringify({passed: log.length, failed: fail, ok: log}, null, 1);
  document.body.appendChild(pre);
})();
</script>
"""



def build(out: Path, profile: str) -> Path:
    """Записать страницу проверки с профилем ``service`` или ``competition``."""
    catalogue = json.loads(CATALOGUE.read_text(encoding="utf-8"))
    html = (WEB / "index.html").read_text(encoding="utf-8").replace('"/ui/', '"' + WEB.as_uri() + "/")
    stub = STUB.replace("__PROFILE__", json.dumps(profile)).replace("__CATALOGUE__", json.dumps(catalogue, ensure_ascii=False))
    html = html.replace("<script src", stub + "<script src", 1).replace("</body>", DRIVER + "</body>")
    out.write_text(html, encoding="utf-8")
    return out
