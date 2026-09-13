# SPDX-License-Identifier: AGPL-3.0-or-later
"""Built-in connector catalog — registers today's real adapters with the
registry (FEATURE-CONNECTOR-FRAMEWORK.md §6).

`mail`/`imap` and `calendar`/`caldav` wrap the pluggable adapters
tools/email.py (`_imap`) and tools/calendar.py (`_dav`) already had before
this framework existed. `server.py`'s `email_list`/`email_read`/
`email_search`/`email_create_draft` now resolve their mailbox via
`registry.get(..., "mail", user)` instead of calling `_make_mailbox`
directly (Byggordning step 4) — `email_send` is untouched since SMTP isn't
a registered capability. Cutting `calendar_*`'s call sites over to
`registry.get(..., "calendar", user)` (replacing `_resolve_calendar_dav`)
remains deferred: unlike mail's single shared-IMAP path, it has to
reproduce a 3-way per-user auth-priority chain (Google OAuth → iCal secret
→ FreeBusy → static CalDAV fallback) that doesn't fit the registry's
single-type/single-auth-mode `get()` shape without extending it — a
focused migration of its own, not a mechanical rewire like mail's.

`mail`/`microsoft` (Byggordning step 6 — the first *new* external adapter,
proof that adding one doesn't touch tools/email.py) wraps
`connectors/adapters/mail_microsoft.py`'s `GraphMailAdapter`, `auth=
"per_user"`: a project sets its `mailbox` resource's `type: microsoft`, and
whichever user is calling uses their own linked "microsoft" account
(`server.py`'s `_ensure_fresh_microsoft_mail_token` refreshes the access
token first if it's expiring — `registry.get()`'s per_user branch only
loads whatever's stored, it doesn't refresh).

`contacts`/`carddav`, `files`/`webdav`, `tasks`/`caldav`, `deck`/`nextcloud`
and `notes`/`nextcloud` are the Nextcloud adapters (FEATURE-NEXTCLOUD-
BACKEND.md §4-5-7, Byggordning steps 5-6) and are wired all the way to
live MCP tools (server.py's `contacts_search`/`contacts_get`, `nc_files_*`,
`nc_tasks_*`, `deck_sync`, `notes_sync`) since, unlike the local vault,
they aren't replacing an existing working code path — they're additional
capability. `tasks` is a resource key distinct from `calendar` even
though both default to type 'caldav' — a VTODO task list and an event
calendar are typically different CalDAV collections; likewise `deck` and
`notes` both default to type 'nextcloud' but are separate resource keys
(a project can link one, the other, both, or neither).

`mail`/`imap_user` wraps `connectors/adapters/mail_imap_user.py`'s
`build_mailbox`, `auth="per_user"`, `provider="imap"`: a user can link their
own personal IMAP mailbox (or several — `TokenStore.store` is unique on
`(memaix_user, provider, account_email)`, so multiple `imap`-provider
accounts per user are just multiple rows) via the non-OAuth web form
(`web/api/accounts.py`'s `api_accounts_link_imap`) instead of a project's
static `mailbox` resource. Distinct `type` from the existing shared
`type="imap"` spec so the two coexist: a project's `mailbox` resource can
still be `type: imap` (shared, unchanged), while `get_all()`'s per_user
sweep independently discovers each user's own linked `imap`-provider
accounts under `type="imap_user"` — see registry.get_all's per-user sweep,
which iterates registered specs by `type`, not by resource `type` key, so
both can be live for the same "mail" capability at once without collision.

`chat` has no adapter to wrap yet — it gets a registered spec once a real
backend exists (Nextcloud Talk).
"""

from __future__ import annotations

from .registry import ConnectorRegistry, ConnectorSpec


def _imap_factory(acl, project, user, resource_cfg, token):
    from ..tools.email import _make_mailbox

    return _make_mailbox(acl, project)


def _caldav_factory(acl, project, user, resource_cfg, token):
    from ..tools.calendar import _RealDavAdapter

    return _RealDavAdapter(acl, project)


def _public_ics_factory(acl, project, user, resource_cfg, token):
    """Public .ics/webcal link, no auth — memaix-src card 324dd801. Reuses
    calendar_find_free's existing iCal fetcher (SSRF-checked) rather than a
    second one; registered so calendar_sources.py's resolver can build one
    from a user-added link the same way it builds any other source."""
    from ..tools.calendar import _ICalAdapter

    return _ICalAdapter(resource_cfg["url"])


def _google_calendar_factory(acl, project, user, resource_cfg, token):
    from ..tools.calendar import _PerUserGoogleAdapter

    return _PerUserGoogleAdapter(token["access_token"])


def _ical_secret_factory(acl, project, user, resource_cfg, token):
    from ..tools.calendar import _ICalAdapter

    return _ICalAdapter(token["ical_url"])


def _microsoft_mail_factory(acl, project, user, resource_cfg, token):
    from .adapters.mail_microsoft import GraphMailAdapter

    return GraphMailAdapter(token["access_token"])


def _imap_user_factory(acl, project, user, resource_cfg, token):
    from .adapters.mail_imap_user import build_mailbox

    return build_mailbox(token)


def _carddav_factory(acl, project, user, resource_cfg, token):
    from .. import config
    from .adapters.contacts_carddav import CardDavContactsAdapter

    password = config.secret(resource_cfg.get("password_ref"))
    return CardDavContactsAdapter(resource_cfg["url"], resource_cfg.get("user", ""), password or "")


def _webdav_files_factory(acl, project, user, resource_cfg, token):
    from .. import config
    from .adapters.files_webdav import WebDavFilesAdapter

    password = config.secret(resource_cfg.get("password_ref"))
    return WebDavFilesAdapter(resource_cfg["url"], resource_cfg.get("user", ""), password or "")


def _tasks_caldav_factory(acl, project, user, resource_cfg, token):
    from .. import config
    from .adapters.tasks_caldav import CalDavTasksAdapter

    password = config.secret(resource_cfg.get("password_ref"))
    return CalDavTasksAdapter(resource_cfg["url"], resource_cfg.get("user", ""), password or "")


def _deck_factory(acl, project, user, resource_cfg, token):
    from .. import config
    from .adapters.deck_nextcloud import DeckAdapter

    password = config.secret(resource_cfg.get("password_ref"))
    return DeckAdapter(resource_cfg["url"], resource_cfg.get("user", ""), password or "")


def _notes_factory(acl, project, user, resource_cfg, token):
    from .. import config
    from .adapters.notes_nextcloud import NotesAdapter

    password = config.secret(resource_cfg.get("password_ref"))
    return NotesAdapter(resource_cfg["url"], resource_cfg.get("user", ""), password or "")


def register_defaults(registry: ConnectorRegistry) -> None:
    registry.register(
        ConnectorSpec(type="imap", capability="mail", auth="shared", factory=_imap_factory),
        ConnectorSpec(type="microsoft", capability="mail", auth="per_user", factory=_microsoft_mail_factory),
        ConnectorSpec(
            type="imap_user", capability="mail", auth="per_user", provider="imap", factory=_imap_user_factory,
        ),
        ConnectorSpec(type="caldav", capability="calendar", auth="shared", factory=_caldav_factory),
        ConnectorSpec(type="public_ics", capability="calendar", auth="shared", factory=_public_ics_factory),
        ConnectorSpec(type="google", capability="calendar", auth="per_user", factory=_google_calendar_factory),
        ConnectorSpec(type="ical_secret", capability="calendar", auth="per_user", provider="ical_secret", factory=_ical_secret_factory),
        ConnectorSpec(type="carddav", capability="contacts", auth="shared", factory=_carddav_factory),
        ConnectorSpec(type="webdav", capability="files", auth="shared", factory=_webdav_files_factory),
        ConnectorSpec(type="caldav", capability="tasks", auth="shared", factory=_tasks_caldav_factory),
        ConnectorSpec(type="nextcloud", capability="deck", auth="shared", factory=_deck_factory),
        ConnectorSpec(type="nextcloud", capability="notes", auth="shared", factory=_notes_factory),
    )
