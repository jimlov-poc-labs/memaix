// SPDX-License-Identifier: AGPL-3.0-or-later
// Shared web-UI utilities (FEATURE-WEB-UI-FOUNDATION.md §4.2).
// Vanilla ES2022, no dependencies, no bundler. DOM is built with
// createElement/textContent — never innerHTML of uncontrolled data.

async function api(method, path, body = null) {
  const opts = { method, headers: { 'Content-Type': 'application/json' },
                 credentials: 'same-origin' };
  if (body !== null) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  if (res.status === 401) {
    window.location = '/app/login?next=' + encodeURIComponent(location.pathname + location.search);
    return;
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    const e = new Error(err.error || res.statusText);
    e.status = res.status;
    e.payload = err;
    throw e;
  }
  return res.json();
}

function t(key) {
  return (window.I18N && window.I18N[key]) ?? key;
}

function toast(msg, type = 'info') {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  container.append(el);
  setTimeout(() => el.remove(), 4000);
}

function modal(contentEl) {
  const backdrop = document.createElement('div');
  backdrop.className = 'modal-backdrop';
  const box = document.createElement('div');
  box.className = 'modal-box';
  if (typeof contentEl === 'string') {
    // Only for trusted, code-authored markup — user data must arrive as nodes.
    box.innerHTML = contentEl;
  } else {
    box.append(contentEl);
  }
  backdrop.append(box);
  const close = () => { backdrop.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close(); });
  document.addEventListener('keydown', onKey);
  document.body.append(backdrop);
  return { close, box };
}

function mdInlineNode(tok) {
  let tag = 'em', cut = 1;
  if (tok.startsWith('**')) { tag = 'strong'; cut = 2; }
  else if (tok.startsWith('`')) tag = 'code';
  const node = document.createElement(tag);
  node.textContent = tok.slice(cut, -cut);
  return node;
}

// Tokenize **bold**, *italic*, `code` — everything else as plain text.
function mdInline(text) {
  const frag = document.createDocumentFragment();
  const re = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)/g;
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) frag.append(text.slice(last, m.index));
    frag.append(mdInlineNode(m[0]));
    last = m.index + m[0].length;
  }
  if (last < text.length) frag.append(text.slice(last));
  return frag;
}

function mdFlushPara(s) {
  if (!s.para.length) return;
  const p = document.createElement('p');
  p.append(mdInline(s.para.join(' ')));
  s.el.append(p);
  s.para = [];
}

function mdFlushList(s) {
  if (!s.list) return;
  s.el.append(s.list);
  s.list = null;
}

function mdFlushAll(s) {
  mdFlushPara(s);
  mdFlushList(s);
}

function mdCodeLine(s, line) {
  if (line.trim() !== '```') { s.codeBlock.push(line); return; }
  const pre = document.createElement('pre');
  const code = document.createElement('code');
  code.textContent = s.codeBlock.join('\n');
  pre.append(code);
  s.el.append(pre);
  s.codeBlock = null;
}

function mdListItem(s, line, text) {
  mdFlushPara(s);
  if (!s.list) s.list = document.createElement(/^\s*\d+\./.test(line) ? 'ol' : 'ul');
  const item = document.createElement('li');
  item.append(mdInline(text));
  s.list.append(item);
}

function mdBlockLine(s, line) {
  const h = line.match(/^(#{1,3})\s+([^\n]+)/);
  if (h) {
    mdFlushAll(s);
    const heading = document.createElement(`h${h[1].length}`);
    heading.append(mdInline(h[2]));
    s.el.append(heading);
    return;
  }
  if (/^---+\s*$/.test(line)) {
    mdFlushAll(s);
    s.el.append(document.createElement('hr'));
    return;
  }
  const li = line.match(/^\s*(?:[-*]|\d+\.)\s+([^\n]+)/);
  if (li) mdListItem(s, line, li[1]);
  else if (line.trim() === '') mdFlushAll(s);
  else s.para.push(line.trim());
}

function mdLine(s, line) {
  if (s.codeBlock !== null) mdCodeLine(s, line);
  else if (line.trim() === '```') { mdFlushAll(s); s.codeBlock = []; }
  else mdBlockLine(s, line);
}

// Minimal markdown rendering without innerHTML of the markdown itself.
// Supports: #/##/### headings, **bold**, *italic*, `code`, ``` blocks,
// - / * / 1. lists, --- rule, blank-line paragraphs.
function mdView(el, markdown) {
  el.textContent = '';
  const s = { el, list: null, codeBlock: null, para: [] };
  for (const line of String(markdown ?? '').split('\n')) mdLine(s, line);
  mdFlushAll(s);
}

function pollBadge(path, badgeEl, interval = 10_000) {
  if (!badgeEl) return { stop() {} };
  const tick = async () => {
    if (document.visibilityState === 'hidden') return;
    try {
      const data = await api('GET', path);
      const count = data?.pending_outbox ?? 0;
      badgeEl.textContent = count > 0 ? String(count) : '';
      badgeEl.hidden = count === 0;
    } catch { /* network error — show nothing */ }
  };
  tick();
  const id = setInterval(tick, interval);
  return { stop() { clearInterval(id); } };
}

// Relative time for feeds: "just now", "2 m", "3 h", "yesterday", else date.
function relTime(iso) {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const s = Math.floor((Date.now() - then) / 1000);
  if (s < 60) return t('web_time_now');
  if (s < 3600) return `${Math.floor(s / 60)} m`;
  if (s < 86400) return `${Math.floor(s / 3600)} h`;
  if (s < 172800) return t('web_time_yesterday');
  return new Date(iso).toLocaleDateString();
}

// Apply data-i18n attributes once strings are loaded.
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-i18n]').forEach((el) => {
    const key = el.getAttribute('data-i18n');
    if (window.I18N && window.I18N[key]) el.textContent = window.I18N[key];
  });
});
