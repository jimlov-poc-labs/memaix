# SPDX-License-Identifier: AGPL-3.0-or-later
"""Characterization of web-UI JS that Sonar flagged for cognitive complexity.

Pins today's DOM output of app.js mdView, the home.js dashboard and the
settings.js accounts list, so the S3776 refactor can be shown not to change
behaviour. API responses are mocked with page.route; markup is compared as
rendered HTML.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import expect

# ---------------------------------------------------------------------------
# app.js: mdView
# ---------------------------------------------------------------------------

MD_CASES = [
    ("empty", "", ""),
    ("none", None, ""),
    ("paragraph_join", "one\n  two  \nthree", "<p>one two three</p>"),
    ("paragraphs", "a\n\nb", "<p>a</p><p>b</p>"),
    ("headings", "# H1\n## H2\n### H3\n#### not", "<h1>H1</h1><h2>H2</h2><h3>H3</h3><p>#### not</p>"),
    ("heading_inline", "## a **b**", "<h2>a <strong>b</strong></h2>"),
    ("inline", "x **b** *i* `c` y", "<p>x <strong>b</strong> <em>i</em> <code>c</code> y</p>"),
    ("inline_escaped", "`<b>x</b>`", "<p><code>&lt;b&gt;x&lt;/b&gt;</code></p>"),
    ("ul", "- a\n* b", "<ul><li>a</li><li>b</li></ul>"),
    ("ol", "1. a\n2. b", "<ol><li>a</li><li>b</li></ol>"),
    ("list_first_item_decides", "- a\n1. b", "<ul><li>a</li><li>b</li></ul>"),
    ("list_flushed_by_blank", "- a\n\n- b", "<ul><li>a</li></ul><ul><li>b</li></ul>"),
    ("para_before_list", "p\n- a", "<p>p</p><ul><li>a</li></ul>"),
    # Quirk pinned on purpose: a paragraph line does not flush an open list, so
    # the paragraph is emitted first and the list at the end.
    ("list_then_para", "- a\ntext", "<p>text</p><ul><li>a</li></ul>"),
    ("hr", "a\n---\nb", "<p>a</p><hr><p>b</p>"),
    ("hr_flushes_list", "- a\n----", "<ul><li>a</li></ul><hr>"),
    ("code_block", "```\nx **y**\n  z\n```", "<pre><code>x **y**\n  z</code></pre>"),
    ("code_flushes_para_and_list", "p\n```\nc\n```\n- a\n```\nd\n```",
     "<p>p</p><pre><code>c</code></pre><ul><li>a</li></ul><pre><code>d</code></pre>"),
    ("code_swallows_headings", "```\n# no\n- no\n```", "<pre><code># no\n- no</code></pre>"),
    ("unterminated_code_dropped", "a\n```\nb", "<p>a</p>"),
    ("star_item_not_italic", "* a *b*", "<ul><li>a <em>b</em></li></ul>"),
]


@pytest.mark.parametrize(("name", "md", "expected"), MD_CASES, ids=[c[0] for c in MD_CASES])
def test_mdview_output(alice_page, name, md, expected):
    alice_page.goto("/app")
    html = alice_page.evaluate(
        "md => { const el = document.createElement('div'); el.textContent = 'old';"
        " mdView(el, md); return el.innerHTML; }",
        md,
    )
    assert html == expected, name


# ---------------------------------------------------------------------------
# home.js: to-do card, project grid, activity feed
# ---------------------------------------------------------------------------


def _mock_me(page, **overrides):
    real = page.request.get("/app/api/me").json()
    me = {**real, **overrides}
    page.route("**/app/api/me", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(me)))


def _mock_json(page, pattern, payload):
    page.route(pattern, lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(payload)))


def test_home_todo_items_and_empty(alice_page):
    page = alice_page
    _mock_me(page, pending_outbox=3, needs_relink=["google", "imap"], onboarding_missing=True)
    page.goto("/app")
    items = page.locator("#todo-list li")
    expect(items).to_have_count(4)
    hrefs = page.locator("#todo-list li a").evaluate_all("els => els.map(e => e.getAttribute('href'))")
    assert hrefs == ["/app/outbox", "/app/settings#accounts", "/app/settings#accounts", "/app/settings"]
    texts = page.locator("#todo-list li span").all_inner_texts()
    assert texts[1].startswith("google: ") and texts[2].startswith("imap: ")
    assert texts[0].startswith("3 ")
    expect(page.locator("#todo-empty")).to_be_hidden()


def test_home_todo_empty_state(alice_page):
    page = alice_page
    _mock_me(page, pending_outbox=0, needs_relink=[], onboarding_missing=False)
    page.goto("/app")
    expect(page.locator("#todo-list li")).to_have_count(0)
    expect(page.locator("#todo-empty")).to_be_visible()


def test_home_project_cards_roles_and_counts(alice_page):
    page = alice_page
    _mock_me(page, is_admin=False, projects=["p1", "p2", "p3"], role_map={"p1": "owner", "p2": "reader"})
    page.route("**/board/api/board?project=p1", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"total_cards": 7})))
    page.route("**/board/api/board?project=p2", lambda r: r.fulfill(status=500, body="boom"))
    page.route("**/board/api/board?project=p3", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"total_cards": 0})))
    page.goto("/app")
    cards = page.locator(".project-card")
    expect(cards).to_have_count(3)
    expect(cards.nth(0).locator(".muted")).to_have_text("7 cards")
    expect(cards.nth(2).locator(".muted")).to_have_text("0 cards")
    expect(cards.nth(1).locator(".muted")).to_have_text("")
    chips = page.locator(".project-card .role-chip")
    assert chips.evaluate_all("els => els.map(e => e.className + '|' + e.textContent)") == [
        "role-chip role-owner|owner", "role-chip role-reader|reader", "role-chip role-|",
    ]
    assert cards.nth(0).evaluate("e => e.outerHTML.replace(/<div class=\"muted\">.*?<\\/div>/, '')") == (
        '<div class="card project-card"><h3>p1</h3><span class="role-chip role-owner">owner</span>'
        '<a href="/app/board?project=p1">Open board →</a></div>')


def test_home_project_card_admin_chip(alice_page):
    page = alice_page
    _mock_me(page, is_admin=True, projects=["p1"], role_map={"p1": "reader"})
    page.goto("/app")
    expect(page.locator(".project-card .role-chip")).to_have_text("admin")


def test_home_activity_feed_rows(alice_page):
    page = alice_page
    events = [{"tool": f"t{i}", "project": "demo", "ts": "2020-01-01T00:00:00Z", "ok": i % 2 == 0,
               "detail": "why" if i == 22 else ""} for i in range(25)]
    _mock_json(page, "**/board/api/activity", {"events": events})
    page.goto("/app")
    rows = page.locator("#activity-feed .act-row")
    expect(rows).to_have_count(20)
    # last 20 events, newest first: t24 .. t5
    assert rows.nth(0).locator("span").nth(0).get_attribute("class") == "act-ok"
    assert rows.nth(1).locator("span").nth(0).get_attribute("class") == "act-fail"
    assert rows.nth(1).locator("span").nth(0).inner_text() == "✗"
    assert rows.nth(0).locator("span").nth(0).inner_text() == "✓"
    assert rows.nth(0).locator("span").nth(1).inner_text().startswith("t24 · demo · ")
    assert rows.nth(19).locator("span").nth(1).inner_text().startswith("t5 · demo · ")
    detail = page.locator("#activity-feed .act-detail")
    expect(detail).to_have_count(1)
    expect(detail).to_have_text("why")
    # the detail row directly follows the t22 row
    assert page.locator("#activity-feed > *").evaluate_all(
        "els => els.map(e => e.className)")[2:5] == ["act-row", "mono muted act-detail", "act-row"]
    expect(page.locator("#activity-feed .empty-state")).to_have_count(0)


def test_home_activity_feed_empty_and_error(alice_page):
    page = alice_page
    _mock_json(page, "**/board/api/activity", {"events": []})
    page.goto("/app")
    expect(page.locator("#activity-feed .empty-state")).to_have_text("No recent activity.")
    expect(page.locator("#activity-feed .act-row")).to_have_count(0)


def test_home_activity_feed_error_swallowed(alice_page):
    page = alice_page
    page.route("**/board/api/activity", lambda r: r.fulfill(status=500, body="x"))
    page.goto("/app")
    expect(page.locator(".project-card").first).to_be_visible()
    expect(page.locator("#activity-feed > *")).to_have_count(0)


# ---------------------------------------------------------------------------
# settings.js: linked accounts list
# ---------------------------------------------------------------------------

ACCOUNTS = [
    {"provider": "google", "account": "a@x.se", "status": "active", "readonly": False,
     "project": None, "capabilities": ["email"], "scopes_by_capability": {"email": ["demo"]}},
    {"provider": "imap", "account": "b@x.se", "status": "needs_relink", "readonly": False,
     "project": "demo", "capabilities": [], "scopes_by_capability": {}},
    {"provider": "microsoft", "account": "c@x.se", "status": "other", "readonly": False,
     "project": "", "capabilities": [], "scopes_by_capability": {}},
    {"provider": "google", "account": "d@x.se", "status": "needs_relink", "readonly": True,
     "project": "demo", "capabilities": ["email"], "scopes_by_capability": {}},
]


def test_settings_accounts_list_rendering(alice_page):
    page = alice_page
    _mock_json(page, "**/app/api/accounts", ACCOUNTS)
    page.goto("/app/settings?project=demo")
    items = page.locator("#accounts-list > li.account-item")
    expect(items).to_have_count(4)
    expect(page.locator("#accounts-empty")).to_be_hidden()
    heads = items.locator(".account-head").evaluate_all(
        "els => els.map(e => [...e.children].map(c => c.tagName + ':' + c.className + ':' + c.textContent))")
    assert heads[0][:2] == ["SPAN::\U0001F7E2", "SPAN:account-label:google · a@x.se"]
    assert heads[0][2].startswith("BUTTON:btn btn-danger:")
    assert len(heads[0]) == 3
    assert heads[1][0] == "SPAN::\U0001F7E1"
    assert heads[1][1] == "SPAN:account-label:IMAP · b@x.se (demo)"
    assert heads[1][2].startswith("SPAN:muted:") and heads[1][3].startswith("BUTTON:")
    assert len(heads[1]) == 4
    assert heads[2][0] == "SPAN::\U0001F7E1"
    assert heads[2][1] == "SPAN:account-label:microsoft · c@x.se"
    assert heads[3][0] == "SPAN::\U0001F535"
    assert heads[3][1] == "SPAN:account-label:google · d@x.se (demo)"
    assert heads[3][2].startswith("SPAN:muted:") and len(heads[3]) == 3  # readonly: no unlink
    # scope grid only for non-readonly accounts that have capabilities
    assert items.evaluate_all("els => els.map(e => e.querySelectorAll('.scope-grid').length)") == [1, 0, 0, 0]


def test_settings_accounts_empty(alice_page):
    page = alice_page
    _mock_json(page, "**/app/api/accounts", [])
    page.goto("/app/settings?project=demo")
    expect(page.locator("#accounts-list > li")).to_have_count(0)
    expect(page.locator("#accounts-empty")).to_be_visible()


def test_settings_accounts_fetch_error_keeps_empty(alice_page):
    page = alice_page
    page.route("**/app/api/accounts", lambda r: r.fulfill(status=500, body="x"))
    page.goto("/app/settings?project=demo")
    expect(page.locator("#accounts-empty")).to_be_visible()


def test_settings_unlink_calls_delete_and_toasts(alice_page):
    page = alice_page
    calls = []

    def handler(route):
        calls.append((route.request.method, route.request.url))
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/app/api/accounts/google?account=*", handler)
    _mock_json(page, "**/app/api/accounts", ACCOUNTS[:1])
    page.goto("/app/settings?project=demo")
    page.locator(".account-head .btn-danger").click()
    expect(page.locator(".toast-success")).to_be_visible()
    assert calls and calls[0][0] == "DELETE" and calls[0][1].endswith("/app/api/accounts/google?account=a%40x.se")


def test_settings_scope_grid_checkboxes(alice_page):
    page = alice_page
    accs = [{"provider": "google", "account": "a@x.se", "status": "active", "readonly": False,
             "project": None, "capabilities": ["email", "chat"],
             "scopes_by_capability": {"email": ["*"], "chat": []}}]
    _mock_json(page, "**/app/api/accounts", accs)
    page.goto("/app/settings?project=demo")
    rows = page.locator(".scope-row")
    expect(rows).to_have_count(2)
    # email: wildcard ticked, project boxes ticked + disabled
    state = rows.nth(0).locator("input").evaluate_all("els => els.map(e => [e.checked, e.disabled])")
    assert state[0] == [True, False] and all(s == [True, True] for s in state[1:])
    state = rows.nth(1).locator("input").evaluate_all("els => els.map(e => [e.checked, e.disabled])")
    assert all(s == [False, False] for s in state)
    # unknown capability label falls back to bare name; known one is translated
    heads = page.locator(".scope-capability").all_inner_texts()
    assert heads[1] == "chat"
    expect(page.locator(".scope-unshared")).to_have_count(0)
