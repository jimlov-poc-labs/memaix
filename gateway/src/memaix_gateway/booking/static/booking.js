/* SPDX-License-Identifier: AGPL-3.0-or-later
 * Memaix booking widget — the whole booking flow as one embeddable file.
 * ---------------------------------------------------------------------
 * Drop this on any page:
 *
 *   <div data-memaix-booking="<slug>"></div>
 *   <script src="https://mcp.memaix.se/embed/booking.js" defer></script>
 *
 * and you get the grid, the form, the captcha and the error handling. The
 * slug is public by construction (it identifies a booking page; it doesn't
 * authorise anything), which is why it can sit in plain HTML.
 *
 * This exists because the same flow was hand-copied into memaix.se/boka and
 * jimlov.se/boka, and both shipped the same broken slot grid on the same
 * day. One file, served from the gateway, means a fix lands everywhere on
 * deploy instead of everywhere someone remembers.
 *
 * Everything below builds DOM with createElement/textContent. Times, labels
 * and error codes all come off the wire, and none of it is ever parsed as
 * markup — the widget renders on other people's origins, so "the gateway is
 * trusted" is not an assumption worth making.
 *
 * Theming is CSS custom properties on the mount element (--mxb-accent and
 * friends, see CSS below). The host page keeps its own look; the widget only
 * insists on layout.
 */
(function () {
  'use strict';

  var SELF = document.currentScript;
  var DEFAULT_GATEWAY = SELF ? new URL(SELF.src, location.href).origin : location.origin;

  var STRINGS = {
    en: {
      loading: 'Loading available times…',
      empty: 'No open times right now — please get in touch instead.',
      selected: 'Selected',
      name: 'Name',
      email: 'Email',
      purpose: 'What would you like to talk about? (optional)',
      form: 'How should we meet?',
      length: 'Session length',
      back: 'Back',
      confirm: 'Confirm',
      booking: 'Booking…',
      dismiss: 'Dismiss',
      booked: 'Booked.',
      done: function (day, time) {
        return 'Your meeting is booked for ' + day + ' at ' + time +
          '. A confirmation is on its way to your inbox.';
      },
      consent: 'I consent to my name, email address and what I write here being ' +
        'stored to book and confirm the meeting. The data is kept for one year ' +
        'after the meeting, then deleted. No account is created.',
      errors: {
        not_found: "This booking link doesn't exist.",
        slot_unavailable: 'That time was just taken — pick another one.',
        captcha_failed: 'Captcha check failed — please try again.',
        rate_limited: 'Too many attempts — please wait a moment and try again.',
        consent_required: 'You need to agree to the data storage to book a meeting.',
        invalid_duration: 'That session length is not available — pick another.',
        invalid_meeting_form: 'That meeting option is no longer available — pick another.',
        meeting_form_unavailable: "We couldn't set up the meeting link — please try again shortly.",
        network: 'Something went wrong — please try again shortly.',
        unknown: 'Something went wrong — please try again shortly.'
      }
    },
    sv: {
      loading: 'Hämtar lediga tider…',
      empty: 'Inga lediga tider just nu — hör av dig så löser vi det.',
      selected: 'Vald tid',
      name: 'Namn',
      email: 'E-post',
      purpose: 'Vad vill du prata om? (frivilligt)',
      form: 'Hur ska vi ses?',
      length: 'Sessionslängd',
      back: 'Tillbaka',
      confirm: 'Boka',
      booking: 'Bokar…',
      dismiss: 'Stäng',
      booked: 'Bokat.',
      done: function (day, time) {
        return 'Ditt möte är bokat ' + day + ' kl ' + time +
          '. En bekräftelse är på väg till din inkorg.';
      },
      consent: 'Jag samtycker till att mitt namn, min e-postadress och det jag ' +
        'skriver här lagras för att boka och bekräfta mötet. Uppgifterna sparas ' +
        'ett år efter mötet och raderas sedan. Inget konto skapas.',
      errors: {
        not_found: 'Den här bokningslänken finns inte.',
        slot_unavailable: 'Tiden blev precis bokad — välj en annan.',
        captcha_failed: 'Captcha-kontrollen misslyckades — försök igen.',
        rate_limited: 'För många försök — vänta en stund och försök igen.',
        consent_required: 'Du behöver godkänna lagringen för att kunna boka.',
        invalid_duration: 'Den sessionslängden finns inte — välj en annan.',
        invalid_meeting_form: 'Det mötesalternativet finns inte längre — välj ett annat.',
        meeting_form_unavailable: 'Vi kunde inte skapa möteslänken — försök igen om en stund.',
        network: 'Något gick fel — försök igen om en stund.',
        unknown: 'Något gick fel — försök igen om en stund.'
      }
    }
  };

  /* Scoped under .mxb so the host page's own reset can't reach in and the
     widget can't leak out. Every colour is a custom property with a fallback:
     an embedder themes it by setting --mxb-accent on the mount element, and
     one that sets nothing still gets something that looks deliberate. */
  var CSS = [
    '.mxb{',
    '--mxb-accent:#3D7A8F;--mxb-accent-bg:rgba(61,122,143,0.07);',
    '--mxb-accent-bdr:rgba(61,122,143,0.45);--mxb-border:rgba(0,0,0,0.12);',
    '--mxb-text:#1a1a1a;--mxb-muted:#6b7280;--mxb-surface:#fff;',
    '--mxb-danger:#dc2626;--mxb-radius:10px;',
    '--mxb-font:inherit;--mxb-mono:ui-monospace,SFMono-Regular,Menlo,monospace;',
    'color:var(--mxb-text);font-family:var(--mxb-font);}',
    '.mxb *{box-sizing:border-box;}',
    '.mxb-hidden{display:none !important;}',
    '.mxb-head{display:flex;align-items:center;justify-content:space-between;',
    'gap:12px;margin-bottom:12px;}',
    '.mxb-tz{font-family:var(--mxb-mono);font-size:10px;color:var(--mxb-muted);}',
    '.mxb-msg{font-size:14px;color:var(--mxb-muted);margin:0;}',
    '.mxb-msg.mxb-bad{color:var(--mxb-danger);}',
    '.mxb-notice{display:flex;align-items:center;justify-content:space-between;',
    'gap:16px;padding:12px 16px;margin-bottom:20px;font-size:13px;',
    'border:1px solid rgba(220,38,38,0.3);border-radius:12px;',
    'background:rgba(220,38,38,0.04);color:var(--mxb-danger);}',
    '.mxb-notice button{background:none;border:none;color:var(--mxb-muted);',
    'cursor:pointer;font-size:18px;line-height:1;padding:0;}',
    '.mxb-scroll{overflow-x:auto;}',
    '.mxb-cal{border-collapse:collapse;width:100%;table-layout:fixed;}',
    '.mxb-cal th{padding:0 4px 8px;text-align:center;font-size:10px;font-weight:600;',
    'color:var(--mxb-muted);text-transform:uppercase;letter-spacing:0.05em;}',
    '.mxb-cal td{padding:6px 4px;border-top:1px solid var(--mxb-border);vertical-align:top;}',
    '.mxb-daynum{font-size:11px;color:var(--mxb-muted);text-align:center;',
    'margin-bottom:6px;font-family:var(--mxb-mono);}',
    '.mxb-col{display:flex;flex-direction:column;}',
    '.mxb-row{padding:2px 3px;border-radius:6px;}',
    '.mxb-row.mxb-alt{background:rgba(0,0,0,0.04);}',
    '.mxb-slot{width:100%;padding:5px 4px;font-size:12px;font-weight:500;',
    'font-family:var(--mxb-mono);border:1px solid var(--mxb-border);border-radius:8px;',
    'background:var(--mxb-surface);color:var(--mxb-accent);cursor:pointer;transition:all .12s;}',
    '.mxb-slot:hover{border-color:var(--mxb-accent-bdr);background:var(--mxb-accent-bg);',
    'transform:translateY(-1px);}',
    '.mxb-slot:focus-visible{outline:2px solid var(--mxb-accent);outline-offset:1px;}',
    '.mxb-gap{width:100%;padding:5px 4px;font-size:12px;font-family:var(--mxb-mono);',
    'border:1px solid transparent;visibility:hidden;}',
    '.mxb-form{max-width:420px;display:flex;flex-direction:column;gap:16px;}',
    '.mxb-picked{padding:12px 16px;border-radius:var(--mxb-radius);',
    'background:var(--mxb-accent-bg);border:1px solid var(--mxb-accent-bdr);}',
    '.mxb-picked-when{font-family:var(--mxb-mono);font-size:14px;font-weight:500;',
    'color:var(--mxb-text);}',
    '.mxb-label{display:block;margin-bottom:6px;font-size:11px;font-weight:500;',
    'color:var(--mxb-muted);}',
    '.mxb-field{width:100%;padding:10px 12px;font-size:14px;font-family:inherit;',
    'border:1px solid var(--mxb-border);border-radius:var(--mxb-radius);',
    'background:var(--mxb-surface);color:var(--mxb-text);outline:none;',
    'transition:border-color .15s,box-shadow .15s;}',
    '.mxb-field:focus{border-color:var(--mxb-accent-bdr);',
    'box-shadow:0 0 0 3px var(--mxb-accent-bg);}',
    '.mxb-consent{display:flex;align-items:flex-start;gap:8px;cursor:pointer;',
    'font-size:12px;color:var(--mxb-muted);line-height:1.5;}',
    '.mxb-consent input{margin-top:3px;flex:none;}',
    '.mxb-actions{display:flex;gap:12px;}',
    '.mxb-btn{padding:9px 18px;font-size:13px;font-weight:500;font-family:inherit;',
    'border-radius:var(--mxb-radius);cursor:pointer;transition:opacity .15s;}',
    '.mxb-btn[disabled]{opacity:.45;cursor:not-allowed;}',
    '.mxb-primary{border:1px solid var(--mxb-accent);background:var(--mxb-accent);color:#fff;}',
    '.mxb-ghost{border:1px solid var(--mxb-border);background:var(--mxb-surface);',
    'color:var(--mxb-text);}',
    '@media (max-width:640px){',
    '.mxb-cal th{font-size:9px;}.mxb-cal td{padding:4px 2px;}',
    '.mxb-slot,.mxb-gap{font-size:11px;padding:4px 2px;}.mxb-row{padding:1px 2px;}}'
  ].join('');

  function injectCss() {
    if (document.getElementById('mxb-css')) return;
    var style = document.createElement('style');
    style.id = 'mxb-css';
    style.textContent = CSS;
    document.head.appendChild(style);
  }

  /* Turnstile scans the DOM once on load, and our widget div is inside a
     panel that's still hidden then — so implicit rendering never finds it.
     Explicit render it is, which means waiting for the script. One loader for
     the whole page: two mounts must not inject two copies of the API. */
  var turnstilePromise = null;
  function loadTurnstile() {
    if (turnstilePromise) return turnstilePromise;
    turnstilePromise = new Promise(function (resolve, reject) {
      if (window.turnstile) { resolve(window.turnstile); return; }
      var cb = '__mxbTurnstile' + Date.now();
      window[cb] = function () { delete window[cb]; resolve(window.turnstile); };
      var s = document.createElement('script');
      s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload=' + cb;
      s.async = true;
      s.defer = true;
      s.onerror = function () { reject(new Error('network')); };
      document.head.appendChild(s);
    });
    return turnstilePromise;
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function mount(root) {
    var slug = root.getAttribute('data-memaix-booking');
    if (!slug) return;

    var gateway = root.getAttribute('data-gateway') || DEFAULT_GATEWAY;
    var langAttr = (root.getAttribute('data-lang') ||
      document.documentElement.lang || 'en').slice(0, 2).toLowerCase();
    var T = STRINGS[langAttr] || STRINGS.en;
    // The BCP-47 tag Intl formats with. Only the widget's own copy is
    // translated; dates follow the page's declared locale so a Swedish page
    // doesn't suddenly print "Sep" among its "sep".
    var locale = root.getAttribute('data-locale') ||
      document.documentElement.lang || (langAttr === 'sv' ? 'sv-SE' : 'en-SE');
    var visitorTz = Intl.DateTimeFormat().resolvedOptions().timeZone;

    var cfg = null;
    var times = [];
    var selected = null;
    var token = '';
    var widgetId = null;
    // widgetId alone can't answer "is a captcha on its way?" — it stays null
    // for the whole time Turnstile's script is loading, which is exactly the
    // window where the visitor hits Back. mounting covers that gap, and
    // captchaGen lets resetCaptcha() disown a load already in flight so it
    // can't render into a pane nobody is looking at any more.
    var mounting = false;
    var captchaGen = 0;
    var booked = false;

    // ---- chrome ----
    root.classList.add('mxb');
    root.replaceChildren();

    var tzLabel = el('span', 'mxb-tz', visitorTz);
    var head = el('div', 'mxb-head');
    head.appendChild(el('span'));
    head.appendChild(tzLabel);
    root.appendChild(head);

    var noticeText = el('span');
    var noticeClose = el('button', null, '×');
    noticeClose.setAttribute('aria-label', T.dismiss);
    noticeClose.type = 'button';
    var notice = el('div', 'mxb-notice mxb-hidden');
    notice.appendChild(noticeText);
    notice.appendChild(noticeClose);
    noticeClose.addEventListener('click', function () { notice.classList.add('mxb-hidden'); });
    root.appendChild(notice);

    var panes = {};
    panes.loading = el('p', 'mxb-msg', T.loading);
    panes.error = el('p', 'mxb-msg mxb-bad');
    panes.empty = el('p', 'mxb-msg', T.empty);
    panes.picking = el('div', 'mxb-scroll');
    panes.form = el('div', 'mxb-form');
    panes.done = el('div');

    var table = el('table', 'mxb-cal');
    var thead = el('thead');
    var headRow = el('tr');
    var tbody = el('tbody');
    thead.appendChild(headRow);
    table.appendChild(thead);
    table.appendChild(tbody);
    var lengthSelect = el('select');
    var lengthField = field(T.length, lengthSelect, 'mxb-' + Math.random().toString(36).slice(2, 8) + '-length');
    lengthField.classList.add('mxb-hidden');
    lengthSelect.addEventListener('change', function () { reload(); });
    panes.picking.appendChild(lengthField);
    panes.picking.appendChild(table);

    // ---- form ----
    var pickedWhen = el('div', 'mxb-picked-when');
    var picked = el('div', 'mxb-picked');
    picked.appendChild(el('div', 'mxb-label', T.selected));
    picked.appendChild(pickedWhen);

    function field(labelText, node, id) {
      var wrap = el('div');
      var label = el('label', 'mxb-label', labelText);
      label.htmlFor = id;
      node.id = id;
      node.className = 'mxb-field';
      wrap.appendChild(label);
      wrap.appendChild(node);
      return wrap;
    }

    var uid = 'mxb-' + Math.random().toString(36).slice(2, 8);
    var nameInput = el('input');
    nameInput.autocomplete = 'name';
    var emailInput = el('input');
    emailInput.type = 'email';
    emailInput.autocomplete = 'email';
    var purposeInput = el('textarea');
    purposeInput.rows = 3;
    purposeInput.style.resize = 'vertical';
    // The gateway truncates at 500 characters silently. Stopping the typing
    // here is the only way the visitor ever learns that — otherwise they write
    // a paragraph and the host reads two thirds of it.
    purposeInput.maxLength = 500;
    var formSelect = el('select');
    var formField = field(T.form, formSelect, uid + '-form');
    formField.classList.add('mxb-hidden');

    var consentBox = el('input');
    consentBox.type = 'checkbox';
    var consentLabel = el('label', 'mxb-consent');
    var consentText = el('span', null, T.consent);
    consentLabel.appendChild(consentBox);
    consentLabel.appendChild(consentText);

    var captcha = el('div');
    var backBtn = el('button', 'mxb-btn mxb-ghost', T.back);
    backBtn.type = 'button';
    var submitBtn = el('button', 'mxb-btn mxb-primary', T.confirm);
    submitBtn.type = 'button';
    submitBtn.disabled = true;
    var actions = el('div', 'mxb-actions');
    actions.appendChild(backBtn);
    actions.appendChild(submitBtn);

    panes.form.appendChild(picked);
    panes.form.appendChild(field(T.name, nameInput, uid + '-name'));
    panes.form.appendChild(field(T.email, emailInput, uid + '-email'));
    panes.form.appendChild(field(T.purpose, purposeInput, uid + '-purpose'));
    panes.form.appendChild(formField);
    panes.form.appendChild(consentLabel);
    panes.form.appendChild(captcha);
    panes.form.appendChild(actions);

    var doneText = el('p', 'mxb-msg');
    panes.done.appendChild(el('div', 'mxb-picked-when', T.booked));
    panes.done.appendChild(doneText);

    Object.keys(panes).forEach(function (key) {
      if (key !== 'loading') panes[key].classList.add('mxb-hidden');
      root.appendChild(panes[key]);
    });

    // ---- helpers ----
    function show(state) {
      Object.keys(panes).forEach(function (key) {
        panes[key].classList.toggle('mxb-hidden', key !== state);
      });
    }

    function fail(code) {
      panes.error.textContent = T.errors[code] || T.errors.unknown;
      show('error');
    }

    function warn(code) {
      noticeText.textContent = T.errors[code] || T.errors.unknown;
      notice.classList.remove('mxb-hidden');
    }

    var fmtTime = function (iso) {
      return new Date(iso).toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' });
    };
    var fmtDay = function (iso) {
      return new Date(iso).toLocaleDateString(locale, {
        weekday: 'short', day: 'numeric', month: 'short'
      });
    };
    // Minutes past local midnight — the key the week's shared time axis is
    // built on. Local to the *visitor*: the server aligned the grid to the
    // host's clock, but the person clicking sees their own.
    var minsOfDay = function (iso) {
      var d = new Date(iso);
      return d.getHours() * 60 + d.getMinutes();
    };

    // fetch() rejects with a TypeError when the request never reached the
    // gateway at all (offline, DNS, CORS refusal). Everything else got an
    // answer, and json() has already turned it into the gateway's own error
    // code. Sniffing the message text instead would break the day a browser
    // rewords "Failed to fetch".
    function codeOf(err) {
      return err instanceof TypeError ? 'network' : err.message;
    }

    function json(res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        if (!res.ok) throw new Error(body.error || 'unknown');
        return body;
      });
    }

    // ---- loading ----
    function loadConfig() {
      return fetch(gateway + '/book/' + encodeURIComponent(slug) + '/config')
        .then(json)
        .then(function (body) {
          cfg = body;
          if (body.consent_text) consentText.textContent = body.consent_text;
          renderMeetingForms(body.meeting_forms || []);
          renderSessionLengths(body.meeting_types || []);
        });
    }

    function renderSessionLengths(types) {
      lengthSelect.replaceChildren();
      if (types.length < 2) return;
      types.forEach(function (t) {
        var opt = el('option', null, t.name);
        opt.value = String(t.duration_min);
        if (t['default']) opt.selected = true;
        lengthSelect.appendChild(opt);
      });
      lengthField.classList.remove('mxb-hidden');
    }

    function renderMeetingForms(forms) {
      formSelect.replaceChildren();
      // One option isn't a choice, it's a fact — booking_create picks the
      // default anyway, so showing a select with a single entry would just be
      // a control that does nothing.
      if (forms.length < 2) return;
      forms.forEach(function (f) {
        var opt = el('option', null, f.label);
        opt.value = f.slug;
        if (f['default']) opt.selected = true;
        formSelect.appendChild(opt);
      });
      formField.classList.remove('mxb-hidden');
    }

    function loadTimes() {
      var days = parseInt(root.getAttribute('data-days'), 10);
      if (!(days > 0)) days = cfg && cfg.max_days_ahead ? cfg.max_days_ahead : 60;
      var from = new Date();
      var to = new Date();
      to.setDate(to.getDate() + days);

      var url = gateway + '/book/' + encodeURIComponent(slug) + '/times' +
        '?within_start=' + encodeURIComponent(from.toISOString()) +
        '&within_end=' + encodeURIComponent(to.toISOString());
      if (lengthSelect.value) url += '&duration_min=' + encodeURIComponent(lengthSelect.value);

      return fetch(url).then(json).then(function (body) {
        times = body.times || [];
        if (!times.length) { show('empty'); return; }
        renderCalendar(days);
        show('picking');
      });
    }

    function start() {
      show('loading');
      loadConfig()
        .then(loadTimes)
        .catch(function (err) { fail(codeOf(err)); });
    }

    function reload() {
      show('loading');
      loadTimes().catch(function (err) { fail(codeOf(err)); });
    }

    // Columns are the weekdays that actually have availability, in Mon-first
    // order — an always-empty Sunday column just wastes width on a phone.
    function renderCalendar(days) {
      var byDate = new Map();
      var activeDows = new Set();
      times.forEach(function (slot) {
        var d = new Date(slot.start);
        var key = d.toLocaleDateString('sv-SE');
        if (!byDate.has(key)) byDate.set(key, []);
        byDate.get(key).push(slot);
        activeDows.add(d.getDay());
      });

      var visible = [1, 2, 3, 4, 5, 6, 0].filter(function (d) { return activeDows.has(d); });

      var today = new Date();
      today.setHours(0, 0, 0, 0);
      var end = new Date(today);
      end.setDate(today.getDate() + days);
      var cur = new Date(today);
      cur.setDate(today.getDate() - ((today.getDay() + 6) % 7));

      headRow.replaceChildren();
      tbody.replaceChildren();
      var firstRow = true;

      while (cur < end) {
        var cells = visible.map(function (dow) {
          var day = new Date(cur);
          day.setDate(cur.getDate() + (dow === 0 ? 6 : dow - 1));
          var key = day.toLocaleDateString('sv-SE');
          var inWindow = day >= today && day < end;
          return {
            date: day, inWindow: inWindow,
            slots: inWindow ? (byDate.get(key) || []) : []
          };
        });
        if (!cells.some(function (c) { return c.inWindow; })) {
          cur.setDate(cur.getDate() + 7);
          continue;
        }

        /* One shared time axis per week. Packing each column from the top
           independently meant a day missing 16:00 showed 16:30 on the 16:00
           row, so times stopped lining up horizontally. Scoped per week
           rather than globally: a quiet week then costs its own few rows
           instead of the union of every time the whole window offers. */
        var axis = Array.from(new Set(cells.reduce(function (acc, c) {
          return acc.concat(c.slots.map(function (s) { return minsOfDay(s.start); }));
        }, []))).sort(function (a, b) { return a - b; });

        // A week with nothing on offer is skipped entirely. Over a 60-day
        // window a fully-booked autumn otherwise renders as eight rows of
        // bare date numbers below the last real time — a calendar the
        // visitor has to scroll past to learn nothing. This is a list of
        // offers, not a month view.
        if (!axis.length) {
          cur.setDate(cur.getDate() + 7);
          continue;
        }

        if (firstRow) {
          cells.forEach(function (c) {
            headRow.appendChild(el('th', null,
              c.date.toLocaleDateString(locale, { weekday: 'short' })));
          });
          firstRow = false;
        }

        var tr = el('tr');
        cells.forEach(function (c) {
          var td = el('td');
          td.appendChild(el('div', 'mxb-daynum',
            c.date.toLocaleDateString(locale, { day: 'numeric', month: 'short' })));

          var col = el('div', 'mxb-col');
          var byMin = new Map(c.slots.map(function (s) { return [minsOfDay(s.start), s]; }));
          axis.forEach(function (mins, i) {
            var row = el('div', 'mxb-row' + (i % 2 ? ' mxb-alt' : ''));
            var slot = byMin.get(mins);
            if (slot) {
              var btn = el('button', 'mxb-slot', fmtTime(slot.start));
              btn.type = 'button';
              btn.addEventListener('click', function () { pick(slot); });
              row.appendChild(btn);
            } else {
              // Hidden, not absent: it reserves the row so the times above
              // and below stay on their own lines, and it holds a real time
              // string so it matches the button it stands in for whatever the
              // font does. aria-hidden keeps a screen reader from announcing
              // a time that isn't offered.
              var gap = el('div', 'mxb-gap', '00:00');
              gap.setAttribute('aria-hidden', 'true');
              row.appendChild(gap);
            }
            col.appendChild(row);
          });
          td.appendChild(col);
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
        cur.setDate(cur.getDate() + 7);
      }
    }

    // ---- picking and booking ----
    function pick(slot) {
      selected = slot;
      notice.classList.add('mxb-hidden');
      pickedWhen.textContent = fmtDay(slot.start) + ' · ' +
        fmtTime(slot.start) + '–' + fmtTime(slot.end);
      show('form');
      mountCaptcha();
    }

    function mountCaptcha() {
      if (widgetId !== null || mounting) return;
      if (!cfg || !cfg.turnstile_site_key) {
        // Fail closed and say so. The gateway verifies the token server-side
        // and refuses when it can't, so a widget that quietly skipped the
        // captcha would just produce a confusing 403 on submit instead.
        fail('captcha_failed');
        return;
      }
      mounting = true;
      var gen = ++captchaGen;
      loadTurnstile().then(function (ts) {
        if (gen !== captchaGen) return;
        mounting = false;
        widgetId = ts.render(captcha, {
          sitekey: cfg.turnstile_site_key,
          callback: function (t) { token = t; refresh(); },
          'expired-callback': function () { token = ''; refresh(); }
        });
      }).catch(function () {
        if (gen !== captchaGen) return;
        mounting = false;
        fail('network');
      });
    }

    function resetCaptcha() {
      // Bumping the generation first is what makes an in-flight load harmless:
      // its .then() sees a stale gen and returns without rendering.
      captchaGen++;
      mounting = false;
      if (widgetId !== null && window.turnstile) window.turnstile.remove(widgetId);
      widgetId = null;
      token = '';
    }

    function refresh() {
      submitBtn.disabled = booked || !(nameInput.value.trim() && emailInput.value.trim() &&
        consentBox.checked && token);
    }

    function submit() {
      if (booked || submitBtn.disabled) return;
      submitBtn.disabled = true;
      submitBtn.textContent = T.booking;

      var payload = {
        start: selected.start,
        end: selected.end,
        name: nameInput.value.trim(),
        email: emailInput.value.trim(),
        purpose: purposeInput.value.trim(),
        turnstile_token: token,
        timezone: visitorTz,
        consent: true,
        // Verbatim what the visitor just read, not a code we resolve later —
        // the gateway stores this string as the record of what was agreed to.
        consent_text: consentText.textContent
      };
      if (formSelect.value) payload.meeting_form_slug = formSelect.value;

      fetch(gateway + '/book/' + encodeURIComponent(slug), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      }).then(json).then(function () {
        booked = true;
        resetCaptcha();
        doneText.textContent = T.done(fmtDay(selected.start), fmtTime(selected.start));
        show('done');
      }).catch(function (err) {
        var code = codeOf(err);
        // A taken time or a rate limit is recoverable — send them back to a
        // freshly reloaded grid rather than into a dead end.
        if (code === 'slot_unavailable' || code === 'rate_limited') {
          warn(code);
          selected = null;
          resetCaptcha();
          reload();
          return;
        }
        fail(code);
      }).then(function () {
        // This tail runs on every outcome, success included — and after a
        // success there is nothing left to confirm. Re-arming the button here
        // would offer the visitor a second booking with a spent captcha token,
        // and the gateway's honest 403 would land as "captcha failed" on top
        // of a booking that actually went through.
        if (booked) return;
        submitBtn.textContent = T.confirm;
        refresh();
      });
    }

    backBtn.addEventListener('click', function () {
      resetCaptcha();
      selected = null;
      show('picking');
    });
    submitBtn.addEventListener('click', submit);
    nameInput.addEventListener('input', refresh);
    emailInput.addEventListener('input', refresh);
    consentBox.addEventListener('change', refresh);

    start();
  }

  function boot() {
    injectCss();
    var nodes = document.querySelectorAll('[data-memaix-booking]');
    for (var i = 0; i < nodes.length; i++) {
      if (!nodes[i].classList.contains('mxb')) mount(nodes[i]);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
