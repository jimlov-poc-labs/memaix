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

// Minimal markdown rendering without innerHTML of the markdown itself.
// Supports: #/##/### headings, **bold**, *italic*, `code`, ``` blocks,
// - / * / 1. lists, --- rule, blank-line paragraphs.
function mdView(el, markdown) {
  el.textContent = '';
  const lines = String(markdown ?? '').split('\n');
  let list = null, codeBlock = null, para = [];

  const inline = (text) => {
    const frag = document.createDocumentFragment();
    // Tokenize **bold**, *italic*, `code` — everything else as plain text.
    const re = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)/g;
    let last = 0, m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) frag.append(text.slice(last, m.index));
      const tok = m[0];
      let node;
      if (tok.startsWith('**')) { node = document.createElement('strong'); node.textContent = tok.slice(2, -2); }
      else if (tok.startsWith('`')) { node = document.createElement('code'); node.textContent = tok.slice(1, -1); }
      else { node = document.createElement('em'); node.textContent = tok.slice(1, -1); }
      frag.append(node);
      last = m.index + tok.length;
    }
    if (last < text.length) frag.append(text.slice(last));
    return frag;
  };

  const flushPara = () => {
    if (!para.length) return;
    const p = document.createElement('p');
    p.append(inline(para.join(' ')));
    el.append(p);
    para = [];
  };
  const flushList = () => { if (list) { el.append(list); list = null; } };

  for (const line of lines) {
    if (codeBlock !== null) {
      if (line.trim() === '```') {
        const pre = document.createElement('pre');
        const code = document.createElement('code');
        code.textContent = codeBlock.join('\n');
        pre.append(code); el.append(pre); codeBlock = null;
      } else codeBlock.push(line);
      continue;
    }
    if (line.trim() === '```') { flushPara(); flushList(); codeBlock = []; continue; }

    const h = line.match(/^(#{1,3})\s+([^\n]+)/);
    if (h) {
      flushPara(); flushList();
      const el2 = document.createElement(`h${h[1].length}`);
      el2.append(inline(h[2])); el.append(el2); continue;
    }
    if (/^---+\s*$/.test(line)) { flushPara(); flushList(); el.append(document.createElement('hr')); continue; }

    const li = line.match(/^\s*(?:[-*]|\d+\.)\s+([^\n]+)/);
    if (li) {
      flushPara();
      if (!list) list = document.createElement(/^\s*\d+\./.test(line) ? 'ol' : 'ul');
      const item = document.createElement('li');
      item.append(inline(li[1])); list.append(item); continue;
    }
    if (line.trim() === '') { flushPara(); flushList(); continue; }
    para.push(line.trim());
  }
  flushPara(); flushList();
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
