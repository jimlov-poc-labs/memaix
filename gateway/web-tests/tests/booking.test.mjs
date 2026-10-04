// SPDX-License-Identifier: AGPL-3.0-or-later
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { flush, jsonResponse, loadApp, mockFetch, mountPage, runPage } from './helpers.mjs';

beforeAll(loadApp);

const I18N = {
  web_saved: 'Sparat', web_booking_err_forbidden: 'Förbjudet', web_booking_err_save: 'Kunde inte spara',
  web_booking_err_need_time: 'Ange en tid', web_booking_date_closed: 'Stängt', web_booking_date_open_short: 'Öppet',
  web_booking_parity_even: 'Jämna', web_booking_parity_odd: 'Udda', web_booking_min: 'min', web_booking_standard: 'Standard',
  web_booking_type_new: 'Ny', web_booking_type_edit: 'Redigera typ', web_booking_remove: 'Ta bort', web_booking_edit: 'Ändra',
  web_booking_add_time: 'Lägg till tid', web_booking_even_weeks: 'Jämna veckor', web_booking_odd_weeks: 'Udda veckor',
  web_booking_all_weeks: 'Alla veckor',
  web_booking_day_mon: 'Mån', web_booking_day_tue: 'Tis', web_booking_day_wed: 'Ons', web_booking_day_thu: 'Tors',
  web_booking_day_fri: 'Fre', web_booking_day_sat: 'Lör', web_booking_day_sun: 'Sön',
};

const baseState = () => ({
  enabled: false, calendar_mode: 'own', tz: 'Europe/Stockholm', week: {}, weeks: {}, dates: {},
  blocks: [], max_per_day: null, meeting_types: [],
});

let state;
beforeEach(() => { state = baseState(); window.I18N = I18N; });

const $ = (id) => document.getElementById(id);
const toasts = () => [...document.querySelectorAll('#toast-container .toast')].map((x) => [x.className, x.textContent]);
const submit = (form) => {
  const ev = new Event('submit', { cancelable: true, bubbles: true });
  $(form).dispatchEvent(ev);
  return ev;
};
const type = (id, value) => { $(id).value = value; };
const input = (el, value) => { el.value = value; el.dispatchEvent(new Event('input', { bubbles: true })); };
const texts = (sel) => [...document.querySelectorAll(sel)].map((e) => e.textContent);
const button = (root, label) => [...root.querySelectorAll('button')].find((b) => b.getAttribute('aria-label') === label || b.textContent === label);

// A tiny stand-in for the server: schedule writes replace the keys they carry, then GET returns the new state.
function server(overrides = {}) {
  return {
    'GET /app/api/booking': () => jsonResponse(state),
    'POST /app/api/booking/schedule': (url, o) => {
      const { project, ...rest } = JSON.parse(o.body);
      // JSON null clears a key back to its empty value (see _check_extras on the server)
      const cleared = { weeks: {}, dates: {}, blocks: [], max_per_day: null };
      for (const [k, v] of Object.entries(rest)) state[k] = v === null && k in cleared ? cleared[k] : v;
      return jsonResponse(state);
    },
    'POST /app/api/booking/enabled': (url, o) => { state.enabled = JSON.parse(o.body).enabled; return jsonResponse({ ok: true }); },
    'POST /app/api/booking/meeting-types': () => jsonResponse({ ok: true }),
    ...overrides,
  };
}

async function open({ routes = server(), me = { projects: ['alpha', 'beta'] }, search = '' } = {}) {
  mountPage('booking');
  document.body.insertAdjacentHTML('beforeend', '<div id="toast-container"></div>');
  history.replaceState(null, '', `/app/booking${search}`);
  window.ME = Promise.resolve(me);
  const f = mockFetch(routes);
  await runPage('booking');
  await flush();
  return f;
}
const posts = (f, path = 'schedule') => f.calls.filter((c) => c.key === `POST /app/api/booking/${path}`).map((c) => JSON.parse(c.opts.body));
const lastPost = (f, path) => posts(f, path).at(-1);

describe('loading', () => {
  it('does nothing when not logged in', async () => {
    mountPage('booking');
    window.ME = Promise.resolve(null);
    const f = mockFetch({});
    await runPage('booking');
    expect(f.calls).toHaveLength(0);
  });

  it('prefers ?project=, then localStorage, then the first project', async () => {
    let f = await open({ search: '?project=beta' });
    expect(f.calls[0].url).toBe('/app/api/booking?project=beta');
    localStorage.setItem('memaix_project', 'gamma');
    f = await open();
    expect(f.calls[0].url).toBe('/app/api/booking?project=gamma');
    localStorage.clear();
    f = await open();
    expect(f.calls[0].url).toBe('/app/api/booking?project=alpha');
    f = await open({ me: { projects: [] } });
    expect(f.calls[0].url).toBe('/app/api/booking?project=');
  });

  it('shows the error when the initial load fails, and a 403 as "forbidden"', async () => {
    await open({ routes: { 'GET /app/api/booking': () => jsonResponse({ error: 'db down' }, 500) } });
    expect(toasts()).toEqual([['toast toast-error', 'Kunde inte spara (db down)']]);
    await open({ routes: { 'GET /app/api/booking': () => jsonResponse({ error: 'no' }, 403) } });
    expect(toasts()).toEqual([['toast toast-error', 'Förbjudet']]);
  });

  it('renders the saved settings and builds the weekday dropdown', async () => {
    state = { ...baseState(), enabled: true, max_per_day: 4, calendar_mode: 'none' };
    await open();
    expect($('booking-enabled').checked).toBe(true);
    expect($('booking-cap-input').value).toBe('4');
    expect($('booking-tz').value).toBe('Europe/Stockholm');
    expect($('booking-nocal').hidden).toBe(false);
    expect([...$('rep-weekday').options].map((o) => [o.value, o.textContent])).toEqual(
      [['mon', 'Mån'], ['tue', 'Tis'], ['wed', 'Ons'], ['thu', 'Tors'], ['fri', 'Fre'], ['sat', 'Lör'], ['sun', 'Sön']],
    );
    expect($('date-windows').hidden).toBe(true);
  });

  it('hides the no-calendar hint for other modes and leaves the cap empty for no limit', async () => {
    await open();
    expect($('booking-nocal').hidden).toBe(true);
    expect($('booking-cap-input').value).toBe('');
    expect($('booking-enabled').checked).toBe(false);
  });
});

describe('on/off', () => {
  it('saves the enabled flag with the project and confirms', async () => {
    const f = await open({ search: '?project=beta' });
    $('booking-enabled').checked = true;
    expect(submit('onoff-form').defaultPrevented).toBe(true);
    await flush();
    expect(lastPost(f, 'enabled')).toEqual({ project: 'beta', enabled: true });
    expect($('onoff-status').textContent).toBe('Sparat');
    expect($('onoff-status').className).toBe('save-status ok');
    expect(toasts()).toEqual([['toast toast-success', 'Sparat']]);
  });

  it('a 403 is reported as forbidden in the status line and a toast', async () => {
    await open({ routes: server({ 'POST /app/api/booking/enabled': () => jsonResponse({ error: 'x' }, 403) }) });
    submit('onoff-form');
    await flush();
    expect($('onoff-status').textContent).toBe('Förbjudet');
    expect($('onoff-status').className).toBe('save-status err');
    expect(toasts()).toEqual([['toast toast-error', 'Förbjudet']]);
  });

  it('other failures include the server message', async () => {
    await open({ routes: server({ 'POST /app/api/booking/enabled': () => jsonResponse({ error: 'boom' }, 500) }) });
    submit('onoff-form');
    await flush();
    expect($('onoff-status').textContent).toBe('Kunde inte spara (boom)');
  });
});

describe('weekly hours', () => {
  it('draws an editor per weekday with the saved windows', async () => {
    state.week = { mon: [{ start: '08:00', end: '12:00' }, { start: '13:00', end: '17:00' }], fri: [{ start: '09:00', end: '15:00' }] };
    await open();
    const rowsOf = (day) => [...document.querySelectorAll('.day-row')].find((r) => r.querySelector('.day-name').textContent === day);
    expect(document.querySelectorAll('.day-row')).toHaveLength(7);
    const monInputs = [...rowsOf('Mån').querySelectorAll('input[type=time]')].map((i) => i.value);
    expect(monInputs).toEqual(['08:00', '12:00', '13:00', '17:00']);
    expect(rowsOf('Tis').querySelectorAll('.window-row')).toHaveLength(0);
    expect(document.querySelector('fieldset legend').textContent).toBe('Alla veckor');
    expect(rowsOf('Mån').querySelector('input').getAttribute('aria-label')).toBe('Alla veckor Mån: web_booking_from');
    expect($('booking-alt-weeks').checked).toBe(false);
  });

  it('adds, edits and removes windows, and saves only days that have times', async () => {
    state.week = { mon: [{ start: '08:00', end: '12:00' }] };
    const f = await open();
    const day = (n) => document.querySelectorAll('.day-row')[n];
    // edit Monday's end
    input(day(0).querySelectorAll('input')[1], '11:30');
    // add a window on Tuesday (default 09:00-17:00), then change its start
    button(day(1), 'Alla veckor Tis: Lägg till tid').click();
    expect(day(1).querySelectorAll('.window-row')).toHaveLength(1);
    input(day(1).querySelector('input'), '10:00');
    // add and remove a window on Wednesday
    button(day(2), 'Alla veckor Ons: Lägg till tid').click();
    button(day(2), 'Alla veckor Ons: Ta bort').click();
    expect(day(2).querySelectorAll('.window-row')).toHaveLength(0);
    type('booking-tz', '  Europe/Oslo ');
    submit('hours-form');
    await flush();
    expect(lastPost(f)).toEqual({
      project: 'alpha', tz: 'Europe/Oslo', weeks: null,
      week: { mon: [{ start: '08:00', end: '11:30' }], tue: [{ start: '10:00', end: '17:00' }] },
    });
    expect($('hours-status').textContent).toBe('Sparat');
  });

  it('ignores half-filled windows', async () => {
    const f = await open();
    const mon = document.querySelectorAll('.day-row')[0];
    button(mon, 'Alla veckor Mån: Lägg till tid').click();
    input(mon.querySelector('input'), '');
    submit('hours-form');
    await flush();
    expect(lastPost(f).week).toEqual({});
  });

  it('removing the first of two windows keeps the right one', async () => {
    state.week = { mon: [{ start: '08:00', end: '09:00' }, { start: '10:00', end: '11:00' }] };
    const f = await open();
    button(document.querySelectorAll('.day-row')[0], 'Alla veckor Mån: Ta bort').click();
    submit('hours-form');
    await flush();
    expect(lastPost(f).week).toEqual({ mon: [{ start: '10:00', end: '11:00' }] });
  });

  it('renders separate even/odd grids when the host has alternating weeks, falling back to the plain week', async () => {
    state.week = { mon: [{ start: '07:00', end: '08:00' }] };
    state.weeks = { even: { tue: [{ start: '09:00', end: '10:00' }] } };
    await open();
    expect($('booking-alt-weeks').checked).toBe(true);
    expect(texts('fieldset legend')).toEqual(['Jämna veckor', 'Udda veckor']);
    const [even, odd] = document.querySelectorAll('fieldset');
    expect(even.querySelectorAll('.window-row')).toHaveLength(1);
    expect(odd.querySelector('input').value).toBe('07:00');
  });

  it('saves alternating weeks as {even, odd} without a plain week', async () => {
    state.weeks = { even: { mon: [{ start: '08:00', end: '09:00' }] }, odd: { tue: [{ start: '10:00', end: '11:00' }] } };
    const f = await open();
    submit('hours-form');
    await flush();
    expect(lastPost(f)).toEqual({
      project: 'alpha', tz: 'Europe/Stockholm',
      weeks: { even: { mon: [{ start: '08:00', end: '09:00' }] }, odd: { tue: [{ start: '10:00', end: '11:00' }] } },
    });
  });

  it('switching to alternating weeks copies the current times to both grids; switching back keeps the even grid', async () => {
    state.week = { mon: [{ start: '08:00', end: '12:00' }] };
    const f = await open();
    $('booking-alt-weeks').checked = true;
    $('booking-alt-weeks').dispatchEvent(new Event('change', { bubbles: true }));
    expect(document.querySelectorAll('fieldset')).toHaveLength(2);
    const odd = document.querySelectorAll('fieldset')[1];
    expect(odd.querySelector('input').value).toBe('08:00');
    // edit the even grid, then go back
    input(document.querySelectorAll('fieldset')[0].querySelector('input'), '06:00');
    $('booking-alt-weeks').checked = false;
    $('booking-alt-weeks').dispatchEvent(new Event('change', { bubbles: true }));
    expect(document.querySelectorAll('fieldset')).toHaveLength(1);
    expect(document.querySelector('.day-row input').value).toBe('06:00');
    submit('hours-form');
    await flush();
    expect(lastPost(f)).toMatchObject({ week: { mon: [{ start: '06:00', end: '12:00' }] }, weeks: null });
  });
});

describe('closed days / extra days', () => {
  it('lists days sorted, describing closed and open days, and shows the empty state', async () => {
    await open();
    expect($('dates-empty').hidden).toBe(false);
    state.dates = { '2026-12-24': [], '2026-01-05': [{ start: '10:00', end: '12:00' }, { start: '13:00', end: '14:00' }] };
    await open();
    expect(texts('#dates-list .list-text')).toEqual(['2026-01-05 — Öppet 10:00–12:00, 13:00–14:00', '2026-12-24 — Stängt']);
    expect($('dates-empty').hidden).toBe(true);
  });

  it('removing a day posts the remaining dates only', async () => {
    state.dates = { '2026-01-05': [], '2026-02-01': [] };
    const f = await open();
    button($('dates-list'), 'Ta bort 2026-01-05').click();
    await flush();
    expect(lastPost(f)).toEqual({ project: 'alpha', dates: { '2026-02-01': [] } });
    expect(texts('#dates-list .list-text')).toEqual(['2026-02-01 — Stängt']);
    expect($('dates-status').textContent).toBe('Sparat');
  });

  it('adds a closed day while keeping the existing ones, then resets the form', async () => {
    state.dates = { '2026-01-05': [] };
    const f = await open();
    type('date-day', '2026-03-03');
    submit('dates-form');
    await flush();
    expect(lastPost(f)).toEqual({ project: 'alpha', dates: { '2026-01-05': [], '2026-03-03': [] } });
    expect($('date-day').value).toBe('');
    expect(texts('#dates-list .list-text')).toHaveLength(2);
  });

  it('shows the time editor only for "open" days and posts the windows', async () => {
    const f = await open();
    type('date-kind', 'open');
    $('date-kind').dispatchEvent(new Event('change'));
    expect($('date-windows').hidden).toBe(false);
    input($('date-windows').querySelectorAll('input')[0], '11:00');
    type('date-day', '2026-04-04');
    submit('dates-form');
    await flush();
    expect(lastPost(f).dates).toEqual({ '2026-04-04': [{ start: '11:00', end: '17:00' }] });
    expect($('date-kind').value).toBe('closed');
    expect($('date-windows').hidden).toBe(true);
    type('date-kind', 'closed');
    $('date-kind').dispatchEvent(new Event('change'));
    expect($('date-windows').hidden).toBe(true);
  });

  it('refuses an open day without any time and sends nothing', async () => {
    const f = await open();
    type('date-kind', 'open');
    $('date-kind').dispatchEvent(new Event('change'));
    button($('date-windows'), 'web_booking_date_open: Ta bort').click();
    type('date-day', '2026-04-04');
    f.calls.length = 0;
    submit('dates-form');
    await flush();
    expect(f.calls).toHaveLength(0);
    expect($('dates-status').textContent).toBe('Ange en tid');
    expect($('dates-status').className).toBe('save-status err');
  });

  it('keeps the form filled in when saving fails', async () => {
    await open({ routes: server({ 'POST /app/api/booking/schedule': () => jsonResponse({ error: 'bad date' }, 400) }) });
    type('date-day', '2026-03-03');
    submit('dates-form');
    await flush();
    expect($('date-day').value).toBe('2026-03-03');
    expect($('dates-status').textContent).toBe('Kunde inte spara (bad date)');
  });
});

describe('blocks', () => {
  it('describes one-off (same day / multi-day) and repeating blocks, with parity', async () => {
    state.blocks = [
      { start: '2026-05-01T09:00:00+02:00', end: '2026-05-01T10:30:00+02:00' },
      { start: '2026-05-01T09:00:00+02:00', end: '2026-05-03T10:30:00+02:00' },
      { weekday: 'wed', start: '12:00', end: '13:00' },
      { weekday: 'fri', start: '14:00', end: '15:00', parity: 'even' },
    ];
    await open();
    expect(texts('#blocks-list .list-text')).toEqual([
      '2026-05-01 09:00–10:30', '2026-05-01 09:00 – 2026-05-03 10:30', 'Ons 12:00–13:00', 'Fre 14:00–15:00, jämna',
    ]);
    expect($('blocks-empty').hidden).toBe(true);
  });

  it('shows the empty state without blocks', async () => {
    await open();
    expect($('blocks-empty').hidden).toBe(false);
  });

  it('removes exactly the clicked block', async () => {
    state.blocks = [
      { weekday: 'mon', start: '08:00', end: '09:00' },
      { weekday: 'tue', start: '08:00', end: '09:00' },
      { weekday: 'wed', start: '08:00', end: '09:00' },
    ];
    const f = await open();
    button($('blocks-list'), 'Ta bort Tis 08:00–09:00').click();
    await flush();
    expect(lastPost(f).blocks.map((b) => b.weekday)).toEqual(['mon', 'wed']);
    expect($('once-status').textContent).toBe('Sparat');
  });

  const fillOnce = (sd, st, ed, et) => { type('once-start-date', sd); type('once-start-time', st); type('once-end-date', ed); type('once-end-time', et); };

  it.each([
    ['Europe/Stockholm', '2026-01-15', '+01:00'],
    ['Europe/Stockholm', '2026-07-15', '+02:00'],
    ['America/New_York', '2026-01-15', '-05:00'],
    ['America/New_York', '2026-07-15', '-04:00'],
    ['Asia/Kolkata', '2026-07-15', '+05:30'],
    ['UTC', '2026-07-15', '+00:00'],
  ])('one-off block uses the host zone offset (%s on %s -> %s)', async (tz, day, off) => {
    state.tz = tz;
    const f = await open();
    fillOnce(day, '09:00', day, '10:15');
    submit('block-once-form');
    await flush();
    expect(lastPost(f).blocks).toEqual([{ start: `${day}T09:00:00${off}`, end: `${day}T10:15:00${off}` }]);
  });

  it('gets the offset right across a DST change (Stockholm, 2026-03-29)', async () => {
    const f = await open();
    fillOnce('2026-03-29', '01:00', '2026-03-29', '04:00');
    submit('block-once-form');
    await flush();
    expect(lastPost(f).blocks).toEqual([{ start: '2026-03-29T01:00:00+01:00', end: '2026-03-29T04:00:00+02:00' }]);
  });

  it('appends the one-off block to existing ones and clears the form', async () => {
    state.blocks = [{ weekday: 'mon', start: '08:00', end: '09:00' }];
    const f = await open();
    fillOnce('2026-06-01', '08:00', '2026-06-01', '09:00');
    submit('block-once-form');
    await flush();
    expect(lastPost(f).blocks).toHaveLength(2);
    expect(lastPost(f).blocks[0]).toEqual({ weekday: 'mon', start: '08:00', end: '09:00' });
    expect($('once-start-date').value).toBe('');
    expect(texts('#blocks-list .list-text')).toHaveLength(2);
  });

  it('reports an unusable time zone instead of crashing', async () => {
    state.tz = 'Not/AZone';
    const f = await open();
    fillOnce('2026-06-01', '08:00', '2026-06-01', '09:00');
    f.calls.length = 0;
    submit('block-once-form');
    await flush();
    expect(f.calls).toHaveLength(0);
    expect($('once-status').className).toBe('save-status err');
    expect($('once-status').textContent).toContain('Kunde inte spara (');
    expect($('once-start-date').value).toBe('2026-06-01');
  });

  it('repeating block: sends weekday, times and parity only when chosen', async () => {
    const f = await open();
    type('rep-weekday', 'thu'); type('rep-start', '12:00'); type('rep-end', '13:00');
    submit('block-repeat-form');
    await flush();
    expect(lastPost(f).blocks).toEqual([{ weekday: 'thu', start: '12:00', end: '13:00' }]);
    expect($('rep-start').value).toBe('');
    type('rep-weekday', 'sat'); type('rep-start', '10:00'); type('rep-end', '11:00'); type('rep-parity', 'odd');
    submit('block-repeat-form');
    await flush();
    expect(lastPost(f).blocks.at(-1)).toEqual({ weekday: 'sat', start: '10:00', end: '11:00', parity: 'odd' });
    expect($('repeat-status').textContent).toBe('Sparat');
  });
});

describe('unsaved edits survive saving another section', () => {
  it('keeps half-edited hours when the on/off switch is saved', async () => {
    state.week = { mon: [{ start: '09:00', end: '17:00' }] };
    const f = await open();
    const from = document.querySelector('.week-grid .window-row input[type=time]');
    input(from, '10:30');
    $('booking-enabled').checked = true;
    submit('onoff-form');
    await flush();
    expect(lastPost(f, 'enabled')).toEqual({ project: 'alpha', enabled: true });
    expect(document.querySelector('.week-grid .window-row input[type=time]').value).toBe('10:30');
    submit('hours-form');
    await flush();
    expect(lastPost(f).week.mon).toEqual([{ start: '10:30', end: '17:00' }]);
  });

  it('keeps an unsaved cap and on/off when the hours are saved, then shows the server hours', async () => {
    const f = await open();
    type('booking-cap-input', '7');
    $('booking-enabled').checked = true;
    submit('hours-form');
    await flush();
    expect($('booking-cap-input').value).toBe('7');
    expect($('booking-enabled').checked).toBe(true);
    expect(f.calls.at(-1).key).toBe('GET /app/api/booking');
  });
});

describe('session expired while saving', () => {
  it('shows no toast or status when both the save and the refresh are answered with 401', async () => {
    let gets = 0;
    await open({ routes: server({
      'GET /app/api/booking': () => (gets++ === 0 ? jsonResponse(state) : jsonResponse({}, 401)),
      'POST /app/api/booking/enabled': () => jsonResponse({}, 401),
    }) });
    vi.stubGlobal('window', { location: '' }); // api() assigns window.location on 401; keep jsdom's real one intact
    $('booking-enabled').checked = true;
    submit('onoff-form');
    await flush();
    expect(gets).toBe(2);
    expect(toasts()).toEqual([]);
    expect($('onoff-status').textContent).toBe('');
  });
});

describe('daily cap', () => {
  it.each([['5', 5], ['12', 12], ['', null]])('sends %j as %j', async (raw, sent) => {
    const f = await open();
    type('booking-cap-input', raw);
    submit('cap-form');
    await flush();
    expect(lastPost(f)).toEqual({ project: 'alpha', max_per_day: sent });
    expect($('cap-status').textContent).toBe('Sparat');
  });

  it('shows the server value again after saving', async () => {
    const f = await open();
    type('booking-cap-input', '3');
    submit('cap-form');
    await flush();
    expect($('booking-cap-input').value).toBe('3');
    expect(f.calls.at(-1).key).toBe('GET /app/api/booking');
  });
});

describe('session lengths', () => {
  const types = () => [
    { slug: 'quick', name: 'Quick chat', duration_min: 15, default: true },
    { slug: 'deep dive', name: '<b>Deep</b>', duration_min: 90, default: false },
  ];

  it('lists lengths with a badge on the standard one, as text', async () => {
    state.meeting_types = types();
    await open();
    expect(texts('#types-list .list-text')).toEqual(['Quick chat — 15 min', '<b>Deep</b> — 90 min']);
    expect(document.querySelector('#types-list b')).toBeNull();
    expect(texts('#types-list .badge')).toEqual(['Standard']);
    expect($('types-empty').hidden).toBe(true);
  });

  it('shows the empty state', async () => {
    await open();
    expect($('types-empty').hidden).toBe(false);
  });

  it('creates a new length without a slug and resets the form', async () => {
    const f = await open();
    type('type-name', '  Intro ');
    type('type-minutes', '20');
    $('type-default').checked = true;
    submit('type-form');
    await flush();
    expect(lastPost(f, 'meeting-types')).toEqual({ project: 'alpha', name: 'Intro', duration_min: 20, default: true });
    expect($('type-name').value).toBe('');
    expect($('type-default').checked).toBe(false);
    expect($('type-status').textContent).toBe('Sparat');
  });

  it('edit fills the form, switches the title, and saves with the slug', async () => {
    state.meeting_types = types();
    const f = await open();
    button($('types-list'), 'Ändra Quick chat').click();
    expect($('type-slug').value).toBe('quick');
    expect($('type-name').value).toBe('Quick chat');
    expect($('type-minutes').value).toBe('15');
    expect($('type-default').checked).toBe(true);
    expect($('type-cancel').hidden).toBe(false);
    expect($('type-form-title').textContent).toBe('Redigera typ');
    expect(document.activeElement).toBe($('type-name'));
    type('type-minutes', '25');
    submit('type-form');
    await flush();
    expect(lastPost(f, 'meeting-types')).toEqual({ project: 'alpha', name: 'Quick chat', duration_min: 25, default: true, slug: 'quick' });
    expect($('type-slug').value).toBe('');
    expect($('type-form-title').textContent).toBe('Ny');
  });

  it('cancel leaves edit mode and clears the form', async () => {
    state.meeting_types = types();
    await open();
    button($('types-list'), 'Ändra Quick chat').click();
    $('type-cancel').click();
    expect($('type-slug').value).toBe('');
    expect($('type-name').value).toBe('');
    expect($('type-cancel').hidden).toBe(true);
    expect($('type-form-title').textContent).toBe('Ny');
  });

  it('delete sends DELETE with the encoded slug and the project', async () => {
    state.meeting_types = types();
    const f = await open({
      search: '?project=beta',
      routes: server({ 'DELETE /app/api/booking/meeting-types/deep%20dive': () => { state.meeting_types = [types()[0]]; return jsonResponse({ ok: true }); } }),
    });
    button($('types-list'), 'Ta bort <b>Deep</b>').click();
    await flush();
    const del = f.calls.find((c) => c.opts.method === 'DELETE');
    expect(del.url).toBe('/app/api/booking/meeting-types/deep%20dive?project=beta');
    expect(texts('#types-list .list-text')).toEqual(['Quick chat — 15 min']);
    expect($('type-status').textContent).toBe('Sparat');
  });

  it('shows a failed delete', async () => {
    state.meeting_types = types();
    await open({ routes: server({ 'DELETE /app/api/booking/meeting-types/quick': () => jsonResponse({ error: 'last one' }, 400) }) });
    button($('types-list'), 'Ta bort Quick chat').click();
    await flush();
    expect($('type-status').textContent).toBe('Kunde inte spara (last one)');
    expect(texts('#types-list .list-text')).toHaveLength(2);
  });
});
