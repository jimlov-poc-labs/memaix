// SPDX-License-Identifier: AGPL-3.0-or-later
// Admin read views: users / projects / audit / system
// (FEATURE-WEB-UI-OUTBOX-AND-ADMIN.md §1.3). All tables are DOM-built.

(async () => {
  const me = await window.ME;
  if (!me) return;
  if (!me.is_admin) {
    document.getElementById('admin-denied').hidden = false;
    document.getElementById('admin-tabs').hidden = true;
    document.querySelectorAll('.admin-pane').forEach((p) => { p.hidden = true; });
    return;
  }

  const panes = ['users', 'projects', 'audit', 'system'];
  document.querySelectorAll('#admin-tabs .tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('#admin-tabs .tab').forEach((x) => x.classList.remove('tab-active'));
      tab.classList.add('tab-active');
      panes.forEach((p) => {
        document.getElementById(`admin-${p}`).hidden = p !== tab.dataset.tab;
      });
    });
  });

  const table = (headers, rows) => {
    const tbl = document.createElement('table');
    tbl.className = 'admin-table';
    const thead = document.createElement('thead');
    const tr = document.createElement('tr');
    headers.forEach((h) => { const th = document.createElement('th'); th.textContent = h; tr.append(th); });
    thead.append(tr);
    const tbody = document.createElement('tbody');
    rows.forEach((cells) => {
      const trb = document.createElement('tr');
      cells.forEach((c) => {
        const td = document.createElement('td');
        if (c instanceof Node) td.append(c); else td.textContent = String(c);
        trb.append(td);
      });
      tbody.append(trb);
    });
    tbl.append(thead, tbody);
    return tbl;
  };

  const roleChips = (grants) => {
    const span = document.createElement('span');
    span.textContent = Object.entries(grants).map(([p, r]) => `${p}:${r}`).join('  ') || '—';
    return span;
  };

  // --- Users -------------------------------------------------------------
  try {
    const usersRaw = await api('GET', '/app/api/admin/users');
    const users = Array.isArray(usersRaw) ? usersRaw : [];
    const rows = users.map((u) => [
      u.id,
      u.admin ? '🛡' : '',
      u.disabled ? t('web_admin_disabled') : '',
      roleChips(u.grants),
    ]);
    document.getElementById('admin-users').append(
      table([t('web_admin_user'), 'Admin', 'Status', t('web_admin_grants')], rows));
  } catch (e) { toast(e.message, 'error'); }

  // --- Projects ----------------------------------------------------------
  try {
    const projects = await api('GET', '/app/api/admin/projects');
    const rows = projects.map((p) => [
      p.name, p.allow_send ? '✓' : '✗', p.outbox, p.users,
      p.vault.length > 40 ? '…' + p.vault.slice(-38) : p.vault,
    ]);
    document.getElementById('admin-projects').append(
      table([t('web_admin_project'), 'allow_send', 'outbox', t('web_admin_users'), 'vault'], rows));
  } catch (e) { toast(e.message, 'error'); }

  // --- Audit ---------------------------------------------------------------
  const tbody = document.getElementById('audit-tbody');
  const moreBtn = document.getElementById('audit-more');
  let offset = 0;

  const auditRow = (entry) => {
    const tr = document.createElement('tr');
    if (!entry.ok) tr.className = 'audit-row-error';
    [relTime(entry.ts), entry.user, entry.project, entry.tool,
     entry.ok ? '✓' : '✗', entry.detail ?? ''].forEach((c) => {
      const td = document.createElement('td');
      td.textContent = String(c);
      tr.append(td);
    });
    return tr;
  };

  const loadAudit = async (reset) => {
    if (reset) { offset = 0; tbody.textContent = ''; }
    const params = new URLSearchParams();
    const userF = document.getElementById('audit-user').value.trim();
    const projF = document.getElementById('audit-project').value.trim();
    const toolF = document.getElementById('audit-tool').value.trim();
    const okF = document.getElementById('audit-ok').value;
    const sinceF = document.getElementById('audit-since').value;
    if (userF) params.set('user', userF);
    if (projF) params.set('project', projF);
    if (toolF) params.set('tool', toolF);
    if (okF) params.set('ok', okF);
    if (sinceF) params.set('since', sinceF);
    params.set('offset', String(offset));
    params.set('limit', '50');
    try {
      const data = await api('GET', `/app/api/admin/audit?${params}`);
      data.entries.forEach((e) => tbody.append(auditRow(e)));
      offset += data.entries.length;
      moreBtn.hidden = !data.has_more;
    } catch (e) { toast(e.message, 'error'); }
  };

  document.getElementById('audit-filter').addEventListener('submit', (e) => {
    e.preventDefault();
    loadAudit(true);
  });
  moreBtn.addEventListener('click', () => loadAudit(false));
  loadAudit(true);

  // --- System ---------------------------------------------------------------
  try {
    const sys = await api('GET', '/app/api/admin/system');
    const statusMark = { PASS: '✅', WARN: '⚠️', FAIL: '❌', SKIP: '⏭' };
    const rows = sys.checks.map((c) => [statusMark[c.status] ?? c.status, c.name, c.message]);
    document.getElementById('admin-system').append(
      table(['', t('web_admin_check'), t('web_admin_detail')], rows));
  } catch (e) { toast(e.message, 'error'); }

  // --- AI-modell (CHOOSE-YOUR-LLM.md; skriver memaix.yaml model-block) ------
  try {
    const llm = await api('GET', '/app/api/admin/llm');
    const box = document.createElement('div');
    box.className = 'llm-settings';
    const h = document.createElement('h3');
    h.textContent = t('web_admin_llm_title');
    box.append(h);

    const field = (labelKey, el) => {
      const label = document.createElement('label');
      label.textContent = t(labelKey) + ' ';
      label.append(el);
      label.style.display = 'block';
      label.style.margin = '.5rem 0';
      box.append(label);
      return el;
    };

    const provider = document.createElement('select');
    llm.providers.forEach((p) => {
      const o = document.createElement('option');
      o.value = p;
      o.textContent = p === 'byo' ? t('web_admin_llm_byo') : p;
      if (p === llm.provider) o.selected = true;
      provider.append(o);
    });
    field('web_admin_llm_provider', provider);

    const name = field('web_admin_llm_model', document.createElement('input'));
    name.value = llm.name || '';
    name.placeholder = 'claude-sonnet-4-5 / gpt-… / gemini-…';
    const endpoint = field('web_admin_llm_endpoint', document.createElement('input'));
    endpoint.value = llm.endpoint || '';
    endpoint.placeholder = 'http://192.168.x.x:11434 (lokalt nät eller molninstans)'; // NOSONAR — placeholder text, not an actual HTTP connection
    const key = field('web_admin_llm_key', document.createElement('input'));
    key.type = 'password';
    key.placeholder = llm.has_key ? t('web_admin_llm_key_kept') : 'sk-…';

    const sync = () => {
      const p = provider.value;
      const isByo = p === 'byo';
      const isEndpoint = ['openai-compatible', 'ollama', 'vllm'].includes(p);
      [name, endpoint, key].forEach((el) => { el.parentElement.hidden = isByo; });
      endpoint.parentElement.hidden = isByo || (!isEndpoint && !llm.endpoint);
    };
    provider.addEventListener('change', sync);
    sync();

    const save = document.createElement('button');
    save.className = 'btn btn-primary';
    save.textContent = t('web_admin_llm_save');
    save.addEventListener('click', async () => {
      try {
        const body = { provider: provider.value, name: name.value, endpoint: endpoint.value };
        if (key.value) body.api_key = key.value;
        await api('PUT', '/app/api/admin/llm', body);
        key.value = '';
        toast(t('web_admin_llm_saved'), 'success');
      } catch (e) { toast(e.message, 'error'); }
    });

    const test = document.createElement('button');
    test.className = 'btn';
    test.textContent = t('web_admin_llm_test');
    test.addEventListener('click', async () => {
      test.disabled = true;
      test.textContent = t('web_admin_llm_testing');
      try {
        const r = await api('POST', '/app/api/admin/llm/test');
        toast(`${r.provider}/${r.model} — ${r.latency_ms} ms ✓`, 'success');
      } catch (e) { toast(e.message, 'error'); }
      test.disabled = false;
      test.textContent = t('web_admin_llm_test');
    });
    box.append(save, document.createTextNode(' '), test);
    document.getElementById('admin-system').append(box);
  } catch (e) { toast(e.message, 'error'); }
})();

// --- MFA + write operations (Fas D) -----------------------------------------
(async () => {
  const me = await window.ME;
  if (!me || !me.is_admin) return;

  const usersPane = document.getElementById('admin-users');
  const bar = document.createElement('div');
  bar.className = 'settings-actions';
  usersPane.prepend(bar);

  const mfaStatus = await api('GET', '/app/api/admin/mfa').catch(() => null);
  if (!mfaStatus) return;

  const codeInput = document.createElement('input');
  codeInput.placeholder = '123456';
  codeInput.maxLength = 6;
  codeInput.className = 'mfa-code';

  if (!mfaStatus.enrolled) {
    const setupBtn = document.createElement('button');
    setupBtn.className = 'btn btn-primary';
    setupBtn.textContent = t('web_mfa_setup');
    setupBtn.addEventListener('click', async () => {
      try {
        const start = await api('POST', '/app/api/admin/mfa/setup/start');
        const box = document.createElement('div');
        const p = document.createElement('p');
        p.textContent = t('web_mfa_setup_help');
        const uri = document.createElement('code');
        uri.className = 'mono';
        uri.textContent = start.otpauth_uri;
        const secretLine = document.createElement('p');
        secretLine.className = 'mono';
        secretLine.textContent = `${t('web_mfa_secret')}: ${start.secret}`;
        const input = document.createElement('input');
        input.placeholder = '123456';
        const confirmBtn = document.createElement('button');
        confirmBtn.className = 'btn btn-primary';
        confirmBtn.textContent = t('web_mfa_confirm');
        box.append(p, uri, secretLine, input, confirmBtn);
        const m = modal(box);
        confirmBtn.addEventListener('click', async () => {
          try {
            await api('POST', '/app/api/admin/mfa/setup', { code: input.value });
            toast(t('web_mfa_enrolled'), 'success');
            m.close();
            location.reload();
          } catch (e) { toast(e.message, 'error'); }
        });
      } catch (e) { toast(e.message, 'error'); }
    });
    bar.append(setupBtn);
    return; // write ops need enrollment first
  }

  if (!mfaStatus.verified) {
    const verifyBtn = document.createElement('button');
    verifyBtn.className = 'btn btn-primary';
    verifyBtn.textContent = t('web_mfa_verify');
    verifyBtn.addEventListener('click', async () => {
      try {
        await api('POST', '/app/api/admin/mfa/verify', { code: codeInput.value });
        toast(t('web_mfa_verified'), 'success');
        location.reload();
      } catch (e) { toast(e.message, 'error'); }
    });
    bar.append(codeInput, verifyBtn);
    return;
  }

  // MFA verified — enable kill-switch toggles on the users table.
  const note = document.createElement('span');
  note.className = 'muted';
  note.textContent = t('web_mfa_active');
  bar.append(note);

  // .catch() fångar bara ett avvisat löfte. Ett anrop som *resolvar* med ett
  // felobjekt — vilket händer när sessionen dött och API:t svarar 401/403 —
  // gav en icke-lista rakt in i for...of, som kastade utanför try-blocket och
  // dog som ett obehandlat JS-fel i konsolen.
  const usersRaw = await api('GET', '/app/api/admin/users').catch(() => []);
  const users = Array.isArray(usersRaw) ? usersRaw : [];
  for (const u of users) {
    if (u.id === me.user) continue;
    const btn = document.createElement('button');
    btn.className = u.disabled ? 'btn' : 'btn btn-danger';
    btn.textContent = `${u.disabled ? t('web_admin_enable') : t('web_admin_disable')}: ${u.id}`;
    btn.addEventListener('click', async () => {
      try {
        await api('PATCH', `/app/api/admin/users/${encodeURIComponent(u.id)}`,
                  { disabled: !u.disabled });
        toast(t('web_saved'), 'success');
        location.reload();
      } catch (e) {
        toast(e.payload?.error === 'last_admin' ? t('web_admin_last_admin') : e.message, 'error');
      }
    });
    bar.append(btn);
  }
})();
