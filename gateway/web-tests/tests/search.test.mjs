// SPDX-License-Identifier: AGPL-3.0-or-later
import { beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { flush, jsonResponse, loadApp, mockFetch, mountPage, runPage } from './helpers.mjs';

beforeAll(loadApp);

async function open({ routes = {}, search = '', me = { projects: ['alpha'] } } = {}) {
  mountPage('search');
  const c = document.createElement('div');
  c.id = 'toast-container';
  document.body.append(c);
  history.replaceState(null, '', `/app/search${search}`);
  window.ME = Promise.resolve(me);
  const f = mockFetch(routes);
  await runPage('search');
  await flush();
  return f;
}

const submit = async (q) => {
  document.getElementById('search-q').value = q;
  const ev = new Event('submit', { cancelable: true });
  document.getElementById('search-form').dispatchEvent(ev);
  await flush();
  return ev;
};
const hits = () => [...document.querySelectorAll('#search-results li')];
const body = (results, extra = {}) => ({ 'GET /app/api/search': { results, projects_searched: ['alpha', 'beta'], ...extra } });

describe('search page', () => {
  beforeEach(() => { window.I18N = { web_search_hits: 'träffar', web_search_hypothesis: 'hypotes' }; });

  it('does nothing when not logged in', async () => {
    mountPage('search');
    window.ME = Promise.resolve(null);
    const f = mockFetch({});
    await runPage('search');
    document.getElementById('search-form').dispatchEvent(new Event('submit', { cancelable: true }));
    await flush();
    expect(f.calls).toHaveLength(0);
  });

  it('does not query without ?q=', async () => {
    const f = await open();
    expect(f.calls).toHaveLength(0);
  });

  it('runs the ?q= query on load, fills the input, and renders cited hits', async () => {
    const f = await open({
      search: '?q=hello%20world',
      routes: body([{ project: 'alpha', source_type: 'memory', ref: 'notes/a.md', title: 'Rubrik', snippet: 'utdrag' }]),
    });
    expect(f.calls[0].url).toBe('/app/api/search?q=hello%20world&limit=20');
    expect(document.getElementById('search-q').value).toBe('hello world');
    expect(hits()).toHaveLength(1);
    expect(hits()[0].className).toBe('search-hit');
    expect(hits()[0].querySelector('strong').textContent).toBe('Rubrik');
    expect(hits()[0].querySelector('.muted').textContent).toBe(' — alpha · memory · notes/a.md');
    expect(hits()[0].lastElementChild.textContent).toBe('utdrag');
    expect(document.getElementById('search-meta').textContent).toBe('1 träffar · alpha, beta');
    expect(document.getElementById('search-empty').hidden).toBe(true);
  });

  it('falls back to ref as title and empty snippet; tolerates missing projects_searched', async () => {
    await open({
      search: '?q=x',
      routes: { 'GET /app/api/search': { results: [{ project: 'p', source_type: 'file', ref: 'r.md', title: '' }] } },
    });
    expect(hits()[0].querySelector('strong').textContent).toBe('r.md');
    expect(hits()[0].lastElementChild.textContent).toBe('');
    expect(document.getElementById('search-meta').textContent).toBe('1 träffar · ');
  });

  it('marks only hypothesis-status hits with a warning badge', async () => {
    await open({
      search: '?q=x',
      routes: body([
        { project: 'p', source_type: 'memory', ref: 'a', title: 'A', status: 'hypotes' },
        { project: 'p', source_type: 'memory', ref: 'b', title: 'B', status: 'verifierad' },
        { project: 'p', source_type: 'memory', ref: 'c', title: 'C' },
      ]),
    });
    const badge = (i) => hits()[i].querySelector('div > span:last-child');
    expect(hits()[0].firstElementChild.textContent).toContain('⚠ hypotes');
    expect(hits()[1].firstElementChild.textContent).not.toContain('⚠');
    expect(hits()[2].firstElementChild.textContent).not.toContain('⚠');
    expect(badge(0).textContent).toBe(' ⚠ hypotes');
  });

  it('renders user data as text, never as elements', async () => {
    await open({
      search: '?q=x',
      routes: body([{ project: '<u>p</u>', source_type: 's', ref: 'r', title: '<img src=x onerror=alert(1)>', snippet: '<script>1</script>' }]),
    });
    expect(hits()[0].querySelector('img, script, u')).toBeNull();
    expect(hits()[0].querySelector('strong').textContent).toBe('<img src=x onerror=alert(1)>');
    expect(hits()[0].lastElementChild.textContent).toBe('<script>1</script>');
  });

  it('shows the empty state and no meta when there are no results (or results missing)', async () => {
    await open({ search: '?q=x', routes: body([]) });
    expect(document.getElementById('search-empty').hidden).toBe(false);
    expect(document.getElementById('search-meta').textContent).toBe('');
    document.body.innerHTML = '';
    await open({ search: '?q=x', routes: { 'GET /app/api/search': {} } });
    expect(document.getElementById('search-empty').hidden).toBe(false);
    expect(hits()).toHaveLength(0);
  });

  it('submit prevents default, trims, encodes, updates the URL and replaces old results', async () => {
    const f = await open({ routes: { 'GET /app/api/search': (url) => jsonResponse({ results: [{ project: 'p', source_type: 's', ref: url.includes('foo') ? 'one' : 'two', title: 'T' }] }) } });
    const ev = await submit('  foo&bar  ');
    expect(ev.defaultPrevented).toBe(true);
    expect(f.calls[0].url).toBe('/app/api/search?q=foo%26bar&limit=20');
    expect(location.search).toBe('?q=foo%26bar');
    expect(hits()).toHaveLength(1);
    await submit('baz');
    expect(hits()).toHaveLength(1);
    expect(hits()[0].querySelector('.muted').textContent).toContain('two');
  });

  it('an empty submit clears results and meta without a request', async () => {
    const f = await open({ search: '?q=x', routes: body([{ project: 'p', source_type: 's', ref: 'r', title: 'T' }]) });
    expect(hits()).toHaveLength(1);
    await submit('   ');
    expect(f.calls).toHaveLength(1);
    expect(hits()).toHaveLength(0);
    expect(document.getElementById('search-meta').textContent).toBe('');
    expect(document.getElementById('search-empty').hidden).toBe(true);
  });

  it('toasts the error message on 500 and leaves no results', async () => {
    await open({ search: '?q=x', routes: { 'GET /app/api/search': () => jsonResponse({ error: 'index down' }, 500) } });
    const t = document.querySelector('.toast');
    expect(t.textContent).toBe('index down');
    expect(t.className).toContain('toast-error');
    expect(hits()).toHaveLength(0);
  });
});
