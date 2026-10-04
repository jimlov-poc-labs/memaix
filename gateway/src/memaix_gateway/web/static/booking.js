// SPDX-License-Identifier: AGPL-3.0-or-later
// Booking page: the host controls hours, closed days, blocks, daily cap and
// session lengths. All writes go through /app/api/booking/*.

(async () => {
  const me = await window.ME;
  if (!me) return;
  const project = new URLSearchParams(location.search).get('project')
        ?? localStorage.getItem('memaix_project') ?? me.projects[0] ?? '';
  const q = `project=${encodeURIComponent(project)}`;
  const DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
  const $ = (id) => document.getElementById(id);
  const dayName = (d) => t(`web_booking_day_${d}`);

  const el = (tag, props = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = v;
      else if (k in n) n[k] = v;
      else n.setAttribute(k, v);
    }
    n.append(...kids);
    return n;
  };

  let state = null;

  // --- Feedback ----------------------------------------------------------
  const report = (statusId, ok, msg) => {
    const s = $(statusId);
    s.textContent = msg;
    s.className = `save-status ${ok ? 'ok' : 'err'}`;
    toast(msg, ok ? 'success' : 'error');
  };
  const friendly = (e) => {
    if (e.status === 403) return t('web_booking_err_forbidden');
    const detail = e.message ? ` (${e.message})` : '';
    return `${t('web_booking_err_save')}${detail}`;
  };
  const refresh = async () => {
    state = await api('GET', `/app/api/booking?${q}`);
    renderAll();
  };
  const saveWith = async (statusId, fn) => {
    try {
      await fn();
      await refresh();
      report(statusId, true, t('web_saved'));
    } catch (e) {
      report(statusId, false, friendly(e));
    }
  };
  const post = (path, body) => api('POST', `/app/api/booking/${path}`, { project, ...body });

  // --- Time-window editor (from–to rows with add/remove) -------------------
  const windowEditor = (initial, label) => {
    const rows = initial.map((w) => ({ start: w.start, end: w.end }));
    const wrap = el('div', { class: 'window-editor' });
    const render = () => {
      wrap.textContent = '';
      rows.forEach((row, i) => {
        const from = el('input', { type: 'time', value: row.start, 'aria-label': `${label}: ${t('web_booking_from')}` });
        const to = el('input', { type: 'time', value: row.end, 'aria-label': `${label}: ${t('web_booking_to')}` });
        from.addEventListener('input', () => { row.start = from.value; });
        to.addEventListener('input', () => { row.end = to.value; });
        const rm = el('button', {
          type: 'button', class: 'btn', text: t('web_booking_remove'),
          'aria-label': `${label}: ${t('web_booking_remove')}`,
        });
        rm.addEventListener('click', () => { rows.splice(i, 1); render(); });
        wrap.append(el('div', { class: 'window-row' }, from, el('span', { text: '–' }), to, rm));
      });
      const add = el('button', {
        type: 'button', class: 'btn', text: t('web_booking_add_time'),
        'aria-label': `${label}: ${t('web_booking_add_time')}`,
      });
      add.addEventListener('click', () => { rows.push({ start: '09:00', end: '17:00' }); render(); });
      wrap.append(add);
    };
    render();
    return { el: wrap, get: () => rows.filter((r) => r.start && r.end).map((r) => ({ ...r })) };
  };

  // --- Weekly hours ------------------------------------------------------
  const weekGrid = (title, week) => {
    const fs = el('fieldset', { class: 'week-grid' }, el('legend', { text: title }));
    const editors = {};
    for (const d of DAYS) {
      editors[d] = windowEditor(week[d] ?? [], `${title} ${dayName(d)}`);
      fs.append(el('div', { class: 'day-row' }, el('span', { class: 'day-name', text: dayName(d) }), editors[d].el));
    }
    const get = () => Object.fromEntries(
      DAYS.map((d) => [d, editors[d].get()]).filter(([, w]) => w.length > 0),
    );
    return { el: fs, get };
  };

  let grids = null; // {week} or {even, odd}
  const drawGrids = (weeks) => {
    const box = $('hours-grids');
    box.textContent = '';
    if (weeks) {
      grids = {
        even: weekGrid(t('web_booking_even_weeks'), weeks.even),
        odd: weekGrid(t('web_booking_odd_weeks'), weeks.odd),
      };
      box.append(grids.even.el, grids.odd.el);
    } else {
      grids = { week: weekGrid(t('web_booking_all_weeks'), weeks === null ? currentPlain : {}) };
      box.append(grids.week.el);
    }
  };
  let currentPlain = {};

  const renderHours = () => {
    const alt = Object.keys(state.weeks).length > 0;
    $('booking-alt-weeks').checked = alt;
    $('booking-tz').value = state.tz;
    currentPlain = state.week;
    drawGrids(alt ? { even: state.weeks.even ?? state.week, odd: state.weeks.odd ?? state.week } : null);
  };

  $('booking-alt-weeks').addEventListener('change', (e) => {
    if (e.target.checked) {
      const base = grids.week.get();
      drawGrids({ even: base, odd: base });
    } else {
      currentPlain = grids.even.get();
      drawGrids(null);
    }
  });

  $('hours-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const body = { tz: $('booking-tz').value.trim() };
    if (grids.week) {
      body.week = grids.week.get();
      body.weeks = null;
    } else {
      body.weeks = { even: grids.even.get(), odd: grids.odd.get() };
    }
    saveWith('hours-status', () => post('schedule', body));
  });

  // --- On/off ------------------------------------------------------------
  $('onoff-form').addEventListener('submit', (e) => {
    e.preventDefault();
    saveWith('onoff-status', () => post('enabled', { enabled: $('booking-enabled').checked }));
  });

  // --- Closed days / extra days -------------------------------------------
  let dateEditor = null;
  const resetDateForm = () => {
    $('dates-form').reset();
    dateEditor = windowEditor([{ start: '09:00', end: '17:00' }], t('web_booking_date_open'));
    $('date-windows').textContent = '';
    $('date-windows').append(dateEditor.el);
    $('date-windows').hidden = true;
  };
  $('date-kind').addEventListener('change', () => {
    $('date-windows').hidden = $('date-kind').value !== 'open';
  });

  const describeWindows = (windows) => {
    if (windows.length === 0) return t('web_booking_date_closed');
    const spans = windows.map((w) => `${w.start}–${w.end}`).join(', ');
    return `${t('web_booking_date_open_short')} ${spans}`;
  };

  const removeButton = (label, onClick) => {
    const b = el('button', { type: 'button', class: 'btn btn-danger', text: t('web_booking_remove'), 'aria-label': `${t('web_booking_remove')} ${label}` });
    b.addEventListener('click', onClick);
    return b;
  };

  const renderDates = () => {
    const list = $('dates-list');
    list.textContent = '';
    const days = Object.keys(state.dates).sort((a, b) => a.localeCompare(b));
    $('dates-empty').hidden = days.length > 0;
    for (const day of days) {
      const li = el('li', {}, el('span', { class: 'list-text', text: `${day} — ${describeWindows(state.dates[day])}` }));
      li.append(removeButton(day, () => {
        const rest = { ...state.dates };
        delete rest[day];
        saveWith('dates-status', () => post('schedule', { dates: rest }));
      }));
      list.append(li);
    }
  };

  $('dates-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const open = $('date-kind').value === 'open';
    const windows = open ? dateEditor.get() : [];
    if (open && windows.length === 0) {
      report('dates-status', false, t('web_booking_err_need_time'));
      return;
    }
    saveWith('dates-status', async () => {
      await post('schedule', { dates: { ...state.dates, [$('date-day').value]: windows } });
      resetDateForm();
    });
  });

  // --- Blocks -----------------------------------------------------------
  const offsetMinutes = (tz, utcMs) => {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: tz, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    }).formatToParts(new Date(utcMs));
    const g = (type) => Number(parts.find((p) => p.type === type).value);
    return Math.round((Date.UTC(g('year'), g('month') - 1, g('day'), g('hour'), g('minute'), g('second')) - utcMs) / 60000);
  };
  // Wall-clock time in the host's zone -> ISO string with that zone's UTC offset.
  const zonedIso = (date, time, tz) => {
    const [y, m, d] = date.split('-').map(Number);
    const [hh, mm] = time.split(':').map(Number);
    const wall = Date.UTC(y, m - 1, d, hh, mm);
    const first = offsetMinutes(tz, wall);
    const off = offsetMinutes(tz, wall - first * 60000);
    const a = Math.abs(off);
    const pad = (n) => String(n).padStart(2, '0');
    return `${date}T${time}:00${off < 0 ? '-' : '+'}${pad(Math.floor(a / 60))}:${pad(a % 60)}`;
  };

  const describeBlock = (b) => {
    if (b.weekday) {
      const parityLabel = b.parity ? t(`web_booking_parity_${b.parity}`).toLowerCase() : '';
      const parity = parityLabel ? `, ${parityLabel}` : '';
      return `${dayName(b.weekday)} ${b.start}–${b.end}${parity}`;
    }
    const [sd, st] = [b.start.slice(0, 10), b.start.slice(11, 16)];
    const [ed, et] = [b.end.slice(0, 10), b.end.slice(11, 16)];
    return sd === ed ? `${sd} ${st}–${et}` : `${sd} ${st} – ${ed} ${et}`;
  };

  const removeBlockAt = (index) => {
    const blocks = state.blocks.filter((_, j) => j !== index);
    saveWith('once-status', () => post('schedule', { blocks }));
  };

  const renderBlocks = () => {
    const list = $('blocks-list');
    list.textContent = '';
    $('blocks-empty').hidden = state.blocks.length > 0;
    state.blocks.forEach((b, i) => {
      const li = el('li', {}, el('span', { class: 'list-text', text: describeBlock(b) }));
      li.append(removeButton(describeBlock(b), () => removeBlockAt(i)));
      list.append(li);
    });
  };

  $('block-once-form').addEventListener('submit', (e) => {
    e.preventDefault();
    saveWith('once-status', async () => {
      const block = {
        start: zonedIso($('once-start-date').value, $('once-start-time').value, state.tz),
        end: zonedIso($('once-end-date').value, $('once-end-time').value, state.tz),
      };
      await post('schedule', { blocks: [...state.blocks, block] });
      e.target.reset();
    });
  });

  $('block-repeat-form').addEventListener('submit', (e) => {
    e.preventDefault();
    saveWith('repeat-status', async () => {
      const block = { weekday: $('rep-weekday').value, start: $('rep-start').value, end: $('rep-end').value };
      if ($('rep-parity').value) block.parity = $('rep-parity').value;
      await post('schedule', { blocks: [...state.blocks, block] });
      e.target.reset();
    });
  });

  for (const d of DAYS) $('rep-weekday').append(el('option', { value: d, text: dayName(d) }));

  // --- Daily cap ---------------------------------------------------------
  $('cap-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const raw = $('booking-cap-input').value.trim();
    saveWith('cap-status', () => post('schedule', { max_per_day: raw === '' ? null : Number(raw) }));
  });

  // --- Session lengths -----------------------------------------------------
  const resetTypeForm = () => {
    $('type-form').reset();
    $('type-slug').value = '';
    $('type-cancel').hidden = true;
    $('type-form-title').textContent = t('web_booking_type_new');
  };
  $('type-cancel').addEventListener('click', resetTypeForm);

  const renderTypes = () => {
    const list = $('types-list');
    list.textContent = '';
    $('types-empty').hidden = state.meeting_types.length > 0;
    for (const mt of state.meeting_types) {
      const text = `${mt.name} — ${mt.duration_min} ${t('web_booking_min')}`;
      const li = el('li', {}, el('span', { class: 'list-text', text }));
      if (mt.default) li.append(el('span', { class: 'badge badge-success', text: t('web_booking_standard') }));
      const edit = el('button', { type: 'button', class: 'btn', text: t('web_booking_edit'), 'aria-label': `${t('web_booking_edit')} ${mt.name}` });
      edit.addEventListener('click', () => {
        $('type-slug').value = mt.slug;
        $('type-name').value = mt.name;
        $('type-minutes').value = mt.duration_min;
        $('type-default').checked = mt.default;
        $('type-cancel').hidden = false;
        $('type-form-title').textContent = t('web_booking_type_edit');
        $('type-name').focus();
      });
      li.append(edit, removeButton(mt.name, () => {
        saveWith('type-status', () => api('DELETE', `/app/api/booking/meeting-types/${encodeURIComponent(mt.slug)}?${q}`));
      }));
      list.append(li);
    }
  };

  $('type-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const body = {
      name: $('type-name').value.trim(),
      duration_min: Number($('type-minutes').value),
      default: $('type-default').checked,
    };
    if ($('type-slug').value) body.slug = $('type-slug').value;
    saveWith('type-status', async () => {
      await post('meeting-types', body);
      resetTypeForm();
    });
  });

  // --- Render everything from `state` --------------------------------------
  const renderAll = () => {
    $('booking-enabled').checked = state.enabled;
    $('booking-nocal').hidden = state.calendar_mode !== 'none';
    $('booking-cap-input').value = state.max_per_day ?? '';
    renderHours();
    renderDates();
    renderBlocks();
    renderTypes();
  };

  resetDateForm();
  try {
    await refresh();
  } catch (e) {
    toast(friendly(e), 'error');
  }
})();
