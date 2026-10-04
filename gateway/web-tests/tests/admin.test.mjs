// SPDX-License-Identifier: AGPL-3.0-or-later
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { flush, jsonResponse, loadApp, mockFetch, mountPage, runPage } from './helpers.mjs';

beforeAll(loadApp);

const ADMIN = { is_admin: true, user: 'root' };
const USERS = [
  { id: 'root', admin: true, disabled: false, grants: { alpha: 'admin' } },
  { id: 'anna', admin: false, disabled: false, grants: { alpha: 'write', beta: 'read' } },
  { id: 'bob', admin: false, disabled: true, grants: {} },
];
const PROJECTS = [
  { name: 'alpha', allow_send: true, outbox: 2, users: 3, vault: '/short/vault' },
  { name: 'beta', allow_send: false, outbox: 0, users: 1, vault: '/very/long/path/to/some/vault/that/exceeds/forty/chars' },
];
const SYSTEM = { checks: [
  { status: 'PASS', name: 'db', message: 'ok' },
  { status: 'WARN', name: 'mail', message: 'slow' },
  { status: 'FAIL', name: 'git', message: 'down' },
  { status: 'SKIP', name: 'x', message: 'skipped' },
  { status: 'ODD', name: 'y', message: 'unknown status' },
] };
const LLM = { providers: ['anthropic', 'ollama', 'byo'], provider: 'anthropic', name: 'claude-x', endpoint: '', has_key: true };
const AUDIT = { entries: [
  { ts: new Date().toISOString(), user: 'anna', project: 'alpha', tool: 'memory_write', ok: true, detail: null },
  { ts: 'not-a-date', user: 'bob', project: 'beta', tool: 'email_send', ok: false, detail: '<b>denied</b>' },
], has_more: false };

const base = (over = {}) => ({
  'GET /app/api/admin/users': USERS,
  'GET /app/api/admin/projects': PROJECTS,
  'GET /app/api/admin/audit': AUDIT,
  'GET /app/api/admin/system': SYSTEM,
  'GET /app/api/admin/llm': LLM,
  'GET /app/api/admin/mfa': { enrolled: true, verified: true },
  ...over,
});

let reload;
async function open({ me = ADMIN, routes = base() } = {}) {
  mountPage('admin');
  document.body.insertAdjacentHTML('beforeend', '<div id="toast-container"></div>');
  window.ME = Promise.resolve(me);
  const f = mockFetch(routes);
  await runPage('admin');
  await flush();
  return f;
}

const toasts = () => [...document.querySelectorAll('.toast')].map((x) => `${x.className}|${x.textContent}`);
const bar = () => document.querySelector('#admin-users .settings-actions');
const barButtons = () => [...bar().querySelectorAll('button')];
const cells = (sel) => [...document.querySelectorAll(`${sel} tbody tr`)].map((tr) => [...tr.children].map((td) => td.textContent));

beforeEach(() => {
  reload = vi.fn();
  vi.stubGlobal('location', { reload, pathname: '/app/admin', search: '' });
});
afterEach(() => vi.useRealTimers());

describe('admin page: role gating', () => {
  it('does nothing without a session', async () => {
    const f = await open({ me: null, routes: {} });
    expect(f.calls).toHaveLength(0);
    expect(document.getElementById('admin-denied').hidden).toBe(true);
  });

  it('shows the denied state for non-admins and makes no API calls', async () => {
    const f = await open({ me: { is_admin: false, user: 'anna' }, routes: {} });
    expect(f.calls).toHaveLength(0);
    expect(document.getElementById('admin-denied').hidden).toBe(false);
    expect(document.getElementById('admin-tabs').hidden).toBe(true);
    expect([...document.querySelectorAll('.admin-pane')].every((p) => p.hidden)).toBe(true);
  });
});

describe('admin page: read views', () => {
  beforeEach(() => { window.I18N = { web_admin_disabled: 'Avstängd' }; });

  it('renders the users table with admin, status and grant chips', async () => {
    await open();
    const rows = cells('#admin-users');
    expect(rows[0]).toEqual(['root', '🛡', '', 'alpha:admin']);
    expect(rows[1]).toEqual(['anna', '', '', 'alpha:write  beta:read']);
    expect(rows[2]).toEqual(['bob', '', 'Avstängd', '—']);
  });

  it('tolerates a non-list users response', async () => {
    await open({ routes: base({ 'GET /app/api/admin/users': { error: 'x' } }) });
    expect(cells('#admin-users')).toEqual([]);
    expect(toasts()).toEqual([]);
  });

  it('renders projects with truncated long vault paths', async () => {
    await open();
    const rows = cells('#admin-projects');
    expect(rows[0]).toEqual(['alpha', '✓', '2', '3', '/short/vault']);
    expect(rows[1][1]).toBe('✗');
    expect(rows[1][4]).toBe('…' + PROJECTS[1].vault.slice(-38));
    expect(rows[1][4]).toHaveLength(39);
  });

  it('renders system checks with status icons and falls back to raw status', async () => {
    await open();
    expect(cells('#admin-system table:first-of-type').map((r) => r[0])).toEqual(['✅', '⚠️', '❌', '⏭', 'ODD']);
    expect(cells('#admin-system table:first-of-type')[2]).toEqual(['❌', 'git', 'down']);
  });

  it('reports a failing read endpoint with a toast and keeps the rest working', async () => {
    await open({ routes: base({
      'GET /app/api/admin/projects': () => jsonResponse({ error: 'boom' }, 500),
      'GET /app/api/admin/system': () => jsonResponse({ error: 'sysfail' }, 500),
      'GET /app/api/admin/users': () => jsonResponse({ error: 'ufail' }, 500),
      'GET /app/api/admin/llm': () => jsonResponse({ error: 'llmfail' }, 500),
    }) });
    expect(toasts().filter((x) => x.includes('toast-error')).map((x) => x.split('|')[1]).sort())
      .toEqual(['boom', 'llmfail', 'sysfail', 'ufail']);
    expect(cells('#admin-audit')).toHaveLength(2);
  });

  it('switches panes when a tab is clicked', async () => {
    await open();
    const tabs = [...document.querySelectorAll('#admin-tabs .tab')];
    tabs[2].click();
    expect(tabs.map((x) => x.classList.contains('tab-active'))).toEqual([false, false, true, false]);
    const hidden = ['users', 'projects', 'audit', 'system'].map((p) => document.getElementById(`admin-${p}`).hidden);
    expect(hidden).toEqual([true, true, false, true]);
  });
});

describe('admin page: audit log', () => {
  beforeEach(() => { window.I18N = { web_time_now: 'jetzt' }; });

  it('renders rows as text, marks failures, and requests the first page', async () => {
    const f = await open();
    const call = f.calls.find((c) => c.key === 'GET /app/api/admin/audit');
    const q = new URLSearchParams(call.url.split('?')[1]);
    expect(Object.fromEntries(q)).toEqual({ offset: '0', limit: '50' });
    const trs = [...document.querySelectorAll('#audit-tbody tr')];
    expect(trs).toHaveLength(2);
    expect(trs[0].className).toBe('');
    expect([...trs[0].children].map((c) => c.textContent)).toEqual(['jetzt', 'anna', 'alpha', 'memory_write', '✓', '']);
    expect(trs[1].className).toBe('audit-row-error');
    expect(trs[1].children[0].textContent).toBe('not-a-date');
    expect(trs[1].children[5].textContent).toBe('<b>denied</b>');
    expect(trs[1].querySelector('b')).toBeNull();
    expect(document.getElementById('audit-more').hidden).toBe(true);
  });

  it('sends only the filters that are set, trimmed, and resets the table on submit', async () => {
    const f = await open();
    document.getElementById('audit-user').value = '  anna ';
    document.getElementById('audit-project').value = 'alpha';
    document.getElementById('audit-tool').value = 'email_send';
    document.getElementById('audit-ok').value = 'false';
    document.getElementById('audit-since').value = '2026-01-02';
    const ev = new Event('submit', { cancelable: true });
    document.getElementById('audit-filter').dispatchEvent(ev);
    expect(ev.defaultPrevented).toBe(true);
    await flush();
    const last = f.calls.filter((c) => c.key === 'GET /app/api/admin/audit').at(-1);
    expect(Object.fromEntries(new URLSearchParams(last.url.split('?')[1]))).toEqual({
      user: 'anna', project: 'alpha', tool: 'email_send', ok: 'false', since: '2026-01-02', offset: '0', limit: '50',
    });
    expect(document.querySelectorAll('#audit-tbody tr')).toHaveLength(2); // reset, not appended
  });

  it('paginates with load-more using the running offset', async () => {
    const f = await open({ routes: base({ 'GET /app/api/admin/audit': { ...AUDIT, has_more: true } }) });
    const more = document.getElementById('audit-more');
    expect(more.hidden).toBe(false);
    more.click();
    await flush();
    const last = f.calls.filter((c) => c.key === 'GET /app/api/admin/audit').at(-1);
    expect(new URLSearchParams(last.url.split('?')[1]).get('offset')).toBe('2');
    expect(document.querySelectorAll('#audit-tbody tr')).toHaveLength(4); // appended
  });

  it('toasts when the audit request fails', async () => {
    await open({ routes: base({ 'GET /app/api/admin/audit': () => jsonResponse({ error: 'nope' }, 403) }) });
    expect(toasts()).toContain('toast toast-error|nope');
    expect(document.querySelectorAll('#audit-tbody tr')).toHaveLength(0);
  });
});

describe('admin page: LLM settings', () => {
  const box = () => document.querySelector('.llm-settings');
  const field = (i) => box().querySelectorAll('label')[i];
  const buttons = () => box().querySelectorAll('button');

  beforeEach(() => {
    window.I18N = { web_admin_llm_byo: 'Egen', web_admin_llm_key_kept: 'behålls', web_admin_llm_saved: 'Sparat',
      web_admin_llm_test: 'Testa', web_admin_llm_testing: 'Testar' };
  });

  it('prefills the form from the server and marks the stored key as kept', async () => {
    await open();
    const sel = box().querySelector('select');
    expect([...sel.options].map((o) => o.textContent)).toEqual(['anthropic', 'ollama', 'Egen']);
    expect(sel.value).toBe('anthropic');
    const [name, endpoint, key] = box().querySelectorAll('input');
    expect(name.value).toBe('claude-x');
    expect(key.type).toBe('password');
    expect(key.placeholder).toBe('behålls');
    expect(endpoint.parentElement.hidden).toBe(true); // no endpoint, not an endpoint provider
    expect(name.parentElement.hidden).toBe(false);
  });

  it('shows a generic key placeholder when no key is stored', async () => {
    await open({ routes: base({ 'GET /app/api/admin/llm': { ...LLM, has_key: false, name: null } }) });
    const [name, , key] = box().querySelectorAll('input');
    expect(key.placeholder).toBe('sk-…');
    expect(name.value).toBe('');
  });

  it('shows the endpoint for endpoint providers, hides all inputs for byo', async () => {
    await open();
    const sel = box().querySelector('select');
    const [name, endpoint, key] = box().querySelectorAll('input');
    sel.value = 'ollama'; sel.dispatchEvent(new Event('change'));
    expect(endpoint.parentElement.hidden).toBe(false);
    sel.value = 'byo'; sel.dispatchEvent(new Event('change'));
    expect([name, endpoint, key].map((e) => e.parentElement.hidden)).toEqual([true, true, true]);
    sel.value = 'anthropic'; sel.dispatchEvent(new Event('change'));
    expect([name, endpoint, key].map((e) => e.parentElement.hidden)).toEqual([false, true, false]);
  });

  it('keeps the endpoint visible for a non-endpoint provider that already has one', async () => {
    await open({ routes: base({ 'GET /app/api/admin/llm': { ...LLM, endpoint: 'http://x:1' } }) });
    expect(box().querySelectorAll('input')[1].parentElement.hidden).toBe(false);
  });

  it('saves provider/name/endpoint and omits api_key when the key field is empty', async () => {
    const f = await open({ routes: base({ 'PUT /app/api/admin/llm': { ok: true } }) });
    const [name, endpoint] = box().querySelectorAll('input');
    name.value = 'new-model'; endpoint.value = 'http://e';
    buttons()[0].click();
    await flush();
    const put = f.calls.find((c) => c.key === 'PUT /app/api/admin/llm');
    expect(JSON.parse(put.opts.body)).toEqual({ provider: 'anthropic', name: 'new-model', endpoint: 'http://e' });
    expect(toasts()).toContain('toast toast-success|Sparat');
  });

  it('sends api_key when typed and clears the field after saving', async () => {
    const f = await open({ routes: base({ 'PUT /app/api/admin/llm': { ok: true } }) });
    const key = box().querySelectorAll('input')[2];
    key.value = 'sk-secret';
    buttons()[0].click();
    await flush();
    const put = f.calls.find((c) => c.key === 'PUT /app/api/admin/llm');
    expect(JSON.parse(put.opts.body).api_key).toBe('sk-secret');
    expect(key.value).toBe('');
  });

  it('shows the server error when saving fails and keeps the typed key', async () => {
    await open({ routes: base({ 'PUT /app/api/admin/llm': () => jsonResponse({ error: 'mfa_required' }, 403) }) });
    const key = box().querySelectorAll('input')[2];
    key.value = 'sk-keep';
    buttons()[0].click();
    await flush();
    expect(toasts()).toEqual(['toast toast-error|mfa_required']);
    expect(key.value).toBe('sk-keep');
  });

  it('runs the connection test, disables the button meanwhile, and reports latency', async () => {
    let release;
    const f = await open({ routes: base({
      'POST /app/api/admin/llm/test': () => new Promise((r) => { release = () => r(jsonResponse({ provider: 'anthropic', model: 'claude-x', latency_ms: 42 })); }),
    }) });
    const test = buttons()[1];
    test.click();
    await flush();
    expect(test.disabled).toBe(true);
    expect(test.textContent).toBe('Testar');
    release();
    await flush();
    expect(test.disabled).toBe(false);
    expect(test.textContent).toBe('Testa');
    expect(toasts()).toContain('toast toast-success|anthropic/claude-x — 42 ms ✓');
    expect(f.calls.find((c) => c.key === 'POST /app/api/admin/llm/test').opts.body).toBeUndefined();
  });

  it('re-enables the test button and toasts on failure', async () => {
    await open({ routes: base({ 'POST /app/api/admin/llm/test': () => jsonResponse({ error: 'unreachable' }, 500) }) });
    const test = buttons()[1];
    test.click();
    await flush();
    expect(test.disabled).toBe(false);
    expect(toasts()).toContain('toast toast-error|unreachable');
  });
});

describe('admin page: MFA and write actions', () => {
  beforeEach(() => {
    window.I18N = { web_mfa_setup: 'Aktivera MFA', web_mfa_verify: 'Verifiera', web_mfa_active: 'MFA aktiv',
      web_admin_enable: 'Aktivera', web_admin_disable: 'Stäng av', web_admin_last_admin: 'Sista admin',
      web_saved: 'Sparat', web_mfa_enrolled: 'Registrerad', web_mfa_verified: 'Verifierad', web_mfa_secret: 'Hemlighet' };
  });

  it('adds an empty action bar and stops when MFA status is unavailable', async () => {
    const f = await open({ routes: base({ 'GET /app/api/admin/mfa': () => jsonResponse({ error: 'x' }, 500) }) });
    expect(bar()).not.toBeNull();
    expect(barButtons()).toHaveLength(0);
    expect(f.calls.some((c) => c.key.startsWith('PATCH'))).toBe(false);
  });

  it('offers only MFA setup when not enrolled (no kill switches)', async () => {
    await open({ routes: base({ 'GET /app/api/admin/mfa': { enrolled: false, verified: false } }) });
    expect(barButtons().map((b) => b.textContent)).toEqual(['Aktivera MFA']);
  });

  it('runs the setup flow: shows secret as text, confirms with the typed code, reloads', async () => {
    const f = await open({ routes: base({
      'GET /app/api/admin/mfa': { enrolled: false, verified: false },
      'POST /app/api/admin/mfa/setup/start': { otpauth_uri: 'otpauth://totp/<img src=x>', secret: 'ABC123' },
      'POST /app/api/admin/mfa/setup': { ok: true },
    }) });
    barButtons()[0].click();
    await flush();
    const m = document.querySelector('.modal-box');
    expect(m.querySelector('code').textContent).toBe('otpauth://totp/<img src=x>');
    expect(m.querySelector('img')).toBeNull();
    expect(m.textContent).toContain('Hemlighet: ABC123');
    m.querySelector('input').value = '654321';
    m.querySelector('button').click();
    await flush();
    const post = f.calls.find((c) => c.key === 'POST /app/api/admin/mfa/setup');
    expect(JSON.parse(post.opts.body)).toEqual({ code: '654321' });
    expect(toasts()).toContain('toast toast-success|Registrerad');
    expect(document.querySelector('.modal-box')).toBeNull();
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('keeps the modal open and does not reload on a wrong setup code', async () => {
    await open({ routes: base({
      'GET /app/api/admin/mfa': { enrolled: false, verified: false },
      'POST /app/api/admin/mfa/setup/start': { otpauth_uri: 'u', secret: 's' },
      'POST /app/api/admin/mfa/setup': () => jsonResponse({ error: 'bad_code' }, 400),
    }) });
    barButtons()[0].click();
    await flush();
    document.querySelector('.modal-box button').click();
    await flush();
    expect(toasts()).toContain('toast toast-error|bad_code');
    expect(document.querySelector('.modal-box')).not.toBeNull();
    expect(reload).not.toHaveBeenCalled();
  });

  it('toasts when setup start fails', async () => {
    await open({ routes: base({
      'GET /app/api/admin/mfa': { enrolled: false, verified: false },
      'POST /app/api/admin/mfa/setup/start': () => jsonResponse({ error: 'no_start' }, 500),
    }) });
    barButtons()[0].click();
    await flush();
    expect(toasts()).toContain('toast toast-error|no_start');
    expect(document.querySelector('.modal-box')).toBeNull();
  });

  it('asks for the code when enrolled but not verified, and no kill switches', async () => {
    const f = await open({ routes: base({
      'GET /app/api/admin/mfa': { enrolled: true, verified: false },
      'POST /app/api/admin/mfa/verify': { ok: true },
    }) });
    const input = bar().querySelector('input.mfa-code');
    expect(input.maxLength).toBe(6);
    expect(barButtons().map((b) => b.textContent)).toEqual(['Verifiera']);
    input.value = '111222';
    barButtons()[0].click();
    await flush();
    const post = f.calls.find((c) => c.key === 'POST /app/api/admin/mfa/verify');
    expect(JSON.parse(post.opts.body)).toEqual({ code: '111222' });
    expect(toasts()).toContain('toast toast-success|Verifierad');
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('shows an error and does not reload on a failed verify', async () => {
    await open({ routes: base({
      'GET /app/api/admin/mfa': { enrolled: true, verified: false },
      'POST /app/api/admin/mfa/verify': () => jsonResponse({ error: 'bad_code' }, 400),
    }) });
    barButtons()[0].click();
    await flush();
    expect(toasts()).toContain('toast toast-error|bad_code');
    expect(reload).not.toHaveBeenCalled();
  });

  it('when verified, shows a status note and a toggle per other user (never for yourself)', async () => {
    await open();
    expect(bar().querySelector('.muted').textContent).toBe('MFA aktiv');
    expect(barButtons().map((b) => [b.className, b.textContent])).toEqual([
      ['btn btn-danger', 'Stäng av: anna'],
      ['btn', 'Aktivera: bob'],
    ]);
  });

  it('disables an active user with PATCH {disabled:true}, then reloads', async () => {
    const f = await open({ routes: base({ 'PATCH /app/api/admin/users/anna': { ok: true } }) });
    barButtons()[0].click();
    await flush();
    const patch = f.calls.find((c) => c.key.startsWith('PATCH'));
    expect(patch.key).toBe('PATCH /app/api/admin/users/anna');
    expect(JSON.parse(patch.opts.body)).toEqual({ disabled: true });
    expect(toasts()).toContain('toast toast-success|Sparat');
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('re-enables a disabled user with PATCH {disabled:false}', async () => {
    const f = await open({ routes: base({ 'PATCH /app/api/admin/users/bob': { ok: true } }) });
    barButtons()[1].click();
    await flush();
    expect(JSON.parse(f.calls.find((c) => c.key.startsWith('PATCH')).opts.body)).toEqual({ disabled: false });
  });

  it('url-encodes the user id in the PATCH path', async () => {
    const odd = [{ id: 'a b/c@x', admin: false, disabled: false, grants: {} }];
    const f = await open({ routes: base({
      'GET /app/api/admin/users': odd,
      'PATCH /app/api/admin/users/a%20b%2Fc%40x': { ok: true },
    }) });
    barButtons()[0].click();
    await flush();
    expect(f.calls.find((c) => c.key.startsWith('PATCH')).key).toBe('PATCH /app/api/admin/users/a%20b%2Fc%40x');
  });

  it('maps the last_admin error to a friendly message and does not reload', async () => {
    await open({ routes: base({ 'PATCH /app/api/admin/users/anna': () => jsonResponse({ error: 'last_admin' }, 409) }) });
    barButtons()[0].click();
    await flush();
    expect(toasts()).toEqual(['toast toast-error|Sista admin']);
    expect(reload).not.toHaveBeenCalled();
  });

  it('shows the raw server message for other PATCH failures (e.g. 403 mfa)', async () => {
    await open({ routes: base({ 'PATCH /app/api/admin/users/anna': () => jsonResponse({ error: 'mfa_required' }, 403) }) });
    barButtons()[0].click();
    await flush();
    expect(toasts()).toEqual(['toast toast-error|mfa_required']);
    expect(reload).not.toHaveBeenCalled();
  });

  it('does not throw when the users list resolves to a non-list (dead session)', async () => {
    await open({ routes: base({ 'GET /app/api/admin/users': { error: 'unauth' } }) });
    expect(bar().querySelector('.muted')).not.toBeNull();
    expect(barButtons()).toHaveLength(0);
  });

  it('survives the users list rejecting', async () => {
    let n = 0;
    await open({ routes: base({ 'GET /app/api/admin/users': () => (++n === 1 ? jsonResponse(USERS) : jsonResponse({ error: 'x' }, 500)) }) });
    expect(bar().querySelector('.muted')).not.toBeNull();
    expect(barButtons()).toHaveLength(0);
  });
});
