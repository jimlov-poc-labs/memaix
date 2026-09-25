# SPDX-License-Identifier: AGPL-3.0-or-later
"""MCP server entrypoint — Fas 3: HTTP transport + Hydra token auth.

User identity:
  HTTP mode  — Bearer JWT verified by HydraTokenVerifier, subject mapped via acl.yaml.
  stdio mode — MEMAIX_USER env var (backward-compatible).
Rate limiting: 60 req/min per user, 120 req/min per project.
Audit: every tool call is logged to the audit DB (MEMAIX_AUDIT_DB or MEMAIX_DATA_DIR/memaix-audit.db).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import config
from .acl import AccessDenied, Acl
from .capabilities.catalog import register_defaults as _register_default_capabilities

# Agentloopens identitetskontext (FEATURE-LLM-ENGINE Fas 2) — definieras i
# llm/identity.py (ett kontrakt, en definition; llm-lagret slipper MCP-
# beroendet). Sätts enbart av ToolBridge.call() från en verifierad
# webbsession, per-task-isolerad, alltid återställd i finally.
from .llm.identity import AGENT_USER as _AGENT_USER
from .paths import data_dir as _data_dir
from .safety.audit import AuditLog
from .safety.rate_limit import rate_limiter as _rate_limiter
from .tools import account as t_account
from .tools import backlog as t_backlog
from .tools import calendar as t_cal
from .tools import contacts as t_contacts
from .tools import email as t_email
from .tools import files as t_files
from .tools import memory as t_memory
from .tools import nc_docgen as t_nc_docgen
from .tools import nc_files as t_nc_files
from .tools import nc_tasks as t_nc_tasks
from .tools import onboarding as t_onboarding
from .tools import pm as t_pm
from .tools import pm_engine as t_pm_engine
from .tools import whoami as t_whoami
from .tools.calendar import (
    CalendarAuthRequired,
    _FreeBusyAdapter,
    _ICalAdapter,
    _MultiCalendarAdapter,
    _PerUserGoogleAdapter,
    _ServiceAccountGoogleCalendarAdapter,
)

logger = logging.getLogger(__name__)


# RFC 7591 §3.2.1: valfri klientmetadata *utelämnas* när den saknas. Hydra
# skickar istället tomma strängar för de valfria URI-fälten och null för
# osatta listor. En klient som validerar svaret — Claude Code gör det —
# avvisar då hela registreringen:
#   client_uri: ""    -> "Invalid URL" / "URL must be parseable"
#   contacts:   null  -> "expected array, received null"
# Det gick obemärkt förbi tills någon försökte ansluta med en strikt klient.
_DCR_URI_FALT = ("client_uri", "policy_uri", "tos_uri", "logo_uri", "jwks_uri")
_GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"  # nosec B105 -- endpoint URL, not a secret
_DEFAULT_PUBLIC_URL = "http://localhost:8080"
_DEFAULT_ISSUER = "https://mcp.example.com"
_CALENDAR_SETUP_HINT = "Kör calendar_setup för att välja åtkomstläge"


def _stada_dcr_svar(data: object) -> object:
    """Ta bort osatta valfria fält ur Hydras registreringssvar."""
    if not isinstance(data, dict):
        return data
    ut = {k: v for k, v in data.items() if v is not None}
    for falt in _DCR_URI_FALT:
        if ut.get(falt) == "":
            ut.pop(falt)
    return ut


_acl: Acl | None = None
_audit: AuditLog | None = None
_token_store: "TokenStore | None" = None  # type: ignore[name-defined]
_outbox_queue: "ActionQueue | None" = None  # type: ignore[name-defined]
_timeline_store: "ActionsStore | None" = None  # type: ignore[name-defined]
_search_store: "EmbeddingStore | None" = None  # type: ignore[name-defined]
_search_embedder = None
_search_embedder_loaded = False
_notify_store: "NotifyStore | None" = None  # type: ignore[name-defined]
_rules_store: "RulesStore | None" = None  # type: ignore[name-defined]
_nudge_state: "NudgeState | None" = None  # type: ignore[name-defined]
_pm_store: "PMStore | None" = None  # type: ignore[name-defined]
_notes_link_store: "NotesLinkStore | None" = None  # type: ignore[name-defined]
_idempotency_store: "IdempotencyStore | None" = None  # type: ignore[name-defined]


def _get_acl() -> Acl:
    global _acl
    if _acl is None:
        cfg = config.load()
        _acl = Acl.from_config(cfg["acl"])
    return _acl


def reload_acl() -> Acl:
    """Drop the cached Acl and rebuild it from disk.

    `_get_acl()` caches the Acl in a module global, so a rewrite of acl.yaml
    (e.g. the admin UI's AclWriter, or a manual edit) is otherwise invisible to
    the running gateway until restart. Any code path that mutates acl.yaml MUST
    call this afterwards so the change takes effect immediately."""
    global _acl
    _acl = None
    return _get_acl()


def _get_token_store():
    global _token_store
    if _token_store is None:
        from cryptography.fernet import Fernet

        from .backends.token_store import TokenStore
        key_ref = os.environ.get("TOKEN_MASTER_KEY")
        if not key_ref:
            # In HTTP (server) mode an ephemeral key silently discards every
            # linked account on restart and differs per worker — refuse to start
            # unless the operator explicitly opts in. stdio/dev keeps the warn.
            import sys
            http_mode = (
                os.environ.get("MEMAIX_TRANSPORT") == "http" or "--http" in sys.argv
            )
            allow_ephemeral = os.environ.get("MEMAIX_ALLOW_EPHEMERAL_KEY", "").lower() in (
                "1", "true", "yes",
            )
            if http_mode and not allow_ephemeral:
                raise RuntimeError(
                    "TOKEN_MASTER_KEY is required in HTTP mode. Generate one with "
                    "`python -c \"from cryptography.fernet import Fernet; "
                    "print(Fernet.generate_key().decode())\"` and set it in .env, "
                    "or set MEMAIX_ALLOW_EPHEMERAL_KEY=1 to accept per-restart key loss."
                )
            import warnings
            warnings.warn(
                "TOKEN_MASTER_KEY not set — using ephemeral key (tokens lost on restart)",
                RuntimeWarning,
                stacklevel=2,
            )
            key: bytes = Fernet.generate_key()
        else:
            key = key_ref.encode() if isinstance(key_ref, str) else key_ref
        db_path = Path(os.environ.get("MEMAIX_TOKEN_DB", str(_data_dir() / "memaix-tokens.db")))
        _token_store = TokenStore.for_path(db_path, key)
        # Accounts linked before project scoping existed keep working: each
        # gets a wildcard grant for the capabilities its provider already
        # served, once. Anything linked afterwards — and any capability a
        # provider gains later — starts scoped to nothing until the user
        # says otherwise. Safe to call from every worker: the schema_meta
        # marker makes it a no-op after the first.
        from .connectors.catalog import LEGACY_PER_USER_CAPABILITIES

        _token_store.backfill_scopes_once(LEGACY_PER_USER_CAPABILITIES)
    return _token_store


def _get_audit() -> AuditLog:
    global _audit
    if _audit is None:
        db_path = Path(os.environ.get("MEMAIX_AUDIT_DB", str(_data_dir() / "memaix-audit.db")))
        _audit = AuditLog.for_path(db_path)
    return _audit


def _get_outbox():
    global _outbox_queue
    if _outbox_queue is None:
        from .outbox.queue import ActionQueue
        db_path = Path(os.environ.get("MEMAIX_OUTBOX_DB", str(_data_dir() / "memaix-outbox.db")))
        _outbox_queue = ActionQueue.for_path(db_path)
    return _outbox_queue


def _get_timeline():
    global _timeline_store
    if _timeline_store is None:
        from .timeline.store import ActionsStore
        db_path = Path(os.environ.get("MEMAIX_ACTIONS_DB", str(_data_dir() / "memaix-actions.db")))
        _timeline_store = ActionsStore.for_path(db_path)
    return _timeline_store


def _http_mode() -> bool:
    """True when the gateway is served over HTTP (vs stdio) — MEMAIX_TRANSPORT
    or --http. In HTTP mode identity MUST come from a verified OAuth token."""
    return os.environ.get("MEMAIX_TRANSPORT") == "http" or "--http" in sys.argv


def _user() -> str:
    # Agentloopen (server-side chatt): process-intern identitet, satt av
    # verktygsbryggan från en autentiserad webbsession. Kollas FÖRE OAuth —
    # i en agentkörning finns ingen MCP-token att läsa.
    _agent_user = _AGENT_USER.get()
    if _agent_user:
        return _agent_user

    # HTTP mode: resolve identity from the OAuth token injected by MCP SDK middleware.
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
        token = get_access_token()
        if token and token.subject:
            uid = _get_acl().user_by_subject(token.subject)
            if uid:
                return uid
            raise RuntimeError(f"OAuth subject not mapped in acl.yaml: {token.subject!r}")
    except ImportError:
        pass

    # Fail closed in HTTP mode: if we're served over HTTP but no verified OAuth
    # subject resolved (missing token, or auth.issuer not configured), deny —
    # never silently fall through to the MEMAIX_USER env identity, which would
    # let an unauthenticated HTTP client act as that single user.
    if _http_mode():
        raise RuntimeError("no authenticated OAuth subject — refusing to identify caller in HTTP mode")

    # stdio fallback (Fas 1-style, backward-compatible).
    uid = os.environ.get("MEMAIX_USER", "").strip()
    if not uid:
        raise RuntimeError("MEMAIX_USER is not set — cannot identify caller")
    return uid


def _rl(user: str, project: str) -> None:
    """Rate-limit check; raises RuntimeError if exceeded."""
    if not _rate_limiter.check_user(user):
        raise RuntimeError("rate_limited: user quota exceeded")
    if not _rate_limiter.check_project(project):
        raise RuntimeError("rate_limited: project quota exceeded")


def _audited(user: str, project: str, tool: str, fn, *args, idempotency_key: str | None = None, **kwargs):
    """Call fn(*args, **kwargs), log result to audit, re-raise on error.

    Every tool call funnels through here (whether via _tool_call or the
    older direct-_audited pattern used by calendar_*), which makes this the
    single choke point for the undo/timeline recording hook below — args is
    always (acl, user, project, *tail) by convention (see FEATURE-UNDO-TIMELINE.md).

    idempotency_key (docs/OPEN-GAPS.md #13): if given and a prior call with
    the same (user, tool, key) already succeeded, its cached result is
    returned without re-running fn — so a retried email_send/calendar_create/
    nc_tasks_add can't repeat the external side effect. A replay skips
    fn() entirely (no re-check of ACL/rate-limit beyond what already ran
    above this call, no duplicate timeline/search-index recording) since
    those already happened for the original call; only the audit trail gets
    a new "idempotent replay" entry so retries stay visible.
    """
    if idempotency_key:
        cached = _get_idempotency().get(user, tool, idempotency_key)
        if cached is not None:
            _get_audit().log(user, project, tool, True, "idempotent replay")
            return cached
    try:
        result = fn(*args, **kwargs)
        _get_audit().log(user, project, tool, True)
        _maybe_record_timeline(user, project, tool, args[3:], kwargs, result)
        _maybe_index_for_search(user, project, tool, args[3:], kwargs, result)
        _maybe_publish_internal_event(user, project, tool, args[3:], kwargs, result)
        if idempotency_key and isinstance(result, dict):
            _get_idempotency().record(user, tool, idempotency_key, result)
        return result
    except Exception as exc:
        _get_audit().log(user, project, tool, False, str(exc))
        raise


def _maybe_record_timeline(user: str, project: str, tool: str, tail: tuple, kwargs: dict, result) -> None:
    """Best-effort undo-log recording — must never break the tool call itself."""
    from .timeline.inverse import TOOL_HANDLERS

    handler = TOOL_HANDLERS.get(tool)
    if handler is None:
        return
    if isinstance(result, dict) and result.get("pending"):
        return  # queued for outbox approval — nothing actually happened yet
    try:
        summary_fn, inverse_fn = handler
        summary = summary_fn(tail, kwargs, result)
        inverse = inverse_fn(tail, kwargs, result)
        _get_timeline().record(user, project, tool, summary, inverse)
    except Exception:
        logger.warning("timeline recording failed for tool %r", tool, exc_info=True)


def _index_memory_write(acl, user, project, tail, kwargs, result):
    note, content = tail[0], tail[1]
    return ("memory", note, note, content)


def _index_memory_append(acl, user, project, tail, kwargs, result):
    # Re-read the full note so the index holds current content, not just the
    # newly appended fragment (replace_chunks would otherwise drop the rest).
    note = tail[0]
    try:
        full_content = t_memory.memory_read(acl, user, project, note)["content"]
    except Exception:
        full_content = tail[1]
    return ("memory", note, note, full_content)


def _index_backlog_add(acl, user, project, tail, kwargs, result):
    if not isinstance(result, dict) or not result.get("id"):
        return None
    title, description = tail[0], tail[1]
    return ("backlog", result["id"], title, f"{title}\n{description or ''}")


def _index_files_write(acl, user, project, tail, kwargs, result):
    path, content = tail[0], tail[1]
    return ("file", path, path, content)


def _index_nc_files_write(acl, user, project, tail, kwargs, result):
    # Distinct source_type from local files ("nc_file" vs "file") so a
    # search_all citation tells you unambiguously which backend it lives in.
    path, content = tail[0], tail[1]
    return ("nc_file", path, path, content)


# Search-index coverage is intentionally scoped to the writes named in
# FEATURE-SEMANTIC-SEARCH.md's acceptance criteria (memory_write/append,
# backlog_add, files_write) — field-level backlog edits (score/comment/
# set_status) don't change the searchable text meaningfully enough to
# justify a full item re-read on every call; reindex via search_reindex.
# nc_files_write (FEATURE-NEXTCLOUD-BACKEND.md §4) follows the same rule as
# files_write once it exists.
SEARCH_INDEX_HANDLERS = {
    "memory_write": _index_memory_write,
    "memory_append": _index_memory_append,
    "backlog_add": _index_backlog_add,
    "files_write": _index_files_write,
    "nc_files_write": _index_nc_files_write,
}


def _get_search_store():
    global _search_store
    if _search_store is None:
        from .search.store import EmbeddingStore
        db_path = Path(os.environ.get("MEMAIX_INDEX_DB", str(_data_dir() / "memaix-index.db")))
        _search_store = EmbeddingStore.for_path(db_path)
    return _search_store


def _get_search_embedder():
    global _search_embedder, _search_embedder_loaded
    if not _search_embedder_loaded:
        from .search.embedder import make_embedder
        search_cfg = config.load().get("memaix", {}).get("search", {})
        _search_embedder = make_embedder(search_cfg)
        _search_embedder_loaded = True
    return _search_embedder


def _get_notify():
    global _notify_store
    if _notify_store is None:
        from .notify.store import NotifyStore
        db_path = Path(os.environ.get("MEMAIX_NOTIFY_DB", str(_data_dir() / "memaix-notify.db")))
        _notify_store = NotifyStore.for_path(db_path)
    return _notify_store


def _extract_email_body(payload: dict, max_chars: int = 800) -> str:
    import base64
    parts = payload.get("parts") or [payload]
    for part in parts:
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                text = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
                return text[:max_chars]
        sub = _extract_email_body(part, max_chars) if part.get("parts") else ""
        if sub:
            return sub
    return ""


def _fetch_gmail_from_account(token_data: dict, inbox_label: str, provider_cfg: dict, days: int, limit: int) -> list[dict]:
    import googleapiclient.discovery
    from google.oauth2.credentials import Credentials

    creds = Credentials(
        token=token_data.get("access_token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=_GOOGLE_TOKEN_URI,  # nosec B106
        client_id=provider_cfg.get("client_id", ""),
        client_secret=config.secret(provider_cfg.get("client_secret_ref", "")) or "",
    )
    svc = googleapiclient.discovery.build("gmail", "v1", credentials=creds, cache_discovery=False)
    resp = svc.users().messages().list(userId="me", q=f"newer_than:{days}d in:inbox", maxResults=limit).execute()
    result = []
    for ref in resp.get("messages", []):
        msg = svc.users().messages().get(userId="me", id=ref["id"], format="full").execute()
        payload = msg.get("payload", {})
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
        result.append({
            "subject": headers.get("Subject", "(inget ämne)"),
            "from": headers.get("From", ""),
            "seen": "UNREAD" not in msg.get("labelIds", []),
            "snippet": _extract_email_body(payload) or msg.get("snippet", ""),
            "inbox": inbox_label,
        })
    return result


def _gmail_or_imap_list(acl, u, project, folder, limit, *, days=3):
    email_res = acl.resource(project, "email")
    if not (isinstance(email_res, dict) and email_res.get("type") == "google"):
        return t_email.email_list(acl, u, project, folder, limit)
    store = _get_token_store()
    google_accounts = [a for a in store.list_accounts(u) if a["provider"] == "google"]
    if not google_accounts:
        return []
    provider_cfg = config.load().get("memaix", {}).get("oauth_providers", {}).get("google", {})
    all_msgs: list[dict] = []
    for acc in google_accounts:
        token_data = store.load_one(u, "google", acc["account"])
        if not token_data:
            continue
        try:
            all_msgs.extend(_fetch_gmail_from_account(token_data, acc["account"], provider_cfg, days, limit))
        except Exception:
            logger.warning("Gmail fetch failed for account %s", acc["account"])
    return all_msgs


def _mail_triage(mails: list[dict]) -> list[dict]:
    if not mails:
        return mails
    try:
        from .llm import LLMClient
        client = LLMClient.from_config(config.load())
        lines = [
            (
                f"{i}. Från: {m.get('from', '')}\n"
                f"   Ämne: {m.get('subject', '(inget ämne)')}\n"
                f"   Förhandsgranskning: {m.get('snippet', '')}"
            )
            for i, m in enumerate(mails, 1)
        ]
        prompt = (
            "Utvärdera dessa e-postmeddelanden och svara med en JSON-array.\n"
            'För varje mail: {"priority": "Hög"|"Medel"|"Låg", "summary": "1-2 meningar på svenska"}\n'
            "\n"
            "Prioritet Hög: säkerhetslarm, kräver omedelbar åtgärd, ekonomiskt kritiskt\n"
            "Prioritet Medel: kräver svar eller åtgärd men inte brådskande\n"
            "Prioritet Låg: information, reklam, nyhetsbrev, automatiska bekräftelser\n"
            "\n"
            f"Mail:\n{chr(10).join(lines)}\n\n"
            "Svara ENBART med JSON-array utan markdown-kodblock, inga andra ord."
        )
        import json as _json
        reply = client.complete([{"role": "user", "content": prompt}], max_tokens=1500)
        verdicts = _json.loads((reply.get("content") or "").strip())
        for m, v in zip(mails, verdicts):
            if isinstance(v, dict):
                m["priority"] = v.get("priority", "")
                m["summary"] = v.get("summary", "")
        return mails
    except Exception:
        return mails


def _brief_tools_for_user() -> dict:
    """Concrete tool functions the BriefBuilder uses to gather content —
    built here (not in notify/brief.py) so that module stays free of any
    server.py/tools.* import at module scope."""

    def calendar_events(acl, u, project, day_start, day_end):
        dav = _resolve_calendar_dav(project, u)
        if dav is None:
            return []
        return t_cal.calendar_list(
            acl, u, project, day_start.isoformat(), day_end.isoformat(), _dav=dav
        )

    return {
        "calendar_events": calendar_events,
        "email_list": _gmail_or_imap_list,
        "mail_triage": _mail_triage,
        "backlog_list": t_backlog.backlog_list,
        "pm_raid_list": t_pm.pm_raid_list,
    }


def _get_rules():
    global _rules_store
    if _rules_store is None:
        from .rules.store import RulesStore
        db_path = Path(os.environ.get("MEMAIX_RULES_DB", str(_data_dir() / "memaix-rules.db")))
        _rules_store = RulesStore.for_path(db_path)
    return _rules_store


def _get_pm():
    global _pm_store
    if _pm_store is None:
        from .pm.store import PMStore
        db_path = Path(os.environ.get("MEMAIX_PM_DB", str(_data_dir() / "memaix-pm.db")))
        _pm_store = PMStore.for_path(db_path)
    return _pm_store


def _get_idempotency():
    global _idempotency_store
    if _idempotency_store is None:
        from .safety.idempotency import IdempotencyStore
        db_path = Path(os.environ.get("MEMAIX_IDEMPOTENCY_DB", str(_data_dir() / "memaix-idempotency.db")))
        _idempotency_store = IdempotencyStore.for_path(db_path)
    return _idempotency_store


def _get_nudge_state():
    global _nudge_state
    if _nudge_state is None:
        from .capabilities.nudges import NudgeState
        db_path = Path(os.environ.get("MEMAIX_NUDGE_DB", str(_data_dir() / "memaix-nudges.db")))
        _nudge_state = NudgeState.for_path(db_path)
    return _nudge_state


def _get_accounts(user: str) -> list:
    try:
        return _get_token_store().list_accounts(user)
    except Exception:
        return []


def _translator_for_config(cfg: dict):
    from .i18n import get_translator
    locale = cfg.get("memaix", {}).get("server", {}).get("locale", "en")
    return get_translator(locale)


_LOCK_REASON_KEYS = {
    "no_role": "cap.lock.no_role",
    "no_mailbox": "cap.lock.no_mailbox",
    "no_calendar": "cap.lock.no_calendar",
    "no_vault": "cap.lock.no_vault",
    "no_contacts": "cap.lock.no_contacts",
    "no_files": "cap.lock.no_files",
    "no_tasks": "cap.lock.no_tasks",
    "no_deck": "cap.lock.no_deck",
    "no_notes": "cap.lock.no_notes",
    "link_google": "cap.lock.link_google",
    "link_microsoft": "cap.lock.link_microsoft",
}


def _lock_reason_text(t, reason: str) -> str:
    return t(_LOCK_REASON_KEYS.get(reason, reason))


def _capability_summary(cap, t) -> dict:
    return {"key": cap.key, "area": cap.area, "title": t(cap.title_key), "summary": t(cap.summary_key)}


def _capability_detail(cap, t) -> dict:
    detail = _capability_summary(cap, t)
    detail["tools"] = list(cap.tools)
    detail["examples"] = t(cap.example_prompts_key)
    return detail


def _capabilities_data(area: str | None = None) -> dict:
    """ACL/account-filtered capability data for the `capabilities` tool,
    `memaix_help` prompt, and `memaix://capabilities` resource — the single
    place these three surfaces read from (docs/FEATURE-DISCOVERABILITY.md §6)."""
    from .capabilities.registry import available_for, group_by_area

    user = _user()
    acl = _get_acl()
    cfg = config.load()
    t = _translator_for_config(cfg)
    available, locked = available_for(acl, user, _get_accounts(user), cfg)

    if area is None:
        grouped = group_by_area(available)
        areas = [
            {"area": a, "capabilities": [_capability_summary(c, t) for c in caps]}
            for a, caps in grouped.items()
        ]
        locked_out = [
            {
                "area": entry["capability"].area,
                "title": t(entry["capability"].title_key),
                "reason": entry["reason"],
                "hint": _lock_reason_text(t, entry["reason"]),
            }
            for entry in locked
        ]
        return {"areas": areas, "locked": locked_out}

    return {
        "area": area,
        "capabilities": [_capability_detail(c, t) for c in available if c.area == area],
        "locked": [
            {
                "title": t(entry["capability"].title_key),
                "reason": entry["reason"],
                "hint": _lock_reason_text(t, entry["reason"]),
            }
            for entry in locked
            if entry["capability"].area == area
        ],
    }


def _index_internal_event_backlog_set_status(tail, kwargs, result):
    # tail = (id, status, expected_version) — see backlog_set_status's _tool_call site.
    if not isinstance(result, dict) or result.get("conflict"):
        return None  # nothing actually transitioned
    item_id, new_status, expected_version = tail[0], tail[1], tail[2]
    return {
        "event_key": f"{item_id}:{expected_version}:{new_status}",
        "payload": {"event": "backlog.status", "to": new_status, "item_id": item_id},
    }


# Internal event sources are intentionally scoped to backlog status
# transitions for v1 — the one already flowing through _audited with a
# reliable pre/post signal. Live mail-poll and schedule-cron trigger sources
# are documented follow-up work (FEATURE-AUTOMATION-RULES.md "Framtida arbete").
INTERNAL_EVENT_HANDLERS = {
    "backlog_set_status": _index_internal_event_backlog_set_status,
}


def _maybe_publish_internal_event(user: str, project: str, tool: str, tail: tuple, kwargs: dict, result) -> None:
    """Best-effort internal-trigger publication — must never break the tool call itself."""
    handler = INTERNAL_EVENT_HANDLERS.get(tool)
    if handler is None:
        return
    try:
        built = handler(tail, kwargs, result)
        if built is None:
            return
        event = {"type": "internal", "project": project, "id": built["event_key"], "payload": built["payload"]}
        from .rules.engine import evaluate
        evaluate(_get_rules(), _get_acl(), event)
    except Exception:
        logger.warning("internal event publish failed for tool %r", tool, exc_info=True)


def _maybe_index_for_search(user: str, project: str, tool: str, tail: tuple, kwargs: dict, result) -> None:
    """Best-effort search-index update — must never break the tool call itself."""
    handler = SEARCH_INDEX_HANDLERS.get(tool)
    if handler is None:
        return
    try:
        acl = _get_acl()
        built = handler(acl, user, project, tail, kwargs, result)
        if built is None:
            return
        source_type, ref, title, text = built
        from .search.index import index_upsert
        index_upsert(_get_search_store(), _get_search_embedder(), project, source_type, ref, title, text)
    except Exception:
        logger.warning("search indexing failed for tool %r", tool, exc_info=True)


def _tool_call(
    tool: str, project: str, fn, *tail, need: str | None = None, idempotency_key: str | None = None, **kwargs,
):
    """Single entry point for a project-scoped tool call.

    Resolves the caller's identity, applies rate limiting, optionally enforces
    an ACL role, and audit-logs the outcome — the four steps every tool must
    perform.  ``fn`` is invoked as ``fn(acl, user, project, *tail, **kwargs)``,
    which is the shared signature of the tools.* functions.

    Most tools leave ``need`` as None because the underlying tools.* function
    performs its own ``acl.enforce``; pass ``need`` only for tools that gate
    at this layer.
    """
    user = _user()
    _rl(user, project)
    acl = _get_acl()
    if need is not None:
        acl.enforce(user, project, need)
    return _audited(user, project, tool, fn, acl, user, project, *tail, idempotency_key=idempotency_key, **kwargs)


mcp = FastMCP("memaix")

# Populate the capability registry (docs/FEATURE-DISCOVERABILITY.md) once at
# import time so onboarding/help/board surfaces always reflect the tools
# actually registered below.
_register_default_capabilities()


def all_tool_names() -> set[str]:
    """Return every MCP tool name registered on `mcp` — used by the anti-drift
    capability-coverage test (tests/test_capabilities_coverage.py) so a tool
    can never be added without being made discoverable or explicitly marked
    internal."""
    return {t.name for t in mcp._tool_manager.list_tools()}


# ------------------------------------------------------------------
# Fas 1 tools (unchanged)
# ------------------------------------------------------------------


@mcp.tool()
def whoami() -> dict:
    """Return the calling user's identity and project grants."""
    user = _user()
    acl = _get_acl()
    shared_vault = acl.resource("shared", "vault")
    vault_path = Path(shared_vault) if shared_vault else None
    return t_whoami.whoami(acl, user, vault=vault_path)


@mcp.prompt()
def onboarding_interview() -> str:
    """Run the new-user onboarding interview and store the resulting profile."""
    user = _user()
    cfg = config.load()
    shared_vault = _get_acl().resource("shared", "vault")
    vault = Path(shared_vault) if shared_vault else None
    return t_onboarding.build_interview_prompt(user, vault, cfg)


@mcp.tool()
def onboarding_complete(profile_content: str) -> dict:
    """Store the compiled onboarding profile and mark onboarding done."""
    user = _user()
    _rl(user, "shared")
    shared_vault = _get_acl().resource("shared", "vault")
    if not shared_vault:
        raise RuntimeError("shared vault not configured")
    result = _audited(
        user, "shared", "onboarding_complete",
        t_onboarding.complete_onboarding, user, Path(shared_vault), profile_content,
    )
    result["tour"] = _build_tour_for_user(user, profile_content)
    return result


def _build_tour_for_user(user: str, profile_text: str) -> dict:
    """Rank the user's now-available capabilities against their profile text
    and return a short guided tour — see docs/FEATURE-DISCOVERABILITY.md §5."""
    from .capabilities.registry import available_for

    cfg = config.load()
    t = _translator_for_config(cfg)
    available, _locked = available_for(_get_acl(), user, _get_accounts(user), cfg)
    return t_onboarding.build_tour(user, profile_text, available, t)


@mcp.tool()
def account_link(provider: str) -> dict:
    """Get an OAuth link URL to connect your account."""
    user = _user()
    cfg = config.load()
    public_url = cfg.get("memaix", {}).get("server", {}).get("public_url", _DEFAULT_PUBLIC_URL)
    return t_account.account_link(_get_acl(), user, provider, public_url)


@mcp.tool()
def account_list() -> list:
    """List your linked OAuth accounts."""
    user = _user()
    store = _get_token_store()
    return t_account.account_list(_get_acl(), user, store)


@mcp.tool()
def account_unlink(provider: str, account: str) -> dict:
    """Unlink an OAuth account."""
    user = _user()
    store = _get_token_store()
    return t_account.account_unlink(_get_acl(), user, provider, account, store)


@mcp.tool()
def account_scope_set(
    provider: str, account: str, capability: str, projects: list[str]
) -> dict:
    """Choose which projects may use a linked account for mail or calendar.

    capability is 'mail' or 'calendar' — set them separately to let a
    project read an account's calendar without reading its mail. projects
    replaces the current selection: ['*'] means every project you can see,
    [] revokes the capability entirely.
    """
    user = _user()
    store = _get_token_store()
    return t_account.account_scope_set(
        _get_acl(), user, provider, account, capability, projects, store
    )


@mcp.tool()
def account_scope_list(provider: str | None = None, account: str | None = None) -> list:
    """Show which projects each of your linked accounts is shared with."""
    user = _user()
    store = _get_token_store()
    return t_account.account_scope_list(_get_acl(), user, store, provider, account)


# ------------------------------------------------------------------
# Outbox tools — approve/reject queued outgoing actions (SAFETY: FEATURE-APPROVAL-OUTBOX.md)
# ------------------------------------------------------------------

# Role table + visibility filter live in outbox/policy.py (single source of
# truth shared with the board and web-UI APIs); these aliases keep the
# server-local names stable.
from .outbox.policy import APPROVAL_ROLE as _OUTBOX_APPROVAL_ROLE  # noqa: E402
from .outbox.policy import can_approve as _can_approve_action  # noqa: E402


def _outbox_action_or_404(outbox, action_id: str) -> dict:
    action = outbox.get(action_id)
    if action is None:
        raise FileNotFoundError(f"no such outbox action: {action_id!r}")
    return action


@mcp.tool()
def outbox_list(project: str | None = None, status: str = "pending") -> list:
    """List queued outgoing actions (default: pending) that you have the role to
    approve — the action body includes recipients/content, so it's scoped to
    would-be approvers, not every reader of the project."""
    user = _user()
    acl = _get_acl()
    visible = set(acl.visible_projects(user))
    projects = [project] if project else sorted(visible)
    projects = [p for p in projects if p in visible]
    return [a for a in _get_outbox().list(projects, status or None) if _can_approve_action(acl, user, a)]


@mcp.tool()
def outbox_get(action_id: str) -> dict:
    """Fetch a single queued action by id (requires the role that could approve it)."""
    user = _user()
    acl = _get_acl()
    outbox = _get_outbox()
    action = _outbox_action_or_404(outbox, action_id)
    if action["project"] not in acl.visible_projects(user):
        raise AccessDenied(f"{user} cannot see outbox actions for {action['project']}")
    if not _can_approve_action(acl, user, action):
        raise AccessDenied(f"{user} lacks the role to view/approve this {action.get('tool')} action")
    return action


@mcp.tool()
def outbox_approve(action_id: str) -> dict:
    """Approve a queued action and execute it now (requires the tool's own role)."""
    user = _user()
    acl = _get_acl()
    outbox = _get_outbox()
    action = _outbox_action_or_404(outbox, action_id)
    need = _OUTBOX_APPROVAL_ROLE.get(action["tool"], "owner")
    acl.enforce(user, action["project"], need)

    claimed = outbox.claim_for_decision(action_id, "approved", user)
    if claimed is None:
        current = outbox.get(action_id) or {}
        return {"conflict": True, "current_status": current.get("status")}

    from .outbox.execute import execute_pending
    result = execute_pending(acl, claimed)
    ok = "error" not in result
    outbox.record_result(action_id, "executed" if ok else "failed", result)
    _get_audit().log(
        user, action["project"], f"outbox_execute:{action['tool']}", ok,
        "" if ok else str(result.get("error", "")),
    )
    return {"ok": ok, "action_id": action_id, "result": result}


@mcp.tool()
def outbox_reject(action_id: str, reason: str = "") -> dict:
    """Reject a queued action — it is never executed."""
    user = _user()
    acl = _get_acl()
    outbox = _get_outbox()
    action = _outbox_action_or_404(outbox, action_id)
    need = _OUTBOX_APPROVAL_ROLE.get(action["tool"], "owner")
    acl.enforce(user, action["project"], need)

    claimed = outbox.claim_for_decision(action_id, "rejected", user, reason)
    if claimed is None:
        current = outbox.get(action_id) or {}
        return {"conflict": True, "current_status": current.get("status")}

    _get_audit().log(user, action["project"], f"outbox_reject:{action['tool']}", True, reason)
    return {"ok": True, "action_id": action_id, "status": "rejected"}


# ------------------------------------------------------------------
# Timeline tools — undo a recorded action (FEATURE-UNDO-TIMELINE.md)
# ------------------------------------------------------------------


@mcp.tool()
def timeline_list(project: str | None = None, limit: int = 50) -> list:
    """List recent actions (newest first) for your visible projects, with
    an `reversible` flag showing which ones can be undone via timeline_undo."""
    user = _user()
    acl = _get_acl()
    visible = set(acl.visible_projects(user))
    projects = [project] if project else sorted(visible)
    projects = [p for p in projects if p in visible]
    return _get_timeline().list(projects, limit)


@mcp.tool()
def timeline_undo(action_id: str) -> dict:
    """Undo a recorded action (requires the same role the original action did)."""
    user = _user()
    acl = _get_acl()
    from .timeline.undo import undo
    return undo(_get_timeline(), acl, user, action_id)


# ------------------------------------------------------------------
# Search tools — unified retrieval with source citations (FEATURE-SEMANTIC-SEARCH.md)
# ------------------------------------------------------------------


@mcp.tool()
def search_all(query: str, projects: list[str] | None = None, limit: int = 8) -> dict:
    """Search memory notes, files and backlog items (plus your mail where
    readable) across your projects. Returns ranked results with source
    citations {project, source_type, ref, title, snippet, score} — cite the
    project/source_type/ref when you answer from these results."""
    user = _user()
    acl = _get_acl()
    cfg = config.load()
    from .search.query import search_all as _search_all
    from .tools.email import email_search as _email_search_fn
    return _search_all(
        acl, user, cfg, _get_search_store(), _get_search_embedder(),
        query, projects, limit, _email_search=_email_search_fn,
    )


@mcp.tool()
def search_reindex(project: str) -> dict:
    """Rebuild the search index for a project from its current vault content (owner only)."""
    user = _user()
    _rl(user, project)
    acl = _get_acl()
    acl.enforce(user, project, "owner")
    from .search.index import reindex_project
    return reindex_project(_get_search_store(), _get_search_embedder(), acl, project)


@mcp.tool()
def search_status() -> dict:
    """Show whether semantic search is active and how many chunks are
    indexed per project you can see."""
    user = _user()
    acl = _get_acl()
    visible = acl.visible_projects(user)
    return {
        "semantic_enabled": _get_search_embedder() is not None,
        "chunks_by_project": _get_search_store().count_by_project(visible),
    }


# ------------------------------------------------------------------
# Brief tools — proactive daily brief & notifications (FEATURE-PROACTIVE-BRIEF.md)
# ------------------------------------------------------------------

_KNOWN_CHANNEL_TYPES = {"email", "webhook", "ntfy"}


def _validate_brief_time(brief_time: str) -> None:
    import re
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", brief_time or ""):
        raise ValueError(f"brief_time must be HH:MM (24h), got {brief_time!r}")


def _validate_timezone(tz_name: str) -> None:
    from zoneinfo import ZoneInfo
    try:
        ZoneInfo(tz_name)
    except Exception as exc:
        raise ValueError(f"unknown timezone: {tz_name!r}") from exc


def _validate_channels(channels: list[dict] | None) -> None:
    from .safety.net import BlockedURLError, validate_external_url

    for spec in channels or []:
        if spec.get("type") not in _KNOWN_CHANNEL_TYPES:
            raise ValueError(
                f"unknown channel type {spec.get('type')!r}; must be one of {sorted(_KNOWN_CHANNEL_TYPES)}"
            )
        # SSRF guard for the URLs the server will POST brief content to.
        # resolve=False at config-time; the authoritative resolve happens in
        # notify/channels.py right before the request.
        url = spec.get("url") if spec.get("type") == "webhook" else spec.get("server")
        if url:
            try:
                validate_external_url(url, resolve=False)
            except BlockedURLError as exc:
                raise ValueError(f"channel url avvisad: {exc}") from exc


@mcp.tool()
def brief_configure(
    enabled: bool,
    brief_time: str = "07:00",
    timezone: str | None = None,
    channels: list[dict] | None = None,
    quiet_hours: dict | None = None,
    projects: list[str] | None = None,
) -> dict:
    """Configure your daily brief: schedule (HH:MM in your timezone), delivery
    channels (email/webhook/ntfy), optional quiet hours, and which projects
    to cover (default: all you can see). timezone defaults to
    memaix.brief.default_timezone in config, falling back to UTC."""
    user = _user()
    cfg = config.load()
    timezone = timezone or cfg.get("memaix", {}).get("brief", {}).get("default_timezone", "UTC")
    _validate_brief_time(brief_time)
    _validate_timezone(timezone)
    _validate_channels(channels)

    from datetime import datetime
    from datetime import timezone as _tz
    store = _get_notify()
    prefs = store.set_prefs(
        user, now_iso=datetime.now(_tz.utc).isoformat(),
        enabled=enabled, brief_time=brief_time, timezone=timezone,
        channels=channels, projects=projects,
        quiet_start=(quiet_hours or {}).get("start"), quiet_end=(quiet_hours or {}).get("end"),
    )

    from .notify.scheduler import next_brief_epoch
    next_epoch = next_brief_epoch(prefs, datetime.now(_tz.utc))
    store.upsert_schedule(user, "daily", next_epoch)

    return {
        "ok": True, "prefs": prefs,
        "next_run": datetime.fromtimestamp(next_epoch, tz=_tz.utc).isoformat(),
    }


@mcp.tool()
def brief_status() -> dict:
    """Show your current brief configuration and next/last scheduled run."""
    user = _user()
    store = _get_notify()
    prefs = store.get_prefs(user)
    if prefs is None:
        return {"configured": False}

    from datetime import datetime
    from datetime import timezone as _tz
    schedule = store.get_schedule(user, "daily")
    next_run = (
        datetime.fromtimestamp(schedule["next_run"], tz=_tz.utc).isoformat() if schedule else None
    )
    last_run = (
        datetime.fromtimestamp(schedule["last_run"], tz=_tz.utc).isoformat()
        if schedule and schedule.get("last_run") else None
    )
    return {"configured": True, "prefs": prefs, "next_run": next_run, "last_run": last_run}


def _build_brief_now() -> dict:
    user = _user()
    acl = _get_acl()
    store = _get_notify()
    prefs = store.get_prefs(user) or {
        "timezone": "UTC", "brief_time": "07:00", "projects": [], "channels": [],
    }
    from datetime import datetime
    from datetime import timezone as _tz

    from .notify.brief import build
    return build(acl, user, config.load(), prefs, now=datetime.now(_tz.utc), tools=_brief_tools_for_user())


@mcp.tool()
def brief_preview() -> dict:
    """Build today's brief right now and return it, without sending it —
    the connector's 'fetch it when I open the app' path."""
    return _build_brief_now()


@mcp.tool()
def brief_send_now() -> dict:
    """Build and deliver your brief immediately via your configured channels.
    Ignores quiet hours and the once-per-day duplicate guard — this is an
    explicit request, so it always sends."""
    user = _user()
    acl = _get_acl()
    store = _get_notify()
    prefs = store.get_prefs(user)
    if not prefs:
        return {"ok": False, "error": "brief not configured — call brief_configure first"}

    from datetime import datetime
    from datetime import timezone as _tz

    from .notify.deliver import deliver
    result = deliver(
        store, acl, config.load(), user, prefs,
        now=datetime.now(_tz.utc), force=True, tools=_brief_tools_for_user(),
    )
    _get_audit().log(user, "shared", "brief_send", bool(result.get("ok")), "")
    return result



def _bd_calendar(acl, user, proj, fn, day_start, day_end) -> list[dict]:
    if not fn or not acl.resource(proj, "calendar"):
        return []
    try:
        return [
            {"title": ev.get("title", ""), "start": ev.get("start", ""),
             "end": ev.get("end", ""), "project": proj}
            for ev in (fn(acl, user, proj, day_start, day_end) or [])
        ]
    except Exception:
        return []


def _bd_mail(acl, user, proj, email_fn, triage_fn, max_mail, mail_days) -> list[dict]:
    if not email_fn or not (acl.resource(proj, "mailbox") or acl.resource(proj, "email")):
        return []
    try:
        msgs = email_fn(acl, user, proj, "INBOX", max_mail, days=mail_days) or []
        if triage_fn and msgs:
            msgs = triage_fn(msgs) or msgs
        result = []
        for m in msgs[:max_mail]:
            entry = {
                "subject": m.get("subject", "(inget ämne)"),
                "from": m.get("from", ""),
                "seen": m.get("seen", False),
                "project": proj,
            }
            if m.get("priority"):
                entry["priority"] = m["priority"]
            if m.get("summary"):
                entry["summary"] = m["summary"]
            result.append(entry)
        return result
    except Exception:
        return []


def _bd_backlog(acl, user, proj, fn, last_run_iso) -> list[dict]:
    if not fn or not acl.resource(proj, "vault"):
        return []
    try:
        items = fn(acl, user, proj) or []
        changed = [i for i in items if str(i.get("updated_at", "")) > last_run_iso]
        return [
            {"id": i.get("id", "?"), "title": i.get("title", ""),
             "status": i.get("status", ""), "updated_at": i.get("updated_at", ""),
             "project": proj}
            for i in changed[:10]
        ]
    except Exception:
        return []


def _bd_raid_open(acl, user, proj, fn) -> int:
    if not fn or not acl.resource(proj, "vault"):
        return 0
    try:
        raid = fn(acl, user, proj)
        entries = raid.get("entries", []) if isinstance(raid, dict) else []
        return sum(1 for e in entries if e.get("status") == "open")
    except Exception:
        return 0


@mcp.tool()
def brief_data(project: str | None = None, days_back: int = 1) -> dict:
    """Return the brief's underlying data as structured JSON (calendar, mail,
    backlog changes, open RAID count) so an AI client can render its own brief.
    project: limit to one project, or all visible projects if None.
    days_back: how far back to look for mail and backlog changes (default 1)."""
    user = _user()
    acl = _get_acl()
    store = _get_notify()
    prefs = store.get_prefs(user) or {}

    from datetime import datetime, timedelta
    from datetime import timezone as _tz

    now = datetime.now(_tz.utc)
    tz_name = prefs.get("timezone", "UTC")
    brief_cfg = config.load().get("memaix", {}).get("brief", {})
    max_mail = brief_cfg.get("max_mail", 5)
    mail_days = max(days_back, brief_cfg.get("mail_days", 3))

    from .notify.brief import _tz_or_utc
    tzinfo = _tz_or_utc(tz_name)
    local_now = now.astimezone(tzinfo)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    last_run_iso = (now - timedelta(days=days_back)).isoformat()

    if project is not None:
        projects = [project] if project in acl.visible_projects(user) else []
    else:
        projects = prefs.get("projects") or acl.visible_projects(user)

    t = _brief_tools_for_user()
    calendar_fn = t.get("calendar_events")
    email_fn = t.get("email_list")
    triage_fn = t.get("mail_triage")
    backlog_fn = t.get("backlog_list")
    raid_fn = t.get("pm_raid_list")

    calendar: list[dict] = []
    mail: list[dict] = []
    backlog_changes: list[dict] = []
    raid_open = 0

    for proj in projects:
        calendar += _bd_calendar(acl, user, proj, calendar_fn, day_start, day_end)
        mail += _bd_mail(acl, user, proj, email_fn, triage_fn, max_mail, mail_days)
        backlog_changes += _bd_backlog(acl, user, proj, backlog_fn, last_run_iso)
        raid_open += _bd_raid_open(acl, user, proj, raid_fn)

    return {
        "date": local_now.strftime("%Y-%m-%d"),
        "generated_at": now.isoformat(),
        "projects": projects,
        "calendar": calendar,
        "mail": mail,
        "backlog_changes": backlog_changes,
        "raid_open": raid_open,
    }


@mcp.prompt()
def daily_brief() -> str:
    """Deliver today's brief for the calling user (fetch-on-open path)."""
    return _build_brief_now()["markdown"]


# ------------------------------------------------------------------
# Automation rules & standing instructions (FEATURE-AUTOMATION-RULES.md)
# ------------------------------------------------------------------

_KNOWN_TRIGGER_TYPES = {"mail", "internal", "webhook", "schedule"}
_KNOWN_ACTION_TYPES = {"backlog_add", "memory_append", "pm_raid_add", "email_create_draft", "email_send", "notify"}
_KNOWN_CONDITION_OPS = {"contains", "equals", "matches"}
# 'notify' is outgoing egress too (posts to a webhook/ntfy/email channel) — a
# rule that auto-sends it therefore requires owner to create, like email_send.
_OUTGOING_ACTION_TYPES = {"email_send", "email_create_draft", "notify"}


def _validate_rule_spec(trigger: dict, actions: list[dict], conditions: list[dict] | None) -> None:
    if not isinstance(trigger, dict) or trigger.get("type") not in _KNOWN_TRIGGER_TYPES:
        raise ValueError(f"trigger.type must be one of {sorted(_KNOWN_TRIGGER_TYPES)}")
    if not actions:
        raise ValueError("a rule needs at least one action")
    for a in actions:
        if not isinstance(a, dict) or a.get("type") not in _KNOWN_ACTION_TYPES:
            raise ValueError(f"unknown action type {a.get('type') if isinstance(a, dict) else a!r}; "
                              f"must be one of {sorted(_KNOWN_ACTION_TYPES)}")
    for c in conditions or []:
        if c.get("op") not in _KNOWN_CONDITION_OPS:
            raise ValueError(f"unknown condition op {c.get('op')!r}; must be one of {sorted(_KNOWN_CONDITION_OPS)}")


@mcp.tool()
def rule_add(
    project: str, name: str, trigger: dict, actions: list[dict], conditions: list[dict] | None = None,
) -> dict:
    """Create an automation rule: when <trigger> happens (and <conditions>
    hold), run <actions>. Requires owner if any action is outgoing
    (email_send/email_create_draft — which still goes through the outbox if
    the project is in review mode), otherwise collaborator."""
    user = _user()
    _rl(user, project)
    acl = _get_acl()
    _validate_rule_spec(trigger, actions, conditions)
    needs_owner = any(a.get("type") in _OUTGOING_ACTION_TYPES for a in actions)
    acl.enforce(user, project, "owner" if needs_owner else "collaborator")
    rule = _get_rules().add_rule(user, project, name, trigger, actions, conditions)
    return {"ok": True, "rule": rule}


@mcp.tool()
def rule_list(project: str | None = None) -> list:
    """List your automation rules for projects you can see."""
    user = _user()
    acl = _get_acl()
    visible = set(acl.visible_projects(user))
    projects = [project] if project else sorted(visible)
    projects = [p for p in projects if p in visible]
    return _get_rules().list_rules(projects)


def _rule_or_404(rules, rule_id: str) -> dict:
    rule = rules.get_rule(rule_id)
    if rule is None:
        raise FileNotFoundError(f"no such rule: {rule_id!r}")
    return rule


@mcp.tool()
def rule_set_enabled(rule_id: str, enabled: bool) -> dict:
    """Enable or disable a rule (owner only)."""
    user = _user()
    acl = _get_acl()
    rules = _get_rules()
    rule = _rule_or_404(rules, rule_id)
    acl.enforce(user, rule["project"], "owner")
    return {"ok": rules.set_enabled(rule_id, enabled)}


@mcp.tool()
def rule_delete(rule_id: str) -> dict:
    """Delete a rule (owner only)."""
    user = _user()
    acl = _get_acl()
    rules = _get_rules()
    rule = _rule_or_404(rules, rule_id)
    acl.enforce(user, rule["project"], "owner")
    return {"ok": rules.delete_rule(rule_id)}


@mcp.tool()
def rule_test(rule_id: str, sample_event: dict) -> dict:
    """Dry-run a rule against a sample event — shows what it WOULD do,
    without doing it (owner only)."""
    user = _user()
    acl = _get_acl()
    rules = _get_rules()
    rule = _rule_or_404(rules, rule_id)
    acl.enforce(user, rule["project"], "owner")
    from .rules.engine import evaluate
    results = evaluate(rules, acl, sample_event, dry_run=True)
    matching = [r for r in results if r["rule_id"] == rule_id]
    return {"matched": bool(matching), "result": matching[0] if matching else None}


@mcp.tool()
def standing_set(text: str) -> dict:
    """Set your standing instructions — guidance the assistant follows every session."""
    user = _user()
    _get_rules().set_standing(user, text)
    return {"ok": True}


@mcp.tool()
def standing_get() -> dict:
    """Get your current standing instructions."""
    user = _user()
    return {"text": _get_rules().get_standing(user) or ""}


@mcp.resource("memaix://standing-instructions")
def standing_instructions_resource() -> str:
    """The calling user's standing instructions, for clients that read
    resources at session start."""
    user = _user()
    return _get_rules().get_standing(user) or ""


@mcp.tool()
def capabilities(area: str | None = None) -> dict:
    """List what Memaix can do for you right now, grouped by outcome area
    (memory, mail, calendar, backlog, pm, brief, search, automation, undo,
    outbox). Call with no area for a grouped overview; pass an area (e.g.
    'mail') to drill down into its capabilities with example prompts. Result
    is filtered to what you can actually use given your role, linked
    accounts, and project resources — locked capabilities are shown with a
    hint on how to unlock them."""
    return _capabilities_data(area)


@mcp.prompt()
def memaix_help(area: str = "") -> str:
    """Explain what Memaix can do: an overview grouped by outcome, or (given
    an area) a drill-down with example prompts and an offer to act now."""
    data = _capabilities_data(area or None)
    lines: list[str] = []
    if "areas" in data:
        lines += ["# Vad kan jag göra?", "", "Här är det jag kan hjälpa till med just nu:"]
        for entry in data["areas"]:
            titles = ", ".join(c["title"] for c in entry["capabilities"])
            lines.append(f"- **{entry['area']}**: {titles}")
        if data["locked"]:
            lines += ["", "Låst just nu:"]
            lines += [f"- {locked['title']} — {locked['hint']}" for locked in data["locked"]]
        lines += ["", "Fråga om ett specifikt område (t.ex. \"mail\") för konkreta exempel."]
    else:
        lines += [f"# {data['area']}", ""]
        for cap in data["capabilities"]:
            lines.append(f"## {cap['title']}")
            lines.append(cap["summary"])
            examples = cap["examples"] if isinstance(cap["examples"], list) else []
            lines += [f"- \"{ex}\"" for ex in examples]
            lines.append("")
        if data["locked"]:
            lines += ["Låst just nu:"]
            lines += [f"- {locked['title']} — {locked['hint']}" for locked in data["locked"]]
        lines += ["", "Vill du att jag gör något av detta nu?"]
    return "\n".join(lines)


@mcp.resource("memaix://capabilities")
def capabilities_resource() -> dict:
    """Same ACL/account-filtered overview as the `capabilities` tool, for
    clients that read resources at session start."""
    return _capabilities_data(None)


@mcp.tool()
def next_suggestion(last_tool: str) -> dict:
    """After calling `last_tool`, ask whether there's one natural next
    capability worth mentioning. Sparse and rate-limited — never suggests a
    locked capability, and returns {} most of the time by design."""
    import time

    from .capabilities.nudges import suggest
    from .capabilities.registry import available_for

    user = _user()
    cfg = config.load()
    t = _translator_for_config(cfg)
    available, _locked = available_for(_get_acl(), user, _get_accounts(user), cfg)
    result = suggest(user, last_tool, available, _get_nudge_state(), now=time.time())
    if result is None:
        return {}
    return {"capability_key": result["capability_key"], "title": t(result["title_key"])}


@mcp.tool()
def files_list(project: str, path: str = "/") -> list:
    """List files and directories in a project vault path."""
    return _tool_call("files_list", project, t_files.list_files, path)


@mcp.tool()
def files_read(project: str, path: str) -> str:
    """Read a file from a project vault."""
    return _tool_call("files_read", project, t_files.read_file, path)


@mcp.tool()
def files_write(project: str, path: str, content: str) -> str:
    """Write a file to a project vault."""
    return _tool_call("files_write", project, t_files.write_file, path, content)


@mcp.tool()
def files_search(project: str, query: str, path: str = "/") -> list:
    """Search file contents in a project vault."""
    return _tool_call("files_search", project, t_files.search_files, query, path)


# ------------------------------------------------------------------
# Memory tools
# ------------------------------------------------------------------


@mcp.tool()
def memory_read(project: str, note: str) -> dict:
    """Read a memory note from a project vault."""
    return _tool_call("memory_read", project, t_memory.memory_read, note)


@mcp.tool()
def memory_search(project: str, query: str) -> list:
    """Full-text search across memory notes in a project vault."""
    return _tool_call("memory_search", project, t_memory.memory_search, query)


@mcp.tool()
def memory_write(project: str, note: str, content: str, status: str | None = None) -> dict:
    """Write (overwrite) a memory note. status: 'hypotes' (default) eller
    'verifierad' — sätt verifierad ENDAST efter källbekräftelse eller
    mänskligt besked (minnestrappan, se whoami.memory_rules).

    Returnerar {"stored": false, "merged_into": path, "similarity": float} om
    innehållet är ett nära-duplikat av ett befintligt minne (novelty gate)."""
    embedder = _get_search_embedder()
    if embedder is not None:
        from .search.novelty import check_novelty
        threshold = (
            config.load().get("memaix", {}).get("memory", {}).get("novelty_threshold", 0.88)
        )
        match = check_novelty(content, project, note, embedder, _get_search_store(), threshold)
        if match is not None:
            return {
                "stored": False,
                "merged_into": match["existing_path"],
                "similarity": match["similarity"],
            }
    return _tool_call("memory_write", project, t_memory.memory_write, note, content, status)


@mcp.tool()
def memory_set_status(project: str, note: str, status: str) -> dict:
    """Flytta en notering i minnestrappan: 'hypotes' eller 'verifierad',
    utan att ändra innehållet. Befordra aldrig för att något låter rimligt —
    bara efter bekräftelse i källa/verktyg eller från en människa."""
    return _tool_call("memory_set_status", project, t_memory.memory_set_status, note, status)


@mcp.tool()
def memory_append(project: str, note: str, text: str) -> dict:
    """Append text to a memory note (creates if absent). En ny notering föds
    som hypotes (minnestrappan)."""
    return _tool_call("memory_append", project, t_memory.memory_append, note, text)


@mcp.tool()
def memory_history(project: str, note: str | None = None, limit: int = 20) -> list:
    """Git log for a note or the whole vault."""
    return _tool_call("memory_history", project, t_memory.memory_history, note, limit)


@mcp.tool()
def memory_revert(project: str, commit: str) -> dict:
    """Revert a git commit in the project vault."""
    return _tool_call("memory_revert", project, t_memory.memory_revert, commit)


# ------------------------------------------------------------------
# Backlog tools
# ------------------------------------------------------------------


@mcp.tool()
def backlog_add(project: str, title: str, description: str, category: str | None = None) -> dict:
    """Create a new backlog item (status: inbox)."""
    return _tool_call("backlog_add", project, t_backlog.backlog_add, title, description, category)


@mcp.tool()
def backlog_list(project: str, status: str | None = None, category: str | None = None) -> list:
    """List backlog items, optionally filtered by status or category."""
    return _tool_call("backlog_list", project, t_backlog.backlog_list, status, category)


@mcp.tool()
def backlog_get(project: str, id: str) -> dict:
    """Fetch a single backlog item by id."""
    return _tool_call("backlog_get", project, t_backlog.backlog_get, id)


@mcp.tool()
def backlog_score(
    project: str,
    id: str,
    expected_version: int,
    value: int | None = None,
    complexity: int | None = None,
    risk: int | None = None,
) -> dict:
    """Update scoring fields on a backlog item (optimistic locking)."""
    return _tool_call(
        "backlog_score", project, t_backlog.backlog_score,
        id, expected_version, value, complexity, risk,
    )


@mcp.tool()
def backlog_comment(project: str, id: str, text: str, expected_version: int) -> dict:
    """Append a comment to a backlog item (optimistic locking)."""
    return _tool_call(
        "backlog_comment", project, t_backlog.backlog_comment,
        id, text, expected_version,
    )


@mcp.tool()
def backlog_set_status(project: str, id: str, status: str, expected_version: int) -> dict:
    """Transition a backlog item to a new status (owner only, optimistic locking)."""
    return _tool_call(
        "backlog_set_status", project, t_backlog.backlog_set_status,
        id, status, expected_version,
    )


@mcp.tool()
def backlog_assign(project: str, id: str, assignee: str | None, expected_version: int) -> dict:
    """Assign a backlog item to an agent/user (owner only, optimistic locking).
    assignee must be a user in acl.yaml, or empty to unassign. The PM/owner
    allocates work; a team member picks up items assigned to them
    (FEATURE-AGENT-TEAM fas 1)."""
    return _tool_call(
        "backlog_assign", project, t_backlog.backlog_assign,
        id, assignee, expected_version,
    )


# ------------------------------------------------------------------
# PM tools
# ------------------------------------------------------------------


@mcp.tool()
def pm_set_methodology(
    project: str,
    methodology: str,
    sprint_length_days: int = 14,
    capacity: dict | None = None,
) -> dict:
    """Set project methodology and sprint capacity (owner only)."""
    return _tool_call(
        "pm_set_methodology", project, t_pm.pm_set_methodology,
        methodology, sprint_length_days, capacity,
    )


@mcp.tool()
def pm_status_report(project: str, note: str = "status") -> dict:
    """Generate a status report snapshot from backlog, RAID and memory notes."""
    return _tool_call("pm_status_report", project, t_pm.pm_status_report, note)


@mcp.tool()
def pm_plan_sprint(
    project: str,
    sprint_id: str,
    item_ids: list[str],
    goal: str = "",
) -> dict:
    """Commit backlog items into a named sprint and stamp each item (owner only)."""
    return _tool_call(
        "pm_plan_sprint", project, t_pm.pm_plan_sprint,
        sprint_id, item_ids, goal,
    )


@mcp.tool()
def pm_sprint_status(project: str, sprint_id: str) -> dict:
    """Burndown summary for a sprint: done vs remaining points."""
    return _tool_call("pm_sprint_status", project, t_pm.pm_sprint_status, sprint_id)


@mcp.tool()
def pm_raid_add(
    project: str,
    raid_type: str,
    summary: str,
    owner: str = "",
    severity: str = "medium",
    mitigation: str = "",
) -> dict:
    """Append a RAID entry (Risk/Assumption/Issue/Dependency) to the project log."""
    return _tool_call(
        "pm_raid_add", project, t_pm.pm_raid_add,
        raid_type, summary, owner, severity, mitigation,
    )


@mcp.tool()
def pm_raid_list(project: str, raid_type: str = "") -> dict:
    """List RAID entries, optionally filtered by type."""
    return _tool_call("pm_raid_list", project, t_pm.pm_raid_list, raid_type)


@mcp.prompt()
def pm_sprint_planning(project: str) -> str:
    """Guide the AI through sprint planning for a project."""
    return (
        f"You are planning a sprint for project '{project}'. "
        "1) Call backlog_list to see approved/evaluated items. "
        "2) Propose a sprint_id (e.g. SPRINT-01), a goal, and item_ids "
        "whose estimates fit the playbook capacity. "
        "3) Confirm with the user, then call pm_plan_sprint. "
        "If any item lacks an estimate, ask the user before committing."
    )


@mcp.prompt()
def pm_weekly_review(project: str) -> str:
    """Guide the AI through a weekly PM review."""
    return (
        f"Run a weekly PM review for '{project}': "
        "call pm_sprint_status on the active sprint, "
        "then pm_raid_list for open risks/issues, "
        "then pm_status_report to persist a snapshot. "
        "Summarize blockers and burndown for the user. "
        "If the project also runs the deterministic PM engine (check with "
        "scenario_list — a committed scenario means it does), also call "
        "pm_report(project, kind='status') for milestone/variance/RAID rollup, "
        "and pm_utilization for the committed scenario over the past week — "
        "quote the engine's numbers (slippage days, utilization %, which tasks "
        "are on the critical path per pm_variance's planned_finish) rather than "
        "estimating them yourself."
    )


# ------------------------------------------------------------------
# PM planning-engine tools (FEATURE-PM-ENGINE.md) — deterministic
# resource/task/critical-path/allocation engine, distinct from the
# markdown+git pm_* tools above. The engine computes; nothing here or in
# tools/pm_engine.py ever guesses a date.
# ------------------------------------------------------------------


@mcp.tool()
def resource_add(project: str, name: str, cost_per_hour: float | None = None, capacity_hours_per_day: float = 8.0) -> dict:
    """Add a plannable resource (person) to the PM engine."""
    return _tool_call(
        "resource_add", project, t_pm_engine.resource_add, name,
        cost_per_hour=cost_per_hour, capacity_hours_per_day=capacity_hours_per_day, _pm=_get_pm(),
    )


@mcp.tool()
def resource_list(project: str) -> list:
    """List PM-engine resources for a project."""
    return _tool_call("resource_list", project, t_pm_engine.resource_list, _pm=_get_pm())


@mcp.tool()
def resource_availability(
    project: str, resource_id: int, start_date: str, end_date: str, hours_per_day: float, reason: str | None = None,
) -> dict:
    """Record an availability exception (vacation, part-time, ...) for a resource."""
    return _tool_call(
        "resource_availability", project, t_pm_engine.resource_availability,
        resource_id, start_date, end_date, hours_per_day, reason, _pm=_get_pm(),
    )


@mcp.tool()
def resource_set_skill(project: str, resource_id: int, skill: str, level: int | None = None) -> dict:
    """Tag a resource with a skill (creates the skill if it's new)."""
    return _tool_call(
        "resource_set_skill", project, t_pm_engine.resource_set_skill, resource_id, skill, level, _pm=_get_pm(),
    )


@mcp.tool()
def milestone_add(project: str, name: str, target_date: str | None = None) -> dict:
    """Add a milestone that tasks can be linked to."""
    return _tool_call("milestone_add", project, t_pm_engine.milestone_add, name, target_date, _pm=_get_pm())


@mcp.tool()
def task_add(
    project: str, title: str, estimate_hours: float | None = None, required_skill: str | None = None,
    priority: int = 3, backlog_id: str | None = None, milestone_id: int | None = None,
) -> dict:
    """Add a PM-engine task (scheduled/allocated work — distinct from backlog_add)."""
    return _tool_call(
        "task_add", project, t_pm_engine.task_add, title,
        estimate_hours=estimate_hours, required_skill=required_skill, priority=priority,
        backlog_id=backlog_id, milestone_id=milestone_id, _pm=_get_pm(),
    )


@mcp.tool()
def task_estimate(project: str, task_id: int, estimate_hours: float) -> dict:
    """Set or update a task's estimate."""
    return _tool_call("task_estimate", project, t_pm_engine.task_estimate, task_id, estimate_hours, _pm=_get_pm())


@mcp.tool()
def task_log_actual(
    project: str, task_id: int, date: str, hours_logged: float | None = None,
    percent_complete: float | None = None, note: str | None = None,
) -> dict:
    """Log actual progress (hours and/or percent complete) against a task, for variance tracking."""
    return _tool_call(
        "task_log_actual", project, t_pm_engine.task_log_actual, task_id, date,
        hours_logged=hours_logged, percent_complete=percent_complete, note=note, _pm=_get_pm(),
    )


@mcp.tool()
def dependency_add(project: str, predecessor_id: int, successor_id: int, type: str = "FS", lag_days: float = 0.0) -> dict:
    """Add a task dependency (FS/SS/FF/SF). Rejected if it would create a cycle."""
    return _tool_call(
        "dependency_add", project, t_pm_engine.dependency_add,
        predecessor_id, successor_id, type, lag_days, _pm=_get_pm(),
    )


@mcp.tool()
def scenario_add(project: str, name: str) -> dict:
    """Create a planning scenario to allocate tasks against."""
    return _tool_call("scenario_add", project, t_pm_engine.scenario_add, name, _pm=_get_pm())


@mcp.tool()
def scenario_list(project: str) -> list:
    """List planning scenarios for a project."""
    return _tool_call("scenario_list", project, t_pm_engine.scenario_list, _pm=_get_pm())


@mcp.tool()
def pm_allocate(project: str, scenario_id: int, project_start: str | None = None) -> dict:
    """Run the planning engine for a scenario: critical path + resource-constrained
    allocation. Deterministic — always recomputed from resources/tasks/dependencies,
    never guessed. Owner only."""
    return _tool_call(
        "pm_allocate", project, t_pm_engine.pm_allocate, scenario_id, project_start, _pm=_get_pm(),
    )


@mcp.tool()
def pm_whatif(project: str, base_scenario_id: int, changes: list[dict], project_start: str | None = None) -> dict:
    """Simulate the effect of `changes` (each {entity: 'task'|'resource',
    entity_id, field, value} — supported fields: task.estimate_hours,
    task.priority, task.required_skill_id, resource.active) against
    base_scenario_id, in a brand-new scenario. Never touches the base or
    committed plan. Returns a diff: which tasks' finish date or criticality
    changed, which resource assignments changed, and which milestones moved.
    Collaborator role (it doesn't touch the committed plan, unlike allocate)."""
    return _tool_call(
        "pm_whatif", project, t_pm_engine.pm_whatif, base_scenario_id, changes, project_start, _pm=_get_pm(),
    )


@mcp.tool()
def pm_utilization(
    project: str, scenario_id: int, period_start: str, period_end: str, resource_id: int | None = None,
) -> dict:
    """Allocated hours vs capacity per resource over a period, for a scenario."""
    return _tool_call(
        "pm_utilization", project, t_pm_engine.pm_utilization,
        scenario_id, period_start, period_end, resource_id, _pm=_get_pm(),
    )


@mcp.tool()
def pm_variance(project: str, today: str | None = None) -> dict:
    """Compare the committed baseline plan against logged actuals (hours + schedule slippage).

    `today` (ISO date) overrides the default of "today in UTC" — e.g. to
    match a project's own calendar day when it differs from UTC."""
    return _tool_call("pm_variance", project, t_pm_engine.pm_variance, today, _pm=_get_pm())


@mcp.tool()
def plan_commit(project: str, scenario_id: int) -> dict:
    """Freeze a scenario as the committed plan and its baseline for future variance
    tracking. Owner only; who committed it is recorded (audit)."""
    return _tool_call("plan_commit", project, t_pm_engine.plan_commit, scenario_id, _pm=_get_pm())


@mcp.tool()
def pm_report(
    project: str, kind: str = "status", audience: str = "team",
    scenario_id: int | None = None, period_start: str | None = None, period_end: str | None = None,
) -> dict:
    """Rollup PM data (milestones/variance/RAID/utilization) for you to narrate.

    kind: 'status' (milestones+variance+raid, default), 'milestones',
    'variance', 'raid', or 'utilization' (requires scenario_id/period_start/
    period_end). audience: 'team' (full detail) or 'leadership' (condensed
    to overdue milestones and high/critical RAID entries)."""
    return _tool_call(
        "pm_report", project, t_pm_engine.pm_report, kind, audience, scenario_id, period_start, period_end,
        _pm=_get_pm(),
    )


@mcp.prompt()
def pm_plan_session(project: str) -> str:
    """Guide the AI through building a plan in the deterministic PM engine
    (resources/tasks/dependencies -> allocate), for '{project}'."""
    return (
        f"You are running a planning session for project '{project}' using the "
        "deterministic PM engine (resource_*/task_*/dependency_add/pm_allocate/"
        "plan_commit) — NOT the markdown pm_* tools (pm_sprint_planning). "
        "The engine computes dates and capacity; you never calculate a schedule, "
        "a critical path, or a finish date yourself. "
        "1) Ask the user for the goal and decompose it into tasks — propose "
        "estimate_hours and dependencies for each, but let the user confirm or "
        "correct them before calling task_add/dependency_add. "
        "2) Capture who's available: call resource_add for each person, "
        "resource_set_skill for any skills tasks require, and "
        "resource_availability for known absences (vacation, part-time). "
        "3) Call scenario_add, then pm_allocate(scenario_id) to run the engine. "
        "4) Explain the result in plain language: what's on the critical path, "
        "which tasks have zero slack, and flag anything the engine warned "
        "about (missing estimate, no eligible resource for a skill) as an open "
        "risk — never invent a date or assignment the engine didn't return. "
        "5) Only an owner can call plan_commit to freeze the baseline — ask them "
        "to review the allocation first."
    )


@mcp.prompt()
def pm_whatif_session(project: str) -> str:
    """Guide the AI through translating a plain-language "what if" question
    into a pm_whatif() call and explaining the diff, for '{project}'."""
    return (
        f"Help the user explore a hypothetical change to project '{project}'s plan "
        "using pm_whatif — it simulates changes in an isolated scenario and never "
        "touches the committed plan. You never compute the consequence yourself; "
        "the engine does. "
        "1) Get the committed baseline scenario_id (scenario_list, kind='baseline' "
        "or 'committed') to pass as base_scenario_id. "
        "2) Translate the user's question (e.g. \"what if we lose Anna for 2 "
        "weeks\", \"what if this task takes twice as long\") into a `changes` list: "
        "each change is {entity: 'task'|'resource', entity_id, field, value} — "
        "only estimate_hours/priority/required_skill_id (task) or active "
        "(resource) are supported; ask the user for a concrete number/id if the "
        "question doesn't map cleanly onto one of those. "
        "3) Call pm_whatif(project, base_scenario_id, changes). "
        "4) Explain the diff in plain language: which tasks' dates moved, "
        "whether the critical path changed, and any new resource conflicts — "
        "quote the engine's numbers, don't estimate your own. The whatif "
        "scenario is disposable; nothing is committed unless the user "
        "separately runs a real plan_commit on a real scenario."
    )


@mcp.tool()
def calendar_setup(
    project: str,
    mode: str,
    ical_url: str | None = None,
    calendar_id: str | None = None,
) -> dict:
    """Configure your personal calendar access mode for a project.

    mode='oauth'       — link via Google OAuth (full read+write). Returns a link_url to open.
    mode='ical_secret' — supply your Google Calendar secret iCal URL (read-only).
    mode='free_busy'   — supply your Google calendar_id (read-only, no event titles).
    mode='none'        — remove calendar configuration for this project.
    """
    user = _user()
    _rl(user, project)
    cfg = config.load()
    public_url = cfg.get("memaix", {}).get("server", {}).get("public_url", "")
    return t_cal.setup_mode(
        _get_acl(), user, project, mode, _get_token_store(), public_url,
        ical_url=ical_url, calendar_id=calendar_id,
    )


@mcp.tool()
def calendar_status(project: str) -> dict:
    """Show which calendar access mode is active for the calling user in this project."""
    user = _user()
    _rl(user, project)
    return t_cal.get_status(user, project, _get_acl(), _get_token_store())


# ------------------------------------------------------------------
# Contacts tools (FEATURE-NEXTCLOUD-BACKEND.md §5 — connector framework)
# ------------------------------------------------------------------


@mcp.tool()
def contacts_search(project: str, query: str) -> list:
    """Search the project's linked address book (e.g. Nextcloud CardDAV) by
    name, email, org, or phone substring. Returns [{id, name, email, org, phone}]."""
    from .connectors.registry import default_registry

    user = _user()
    _rl(user, project)
    acl = _get_acl()
    backend = default_registry().get(acl, _get_token_store(), project, "contacts", user)
    return _audited(
        user, project, "contacts_search", t_contacts.contacts_search, acl, user, project, query, _contacts=backend,
    )


@mcp.tool()
def contacts_get(project: str, id: str) -> dict:
    """Fetch one contact by id from the project's linked address book."""
    from .connectors.registry import default_registry

    user = _user()
    _rl(user, project)
    acl = _get_acl()
    backend = default_registry().get(acl, _get_token_store(), project, "contacts", user)
    return _audited(
        user, project, "contacts_get", t_contacts.contacts_get, acl, user, project, id, _contacts=backend,
    )


def _get_nc_files(project: str, user: str):
    from .connectors.registry import default_registry

    return default_registry().get(_get_acl(), _get_token_store(), project, "files", user)


@mcp.tool()
def nc_files_list(project: str, path: str = "/") -> list:
    """List files/directories in the project's linked Nextcloud (WebDAV) files —
    separate from files_list, which is the local vault."""
    user = _user()
    _rl(user, project)
    acl = _get_acl()
    backend = _get_nc_files(project, user)
    return _audited(user, project, "nc_files_list", t_nc_files.nc_files_list, acl, user, project, path, _files=backend)


@mcp.tool()
def nc_files_read(project: str, path: str) -> str:
    """Read a file from the project's linked Nextcloud (WebDAV) files."""
    user = _user()
    _rl(user, project)
    acl = _get_acl()
    backend = _get_nc_files(project, user)
    return _audited(user, project, "nc_files_read", t_nc_files.nc_files_read, acl, user, project, path, _files=backend)


@mcp.tool()
def nc_files_write(project: str, path: str, content: str) -> str:
    """Write a file to the project's linked Nextcloud (WebDAV) files — indexed
    for search_all like a local vault file."""
    user = _user()
    _rl(user, project)
    acl = _get_acl()
    backend = _get_nc_files(project, user)
    return _audited(
        user, project, "nc_files_write", t_nc_files.nc_files_write, acl, user, project, path, content, _files=backend,
    )


@mcp.tool()
def nc_files_search(project: str, query: str, path: str = "/") -> list:
    """Search the project's linked Nextcloud (WebDAV) files by content (skips large/binary files)."""
    user = _user()
    _rl(user, project)
    acl = _get_acl()
    backend = _get_nc_files(project, user)
    return _audited(
        user, project, "nc_files_search", t_nc_files.nc_files_search, acl, user, project, query, path, _files=backend,
    )


@mcp.tool()
def nc_generate_report(
    project: str, path: str, kind: str = "status", audience: str = "team",
    scenario_id: int | None = None, period_start: str | None = None, period_end: str | None = None,
) -> dict:
    """Render a pm_report() rollup (milestones/variance/RAID/utilization) as
    a real .odt file and write it to the project's linked Nextcloud files —
    for sharing a status report with stakeholders as a document, not just
    structured data. Same kind/audience options as pm_report."""
    user = _user()
    _rl(user, project)
    acl = _get_acl()
    backend = _get_nc_files(project, user)
    return _audited(
        user, project, "nc_generate_report", t_nc_docgen.nc_generate_report,
        acl, user, project, path, kind, audience, scenario_id, period_start, period_end,
        _files=backend, _pm=_get_pm(),
    )


def _get_nc_tasks(project: str, user: str):
    from .connectors.registry import default_registry

    return default_registry().get(_get_acl(), _get_token_store(), project, "tasks", user)


@mcp.tool()
def nc_tasks_list(project: str) -> list:
    """List tasks in the project's linked Nextcloud task list (CalDAV VTODO)."""
    user = _user()
    _rl(user, project)
    acl = _get_acl()
    backend = _get_nc_tasks(project, user)
    return _audited(user, project, "nc_tasks_list", t_nc_tasks.nc_tasks_list, acl, user, project, _tasks=backend)


@mcp.tool()
def nc_tasks_add(
    project: str, title: str, due: str | None = None, notes: str | None = None, idempotency_key: str | None = None,
) -> dict:
    """Add a task to the project's linked Nextcloud task list.

    Pass idempotency_key (same value on retry) to avoid creating a duplicate
    task if the call is retried after e.g. a network timeout."""
    user = _user()
    _rl(user, project)
    acl = _get_acl()
    backend = _get_nc_tasks(project, user)
    return _audited(
        user, project, "nc_tasks_add", t_nc_tasks.nc_tasks_add, acl, user, project, title, due, notes,
        _tasks=backend, idempotency_key=idempotency_key,
    )


@mcp.tool()
def nc_tasks_complete(project: str, id: str) -> dict:
    """Mark a Nextcloud task complete."""
    user = _user()
    _rl(user, project)
    acl = _get_acl()
    backend = _get_nc_tasks(project, user)
    return _audited(
        user, project, "nc_tasks_complete", t_nc_tasks.nc_tasks_complete, acl, user, project, id, _tasks=backend,
    )


@mcp.tool()
def deck_sync(project: str) -> dict:
    """Two-way sync between the project's linked Nextcloud Deck stack and its
    backlog. New cards become backlog items; when only one side changed since
    the last sync that side wins, and when both changed it's reported as a
    conflict (most-recently-changed side wins). Owner only — it mutates both
    stores. Only title/description are synced (v1 scope)."""
    from .connectors.registry import default_registry
    from .nextcloud.sync import deck_sync as _run_deck_sync

    user = _user()
    _rl(user, project)
    acl = _get_acl()
    deck_cfg = acl.resource(project, "deck") or {}
    backend = default_registry().get(acl, _get_token_store(), project, "deck", user)
    return _audited(
        user, project, "deck_sync", _run_deck_sync, acl, user, project,
        _deck=backend, board_id=deck_cfg.get("board_id"), stack_id=deck_cfg.get("stack_id"),
    )


def _get_notes_link_store():
    global _notes_link_store
    if _notes_link_store is None:
        from .nextcloud.notes_store import NotesLinkStore
        db_path = Path(os.environ.get("MEMAIX_NOTES_LINK_DB", str(_data_dir() / "memaix-notes-link.db")))
        _notes_link_store = NotesLinkStore.for_path(db_path)
    return _notes_link_store


@mcp.tool()
def notes_sync(project: str) -> dict:
    """Two-way sync between the project's linked Nextcloud Notes account and
    its memory notes. New notes become memory notes at 'notes/<slug>.md';
    when only one side changed since the last sync that side wins, and when
    both changed it's reported as a conflict (most-recently-changed side
    wins). Owner only — it mutates both stores."""
    from .connectors.registry import default_registry
    from .nextcloud.sync import notes_sync as _run_notes_sync

    user = _user()
    _rl(user, project)
    acl = _get_acl()
    backend = default_registry().get(acl, _get_token_store(), project, "notes", user)
    return _audited(
        user, project, "notes_sync", _run_notes_sync, acl, user, project,
        _notes=backend, link_store=_get_notes_link_store(),
    )


# ------------------------------------------------------------------
# Email tools
# ------------------------------------------------------------------


def _mail_backend(project: str, user: str):
    """Resolve the mail connector(s) for this project/user
    (FEATURE-CONNECTOR-FRAMEWORK.md — IMAP per-user, multiple mail sources
    per user).

    Uses registry.get_all() (not get()) so a user with more than one linked
    mail source (a project's shared IMAP mailbox AND/OR their own linked
    per-user IMAP mailbox(es)) sees all of them merged — see
    _multi_mail_backend below. With exactly one source (today's
    overwhelmingly common case: one shared IMAP mailbox, or one linked
    Microsoft account) this degenerates to returning that single adapter
    directly, so tools/email.py's behavior is byte-identical to before
    get_all() existed here — proven by test_email_server.py and
    test_mail_microsoft_server.py staying green unmodified.

    _ensure_fresh_microsoft_mail_token runs first: the registry's per_user
    branch just loads whatever token is stored, it doesn't refresh an
    expiring one — refreshing (and re-storing) has to happen before load,
    same division of labor as _resolve_calendar_dav's Google refresh."""
    from .connectors.registry import ConnectorAuthRequired, default_registry

    _ensure_fresh_microsoft_mail_token(user)
    _ensure_fresh_google_mail_token(user)
    acl = _get_acl()
    token_store = _get_token_store()
    registry = default_registry()

    sources = registry.get_all(acl, token_store, project, "mail", user)
    if not sources:
        # get_all() never raises for "nothing configured/linked" (registry.py's
        # documented contract) — but email_list/read/search callers expect the
        # same ConnectorAuthRequired/ValueError get() always raised in that
        # case, so re-derive it via get() for an identical error message.
        registry.get(acl, token_store, project, "mail", user)
        raise ConnectorAuthRequired("mail", "unknown")  # pragma: no cover - get() above always raises first
    from .connectors.adapters.mail_multi import MultiMailBackend, source_address

    if len(sources) == 1:
        label, adapter = sources[0]
        address = source_address(label)
        if address:
            # A linked account is its own inbox; tools/email.py labels its
            # messages with this rather than the acl.yaml mailbox address.
            try:
                setattr(adapter, "inbox_address", address)
            except AttributeError:
                pass  # an adapter that refuses attributes just keeps the fallback
        return adapter

    return MultiMailBackend(sources)


def _with_mail_backend(fn):
    """Wrap a tools.email function so `_imap` is resolved lazily, inside
    _audited's try/except — resolving it eagerly at the call site would move
    an unconfigured-mailbox ValueError outside the audit-log boundary."""

    def wrapped(acl, user_id, project, *args, **kwargs):
        return fn(acl, user_id, project, *args, _imap=_mail_backend(project, user_id), **kwargs)

    return wrapped


@mcp.tool()
def email_list(project: str, folder: str = "INBOX", limit: int = 20) -> list:
    """List recent messages in a mailbox folder."""
    return _tool_call("email_list", project, _with_mail_backend(t_email.email_list), folder, limit)


@mcp.tool()
def email_read(project: str, id: str, mark_seen: bool = False) -> dict:
    """Read a message by id (as returned by email_list/email_search).

    Reading does not change the message. Pass mark_seen=True to also mark it
    read; that is best-effort (a read-only linked account cannot) and never
    makes the read itself fail."""
    return _tool_call(
        "email_read", project, _with_mail_backend(t_email.email_read), id, mark_seen=mark_seen,
    )


@mcp.tool()
def email_search(
    project: str,
    query: str | None = None,
    limit: int = 50,
    since: str | None = None,
    until: str | None = None,
    from_addr: str | None = None,
    folder: str = "INBOX",
) -> list:
    """Search messages in a mailbox with optional filters.

    Args:
        project:   Memaix project whose mailbox to search.
        query:     Text to match. Omit to search by date/sender only. On a
                   Gmail source this is a Gmail search, so operators such as
                   has:attachment or subject:faktura work.
        limit:     Max messages to return (default 50).
        since:     ISO date YYYY-MM-DD — only messages on or after this date.
        until:     ISO date YYYY-MM-DD — only messages before this date
                   (so since=2026-09-01, until=2026-10-01 is all of September).
        from_addr: Sender address or domain, e.g. "anthropic.com" or "noreply@loopia.se".
        folder:    Mailbox folder (default "INBOX"). "ALL" searches all mail,
                   archived included (on an IMAP mailbox: every folder
                   except trash and junk).

    If one mail source (or IMAP folder) fails, the rest are still searched;
    the result then ends with a {warning, source_errors} row naming it.
    """
    return _tool_call(
        "email_search", project,
        _with_mail_backend(t_email.email_search),
        query, limit,
        since=since, until=until, from_addr=from_addr, folder=folder,
    )


@mcp.tool()
def email_create_draft(
    project: str,
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    in_reply_to: str | None = None,
    account: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Save a draft in one of the project's mailboxes (IMAP Drafts folder, or
    a Gmail/Outlook draft for a linked account). Nothing is sent.

    account: which mailbox gets the draft, when the project has several —
    the address shown in the `inbox` field of email_list/email_search, e.g.
    "jimmy@jimlov.se" for a linked Gmail account. Omit it to use the
    project's own mailbox (as before). An unknown account fails and lists
    the valid ones. A draft in a linked account gets that account's own
    From address.

    in_reply_to: make the draft a reply in an existing thread. Pass either
    the `id` of a message as returned by email_list/email_search/email_read
    (e.g. "123", or "google_mail:me@example.com|18f2c..." when the project
    has several mail sources) — the original's real Message-ID is looked up
    — or an RFC 5322 Message-ID such as "<abc@mail.example.com>". The draft
    gets In-Reply-To and References headers; keep the subject ("Re: ...")
    for mail clients to show it in the same conversation. Fails if the
    message can't be found rather than saving an unthreaded draft. Without
    `account`, a reply to an id from a specific source is saved in that
    source's mailbox, next to the thread.

    Returns {status, subject, account}: `account` is where the draft landed.

    Pass idempotency_key (same value on retry) to avoid creating a second
    draft if the call is retried after e.g. a network timeout."""
    return _tool_call(
        "email_create_draft", project, _with_mail_backend(t_email.email_create_draft),
        to, subject, body, cc, in_reply_to, account, idempotency_key=idempotency_key,
    )


@mcp.tool()
def email_send(
    project: str, to: str, subject: str, body: str, cc: str | None = None, idempotency_key: str | None = None,
) -> dict:
    """Send an email (requires owner + allow_send feature flag).

    Pass idempotency_key (same value on retry) to avoid sending a duplicate
    if the call is retried after e.g. a network timeout — a retry with the
    same key returns the original result (including a queued outbox
    action_id) instead of sending/queuing again."""
    return _tool_call(
        "email_send", project, t_email.email_send,
        to, subject, body, cc, idempotency_key=idempotency_key,
    )


# ------------------------------------------------------------------
# Calendar tools
# ------------------------------------------------------------------


@mcp.tool()
def calendar_list(project: str, start: str, end: str) -> list | dict:
    """List calendar events within a time range (ISO 8601)."""
    user = _user()
    _rl(user, project)
    try:
        dav = _resolve_calendar_dav(project, user)
        return _audited(user, project, "calendar_list", t_cal.calendar_list, _get_acl(), user, project, start, end, _dav=dav)
    except CalendarAuthRequired as e:
        return {"auth_required": True, "link_url": e.link_url, "options": e.options, "hint": _CALENDAR_SETUP_HINT}


@mcp.tool()
def calendar_find_free(
    project: str, duration_min: int, within_start: str, within_end: str
) -> list | dict:
    """Find free time slots of at least duration_min minutes."""
    user = _user()
    _rl(user, project)
    try:
        dav = _resolve_calendar_dav(project, user)
        return _audited(
            user, project, "calendar_find_free",
            t_cal.calendar_find_free,
            _get_acl(), user, project, duration_min, within_start, within_end, _dav=dav,
        )
    except CalendarAuthRequired as e:
        return {"auth_required": True, "link_url": e.link_url, "options": e.options, "hint": _CALENDAR_SETUP_HINT}


@mcp.tool()
def calendar_free_busy(project: str, start: str, end: str) -> dict:
    """Aggregated busy view across every calendar source linked for this
    project (memaix-src card 4daa20e2), from the periodic sync cache —
    not a live query. Unlike calendar_find_free (single adapter, live),
    this merges every linked calendar account plus any configured extra
    sources. Returns {busy, synced_at, stale, source_count, errors}."""
    user = _user()
    _rl(user, project)
    return _audited(
        user, project, "calendar_free_busy",
        t_cal.calendar_free_busy,
        _get_acl(), user, project, start, end,
    )


@mcp.tool()
def calendar_sources_list(project: str) -> dict:
    """Every calendar source configured/linked for this user/project, plus
    which are currently included in the free/busy aggregate — memaix-src
    card 324dd801. Returns {sources: [{label, kind, enabled}], public_links:
    [{id, label, url, enabled}]}, plus a warning if nothing is enabled."""
    user = _user()
    _rl(user, project)
    return _audited(
        user, project, "calendar_sources_list",
        t_cal.calendar_sources_list,
        _get_acl(), user, project, _get_token_store(),
    )


@mcp.tool()
def calendar_source_set_enabled(project: str, label: str, enabled: bool) -> dict:
    """Include/exclude one calendar source (label from calendar_sources_list)
    from the free/busy aggregate — memaix-src card 324dd801."""
    user = _user()
    _rl(user, project)
    return _audited(
        user, project, "calendar_source_set_enabled",
        t_cal.calendar_source_set_enabled,
        _get_acl(), user, project, label, enabled,
    )


@mcp.tool()
def calendar_public_link_add(project: str, url: str, label: str = "") -> dict:
    """Add a public .ics/webcal URL as an extra calendar source, no OAuth
    needed — memaix-src card 324dd801."""
    user = _user()
    _rl(user, project)
    return _audited(
        user, project, "calendar_public_link_add",
        t_cal.calendar_public_link_add,
        _get_acl(), user, project, url, label,
    )


@mcp.tool()
def calendar_public_link_remove(project: str, id: str) -> dict:
    """Remove a previously-added public calendar link — memaix-src card
    324dd801."""
    user = _user()
    _rl(user, project)
    return _audited(
        user, project, "calendar_public_link_remove",
        t_cal.calendar_public_link_remove,
        _get_acl(), user, project, id,
    )


@mcp.tool()
def calendar_events_list(project: str, start: str, end: str) -> dict:
    """Every cached source event in [start, end] with its resolved
    busy/free override state — memaix-src card c7698ff3. Returns {events:
    [{uid, source, title, start, end, source_busy, override, effective_busy,
    series_id, is_exception, in_series, overridable}], synced_at, stale}.
    A client renders these clickable and, when in_series is true, asks
    "just this instance, or the whole series?" before calling
    calendar_event_override_set."""
    user = _user()
    _rl(user, project)
    return _audited(
        user, project, "calendar_events_list",
        t_cal.calendar_events_list,
        _get_acl(), user, project, start, end,
    )


@mcp.tool()
def calendar_event_override_set(
    project: str, source: str, state: str, scope: str = "instance",
    uid: str | None = None, series_id: str | None = None, note: str = "",
) -> dict:
    """Force one event (scope="instance", needs uid) or a whole recurring
    series (scope="series", needs series_id) to busy/free regardless of
    what the source calendar said — memaix-src card c7698ff3. An exception
    instance never inherits a series override; set it individually via
    scope="instance"."""
    user = _user()
    _rl(user, project)
    return _audited(
        user, project, "calendar_event_override_set",
        t_cal.calendar_event_override_set,
        _get_acl(), user, project, source, state, scope, uid, series_id, note,
    )


@mcp.tool()
def calendar_event_override_clear(
    project: str, source: str, scope: str = "instance",
    uid: str | None = None, series_id: str | None = None,
) -> dict:
    """Remove a previously-set event/series override — memaix-src card
    c7698ff3."""
    user = _user()
    _rl(user, project)
    return _audited(
        user, project, "calendar_event_override_clear",
        t_cal.calendar_event_override_clear,
        _get_acl(), user, project, source, scope, uid, series_id,
    )


@mcp.tool()
def calendar_working_hours_get(project: str) -> dict:
    """The user's configured bookable weekly schedule — memaix-src card
    e21fde31. {} if never configured (wide-open, every time bookable)."""
    user = _user()
    _rl(user, project)
    return _audited(
        user, project, "calendar_working_hours_get",
        t_cal.calendar_working_hours_get,
        _get_acl(), user, project,
    )


@mcp.tool()
def calendar_working_hours_set(project: str, tz: str, week: dict) -> dict:
    """Set the bookable weekly schedule — memaix-src card e21fde31. *week*
    maps mon/tue/wed/thu/fri/sat/sun to a list of {start, end} local HH:MM
    windows; an empty/omitted day has nothing bookable. *tz* is an IANA
    zone (e.g. "Europe/Stockholm"). Only narrows calendar_find_free's
    output — never changes what the calendar itself reports as busy/free."""
    user = _user()
    _rl(user, project)
    return _audited(
        user, project, "calendar_working_hours_set",
        t_cal.calendar_working_hours_set,
        _get_acl(), user, project, tz, week,
    )


@mcp.tool()
def calendar_booking_enabled_get(project: str) -> dict:
    """Whether the meeting booker is on for this user — memaix-src card
    9e035c73. Off by default."""
    user = _user()
    _rl(user, project)
    return _audited(
        user, project, "calendar_booking_enabled_get",
        t_cal.calendar_booking_enabled_get,
        _get_acl(), user, project,
    )


@mcp.tool()
def calendar_booking_enabled_set(project: str, enabled: bool) -> dict:
    """Turn the meeting booker on or off — memaix-src card 9e035c73."""
    user = _user()
    _rl(user, project)
    return _audited(
        user, project, "calendar_booking_enabled_set",
        t_cal.calendar_booking_enabled_set,
        _get_acl(), user, project, enabled,
    )


@mcp.tool()
def calendar_meeting_type_list(project: str) -> list[dict]:
    """The user's named meeting-type presets — memaix-src card d0a1f633.
    [] if never configured. Advisory only, doesn't affect calendar_find_free."""
    user = _user()
    _rl(user, project)
    return _audited(
        user, project, "calendar_meeting_type_list",
        t_cal.calendar_meeting_type_list,
        _get_acl(), user, project,
    )


@mcp.tool()
def calendar_meeting_type_set(project: str, types: list[dict]) -> dict:
    """Replace the full list of meeting-type presets — memaix-src card
    d0a1f633. Each needs slug, name, duration_min (1..43200 min);
    interval_min defaults to duration_min. At most one default."""
    user = _user()
    _rl(user, project)
    return _audited(
        user, project, "calendar_meeting_type_set",
        t_cal.calendar_meeting_type_set,
        _get_acl(), user, project, types,
    )


@mcp.tool()
def calendar_meeting_type_delete(project: str, slug: str) -> dict:
    """Remove one meeting-type preset by slug — memaix-src card d0a1f633."""
    user = _user()
    _rl(user, project)
    return _audited(
        user, project, "calendar_meeting_type_delete",
        t_cal.calendar_meeting_type_delete,
        _get_acl(), user, project, slug,
    )


@mcp.tool()
def calendar_meeting_form_list(project: str) -> list[dict]:
    """The host's enabled meeting forms (video/phone options offered at
    booking time) — memaix-src card 85854d2c. [] if never configured,
    which the public booking page treats as "feature off"."""
    user = _user()
    _rl(user, project)
    return _audited(
        user, project, "calendar_meeting_form_list",
        t_cal.calendar_meeting_form_list,
        _get_acl(), user, project,
    )


@mcp.tool()
def calendar_meeting_form_set(project: str, forms: list[dict]) -> dict:
    """Replace the full list of enabled meeting forms — memaix-src card
    85854d2c. Each needs slug, provider (google_meet/zoom/phone) and
    label; a phone form also needs config.phone_number. At most one
    default; the first is auto-promoted if none is marked."""
    user = _user()
    _rl(user, project)
    return _audited(
        user, project, "calendar_meeting_form_set",
        t_cal.calendar_meeting_form_set,
        _get_acl(), user, project, forms,
    )


@mcp.tool()
def calendar_meeting_form_delete(project: str, slug: str) -> dict:
    """Remove one meeting form by slug — memaix-src card 85854d2c."""
    user = _user()
    _rl(user, project)
    return _audited(
        user, project, "calendar_meeting_form_delete",
        t_cal.calendar_meeting_form_delete,
        _get_acl(), user, project, slug,
    )


@mcp.tool()
def calendar_create(
    project: str,
    title: str,
    start: str,
    end: str,
    attendees: list[str] | None = None,
    location: str | None = None,
    description: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Create a calendar event.

    Pass idempotency_key (same value on retry) to avoid creating a duplicate
    event if the call is retried after e.g. a network timeout."""
    user = _user()
    _rl(user, project)
    try:
        dav = _resolve_calendar_dav(project, user, write=True)
        return _audited(
            user, project, "calendar_create",
            t_cal.calendar_create,
            _get_acl(), user, project, title, start, end, attendees, location, description, _dav=dav,
            idempotency_key=idempotency_key,
        )
    except CalendarAuthRequired as e:
        return {"auth_required": True, "link_url": e.link_url, "options": e.options, "hint": _CALENDAR_SETUP_HINT}


@mcp.tool()
def calendar_update(project: str, id: str, idempotency_key: str | None = None, **fields) -> dict:
    """Update fields on an existing calendar event.

    Pass idempotency_key (same value on retry) to avoid re-applying the
    same update twice if the call is retried after e.g. a network timeout."""
    user = _user()
    _rl(user, project)
    # Reject any leading-underscore key from the client: those names are
    # reserved for internal control kwargs (_dav/_confirmed/_outbox/_cfg) and
    # must never be settable by a caller — otherwise a client could pass
    # _confirmed=True and bypass the outbox gate in tools/calendar.py.
    fields = {k: v for k, v in fields.items() if not k.startswith("_")}
    try:
        dav = _resolve_calendar_dav(project, user, write=True)
        return _audited(
            user, project, "calendar_update",
            t_cal.calendar_update,
            _get_acl(), user, project, id, _dav=dav, idempotency_key=idempotency_key, **fields,
        )
    except CalendarAuthRequired as e:
        return {"auth_required": True, "link_url": e.link_url, "options": e.options, "hint": _CALENDAR_SETUP_HINT}


def _stamp_expiry(token_data: dict) -> dict:
    """Convert the provider's relative `expires_in` into an absolute
    `expires_at`, in place, before the token is stored.

    OAuth responses date themselves relatively ("valid for 3599 seconds"),
    which stops being true the moment it's written to disk. Every freshness
    check wants an absolute instant, and without one the fallback
    `created_at(0) + expires_in` evaluates to an epoch timestamp in 1970 —
    so a token stored this way is read as expired on EVERY request, forever.

    That was the live behaviour: each Gmail call burned a refresh round
    trip, and the moment a refresh_token was revoked the account went from
    "needs re-linking eventually" to "hard 401 on every call" with no
    usable access token left in between.

    Only stamped when `expires_in` is actually numeric — a provider that
    omits it leaves the existing (unknown) handling alone rather than
    getting a fabricated deadline.
    """
    import time

    expires_in = token_data.get("expires_in")
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
        token_data["expires_at"] = time.time() + expires_in
    return token_data


def _refresh_google_token(cfg: dict, store, user: str, account: str, token_data: dict) -> str | None:
    """Use the stored refresh_token to mint a new access_token. Updates store on success."""
    import requests as req_lib
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        return None
    provider_cfg = cfg.get("memaix", {}).get("oauth_providers", {}).get("google", {})
    client_id = provider_cfg.get("client_id", "")
    client_secret = config.secret(provider_cfg.get("client_secret_ref", "")) or ""
    try:
        resp = req_lib.post(
            _GOOGLE_TOKEN_URI,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=10,
        )
        resp.raise_for_status()
        new_data = resp.json()
        # Google doesn't re-issue refresh_token on refresh — preserve the original
        new_data.setdefault("refresh_token", refresh_token)
        store.store(user, "google", account, _stamp_expiry(new_data))
        return new_data.get("access_token")
    except Exception:
        return None


def _refresh_microsoft_token(cfg: dict, store, user: str, account: str, token_data: dict) -> str | None:
    """Mirrors _refresh_google_token for the microsoft provider (used by
    the Graph mail adapter)."""
    import requests as req_lib
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        return None
    provider_cfg = cfg.get("memaix", {}).get("oauth_providers", {}).get("microsoft", {})
    client_id = provider_cfg.get("client_id", "")
    client_secret = config.secret(provider_cfg.get("client_secret_ref", "")) or ""
    try:
        resp = req_lib.post(
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=10,
        )
        resp.raise_for_status()
        new_data = resp.json()
        # Microsoft may not re-issue refresh_token on every refresh — preserve the original
        new_data.setdefault("refresh_token", refresh_token)
        store.store(user, "microsoft", account, _stamp_expiry(new_data))
        return new_data.get("access_token")
    except Exception:
        return None


def _ensure_fresh_microsoft_mail_token(user: str) -> None:
    """If the user has a linked microsoft account, refresh its access_token
    when it's missing/expiring before the connector registry loads it —
    registry.get()'s per_user branch only loads whatever is stored, it
    doesn't refresh. A no-op if the user has no microsoft account (the
    project's mail resource is presumably shared IMAP instead)."""
    store = _get_token_store()
    accounts = [a for a in store.list_accounts(user) if a["provider"] == "microsoft"]
    if not accounts:
        return
    account = accounts[0]["account"]
    token_data = store.load_one(user, "microsoft", account)
    if not token_data:
        return
    import time
    expires_at = token_data.get("expires_at") or (
        token_data.get("created_at", 0) + token_data.get("expires_in", 3600)
    )
    if isinstance(expires_at, (int, float)) and expires_at - 60 < time.time():
        if not _refresh_microsoft_token(config.load(), store, user, account, token_data):
            store.mark_needs_relink(user, "microsoft", account)


def _ensure_fresh_google_mail_token(user: str) -> None:
    """Refresh expiring google access_tokens before the registry loads them.

    Mirrors _ensure_fresh_microsoft_mail_token: registry.get()'s per_user
    branch only loads what is stored, it never refreshes. Unlike the
    microsoft version this loops over every linked google account rather
    than just the first — multi-account is the whole point of this feature,
    and a stale token on account #2 would fail the merged fetch while
    account #1 looked fine.

    A no-op when the user has no google account linked.
    """
    import time

    store = _get_token_store()
    cfg = None
    for account in [a for a in store.list_accounts(user) if a["provider"] == "google"]:
        token_data = store.load_one(user, "google", account["account"])
        if not token_data:
            continue
        expires_at = token_data.get("expires_at") or (
            token_data.get("created_at", 0) + token_data.get("expires_in", 3600)
        )
        if not isinstance(expires_at, (int, float)) or expires_at - 60 >= time.time():
            continue
        if cfg is None:
            cfg = config.load()
        if not _refresh_google_token(cfg, store, user, account["account"], token_data):
            store.mark_needs_relink(user, "google", account["account"])


def _resolve_calendar_dav(project: str, user: str, *, write: bool = False):
    """Return a calendar adapter for the project/user.

    Checks TokenStore for the user's configured mode in priority order:
      1. OAuth (Google Calendar REST) — provider='google'
      2. iCal secret URL — provider='ical_secret'
      3. FreeBusy (read-only) — provider='free_busy'
      None → fall back to static CalDAV config from acl.yaml.
    Raises CalendarAuthRequired if project requires per_user but nothing is configured.

    write=True (memaix-src PR #100 follow-up) skips the service-account
    merge below: _ServiceAccountGoogleCalendarAdapter is read-only
    (calendar.readonly scope, no create/update/delete_event) and
    _MultiCalendarAdapter never implements those either, so a caller that
    needs to write (calendar_create/_update/_delete) must get back the
    single normal per-user/fallback adapter, never the SA or merged one —
    even though a read caller (calendar_list/_find_free/...) still wants
    the merged view so SA-only calendars show up as busy/free.
    """
    acl = _get_acl()
    cal_cfg = acl.resource(project, "calendar")
    cfg = config.load()
    store = _get_token_store()
    require_per_user = isinstance(cal_cfg, dict) and cal_cfg.get("auth") == "per_user"
    public_url = cfg.get("memaix", {}).get("server", {}).get("public_url", "")
    all_accounts = store.list_accounts(user)

    # 0. Service account (domain-wide delegation) — merged with the normal
    # per-user adapter when both resolve, so the SA's org calendars appear
    # alongside whatever the user has linked. Runs before OAuth on purpose.
    sa_res = acl.resource(project, "calendar_sa")
    if isinstance(sa_res, dict) and sa_res.get("auth") == "service_account":
        if write:
            # SA adapter can't write and merging it in would only ever
            # produce a read-only _MultiCalendarAdapter — go straight to
            # the normal/fallback adapter instead of resolving the SA.
            return _resolve_normal_calendar_dav(
                acl, cfg, store, project, user, all_accounts, require_per_user, public_url
            )
        import json
        import os
        try:
            ref = sa_res["service_account_ref"]
            if ref.startswith("env:"):
                env_var = ref[4:]
                sa_json = os.environ.get(env_var, "")
                if not sa_json:
                    raise CalendarAuthRequired(f"Env-var {env_var} saknas för SA-kalender")
                sa_info = json.loads(sa_json)
            elif ref.startswith("file:"):
                with open(ref[5:]) as f:
                    sa_info = json.load(f)
            else:
                raise CalendarAuthRequired(f"Okänd service_account_ref: {ref}")
        except CalendarAuthRequired:
            raise
        except (ValueError, FileNotFoundError, json.JSONDecodeError, KeyError, OSError) as exc:
            raise CalendarAuthRequired(f"SA-konfigfel för {project}: {exc}") from exc
        sa_adapter = _ServiceAccountGoogleCalendarAdapter(sa_info, sa_res["impersonate"])
        normal_adapter = None
        try:
            normal_adapter = _resolve_normal_calendar_dav(
                acl, cfg, store, project, user, all_accounts, require_per_user, public_url
            )
        except CalendarAuthRequired:
            normal_adapter = None
        if normal_adapter is not None:
            return _MultiCalendarAdapter([sa_adapter, normal_adapter])
        return sa_adapter

    return _resolve_normal_calendar_dav(
        acl, cfg, store, project, user, all_accounts, require_per_user, public_url
    )


def _resolve_normal_calendar_dav(
    acl, cfg, store, project, user, all_accounts, require_per_user, public_url
):
    # 1. OAuth (Google)
    google_accounts = [a for a in all_accounts if a["provider"] == "google"]
    if google_accounts:
        account_email = google_accounts[0]["account"]
        token_data = store.load_one(user, "google", account_email)
        if token_data:
            import time
            access_token = token_data.get("access_token")
            expires_at = token_data.get("expires_at") or (
                token_data.get("created_at", 0) + token_data.get("expires_in", 3600)
            )
            if not access_token or (
                isinstance(expires_at, (int, float)) and expires_at - 60 < time.time()
            ):
                access_token = _refresh_google_token(cfg, store, user, account_email, token_data)
                if not access_token:
                    store.mark_needs_relink(user, "google", account_email)
            if access_token:
                return _PerUserGoogleAdapter(access_token)

    # 2. iCal secret URL
    ical_accounts = [a for a in all_accounts if a["provider"] == "ical_secret"]
    if ical_accounts:
        token_data = store.load_one(user, "ical_secret", ical_accounts[0]["account"])
        if token_data and token_data.get("ical_url"):
            return _ICalAdapter(token_data["ical_url"])

    # 3. FreeBusy
    fb_accounts = [a for a in all_accounts if a["provider"] == "free_busy"]
    if fb_accounts:
        token_data = store.load_one(user, "free_busy", fb_accounts[0]["account"])
        api_key = cfg.get("memaix", {}).get("google_api_key", "")
        if token_data and token_data.get("calendar_id") and api_key:
            return _FreeBusyAdapter(token_data["calendar_id"], api_key)

    if not require_per_user:
        return None  # use static CalDAV from acl.yaml

    # Nothing configured — raise with all three setup options
    oauth_link = ""
    if public_url:
        try:
            oauth_link = t_account.account_link(acl, user, "google", public_url)["link_url"]
        except Exception:
            pass
    raise CalendarAuthRequired(
        link_url=oauth_link,
        options=[
            {
                "mode": "oauth",
                "label": "Google Calendar (full access, read+write)",
                "action": f"Öppna {oauth_link} och logga in med Google",
            },
            {
                "mode": "ical_secret",
                "label": "iCal secret URL (read-only, alla providers)",
                "action": "calendar_setup(mode='ical_secret', ical_url='din-hemliga-ical-url')",
            },
            {
                "mode": "free_busy",
                "label": "FreeBusy (visar bara ledig/upptagen, kräver publik kalender)",
                "action": "calendar_setup(mode='free_busy', calendar_id='din@gmail.com')",
            },
        ],
    )


def build_http_app():
    """Build the Starlette app with Bearer-auth for HTTP transport."""
    from starlette.middleware.cors import CORSMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse, RedirectResponse, Response
    from starlette.routing import Route

    # ------------------------------------------------------------------
    # Custom HTTP handlers
    # ------------------------------------------------------------------

    def health_handler(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "memaix"})

    def protected_resource_handler(request: Request) -> JSONResponse:
        """RFC 9728 protected resource metadata.

        FastMCP auto-generates this from AuthSettings.resource_server_url, but
        Pydantic's AnyHttpUrl always adds a trailing slash.  That creates a
        mismatch when claude.ai validates the JWT aud claim against the
        connector URL (typically typed without a trailing slash).  We override
        it here so the resource value is canonical without a trailing slash,
        which matches both forms.
        """
        cfg = config.load()
        auth_cfg = cfg.get("memaix", {}).get("auth", {})
        issuer = auth_cfg.get("issuer", "https://mcp.example.com/").rstrip("/")
        resource = auth_cfg.get("resource_server_url", issuer + "/").rstrip("/")
        return JSONResponse(
            {
                "resource": resource,
                "authorization_servers": [issuer + "/"],
                "bearer_methods_supported": ["header"],
            },
            headers={"Cache-Control": "public, max-age=3600"},
        )

    async def as_metadata_handler(request: Request) -> JSONResponse:
        """Serve OAuth AS metadata with registration_endpoint injected.

        Hydra v2 doesn't advertise registration_endpoint in its discovery
        document even when DCR is enabled — this handler proxies Hydra's
        openid-configuration and adds the missing field.
        """
        import httpx
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "http://hydra:4444/.well-known/openid-configuration",
                    timeout=5.0,
                )
                metadata = resp.json()
        except Exception:
            # Fallback: return minimal metadata so discovery doesn't hard-fail
            cfg = config.load()
            issuer = cfg.get("memaix", {}).get("auth", {}).get("issuer", _DEFAULT_ISSUER)
            metadata = {"issuer": issuer}

        issuer = metadata.get("issuer", _DEFAULT_ISSUER).rstrip("/")
        metadata["registration_endpoint"] = f"{issuer}/oauth2/register"
        return JSONResponse(metadata)

    async def dcr_handler(request: Request) -> JSONResponse:
        """Proxy DCR to Hydra, injecting resource audience so JWTs include aud claim.

        Hydra issues JWTs without aud unless the client's audience list explicitly
        contains the resource URL. This handler ensures every dynamically registered
        client is whitelisted for https://mcp.example.com (with and without trailing
        slash) before forwarding to Hydra's public DCR endpoint.
        """
        import httpx
        try:
            body = await request.json()
        except Exception:
            body = {}

        cfg = config.load()
        issuer = cfg.get("memaix", {}).get("auth", {}).get("issuer", _DEFAULT_ISSUER).rstrip("/")
        resource_urls = [f"{issuer}/", issuer]
        existing = body.get("audience") or []
        body["audience"] = list({*existing, *resource_urls})

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "http://hydra:4444/oauth2/register",
                    json=body,
                    headers={"Content-Type": "application/json"},
                    timeout=10.0,
                )
                return JSONResponse(_stada_dcr_svar(resp.json()), status_code=resp.status_code)
        except Exception as exc:
            logger.warning("DCR proxy error: %s", exc)
            return JSONResponse({"error": "server_error"}, status_code=500)

    def link_start(request: Request) -> "RedirectResponse | JSONResponse":
        """Start OAuth flow for a provider."""
        provider = request.path_params["provider"]
        state = request.query_params.get("state", "")

        PROVIDER_AUTH_URLS = {
            "google": "https://accounts.google.com/o/oauth2/v2/auth",
            "microsoft": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        }
        if provider not in PROVIDER_AUTH_URLS:
            return JSONResponse({"error": "unknown_provider"}, status_code=400)

        cfg = config.load()
        provider_cfg = cfg.get("memaix", {}).get("oauth_providers", {}).get(provider, {})
        client_id = provider_cfg.get("client_id", "")
        public_url = cfg.get("memaix", {}).get("server", {}).get("public_url", _DEFAULT_PUBLIC_URL)
        redirect_uri = f"{public_url.rstrip('/')}/link/{provider}/callback"

        from urllib.parse import urlencode
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": " ".join(provider_cfg.get("scopes", [])),
            "access_type": "offline",
            "prompt": "consent",
        }
        auth_url = PROVIDER_AUTH_URLS[provider] + "?" + urlencode(params)
        return RedirectResponse(auth_url)

    async def link_callback(request: Request) -> Response:
        """Handle OAuth callback: exchange code for tokens and store them."""
        provider = request.path_params["provider"]
        code = request.query_params.get("code", "")
        state = request.query_params.get("state", "")
        error = request.query_params.get("error", "")

        if error:
            return JSONResponse({"error": error}, status_code=400)

        from .tools.account import validate_state
        pending = validate_state(state)
        if not pending:
            return JSONResponse({"error": "invalid_or_expired_state"}, status_code=400)

        user_id = pending["user_id"]
        cfg = config.load()
        provider_cfg = cfg.get("memaix", {}).get("oauth_providers", {}).get(provider, {})
        public_url = cfg.get("memaix", {}).get("server", {}).get("public_url", _DEFAULT_PUBLIC_URL)
        redirect_uri = f"{public_url.rstrip('/')}/link/{provider}/callback"

        PROVIDER_TOKEN_URLS = {
            "google": _GOOGLE_TOKEN_URI,
            "microsoft": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        }
        token_url = PROVIDER_TOKEN_URLS.get(provider, "")
        client_secret = config.secret(provider_cfg.get("client_secret_ref", "")) or ""

        import httpx
        try:
            async with httpx.AsyncClient(timeout=10) as _hc:
                _resp = await _hc.post(
                    token_url,
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
                        "client_id": provider_cfg.get("client_id", ""),
                        "client_secret": client_secret,
                    },
                )
            _resp.raise_for_status()
            token_data = _resp.json()
        except Exception as exc:
            # Don't echo the raw exception (can carry internal URLs / response
            # fragments) back to the caller — log it, return a generic error.
            logger.warning("OAuth token exchange failed for provider %s: %s", provider, exc)
            return JSONResponse({"error": "token_exchange_failed"}, status_code=500)

        account_email = token_data.get("email", "") or _get_account_email(provider, token_data)

        store = _get_token_store()
        store.store(user_id, provider, account_email, _stamp_expiry(token_data))

        # HTML-escape values that ultimately derive from an IdP claim
        # (account_email) or the provider string before embedding them in the
        # success page — an attacker-controlled claim must not inject markup.
        from html import escape as _html_escape
        provider_label = _html_escape({"google": "Google", "microsoft": "Microsoft"}.get(provider, provider.title()))
        account_email = _html_escape(account_email or "")
        board_url = public_url.rstrip("/") + "/board"
        html = f"""<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Konto kopplat — Memaix</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0f1117; color: #e2e8f0;
      min-height: 100vh; display: flex; align-items: center; justify-content: center;
    }}
    .card {{
      background: #1a1f2e; border: 1px solid #2d3748; border-radius: 12px;
      padding: 2.5rem 3rem; max-width: 420px; width: 90%; text-align: center;
    }}
    .icon {{
      width: 56px; height: 56px; border-radius: 50%;
      background: #1a3a2a; border: 2px solid #38a169;
      display: flex; align-items: center; justify-content: center;
      margin: 0 auto 1.5rem;
      font-size: 1.6rem;
    }}
    h1 {{ font-size: 1.25rem; font-weight: 600; margin-bottom: .5rem; color: #f7fafc; }}
    .provider {{ color: #68d391; font-weight: 600; }}
    .account {{
      margin: 1.25rem auto 0;
      background: #0f1117; border: 1px solid #2d3748; border-radius: 8px;
      padding: .6rem 1rem; font-size: .85rem; color: #a0aec0;
      word-break: break-all;
    }}
    .account span {{ color: #e2e8f0; }}
    .actions {{ margin-top: 2rem; display: flex; gap: .75rem; justify-content: center; flex-wrap: wrap; }}
    a.btn {{
      display: inline-block; padding: .55rem 1.25rem; border-radius: 8px;
      font-size: .875rem; font-weight: 500; text-decoration: none; cursor: pointer;
    }}
    a.btn-primary {{ background: #2b6cb0; color: #fff; }}
    a.btn-primary:hover {{ background: #2c5282; }}
    a.btn-ghost {{ border: 1px solid #2d3748; color: #a0aec0; }}
    a.btn-ghost:hover {{ background: #2d3748; color: #e2e8f0; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">✓</div>
    <h1><span class="provider">{provider_label}</span> kopplat</h1>
    <p style="color:#a0aec0;font-size:.9rem;margin-top:.4rem">Ditt konto är länkat och redo att användas.</p>
    <div class="account">Inloggad som <span>{account_email or "okänt konto"}</span></div>
    <div class="actions">
      <a class="btn btn-primary" href="{board_url}">Tillbaka till board</a>
      <a class="btn btn-ghost" href="javascript:window.close()">Stäng fliken</a>
    </div>
  </div>
</body>
</html>"""
        from starlette.responses import HTMLResponse as _HTMLResponse
        return _HTMLResponse(html)

    async def rule_webhook(request: Request) -> JSONResponse:
        """Inbound trigger for webhook-type automation rules (FEATURE-AUTOMATION-RULES.md §6).

        The token is itself the shared secret (a random 'token' generated when
        the rule was created), compared in constant time (rules/match.py).
        Rate-limited per client IP so the token can't be brute-forced.

        Token resolution order:
          1. X-Webhook-Token header (preferred — keeps secret out of server logs)
          2. URL path param /hooks/{token} (backward-compatible, deprecated)
        """
        # Unauthenticated endpoint — rate-limit per client IP so a valid token
        # can't be guessed by volume (30 attempts / 60 s).
        client_ip = request.client.host if request.client else "unknown"
        if not _rate_limiter.check(f"webhook:{client_ip}", limit=30, window_s=60):
            return JSONResponse({"error": "rate_limited"}, status_code=429)

        # Prefer header over URL path so the token never appears in access logs.
        token = request.headers.get("X-Webhook-Token") or request.path_params.get("token", "")
        if not token:
            return JSONResponse({"error": "missing webhook token"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            body = {}

        import hashlib
        import json as _json
        digest = hashlib.sha256(_json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]
        event = {
            "type": "webhook", "project": None, "id": f"webhook:{token}:{digest}",
            "payload": {**body, "token": token},
        }
        from .rules.engine import evaluate
        results = evaluate(_get_rules(), _get_acl(), event)
        if not results:
            return JSONResponse({"error": "no matching enabled rule for this token"}, status_code=404)
        return JSONResponse({"ok": True, "matched": len(results)})

    # ------------------------------------------------------------------

    cfg = config.load()
    auth_cfg = cfg.get("memaix", {}).get("auth", {})

    if auth_cfg.get("issuer"):
        from mcp.server.auth.settings import AuthSettings

        from .auth.token import HydraTokenVerifier
        verifier = HydraTokenVerifier.from_config(cfg)
        mcp.settings.auth = AuthSettings(
            issuer_url=auth_cfg["issuer"],
            resource_server_url=auth_cfg.get("resource_server_url", auth_cfg["issuer"]),
        )
        mcp._token_verifier = verifier

    # FastMCP's DNS rebinding protection defaults to allowed_hosts=[] when binding
    # to 0.0.0.0 (as opposed to localhost), which causes 421 for every real hostname.
    # Explicitly allow the public host extracted from resource_server_url.
    from urllib.parse import urlparse as _urlparse2

    from mcp.server.transport_security import TransportSecuritySettings
    _pub_host = _urlparse2(
        auth_cfg.get("resource_server_url", auth_cfg.get("issuer", ""))
    ).netloc or "mcp.example.com"
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[_pub_host],
    )

    # Mount at root so claude.ai finds the endpoint at the connector URL directly.
    mcp.settings.streamable_http_path = "/"

    from .board.routes import board_routes
    from .booking.routes import booking_routes
    from .web.routes import web_routes

    custom_routes = [
        Route("/health", health_handler),
        Route("/.well-known/oauth-authorization-server", as_metadata_handler),
        Route("/oauth2/register", dcr_handler, methods=["POST"]),
        Route("/link/{provider}", link_start),
        Route("/link/{provider}/callback", link_callback),
        Route("/hooks", rule_webhook, methods=["POST"]),
        Route("/hooks/{token}", rule_webhook, methods=["POST"]),
        *board_routes,
        *web_routes,
        *booking_routes,
    ]

    mcp._custom_starlette_routes = custom_routes
    starlette_app = mcp.streamable_http_app()

    # FastMCP appends custom_starlette_routes AFTER its built-in routes, so the
    # auto-generated /.well-known/oauth-protected-resource route wins.  Prepend
    # our handler into the Starlette router routes list so it matches first.
    from starlette.routing import Route as _Route
    starlette_app.router.routes.insert(
        0, _Route("/.well-known/oauth-protected-resource", protected_resource_handler)
    )

    # Start the proactive-brief scheduler (FEATURE-PROACTIVE-BRIEF.md §7).
    # Runs as a background asyncio task inside the same process as the HTTP
    # server; a single worker deployment is assumed (see docs's multi-worker
    # note — the schedule table's compare-and-set claim keeps a second worker
    # from double-sending, but only one worker needs to run the loop at all).
    # Starlette dropped add_event_handler(); wrap the router's lifespan context
    # manager instead so our task starts/stops alongside FastMCP's own lifespan.
    if cfg.get("memaix", {}).get("brief", {}).get("enabled", True):
        import contextlib

        _original_lifespan = starlette_app.router.lifespan_context

        @contextlib.asynccontextmanager
        async def _lifespan_with_scheduler(app):
            import asyncio

            from .notify.deliver import deliver as _deliver_brief
            from .notify.scheduler import scheduler_loop

            def _deliver_for_user(user, prefs, now):
                _deliver_brief(
                    _get_notify(), _get_acl(), config.load(), user, prefs,
                    now=now, tools=_brief_tools_for_user(),
                )

            task = asyncio.create_task(scheduler_loop(_get_notify(), _deliver_for_user))
            try:
                async with _original_lifespan(app) as state:
                    yield state
            finally:
                task.cancel()

        starlette_app.router.lifespan_context = _lifespan_with_scheduler

    # Start the calendar cache/sync loop (memaix-src card 4daa20e2). Same
    # lifespan-wrapping pattern as the brief scheduler above — a second,
    # independent background task, since calendar sync and brief delivery
    # have unrelated schedules and failure isolation is worth two tasks
    # instead of one that does both.
    if cfg.get("memaix", {}).get("calendar_sync", {}).get("enabled", True):
        import contextlib

        _prior_lifespan = starlette_app.router.lifespan_context

        @contextlib.asynccontextmanager
        async def _lifespan_with_calendar_sync(app):
            import asyncio

            from .connectors.calendar_cache import calendar_sync_loop

            task = asyncio.create_task(calendar_sync_loop(_get_acl, _get_token_store))
            try:
                async with _prior_lifespan(app) as state:
                    yield state
            finally:
                task.cancel()

        starlette_app.router.lifespan_context = _lifespan_with_calendar_sync

    # Start the booking-consent purge loop (memaix-src card 01cf3b74). Same
    # lifespan-wrapping pattern as the two loops above — a third,
    # independent background task, since retention purging has nothing in
    # common with brief delivery or calendar sync's schedules.
    if cfg.get("memaix", {}).get("booking", {}).get("purge_enabled", True):
        import contextlib

        _prior_purge_lifespan = starlette_app.router.lifespan_context

        @contextlib.asynccontextmanager
        async def _lifespan_with_consent_purge(app):
            import asyncio

            from .booking.purge import consent_purge_loop

            task = asyncio.create_task(consent_purge_loop(_get_acl, _resolve_calendar_dav))
            try:
                async with _prior_purge_lifespan(app) as state:
                    yield state
            finally:
                task.cancel()

        starlette_app.router.lifespan_context = _lifespan_with_consent_purge

    # Start the meeting-reminders loop (memaix-src card ecffcb5b). Same
    # lifespan-wrapping pattern as the three loops above — a fourth,
    # independent background task.
    if cfg.get("memaix", {}).get("booking", {}).get("reminders_enabled", True):
        import contextlib

        _prior_reminder_lifespan = starlette_app.router.lifespan_context

        @contextlib.asynccontextmanager
        async def _lifespan_with_reminders(app):
            import asyncio

            from .booking.links import get_link
            from .booking.reminders import reminder_loop

            task = asyncio.create_task(reminder_loop(_get_acl, get_link))
            try:
                async with _prior_reminder_lifespan(app) as state:
                    yield state
            finally:
                task.cancel()

        starlette_app.router.lifespan_context = _lifespan_with_reminders

    # Browsers hitting the bare domain get the web UI, not the MCP 401 JSON.
    from .web.routes import BrowserRootRedirect

    starlette_app = BrowserRootRedirect(starlette_app)

    # Wrap with CORS so claude.ai browser requests aren't blocked.
    _cors_wrapped = CORSMiddleware(
        app=starlette_app,
        allow_origins=["https://claude.ai", "https://api.claude.ai"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "mcp-session-id"],
        expose_headers=["mcp-session-id"],
    )

    # /book/* handles CORS itself, scoped to jimlov.se (booking/routes.py
    # _cors_headers), including its own OPTIONS preflight responses. The
    # app-wide CORSMiddleware above only knows claude.ai — left in place it
    # would intercept the booking preflight and 400 it before ever reaching
    # booking/routes.py, since Starlette's CORSMiddleware answers every
    # Access-Control-Request-Method OPTIONS itself instead of delegating.
    # Route /book/* around it entirely rather than widening the claude.ai
    # allowlist, which would apply CORS to every other route too.
    class _BookingCorsBypass:
        def __init__(self, plain_app, cors_app):
            self._plain_app = plain_app
            self._cors_app = cors_app

        async def __call__(self, scope, receive, send):
            path = scope.get("path", "")
            is_booking = path.startswith("/book/") or path.startswith("/booking/")
            if scope["type"] == "http" and is_booking:
                await self._plain_app(scope, receive, send)
            else:
                await self._cors_app(scope, receive, send)

    app = _BookingCorsBypass(starlette_app, _cors_wrapped)

    return app


def _decode_id_token_claims(id_token: str) -> dict:
    """Decode an OIDC id_token's claims without verifying the signature.

    Safe here: the token was obtained directly from the provider's token
    endpoint over TLS during the exchange above (never supplied by the
    client), and is only used to derive a stable account identifier — not to
    authenticate a request. Returns {} on any decode failure.
    """
    import jwt
    try:
        return jwt.decode(
            id_token,
            options={"verify_signature": False, "verify_aud": False, "verify_exp": False},  # NOSONAR
        )
    except Exception:
        return {}


def _get_account_email(provider: str, token_data: dict) -> str:
    """Derive a stable per-account identifier from the token response.

    Google/Microsoft never put the email in the token endpoint body itself —
    it lives in the id_token's claims (requires an 'openid'+'email' scope).
    Without this, every linked account for a provider fell back to the same
    'linked-<provider>' key and a second linked account silently overwrote
    the first in the token store. Falls back to the token's 'sub' (still
    unique per account) before the last-resort shared placeholder.
    """
    id_token = token_data.get("id_token")
    if id_token:
        claims = _decode_id_token_claims(id_token)
        email = claims.get("email") or claims.get("preferred_username") or claims.get("upn")
        if email:
            return email
        sub = claims.get("sub")
        if sub:
            return f"{provider}-{sub}"
    return f"linked-{provider}"


def main() -> None:
    import sys
    if "--http" in sys.argv or os.environ.get("MEMAIX_TRANSPORT") == "http":
        import uvicorn
        app = build_http_app()
        cfg = config.load()
        bind = cfg.get("memaix", {}).get("server", {}).get("bind", "0.0.0.0:8080")
        host, port = bind.rsplit(":", 1)
        uvicorn.run(app, host=host, port=int(port), log_level="info")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
