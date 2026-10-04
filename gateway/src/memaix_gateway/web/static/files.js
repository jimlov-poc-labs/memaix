// SPDX-License-Identifier: AGPL-3.0-or-later
// Files page: browse and download the project's Nextcloud files (read-only).
// The folder lives in the URL hash so the sidebar's project picker, which
// rewrites ?project=, does not fight with navigation.

const me = await window.ME;
if (me) {
  const project = new URLSearchParams(location.search).get('project')
        ?? localStorage.getItem('memaix_project') ?? me.projects[0] ?? '';
  const q = (path) => `project=${encodeURIComponent(project)}&path=${encodeURIComponent(path)}`;
  const $ = (id) => document.getElementById(id);

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

  const currentPath = () => {
    let raw = '';
    try { raw = decodeURIComponent(location.hash.slice(1)); } catch { /* malformed hash → root */ }
    return raw.startsWith('/') ? raw : '/';
  };
  const join = (dir, name) => `${dir === '/' ? '' : dir}/${name}`;

  const formatSize = (bytes) => {
    if (bytes === null || bytes === undefined) return '';
    if (bytes < 1024) return `${bytes} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let v = bytes / 1024;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
  };

  const showMessage = (text) => {
    const box = $('files-msg');
    box.textContent = text;
    box.hidden = !text;
  };

  const crumb = (label, target, last) => {
    if (last) return el('span', { class: 'crumb-current', text: label, 'aria-current': 'page' });
    return el('a', { href: `#${encodeURIComponent(target)}`, text: label });
  };

  const renderCrumbs = (path) => {
    const nav = $('files-crumbs');
    nav.textContent = '';
    const parts = path.split('/').filter(Boolean);
    let acc = '';
    nav.append(crumb(t('web_files_root'), '/', parts.length === 0));
    parts.forEach((part, i) => {
      acc += `/${part}`;
      nav.append(el('span', { class: 'crumb-sep', text: '/' }), crumb(part, acc, i === parts.length - 1));
    });
  };

  const entryLink = (path, entry) => {
    const target = join(path, entry.name);
    if (entry.type === 'dir') {
      return el('a', { href: `#${encodeURIComponent(target)}`, text: `\u{1F4C1} ${entry.name}` });
    }
    return el('a', {
      href: `/app/api/files/download?${q(target)}`,
      text: `\u{1F4C4} ${entry.name}`,
      download: entry.name,
    });
  };

  const entryRow = (path, entry) => el(
    'li', {},
    el('span', { class: 'list-text' }, entryLink(path, entry)),
    el('span', { class: 'muted', text: entry.type === 'dir' ? '' : formatSize(entry.size) }),
  );

  const failureText = (e) => {
    if (e.message === 'no_files') return t('web_files_none');
    if (e.status === 403) return t('web_files_err_forbidden');
    if (e.status === 404) return t('web_files_err_notfound');
    return t('web_files_err_load');
  };

  const load = async () => {
    const path = currentPath();
    renderCrumbs(path);
    showMessage('');
    const list = $('files-list');
    list.textContent = '';
    $('files-empty').hidden = true;
    try {
      const data = await api('GET', `/app/api/files?${q(path)}`);
      if (!data) return;
      data.entries.forEach((entry) => list.append(entryRow(data.path, entry)));
      $('files-empty').hidden = data.entries.length > 0;
    } catch (e) {
      showMessage(failureText(e));
    }
  };

  window.addEventListener('hashchange', load);
  await load();
}
