'use strict';
// Вкладка «Узлы»: все узлы по группам, состояние из последней телеметрии и все управляющие воздействия узла
// из каталога describe_nodes — тот же набор и те же ограничения, что на вкладке «Управление»
// (только controls; операторские действия лишь перечислены; отправка — сервисный профиль и текущий запуск).
(() => {
  const SYSTEMS = {power:'Энергия', climate:'Климат', production:'Производство', safety:'Безопасность', external:'Внешние системы',
    water:'Вода', comms:'Связь', ventilation:'Вентиляция', weather:'Погода', security:'Доступ', compute:'Вычисления',
    automation:'Автоматика', sandbox:'Песочница', other:'Прочее'};
  const ORDER = Object.keys(SYSTEMS);
  const LINES = Array.from({length: 8}, (_, i) => i + 1);
  const NO_VALUE = ['home', 'resume', 'turn_on', 'turn_off', 'reset_alarm', 'reset_error', 'open_valves', 'close_valves', 'stop',
    'block_automation', 'unblock_automation', 'sync_time', 'reboot', 'start_cell_balancing', 'cancel_balancing', 'set_mode_hybrid',
    'set_mode_grid_only', 'enable_mppt', 'disable_mppt', 'disable_all_mppt', 'enable_all_mppt', 'reset_filter', 'switch_to_reserve'];
  // Подсказки значений по справочнику узлов (01_API §8); неизвестное действие получает JSON-поле
  const HINTS = {
    line_on: {t: 'select', opts: LINES}, line_off: {t: 'select', opts: LINES},
    reset_protection: {t: 'select', opts: LINES}, reset_line_error: {t: 'select', opts: LINES},
    start_job: {t: 'file'}, delete_file: {t: 'file'},
    set_speed: {t: 'number', min: 0, max: 100, unit: '%'},
    'printer_3d_01.set_speed': {t: 'number', min: 50, max: 200, unit: '%'},
    'supply_ventilation_01.set_speed': {t: 'number', min: 10, max: 100, unit: '%'},
    set_mode: {t: 'select', opts: ['RECIRCULATION', 'FRESH_AIR', 'MIXED']},
    set_fan: {t: 'select', opts: ['AUTO', 'ON', 'OFF']},
    seal_zone: {t: 'select', opts: ['MAIN', 'FABLAB', 'LIVING', 'STORAGE']},
    manual_start_zone: {t: 'select', opts: ['FABLAB', 'LIVING', 'STORAGE', 'POWER_ROOM']},
    lock_door: {t: 'select', opts: ['MAIN_AIRLOCK', 'FABLAB', 'STORAGE', 'SERVER_ROOM']},
    unlock_door: {t: 'select', opts: ['MAIN_AIRLOCK', 'FABLAB', 'STORAGE', 'SERVER_ROOM']},
    lockdown: {t: 'bool'}, set_ventilation_auto: {t: 'bool'}, set_diesel_autostart: {t: 'bool'},
    switch_channel: {t: 'select', opts: ['SATELLITE', 'LORA', 'MESH']},
    set_traffic_priority: {t: 'select', opts: ['NORMAL', 'TELEMETRY', 'EMERGENCY']},
    set_primary_sensor: {t: 'select', opts: ['climate_sensor_01', 'climate_sensor_02', 'climate_sensor_03']},
    restart_service: {t: 'select', opts: ['agent_runtime', 'telemetry_collector', 'vision_inference', 'api_gateway']},
    limit_load: {t: 'number', min: 10, max: 100, unit: '%'}, set_load_limit: {t: 'number', min: 10, max: 100, unit: '%'},
    set_poll_interval: {t: 'number', min: 50, max: 5000, unit: 'мс'},
    set_frequency: {t: 'number', min: 430, max: 440, unit: 'МГц'},
    set_relay_fan_level: {t: 'number', min: 0, max: 10}, transfer_fuel: {t: 'number', min: 1, unit: 'л (пусто — до полного)'},
    set_vacuum: {t: 'select', opts: [1, 0]},
    set_fan_pwm: {t: 'json', example: '{"fan": 0, "pwm": 128}'},
    set_lock: {t: 'json', example: '{"lock": 0, "locked": true}'},
    'thermal_insulation_02.set_lock': {t: 'bool'},
    move: {t: 'json', example: '{"x": 5, "mode": "relative", "feedrate": 600}'},
    set_zero: {t: 'select', opts: ['xyz', 'xy', 'z']},
    probe_z: {t: 'json', example: '{"distance": 10}', optional: true},
    upload_file: {t: 'json', example: '{"file_name": "part.gcode", "content": "..."}'},
    set_data_trust: {t: 'json', example: '{"node": "climate_sensor_01", "param": "co2_ppm", "trust": "UNRELIABLE"}'},
    block_interlock: {t: 'json', example: '{"id": "IL_SMOKE", "minutes": 10}'},
    reset_interlock: {t: 'text', example: 'IL_CO2'}, unblock_interlock: {t: 'text', example: 'IL_CO2'},
    acknowledge: {t: 'text', example: 'all или id события AUT-0003'},
    set_thresholds: {t: 'json', example: '{"warning": 1000, "alarm": 2000}'},
    set_protocol: {t: 'text', example: 'LORAWAN'},
    enable_mppt: {t: 'number', min: 1, optional: true}, disable_mppt: {t: 'number', min: 1, optional: true},
    post_message: {t: 'json', example: '{"text": "...", "to": "operator"}'},
  };
  const hintFor = (node, name) => HINTS[`${node}.${name}`] || (NO_VALUE.includes(name) && !HINTS[name] ? {t: 'none'} : HINTS[name] || {t: 'json', example: '', optional: true});
  const PRIORITY = /state|mode|alarm|error|fault|hold|lock|enabled|active|running|^on$|status|health|fan|pump/i;
  const SKIP = new Set(['online', 'updated_at', 'status', 'reading_delay_s', 'files']);
  const nodes = new Map(); // node_id -> {card, controlsKey}
  const groups = new Map(); // group id -> {box, grid, count}
  let filtersReady = false;

  const groupOf = (id, entry) => {
    const raw = String(entry?.category || '').toLowerCase();
    if (SYSTEMS[raw]) return raw;
    const bySystem = Object.entries(SYSTEMS).find(([, name]) => name === entry?.system);
    return bySystem ? bySystem[0] : 'other';
  };
  const shortValue = v => typeof v === 'number' ? String(Number(v.toFixed(3))) : v === null ? 'null' : typeof v === 'boolean' ? (v ? 'да' : 'нет') : String(v);
  const healthy = body => body.online === true && body.status === 'OK' && !body.alarm && body.alarm_state !== 'ALARM' && !body.error;
  const age = (body, row) => {
    const t = Date.parse(row.ingested_at); // когда сервер получил снимок (updated_at узла может идти по игровому времени)
    const s = Math.max(0, Math.round((Date.now() - t) / 1000));
    return Number.isFinite(s) ? (s < 90 ? `${s} с назад` : s < 5400 ? `${Math.round(s / 60)} мин назад` : `${Math.round(s / 3600)} ч назад`) : '—';
  };

  function stateRows(body) {
    const flat = flatten(body);
    const entries = Object.entries(flat).filter(([k]) => !SKIP.has(k) && !/^(files|events|recent_messages)/.test(k) && !k.includes('.interlocks'));
    const marked = entries.filter(([k, v]) => PRIORITY.test(k) || typeof v === 'boolean' || typeof v === 'string');
    const numeric = entries.filter(([k, v]) => typeof v === 'number' && !PRIORITY.test(k));
    return [...marked, ...numeric].slice(0, 8);
  }

  function widget(node, name, files) {
    const h = hintFor(node, name);
    if (h.t === 'none') return '<span class="muted tiny">без значения</span>';
    if (h.t === 'select') return `<select class="ctl-value">${h.opts.map(o => `<option>${esc(o)}</option>`).join('')}</select>`;
    if (h.t === 'bool') return '<select class="ctl-value"><option value="true">true</option><option value="false">false</option></select>';
    if (h.t === 'file') return `<select class="ctl-value" data-files="1">${(files || []).map(f => `<option>${esc(f)}</option>`).join('') || '<option value="">нет файлов</option>'}</select>`;
    if (h.t === 'number') return `<input class="ctl-value" type="number" step="any" ${h.min !== undefined ? `min="${h.min}"` : ''} ${h.max !== undefined ? `max="${h.max}"` : ''} placeholder="${esc(`${h.min ?? ''}${h.max !== undefined ? '…' + h.max : ''} ${h.unit || ''}`.trim())}">`;
    return `<input class="ctl-value" type="text" placeholder="${esc(h.example || 'JSON; пусто — без значения')}">`;
  }
  function readValue(row, node, name) {
    const h = hintFor(node, name), el = row.querySelector('.ctl-value');
    if (h.t === 'none' || !el) return null;
    const raw = el.value.trim();
    if (h.t === 'select') {
      const option = h.opts.find(o => String(o) === raw);
      if (option === undefined) throw new Error('Выберите значение');
      return option;
    }
    if (h.t === 'bool') return raw === 'true';
    if (h.t === 'file') { if (!raw) throw new Error('Нет файла для выбора'); return raw; }
    if (h.t === 'number') {
      if (raw === '') { if (h.optional || h.unit?.includes('пусто')) return null; throw new Error('Введите число'); }
      if (!Number.isFinite(Number(raw))) throw new Error('Значение — не число');
      return Number(raw);
    }
    if (h.t === 'text') {
      if (raw === '') return null;
      return name === 'acknowledge' && raw !== 'all' ? {event_id: raw} : raw;
    }
    if (raw === '') { if (h.optional || !h.example) return null; throw new Error('Нужно значение JSON'); }
    try { return JSON.parse(raw); } catch { throw new Error('Значение — не JSON'); }
  }

  function buildCard(id) {
    const card = document.createElement('article');
    card.className = 'node-card node-wide';
    card.dataset.node = id;
    card.innerHTML = `<div class="node-heading"><h3></h3><span class="dot"></span></div><div class="node-title"></div><div class="node-age tiny muted"></div>
      <div class="node-values"></div><details class="node-fields"><summary>Все поля</summary><pre tabindex="0"></pre></details>
      <details class="node-controls"><summary></summary><div class="ctl-body"></div></details>`;
    card.querySelector('h3').textContent = id;
    return card;
  }

  function buildControls(card, id, entry, files) {
    const body = card.querySelector('.ctl-body'), summary = card.querySelector('.node-controls > summary');
    const controls = [...(entry?.controls || [])];
    const operator = entry?.operator_controls || [];
    summary.textContent = controls.length ? `Воздействия · ${controls.length}` : 'Воздействий нет';
    card.querySelector('.node-controls').classList.toggle('hidden', !controls.length && !operator.length);
    body.innerHTML = `${controls.length ? `<div class="ctl-common"><label>Причина<input class="ctl-reason" maxlength="2000" value="Команда человека из веб-панели"></label>
      <label class="checkbox"><input class="ctl-confirm" type="checkbox">Подтверждаю проверку и отправку</label></div>` : ''}
      ${controls.map(name => `<div class="ctl-row" data-action="${esc(name)}"><code title="${esc((entry?.control_aliases?.[name] || []).join(', '))}">${esc(name)}</code>
        <span class="ctl-widget">${widget(id, name, files)}</span><button type="button" class="small ctl-run">Выполнить</button></div>`).join('')}
      ${operator.length ? `<p class="tiny muted">Только Оператор на месте: ${esc(operator.join(', '))}</p>` : ''}
      <pre class="ctl-result hidden"></pre>`;
  }

  function wire(card, id) {
    card.addEventListener('click', event => {
      const run = event.target.closest('.ctl-run');
      if (!run) return;
      const row = run.closest('.ctl-row'), name = row.dataset.action;
      action(run, async () => {
        if (!card.querySelector('.ctl-confirm').checked) throw new Error('Подтвердите выполнение команды');
        if (!isActive()) throw new Error('Выбран другой запуск');
        const content = {node_id: id, action: name, value: readValue(row, id, name), reason: card.querySelector('.ctl-reason').value};
        const signature = JSON.stringify(content);
        if (!card._op || card._op.signature !== signature) card._op = {signature, id: crypto.randomUUID()};
        const result = await api('/control/commands', {method: 'POST', body: {...content, confirmed: true, operation_id: card._op.id}});
        card._op = null;
        card.querySelector('.ctl-confirm').checked = false;
        const out = card.querySelector('.ctl-result');
        out.textContent = `${name}: ${pretty(result)}`;
        out.classList.remove('hidden');
        toast(`${id}.${name}: ${result.status}. ${result.reason || ''}`, ['rejected', 'failed'].includes(result.status));
        await refresh();
      });
    });
  }

  function groupBox(group) {
    let g = groups.get(group);
    if (!g) {
      const box = document.createElement('details');
      box.className = 'node-group';
      box.dataset.key = 'group:' + group;
      box.open = true;
      box.innerHTML = '<summary></summary><div class="node-grid"></div>';
      g = {box, grid: box.querySelector('.node-grid'), title: box.querySelector('summary')};
      groups.set(group, g);
      const host = $('nodeGroups'), at = ORDER.indexOf(group);
      const next = [...host.children].find(el => ORDER.indexOf(el.dataset.key.slice(6)) > at);
      host.insertBefore(box, next || null);
    }
    return g;
  }

  function applyFilters() {
    const text = $('nodeSearch').value.trim().toLowerCase(), bad = $('nodeProblems').checked, acts = $('nodeWithControls').checked;
    for (const group of groups.values()) {
      let shown = 0, problems = 0, total = 0;
      for (const card of group.grid.children) {
        total++;
        const isBad = card.dataset.bad === '1';
        problems += isBad;
        const visible = (!text || card.dataset.search.includes(text)) && (!bad || isBad) && (!acts || card.dataset.controls !== '0');
        card.classList.toggle('hidden', !visible);
        shown += visible;
      }
      group.box.classList.toggle('hidden', !shown);
      group.title.innerHTML = `${esc(group.name)} <span class="muted tiny">${shown === total ? total : `${shown} из ${total}`} узл.${problems ? ` · <b class="bad-text">с проблемами: ${problems}</b>` : ''}</span>`;
    }
  }

  function initFilters() {
    if (filtersReady) return;
    filtersReady = true;
    for (const id of ['nodeSearch', 'nodeProblems', 'nodeWithControls']) $(id).addEventListener('input', applyFilters);
    $('nodeGroupsOpen').onclick = () => document.querySelectorAll('#nodeGroups > details').forEach(d => { d.open = true; });
    $('nodeGroupsClose').onclick = () => document.querySelectorAll('#nodeGroups > details').forEach(d => { d.open = false; });
  }

  window.renderNodesPage = function renderNodesPage(data) {
    initFilters();
    const catalogue = new Map((data.catalogue || []).map(n => [n.node_id, n]));
    const live = data.nodes || [];
    const canSend = state.runtime.profile === 'service' && isActive() && catalogue.size > 0;
    $('nodesNote').textContent = canSend
      ? 'Воздействия проходят те же проверки, что на вкладке «Управление»: каталог, параметры, свежесть данных, Gate.'
      : catalogue.size
        ? 'Прямые команды доступны только в сервисном профиле и для текущего запуска; в competition поставьте задачу агенту в чате.'
        : 'Каталог узлов ещё не получен: воздействия появятся после describe_nodes.';
    $('nodeEmpty').classList.toggle('hidden', live.length > 0);
    const present = new Set();
    for (const row of live) {
      const id = row.node_id, body = row.payload || {}, entry = catalogue.get(id);
      present.add(id);
      let item = nodes.get(id);
      const group = groupOf(id, entry);
      const g = groupBox(group);
      g.name = SYSTEMS[group];
      if (!item) {
        item = {card: buildCard(id), key: null};
        wire(item.card, id);
        nodes.set(id, item);
      }
      const card = item.card;
      if (card.parentElement !== g.grid) g.grid.append(card);
      const bad = !healthy(body);
      card.dataset.bad = bad ? '1' : '0';
      card.dataset.controls = String(entry?.controls?.length || 0);
      card.dataset.search = `${id} ${entry?.title || ''} ${SYSTEMS[group]}`.toLowerCase();
      card.querySelector('.dot').className = 'dot ' + (bad ? 'bad' : 'good');
      card.querySelector('.node-title').textContent = entry?.title || nodeName(id);
      card.querySelector('.node-age').textContent = `${body.online === false ? 'нет связи · ' : ''}${body.status && body.status !== 'OK' ? body.status + ' · ' : ''}данные ${age(body, row)}`;
      const values = stateRows(body).map(([k, v]) => `<div class="node-value"><span>${esc(k)}</span><b>${esc(shortValue(v))}</b></div>`).join('');
      const box = card.querySelector('.node-values');
      if (box.innerHTML !== values) box.innerHTML = values;
      const pre = card.querySelector('.node-fields pre'), text = pretty(body);
      if (pre.textContent !== text) { const top = pre.scrollTop; pre.textContent = text; pre.scrollTop = top; }
      const files = Array.isArray(body.files) ? body.files.filter(f => typeof f === 'string') : [];
      const key = JSON.stringify([entry?.controls, entry?.operator_controls, files]);
      if (item.key !== key) { buildControls(card, id, entry, files); item.key = key; }
      for (const button of card.querySelectorAll('.ctl-run')) button.disabled = !canSend;
      card.classList.toggle('ctl-locked', !canSend);
    }
    for (const [id, item] of nodes) if (!present.has(id)) { item.card.remove(); nodes.delete(id); }
    for (const [name, g] of groups) if (!g.grid.children.length) { g.box.remove(); groups.delete(name); }
    applyFilters();
  };
})();
