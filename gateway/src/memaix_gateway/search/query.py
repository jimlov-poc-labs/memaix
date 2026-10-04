# SPDX-License-Identifier: AGPL-3.0-or-later
"""search_all — ACL-scoped hybrid (lexical + semantic + live-mail) retrieval.

See docs/FEATURE-SEMANTIC-SEARCH.md §7. Memaix is the retrieval layer only —
this returns ranked source citations; the calling AI client formulates the
answer and is expected to cite `ref`.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np

_ROLES = ("reader", "collaborator", "owner")
_NEED_FOR_SOURCE: dict[str, str] = {
    "memory": "reader", "file": "collaborator", "backlog": "reader", "nc_file": "collaborator",
}
_SOURCE_TYPES = ("memory", "file", "backlog", "nc_file")


def _rank(role: str | None) -> int:
    if role is None:
        return -1
    try:
        return _ROLES.index(role)
    except ValueError:
        return -1


def _scoped_projects(acl, user: str, projects: list[str] | None, source_type: str) -> list[str]:
    """Projects the user may search this source_type in (role-gated)."""
    need = _NEED_FOR_SOURCE.get(source_type, "reader")
    grants = acl.grants(user)
    candidates = set(projects) if projects else set(grants.keys())
    return sorted(p for p in candidates if _rank(grants.get(p)) >= _rank(need))


def _key(item: dict) -> tuple:
    return (item["project"], item["source_type"], item["ref"])


def _cosine_topk(query_vec: list[float], candidates: list[dict], k: int) -> list[dict]:
    if not candidates:
        return []
    q = np.asarray(query_vec, dtype=np.float32)
    qn = float(np.linalg.norm(q)) or 1.0
    scored = []
    for c in candidates:
        v = c["vector"]
        vn = float(np.linalg.norm(v)) or 1.0
        score = float(np.dot(q, v) / (qn * vn))
        scored.append((score, c))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [c for _, c in scored[:k]]


def _reciprocal_rank_fusion(rank_lists: list[list[dict]], k: int = 60) -> list[dict]:
    """Merge several ranked lists into one, deduping on (project, source_type, ref)."""
    scores: dict[tuple, float] = {}
    payload: dict[tuple, dict] = {}
    for rank_list in rank_lists:
        for rank, item in enumerate(rank_list):
            key = _key(item)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            payload.setdefault(key, item)
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [{**payload[key], "score": score} for key, score in ordered]


def _apply_decay(fused: list[dict], store, decay_lambda: float) -> list[dict]:
    """Re-weight RRF scores by exponential time decay: score *= exp(-λ * days_old).

    λ=0 (default) leaves ranking identical to today. A value of ~0.005 halves
    a score after ~140 days. Configured under memaix.search.decay_lambda.
    """
    if not decay_lambda or not fused:
        return fused
    now = datetime.now(timezone.utc)
    out = []
    for item in fused:
        score = item.get("score", 0.0)
        updated_at_str = store.get_ref_updated_at(
            item["project"], item["source_type"], item["ref"]
        )
        if updated_at_str:
            try:
                updated_at = datetime.fromisoformat(updated_at_str)
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                days_old = max(0.0, (now - updated_at).total_seconds() / 86400)
                score = score * math.exp(-decay_lambda * days_old)
            except (ValueError, TypeError):
                pass
        out.append({**item, "score": round(score, 6)})
    out.sort(key=lambda x: x.get("score", 0.0), reverse=True)
    return out


def _memory_status(acl, project: str, ref: str) -> str:
    """Aktuell trapp-status för en memory-träff — slås upp vid frågetillfället
    (indexet kan vara äldre än en färsk befordran)."""
    from pathlib import Path

    from ..backends.memory_store import MemoryStore
    from ..tools.memory import note_status

    try:
        vault = acl.resource(project, "vault")
        if not vault:
            return "hypotes"
        return note_status(MemoryStore.for_vault(Path(vault)).read(ref) or "")
    except Exception:
        return "hypotes"


def _search_cfg(cfg: dict | None) -> dict:
    return ((cfg or {}).get("memaix", {}) or {}).get("search", {})


def _lexical_hits(store, scoped_by_source: dict[str, list[str]], query: str, limit: int) -> list[dict]:
    hits: list[dict] = []
    for st in _SOURCE_TYPES:
        ps = scoped_by_source[st]
        if ps:
            hits.extend(store.fts_search(ps, [st], query, limit * 3))
    return hits


def _semantic_hits(
    store, embedder, scoped_by_source: dict[str, list[str]], query: str, limit: int, max_candidates: int,
) -> list[dict]:
    query_vec = embedder.embed([query])[0]
    hits: list[dict] = []
    for st in _SOURCE_TYPES:
        ps = scoped_by_source[st]
        if not ps:
            continue
        candidates = store.candidates(ps, [st], max_candidates)
        hits.extend(_cosine_topk(query_vec, candidates, limit * 3))
    return hits


def _email_hits(acl, user: str, projects: list[str] | None, query: str, email_search) -> list[dict]:
    mail_projects = _scoped_projects(acl, user, projects, "mail")
    # email_search itself requires 'collaborator' (see tools/email.py) —
    # 'mail' isn't in _NEED_FOR_SOURCE, so gate it explicitly here.
    mail_projects = [p for p in mail_projects if _rank(acl.grants(user).get(p)) >= _rank("collaborator")]
    hits: list[dict] = []
    for project in mail_projects:
        if not acl.resource(project, "mailbox"):
            continue
        try:
            msgs = email_search(acl, user, project, query, 5)
        except Exception:
            continue
        for i, m in enumerate(msgs):
            hits.append({
                "project": project, "source_type": "email", "ref": str(m.get("id", i)),
                "title": m.get("subject", ""), "text": m.get("subject", ""),
            })
    return hits


def _to_result(acl, item: dict) -> dict:
    hit = {
        "project": item["project"],
        "source_type": item["source_type"],
        "ref": item["ref"],
        "title": item.get("title", ""),
        "snippet": (item.get("text") or "")[:200],
        "score": round(item.get("score", 0.0), 6),
    }
    # Minnestrappan: memory-träffar bär sin status så en hypotes aldrig
    # presenteras som faktum i sökresultat (SELF-IMPROVING-SYSTEM Fas B).
    if item["source_type"] == "memory":
        hit["status"] = _memory_status(acl, item["project"], item["ref"])
    return hit


def search_all(
    acl, user: str, cfg: dict | None, store, embedder,
    query: str, projects: list[str] | None = None, limit: int = 8,
    *, _email_search=None,
) -> dict:
    """Return {"results": [...], "semantic": bool, "projects_searched": [...]}.

    _email_search, if given, is a callable(acl, user, project, query, limit)
    -> list[{"id","subject","from","date"}] used to fold in live mailbox
    results for projects the user can read mail in (collaborator role, same
    as email_list). Left None by default (opt-in — no live network calls
    unless the caller wires one up).
    """
    search_cfg = _search_cfg(cfg)
    max_candidates = search_cfg.get("max_candidates", 500)

    scoped_by_source = {st: _scoped_projects(acl, user, projects, st) for st in _SOURCE_TYPES}
    all_scoped = sorted({p for ps in scoped_by_source.values() for p in ps})

    lexical_hits = _lexical_hits(store, scoped_by_source, query, limit)

    semantic_used = embedder is not None
    semantic_hits: list[dict] = []
    if semantic_used:
        semantic_hits = _semantic_hits(store, embedder, scoped_by_source, query, limit, max_candidates)

    email_hits: list[dict] = []
    if _email_search is not None:
        email_hits = _email_hits(acl, user, projects, query, _email_search)

    rank_lists = [lst for lst in (lexical_hits, semantic_hits, email_hits) if lst]
    fused = _reciprocal_rank_fusion(rank_lists) if rank_lists else []

    decay_lambda = search_cfg.get("decay_lambda", 0.0)
    if decay_lambda:
        fused = _apply_decay(fused, store, decay_lambda)

    # limit <= 0 returns one hit (the old loop appended before checking the cap).
    results = [_to_result(acl, item) for item in fused[:max(limit, 1)]]
    return {"results": results, "semantic": semantic_used, "projects_searched": all_scoped}
