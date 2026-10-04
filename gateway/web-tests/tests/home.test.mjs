// SPDX-License-Identifier: AGPL-3.0-or-later
import { beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { flush, jsonResponse, loadApp, mockFetch, mountPage, runPage } from './helpers.mjs';

beforeAll(loadApp);

const baseMe = () => ({
  projects: ['alpha', 'beta'], role_map: { alpha: 'editor' }, is_admin: false,
  pending_outbox: 0, needs_relink: [], onboarding_missing: false,
});
const ago = (sec) => new Date(Date.now() - sec * 1000).toISOString();

async function open({ me = baseMe(), routes = {}, toastBox = true } = {}) {
  mountPage('home');
  if (toastBox) {
    const c = document.createElement('div');
    c.id = 'toast-container';
    document.body.append(c);
  }
  window.ME = Promise.resolve(me);
  const f = mockFetch({ 'GET /board/api/activity': { events: [] }, 'GET /app/api/timeline': [], ...routes });
  await runPage('home');
  await flush();
  return f;
}

const todoRows = () => [...document.querySelectorAll('#todo-list li')].map((li) => ({
  text: li.querySelector('span').textContent,
  href: li.querySelector('a').getAttribute('href'),
  btn: li.querySelector('a').textContent,
}));
const toasts = () => [...document.querySelectorAll('.toast')].map((e) => [e.textContent, e.className]);

describe('home page', () => {
  beforeEach(() => {
    window.I18N = {
      web_todo_outbox: 'väntar i utkorgen', web_todo_outbox_go: 'Granska', web_todo_relink: 'behöver länkas om',
      web_todo_relink_go: 'Länka', web_todo_onboarding: 'Slutför introt', web_todo_onboarding_go: 'Starta',
      web_open_board: 'Öppna tavla', web_cards: 'kort', web_activity_empty: 'Inget hänt',
      web_timeline_undo: 'Ångra', web_timeline_undone: 'Ångrad', web_timeline_undo_failed: 'Kunde inte ångra',
      web_timeline_conflict: 'Konflikt', web_time_now: 'nu',
    };
  });

  it('does nothing when not logged in', async () => {
    mountPage('home');
    window.ME = Promise.resolve(null);
    const f = mockFetch({});
    await runPage('home');
    expect(f.calls).toHaveLength(0);
    expect(document.getElementById('projects-grid').children).toHaveLength(0);
  });

  describe('to-do card', () => {
    it('shows the empty state when nothing needs attention', async () => {
      await open();
      expect(todoRows()).toEqual([]);
      expect(document.getElementById('todo-empty').hidden).toBe(false);
    });

    it('lists outbox, relink per provider and onboarding with links', async () => {
      await open({ me: { ...baseMe(), pending_outbox: 3, needs_relink: ['google', 'nextcloud'], onboarding_missing: true } });
      expect(todoRows()).toEqual([
        { text: '3 väntar i utkorgen', href: '/app/outbox', btn: 'Granska' },
        { text: 'google: behöver länkas om', href: '/app/settings#accounts', btn: 'Länka' },
        { text: 'nextcloud: behöver länkas om', href: '/app/settings#accounts', btn: 'Länka' },
        { text: 'Slutför introt', href: '/app/settings', btn: 'Starta' },
      ]);
      expect(document.getElementById('todo-empty').hidden).toBe(true);
    });

    it('omits the outbox item at zero pending', async () => {
      await open({ me: { ...baseMe(), onboarding_missing: true } });
      expect(todoRows().map((r) => r.href)).toEqual(['/app/settings']);
    });
  });

  it('renders without a projects list in the profile', async () => {
    await open({ me: { ...baseMe(), projects: undefined } });
    expect(document.querySelectorAll('#projects-grid > *')).toHaveLength(0);
  });

  describe('project cards', () => {
    it('renders a card per project with role chip, board link and lazy card count', async () => {
      const f = await open({
        routes: {
          'GET /board/api/board': (url) => jsonResponse({ total_cards: url.includes('project=alpha') ? 7 : 0 }),
        },
      });
      const cards = [...document.querySelectorAll('#projects-grid .project-card')];
      expect(cards.map((c) => c.querySelector('h3').textContent)).toEqual(['alpha', 'beta']);
      expect(cards[0].querySelector('.role-chip').textContent).toBe('editor');
      expect(cards[0].querySelector('.role-chip').className).toContain('role-editor');
      expect(cards[0].querySelector('a').getAttribute('href')).toBe('/app/board?project=alpha');
      expect(cards[0].querySelector('a').textContent).toBe('Öppna tavla →');
      expect(cards[0].querySelector('.muted').textContent).toBe('7 kort');
      expect(cards[1].querySelector('.muted').textContent).toBe('0 kort');
      expect(f.calls.filter((c) => c.key === 'GET /board/api/board').map((c) => c.url))
        .toEqual(['/board/api/board?project=alpha', '/board/api/board?project=beta']);
    });

    it('admins get the admin role everywhere; unknown roles fall back to empty', async () => {
      await open({ me: { ...baseMe(), is_admin: true } });
      const chips = [...document.querySelectorAll('.role-chip')].map((c) => c.textContent);
      expect(chips).toEqual(['admin', 'admin']);
      document.body.innerHTML = '';
      await open();
      expect([...document.querySelectorAll('.role-chip')].map((c) => c.textContent)).toEqual(['editor', '']);
    });

    it('url-encodes project names and renders them as text', async () => {
      const f = await open({ me: { ...baseMe(), projects: ['a&b <img src=x>'] } });
      expect(f.calls.find((c) => c.key === 'GET /board/api/board').url)
        .toBe('/board/api/board?project=a%26b%20%3Cimg%20src%3Dx%3E');
      const card = document.querySelector('.project-card');
      expect(card.querySelector('h3').textContent).toBe('a&b <img src=x>');
      expect(card.querySelector('img')).toBeNull();
    });

    it('clears the spinner when the count request fails', async () => {
      await open({ routes: { 'GET /board/api/board': () => jsonResponse({ error: 'x' }, 500) } });
      for (const c of document.querySelectorAll('.project-card .muted')) {
        expect(c.textContent).toBe('');
        expect(c.querySelector('.spinner')).toBeNull();
      }
    });
  });

  describe('activity feed', () => {
    const feed = () => document.getElementById('activity-feed');

    it('shows an empty state without events or with events missing', async () => {
      await open();
      expect(feed().querySelector('.empty-state').textContent).toBe('Inget hänt');
      document.body.innerHTML = '';
      await open({ routes: { 'GET /board/api/activity': {} } });
      expect(feed().querySelector('.empty-state')).not.toBeNull();
    });

    it('renders the newest 20 first with ok/fail marks, relative time and detail', async () => {
      const events = Array.from({ length: 25 }, (_, i) => ({ tool: `tool${i}`, project: 'alpha', ts: ago(30), ok: true }));
      events[24] = { tool: 'last', project: 'beta', ts: ago(7200), ok: false, detail: '<b>boom</b>' };
      await open({ routes: { 'GET /board/api/activity': { events } } });
      const rows = [...feed().querySelectorAll('.act-row')];
      expect(rows).toHaveLength(20);
      expect(rows[0].textContent).toBe('✗last · beta · 2 h');
      expect(rows[0].querySelector('.act-fail')).not.toBeNull();
      expect(rows[1].querySelector('.act-ok').textContent).toBe('✓');
      expect(rows[1].textContent).toBe('✓tool23 · alpha · nu');
      expect(rows[19].textContent).toContain('tool5 ');
      const detail = feed().querySelector('.act-detail');
      expect(detail.textContent).toBe('<b>boom</b>');
      expect(detail.querySelector('b')).toBeNull();
      expect(feed().querySelectorAll('.act-detail')).toHaveLength(1);
      expect(feed().querySelector('.empty-state')).toBeNull();
    });

    it('stays blank (and the dashboard still renders) when activity fails', async () => {
      await open({ routes: { 'GET /board/api/activity': () => jsonResponse({}, 500) } });
      expect(feed().children).toHaveLength(0);
      expect(document.querySelectorAll('.project-card')).toHaveLength(2);
    });
  });

  describe('timeline', () => {
    const feed = () => document.getElementById('timeline-feed');
    const act = (o = {}) => ({ id: 'a1', tool: 'memory_write', project: 'alpha', ts: ago(120), reversible: true, status: 'done', ...o });

    it('requests the last 20 actions and shows the empty state when none', async () => {
      const f = await open();
      expect(f.calls.find((c) => c.key === 'GET /app/api/timeline').url).toBe('/app/api/timeline?limit=20');
      expect(document.getElementById('timeline-empty').hidden).toBe(false);
    });

    it('treats a failing timeline request as empty', async () => {
      await open({ routes: { 'GET /app/api/timeline': () => jsonResponse({}, 500) } });
      expect(feed().children).toHaveLength(0);
      expect(document.getElementById('timeline-empty').hidden).toBe(false);
    });

    it('treats a 401 (login redirect in flight) as empty without errors', async () => {
      await open({ routes: { 'GET /app/api/timeline': () => jsonResponse({}, 401) } });
      expect(feed().children).toHaveLength(0);
      expect(document.getElementById('timeline-empty').hidden).toBe(false);
    });

    it('undo answered with 401 shows no stray toast', async () => {
      const f = await open({
        routes: {
          'GET /app/api/timeline': [act()],
          'POST /app/api/timeline/a1/undo': () => jsonResponse({}, 401),
        },
      });
      feed().querySelector('button').click();
      await flush();
      expect(f.calls.some((c) => c.key.startsWith('POST'))).toBe(true);
      expect(toasts()).toEqual([]);
    });

    it('renders summary or tool, falls back across timestamp fields, escapes text', async () => {
      await open({
        routes: { 'GET /app/api/timeline': [
          act({ id: '1', summary: '<i>Skrev</i>', reversible: false }),
          act({ id: '2', ts: undefined, created_at: ago(10), reversible: false }),
          act({ id: '3', ts: undefined, created_at: undefined, reversible: false }),
        ] },
      });
      const rows = [...feed().querySelectorAll('.act-row')];
      expect(rows[0].textContent).toBe('<i>Skrev</i> · alpha · 2 m');
      expect(rows[0].querySelector('i')).toBeNull();
      expect(rows[1].textContent).toBe('memory_write · alpha · nu');
      expect(rows[2].textContent).toBe('memory_write · alpha · ');
      expect(document.getElementById('timeline-empty').hidden).toBe(true);
    });

    it('offers undo only for reversible actions that are done', async () => {
      await open({
        routes: { 'GET /app/api/timeline': [
          act({ id: '1' }),
          act({ id: '2', reversible: false }),
          act({ id: '3', status: 'undone' }),
        ] },
      });
      const btns = [...feed().querySelectorAll('.act-row')].map((r) => r.querySelector('button')?.textContent ?? null);
      expect(btns).toEqual(['Ångra', null, null]);
    });

    it('undo posts to the encoded id, toasts success and re-renders the list', async () => {
      let listCalls = 0;
      const f = await open({
        routes: {
          'GET /app/api/timeline': () => jsonResponse(listCalls++ === 0 ? [act({ id: 'x/y' })] : []),
          'POST /app/api/timeline/x%2Fy/undo': { ok: true },
        },
      });
      feed().querySelector('button').click();
      await flush();
      const post = f.calls.find((c) => c.key.startsWith('POST'));
      expect(post.key).toBe('POST /app/api/timeline/x%2Fy/undo');
      expect(post.opts.body).toBeUndefined();
      expect(toasts()).toEqual([['Ångrad', 'toast toast-success']]);
      expect(listCalls).toBe(2);
      expect(feed().children).toHaveLength(0);
      expect(document.getElementById('timeline-empty').hidden).toBe(false);
    });

    it('toasts the server error when undo returns ok:false, or the default text', async () => {
      const post = [{ ok: false, error: 'Redan ändrad' }, { ok: false }];
      await open({
        routes: {
          'GET /app/api/timeline': [act()],
          'POST /app/api/timeline/a1/undo': () => jsonResponse(post.shift()),
        },
      });
      feed().querySelector('button').click();
      await flush();
      feed().querySelector('button').click();
      await flush();
      expect(toasts()).toEqual([
        ['Redan ändrad', 'toast toast-error'],
        ['Kunde inte ångra', 'toast toast-error'],
      ]);
    });

    it('maps a 409 to the conflict message and other failures to their message', async () => {
      const results = [jsonResponse({ error: 'stale' }, 409), jsonResponse({ error: 'kaputt' }, 500)];
      let lists = 0;
      await open({
        routes: {
          'GET /app/api/timeline': () => { lists++; return jsonResponse([act()]); },
          'POST /app/api/timeline/a1/undo': () => results.shift(),
        },
      });
      feed().querySelector('button').click();
      await flush();
      feed().querySelector('button').click();
      await flush();
      expect(toasts()).toEqual([
        ['Konflikt', 'toast toast-error'],
        ['kaputt', 'toast toast-error'],
      ]);
      expect(lists).toBe(3); // list re-rendered after each failure
    });
  });
});
