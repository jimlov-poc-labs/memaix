// SPDX-License-Identifier: AGPL-3.0-or-later
// Unified search page (FEATURE-WEB-UI-PHASE2.md §sök): queries the index and
// renders cited results {project, source_type, ref, title, snippet, score}.

async function searchPage() {
  const me = await window.ME;
  if (!me) return;
  const list = document.getElementById('search-results');
  const empty = document.getElementById('search-empty');
  const meta = document.getElementById('search-meta');

  const run = async (q) => {
    list.textContent = '';
    empty.hidden = true;
    meta.textContent = '';
    if (!q) return;
    try {
      const data = await api('GET', `/app/api/search?q=${encodeURIComponent(q)}&limit=20`);
      const results = data.results ?? [];
      empty.hidden = results.length > 0;
      meta.textContent = results.length
        ? `${results.length} ${t('web_search_hits')} · ${(data.projects_searched ?? []).join(', ')}`
        : '';
      for (const r of results) {
        const li = document.createElement('li');
        li.className = 'search-hit';
        const head = document.createElement('div');
        const title = document.createElement('strong');
        title.textContent = r.title || r.ref;
        const src = document.createElement('span');
        src.className = 'muted';
        src.textContent = ` — ${r.project} · ${r.source_type} · ${r.ref}`;
        head.append(title, src);
        if (r.status === 'hypotes') {
          // Minnestrappan: en obekräftad notering ska aldrig se ut som fakta.
          const badge = document.createElement('span');
          badge.className = 'muted';
          badge.textContent = ` ⚠ ${t('web_search_hypothesis')}`;
          head.append(badge);
        }
        const snip = document.createElement('div');
        snip.className = 'muted';
        snip.textContent = r.snippet ?? '';
        li.append(head, snip);
        list.append(li);
      }
    } catch (e) { toast(e.message, 'error'); }
  };

  document.getElementById('search-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const q = document.getElementById('search-q').value.trim();
    history.replaceState(null, '', '?q=' + encodeURIComponent(q));
    run(q);
  });

  const initial = new URLSearchParams(location.search).get('q');
  if (initial) {
    document.getElementById('search-q').value = initial;
    run(initial);
  }
}

await searchPage();
