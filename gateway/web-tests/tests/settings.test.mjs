// SPDX-License-Identifier: AGPL-3.0-or-later
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { jsonResponse, loadApp, mockFetch, mountPage, runPage } from './helpers.mjs';

beforeAll(loadApp);

const ME = { projects: ['alpha', 'beta'] };
const CAL = { 'GET /app/api/settings/calendar-mode': { active_mode: 'none' } };
const tick = (ms = 0) => vi.advanceTimersByTimeAsync(ms);
const $ = (id) => document.getElementById(id);
const items = () => [...document.querySelectorAll('#accounts-list > li')];
const toasts = () => [...document.querySelectorAll('#toast-container .toast')].map((x) => [x.className, x.textContent]);
const bodyOf = (call) => JSON.parse(call.opts.body);

async function open({ routes = {}, me = ME, search = '', brief = true } = {}) {
  mountPage('settings');
  if (!brief) $('brief-form').remove();
  const tc = document.createElement('div');
  tc.id = 'toast-container';
  document.body.append(tc);
  history.replaceState(null, '', `/app/settings${search}`);
  window.ME = Promise.resolve(me);
  const f = mockFetch({
    'GET /app/api/accounts': [],
    'GET /app/api/brief': { configured: false },
    ...CAL,
    ...routes,
  });
  await runPage('settings');
  await tick();
  return f;
}

const acct = (over = {}) => ({
  provider: 'google', account: 'a@x.se', status: 'active', capabilities: ['email', 'calendar'],
  scopes_by_capability: { email: ['alpha'] }, ...over,
});

beforeEach(() => {
  vi.useFakeTimers();
  window.I18N = {
    web_settings_scope_none: 'Delas inte', web_settings_capability_email: 'E-post', web_saved: 'Sparat',
    web_settings_unlink: 'Koppla bort', web_settings_unlinked: 'Bortkopplad', web_settings_needs_relink: 'Länka om',
    web_settings_scope_all: 'Alla', web_settings_link_started: 'Startad', web_settings_calendar_active: 'Aktiv',
    web_brief_next: 'Nästa',
  };
});
afterEach(() => vi.useRealTimers());

describe('settings: not logged in', () => {
  it('does nothing without a session (brief page still skipped by missing fetch)', async () => {
    mountPage('settings');
    window.ME = Promise.resolve(null);
    const f = mockFetch({ 'GET /app/api/brief': { configured: false } });
    await runPage('settings');
    await tick();
    expect(f.calls.map((c) => c.key)).toEqual(['GET /app/api/brief']);
    expect($('calendar-current').textContent).toBe('');
  });
});

describe('linked accounts list', () => {
  it('shows the empty state when there are no accounts', async () => {
    await open();
    expect($('accounts-empty').hidden).toBe(false);
    expect(items()).toHaveLength(0);
  });

  it('keeps the empty state when loading accounts fails', async () => {
    await open({ routes: { 'GET /app/api/accounts': () => jsonResponse({ error: 'x' }, 500) } });
    expect($('accounts-empty').hidden).toBe(false);
    expect(items()).toHaveLength(0);
  });

  it('renders status dots, labels, project suffix, relink note and unlink buttons', async () => {
    await open({
      routes: { 'GET /app/api/accounts': [
        acct(),
        acct({ provider: 'imap', account: 'm@x.se', status: 'needs_relink', project: 'beta', capabilities: [] }),
        acct({ provider: 'microsoft', account: 'ro@x.se', readonly: true, project: 'alpha' }),
      ] },
    });
    expect($('accounts-empty').hidden).toBe(true);
    const heads = items().map((li) => li.querySelector('.account-head'));
    expect(heads.map((h) => h.children[0].textContent)).toEqual(['🟢', '🟡', '🔵']);
    expect(heads.map((h) => h.querySelector('.account-label').textContent)).toEqual([
      'google · a@x.se', 'IMAP · m@x.se (beta)', 'microsoft · ro@x.se (alpha)',
    ]);
    expect(heads[1].querySelector('.muted').textContent).toBe('Länka om');
    expect(heads[0].querySelector('.muted')).toBeNull();
    // read-only: no unlink button and no scope grid
    expect(heads.map((h) => !!h.querySelector('.btn-danger'))).toEqual([true, true, false]);
    expect(items().map((li) => !!li.querySelector('.scope-grid'))).toEqual([true, false, false]);
  });

  it('renders account data as text, never as markup', async () => {
    await open({ routes: { 'GET /app/api/accounts': [acct({ account: '<img src=x onerror=alert(1)>', capabilities: ['<b>x</b>'] })] } });
    expect(document.querySelector('#accounts-list img')).toBeNull();
    expect(document.querySelector('#accounts-list b')).toBeNull();
    expect(document.querySelector('.account-label').textContent).toContain('<img src=x');
    expect(document.querySelector('.scope-capability').textContent).toBe('<b>x</b>');
  });

  it('unlinks with an encoded URL, toasts, and reloads the list', async () => {
    let accounts = [acct({ provider: 'google', account: 'a+b@x.se' })];
    const f = await open({
      routes: {
        'GET /app/api/accounts': () => jsonResponse(accounts),
        'DELETE /app/api/accounts/google': () => { accounts = []; return jsonResponse({}); },
      },
    });
    document.querySelector('.account-head .btn-danger').click();
    await tick();
    const del = f.calls.find((c) => c.key.startsWith('DELETE'));
    expect(del.url).toBe('/app/api/accounts/google?account=a%2Bb%40x.se');
    expect(toasts()).toEqual([['toast toast-success', 'Bortkopplad']]);
    expect(items()).toHaveLength(0);
  });

  it('toasts the server error when unlinking fails and keeps the account', async () => {
    await open({
      routes: {
        'GET /app/api/accounts': [acct()],
        'DELETE /app/api/accounts/google': () => jsonResponse({ error: 'kan inte' }, 500),
      },
    });
    document.querySelector('.account-head .btn-danger').click();
    await tick();
    expect(toasts()).toEqual([['toast toast-error', 'kan inte']]);
    expect(items()).toHaveLength(1);
  });
});

describe('scope grid', () => {
  const grid = () => document.querySelector('.scope-grid');
  const rowsOf = () => [...grid().querySelectorAll('.scope-row')];
  const boxes = (row) => [...row.querySelectorAll('input')];

  it('warns that nothing is shared when no capability has projects', async () => {
    await open({ routes: { 'GET /app/api/accounts': [acct({ scopes_by_capability: undefined })] } });
    expect(grid().querySelector('.scope-unshared').textContent).toBe('Delas inte');
  });

  it('shows no warning when at least one capability is shared', async () => {
    await open({ routes: { 'GET /app/api/accounts': [acct()] } });
    expect(grid().querySelector('.scope-unshared')).toBeNull();
  });

  it('labels capabilities by translation, falling back to the bare name', async () => {
    await open({ routes: { 'GET /app/api/accounts': [acct()] } });
    expect(rowsOf().map((r) => r.querySelector('.scope-capability').textContent)).toEqual(['E-post', 'calendar']);
  });

  it('ticks explicit projects and leaves "all" unticked', async () => {
    await open({ routes: { 'GET /app/api/accounts': [acct()] } });
    const [all, a, b] = boxes(rowsOf()[0]);
    expect([all.checked, a.checked, b.checked]).toEqual([false, true, false]);
    expect([a.disabled, b.disabled]).toEqual([false, false]);
    expect(boxes(rowsOf()[0]).slice(1).map((x) => x.value)).toEqual(['alpha', 'beta']);
  });

  it('wildcard ticks and locks every project box', async () => {
    await open({ routes: { 'GET /app/api/accounts': [acct({ scopes_by_capability: { email: ['*'] } })] } });
    const [all, a, b] = boxes(rowsOf()[0]);
    expect([all.checked, a.checked, b.checked]).toEqual([true, true, true]);
    expect([a.disabled, b.disabled]).toEqual([true, true]);
  });

  it('renders only the "all" box when the user has no projects', async () => {
    await open({ me: { projects: [] }, routes: { 'GET /app/api/accounts': [acct()] } });
    expect(boxes(rowsOf()[0])).toHaveLength(1);
  });

  it('saves the ticked projects for that capability and reloads', async () => {
    const f = await open({
      routes: { 'GET /app/api/accounts': [acct()], 'POST /app/api/accounts/scopes': {} },
    });
    const [, , b] = boxes(rowsOf()[0]);
    b.checked = true;
    b.dispatchEvent(new Event('change'));
    await tick();
    const post = f.calls.find((c) => c.key === 'POST /app/api/accounts/scopes');
    expect(bodyOf(post)).toEqual({ provider: 'google', account: 'a@x.se', capability: 'email', projects: ['alpha', 'beta'] });
    expect(toasts()).toEqual([['toast toast-success', 'Sparat']]);
    expect(f.calls.filter((c) => c.key === 'GET /app/api/accounts')).toHaveLength(2);
  });

  it('stores "all" as the wildcard, and unticking it clears everything', async () => {
    const f = await open({
      routes: { 'GET /app/api/accounts': [acct({ scopes_by_capability: { email: ['*'] } })], 'POST /app/api/accounts/scopes': {} },
    });
    const [all] = boxes(rowsOf()[1]);
    all.checked = true;
    all.dispatchEvent(new Event('change'));
    await tick();
    const [allEmail] = boxes(rowsOf()[0]);
    allEmail.checked = false;
    allEmail.dispatchEvent(new Event('change'));
    await tick();
    const posts = f.calls.filter((c) => c.key === 'POST /app/api/accounts/scopes').map(bodyOf);
    expect(posts.map((p) => [p.capability, p.projects])).toEqual([['calendar', ['*']], ['email', []]]);
  });

  it('toasts the error and reloads from the server when saving fails', async () => {
    const f = await open({
      routes: { 'GET /app/api/accounts': [acct()], 'POST /app/api/accounts/scopes': () => jsonResponse({ error: 'nej' }, 403) },
    });
    const [, a] = boxes(rowsOf()[0]);
    a.checked = false;
    a.dispatchEvent(new Event('change'));
    await tick();
    expect(toasts()).toEqual([['toast toast-error', 'nej']]);
    expect(f.calls.filter((c) => c.key === 'GET /app/api/accounts')).toHaveLength(2);
  });
});

describe('OAuth link flow', () => {
  it.each(['google', 'microsoft'])('opens the %s consent window and polls for 2 minutes', async (provider) => {
    const open_ = vi.spyOn(window, 'open').mockImplementation(() => null);
    const f = await open({ routes: { [`GET /app/api/accounts/link/${provider}`]: { url: 'https://idp/auth' } } });
    const accountLoads = () => f.calls.filter((c) => c.key === 'GET /app/api/accounts').length;
    $(`link-${provider}`).click();
    await tick();
    expect(open_).toHaveBeenCalledWith('https://idp/auth', '_blank', 'width=600,height=700');
    expect(toasts()).toEqual([['toast toast-info', 'Startad']]);
    const before = accountLoads();
    await tick(4000);
    expect(accountLoads()).toBe(before + 1);
    await tick(120_000);
    const stopped = accountLoads();
    await tick(20_000);
    expect(accountLoads()).toBe(stopped);
    expect(stopped).toBeGreaterThan(before + 20);
  });

  it('does nothing visible when the server returns no url', async () => {
    const open_ = vi.spyOn(window, 'open').mockImplementation(() => null);
    await open({ routes: { 'GET /app/api/accounts/link/google': {} } });
    $('link-google').click();
    await tick();
    expect(open_).not.toHaveBeenCalled();
    expect(toasts()).toEqual([]);
  });

  it('toasts the error when link start fails', async () => {
    await open({ routes: { 'GET /app/api/accounts/link/google': () => jsonResponse({ error: 'ej konfigurerad' }, 500) } });
    $('link-google').click();
    await tick();
    expect(toasts()).toEqual([['toast toast-error', 'ej konfigurerad']]);
  });
});

describe('IMAP form', () => {
  const fill = (port) => {
    $('imap-account-email').value = 'me@x.se';
    $('imap-host').value = 'imap.x.se';
    $('imap-user').value = 'me';
    $('imap-password').value = 's3cret';
    $('imap-port').value = port;
  };
  const submit = () => $('imap-link-form').dispatchEvent(new Event('submit', { cancelable: true }));

  it('posts the credentials with a numeric port, then resets and reloads', async () => {
    const f = await open({ routes: { 'POST /app/api/accounts/link-imap': {} } });
    fill('1993');
    submit();
    await tick();
    const post = f.calls.find((c) => c.key === 'POST /app/api/accounts/link-imap');
    expect(bodyOf(post)).toEqual({ account_email: 'me@x.se', host: 'imap.x.se', user: 'me', password: 's3cret', port: 1993 });
    expect(toasts()).toEqual([['toast toast-success', 'Sparat']]);
    expect($('imap-password').value).toBe('');
    expect(f.calls.filter((c) => c.key === 'GET /app/api/accounts')).toHaveLength(2);
  });

  it('omits the port when blank and prevents the native submit', async () => {
    const f = await open({ routes: { 'POST /app/api/accounts/link-imap': {} } });
    fill('');
    const ev = new Event('submit', { cancelable: true });
    $('imap-link-form').dispatchEvent(ev);
    await tick();
    expect(ev.defaultPrevented).toBe(true);
    expect('port' in bodyOf(f.calls.find((c) => c.key === 'POST /app/api/accounts/link-imap'))).toBe(false);
  });

  it('toasts the error and keeps the typed values on failure', async () => {
    await open({ routes: { 'POST /app/api/accounts/link-imap': () => jsonResponse({ error: 'login failed' }, 400) } });
    fill('993');
    submit();
    await tick();
    expect(toasts()).toEqual([['toast toast-error', 'login failed']]);
    expect($('imap-password').value).toBe('s3cret');
  });
});

describe('calendar mode', () => {
  const select = () => $('calendar-mode-select');
  const pick = (v) => { select().value = v; select().dispatchEvent(new Event('change')); };
  const submit = () => $('calendar-form').dispatchEvent(new Event('submit', { cancelable: true }));
  const visible = () => [$('calendar-ical-url').hidden, $('calendar-calendar-id').hidden, $('calendar-native-hint').hidden];

  it('loads the active mode for the ?project= project and preselects it', async () => {
    const f = await open({ search: '?project=beta', routes: { 'GET /app/api/settings/calendar-mode': { active_mode: 'free_busy' } } });
    expect(f.calls.find((c) => c.key === 'GET /app/api/settings/calendar-mode').url).toBe('/app/api/settings/calendar-mode?project=beta');
    expect($('calendar-current').textContent).toBe('Aktiv: free_busy');
    expect(select().value).toBe('free_busy');
    expect(visible()).toEqual([true, false, true]);
  });

  it('falls back to localStorage, then the first project, then empty', async () => {
    localStorage.setItem('memaix_project', 'gamma');
    let f = await open();
    expect(f.calls.find((c) => c.key.includes('calendar-mode')).url).toContain('project=gamma');
    localStorage.clear();
    f = await open();
    expect(f.calls.find((c) => c.key.includes('calendar-mode')).url).toContain('project=alpha');
    f = await open({ me: { projects: [] } });
    expect(f.calls.find((c) => c.key.includes('calendar-mode')).url).toContain('project=');
    expect(f.calls.find((c) => c.key.includes('calendar-mode')).url).not.toContain('alpha');
  });

  it('keeps the default selection when no mode is active', async () => {
    await open();
    expect($('calendar-current').textContent).toBe('Aktiv: none');
    expect(select().value).toBe('oauth');
    expect(visible()).toEqual([true, true, true]);
  });

  it('clears the status line when the mode cannot be loaded', async () => {
    await open({ routes: { 'GET /app/api/settings/calendar-mode': () => jsonResponse({ error: 'x' }, 500) } });
    expect($('calendar-current').textContent).toBe('');
  });

  it('shows only the input that belongs to the chosen mode', async () => {
    await open();
    pick('ical_secret');
    expect(visible()).toEqual([false, true, true]);
    pick('free_busy');
    expect(visible()).toEqual([true, false, true]);
    pick('native');
    expect(visible()).toEqual([true, true, false]);
    pick('oauth');
    expect(visible()).toEqual([true, true, true]);
  });

  it('sends the ical url only in ical_secret mode', async () => {
    const f = await open({ routes: { 'POST /app/api/settings/calendar-mode': {} } });
    $('calendar-ical-url').value = 'https://cal/x.ics';
    $('calendar-calendar-id').value = 'ignored';
    pick('ical_secret');
    submit();
    await tick();
    const post = f.calls.find((c) => c.key === 'POST /app/api/settings/calendar-mode');
    expect(bodyOf(post)).toEqual({ project: 'alpha', mode: 'ical_secret', ical_url: 'https://cal/x.ics' });
    expect(toasts()).toEqual([['toast toast-success', 'Sparat']]);
    expect($('calendar-current').textContent).toBe('Aktiv: ical_secret');
  });

  it('sends the calendar id only in free_busy mode', async () => {
    const f = await open({ routes: { 'POST /app/api/settings/calendar-mode': {} } });
    $('calendar-ical-url').value = 'ignored';
    $('calendar-calendar-id').value = 'cal@id';
    pick('free_busy');
    submit();
    await tick();
    expect(bodyOf(f.calls.find((c) => c.key === 'POST /app/api/settings/calendar-mode')))
      .toEqual({ project: 'alpha', mode: 'free_busy', calendar_id: 'cal@id' });
  });

  it('sends just project and mode for oauth and prevents native submit', async () => {
    const f = await open({ routes: { 'POST /app/api/settings/calendar-mode': {} } });
    const ev = new Event('submit', { cancelable: true });
    $('calendar-form').dispatchEvent(ev);
    await tick();
    expect(ev.defaultPrevented).toBe(true);
    expect(bodyOf(f.calls.find((c) => c.key === 'POST /app/api/settings/calendar-mode'))).toEqual({ project: 'alpha', mode: 'oauth' });
  });

  it('shows the server hint and opens the link when the server asks for more steps', async () => {
    const open_ = vi.spyOn(window, 'open').mockImplementation(() => null);
    await open({ routes: { 'POST /app/api/settings/calendar-mode': { next: 'Godkänn i fönstret', link_url: 'https://idp/x' } } });
    submit();
    await tick();
    expect(toasts()).toEqual([['toast toast-info', 'Godkänn i fönstret']]);
    expect(open_).toHaveBeenCalledWith('https://idp/x', '_blank', 'width=600,height=700');
  });

  it('uses the link url as the message when there is no hint', async () => {
    const open_ = vi.spyOn(window, 'open').mockImplementation(() => null);
    await open({ routes: { 'POST /app/api/settings/calendar-mode': { link_url: 'https://idp/y' } } });
    submit();
    await tick();
    expect(toasts()).toEqual([['toast toast-info', 'https://idp/y']]);
    expect(open_).toHaveBeenCalledWith('https://idp/y', '_blank', 'width=600,height=700');
  });

  it('shows only the hint when no link_url is returned', async () => {
    const open_ = vi.spyOn(window, 'open').mockImplementation(() => null);
    await open({ routes: { 'POST /app/api/settings/calendar-mode': { next: 'Klart snart' } } });
    submit();
    await tick();
    expect(toasts()).toEqual([['toast toast-info', 'Klart snart']]);
    expect(open_).not.toHaveBeenCalled();
  });

  it('toasts the error and leaves the status line untouched on failure', async () => {
    await open({ routes: { 'POST /app/api/settings/calendar-mode': () => jsonResponse({ error: 'ogiltig url' }, 400) } });
    submit();
    await tick();
    expect(toasts()).toEqual([['toast toast-error', 'ogiltig url']]);
    expect($('calendar-current').textContent).toBe('Aktiv: none');
  });
});

describe('daily brief', () => {
  const submit = () => $('brief-form').dispatchEvent(new Event('submit', { cancelable: true }));
  const next = '2030-01-02T03:04:00Z';

  it('is skipped entirely when the page has no brief form', async () => {
    const f = await open({ brief: false });
    expect(f.calls.some((c) => c.key === 'GET /app/api/brief')).toBe(false);
  });

  it('fills the form from a configured brief and shows the next run', async () => {
    await open({ routes: { 'GET /app/api/brief': { configured: true, prefs: { enabled: true, brief_time: '06:30', timezone: 'Europe/Oslo' }, next_run: next } } });
    expect($('brief-enabled').checked).toBe(true);
    expect($('brief-time').value).toBe('06:30');
    expect($('brief-timezone').value).toBe('Europe/Oslo');
    expect($('brief-status').textContent).toBe(`Nästa: ${new Date(next).toLocaleString()}`);
  });

  it('keeps form defaults for missing prefs and shows no status without next_run', async () => {
    await open({ routes: { 'GET /app/api/brief': { configured: true, prefs: { enabled: false } } } });
    expect($('brief-enabled').checked).toBe(false);
    expect($('brief-time').value).toBe('07:00');
    expect($('brief-timezone').value).toBe('');
    expect($('brief-status').textContent).toBe('');
  });

  it('leaves the form alone when no brief is configured or loading fails', async () => {
    await open();
    expect($('brief-time').value).toBe('07:00');
    await open({ routes: { 'GET /app/api/brief': () => jsonResponse({ error: 'x' }, 500) } });
    expect($('brief-status').textContent).toBe('');
  });

  it('saves enabled/time/timezone and shows the next run', async () => {
    const f = await open({ routes: { 'POST /app/api/brief': { next_run: next } } });
    $('brief-enabled').checked = true;
    $('brief-time').value = '08:15';
    $('brief-timezone').value = 'Europe/Stockholm';
    const ev = new Event('submit', { cancelable: true });
    $('brief-form').dispatchEvent(ev);
    await tick();
    expect(ev.defaultPrevented).toBe(true);
    expect(bodyOf(f.calls.find((c) => c.key === 'POST /app/api/brief')))
      .toEqual({ enabled: true, brief_time: '08:15', timezone: 'Europe/Stockholm' });
    expect($('brief-status').textContent).toBe(`Nästa: ${new Date(next).toLocaleString()}`);
    expect(toasts()).toEqual([['toast toast-success', 'Sparat']]);
  });

  it('omits an empty timezone and clears status when there is no next run', async () => {
    const f = await open({ routes: { 'POST /app/api/brief': {} } });
    $('brief-status').textContent = 'old';
    submit();
    await tick();
    expect('timezone' in bodyOf(f.calls.find((c) => c.key === 'POST /app/api/brief'))).toBe(false);
    expect($('brief-status').textContent).toBe('');
  });

  it('toasts the error when saving fails', async () => {
    await open({ routes: { 'POST /app/api/brief': () => jsonResponse({ error: 'ogiltig tid' }, 400) } });
    submit();
    await tick();
    expect(toasts()).toEqual([['toast toast-error', 'ogiltig tid']]);
  });
});
