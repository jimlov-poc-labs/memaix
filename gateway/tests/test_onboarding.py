# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for onboarding helpers and whoami integration."""

from __future__ import annotations

import pytest

from memaix_gateway.tools.onboarding import (
    DEFAULT_INTERVIEW,
    check_onboarding,
    complete_onboarding,
)
from memaix_gateway.tools.whoami import whoami
from memaix_gateway.acl import Acl


# ---------------------------------------------------------------------------
# check_onboarding
# ---------------------------------------------------------------------------


def test_check_onboarding_missing(tmp_path):
    """A user without any profile should need onboarding (status: missing)."""
    result = check_onboarding("newuser", tmp_path)
    assert result["needs_onboarding"] is True
    assert result["profile_status"] == "missing"
    assert result["interview_template"] is not None


def test_check_onboarding_incomplete(tmp_path):
    """A profile flagged 'profil_status: ofullständig' should need onboarding."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "om-partial.md").write_text(
        "---\nprofil_status: ofullständig\n---\nNot done yet.\n"
    )
    result = check_onboarding("partial", tmp_path)
    assert result["needs_onboarding"] is True
    assert result["profile_status"] == "incomplete"
    assert result["interview_template"] is not None


def test_check_onboarding_complete(tmp_path):
    """A profile without 'ofullständig' flag should be treated as complete."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "om-done.md").write_text(
        "---\nprofil_status: klar\n---\nFull profile here.\n"
    )
    result = check_onboarding("done", tmp_path)
    assert result["needs_onboarding"] is False
    assert result["profile_status"] == "complete"
    assert "interview_template" not in result


def test_check_uses_custom_interview_template(tmp_path):
    """Custom onboarding-interview.md in shared/ should be used when present."""
    shared = tmp_path / "shared"
    shared.mkdir()
    custom = "# Custom Interview\n1. Question A?\n2. Question B?\n"
    (shared / "onboarding-interview.md").write_text(custom)

    result = check_onboarding("newguy", tmp_path)
    assert result["interview_template"] == custom


def test_check_uses_default_template_if_missing(tmp_path):
    """When no custom template exists, DEFAULT_INTERVIEW should be returned."""
    result = check_onboarding("newguy", tmp_path)
    assert result["interview_template"] == DEFAULT_INTERVIEW


# ---------------------------------------------------------------------------
# complete_onboarding
# ---------------------------------------------------------------------------


def test_complete_onboarding_writes_file(tmp_path):
    """complete_onboarding creates shared/om-{user}.md and reports ok + profile path."""
    result = complete_onboarding("alice", tmp_path, "My name is Alice.\n")
    assert result["ok"] is True
    assert result["profile"] == "shared/om-alice.md"
    profile_path = tmp_path / "shared" / "om-alice.md"
    assert profile_path.exists()
    content = profile_path.read_text()
    assert "My name is Alice." in content
    assert "profil_status: klar" in content


def test_complete_onboarding_preserves_existing_frontmatter(tmp_path):
    """If content already has frontmatter, no extra frontmatter is prepended."""
    profile_content = "---\nprofil_status: klar\nname: Alice\n---\nBio text.\n"
    complete_onboarding("alice", tmp_path, profile_content)
    written = (tmp_path / "shared" / "om-alice.md").read_text()
    # Should not double-up on frontmatter
    assert written.count("---") == 2


# ---------------------------------------------------------------------------
# whoami integration
# ---------------------------------------------------------------------------


def test_whoami_includes_onboarding_state(tmp_path):
    """whoami with vault= includes needs_onboarding in the result."""
    acl = Acl(
        users={"alice": {"grants": {"myproject": "owner"}}},
        projects={"myproject": {"vault": str(tmp_path)}},
    )
    result = whoami(acl, "alice", vault=tmp_path)
    assert "needs_onboarding" in result
    assert result["needs_onboarding"] is True  # no profile yet
    assert result["user_id"] == "alice"


# ---------------------------------------------------------------------------
# per-user flag
# ---------------------------------------------------------------------------


def test_flag_is_per_user(tmp_path):
    """One user finishing onboarding must not mark another user as done."""
    complete_onboarding("anna", tmp_path, "Profil för Anna")
    assert check_onboarding("anna", tmp_path)["needs_onboarding"] is False
    other = check_onboarding("bertil", tmp_path)
    assert other["needs_onboarding"] is True
    assert other["profile_status"] == "missing"


def test_second_user_keeps_first_users_flag(tmp_path):
    import json

    complete_onboarding("anna", tmp_path, "A")
    complete_onboarding("bertil", tmp_path, "B")
    flags = json.loads((tmp_path / "_system" / "onboarding.json").read_text())
    assert flags["schema"] == 2
    assert set(flags["users"]) == {"anna", "bertil"}
    assert check_onboarding("anna", tmp_path)["needs_onboarding"] is False


def test_legacy_global_flag_only_counts_for_its_user(tmp_path):
    import json

    (tmp_path / "_system").mkdir()
    (tmp_path / "_system" / "onboarding.json").write_text(
        json.dumps({"schema": 1, "user_id": "anna", "onboarded": True})
    )
    assert check_onboarding("anna", tmp_path)["needs_onboarding"] is False
    assert check_onboarding("bertil", tmp_path)["needs_onboarding"] is True
    complete_onboarding("bertil", tmp_path, "B")
    assert check_onboarding("anna", tmp_path)["needs_onboarding"] is False


def test_incomplete_marker_overrides_flag(tmp_path):
    complete_onboarding("anna", tmp_path, "klar")
    (tmp_path / "shared" / "om-anna.md").write_text("---\nprofil_status: ofullständig\n---\n")
    assert check_onboarding("anna", tmp_path)["profile_status"] == "incomplete"
