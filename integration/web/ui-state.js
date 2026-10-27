'use strict';
// Панель обновляется каждые 4 с и пересобирает блоки через innerHTML. Чтобы ничего не сбрасывалось:
// раскрытые <details>, прокрутка внутри блоков, введённые значения и фокус сохраняются при каждой замене,
// а запись, не меняющая разметку, пропускается. Подключается раньше app.js; в коде страниц менять ничего не нужно.
(() => {
  const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'innerHTML');
  if (!descriptor || !descriptor.set) return;
  const written = new WeakMap(); // элемент -> [последняя записанная строка, разметка после записи]
  const FIELDS = 'input:not([type=file]):not([type=button]):not([type=submit]),select,textarea';
  const SCROLLED = 'pre,.table-wrap,.chat-log,textarea,details';

  const anchor = el => {
    const holder = el.closest('[data-key],[data-node]');
    return holder ? (holder.dataset.key || holder.dataset.node) : '';
  };
  // Ключ не зависит от порядка блоков в списке: якорь — data-key/data-node ближайшего родителя, затем подпись
  function keyed(root, selector, label, visit) {
    const seen = new Map();
    for (const el of root.querySelectorAll(selector)) {
      const base = `${el.tagName}|${anchor(el)}|${label(el)}`;
      const n = seen.get(base) || 0;
      seen.set(base, n + 1);
      visit(el, `${base}#${n}`);
    }
  }
  const detailsLabel = el => ([...el.children].find(c => c.tagName === 'SUMMARY')?.textContent || '').trim().slice(0, 80);
  const fieldLabel = el => el.id || el.name || el.placeholder || el.type || '';
  const scrollLabel = el => el.id || el.className || '';

  function snapshot(root) {
    const open = new Map(), fields = new Map(), scroll = new Map();
    keyed(root, 'details', detailsLabel, (el, key) => open.set(key, el.open));
    keyed(root, FIELDS, fieldLabel, (el, key) => fields.set(key, el.type === 'checkbox' || el.type === 'radio'
      ? {checked: el.checked} : {value: el.value}));
    keyed(root, SCROLLED, scrollLabel, (el, key) => {
      if (el.scrollTop || el.scrollLeft) scroll.set(key, [el.scrollTop, el.scrollLeft]);
    });
    const active = document.activeElement;
    let focus = null;
    if (active && active !== root && root.contains(active) && active.matches(FIELDS)) {
      keyed(root, FIELDS, fieldLabel, (el, key) => {
        if (el === active) focus = {key, start: active.selectionStart, end: active.selectionEnd};
      });
    }
    return {open, fields, scroll, focus, own: [root.scrollTop, root.scrollLeft]};
  }

  function restore(root, snap) {
    if (snap.own[0] || snap.own[1]) { root.scrollTop = snap.own[0]; root.scrollLeft = snap.own[1]; } // сам контейнер с прокруткой
    keyed(root, 'details', detailsLabel, (el, key) => {
      if (snap.open.has(key)) el.open = snap.open.get(key);
    });
    keyed(root, FIELDS, fieldLabel, (el, key) => {
      const saved = snap.fields.get(key);
      if (!saved) return;
      if ('checked' in saved) el.checked = saved.checked;
      else if (el.tagName !== 'SELECT' || [...el.options].some(o => o.value === saved.value)) el.value = saved.value;
      if (snap.focus && snap.focus.key === key) {
        el.focus({preventScroll: true});
        try { if (snap.focus.start !== null) el.setSelectionRange(snap.focus.start, snap.focus.end); } catch (_) { /* тип без выделения */ }
      }
    });
    keyed(root, SCROLLED, scrollLabel, (el, key) => {
      const saved = snap.scroll.get(key);
      if (saved) { el.scrollTop = saved[0]; el.scrollLeft = saved[1]; }
    });
  }

  Object.defineProperty(Element.prototype, 'innerHTML', {
    configurable: true,
    enumerable: descriptor.enumerable,
    get: descriptor.get,
    set(html) {
      const text = String(html);
      const prior = written.get(this);
      // Та же строка и разметка с тех пор не менялась (кроме открытия блоков): ничего не трогаем
      if (prior && prior[0] === text && this.childNodes.length && descriptor.get.call(this).replace(/ open=""/g, '') === prior[1]) return;
      const snap = this.firstElementChild ? snapshot(this) : null;
      descriptor.set.call(this, text);
      if (snap) restore(this, snap);
      written.set(this, [text, descriptor.get.call(this).replace(/ open=""/g, '')]);
    },
  });
})();
