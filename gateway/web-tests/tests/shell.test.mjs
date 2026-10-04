// SPDX-License-Identifier: AGPL-3.0-or-later
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { flush, jsonResponse, loadApp, mockFetch, mountPage, runPage } from './helpers.mjs';

beforeAll(loadApp);
afterEach(() => {
  vi.useRealTimers();
  delete document.hidden;
});

const ME = { user: 'ann', projects: ['alpha', 'beta'], role_map: { alpha: 'approver', beta: 'viewer' }, is_admin: false, pending_outbox: 0 };
const $ = (id) => document.getElementById(id);
const options = (id) => [...$(id).querySelectorAll('option')].map((o) => [o.value, o.selected]);

// location.href assignments would trigger jsdom navigation; capture them instead.
function captureNavigation(pathname = '/app', search = '') {
  history.replaceState(null, '', pathname + search);
  const nav = { href: null };
  vi.stubGlobal('location', {
    pathname, search,
    get href() { return nav.href; },
    set href(v) { nav.href = v; },
  });
  return nav;
}

async function boot({ me = ME, path = '/app', search = '', extra = () => {}, routes = {}, timers = false } = {}) {
  if (timers) vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
  mountPage('shell');
  extra();
  history.replaceState(null, '', path + search);
  const f = mockFetch({
    'GET /app/api/me': () => jsonResponse(typeof me === 'function' ? me() : me),
    'POST /board/auth/logout': {},
    ...routes,
  });
  await runPage('shell');
  await window.ME;
  return f;
}

describe('theme', () => {
  it('defaults to light and shows the sun glyph', async () => {
    await boot();
    expect(document.documentElement.dataset.theme).toBe('light');
    expect($('theme-btn').textContent).toBe('☼');
  });

  it('boots dark from localStorage', async () => {
    localStorage.setItem('mb_theme', 'dark');
    await boot();
    expect(document.documentElement.dataset.theme).toBe('dark');
    expect($('theme-btn').textContent).toBe('☽');
  });

  it('toggle flips the theme both ways and persists the choice', async () => {
    await boot();
    $('theme-btn').click();
    expect(document.documentElement.dataset.theme).toBe('dark');
    expect(localStorage.getItem('mb_theme')).toBe('dark');
    expect($('theme-btn').textContent).toBe('☽');
    $('theme-btn').click();
    expect(document.documentElement.dataset.theme).toBe('light');
    expect(localStorage.getItem('mb_theme')).toBe('light');
  });

  it('keeps the board iframe (theme and its own button) in sync', async () => {
    let frame;
    await boot({
      extra: () => {
        frame = document.createElement('iframe');
        frame.className = 'board-frame';
        document.body.append(frame);
        frame.contentDocument.body.innerHTML = '<button id="theme-btn">x</button>';
      },
    });
    const fdoc = frame.contentDocument;
    expect(fdoc.documentElement.dataset.theme).toBe('light');
    expect(fdoc.getElementById('theme-btn').textContent).toBe('☼');
    $('theme-btn').click();
    expect(fdoc.documentElement.dataset.theme).toBe('dark');
    expect(fdoc.getElementById('theme-btn').textContent).toBe('☽');
  });

  it('re-applies the current theme when the board iframe reloads', async () => {
    let frame;
    await boot({
      extra: () => {
        frame = document.createElement('iframe');
        frame.className = 'board-frame';
        document.body.append(frame);
      },
    });
    $('theme-btn').click();
    // navigation inside the board replaces the frame document with a fresh one
    frame.contentDocument.documentElement.removeAttribute('data-theme');
    frame.dispatchEvent(new Event('load'));
    expect(frame.contentDocument.documentElement.dataset.theme).toBe('dark');
  });

  it('survives an iframe whose document is not accessible', async () => {
    await boot({
      extra: () => {
        const frame = document.createElement('iframe');
        frame.className = 'board-frame';
        document.body.append(frame);
        Object.defineProperty(frame, 'contentDocument', { get() { throw new Error('cross-origin'); } });
      },
    });
    expect(() => $('theme-btn').click()).not.toThrow();
    expect(document.documentElement.dataset.theme).toBe('dark');
  });
});

describe('sidebar and navigation', () => {
  it('toggles collapse and persists it', async () => {
    await boot();
    expect(document.body.dataset.collapsed).toBeUndefined();
    $('sidebar-toggle').click();
    expect(document.body.dataset.collapsed).toBe('true');
    expect(localStorage.getItem('memaix_sidebar_collapsed')).toBe('true');
    $('sidebar-toggle').click();
    expect(document.body.dataset.collapsed).toBe('false');
    expect(localStorage.getItem('memaix_sidebar_collapsed')).toBe('false');
  });

  it('starts collapsed when that was saved', async () => {
    localStorage.setItem('memaix_sidebar_collapsed', 'true');
    await boot();
    expect(document.body.dataset.collapsed).toBe('true');
  });

  it('highlights the nav entries for the current page, sidebar and tab bar', async () => {
    await boot({ path: '/app/outbox/' });
    const active = [...document.querySelectorAll('.active')];
    expect(active).toHaveLength(2);
    expect(active.every((e) => e.classList.contains('nav-outbox'))).toBe(true);
  });

  it('treats /app as home', async () => {
    await boot({ path: '/app' });
    expect([...document.querySelectorAll('.active')].every((e) => e.classList.contains('nav-home'))).toBe(true);
    expect(document.querySelectorAll('.active')).toHaveLength(2);
  });
});

describe('/app/api/me, project picker and badges', () => {
  it('fills both pickers, selects the first project and shows user and role', async () => {
    const f = await boot();
    expect(options('project-picker')).toEqual([['alpha', true], ['beta', false]]);
    expect(options('project-picker-mobile')).toEqual([['alpha', true], ['beta', false]]);
    expect($('user-badge').textContent).toBe('ann · approver');
    expect($('user-badge-mobile').textContent).toBe('ann · approver');
    expect(f.calls[0].key).toBe('GET /app/api/me');
    expect(await window.ME).toMatchObject({ user: 'ann' });
  });

  it('URL ?project= wins and is stored; localStorage is the next choice', async () => {
    localStorage.setItem('memaix_project', 'alpha');
    await boot({ search: '?project=beta' });
    expect(localStorage.getItem('memaix_project')).toBe('beta');
    expect(options('project-picker')).toEqual([['alpha', false], ['beta', true]]);
    expect($('user-badge').textContent).toBe('ann · viewer');
    localStorage.setItem('memaix_project', 'beta');
    await boot();
    expect(options('project-picker')).toEqual([['alpha', false], ['beta', true]]);
  });

  it('shows the plain user name when the project has no role', async () => {
    await boot({ me: { ...ME, role_map: {} } });
    expect($('user-badge').textContent).toBe('ann');
  });

  it('admins get the admin label and the admin nav link; others keep it hidden', async () => {
    await boot({ me: { ...ME, is_admin: true } });
    expect($('user-badge').textContent).toBe('ann · admin');
    expect([...document.querySelectorAll('.nav-admin')].every((e) => !e.hidden)).toBe(true);
    await boot();
    expect([...document.querySelectorAll('.nav-admin')].every((e) => e.hidden)).toBe(true);
  });

  it('escapes project names (options are text, not markup)', async () => {
    await boot({ me: { ...ME, projects: ['<img src=x>'], role_map: {} } });
    expect($('project-picker').querySelector('img')).toBeNull();
    expect($('project-picker').querySelector('option').textContent).toBe('<img src=x>');
  });

  it('handles an account without projects', async () => {
    await boot({ me: { ...ME, projects: [] } });
    expect($('project-picker').children).toHaveLength(0);
    expect($('user-badge').textContent).toBe('ann');
  });

  it('window.ME resolves to null when the API says unauthenticated (401)', async () => {
    mountPage('shell');
    const nav = captureNavigation('/app/outbox', '?x=1');
    mockFetch({ 'GET /app/api/me': () => jsonResponse({}, 401) });
    await runPage('shell');
    expect(await window.ME).toBeNull();
    expect($('user-badge').textContent).toBe('');
    expect(nav.href).toBeNull();
  });

  it('window.ME resolves to null when the request fails', async () => {
    mountPage('shell');
    mockFetch({ 'GET /app/api/me': () => jsonResponse({ error: 'boom' }, 500) });
    await runPage('shell');
    expect(await window.ME).toBeNull();
  });

  it('choosing a project stores it and navigates with an encoded query', async () => {
    await boot();
    const nav = captureNavigation('/app/memory');
    const picker = $('project-picker');
    picker.append(new Option('a b&c', 'a b&c'));
    picker.value = 'a b&c';
    picker.dispatchEvent(new Event('change'));
    expect(localStorage.getItem('memaix_project')).toBe('a b&c');
    expect(nav.href).toBe('/app/memory?project=a%20b%26c');
  });

  it('the mobile picker navigates too', async () => {
    await boot();
    const nav = captureNavigation('/app');
    $('project-picker-mobile').value = 'beta';
    $('project-picker-mobile').dispatchEvent(new Event('change'));
    expect(nav.href).toBe('/app?project=beta');
  });

  it('polls the outbox badge from /app/api/me', async () => {
    await boot({ me: { ...ME, pending_outbox: 3 }, timers: true });
    await flush();
    const badge = document.querySelector('.outbox-badge');
    expect(badge.textContent).toBe('3');
    expect(badge.hidden).toBe(false);
  });

  it('keeps the outbox badge hidden when nothing is pending', async () => {
    await boot({ timers: true });
    await flush();
    expect(document.querySelector('.outbox-badge').hidden).toBe(true);
  });

  it('works on a page without pickers or badges (no crash)', async () => {
    document.body.innerHTML = '';
    mockFetch({ 'GET /app/api/me': ME });
    await runPage('shell');
    expect(await window.ME).toMatchObject({ user: 'ann' });
  });
});

describe('maintenance banner', () => {
  const withMaint = (message) => ({ ...ME, maintenance: message ? { message } : null });

  it('shows the operator message above the content with role=status', async () => {
    await boot({ me: withMaint('Down at 22:00'), timers: true });
    await flush();
    const bar = $('maintenance-banner');
    expect(bar.textContent).toBe('Down at 22:00');
    expect(bar.getAttribute('role')).toBe('status');
    expect(bar.nextElementSibling).toBe($('content'));
  });

  it('renders the message as text', async () => {
    await boot({ me: withMaint('<b>x</b>'), timers: true });
    await flush();
    expect($('maintenance-banner').querySelector('b')).toBeNull();
    expect($('maintenance-banner').textContent).toBe('<b>x</b>');
  });

  it('adds no banner when there is no maintenance notice', async () => {
    await boot({ timers: true });
    await flush();
    expect($('maintenance-banner')).toBeNull();
  });

  it('refreshes every minute: updates the text, reuses the bar, removes it when cleared', async () => {
    let msg = 'first';
    const f = await boot({ me: () => withMaint(msg), timers: true });
    await flush();
    const meCalls = () => f.calls.filter((c) => c.key === 'GET /app/api/me').length;
    const before = meCalls();
    msg = 'second';
    await vi.advanceTimersByTimeAsync(60000);
    await flush();
    expect(document.querySelectorAll('#maintenance-banner')).toHaveLength(1);
    expect($('maintenance-banner').textContent).toBe('second');
    expect(meCalls()).toBeGreaterThan(before);
    msg = null;
    await vi.advanceTimersByTimeAsync(60000);
    await flush();
    expect($('maintenance-banner')).toBeNull();
  });

  it('does not refresh the banner while the tab is hidden', async () => {
    let msg = 'old';
    await boot({ me: () => withMaint(msg), timers: true });
    await flush();
    msg = 'new';
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
    await vi.advanceTimersByTimeAsync(60000);
    await flush();
    expect($('maintenance-banner').textContent).toBe('old');
    delete document.hidden;
    await vi.advanceTimersByTimeAsync(60000);
    await flush();
    expect($('maintenance-banner').textContent).toBe('new');
  });

  it('removes the banner when the fresh fetch fails', async () => {
    let fail = false;
    await boot({ me: () => withMaint('x'), timers: true, routes: {
      'GET /app/api/me': () => (fail ? jsonResponse({ error: 'e' }, 500) : jsonResponse(withMaint('x'))),
    } });
    await flush();
    expect($('maintenance-banner')).not.toBeNull();
    fail = true;
    await vi.advanceTimersByTimeAsync(60000);
    await flush();
    expect($('maintenance-banner')).toBeNull();
  });
});

describe('logout', () => {
  it('posts to the board logout and returns to /app', async () => {
    const f = await boot();
    const nav = captureNavigation('/app/memory');
    $('logout-btn').click();
    await flush();
    expect(f.calls.at(-1).key).toBe('POST /board/auth/logout');
    expect(nav.href).toBe('/app');
  });

  it('still leaves when the logout request fails', async () => {
    await boot({ routes: { 'POST /board/auth/logout': () => jsonResponse({ error: 'x' }, 500) } });
    const nav = captureNavigation('/app/memory');
    $('logout-btn').click();
    await flush();
    expect(nav.href).toBe('/app');
  });
});
