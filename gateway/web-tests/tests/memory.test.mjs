// SPDX-License-Identifier: AGPL-3.0-or-later
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { jsonResponse, loadApp, mockFetch, mountPage, runPage } from './helpers.mjs';

beforeAll(loadApp);

const NOTES = [{ path: 'a.md' }, { path: 'dir/b.md' }];
const tick = (ms = 0) => vi.advanceTimersByTimeAsync(ms);
const $ = (id) => document.getElementById(id);
const links = () => [...document.querySelectorAll('#memory-tree li a')];
const toasts = () => [...document.querySelectorAll('#toast-container .toast')].map((x) => [x.className, x.textContent]);
const bodyOf = (call) => JSON.parse(call.opts.body);

const ownerMe = { projects: ['alpha', 'beta'], role_map: { alpha: 'owner', beta: 'member' } };

async function open({ routes = {}, me = ownerMe, search = '' } = {}) {
  mountPage('memory');
  const tc = document.createElement('div');
  tc.id = 'toast-container';
  document.body.append(tc);
  history.replaceState(null, '', `/app/memory${search}`);
  window.ME = Promise.resolve(me);
  const f = mockFetch({ 'GET /app/api/memory/notes': NOTES, ...routes });
  await runPage('memory');
  await tick();
  return f;
}

const HISTORY = [
  { hash: 'abcdef1234567', message: 'edit <b>x</b>', date: '2020-01-01T00:00:00Z' },
  { hash: '1234567890abc', message: 'init', date: 'not-a-date' },
];

async function openHistory(opts = {}) {
  const f = await open({
    routes: {
      'GET /app/api/memory/note': { content: '# Hej' },
      'GET /app/api/memory/history': HISTORY,
      ...opts.routes,
    },
    me: opts.me,
  });
  links()[0].click();
  await tick();
  $('memory-history-btn').click();
  await tick();
  return f;
}

beforeEach(() => {
  vi.useFakeTimers({ now: new Date('2020-06-01T00:00:00Z') });
  window.I18N = { web_memory_revert: 'Återställ', web_memory_revert_confirm: 'Återställa till', web_memory_reverted: 'Återställd', web_time_now: 'nu' };
});
afterEach(() => vi.useRealTimers());

describe('memory: tree', () => {
  it('does nothing when not logged in', async () => {
    mountPage('memory');
    window.ME = Promise.resolve(null);
    const f = mockFetch({});
    await runPage('memory');
    expect(f.calls).toHaveLength(0);
  });

  it('lists notes as links and hides the empty state', async () => {
    const f = await open();
    expect(links().map((a) => a.textContent)).toEqual(['a.md', 'dir/b.md']);
    expect(links()[0].getAttribute('href')).toBe('#');
    expect($('memory-empty').hidden).toBe(true);
    expect(f.calls[0].url).toBe('/app/api/memory/notes?project=alpha');
  });

  it('shows the empty state when there are no notes', async () => {
    await open({ routes: { 'GET /app/api/memory/notes': [] } });
    expect(links()).toHaveLength(0);
    expect($('memory-empty').hidden).toBe(false);
  });

  it('toasts the error when the note list cannot be loaded', async () => {
    await open({ routes: { 'GET /app/api/memory/notes': () => jsonResponse({ error: 'git trasig' }, 500) } });
    expect(toasts()).toEqual([['toast toast-error', 'git trasig']]);
  });

  it('renders note paths as text, not markup', async () => {
    await open({ routes: { 'GET /app/api/memory/notes': [{ path: '<img src=x onerror=1>.md' }] } });
    expect(document.querySelector('#memory-tree img')).toBeNull();
    expect(links()[0].textContent).toBe('<img src=x onerror=1>.md');
  });

  it('prefers ?project=, then localStorage, then the first project', async () => {
    let f = await open({ search: '?project=my%20proj' });
    expect(f.calls[0].url).toBe('/app/api/memory/notes?project=my%20proj');
    localStorage.setItem('memaix_project', 'beta');
    f = await open();
    expect(f.calls[0].url).toContain('project=beta');
    localStorage.clear();
    f = await open();
    expect(f.calls[0].url).toContain('project=alpha');
  });
});

describe('memory: viewer', () => {
  it('opens a note, renders its markdown, shows the filename and the history button', async () => {
    const f = await open({ routes: { 'GET /app/api/memory/note': { content: '# Rubrik\n\ntext **fet**' } } });
    expect($('memory-history-btn').hidden).toBe(true);
    const ev = new MouseEvent('click', { cancelable: true, bubbles: true });
    links()[1].dispatchEvent(ev);
    await tick();
    expect(ev.defaultPrevented).toBe(true);
    expect(f.calls.at(-1).url).toBe('/app/api/memory/note?project=alpha&path=dir%2Fb.md');
    expect($('memory-filename').textContent).toBe('dir/b.md');
    expect($('memory-history-btn').hidden).toBe(false);
    expect($('memory-view').querySelector('h1, h2, h3').textContent).toBe('Rubrik');
    expect($('memory-view').querySelector('strong').textContent).toBe('fet');
  });

  it('never turns note content into live elements', async () => {
    await open({ routes: { 'GET /app/api/memory/note': { content: '<script>window.__pwned=1</script><img src=x onerror=1>' } } });
    links()[0].click();
    await tick();
    expect($('memory-view').querySelector('script, img')).toBeNull();
    expect($('memory-view').textContent).toContain('<img');
  });

  it('toasts and leaves the viewer untouched when a note fails to load', async () => {
    await open({ routes: { 'GET /app/api/memory/note': () => jsonResponse({ error: 'saknas' }, 404) } });
    links()[0].click();
    await tick();
    expect(toasts()).toEqual([['toast toast-error', 'saknas']]);
    expect($('memory-filename').textContent).toBe('');
    expect($('memory-history-btn').hidden).toBe(true);
  });
});

describe('memory: search', () => {
  const type = (v) => { const i = $('memory-search'); i.value = v; i.dispatchEvent(new Event('input')); };

  it('debounces by 250 ms and sends only the last trimmed, encoded query', async () => {
    const f = await open({ routes: { 'GET /app/api/memory/search': [{ path: 'hit.md' }] } });
    type('a');
    await tick(100);
    type('  råd & dåd ');
    await tick(249);
    expect(f.calls.some((c) => c.key === 'GET /app/api/memory/search')).toBe(false);
    await tick(1);
    const searches = f.calls.filter((c) => c.key === 'GET /app/api/memory/search');
    expect(searches).toHaveLength(1);
    expect(searches[0].url).toBe('/app/api/memory/search?project=alpha&q=r%C3%A5d%20%26%20d%C3%A5d');
    expect(links().map((a) => a.textContent)).toEqual(['hit.md']);
  });

  it('restores the full tree when the query is cleared (whitespace counts as empty)', async () => {
    const f = await open({ routes: { 'GET /app/api/memory/search': [{ path: 'hit.md' }] } });
    type('x');
    await tick(250);
    expect(links()).toHaveLength(1);
    type('   ');
    await tick(250);
    expect(links().map((a) => a.textContent)).toEqual(['a.md', 'dir/b.md']);
    expect(f.calls.filter((c) => c.key === 'GET /app/api/memory/notes')).toHaveLength(2);
    expect(f.calls.filter((c) => c.key === 'GET /app/api/memory/search')).toHaveLength(1);
  });

  it('shows the empty state for a search without hits', async () => {
    await open({ routes: { 'GET /app/api/memory/search': [] } });
    type('nada');
    await tick(250);
    expect($('memory-empty').hidden).toBe(false);
  });

  it('swallows search errors without a toast and keeps the current tree', async () => {
    await open({ routes: { 'GET /app/api/memory/search': () => jsonResponse({ error: 'x' }, 500) } });
    type('boom');
    await tick(250);
    expect(toasts()).toEqual([]);
    expect(links()).toHaveLength(2);
  });
});

describe('memory: history drawer', () => {
  it('does nothing when no note has been opened yet', async () => {
    const f = await open();
    $('memory-history-btn').click();
    await tick();
    expect(f.calls.some((c) => c.key === 'GET /app/api/memory/history')).toBe(false);
    expect($('history-drawer').hidden).toBe(true);
  });

  it('lists commits with short sha, message and relative time, then opens the drawer', async () => {
    const f = await openHistory();
    expect(f.calls.at(-1).url).toBe('/app/api/memory/history?project=alpha&path=a.md');
    expect($('history-filename').textContent).toBe('a.md');
    const rows = [...document.querySelectorAll('#history-list li')];
    expect(rows.map((r) => r.querySelector('.sha').textContent)).toEqual(['abcdef1', '1234567']);
    expect(rows[0].querySelector('.commit-msg').textContent).toBe('edit <b>x</b>');
    expect(rows[0].querySelector('b')).toBeNull();
    expect(rows[0].querySelector('time').textContent).not.toBe('2020-01-01T00:00:00Z');
    expect(rows[1].querySelector('time').textContent).toBe('not-a-date');
    expect($('history-drawer').hidden).toBe(false);
    expect($('history-drawer').hasAttribute('open')).toBe(true);
  });

  it('tolerates entries with missing fields', async () => {
    await openHistory({ routes: { 'GET /app/api/memory/history': [{}] } });
    const row = document.querySelector('#history-list li');
    expect(row.querySelector('.sha').textContent).toBe('');
    expect(row.querySelector('.commit-msg').textContent).toBe('');
  });

  it('replaces the previous list on each open', async () => {
    await openHistory();
    $('memory-history-btn').click();
    await tick();
    expect(document.querySelectorAll('#history-list li')).toHaveLength(2);
  });

  it('closes via the close button', async () => {
    await openHistory();
    $('close-drawer').click();
    expect($('history-drawer').hidden).toBe(true);
    expect($('history-drawer').hasAttribute('open')).toBe(false);
  });

  it('toasts when history fails and does not open the drawer', async () => {
    await openHistory({ routes: { 'GET /app/api/memory/history': () => jsonResponse({ error: 'ingen historik' }, 500) } });
    expect(toasts()).toEqual([['toast toast-error', 'ingen historik']]);
    expect($('history-drawer').hidden).toBe(true);
  });

  describe('revert permissions', () => {
    it.each([
      ['owner', { projects: ['alpha'], role_map: { alpha: 'owner' } }, true],
      ['admin role', { projects: ['alpha'], role_map: { alpha: 'admin' } }, true],
      ['global admin', { projects: ['alpha'], is_admin: true, role_map: {} }, true],
      ['member', { projects: ['alpha'], role_map: { alpha: 'member' } }, false],
      ['no role in project', { projects: ['alpha'], role_map: {} }, false],
    ])('%s -> revert buttons: %s', async (_n, me, shown) => {
      await openHistory({ me });
      expect(document.querySelectorAll('.revert-btn')).toHaveLength(shown ? 2 : 0);
    });

    it('judges the role of the selected project, not of the first one', async () => {
      await openHistory({ me: ownerMe, search: '' });
      expect(document.querySelectorAll('.revert-btn')).toHaveLength(2);
      localStorage.setItem('memaix_project', 'beta');
      await openHistory({ me: ownerMe });
      expect(document.querySelectorAll('.revert-btn')).toHaveLength(0);
    });
  });

  describe('revert flow', () => {
    const start = (routes = {}) => openHistory({ routes: { 'POST /app/api/memory/revert': {}, ...routes } });
    const confirmBtn = () => document.querySelector('.modal-box .btn-danger');

    it('asks for confirmation naming the short sha before sending anything', async () => {
      const f = await start();
      document.querySelectorAll('.revert-btn')[0].click();
      expect(document.querySelector('.modal-box p').textContent).toBe('Återställa till abcdef1?');
      expect(f.calls.some((c) => c.key === 'POST /app/api/memory/revert')).toBe(false);
    });

    it('posts project and full sha, then toasts, closes modal and drawer and reloads the note', async () => {
      const f = await start({ 'GET /app/api/memory/note': { content: 'efter revert' } });
      document.querySelectorAll('.revert-btn')[0].click();
      confirmBtn().click();
      await tick();
      const post = f.calls.find((c) => c.key === 'POST /app/api/memory/revert');
      expect(bodyOf(post)).toEqual({ project: 'alpha', sha: 'abcdef1234567' });
      expect(toasts()).toEqual([['toast toast-success', 'Återställd']]);
      expect(document.querySelector('.modal-backdrop')).toBeNull();
      expect($('history-drawer').hidden).toBe(true);
      expect(f.calls.at(-1).url).toBe('/app/api/memory/note?project=alpha&path=a.md');
      expect($('memory-view').textContent).toContain('efter revert');
    });

    it('keeps the modal and drawer open and toasts when the revert fails', async () => {
      await start({ 'POST /app/api/memory/revert': () => jsonResponse({ error: 'konflikt' }, 409) });
      document.querySelectorAll('.revert-btn')[1].click();
      confirmBtn().click();
      await tick();
      expect(toasts()).toEqual([['toast toast-error', 'konflikt']]);
      expect(document.querySelector('.modal-backdrop')).not.toBeNull();
      expect($('history-drawer').hidden).toBe(false);
    });

    it('does not revert when the modal is dismissed with Escape', async () => {
      const f = await start();
      document.querySelectorAll('.revert-btn')[0].click();
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
      expect(document.querySelector('.modal-backdrop')).toBeNull();
      expect(f.calls.some((c) => c.key === 'POST /app/api/memory/revert')).toBe(false);
    });
  });
});
