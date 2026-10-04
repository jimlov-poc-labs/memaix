# SPDX-License-Identifier: AGPL-3.0-or-later
"""evaluate() — match an event against rules and run their actions.

See docs/FEATURE-AUTOMATION-RULES.md §5. Idempotent via RulesStore.try_reserve:
a rule that already ran for a given event_key is skipped on a repeat
evaluation (mail-poll overlap, retry, ...) — dry_run never reserves/logs.
"""

from __future__ import annotations

from .actions import run_action
from .match import conditions_pass, trigger_matches


def _run_rule(acl, rule: dict, event: dict, tools, dry_run: bool) -> dict:
    """Kör en regels åtgärder; fel i en åtgärd stoppar inte de övriga."""
    action_results = [
        run_action(
            acl, rule["memaix_user"], action, event.get("payload", {}),
            tools=tools, dry_run=dry_run,
        )
        for action in rule["actions"]
    ]
    return {"results": action_results, "ok": all(r.get("ok") for r in action_results)}


def evaluate(store, acl, event: dict, *, tools: dict | None = None, dry_run: bool = False) -> list[dict]:
    """event = {"type", "project", "id", "payload"}.

    Returns one entry per MATCHING rule: {"rule_id", "rule_name", "ok",
    "actions": [...]}. A single failing action doesn't stop the rule's other
    actions, and one rule's failure doesn't stop the next rule from running.
    """
    project = event.get("project")
    candidates = store.list_rules([project] if project else None, enabled_only=True)
    results: list[dict] = []

    for rule in candidates:
        if not trigger_matches(rule["trigger"], event):
            continue
        if not conditions_pass(rule["conditions"], event.get("payload", {})):
            continue

        event_key = str(event.get("id", ""))
        if not dry_run and not store.try_reserve(rule["id"], event_key):
            continue  # already handled this exact event

        outcome = _run_rule(acl, rule, event, tools, dry_run)
        action_results = outcome["results"]
        if not dry_run:
            detail = "; ".join(r.get("error", "") for r in action_results if not r.get("ok"))
            store.record_run_detail(rule["id"], event_key, outcome["ok"], detail)

        results.append(
            {"rule_id": rule["id"], "rule_name": rule["name"], "ok": outcome["ok"], "actions": action_results}
        )

    return results
