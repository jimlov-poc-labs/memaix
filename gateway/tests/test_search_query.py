# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for search.query.search_all — ACL scoping, hybrid ranking, fusion."""

from __future__ import annotations

import pytest

from memaix_gateway.acl import Acl
from memaix_gateway.search.embedder import FakeEmbedder
from memaix_gateway.search.index import index_upsert
from memaix_gateway.search.query import search_all
from memaix_gateway.search.store import EmbeddingStore


@pytest.fixture()
def store(tmp_path):
    return EmbeddingStore.for_path(tmp_path / "index.db")


@pytest.fixture()
def embedder():
    return FakeEmbedder(dim=64)


@pytest.fixture()
def acl():
    return Acl(
        users={
            "alice": {"grants": {"proj": "owner"}},
            "bob": {"grants": {"proj": "reader"}},
        },
        projects={
            "proj": {"vault": "/v", "mailbox": {"host": "x"}},
            "other": {"vault": "/v2"},
        },
    )


def test_lexical_search_finds_matching_note(store, acl):
    index_upsert(store, None, "proj", "memory", "note.md", "note.md", "the invoice is overdue")
    result = search_all(acl, "alice", None, store, None, "invoice")
    assert result["semantic"] is False
    assert len(result["results"]) == 1
    assert result["results"][0]["ref"] == "note.md"


def test_semantic_search_used_when_embedder_present(store, embedder, acl):
    index_upsert(store, embedder, "proj", "memory", "note.md", "note.md", "invoice payment overdue")
    result = search_all(acl, "alice", None, store, embedder, "late payment on invoice")
    assert result["semantic"] is True
    assert len(result["results"]) >= 1


def test_reader_cannot_search_files_but_can_search_memory(store, embedder, acl):
    index_upsert(store, embedder, "proj", "file", "secret.txt", "secret.txt", "confidential content")
    index_upsert(store, embedder, "proj", "memory", "note.md", "note.md", "confidential content")
    result = search_all(acl, "bob", None, store, embedder, "confidential")
    refs = {(r["source_type"], r["ref"]) for r in result["results"]}
    assert ("file", "secret.txt") not in refs
    assert ("memory", "note.md") in refs


def test_project_outside_visible_is_filtered(store, embedder, acl):
    index_upsert(store, embedder, "other", "memory", "x.md", "x.md", "budget report")
    result = search_all(acl, "alice", None, store, embedder, "budget", projects=["proj"])
    assert result["results"] == []  # only 'proj' was requested, and it has no content
    assert "other" not in result["projects_searched"]


def test_projects_param_filters_within_visible_set(store, embedder, acl):
    index_upsert(store, embedder, "proj", "memory", "a.md", "a.md", "roadmap notes")
    result = search_all(acl, "alice", None, store, embedder, "roadmap")
    assert "proj" in result["projects_searched"]


def test_no_embedder_falls_back_to_lexical_only(store, acl):
    index_upsert(store, None, "proj", "memory", "note.md", "note.md", "quarterly roadmap plan")
    result = search_all(acl, "alice", None, store, None, "roadmap")
    assert result["semantic"] is False
    assert len(result["results"]) == 1


def test_rrf_dedupes_hits_present_in_both_lexical_and_semantic(store, embedder, acl):
    index_upsert(store, embedder, "proj", "memory", "note.md", "note.md", "invoice overdue payment")
    result = search_all(acl, "alice", None, store, embedder, "invoice")
    refs = [r["ref"] for r in result["results"]]
    assert refs.count("note.md") == 1  # not duplicated across lexical+semantic


def test_email_search_folds_into_results_when_injected(store, embedder, acl):
    def fake_email_search(acl_, user, project, query, limit):
        return [{"id": "42", "subject": "Invoice reminder", "from": "x@y.com", "date": "2026-01-01"}]

    result = search_all(
        acl, "alice", None, store, embedder, "invoice", _email_search=fake_email_search
    )
    email_hits = [r for r in result["results"] if r["source_type"] == "email"]
    assert len(email_hits) == 1
    assert email_hits[0]["ref"] == "42"


def test_email_search_skipped_for_project_without_mailbox(store, embedder):
    acl = Acl(
        users={"alice": {"grants": {"nomail": "owner"}}},
        projects={"nomail": {"vault": "/v"}},
    )
    calls = []

    def fake_email_search(acl_, user, project, query, limit):
        calls.append(project)
        return []

    search_all(acl, "alice", None, store, embedder, "x", _email_search=fake_email_search)
    assert calls == []


def test_email_search_not_available_to_reader(store, embedder, acl):
    calls = []

    def fake_email_search(acl_, user, project, query, limit):
        calls.append(project)
        return []

    search_all(acl, "bob", None, store, embedder, "x", _email_search=fake_email_search)
    assert calls == []  # bob is only a reader; email_search needs collaborator


def test_max_candidates_from_cfg_is_respected(store, embedder, acl):
    for i in range(5):
        index_upsert(store, embedder, "proj", "memory", f"n{i}.md", f"n{i}.md", f"content number {i}")
    cfg = {"memaix": {"search": {"max_candidates": 2}}}
    result = search_all(acl, "alice", cfg, store, embedder, "content", limit=10)
    # Can't directly observe candidate cap from results shape, but this
    # should not error and should still return results.
    assert isinstance(result["results"], list)


def test_memory_hits_carry_ladder_status(tmp_path, store, acl):
    """Minnestrappan i sök: memory-träffar bär status, uppslagen vid
    frågetillfället — en hypotes kan aldrig se ut som faktum (Fas B)."""
    from memaix_gateway.acl import Acl
    from memaix_gateway.backends.memory_store import MemoryStore
    from memaix_gateway.tools.memory import memory_write

    vault = tmp_path / "vault"
    vault.mkdir()
    MemoryStore._clear_instances()
    real_acl = Acl(
        users={"alice": {"grants": {"proj": "owner"}}},
        projects={"proj": {"vault": str(vault)}},
    )
    memory_write(real_acl, "alice", "proj", "obekraftad.md", "kunden gillar blått kanske")
    memory_write(real_acl, "alice", "proj", "bekraftad.md", "kunden gillar blått bevisligen",
                 status="verifierad")
    index_upsert(store, None, "proj", "memory", "obekraftad.md", "obekraftad.md",
                 "kunden gillar blått kanske")
    index_upsert(store, None, "proj", "memory", "bekraftad.md", "bekraftad.md",
                 "kunden gillar blått bevisligen")
    index_upsert(store, None, "proj", "file", "f.txt", "f.txt", "blått dokument")

    result = search_all(real_acl, "alice", None, store, None, "blått")
    by_ref = {r["ref"]: r for r in result["results"]}
    assert by_ref["obekraftad.md"]["status"] == "hypotes"
    assert by_ref["bekraftad.md"]["status"] == "verifierad"
    assert "status" not in by_ref["f.txt"], "bara memory-träffar bär trapp-status"


# ---------------------------------------------------------------------------
# Karakteriseringstester (Sonar S3776-saneringen av search_all): låser dagens
# beteende — behörighetsfiltrering, rollgrindar, decay, mejl-fel, limit.
# ---------------------------------------------------------------------------

import math  # noqa: E402
import sqlite3  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from memaix_gateway.search import query as q_mod  # noqa: E402


def _multi_acl():
    return Acl(
        users={
            "alice": {"grants": {"proj": "owner"}},
            "carol": {"grants": {"proj": "collaborator", "other": "reader"}},
            "bob": {"grants": {"proj": "reader"}},
            "eve": {"grants": {"proj": "superuser"}},
        },
        projects={
            "proj": {"vault": "/va", "mailbox": {"host": "x"}},
            "other": {"vault": "/vb", "mailbox": {"host": "y"}},
            "secret": {"vault": "/vs"},
        },
    )


def _seed_three_projects(store):
    for project in ("proj", "other", "secret"):
        index_upsert(store, None, project, "memory", f"{project}.md", f"{project}.md", "budget forecast")


def test_search_only_returns_projects_user_has_grants_in(store):
    acl = _multi_acl()
    _seed_three_projects(store)
    result = search_all(acl, "alice", None, store, None, "budget")
    assert [r["project"] for r in result["results"]] == ["proj"]
    assert result["projects_searched"] == ["proj"]


def test_requesting_ungranted_project_returns_nothing_and_is_not_searched(store):
    """Behörighet: att be om ett projekt man saknar åtkomst till ger varken
    träffar eller en post i projects_searched."""
    acl = _multi_acl()
    _seed_three_projects(store)
    result = search_all(acl, "alice", None, store, None, "budget", projects=["secret"])
    assert result["results"] == []
    assert result["projects_searched"] == []

    mixed = search_all(acl, "alice", None, store, None, "budget", projects=["proj", "secret", "other"])
    assert [r["project"] for r in mixed["results"]] == ["proj"]
    assert mixed["projects_searched"] == ["proj"]


def test_unknown_user_gets_nothing(store):
    acl = _multi_acl()
    _seed_three_projects(store)
    result = search_all(acl, "mallory", None, store, None, "budget")
    assert result == {"results": [], "semantic": False, "projects_searched": []}


def test_unknown_role_name_grants_no_access(store):
    acl = _multi_acl()
    _seed_three_projects(store)
    result = search_all(acl, "eve", None, store, None, "budget")
    assert result["results"] == []
    assert result["projects_searched"] == []


def test_ungranted_project_is_never_queried_in_store(store):
    """Projektfiltret ska verkligen nå lagret: store får bara se tillåtna projekt."""
    acl = _multi_acl()
    seen: list[tuple] = []
    real_fts, real_cand = store.fts_search, store.candidates

    def spy_fts(projects, source_types, query, limit):
        seen.append(("fts", tuple(projects), tuple(source_types), limit))
        return real_fts(projects, source_types, query, limit)

    def spy_cand(projects, source_types, limit):
        seen.append(("cand", tuple(projects), tuple(source_types), limit))
        return real_cand(projects, source_types, limit)

    store.fts_search, store.candidates = spy_fts, spy_cand
    search_all(acl, "carol", {"memaix": {"search": {"max_candidates": 7}}}, store, FakeEmbedder(dim=16),
               "budget", projects=["proj", "other", "secret"], limit=4)
    # carol: proj=collaborator (alla fyra källor), other=reader (memory+backlog), secret=ingen
    assert ("fts", ("proj",), ("file",), 12) in seen
    assert ("fts", ("proj",), ("nc_file",), 12) in seen
    assert ("fts", ("other", "proj"), ("memory",), 12) in seen
    assert ("fts", ("other", "proj"), ("backlog",), 12) in seen
    assert ("cand", ("other", "proj"), ("memory",), 7) in seen
    assert ("cand", ("proj",), ("file",), 7) in seen
    assert all("secret" not in entry[1] for entry in seen)
    assert len([e for e in seen if e[0] == "fts"]) == 4
    assert len([e for e in seen if e[0] == "cand"]) == 4


def test_role_gates_per_source_type(store):
    acl = _multi_acl()
    for st in ("memory", "backlog", "file", "nc_file"):
        index_upsert(store, None, "proj", st, f"{st}.md", f"{st}.md", "gate keyword")
    reader = search_all(acl, "bob", None, store, None, "gate")
    assert {r["source_type"] for r in reader["results"]} == {"memory", "backlog"}
    collab = search_all(acl, "carol", None, store, None, "gate")
    assert {r["source_type"] for r in collab["results"]} == {"memory", "backlog", "file", "nc_file"}
    assert collab["projects_searched"] == ["other", "proj"]


def test_semantic_candidates_skipped_for_source_without_scope(store):
    """Reader har ingen file/nc_file-räckvidd: inga kandidater hämtas för dem."""
    acl = _multi_acl()
    calls = []
    real = store.candidates
    store.candidates = lambda ps, sts, lim: (calls.append(tuple(sts)), real(ps, sts, lim))[1]
    search_all(acl, "bob", None, store, FakeEmbedder(dim=16), "x")
    assert calls == [("memory",), ("backlog",)]


def test_default_max_candidates_is_500_and_cfg_none_values_tolerated(store):
    acl = _multi_acl()
    limits = []
    real = store.candidates
    store.candidates = lambda ps, sts, lim: (limits.append(lim), real(ps, sts, lim))[1]
    search_all(acl, "bob", None, store, FakeEmbedder(dim=16), "x")
    search_all(acl, "bob", {"memaix": None}, store, FakeEmbedder(dim=16), "x")
    search_all(acl, "bob", {"memaix": {"search": {}}}, store, FakeEmbedder(dim=16), "x")
    assert limits == [500] * 6


def test_semantic_only_hit_without_lexical_match(store, embedder, acl):
    index_upsert(store, embedder, "proj", "memory", "n.md", "n.md", "alpha beta gamma")
    result = search_all(acl, "alice", None, store, embedder, "zzzunmatchedzzz")
    assert result["semantic"] is True
    assert [r["ref"] for r in result["results"]] == ["n.md"]


def test_no_hits_anywhere_gives_empty_results(store, acl):
    result = search_all(acl, "alice", None, store, None, "nothing")
    assert result == {"results": [], "semantic": False, "projects_searched": ["proj"]}


def test_result_shape_snippet_truncation_and_score_rounding(store, acl):
    long_text = "needle " + "x" * 400
    index_upsert(store, None, "proj", "backlog", "b1", "Rubrik", long_text)
    (hit,) = search_all(acl, "alice", None, store, None, "needle")["results"]
    assert set(hit) == {"project", "source_type", "ref", "title", "snippet", "score"}
    assert hit["title"] == "Rubrik"
    assert hit["snippet"] == long_text[:200]
    assert hit["score"] == round(1 / 61, 6)


def test_limit_caps_results(store, acl):
    for i in range(4):
        index_upsert(store, None, "proj", "memory", f"n{i}.md", f"n{i}.md", "common word")
    assert len(search_all(acl, "alice", None, store, None, "common", limit=10)["results"]) == 4
    assert len(search_all(acl, "alice", None, store, None, "common", limit=2)["results"]) == 2
    assert len(search_all(acl, "alice", None, store, None, "common", limit=1)["results"]) == 1


def test_item_in_both_lexical_and_semantic_outranks_single_list_item(store, embedder, acl):
    index_upsert(store, embedder, "proj", "memory", "both.md", "both.md", "invoice overdue")
    result = search_all(acl, "alice", None, store, embedder, "invoice overdue")
    assert result["results"][0]["ref"] == "both.md"
    assert result["results"][0]["score"] == round(2 / 61, 6)


# --- decay -------------------------------------------------------------------

def _age(store, ref, value):
    conn = sqlite3.connect(str(store._path))
    conn.execute("UPDATE chunks SET updated_at=? WHERE ref=?", (value, ref))
    conn.commit()
    conn.close()


def test_decay_zero_or_missing_leaves_scores_untouched(store, acl):
    index_upsert(store, None, "proj", "memory", "n.md", "n.md", "decay word")
    calls = []
    real = store.get_ref_updated_at
    store.get_ref_updated_at = lambda *a: (calls.append(a), real(*a))[1]
    for cfg in (None, {"memaix": {"search": {"decay_lambda": 0.0}}}):
        (hit,) = search_all(acl, "alice", cfg, store, None, "decay")["results"]
        assert hit["score"] == round(1 / 61, 6)
    assert calls == []


def test_decay_reorders_by_age_and_handles_naive_and_garbage_timestamps(store, acl):
    for ref in ("old.md", "new.md", "naive.md", "garbage.md"):
        index_upsert(store, None, "proj", "memory", ref, ref, "decay word")
    now = datetime.now(timezone.utc)
    _age(store, "old.md", (now - timedelta(days=400)).isoformat())
    _age(store, "new.md", now.isoformat())
    _age(store, "naive.md", (now - timedelta(days=100)).replace(tzinfo=None).isoformat())
    _age(store, "garbage.md", "inte-ett-datum")
    base = {r["ref"]: r["score"] for r in search_all(acl, "alice", None, store, None, "decay", limit=10)["results"]}
    cfg = {"memaix": {"search": {"decay_lambda": 0.01}}}
    result = search_all(acl, "alice", cfg, store, None, "decay", limit=10)
    scores = {r["ref"]: r["score"] for r in result["results"]}
    assert [r["ref"] for r in result["results"]][-1] == "old.md"
    assert scores["old.md"] == pytest.approx(base["old.md"] * math.exp(-0.01 * 400), abs=2e-6)
    assert scores["naive.md"] == pytest.approx(base["naive.md"] * math.exp(-0.01 * 100), abs=2e-6)
    assert scores["new.md"] == pytest.approx(base["new.md"], abs=2e-6)
    assert scores["garbage.md"] == pytest.approx(base["garbage.md"], abs=2e-6)  # ogiltigt datum: ingen decay


def test_decay_leaves_items_without_timestamp_untouched(store, acl):
    index_upsert(store, None, "proj", "memory", "n.md", "n.md", "decay word")
    store.get_ref_updated_at = lambda *a: None
    cfg = {"memaix": {"search": {"decay_lambda": 0.5}}}
    (hit,) = search_all(acl, "alice", cfg, store, None, "decay")["results"]
    assert hit["score"] == round(1 / 61, 6)


# --- live mejl -----------------------------------------------------------------

def test_email_hits_shape_and_fallback_ref(store):
    acl = _multi_acl()
    seen = []

    def email_search(acl_, user, project, query, limit):
        seen.append((project, query, limit))
        return [{"id": 7, "subject": "Hej"}, {"subject": "Utan id"}, {}]

    result = search_all(acl, "alice", None, store, None, "q", _email_search=email_search, limit=10)
    assert seen == [("proj", "q", 5)]
    by_ref = {r["ref"]: r for r in result["results"]}
    assert set(by_ref) == {"7", "1", "2"}
    assert by_ref["7"]["title"] == "Hej" and by_ref["7"]["snippet"] == "Hej"
    assert by_ref["7"]["source_type"] == "email" and by_ref["7"]["project"] == "proj"
    assert by_ref["2"]["title"] == "" and by_ref["2"]["snippet"] == ""
    assert "status" not in by_ref["7"]


def test_email_search_failure_is_swallowed_and_next_project_still_searched(store):
    acl = Acl(
        users={"alice": {"grants": {"a": "owner", "b": "owner"}}},
        projects={"a": {"vault": "/v", "mailbox": {"h": 1}}, "b": {"vault": "/v", "mailbox": {"h": 1}}},
    )

    def email_search(acl_, user, project, query, limit):
        if project == "a":
            raise RuntimeError("imap down")
        return [{"id": "m1", "subject": "S"}]

    result = search_all(acl, "alice", None, store, None, "q", _email_search=email_search)
    assert [(r["project"], r["ref"]) for r in result["results"]] == [("b", "m1")]


def test_email_search_requires_collaborator_and_respects_projects_filter(store):
    acl = _multi_acl()
    calls = []

    def email_search(acl_, user, project, query, limit):
        calls.append(project)
        return []

    search_all(acl, "carol", None, store, None, "q", _email_search=email_search)
    assert calls == ["proj"]  # other: bara reader -> ingen mejlsökning
    calls.clear()
    search_all(acl, "alice", None, store, None, "q", projects=["secret"], _email_search=email_search)
    assert calls == []  # ingen åtkomst till secret
    search_all(acl, "bob", None, store, None, "q", _email_search=email_search)
    assert calls == []


# --- memory-status -------------------------------------------------------------

def test_memory_status_defaults_to_hypotes_without_vault_or_on_error(store, monkeypatch):
    acl = Acl(users={"alice": {"grants": {"nov": "owner", "proj": "owner"}}},
              projects={"nov": {}, "proj": {"vault": "/finns/inte"}})
    index_upsert(store, None, "nov", "memory", "a.md", "a.md", "statusword")
    index_upsert(store, None, "proj", "memory", "b.md", "b.md", "statusword")
    result = search_all(acl, "alice", None, store, None, "statusword")
    assert {r["ref"]: r["status"] for r in result["results"]} == {"a.md": "hypotes", "b.md": "hypotes"}

    from memaix_gateway.backends.memory_store import MemoryStore

    def boom(_vault):
        raise OSError("disk")

    monkeypatch.setattr(MemoryStore, "for_vault", staticmethod(boom))
    result = search_all(acl, "alice", None, store, None, "statusword")
    assert all(r["status"] == "hypotes" for r in result["results"])


def test_rank_helper_edge_cases():
    assert q_mod._rank(None) == -1
    assert q_mod._rank("nonsense") == -1
    assert q_mod._rank("reader") < q_mod._rank("collaborator") < q_mod._rank("owner")


def test_decay_with_no_hits_returns_empty(store, acl):
    cfg = {"memaix": {"search": {"decay_lambda": 0.1}}}
    result = search_all(acl, "alice", cfg, store, None, "nothing")
    assert result["results"] == []


def test_nonpositive_limit_quirks(store, acl):
    """Kvirk som låses (ändra inte): limit=0 ger inga lexikala träffar (LIMIT 0),
    limit<0 ger obegränsad FTS men loopen lade till träffen före limit-kollen,
    så exakt en träff returneras."""
    for i in range(3):
        index_upsert(store, None, "proj", "memory", f"n{i}.md", f"n{i}.md", "common word")
    assert search_all(acl, "alice", None, store, None, "common", limit=0)["results"] == []
    assert len(search_all(acl, "alice", None, store, None, "common", limit=-5)["results"]) == 1
