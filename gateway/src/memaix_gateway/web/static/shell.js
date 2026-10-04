// SPDX-License-Identifier: AGPL-3.0-or-later
// Shell behaviour: sidebar toggle, project picker, user badge, admin link,
// outbox badge (FEATURE-WEB-UI-FOUNDATION.md §4.4). Loaded last on every page.

(() => {
  // --- Theme (init early, persisted) -------------------------------------
  // The board lives in a same-origin <iframe> with its own document, so the
  // attribute has to be set on both roots \u2014 setting it only here leaves the
  // board stuck on whatever theme it booted with.
  const _applyTheme = (dark) => {
    const value = dark ? 'dark' : 'light';
    const glyph = dark ? '\u263D' : '\u263C';
    document.documentElement.dataset.theme = value;
    const btn = document.getElementById('theme-btn');
    if (btn) btn.textContent = glyph;
    const frame = document.querySelector('iframe.board-frame');
    try {
      const frameDoc = frame?.contentDocument;
      if (frameDoc) frameDoc.documentElement.dataset.theme = value;
      const fbtn = frameDoc?.getElementById('theme-btn');
      if (fbtn) fbtn.textContent = glyph;
    } catch { /* iframe not ready yet \u2014 its own init reads localStorage */ }
  };
  // The iframe may not have parsed when the toggle fires, and it re-parses on
  // every board navigation, so re-apply on load rather than only on click.
  document.querySelector('iframe.board-frame')?.addEventListener('load', () => {
    _applyTheme(document.documentElement.dataset.theme === 'dark');
  });
  (() => {
    const saved = localStorage.getItem('mb_theme');
    const prefersDark = saved === 'dark';  // default is light
    _applyTheme(prefersDark);
  })();
  document.getElementById('theme-btn')?.addEventListener('click', () => {
    const isDark = document.documentElement.dataset.theme === 'dark';
    _applyTheme(!isDark);
    localStorage.setItem('mb_theme', isDark ? 'light' : 'dark');
  });

  // --- Sidebar collapse (persisted) -----------------------------------
  if (localStorage.getItem('memaix_sidebar_collapsed') === 'true') {
    document.body.dataset.collapsed = 'true';
  }
  document.getElementById('sidebar-toggle')?.addEventListener('click', () => {
    const collapsed = document.body.dataset.collapsed === 'true';
    document.body.dataset.collapsed = collapsed ? 'false' : 'true';
    localStorage.setItem('memaix_sidebar_collapsed', String(!collapsed));
  });

  // --- Active nav highlight -------------------------------------------
  const path = location.pathname.replace(/\/$/, '') || '/app';
  const page = path === '/app' ? 'home' : path.split('/')[2];
  document.querySelectorAll(`.nav-${page}`).forEach((el) => el.classList.add('active'));

  // --- Project selection ------------------------------------------------
  // URL ?project=X always wins and is written back to localStorage.
  const urlProject = new URLSearchParams(location.search).get('project');
  if (urlProject) localStorage.setItem('memaix_project', urlProject);
  const currentProject = () =>
    new URLSearchParams(location.search).get('project')
      ?? localStorage.getItem('memaix_project') ?? '';

  const pickers = [
    document.getElementById('project-picker'),
    document.getElementById('project-picker-mobile'),
  ].filter(Boolean);

  const onPick = (val) => {
    localStorage.setItem('memaix_project', val);
    location.href = location.pathname + '?project=' + encodeURIComponent(val);
  };
  pickers.forEach((p) => p.addEventListener('change', () => onPick(p.value)));

  // --- Populate from /app/api/me ---------------------------------------
  window.ME = api('GET', '/app/api/me').then((me) => {
    if (!me) return null;
    const project = currentProject() || me.projects[0] || '';
    pickers.forEach((p) => {
      p.textContent = '';
      me.projects.forEach((proj) => {
        const opt = document.createElement('option');
        opt.value = proj;
        opt.textContent = proj;
        if (proj === project) opt.selected = true;
        p.append(opt);
      });
    });
    const role = me.is_admin ? 'admin' : (me.role_map[project] ?? '');
    for (const id of ['user-badge', 'user-badge-mobile']) {
      const badge = document.getElementById(id);
      if (badge) badge.textContent = role ? `${me.user} · ${role}` : me.user;
    }
    document.querySelectorAll('.nav-admin').forEach((el) => { el.hidden = !me.is_admin; });
    return me;
  }).catch(() => null);

  // --- Maintenance banner (operator notice from /app/api/me) ------------
  const showMaintenance = async (fresh) => {
    const me = fresh ? await api('GET', '/app/api/me').catch(() => null) : await window.ME;
    const text = me?.maintenance?.message;
    let bar = document.getElementById('maintenance-banner');
    if (!text) { bar?.remove(); return; }
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'maintenance-banner';
      bar.className = 'maintenance-banner';
      bar.setAttribute('role', 'status');
      document.getElementById('content')?.before(bar);
    }
    bar.textContent = text;
  };
  showMaintenance(false);
  setInterval(() => { if (!document.hidden) showMaintenance(true); }, 60000);

  // --- Outbox badge (poll, pauses when tab hidden) ----------------------
  pollBadge('/app/api/me', document.querySelector('.outbox-badge'));

  // --- Logout ------------------------------------------------------------
  document.getElementById('logout-btn')?.addEventListener('click', async () => {
    try { await api('POST', '/board/auth/logout'); } catch { /* best effort */ }
    location.href = '/app';
  });
})();
