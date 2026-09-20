# SPDX-License-Identifier: AGPL-3.0-or-later
"""Capability catalog — registers today's MCP tools with the registry.

Import this module once at process startup (server.py does it) so the
registry is populated before any capability lookup happens. Every new tool
added to server.py must either get a Capability here or be added to
INTERNAL_TOOLS — enforced by tests/test_capabilities_coverage.py.
"""

from __future__ import annotations

from .registry import Capability, register

# Tools that are plumbing, not a user-facing "job to be done" on their own.
# whoami/onboarding_complete are identity/setup mechanics; account_* are
# covered contextually by calendar_setup's auth_required flow rather than as
# a standalone capability (see docs/FEATURE-DISCOVERABILITY.md §9).
INTERNAL_TOOLS: frozenset[str] = frozenset(
    {
        "whoami", "onboarding_complete", "account_link", "account_list", "account_unlink",
        # account_scope_* share that classification: the settings page is their
        # discovery surface, not the capability list. Revisit if they ever need
        # to be suggested on their own (would need i18n title/summary keys).
        "account_scope_set", "account_scope_list",
        # Discoverability's own meta-surface (docs/FEATURE-DISCOVERABILITY.md §9) —
        # these describe/surface capabilities, they aren't a "job to be done" themselves.
        "capabilities", "next_suggestion",
    }
)


def register_defaults() -> None:
    register(
        # ------------------------------------------------------------------
        # memory
        # ------------------------------------------------------------------
        Capability(
            key="memory.remember", area="memory",
            title_key="cap.memory.remember.title",
            summary_key="cap.memory.remember.summary",
            tools=("memory_write", "memory_append", "memory_set_status"),
            example_prompts_key="cap.memory.remember.examples",
            needs_role="collaborator", needs_resource="vault",
            tags=("minne", "anteckning", "memory", "note"),
        ),
        Capability(
            key="memory.recall", area="memory",
            title_key="cap.memory.recall.title",
            summary_key="cap.memory.recall.summary",
            tools=("memory_read", "memory_search", "memory_history"),
            example_prompts_key="cap.memory.recall.examples",
            needs_role="reader", needs_resource="vault",
            tags=("minne", "sök", "memory", "search"),
        ),
        Capability(
            key="memory.undo", area="memory",
            title_key="cap.memory.undo.title",
            summary_key="cap.memory.undo.summary",
            tools=("memory_revert",),
            example_prompts_key="cap.memory.undo.examples",
            needs_role="collaborator", needs_resource="vault",
            tags=("ångra", "undo"),
        ),
        Capability(
            key="memory.notes_sync", area="memory",
            title_key="cap.memory.notes_sync.title",
            summary_key="cap.memory.notes_sync.summary",
            tools=("notes_sync",),
            example_prompts_key="cap.memory.notes_sync.examples",
            needs_role="owner", needs_resource="notes",
            tags=("nextcloud", "notes", "memory", "sync"),
        ),
        # ------------------------------------------------------------------
        # mail
        # ------------------------------------------------------------------
        Capability(
            key="mail.triage", area="mail",
            title_key="cap.mail.triage.title",
            summary_key="cap.mail.triage.summary",
            tools=("email_list", "email_search", "email_read"),
            example_prompts_key="cap.mail.triage.examples",
            needs_role="collaborator", needs_resource="mailbox",
            tags=("mejl", "inkorg", "mail", "inbox"),
        ),
        Capability(
            key="mail.draft", area="mail",
            title_key="cap.mail.draft.title",
            summary_key="cap.mail.draft.summary",
            tools=("email_create_draft",),
            example_prompts_key="cap.mail.draft.examples",
            needs_role="collaborator", needs_resource="mailbox",
            tags=("mejl", "utkast", "draft"),
        ),
        Capability(
            key="mail.send", area="mail",
            title_key="cap.mail.send.title",
            summary_key="cap.mail.send.summary",
            tools=("email_send",),
            example_prompts_key="cap.mail.send.examples",
            needs_role="owner", needs_resource="mailbox",
            tags=("mejl", "skicka", "send"),
        ),
        # ------------------------------------------------------------------
        # calendar
        # ------------------------------------------------------------------
        Capability(
            key="calendar.view", area="calendar",
            title_key="cap.calendar.view.title",
            summary_key="cap.calendar.view.summary",
            tools=("calendar_list", "calendar_find_free", "calendar_free_busy"),
            example_prompts_key="cap.calendar.view.examples",
            needs_role="collaborator", needs_resource="calendar",
            tags=("kalender", "möte", "calendar", "meeting"),
        ),
        Capability(
            key="calendar.manage", area="calendar",
            title_key="cap.calendar.manage.title",
            summary_key="cap.calendar.manage.summary",
            tools=("calendar_create", "calendar_update"),
            example_prompts_key="cap.calendar.manage.examples",
            needs_role="collaborator", needs_resource="calendar",
            tags=("kalender", "boka", "calendar", "book"),
        ),
        Capability(
            key="calendar.sources_view", area="calendar",
            title_key="cap.calendar.sources_view.title",
            summary_key="cap.calendar.sources_view.summary",
            tools=("calendar_sources_list",),
            example_prompts_key="cap.calendar.sources_view.examples",
            needs_role="reader", needs_resource="calendar",
            tags=("kalender", "källor", "calendar", "sources"),
        ),
        Capability(
            key="calendar.sources_manage", area="calendar",
            title_key="cap.calendar.sources_manage.title",
            summary_key="cap.calendar.sources_manage.summary",
            tools=("calendar_source_set_enabled", "calendar_public_link_add", "calendar_public_link_remove"),
            example_prompts_key="cap.calendar.sources_manage.examples",
            needs_role="collaborator", needs_resource="calendar",
            tags=("kalender", "källor", "publik länk", "calendar", "sources", "ics"),
        ),
        Capability(
            key="calendar.events_view", area="calendar",
            title_key="cap.calendar.events_view.title",
            summary_key="cap.calendar.events_view.summary",
            tools=("calendar_events_list",),
            example_prompts_key="cap.calendar.events_view.examples",
            needs_role="reader", needs_resource="calendar",
            tags=("kalender", "händelser", "calendar", "events"),
        ),
        Capability(
            key="calendar.events_override", area="calendar",
            title_key="cap.calendar.events_override.title",
            summary_key="cap.calendar.events_override.summary",
            tools=("calendar_event_override_set", "calendar_event_override_clear"),
            example_prompts_key="cap.calendar.events_override.examples",
            needs_role="collaborator", needs_resource="calendar",
            tags=("kalender", "upptagen", "tillgänglig", "serie", "calendar", "override", "busy", "series"),
        ),
        Capability(
            key="calendar.working_hours", area="calendar",
            title_key="cap.calendar.working_hours.title",
            summary_key="cap.calendar.working_hours.summary",
            tools=("calendar_working_hours_get", "calendar_working_hours_set"),
            example_prompts_key="cap.calendar.working_hours.examples",
            needs_role="collaborator", needs_resource="calendar",
            tags=("kalender", "arbetstid", "veckoschema", "calendar", "working hours", "schedule"),
        ),
        Capability(
            key="calendar.booking_enabled", area="calendar",
            title_key="cap.calendar.booking_enabled.title",
            summary_key="cap.calendar.booking_enabled.summary",
            tools=("calendar_booking_enabled_get", "calendar_booking_enabled_set"),
            example_prompts_key="cap.calendar.booking_enabled.examples",
            needs_role="collaborator", needs_resource="calendar",
            tags=("kalender", "bokning", "på/av", "calendar", "booking", "toggle"),
        ),
        Capability(
            key="calendar.meeting_types", area="calendar",
            title_key="cap.calendar.meeting_types.title",
            summary_key="cap.calendar.meeting_types.summary",
            tools=(
                "calendar_meeting_type_list",
                "calendar_meeting_type_set",
                "calendar_meeting_type_delete",
            ),
            example_prompts_key="cap.calendar.meeting_types.examples",
            needs_role="collaborator", needs_resource="calendar",
            tags=("kalender", "mötestyp", "längd", "calendar", "meeting type", "duration"),
        ),
        Capability(
            key="calendar.meeting_forms", area="calendar",
            title_key="cap.calendar.meeting_forms.title",
            summary_key="cap.calendar.meeting_forms.summary",
            tools=(
                "calendar_meeting_form_list",
                "calendar_meeting_form_set",
                "calendar_meeting_form_delete",
            ),
            example_prompts_key="cap.calendar.meeting_forms.examples",
            needs_role="collaborator", needs_resource="calendar",
            tags=("kalender", "mötesform", "video", "zoom", "meet", "telefon", "calendar", "meeting form", "video"),
        ),
        Capability(
            key="calendar.connect", area="calendar",
            title_key="cap.calendar.connect.title",
            summary_key="cap.calendar.connect.summary",
            tools=("calendar_setup", "calendar_status"),
            example_prompts_key="cap.calendar.connect.examples",
            needs_role="reader", needs_resource=None,
            tags=("kalender", "koppla", "connect"),
        ),
        # ------------------------------------------------------------------
        # files
        # ------------------------------------------------------------------
        Capability(
            key="files.manage", area="pm",  # grouped visually with project work
            title_key="cap.files.manage.title",
            summary_key="cap.files.manage.summary",
            tools=("files_list", "files_read", "files_search", "files_write"),
            example_prompts_key="cap.files.manage.examples",
            needs_role="collaborator", needs_resource="vault",
            tags=("filer", "dokument", "files", "documents"),
        ),
        Capability(
            key="files.nextcloud", area="pm",
            title_key="cap.files.nextcloud.title",
            summary_key="cap.files.nextcloud.summary",
            tools=("nc_files_list", "nc_files_read", "nc_files_write", "nc_files_search", "nc_generate_report"),
            example_prompts_key="cap.files.nextcloud.examples",
            needs_role="collaborator", needs_resource="files",
            tags=("filer", "nextcloud", "webdav", "files", "documents"),
        ),
        Capability(
            key="tasks.nextcloud", area="pm",
            title_key="cap.tasks.nextcloud.title",
            summary_key="cap.tasks.nextcloud.summary",
            tools=("nc_tasks_list", "nc_tasks_add", "nc_tasks_complete"),
            example_prompts_key="cap.tasks.nextcloud.examples",
            needs_role="reader", needs_resource="tasks",
            tags=("uppgifter", "tasks", "nextcloud", "todo", "checklist"),
        ),
        # ------------------------------------------------------------------
        # backlog
        # ------------------------------------------------------------------
        Capability(
            key="backlog.capture", area="backlog",
            title_key="cap.backlog.capture.title",
            summary_key="cap.backlog.capture.summary",
            tools=("backlog_add",),
            example_prompts_key="cap.backlog.capture.examples",
            needs_role="collaborator", needs_resource="vault",
            tags=("backlog", "idé", "idea"),
        ),
        Capability(
            key="backlog.review", area="backlog",
            title_key="cap.backlog.review.title",
            summary_key="cap.backlog.review.summary",
            tools=("backlog_list", "backlog_get"),
            example_prompts_key="cap.backlog.review.examples",
            needs_role="reader", needs_resource="vault",
            tags=("backlog", "lista", "list"),
        ),
        Capability(
            key="backlog.score", area="backlog",
            title_key="cap.backlog.score.title",
            summary_key="cap.backlog.score.summary",
            tools=("backlog_score", "backlog_comment"),
            example_prompts_key="cap.backlog.score.examples",
            needs_role="collaborator", needs_resource="vault",
            tags=("backlog", "prioritera", "prioritize"),
        ),
        Capability(
            key="backlog.decide", area="backlog",
            title_key="cap.backlog.decide.title",
            summary_key="cap.backlog.decide.summary",
            tools=("backlog_set_status",),
            example_prompts_key="cap.backlog.decide.examples",
            needs_role="owner", needs_resource="vault",
            tags=("backlog", "godkänn", "approve"),
        ),
        Capability(
            key="backlog.assign", area="backlog",
            title_key="cap.backlog.assign.title",
            summary_key="cap.backlog.assign.summary",
            tools=("backlog_assign",),
            example_prompts_key="cap.backlog.assign.examples",
            needs_role="owner", needs_resource="vault",
            tags=("backlog", "tilldela", "assign", "team", "agent"),
        ),
        Capability(
            key="backlog.deck_sync", area="backlog",
            title_key="cap.backlog.deck_sync.title",
            summary_key="cap.backlog.deck_sync.summary",
            tools=("deck_sync",),
            example_prompts_key="cap.backlog.deck_sync.examples",
            needs_role="owner", needs_resource="deck",
            tags=("deck", "nextcloud", "backlog", "sync", "kanban"),
        ),
        # ------------------------------------------------------------------
        # pm
        # ------------------------------------------------------------------
        Capability(
            key="pm.methodology", area="pm",
            title_key="cap.pm.methodology.title",
            summary_key="cap.pm.methodology.summary",
            tools=("pm_set_methodology",),
            example_prompts_key="cap.pm.methodology.examples",
            needs_role="owner", needs_resource="vault",
            tags=("pm", "metodik", "methodology"),
        ),
        Capability(
            key="pm.sprint_plan", area="pm",
            title_key="cap.pm.sprint_plan.title",
            summary_key="cap.pm.sprint_plan.summary",
            tools=("pm_plan_sprint",),
            example_prompts_key="cap.pm.sprint_plan.examples",
            needs_role="owner", needs_resource="vault",
            tags=("pm", "sprint", "planera", "plan"),
        ),
        Capability(
            key="pm.sprint_status", area="pm",
            title_key="cap.pm.sprint_status.title",
            summary_key="cap.pm.sprint_status.summary",
            tools=("pm_sprint_status",),
            example_prompts_key="cap.pm.sprint_status.examples",
            needs_role="reader", needs_resource="vault",
            tags=("pm", "sprint", "status", "burndown"),
        ),
        Capability(
            key="pm.raid_add", area="pm",
            title_key="cap.pm.raid_add.title",
            summary_key="cap.pm.raid_add.summary",
            tools=("pm_raid_add",),
            example_prompts_key="cap.pm.raid_add.examples",
            needs_role="collaborator", needs_resource="vault",
            tags=("pm", "risk", "raid"),
        ),
        Capability(
            key="pm.raid_view", area="pm",
            title_key="cap.pm.raid_view.title",
            summary_key="cap.pm.raid_view.summary",
            tools=("pm_raid_list",),
            example_prompts_key="cap.pm.raid_view.examples",
            needs_role="reader", needs_resource="vault",
            tags=("pm", "risk", "raid"),
        ),
        Capability(
            key="pm.report", area="pm",
            title_key="cap.pm.report.title",
            summary_key="cap.pm.report.summary",
            tools=("pm_status_report",),
            example_prompts_key="cap.pm.report.examples",
            needs_role="reader", needs_resource="vault",
            tags=("pm", "rapport", "report", "status"),
        ),
        Capability(
            key="pm.planning_engine", area="pm",
            title_key="cap.pm.planning_engine.title",
            summary_key="cap.pm.planning_engine.summary",
            tools=(
                "resource_add", "resource_list", "resource_availability", "resource_set_skill",
                "milestone_add", "task_add", "task_estimate", "task_log_actual", "dependency_add",
                "scenario_add", "scenario_list", "pm_allocate", "pm_whatif", "pm_utilization", "pm_variance",
                "plan_commit", "pm_report",
            ),
            example_prompts_key="cap.pm.planning_engine.examples",
            needs_role="reader", needs_resource="vault",
            tags=(
                "pm", "planering", "planning", "resurser", "resources", "schema", "kritisk linje",
                "critical path", "allokering", "whatif", "vad händer om",
            ),
        ),
        # ------------------------------------------------------------------
        # outbox (FEATURE-APPROVAL-OUTBOX.md)
        # ------------------------------------------------------------------
        Capability(
            key="outbox.review", area="outbox",
            title_key="cap.outbox.review.title",
            summary_key="cap.outbox.review.summary",
            tools=("outbox_list", "outbox_get", "outbox_approve", "outbox_reject"),
            example_prompts_key="cap.outbox.review.examples",
            needs_role="reader", needs_resource="vault",
            tags=("utkorg", "godkänn", "outbox", "approve"),
        ),
        # ------------------------------------------------------------------
        # undo (FEATURE-UNDO-TIMELINE.md)
        # ------------------------------------------------------------------
        Capability(
            key="undo.timeline", area="undo",
            title_key="cap.undo.timeline.title",
            summary_key="cap.undo.timeline.summary",
            tools=("timeline_list", "timeline_undo"),
            example_prompts_key="cap.undo.timeline.examples",
            needs_role="reader", needs_resource="vault",
            tags=("ångra", "historik", "undo", "timeline"),
        ),
        # ------------------------------------------------------------------
        # search (FEATURE-SEMANTIC-SEARCH.md)
        # ------------------------------------------------------------------
        Capability(
            key="search.ask", area="search",
            title_key="cap.search.ask.title",
            summary_key="cap.search.ask.summary",
            tools=("search_all", "search_reindex", "search_status"),
            example_prompts_key="cap.search.ask.examples",
            needs_role="reader", needs_resource="vault",
            tags=("sök", "fråga", "search", "rag"),
        ),
        # ------------------------------------------------------------------
        # brief (FEATURE-PROACTIVE-BRIEF.md)
        # ------------------------------------------------------------------
        Capability(
            key="brief.daily", area="brief",
            title_key="cap.brief.daily.title",
            summary_key="cap.brief.daily.summary",
            tools=("brief_configure", "brief_status", "brief_preview", "brief_send_now", "brief_data"),
            example_prompts_key="cap.brief.daily.examples",
            needs_role="reader", needs_resource=None,
            tags=("brief", "morgonbrief", "notiser", "notifications"),
        ),
        # ------------------------------------------------------------------
        # automation (FEATURE-AUTOMATION-RULES.md)
        # ------------------------------------------------------------------
        Capability(
            key="automation.rules", area="automation",
            title_key="cap.automation.rules.title",
            summary_key="cap.automation.rules.summary",
            tools=("rule_add", "rule_list", "rule_set_enabled", "rule_delete", "rule_test"),
            example_prompts_key="cap.automation.rules.examples",
            needs_role="collaborator", needs_resource="vault",
            tags=("automation", "regel", "rule", "when"),
        ),
        Capability(
            key="automation.standing", area="automation",
            title_key="cap.automation.standing.title",
            summary_key="cap.automation.standing.summary",
            tools=("standing_set", "standing_get"),
            example_prompts_key="cap.automation.standing.examples",
            needs_role="reader", needs_resource=None,
            tags=("automation", "instruktioner", "standing", "instructions"),
        ),
        # ------------------------------------------------------------------
        # contacts (FEATURE-NEXTCLOUD-BACKEND.md)
        # ------------------------------------------------------------------
        Capability(
            key="contacts.search", area="contacts",
            title_key="cap.contacts.search.title",
            summary_key="cap.contacts.search.summary",
            tools=("contacts_search", "contacts_get"),
            example_prompts_key="cap.contacts.search.examples",
            needs_role="reader", needs_resource="contacts",
            tags=("kontakt", "contact", "adressbok", "nextcloud"),
        ),
    )
