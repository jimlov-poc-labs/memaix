# SPDX-License-Identifier: AGPL-3.0-or-later
"""E2E: the web UI driven by real Chromium against a real gateway server.

Covers the acceptance criteria from FEATURE-WEB-UI-FOUNDATION/-MVP/-OUTBOX-AND-
ADMIN/-PHASE2 that unit tests cannot: rendered shell, client-side JS behaviour,
role-dependent UI, the full MFA enrollment flow, and mobile layout.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from playwright.sync_api import expect

from .conftest import ALICE_PASSWORD, login_as

# ---------------------------------------------------------------------------
# Auth + shell
# ---------------------------------------------------------------------------


def test_login_via_ui_endpoint_and_401_redirect(context, page, base_url):
    # Unauthenticated: the API answers 401 (the JS layer redirects to /login).
    resp = page.request.get(f"{base_url}/app/api/me")
    assert resp.status == 401

    # Real password login via the board auth endpoint issues the session
    # cookie. (It is Secure — correct in production behind TLS — so the plain-
    # http e2e transport can't retain it; authed navigation below uses an
    # injected cookie signed with the same secret.)
    login = page.request.post(
        f"{base_url}/board/auth/login",
        data={"username": "alice", "password": ALICE_PASSWORD},
    )
    assert login.ok and login.json()["user"] == "alice"
    assert "memaix_board=" in login.headers.get("set-cookie", "")

    login_as(context, "alice")
    me = page.request.get(f"{base_url}/app/api/me").json()
    assert me["user"] == "alice" and me["is_admin"] is True


def test_wrong_password_rejected(page, base_url):
    resp = page.request.post(
        f"{base_url}/board/auth/login",
        data={"username": "alice", "password": "wrong"},
    )
    assert resp.status == 401


def test_login_page_full_ui_flow(context, page):
    """The standalone /app/login page: wrong password shows the inline error,
    right password logs in. Chromium treats 127.0.0.1 as a trustworthy origin,
    so the Secure session cookie is accepted in a real page context and the
    redirect lands authenticated."""
    page.goto("/app/login?next=/app")
    expect(page.locator(".login-card h1")).to_have_text("Memaix")

    page.fill("#login-user", "alice")
    page.fill("#login-pass", "wrong")
    page.click("#login-btn")
    expect(page.locator("#login-error")).not_to_have_text("")

    # Input fields must be styled (regression: var(--background) didn't exist
    # → transparent inputs on the dark card).
    bg = page.eval_on_selector("#login-user", "el => getComputedStyle(el).backgroundColor")
    assert bg not in ("rgba(0, 0, 0, 0)", "transparent")

    page.fill("#login-pass", ALICE_PASSWORD)
    page.click("#login-btn")
    page.wait_for_url("**/app")
    # Authenticated shell rendered — the page JS fetched /app/api/me with the
    # freshly set cookie. (page.request's jar refuses Secure cookies over
    # plain http, so assert through the browser context, not the API helper.)
    expect(page.locator("#user-badge")).to_contain_text("alice")


def test_login_page_next_param_open_redirect_guard(page):
    """?next= must never leave the origin — an absolute/protocol-relative URL
    collapses to its path."""
    page.goto("/app/login?next=//evil.example.com/phish")
    page.fill("#login-user", "alice")
    page.fill("#login-pass", ALICE_PASSWORD)
    page.click("#login-btn")
    page.wait_for_url(lambda url: "evil.example.com" not in url)
    assert "evil.example.com" not in page.url


def test_shell_renders_light_with_sidebar(alice_page):
    page = alice_page
    page.goto("/app")
    expect(page.locator(".sidebar")).to_be_visible()
    expect(page.locator("#user-badge")).to_contain_text("alice")
    # Light is the default — deliberately not the OS prefers-color-scheme,
    # which used to hand a night-time visitor a dark shell after a light login.
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert bg == "rgb(255, 253, 247)"  # --bg: #fffdf7


def test_theme_toggle_switches_to_dark(alice_page):
    page = alice_page
    page.goto("/app")
    page.click("#theme-btn")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert bg == "rgb(28, 25, 23)"  # --bg: #1c1917


def test_sidebar_collapse_persists_via_localstorage(alice_page):
    page = alice_page
    page.goto("/app")
    page.click("#sidebar-toggle")
    expect(page.locator("body")).to_have_attribute("data-collapsed", "true")
    page.reload()
    expect(page.locator("body")).to_have_attribute("data-collapsed", "true")
    page.click("#sidebar-toggle")
    expect(page.locator("body")).to_have_attribute("data-collapsed", "false")


def test_project_picker_navigates_and_persists(alice_page):
    page = alice_page
    page.goto("/app")
    picker = page.locator("#project-picker")
    expect(picker).to_have_value("demo")
    page.select_option("#project-picker", "demo")
    page.wait_for_url("**/app?project=demo")
    assert page.evaluate("localStorage.getItem('memaix_project')") == "demo"


def test_admin_link_hidden_for_reader(bob_page):
    page = bob_page
    page.goto("/app")
    expect(page.locator("#user-badge")).to_contain_text("bob")
    expect(page.locator(".sidebar .nav-admin")).to_be_hidden()


def test_board_301_and_frame_loads(alice_page):
    page = alice_page
    resp = page.request.get("/board?project=demo", max_redirects=0)
    assert resp.status == 301
    assert resp.headers["location"] == "/app/board?project=demo"

    page.goto("/app/board?project=demo")
    frame_el = page.locator("#board-frame")
    expect(frame_el).to_be_visible()
    frame = page.frame(url="**/app/board/frame*")
    assert frame is not None
    # The embedded board document rendered (its own markup is inside the frame).
    frame.wait_for_selector("body")
    assert len(frame.content()) > 500


# ---------------------------------------------------------------------------
# Home dashboard
# ---------------------------------------------------------------------------


def test_home_dashboard_projects_and_role_chip(alice_page):
    page = alice_page
    page.goto("/app")
    card = page.locator(".project-card", has_text="demo")
    expect(card).to_be_visible()
    expect(card.locator(".role-chip")).to_have_text("admin")
    expect(card.locator("a", has_text="Open board")).to_be_visible()


# ---------------------------------------------------------------------------
# Memory explorer
# ---------------------------------------------------------------------------


def test_memory_tree_view_search_history(alice_page):
    page = alice_page
    page.goto("/app/memory?project=demo")

    tree = page.locator("#memory-tree")
    expect(tree.locator("a", has_text="welcome.md")).to_be_visible()
    expect(tree.locator("a", has_text="ideas/zebra.md")).to_be_visible()

    # Open a note → markdown rendered via mdView (h1 + strong, no raw #).
    tree.locator("a", has_text="welcome.md").click()
    view = page.locator("#memory-view")
    expect(view.locator("h1")).to_have_text("Welcome")
    expect(view).to_contain_text("Second revision.")

    # Search narrows the tree.
    page.fill("#memory-search", "zebra")
    expect(tree.locator("li")).to_have_count(1)
    expect(tree.locator("a", has_text="zebra")).to_be_visible()
    page.fill("#memory-search", "")
    expect(tree.locator("li")).to_have_count(2)

    # History drawer opens with commits and (owner) revert buttons.
    tree.locator("a", has_text="welcome.md").click()
    page.click("#memory-history-btn")
    drawer = page.locator("#history-drawer")
    expect(drawer).to_be_visible()
    expect(drawer.locator(".commit-row").first).to_be_visible()
    expect(drawer.locator(".revert-btn").first).to_be_visible()
    page.click("#close-drawer")
    expect(drawer).to_be_hidden()


def test_memory_reader_sees_no_revert_button(bob_page):
    page = bob_page
    page.goto("/app/memory?project=demo")
    tree = page.locator("#memory-tree")
    tree.locator("a", has_text="welcome.md").click()
    page.click("#memory-history-btn")
    expect(page.locator("#history-drawer .commit-row").first).to_be_visible()
    expect(page.locator("#history-drawer .revert-btn")).to_have_count(0)


# ---------------------------------------------------------------------------
# Outbox — approver scoping in the rendered UI
# ---------------------------------------------------------------------------


def _enqueue_email(preview: str) -> str:
    from memaix_gateway.outbox.queue import ActionQueue

    queue = ActionQueue.for_path(Path(os.environ["MEMAIX_OUTBOX_DB"]))
    return queue.enqueue(
        "alice", "demo", "email_send",
        {"to": "kund@example.com", "subject": "Offert", "body": "hemligt", "cc": None},
        preview,
    )


def test_outbox_owner_sees_reader_does_not(context, page):
    action_id = _enqueue_email("Offert till kund@example.com")

    login_as(context, "bob")
    page.goto("/app/outbox")
    expect(page.locator("#outbox-empty")).to_be_visible()
    expect(page.locator(".outbox-row")).to_have_count(0)
    # Direct API access is 403 too — not just hidden in the UI.
    assert page.request.get(f"/app/api/outbox/{action_id}").status == 403

    context.clear_cookies()
    login_as(context, "alice")
    page.goto("/app/outbox")
    row = page.locator(".outbox-row", has_text="email_send")
    expect(row).to_be_visible()
    expect(row).to_contain_text("Offert till kund@example.com")

    # Preview modal shows the full action detail.
    row.locator("button", has_text="Preview").click()
    expect(page.locator(".modal-box")).to_contain_text("email_send · demo")
    page.keyboard.press("Escape")
    expect(page.locator(".modal-box")).to_have_count(0)

    # Reject with a reason — never executes, row leaves the pending list.
    row.locator("button", has_text="Reject").click()
    page.fill(".reject-form textarea", "fel mottagare")
    page.click(".reject-form button.btn-danger")
    expect(page.locator("#outbox-empty")).to_be_visible()

    # Decided tab shows it as rejected.
    page.click("#outbox-tabs .tab >> text=Decided")
    expect(page.locator(".outbox-row", has_text="rejected")).to_be_visible()


def test_outbox_approve_executes_via_ui(context, page, monkeypatch):
    # The server runs in-process, so the dispatch table can be stubbed here.
    calls: list[dict] = []
    monkeypatch.setattr(
        "memaix_gateway.outbox.execute._default_dispatch",
        lambda: {"email_send": lambda acl, u, p, **kw: calls.append(kw) or {"status": "sent"}},
    )
    _enqueue_email("Approve-me")

    login_as(context, "alice")
    page.goto("/app/outbox")
    row = page.locator(".outbox-row", has_text="Approve-me")
    row.locator("button", has_text="Approve").click()
    expect(page.locator(".toast")).to_be_visible()
    # Executed exactly once with the confirmation flag the outbox injects.
    for _ in range(50):
        if calls:
            break
        time.sleep(0.1)
    assert len(calls) == 1 and calls[0]["_confirmed"] is True


# ---------------------------------------------------------------------------
# Admin: gate, tables, full MFA enrollment, kill-switch
# ---------------------------------------------------------------------------


def test_admin_denied_for_reader(bob_page):
    page = bob_page
    page.goto("/app/admin")
    expect(page.locator("#admin-denied")).to_be_visible()
    expect(page.locator("#admin-tabs")).to_be_hidden()


def test_admin_tables_and_audit_filter(alice_page):
    page = alice_page
    page.goto("/app/admin")
    users_table = page.locator("#admin-users table")
    expect(users_table).to_contain_text("alice")
    expect(users_table).to_contain_text("bob")

    page.click("#admin-tabs .tab >> text=Projects")
    expect(page.locator("#admin-projects table")).to_contain_text("demo")

    page.click("#admin-tabs .tab >> text=Audit")
    page.fill("#audit-user", "alice")
    page.click("#audit-filter button[type=submit]")
    expect(page.locator("#admin-audit")).to_be_visible()


def _inject_mfa_cookie(context, user: str) -> None:
    """The MFA session cookie is Secure (correct behind production TLS), so
    plain-http e2e transport can't retain the server-set one — inject a cookie
    signed with the same secret instead, like login_as does for the board."""
    from memaix_gateway.web.api.mfa import _make_signed_cookie

    context.add_cookies(
        [{
            "name": "memaix_mfa",
            "value": _make_signed_cookie(user, str(int(time.time()))),
            "domain": "127.0.0.1",
            "path": "/",
        }]
    )


def test_mfa_enrollment_and_kill_switch_flow(context, page):
    """The full Fas D loop in a real browser: enroll TOTP through the modal
    with a real authenticator code, verify, then disable bob via the
    kill-switch and see his access die immediately."""
    from memaix_gateway.web import totp as totp_mod

    login_as(context, "alice")
    # wait_until="commit" avoids "interrupted by navigation" if the JS auth
    # redirect fires before the load event (intermittent race with cookie flush).
    # The subsequent .click() and expect() calls provide the real stability gate.
    page.goto("/app/admin", wait_until="commit")
    expect(page).to_have_url(re.compile(r"/app/admin"))

    # 1) Enroll: open the setup modal, read the secret, answer with a real code.
    page.locator("button", has_text="Set up MFA").click()
    secret_line = page.locator(".modal-box p.mono")
    expect(secret_line).to_be_visible()
    secret = secret_line.inner_text().split(":", 1)[1].strip()
    code = totp_mod.totp_at(secret, time.time())
    page.locator(".modal-box input").fill(code)
    with page.expect_navigation():  # same-URL reload after enrollment
        page.locator(".modal-box button", has_text="Confirm").click()
    assert page.request.get("/app/api/admin/mfa").json()["enrolled"] is True

    # 2) Verify endpoint accepts a fresh real code (and rejects a wrong one).
    wrong = page.request.post("/app/api/admin/mfa/verify", data={"code": "000000"})
    assert wrong.status == 401
    ok = page.request.post(
        "/app/api/admin/mfa/verify", data={"code": totp_mod.totp_at(secret, time.time())}
    )
    assert ok.ok and "memaix_mfa=" in ok.headers.get("set-cookie", "")

    # Secure cookie won't survive plain http — inject the equivalent session.
    _inject_mfa_cookie(context, "alice")
    page.goto("/app/admin", wait_until="commit")
    expect(page).to_have_url(re.compile(r"/app/admin"))
    assert page.request.get("/app/api/admin/mfa").json()["verified"] is True

    # 3) Kill-switch: disable bob from the UI…
    with page.expect_navigation():
        page.locator("button", has_text="Disable: bob").click()
    users = page.request.get("/app/api/admin/users").json()
    assert next(u for u in users if u["id"] == "bob")["disabled"] is True

    # …and bob is locked out of project data immediately (live acl reload).
    # Navigate away BEFORE clearing cookies so pollBadge's interval is already
    # stopped when the session disappears — clearing first leaves a live interval
    # that can fire a 401→redirect and race the next goto.
    page.goto("about:blank", wait_until="commit")
    context.clear_cookies()
    login_as(context, "bob")
    resp = page.request.get("/app/api/memory/notes?project=demo")
    assert resp.status == 403

    # 4) Re-enable via the UI so later tests see a clean state.
    page.goto("about:blank", wait_until="commit")
    context.clear_cookies()
    login_as(context, "alice")
    _inject_mfa_cookie(context, "alice")
    page.goto("/app/admin", wait_until="commit")
    expect(page).to_have_url(re.compile(r"/app/admin"))
    with page.expect_navigation():
        page.locator("button", has_text="Enable: bob").click()
    users = page.request.get("/app/api/admin/users").json()
    assert next(u for u in users if u["id"] == "bob")["disabled"] is False


# ---------------------------------------------------------------------------
# Search, settings, timeline
# ---------------------------------------------------------------------------


def test_search_page_renders_and_queries(alice_page):
    page = alice_page
    page.goto("/app/search")
    page.fill("#search-q", "zebra")
    page.click("#search-form button[type=submit]")
    # Index is empty in e2e — the point is the roundtrip renders without errors.
    expect(page.locator("#search-empty")).to_be_visible()


def test_settings_brief_save(alice_page):
    page = alice_page
    page.goto("/app/settings?project=demo")
    page.check("#brief-enabled")
    page.fill("#brief-time", "07:30")
    page.locator("#brief-form button[type=submit]").click()
    expect(page.locator("#brief-status")).to_contain_text("Next brief")
    # Persisted server-side.
    brief = page.request.get("/app/api/brief").json()
    assert brief["configured"] is True and brief["prefs"]["brief_time"] == "07:30"


def test_settings_calendar_ical_ssrf_rejected_in_ui(alice_page):
    page = alice_page
    page.goto("/app/settings?project=demo")
    page.select_option("#calendar-mode-select", "ical_secret")
    page.fill("#calendar-ical-url", "http://169.254.169.254/latest/meta-data/")
    page.locator("#calendar-form button[type=submit]").click()
    expect(page.locator(".toast-error")).to_be_visible()  # rejected, not saved


def test_settings_calendar_offers_native_mode(alice_page):
    page = alice_page
    page.goto("/app/settings?project=demo")
    page.select_option("#calendar-mode-select", "native")
    expect(page.locator("#calendar-native-hint")).to_be_visible()


def test_booking_page_host_controls_schedule(alice_page):
    page = alice_page
    page.goto("/app/booking?project=demo")
    expect(page.locator("#hours-grids .week-grid")).to_have_count(1)

    page.check("#booking-enabled")
    page.locator("#onoff-form button[type=submit]").click()
    expect(page.locator("#onoff-status")).to_have_text("Saved.")

    page.get_by_role("button", name="Add time").first.click()
    page.locator("#hours-form button[type=submit]").click()
    expect(page.locator("#hours-status")).to_have_text("Saved.")

    page.check("#booking-alt-weeks")
    expect(page.locator("#hours-grids .week-grid")).to_have_count(2)

    page.fill("#booking-cap-input", "3")
    page.locator("#cap-form button[type=submit]").click()
    expect(page.locator("#cap-status")).to_have_text("Saved.")

    page.fill("#type-name", "Kort samtal")
    page.fill("#type-minutes", "30")
    page.locator("#type-form button[type=submit]").click()
    expect(page.locator("#types-list li")).to_have_count(1)

    state = page.request.get("/app/api/booking?project=demo").json()
    assert state["enabled"] is True and state["max_per_day"] == 3
    assert state["meeting_types"][0]["duration_min"] == 30
    assert "mon" in state["week"]


def test_booking_page_date_exception_add_and_remove(alice_page):
    page = alice_page
    page.goto("/app/booking?project=demo")
    expect(page.locator("#hours-grids .week-grid")).to_have_count(1)

    page.fill("#date-day", "2031-05-06")
    page.select_option("#date-kind", "closed")
    page.locator("#dates-form button[type=submit]").click()
    expect(page.locator("#dates-list li")).to_have_count(1)
    state = page.request.get("/app/api/booking?project=demo").json()
    assert state["dates"] == {"2031-05-06": []}

    page.get_by_role("button", name="Remove 2031-05-06").click()
    expect(page.locator("#dates-list li")).to_have_count(0)
    state = page.request.get("/app/api/booking?project=demo").json()
    assert state["dates"] == {}


# ---------------------------------------------------------------------------
# Mobile layout
# ---------------------------------------------------------------------------


def test_mobile_layout_tab_bar(browser, base_url):
    ctx = browser.new_context(base_url=base_url, viewport={"width": 375, "height": 667})
    try:
        login_as(ctx, "alice")
        page = ctx.new_page()
        page.goto("/app")
        expect(page.locator(".tab-bar")).to_be_visible()
        expect(page.locator(".sidebar")).to_be_hidden()
        expect(page.locator("#project-picker-mobile")).to_be_visible()
    finally:
        ctx.close()
