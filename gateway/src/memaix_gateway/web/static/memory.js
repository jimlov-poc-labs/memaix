// SPDX-License-Identifier: AGPL-3.0-or-later
// Memory explorer: note tree, viewer (mdView), search, history drawer with
// owner-gated revert (FEATURE-WEB-UI-MVP.md §1.4–1.5).

async function memoryPage() {
  const me = await window.ME;
  if (!me) return;
  const project = new URLSearchParams(location.search).get('project')
        ?? localStorage.getItem('memaix_project') ?? (me.projects ?? [])[0] ?? '';
  const role = me.is_admin ? 'admin' : ((me.role_map ?? {})[project] ?? '');
  const canRevert = role === 'owner' || role === 'admin';

  const tree = document.getElementById('memory-tree');
  const view = document.getElementById('memory-view');
  const filename = document.getElementById('memory-filename');
  const historyBtn = document.getElementById('memory-history-btn');
  const drawer = document.getElementById('history-drawer');
  let currentFile = '';

  const openNote = async (path) => {
    try {
      const note = await api('GET', `/app/api/memory/note?project=${encodeURIComponent(project)}&path=${encodeURIComponent(path)}`);
      currentFile = path;
      filename.textContent = path;
      historyBtn.hidden = false;
      mdView(view, note.content);
    } catch (e) { toast(e.message, 'error'); }
  };

  const renderTree = (notes) => {
    tree.textContent = '';
    document.getElementById('memory-empty').hidden = notes.length > 0;
    for (const note of notes) {
      const li = document.createElement('li');
      const a = document.createElement('a');
      a.href = '#';
      a.className = 'mono';
      a.textContent = note.path;
      a.addEventListener('click', (e) => { e.preventDefault(); openNote(note.path); });
      li.append(a);
      tree.append(li);
    }
  };

  try {
    renderTree(await api('GET', `/app/api/memory/notes?project=${encodeURIComponent(project)}`));
  } catch (e) { toast(e.message, 'error'); }

  // --- Search (debounced; empty query restores the full tree) ------------
  let searchTimer = null;
  document.getElementById('memory-search').addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      const q = e.target.value.trim();
      try {
        if (!q) {
          renderTree(await api('GET', `/app/api/memory/notes?project=${encodeURIComponent(project)}`));
          return;
        }
        const hits = await api('GET', `/app/api/memory/search?project=${encodeURIComponent(project)}&q=${encodeURIComponent(q)}`);
        renderTree(hits);
      } catch { /* transient search errors are non-fatal */ }
    }, 250);
  });

  // --- History drawer -----------------------------------------------------
  const closeDrawer = () => { drawer.hidden = true; drawer.removeAttribute('open'); };
  document.getElementById('close-drawer').addEventListener('click', closeDrawer);

  historyBtn.addEventListener('click', async () => {
    if (!currentFile) return;
    document.getElementById('history-filename').textContent = currentFile;
    const list = document.getElementById('history-list');
    list.textContent = '';
    try {
      const entries = await api('GET', `/app/api/memory/history?project=${encodeURIComponent(project)}&path=${encodeURIComponent(currentFile)}`);
      for (const entry of entries) {
        const li = document.createElement('li');
        li.className = 'commit-row';
        const sha = document.createElement('code');
        sha.className = 'mono sha';
        sha.textContent = String(entry.hash ?? '').slice(0, 7);
        const msg = document.createElement('span');
        msg.className = 'commit-msg';
        msg.textContent = entry.message ?? '';
        const when = document.createElement('time');
        when.className = 'muted';
        when.textContent = relTime(entry.date ?? '');
        li.append(sha, msg, when);
        if (canRevert) {
          const btn = document.createElement('button');
          btn.className = 'btn revert-btn';
          btn.textContent = t('web_memory_revert');
          btn.addEventListener('click', async () => {
            const body = document.createElement('div');
            const p = document.createElement('p');
            p.textContent = `${t('web_memory_revert_confirm')} ${String(entry.hash ?? '').slice(0, 7)}?`;
            const confirmBtn = document.createElement('button');
            confirmBtn.className = 'btn btn-danger';
            confirmBtn.textContent = t('web_memory_revert');
            body.append(p, confirmBtn);
            const m = modal(body);
            confirmBtn.addEventListener('click', async () => {
              try {
                await api('POST', '/app/api/memory/revert', { project, sha: entry.hash });
                toast(t('web_memory_reverted'), 'success');
                m.close(); closeDrawer();
                openNote(currentFile);
              } catch (err) { toast(err.message, 'error'); }
            });
          });
          li.append(btn);
        }
        list.append(li);
      }
      drawer.hidden = false;
      drawer.setAttribute('open', '');
    } catch (e) { toast(e.message, 'error'); }
  });
}

await memoryPage();
