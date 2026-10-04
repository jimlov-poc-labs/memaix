// SPDX-License-Identifier: AGPL-3.0-or-later
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { loadApp, mockFetch, jsonResponse } from './helpers.mjs';

beforeAll(loadApp);

describe('api()', () => {
  it('sends JSON with credentials and returns the parsed body', async () => {
    const f = mockFetch({ 'POST /x': { ok: true } });
    expect(await api('POST', '/x', { a: 1 })).toEqual({ ok: true });
    const opts = f.calls[0].opts;
    expect(opts.method).toBe('POST');
    expect(opts.credentials).toBe('same-origin');
    expect(opts.body).toBe('{"a":1}');
  });

  it('sends no body for a bodyless call', async () => {
    const f = mockFetch({ 'GET /x': {} });
    await api('GET', '/x');
    expect('body' in f.calls[0].opts).toBe(false);
  });

  it('throws with status and payload on an error answer', async () => {
    mockFetch({ 'GET /x': () => jsonResponse({ error: 'nope' }, 403) });
    await expect(api('GET', '/x')).rejects.toMatchObject({ message: 'nope', status: 403 });
  });

  it('falls back to statusText when the error body is not JSON', async () => {
    vi.stubGlobal('fetch', async () => ({ ok: false, status: 500, statusText: 'Boom', json: async () => { throw new Error('x'); } }));
    await expect(api('GET', '/x')).rejects.toMatchObject({ message: 'Boom', status: 500 });
  });

  it('redirects to login with next= on 401 and returns undefined', async () => {
    mockFetch({ 'GET /x': () => jsonResponse({}, 401) });
    const fakeWindow = { location: '' };
    vi.stubGlobal('window', fakeWindow);
    vi.stubGlobal('location', { pathname: '/app/files', search: '?project=a' });
    expect(await api('GET', '/x')).toBeUndefined();
    expect(fakeWindow.location).toBe('/app/login?next=%2Fapp%2Ffiles%3Fproject%3Da');
  });
});

describe('t()', () => {
  it('returns the translation, else the key', () => {
    window.I18N = { hello: 'Hej' };
    expect(t('hello')).toBe('Hej');
    expect(t('missing')).toBe('missing');
    delete window.I18N;
    expect(t('hello')).toBe('hello');
  });
});

describe('toast()', () => {
  it('adds a toast and removes it after 4 s', () => {
    vi.useFakeTimers();
    document.body.innerHTML = '<div id="toast-container"></div>';
    toast('Hi', 'error');
    const el = document.querySelector('.toast');
    expect(el.className).toBe('toast toast-error');
    expect(el.textContent).toBe('Hi');
    vi.advanceTimersByTime(4000);
    expect(document.querySelector('.toast')).toBeNull();
    vi.useRealTimers();
  });

  it('does nothing without a container', () => {
    expect(() => toast('x')).not.toThrow();
  });
});

describe('modal()', () => {
  it('shows a node, closes on Escape and on backdrop click', () => {
    const inner = document.createElement('p');
    const m = modal(inner);
    expect(document.querySelector('.modal-box p')).toBe(inner);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
    expect(document.querySelector('.modal-backdrop')).not.toBeNull();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(document.querySelector('.modal-backdrop')).toBeNull();

    const m2 = modal(document.createElement('div'));
    m2.box.click();
    expect(document.querySelector('.modal-backdrop')).not.toBeNull();
    document.querySelector('.modal-backdrop').click();
    expect(document.querySelector('.modal-backdrop')).toBeNull();
    m.close();
  });

  it('accepts trusted markup as a string and close() is explicit', () => {
    const m = modal('<b id="in">x</b>');
    expect(document.getElementById('in')).not.toBeNull();
    m.close();
    expect(document.querySelector('.modal-backdrop')).toBeNull();
  });
});

describe('markdown', () => {
  const render = (md) => { const el = document.createElement('div'); mdView(el, md); return el; };

  it('renders headings, rules and paragraphs', () => {
    const el = render('# One\n## Two\n### Three\n---\nfirst\nsecond\n\nnext');
    expect(el.querySelector('h1').textContent).toBe('One');
    expect(el.querySelector('h2').textContent).toBe('Two');
    expect(el.querySelector('h3').textContent).toBe('Three');
    expect(el.querySelector('hr')).not.toBeNull();
    expect([...el.querySelectorAll('p')].map((p) => p.textContent)).toEqual(['first second', 'next']);
  });

  it('renders ul and ol lists', () => {
    const el = render('- a\n* b\n\n1. c\n2. d');
    expect([...el.querySelectorAll('ul li')].map((x) => x.textContent)).toEqual(['a', 'b']);
    expect([...el.querySelectorAll('ol li')].map((x) => x.textContent)).toEqual(['c', 'd']);
  });

  it('renders inline bold, italic and code', () => {
    const el = render('a **b** *c* `d` e');
    expect(el.querySelector('strong').textContent).toBe('b');
    expect(el.querySelector('em').textContent).toBe('c');
    expect(el.querySelector('code').textContent).toBe('d');
    expect(el.textContent).toBe('a b c d e');
  });

  it('renders fenced code verbatim and never as HTML', () => {
    const el = render('```\n<b>x</b>\n# not a heading\n```\nafter');
    expect(el.querySelector('pre code').textContent).toBe('<b>x</b>\n# not a heading');
    expect(el.querySelector('b')).toBeNull();
    expect(el.querySelector('h1')).toBeNull();
    expect(el.querySelector('p').textContent).toBe('after');
  });

  it('never turns markup in text into elements', () => {
    const el = render('<img src=x onerror=alert(1)> **b**');
    expect(el.querySelector('img')).toBeNull();
    expect(el.textContent).toContain('<img');
  });

  it('survives null and replaces earlier content', () => {
    const el = document.createElement('div');
    el.textContent = 'old';
    mdView(el, null);
    expect(el.textContent).toBe('');
  });
});

describe('pollBadge()', () => {
  it('returns a no-op handle without a badge', () => {
    expect(pollBadge('/p', null).stop()).toBeUndefined();
  });

  it('shows the count, hides at zero, skips when hidden, and stops', async () => {
    vi.useFakeTimers();
    let n = 3;
    const f = mockFetch({ 'GET /p': () => jsonResponse({ pending_outbox: n }) });
    const badge = document.createElement('span');
    const h = pollBadge('/p', badge, 1000);
    await vi.advanceTimersByTimeAsync(0);
    expect(badge.textContent).toBe('3');
    expect(badge.hidden).toBe(false);

    n = 0;
    await vi.advanceTimersByTimeAsync(1000);
    expect(badge.textContent).toBe('');
    expect(badge.hidden).toBe(true);

    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    const before = f.calls.length;
    await vi.advanceTimersByTimeAsync(1000);
    expect(f.calls.length).toBe(before);

    h.stop();
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
    await vi.advanceTimersByTimeAsync(5000);
    expect(f.calls.length).toBe(before);
    vi.useRealTimers();
  });

  it('keeps quiet on network errors', async () => {
    vi.stubGlobal('fetch', async () => { throw new Error('offline'); });
    const badge = document.createElement('span');
    badge.textContent = '7';
    const h = pollBadge('/p', badge, 100000);
    await Promise.resolve();
    await Promise.resolve();
    expect(badge.textContent).toBe('7');
    h.stop();
  });
});

describe('relTime()', () => {
  beforeAll(() => { window.I18N = { web_time_now: 'nu', web_time_yesterday: 'igår' }; });
  const ago = (s) => new Date(Date.now() - s * 1000).toISOString();

  it('formats each range', () => {
    expect(relTime(ago(5))).toBe('nu');
    expect(relTime(ago(150))).toBe('2 m');
    expect(relTime(ago(3 * 3600 + 5))).toBe('3 h');
    expect(relTime(ago(30 * 3600))).toBe('igår');
    expect(relTime(ago(5 * 86400))).toBe(new Date(ago(5 * 86400)).toLocaleDateString());
  });

  it('echoes an unparseable value', () => {
    expect(relTime('not a date')).toBe('not a date');
  });
});

describe('data-i18n', () => {
  it('fills translated elements on DOMContentLoaded and leaves untranslated ones', () => {
    window.I18N = { k1: 'Översatt' };
    document.body.innerHTML = '<h1 data-i18n="k1">Orig</h1><p data-i18n="k2">Keep</p>';
    document.dispatchEvent(new Event('DOMContentLoaded'));
    expect(document.querySelector('h1').textContent).toBe('Översatt');
    expect(document.querySelector('p').textContent).toBe('Keep');
  });
});
