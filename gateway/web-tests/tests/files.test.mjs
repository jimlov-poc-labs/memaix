// SPDX-License-Identifier: AGPL-3.0-or-later
import { beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { flush, jsonResponse, loadApp, mockFetch, mountPage, runPage } from './helpers.mjs';

beforeAll(loadApp);

const rows = () => [...document.querySelectorAll('#files-list li')];

async function open({ hash = '', routes, me = { projects: ['alpha', 'beta'] }, search = '' }) {
  mountPage('files');
  history.replaceState(null, '', `/app/files${search}${hash}`);
  window.ME = Promise.resolve(me);
  const f = mockFetch(routes);
  await runPage('files');
  return f;
}

describe('files page', () => {
  beforeEach(() => { window.I18N = { web_files_root: 'Start', web_files_none: 'Ingen mapp', web_files_err_forbidden: 'Förbjudet', web_files_err_notfound: 'Saknas', web_files_err_load: 'Fel' }; });

  it('does nothing when not logged in', async () => {
    mountPage('files');
    window.ME = Promise.resolve(null);
    const f = mockFetch({});
    await runPage('files');
    expect(f.calls).toHaveLength(0);
  });

  it('lists dirs and files with sizes, links, and a root crumb', async () => {
    const f = await open({
      routes: {
        'GET /app/api/files': { path: '/', entries: [
          { name: 'docs', type: 'dir' },
          { name: 'a.txt', type: 'file', size: 10 },
          { name: 'b.bin', type: 'file', size: 2048 },
          { name: 'c.bin', type: 'file', size: 15 * 1024 * 1024 },
          { name: 'd.bin', type: 'file', size: 3 * 1024 ** 3 },
          { name: 'e.bin', type: 'file', size: null },
        ] },
      },
    });
    expect(f.calls[0].url).toBe('/app/api/files?project=alpha&path=%2F');
    expect(rows()).toHaveLength(6);
    const link = (i) => rows()[i].querySelector('a');
    expect(link(0).getAttribute('href')).toBe('#%2Fdocs');
    expect(link(1).getAttribute('href')).toBe('/app/api/files/download?project=alpha&path=%2Fa.txt');
    expect(link(1).getAttribute('download')).toBe('a.txt');
    const sizes = rows().map((r) => r.querySelector('.muted').textContent);
    expect(sizes).toEqual(['', '10 B', '2.0 KB', '15 MB', '3.0 GB', '']);
    expect(document.getElementById('files-empty').hidden).toBe(true);
    expect(document.querySelector('#files-crumbs .crumb-current').textContent).toBe('Start');
  });

  it('builds breadcrumbs and joins paths from the hash', async () => {
    const f = await open({
      hash: '#%2Fdocs%2Fsub',
      routes: { 'GET /app/api/files': { path: '/docs/sub', entries: [{ name: 'x.md', type: 'file', size: 1 }] } },
    });
    expect(f.calls[0].url).toBe('/app/api/files?project=alpha&path=%2Fdocs%2Fsub');
    const nav = document.getElementById('files-crumbs');
    expect([...nav.querySelectorAll('a')].map((a) => a.textContent)).toEqual(['Start', 'docs']);
    expect(nav.querySelector('.crumb-current').textContent).toBe('sub');
    expect(nav.querySelector('.crumb-current').getAttribute('aria-current')).toBe('page');
    expect(rows()[0].querySelector('a').getAttribute('href')).toContain('path=%2Fdocs%2Fsub%2Fx.md');
  });

  it('shows the empty state for an empty folder', async () => {
    await open({ routes: { 'GET /app/api/files': { path: '/', entries: [] } } });
    expect(document.getElementById('files-empty').hidden).toBe(false);
  });

  it('prefers ?project=, then localStorage, then the first project', async () => {
    let f = await open({ search: '?project=beta', routes: { 'GET /app/api/files': { path: '/', entries: [] } } });
    expect(f.calls[0].url).toContain('project=beta');
    localStorage.setItem('memaix_project', 'gamma');
    f = await open({ routes: { 'GET /app/api/files': { path: '/', entries: [] } } });
    expect(f.calls[0].url).toContain('project=gamma');
    localStorage.clear();
    f = await open({ me: { projects: [] }, routes: { 'GET /app/api/files': { path: '/', entries: [] } } });
    expect(f.calls[0].url).toContain('project=&');
  });

  it('treats a non-absolute or malformed hash as the root', async () => {
    let f = await open({ hash: '#docs', routes: { 'GET /app/api/files': { path: '/', entries: [] } } });
    expect(f.calls[0].url).toContain('path=%2F');
    f = await open({ hash: '#%E0%A4%A', routes: { 'GET /app/api/files': { path: '/', entries: [] } } });
    expect(f.calls[0].url).toContain('path=%2F');
  });

  it.each([
    [403, 'forbidden', 'Förbjudet'],
    [404, 'gone', 'Saknas'],
    [500, 'boom', 'Fel'],
    [400, 'no_files', 'Ingen mapp'],
  ])('maps a %i (%s) answer to a message', async (status, error, text) => {
    await open({ routes: { 'GET /app/api/files': () => jsonResponse({ error }, status) } });
    const box = document.getElementById('files-msg');
    expect(box.textContent).toBe(text);
    expect(box.hidden).toBe(false);
    expect(rows()).toHaveLength(0);
  });

  it('reloads on hashchange and clears the old message', async () => {
    let fail = true;
    const f = await open({
      routes: { 'GET /app/api/files': () => (fail ? jsonResponse({ error: 'x' }, 500) : jsonResponse({ path: '/z', entries: [{ name: 'n', type: 'dir' }] })) },
    });
    expect(document.getElementById('files-msg').hidden).toBe(false);
    fail = false;
    history.replaceState(null, '', '/app/files#%2Fz');
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    await flush();
    expect(f.calls.at(-1).url).toContain('path=%2Fz');
    expect(document.getElementById('files-msg').hidden).toBe(true);
    expect(rows()).toHaveLength(1);
  });

  it('ignores an undefined answer (login redirect in flight)', async () => {
    await open({ routes: { 'GET /app/api/files': () => jsonResponse({}, 401) } });
    expect(rows()).toHaveLength(0);
    expect(document.getElementById('files-msg').hidden).toBe(true);
  });
});
