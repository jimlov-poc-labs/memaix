# SPDX-License-Identifier: AGPL-3.0-or-later
"""Onboarding helpers — check whether a user has a completed profile and scaffold one."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .. import gitvault

DEFAULT_QUESTIONS = [
    "Vad heter du och vad är din roll?",
    "Vilka projekt jobbar du med, och vad är ditt primära ansvar?",
    "Vad är dina styrkor ur ett projektperspektiv?",
    "Var vill du att assistenten tar arbete av dig?",
    "Hur vill du jobba — tempo, beslutsstil, när ska jag avbryta vs jobba självständigt?",
    "Preferenser och ogillanden kring text, ton och format?",
    "Vad är dina mål just nu?",
]

DEFAULT_INTERVIEW = (
    "# Onboarding-intervju\n"
    "Hej! Jag behöver lära känna dig för att hjälpa dig bättre.\n"
    + "\n".join(f"{i+1}. {q}" for i, q in enumerate(DEFAULT_QUESTIONS))
)

_FLAG_PATH = "_system/onboarding.json"
_INCOMPLETE_TOKEN = "profil_status: ofullständig"  # nosec B105 -- profile-status marker, not a credential


def _read_flags(vault: Path) -> dict:
    """Per-user onboarding flags. Schema 1 held one global user; it is read as that user's entry."""
    try:
        raw = json.loads((vault / _FLAG_PATH).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    if isinstance(raw.get("users"), dict):
        return {u: f for u, f in raw["users"].items() if isinstance(f, dict)}
    if raw.get("onboarded") is True and isinstance(raw.get("user_id"), str):
        return {raw["user_id"]: raw}
    return {}


def _interview_template(vault: Path, cfg: dict | None = None) -> str:
    custom = vault / "shared" / "onboarding-interview.md"
    if custom.exists():
        return custom.read_text()
    questions = (cfg or {}).get("memaix", {}).get("onboarding", {}).get("questions")
    if questions:
        intro = (cfg or {}).get("memaix", {}).get("onboarding", {}).get(
            "intro", "Hej! Jag behöver lära känna dig för att hjälpa dig bättre."
        )
        return "# Onboarding-intervju\n" + intro + "\n" + "\n".join(
            f"{i+1}. {q}" for i, q in enumerate(questions)
        )
    return DEFAULT_INTERVIEW


def build_interview_prompt(user_id: str, vault: Path | None, cfg: dict | None = None) -> str:
    """Return the full prompt text for the onboarding_interview MCP prompt."""
    template = _interview_template(vault, cfg) if vault else DEFAULT_INTERVIEW
    outro = (cfg or {}).get("memaix", {}).get("onboarding", {}).get(
        "outro", "Tack! Sammanställ nu en kondenserad profil (löptext + punkter, inte råa svar) och anropa verktyget `onboarding_complete` med texten som `profile_content`. Hoppa inte över det steget — det är det som markerar onboarding som klar."  # noqa: E501
    )
    from .whoami import MEMORY_RULES

    return (
        f"{template}\n\n"
        f"Fråga en fråga i taget. När alla är besvarade:\n{outro}\n\n"
        f"Från och med nu gäller även: {MEMORY_RULES}"
    )


def check_onboarding(user_id: str, vault: Path, cfg: dict | None = None) -> dict:
    """Return onboarding status for user_id.

    Returns a dict with keys:
      - needs_onboarding: bool
      - profile_status: 'missing' | 'incomplete' | 'complete'
      - interview_template: str | None  (present when needs_onboarding is True)
      - onboarding_action: str | None   (present when needs_onboarding is True)
    """
    if (cfg or {}).get("memaix", {}).get("onboarding", {}).get("enabled") is False:
        return {"needs_onboarding": False, "profile_status": "complete"}

    # Fast path: authoritative per-user JSON flag.
    if _read_flags(vault).get(user_id, {}).get("onboarded") is True:
        # Let explicit markdown re-trigger override the flag.
        profile_path = vault / "shared" / f"om-{user_id}.md"
        if not (profile_path.exists() and _INCOMPLETE_TOKEN in profile_path.read_text()):
            return {"needs_onboarding": False, "profile_status": "complete"}

    profile_path = vault / "shared" / f"om-{user_id}.md"
    if not profile_path.exists():
        return {
            "needs_onboarding": True,
            "profile_status": "missing",
            "interview_template": _interview_template(vault, cfg),
            "onboarding_action": (
                "Den här användaren har ingen profil ännu. Erbjud en kort onboarding-intervju "
                "innan annat arbete påbörjas. Använd MCP-prompten 'onboarding_interview', eller "
                "kör frågorna i 'interview_template' en i taget, och anropa sedan verktyget "
                "'onboarding_complete' med den sammanställda profilen."
            ),
        }

    content = profile_path.read_text()
    if _INCOMPLETE_TOKEN in content:
        return {
            "needs_onboarding": True,
            "profile_status": "incomplete",
            "interview_template": _interview_template(vault, cfg),
            "onboarding_action": (
                "Onboarding-intervjun påbörjades men avbröts. Fortsätt från där ni slutade "
                "och anropa 'onboarding_complete' när profilen är klar."
            ),
        }

    return {"needs_onboarding": False, "profile_status": "complete"}


def _git_commit(vault: Path, message: str) -> None:
    """Commit the onboarding write. Returns nothing, but no longer hides
    anything either -- the two unchecked subprocess calls this replaces
    reported neither success nor failure to anyone.

    `add -A` is preserved: onboarding writes a profile and a flag file in
    different directories and has always swept the whole tree. Named
    explicitly now so the sweep is visible at the call rather than implied.
    """
    if not gitvault.is_repo(vault):
        return
    gitvault.commit(vault, [], message, all_tracked=True)


_DEFAULT_TOUR_KEYS = ("memory.remember", "mail.triage", "backlog.capture", "brief.daily")


def build_tour(user_id: str, profile_text: str, available: list, t, max_items: int = 4) -> dict:
    """Rank `available` capabilities against words in `profile_text` (role,
    responsibilities, goals) via Capability.tags and return a short, localized
    "want to try this?" tour. Falls back to a generic starter set when nothing
    in the profile matches. See docs/FEATURE-DISCOVERABILITY.md §5.
    """
    text_lower = (profile_text or "").lower()
    scored = sorted(
        (cap for cap in available if any(tag.lower() in text_lower for tag in cap.tags)),
        key=lambda cap: sum(1 for tag in cap.tags if tag.lower() in text_lower),
        reverse=True,
    )

    if not scored:
        by_key = {cap.key: cap for cap in available}
        scored = [by_key[k] for k in _DEFAULT_TOUR_KEYS if k in by_key]

    chosen = list(scored[:max_items])
    if len(chosen) < max_items:
        chosen_keys = {cap.key for cap in chosen}
        for cap in available:
            if len(chosen) >= max_items:
                break
            if cap.key not in chosen_keys:
                chosen.append(cap)
                chosen_keys.add(cap.key)

    suggestions = []
    for cap in chosen:
        examples = t(cap.example_prompts_key)
        example = examples[0] if isinstance(examples, list) and examples else ""
        suggestions.append(
            {
                "capability_key": cap.key,
                "title": t(cap.title_key),
                "why": t(cap.summary_key),
                "example": example,
            }
        )

    return {
        "greeting": t("tour.greeting"),
        "suggestions": suggestions,
        "areas": sorted({cap.area for cap in available}),
    }


def complete_onboarding(user_id: str, vault: Path, profile_content: str) -> dict:
    """Write profile + completion flag and git-commit both. Returns {'ok': True}."""
    shared = vault / "shared"
    shared.mkdir(parents=True, exist_ok=True)

    if not profile_content.lstrip().startswith("---"):
        today = datetime.now(timezone.utc).date().isoformat()
        profile_content = f"---\nprofil_status: klar\nuppdaterad: {today}\n---\n{profile_content}"
    else:
        # Replace ofullständig → klar if present.
        profile_content = profile_content.replace(_INCOMPLETE_TOKEN, "profil_status: klar")

    profile_path = shared / f"om-{user_id}.md"
    profile_path.write_text(profile_content)

    # Write the authoritative flag last (crash-safe ordering).
    system_dir = vault / "_system"
    system_dir.mkdir(parents=True, exist_ok=True)
    users = _read_flags(vault)
    users[user_id] = {
        "onboarded": True,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "interview_version": 1,
    }
    (system_dir / "onboarding.json").write_text(
        json.dumps({"schema": 2, "users": users}, indent=2)
    )

    _git_commit(vault, f"onboarding: profile for {user_id}")

    return {"ok": True, "profile": f"shared/om-{user_id}.md"}
