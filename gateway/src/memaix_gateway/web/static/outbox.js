// SPDX-License-Identifier: AGPL-3.0-or-later
// Outbox page: approver-scoped list, preview modal, approve with optimistic
// row move + 409 handling, reject with inline reason form
// (FEATURE-WEB-UI-OUTBOX-AND-ADMIN.md §1). The server only returns actions
// this user may approve, so everything rendered is actionable.

(async () => {
  const me = await window.ME;
  if (!me) return;
  const list = document.getElementById('outbox-list');
  const empty = document.getElementById('outbox-empty');
  let status = 'pending';

  const statusColor = { pending: 'var(--warning)', executed: 'var(--success)',
                        rejected: 'var(--muted)', expired: 'var(--muted)',
                        failed: 'var(--danger)' };

  const preview = (action) => {
    const box = document.createElement('div');
    const h = document.createElement('h3');
    h.textContent = `${action.tool} · ${action.project}`;
    const pre = document.createElement('pre');
    pre.className = 'mono';
    pre.textContent = action.preview || JSON.stringify(action.args ?? {}, null, 2);
    box.append(h, pre);
    if (action.status !== 'pending') {
      const meta = document.createElement('p');
      meta.className = 'muted';
      meta.textContent = `${action.status}` +
        (action.decided_by ? ` · ${action.decided_by}` : '') +
        (action.result && action.result.reason ? ` · ${action.result.reason}` : '');
      box.append(meta);
    }
    modal(box);
  };

  const showLink = (url) => {
    const box = document.createElement('div');
    const h = document.createElement('h3');
    h.textContent = t('web_outbox_link_title');
    const hint = document.createElement('p');
    hint.className = 'muted';
    hint.textContent = t('web_outbox_link_hint');
    const ta = document.createElement('textarea');
    ta.readOnly = true;
    ta.rows = 5;
    ta.value = `${t('web_outbox_link_message')}\n${url}`;
    const copy = document.createElement('button');
    copy.className = 'btn btn-primary';
    copy.textContent = t('web_outbox_link_copy');
    copy.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(ta.value);
        toast(t('web_outbox_link_copied'), 'success');
      } catch {
        ta.select();
      }
    });
    box.append(h, hint, ta, copy);
    modal(box);
  };

  const decide = async (action, li, kind, reason = '') => {
    try {
      const path = `/app/api/outbox/${encodeURIComponent(action.id)}/${kind}`;
      const body = kind === 'reject' ? { reason } : null;
      li.style.opacity = '.4';
      const res = await api('POST', path, body);
      toast(kind === 'approve'
        ? (res.ok ? t('web_outbox_approved') : t('web_outbox_failed'))
        : t('web_outbox_rejected'), res.ok === false ? 'error' : 'success');
      const link = res.result?.invite_url ?? res.result?.reset_url;
      if (kind === 'approve' && res.ok && link) showLink(link);
      render();
    } catch (e) {
      if (e.status === 409) {
        toast(`${t('web_outbox_conflict')} ${e.payload?.decided_by ?? ''}`, 'warning');
        render();
      } else {
        li.style.opacity = '1';
        toast(e.message, 'error');
      }
    }
  };

  const row = (action) => {
    const li = document.createElement('li');
    li.className = 'card outbox-row';
    li.style.borderLeft = `3px solid ${statusColor[action.status] ?? 'var(--border)'}`;

    const head = document.createElement('div');
    head.className = 'outbox-row-head';
    const title = document.createElement('strong');
    title.textContent = `${action.tool} · ${action.project}`;
    const when = document.createElement('span');
    when.className = 'muted';
    when.textContent = relTime(action.created_at ?? action.ts ?? '');
    head.append(title, when);

    const prev = document.createElement('div');
    prev.className = 'muted outbox-preview';
    prev.textContent = action.preview ?? '';

    const actions = document.createElement('div');
    actions.className = 'outbox-actions';
    const previewBtn = document.createElement('button');
    previewBtn.className = 'btn';
    previewBtn.textContent = t('web_outbox_preview');
    previewBtn.addEventListener('click', () => preview(action));
    actions.append(previewBtn);

    if (action.status === 'pending') {
      const rejectBtn = document.createElement('button');
      rejectBtn.className = 'btn btn-danger';
      rejectBtn.textContent = t('web_outbox_reject');
      const approveBtn = document.createElement('button');
      approveBtn.className = 'btn btn-primary';
      approveBtn.textContent = t('web_outbox_approve');

      rejectBtn.addEventListener('click', () => {
        if (li.querySelector('.reject-form')) return;
        const form = document.createElement('div');
        form.className = 'reject-form surface';
        const textarea = document.createElement('textarea');
        textarea.placeholder = t('web_outbox_reject_reason');
        const confirmBtn = document.createElement('button');
        confirmBtn.className = 'btn btn-danger';
        confirmBtn.textContent = t('web_outbox_reject_confirm');
        const cancelBtn = document.createElement('button');
        cancelBtn.className = 'btn';
        cancelBtn.textContent = t('web_cancel');
        cancelBtn.addEventListener('click', () => form.remove());
        confirmBtn.addEventListener('click', () => decide(action, li, 'reject', textarea.value));
        form.append(textarea, confirmBtn, cancelBtn);
        li.append(form);
      });
      approveBtn.addEventListener('click', () => decide(action, li, 'approve'));
      actions.append(rejectBtn, approveBtn);
    } else {
      const st = document.createElement('span');
      st.className = 'badge';
      st.textContent = action.status + (action.decided_by ? ` · ${action.decided_by}` : '');
      actions.append(st);
    }

    li.append(head, prev, actions);
    return li;
  };

  const render = async () => {
    list.textContent = '';
    let actions = [];
    const statuses = status === 'pending' ? ['pending'] : ['executed', 'rejected', 'failed', 'expired'];
    for (const st of statuses) {
      try { actions = actions.concat(await api('GET', `/app/api/outbox?status=${st}`)); }
      catch { /* transient */ }
    }
    empty.hidden = actions.length > 0;
    for (const action of actions) list.append(row(action));
  };

  document.querySelectorAll('#outbox-tabs .tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('#outbox-tabs .tab').forEach((x) => x.classList.remove('tab-active'));
      tab.classList.add('tab-active');
      status = tab.dataset.status;
      render();
    });
  });

  render();
})();
