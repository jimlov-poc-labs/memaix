// SPDX-License-Identifier: AGPL-3.0-or-later
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { flush, jsonResponse, loadApp, mockFetch, mountPage, runPage } from './helpers.mjs';

beforeAll(loadApp);

const pending = (over = {}) => ({
  id: 'a/1', tool: 'email_send', project: 'alpha', status: 'pending',
  preview: 'To: bob', args: { to: 'bob' }, created_at: new Date().toISOString(), ...over,
});
const rows = () => [...document.querySelectorAll('#outbox-list li')];
const btn = (root, text) => [...root.querySelectorAll('button')].find((b) => b.textContent === text);
const toasts = () => [...document.querySelectorAll('#toast-container .toast')].map((x) => [x.className, x.textContent]);

async function open(routes, { me = { projects: ['alpha'] } } = {}) {
  mountPage('outbox');
  document.body.insertAdjacentHTML('beforeend', '<div id="toast-container"></div>');
  window.ME = Promise.resolve(me);
  const f = mockFetch(routes);
  await runPage('outbox');
  await flush();
  return f;
}

describe('outbox page', () => {
  it('does nothing when not logged in', async () => {
    mountPage('outbox');
    window.ME = Promise.resolve(null);
    const f = mockFetch({});
    await runPage('outbox');
    expect(f.calls).toHaveLength(0);
  });

  it('lists pending actions with approve/reject/preview buttons', async () => {
    const f = await open({ 'GET /app/api/outbox': [pending(), pending({ id: 'b', tool: 'task_add' })] });
    expect(f.calls.map((c) => c.url)).toEqual(['/app/api/outbox?status=pending']);
    expect(rows()).toHaveLength(2);
    expect(rows()[0].querySelector('strong').textContent).toBe('email_send · alpha');
    expect(rows()[0].querySelector('.outbox-preview').textContent).toBe('To: bob');
    expect(rows()[0].style.borderLeft).toContain('var(--warning)');
    expect(rows()[0].querySelectorAll('button')).toHaveLength(3);
    expect(document.getElementById('outbox-empty').hidden).toBe(true);
  });

  it('shows the empty state when nothing is pending, and survives a failing list call', async () => {
    await open({ 'GET /app/api/outbox': [] });
    expect(document.getElementById('outbox-empty').hidden).toBe(false);
    await open({ 'GET /app/api/outbox': () => jsonResponse({ error: 'x' }, 500) });
    expect(rows()).toHaveLength(0);
    expect(document.getElementById('outbox-empty').hidden).toBe(false);
  });

  it('renders user data as text, never as elements', async () => {
    await open({ 'GET /app/api/outbox': [pending({ tool: '<img src=x>', project: '<b>p</b>', preview: '<script>alert(1)</script>' })] });
    const li = rows()[0];
    expect(li.querySelector('img, b, script')).toBeNull();
    expect(li.querySelector('strong').textContent).toBe('<img src=x> · <b>p</b>');
    expect(li.querySelector('.outbox-preview').textContent).toBe('<script>alert(1)</script>');
  });

  it('falls back to ts, empty preview and a neutral border for odd actions', async () => {
    await open({ 'GET /app/api/outbox': [{ id: 'z', tool: 't', project: 'p', status: 'weird', ts: 'garbage' }] });
    const li = rows()[0];
    expect(li.querySelector('.outbox-preview').textContent).toBe('');
    expect(li.querySelector('.muted').textContent).toBe('garbage');
    expect(li.style.borderLeft).toContain('var(--border)');
  });

  it('decided tab fetches all four final statuses and shows badges, no approve button', async () => {
    const byStatus = { executed: 'ann', rejected: null, failed: 'bo', expired: undefined };
    const f = await open({
      'GET /app/api/outbox': (url) => {
        const st = new URL(url, 'http://x').searchParams.get('status');
        return jsonResponse(st in byStatus ? [pending({ id: st, status: st, decided_by: byStatus[st] })] : []);
      },
    });
    f.calls.length = 0;
    document.querySelector('[data-status="executed"]').click();
    await flush();
    expect(f.calls.map((c) => c.url.split('=')[1])).toEqual(['executed', 'rejected', 'failed', 'expired']);
    expect(document.querySelector('[data-status="executed"]').classList.contains('tab-active')).toBe(true);
    expect(document.querySelector('[data-status="pending"]').classList.contains('tab-active')).toBe(false);
    expect(rows()).toHaveLength(4);
    expect(rows().map((r) => r.querySelector('.badge').textContent)).toEqual(['executed · ann', 'rejected', 'failed · bo', 'expired']);
    expect(rows().every((r) => !btn(r, 'web_outbox_approve'))).toBe(true);
    expect(rows()[0].style.borderLeft).toContain('var(--success)');
    expect(rows()[2].style.borderLeft).toContain('var(--danger)');
    // and back to pending
    f.calls.length = 0;
    document.querySelector('[data-status="pending"]').click();
    await flush();
    expect(f.calls).toHaveLength(1);
  });

  it('preview modal shows preview text, or JSON args when there is none', async () => {
    await open({ 'GET /app/api/outbox': [pending(), pending({ id: 'b', preview: '', args: { k: 1 } }), pending({ id: 'c', preview: '', args: undefined })] });
    btn(rows()[0], 'web_outbox_preview').click();
    let box = document.querySelector('.modal-box');
    expect(box.querySelector('h3').textContent).toBe('email_send · alpha');
    expect(box.querySelector('pre').textContent).toBe('To: bob');
    expect(box.querySelector('p')).toBeNull();
    box.parentElement.remove();
    btn(rows()[1], 'web_outbox_preview').click();
    expect(document.querySelector('.modal-box pre').textContent).toBe(JSON.stringify({ k: 1 }, null, 2));
    document.querySelector('.modal-backdrop').remove();
    btn(rows()[2], 'web_outbox_preview').click();
    expect(document.querySelector('.modal-box pre').textContent).toBe('{}');
  });

  it('preview of a decided action adds status, decider and reason', async () => {
    await open({
      'GET /app/api/outbox': (url) => jsonResponse(url.endsWith('status=rejected')
        ? [pending({ status: 'rejected', decided_by: 'ann', result: { reason: 'nope' } })]
        : url.endsWith('status=expired') ? [pending({ id: 'e', status: 'expired' })] : []),
    });
    document.querySelector('[data-status="executed"]').click();
    await flush();
    btn(rows()[0], 'web_outbox_preview').click();
    expect(document.querySelector('.modal-box p').textContent).toBe('rejected · ann · nope');
    document.querySelector('.modal-backdrop').remove();
    btn(rows()[1], 'web_outbox_preview').click();
    expect(document.querySelector('.modal-box p').textContent).toBe('expired');
  });

  it('approve posts without a body, encodes the id, toasts and reloads', async () => {
    let list = [pending()];
    const f = await open({
      'GET /app/api/outbox': () => jsonResponse(list),
      'POST /app/api/outbox/a%2F1/approve': () => { list = []; return jsonResponse({ ok: true, result: {} }); },
    });
    btn(rows()[0], 'web_outbox_approve').click();
    expect(rows()[0].style.opacity).toBe('0.4');
    await flush();
    const post = f.calls.find((c) => c.key.startsWith('POST'));
    expect(post.key).toBe('POST /app/api/outbox/a%2F1/approve');
    expect(post.opts.body).toBeUndefined();
    expect(toasts()).toEqual([['toast toast-success', 'web_outbox_approved']]);
    expect(rows()).toHaveLength(0);
    expect(document.querySelector('.modal-box')).toBeNull();
  });

  it('approve with ok:false reports a failure toast', async () => {
    await open({
      'GET /app/api/outbox': [pending()],
      'POST /app/api/outbox/a%2F1/approve': { ok: false, result: { error: 'smtp' } },
    });
    btn(rows()[0], 'web_outbox_approve').click();
    await flush();
    expect(toasts()).toEqual([['toast toast-error', 'web_outbox_failed']]);
    expect(document.querySelector('.modal-box')).toBeNull();
  });

  it.each(['invite_url', 'reset_url'])('shows a copyable link modal when approve returns %s', async (field) => {
    await open({
      'GET /app/api/outbox': [pending()],
      'POST /app/api/outbox/a%2F1/approve': { ok: true, result: { [field]: 'https://x/y' } },
    });
    btn(rows()[0], 'web_outbox_approve').click();
    await flush();
    const ta = document.querySelector('.modal-box textarea');
    expect(ta.readOnly).toBe(true);
    expect(ta.value).toBe('web_outbox_link_message\nhttps://x/y');
    const writeText = vi.fn().mockResolvedValue();
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    btn(document.querySelector('.modal-box'), 'web_outbox_link_copy').click();
    await flush();
    expect(writeText).toHaveBeenCalledWith(ta.value);
    expect(toasts().at(-1)).toEqual(['toast toast-success', 'web_outbox_link_copied']);
  });

  it('copy falls back to selecting the text when the clipboard is unavailable', async () => {
    await open({
      'GET /app/api/outbox': [pending()],
      'POST /app/api/outbox/a%2F1/approve': { ok: true, result: { invite_url: 'https://x' } },
    });
    btn(rows()[0], 'web_outbox_approve').click();
    await flush();
    const ta = document.querySelector('.modal-box textarea');
    const sel = vi.spyOn(ta, 'select');
    vi.stubGlobal('navigator', { clipboard: { writeText: vi.fn().mockRejectedValue(new Error('denied')) } });
    btn(document.querySelector('.modal-box'), 'web_outbox_link_copy').click();
    await flush();
    expect(sel).toHaveBeenCalled();
    expect(toasts().map((x) => x[1])).not.toContain('web_outbox_link_copied');
  });

  it('reject opens one inline form; cancel removes it', async () => {
    await open({ 'GET /app/api/outbox': [pending()] });
    const li = rows()[0];
    btn(li, 'web_outbox_reject').click();
    btn(li, 'web_outbox_reject').click();
    expect(li.querySelectorAll('.reject-form')).toHaveLength(1);
    expect(li.querySelector('.reject-form textarea').placeholder).toBe('web_outbox_reject_reason');
    btn(li.querySelector('.reject-form'), 'web_cancel').click();
    expect(li.querySelector('.reject-form')).toBeNull();
  });

  it('confirming a reject posts the reason and reloads', async () => {
    let list = [pending()];
    const f = await open({
      'GET /app/api/outbox': () => jsonResponse(list),
      'POST /app/api/outbox/a%2F1/reject': () => { list = []; return jsonResponse({ ok: true, status: 'rejected' }); },
    });
    btn(rows()[0], 'web_outbox_reject').click();
    rows()[0].querySelector('textarea').value = 'not now';
    btn(rows()[0].querySelector('.reject-form'), 'web_outbox_reject_confirm').click();
    await flush();
    const post = f.calls.find((c) => c.key.startsWith('POST'));
    expect(JSON.parse(post.opts.body)).toEqual({ reason: 'not now' });
    expect(toasts()).toEqual([['toast toast-success', 'web_outbox_rejected']]);
    expect(rows()).toHaveLength(0);
  });

  it('a 409 shows who decided first and reloads the list', async () => {
    let list = [pending()];
    const f = await open({
      'GET /app/api/outbox': () => jsonResponse(list),
      'POST /app/api/outbox/a%2F1/approve': () => { list = []; return jsonResponse({ conflict: true, decided_by: 'carol', error: 'conflict' }, 409); },
    });
    btn(rows()[0], 'web_outbox_approve').click();
    await flush();
    expect(toasts()).toEqual([['toast toast-warning', 'web_outbox_conflict carol']]);
    expect(f.calls.filter((c) => c.key.startsWith('GET'))).toHaveLength(2);
    expect(rows()).toHaveLength(0);
  });

  it('a 409 without decided_by still renders a message', async () => {
    await open({
      'GET /app/api/outbox': [pending()],
      'POST /app/api/outbox/a%2F1/approve': () => jsonResponse({ conflict: true }, 409),
    });
    btn(rows()[0], 'web_outbox_approve').click();
    await flush();
    expect(toasts()[0][1]).toBe('web_outbox_conflict ');
  });

  it('other errors restore the row and show the error, without reloading', async () => {
    const f = await open({
      'GET /app/api/outbox': [pending()],
      'POST /app/api/outbox/a%2F1/approve': () => jsonResponse({ error: 'forbidden' }, 403),
    });
    const li = rows()[0];
    btn(li, 'web_outbox_approve').click();
    await flush();
    expect(li.style.opacity).toBe('1');
    expect(li.isConnected).toBe(true);
    expect(toasts()).toEqual([['toast toast-error', 'forbidden']]);
    expect(f.calls.filter((c) => c.key.startsWith('GET'))).toHaveLength(1);
  });
});
