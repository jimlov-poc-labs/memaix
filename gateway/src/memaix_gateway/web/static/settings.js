// SPDX-License-Identifier: AGPL-3.0-or-later
// Settings page: linked accounts + calendar mode (FEATURE-WEB-UI-MVP.md §1.2).

(async () => {
  const me = await window.ME;
  if (!me) return;
  const project = new URLSearchParams(location.search).get('project')
        ?? localStorage.getItem('memaix_project') ?? me.projects[0] ?? '';

  // --- Linked accounts --------------------------------------------------
  const list = document.getElementById('accounts-list');

  // Which projects may use this account, for one capability. Rendered under
  // the account it belongs to rather than in a separate card: the question
  // is "who may use THIS mailbox", and splitting it out would mean listing
  // every account twice.
  //
  // `projects` arrives from the server as either ['*'] (every project the
  // user can see, re-evaluated on each use — so a project added tomorrow is
  // included without revisiting this page) or an explicit list. An empty
  // list is the opt-in default and is called out in words, because "no
  // boxes ticked" reads identically to "hasn't loaded yet".
  const renderScopes = (acc) => {
    const wrap = document.createElement('div');
    wrap.className = 'scope-grid';
    const granted = acc.scopes_by_capability ?? {};
    const shared = (acc.capabilities ?? []).some((c) => (granted[c] ?? []).length > 0);

    if (!shared) {
      const warn = document.createElement('div');
      warn.className = 'muted scope-unshared';
      warn.textContent = t('web_settings_scope_none');
      wrap.append(warn);
    }

    for (const capability of acc.capabilities ?? []) {
      const current = granted[capability] ?? [];
      const row = document.createElement('div');
      row.className = 'scope-row';

      const heading = document.createElement('span');
      heading.className = 'scope-capability';
      // t() echoes the key back when it's missing, so a capability added by
      // a future adapter degrades to its bare name rather than to
      // "web_settings_capability_chat" staring back at the user.
      const capKey = `web_settings_capability_${capability}`;
      const capLabel = t(capKey);
      heading.textContent = capLabel === capKey ? capability : capLabel;
      row.append(heading);

      const boxes = [];
      const save = async (projects) => {
        try {
          await api('POST', '/app/api/accounts/scopes', {
            provider: acc.provider, account: acc.account, capability, projects,
          });
          toast(t('web_saved'), 'success');
          renderAccounts();
        } catch (e) { toast(e.message, 'error'); renderAccounts(); }
      };

      const all = document.createElement('label');
      const allBox = document.createElement('input');
      allBox.type = 'checkbox';
      allBox.checked = current.includes('*');
      const allText = document.createElement('span');
      allText.textContent = t('web_settings_scope_all');
      all.append(allBox, allText);
      allBox.addEventListener('change', () => {
        // '*' is stored as a wildcard, not expanded into today's project
        // list — unticking it therefore clears everything rather than
        // leaving a frozen snapshot behind.
        save(allBox.checked ? ['*'] : []);
      });
      row.append(all);

      for (const proj of me.projects ?? []) {
        const label = document.createElement('label');
        const box = document.createElement('input');
        box.type = 'checkbox';
        box.value = proj;
        box.checked = current.includes('*') || current.includes(proj);
        box.disabled = current.includes('*');
        const text = document.createElement('span');
        text.textContent = proj;
        label.append(box, text);
        box.addEventListener('change', () => {
          save(boxes.filter((b) => b.checked).map((b) => b.value));
        });
        boxes.push(box);
        row.append(label);
      }
      wrap.append(row);
    }
    return wrap;
  };

  const renderAccounts = async () => {
    list.textContent = '';
    let accounts = [];
    try { accounts = await api('GET', '/app/api/accounts'); } catch { /* keep empty */ }
    document.getElementById('accounts-empty').hidden = accounts.length > 0;
    for (const acc of accounts) {
      const li = document.createElement('li');
      li.className = 'account-item';
      const head = document.createElement('div');
      head.className = 'account-head';
      const dot = document.createElement('span');
      dot.textContent = acc.readonly ? '🔵' : (acc.status === 'active' ? '🟢' : '🟡');
      const label = document.createElement('span');
      label.className = 'account-label';
      const providerLabel = acc.provider === 'imap' ? 'IMAP' : acc.provider;
      const projectSuffix = acc.project ? ` (${acc.project})` : '';
      label.textContent = `${providerLabel} · ${acc.account}${projectSuffix}`;
      head.append(dot, label);
      if (acc.status === 'needs_relink') {
        const note = document.createElement('span');
        note.className = 'muted';
        note.textContent = t('web_settings_needs_relink');
        head.append(note);
      }
      if (!acc.readonly) {
        const unlink = document.createElement('button');
        unlink.className = 'btn btn-danger';
        unlink.textContent = t('web_settings_unlink');
        unlink.addEventListener('click', async () => {
          try {
            await api('DELETE', `/app/api/accounts/${encodeURIComponent(acc.provider)}?account=${encodeURIComponent(acc.account)}`);
            toast(t('web_settings_unlinked'), 'success');
            renderAccounts();
          } catch (e) { toast(e.message, 'error'); }
        });
        head.append(unlink);
      }
      li.append(head);
      // Shared acl.yaml mailboxes belong to the project, not to the user —
      // there is nothing for the user to scope, so no grid is drawn.
      if (!acc.readonly && (acc.capabilities ?? []).length > 0) {
        li.append(renderScopes(acc));
      }
      list.append(li);
    }
  };
  renderAccounts();

  const linkFlow = async (provider) => {
    try {
      const res = await api('GET', `/app/api/accounts/link/${provider}`);
      if (res?.url) {
        window.open(res.url, '_blank', 'width=600,height=700');
        toast(t('web_settings_link_started'), 'info');
        // Poll for the new account while the OAuth window is open.
        const poll = setInterval(renderAccounts, 4000);
        setTimeout(() => clearInterval(poll), 120_000);
      }
    } catch (e) { toast(e.message, 'error'); }
  };
  document.getElementById('link-google')?.addEventListener('click', () => linkFlow('google'));
  document.getElementById('link-microsoft')?.addEventListener('click', () => linkFlow('microsoft'));

  // --- IMAP mailbox linking (non-OAuth credential form) ------------------
  document.getElementById('imap-link-form')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = {
      account_email: document.getElementById('imap-account-email').value,
      host: document.getElementById('imap-host').value,
      user: document.getElementById('imap-user').value,
      password: document.getElementById('imap-password').value,
    };
    const portVal = document.getElementById('imap-port').value;
    if (portVal) body.port = Number(portVal);
    try {
      await api('POST', '/app/api/accounts/link-imap', body);
      toast(t('web_saved'), 'success');
      e.target.reset();
      renderAccounts();
    } catch (err) { toast(err.message, 'error'); }
  });

  // --- Calendar mode -----------------------------------------------------
  const select = document.getElementById('calendar-mode-select');
  const icalInput = document.getElementById('calendar-ical-url');
  const calIdInput = document.getElementById('calendar-calendar-id');
  const current = document.getElementById('calendar-current');

  const syncInputs = () => {
    icalInput.hidden = select.value !== 'ical_secret';
    calIdInput.hidden = select.value !== 'free_busy';
  };
  select.addEventListener('change', syncInputs);

  try {
    const status = await api('GET', `/app/api/settings/calendar-mode?project=${encodeURIComponent(project)}`);
    current.textContent = `${t('web_settings_calendar_active')}: ${status.active_mode}`;
    if (status.active_mode !== 'none') select.value = status.active_mode;
  } catch { current.textContent = ''; }
  syncInputs();

  document.getElementById('calendar-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = { project, mode: select.value };
    if (select.value === 'ical_secret') body.ical_url = icalInput.value;
    if (select.value === 'free_busy') body.calendar_id = calIdInput.value;
    try {
      const res = await api('POST', '/app/api/settings/calendar-mode', body);
      if (res.link_url || res.next) {
        toast(res.next ?? res.link_url, 'info');
        if (res.link_url) window.open(res.link_url, '_blank', 'width=600,height=700');
      } else {
        toast(t('web_saved'), 'success');
      }
      current.textContent = `${t('web_settings_calendar_active')}: ${select.value}`;
    } catch (err) { toast(err.message, 'error'); }
  });
})();

// --- Daily brief (Fas D) ---------------------------------------------------
(async () => {
  const form = document.getElementById('brief-form');
  if (!form) return;
  const enabled = document.getElementById('brief-enabled');
  const timeEl = document.getElementById('brief-time');
  const tzEl = document.getElementById('brief-timezone');
  const statusEl = document.getElementById('brief-status');

  try {
    const brief = await api('GET', '/app/api/brief');
    if (brief.configured) {
      enabled.checked = !!brief.prefs.enabled;
      if (brief.prefs.brief_time) timeEl.value = brief.prefs.brief_time;
      if (brief.prefs.timezone) tzEl.value = brief.prefs.timezone;
      statusEl.textContent = brief.next_run
        ? `${t('web_brief_next')}: ${new Date(brief.next_run).toLocaleString()}` : '';
    }
  } catch { /* not configured yet */ }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      const res = await api('POST', '/app/api/brief', {
        enabled: enabled.checked,
        brief_time: timeEl.value,
        timezone: tzEl.value || undefined,
      });
      statusEl.textContent = res.next_run
        ? `${t('web_brief_next')}: ${new Date(res.next_run).toLocaleString()}` : '';
      toast(t('web_saved'), 'success');
    } catch (err) { toast(err.message, 'error'); }
  });
})();
