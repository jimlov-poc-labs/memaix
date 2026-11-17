# SPDX-License-Identifier: AGPL-3.0-or-later
"""Revert/index sync, atomic read-modify-write and concurrent reads for MemoryStore."""

from __future__ import annotations

import threading

import pytest

from memaix_gateway import gitvault
from memaix_gateway.acl import Acl
from memaix_gateway.backends.memory_store import IndexSyncError, MemoryStore
from memaix_gateway.tools.memory import memory_set_status


@pytest.fixture()
def vault(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    return v


@pytest.fixture()
def store(vault):
    MemoryStore._clear_instances()
    return MemoryStore.for_vault(vault)


def _head(store):
    return store.history()[0]["hash"]


def test_revert_resyncs_read_and_search(store, vault):
    store.write("rv.md", "original zebra", "a")
    store.write("rv.md", "modified giraffe", "a")
    store.revert(_head(store))
    assert store.read("rv.md") == "original zebra"
    assert store.search("zebra") and not store.search("giraffe")
    # A fresh instance (e.g. after restart) sees the same.
    MemoryStore._clear_instances()
    fresh = MemoryStore(vault)
    assert fresh.read("rv.md") == "original zebra"
    assert not fresh.search("giraffe")


def test_revert_of_creation_removes_note(store):
    store.write("gone.md", "ephemeral walrus", "a")
    store.revert(_head(store))
    assert store.read("gone.md") is None
    assert "gone.md" not in store.list_all()
    assert store.search("walrus") == []


def test_revert_conflict_raises_without_half_state(store, vault):
    store.write("c.md", "one", "a")
    first = _head(store)
    store.write("c.md", "two", "a")
    store.write("c.md", "three", "a")
    with pytest.raises(gitvault.GitError):
        store.revert(first)  # conflicts with later edits
    assert store.read("c.md") == "three"
    assert (vault / "memory" / "c.md").read_text() == "three"
    assert not (vault / ".git" / "REVERT_HEAD").exists()
    assert gitvault.run(vault, ["status", "--porcelain"]).stdout.strip() == ""
    store.write("c.md", "four", "a")  # store still usable


def test_concurrent_append_loses_nothing(store):
    for attempt in range(30):
        path = f"ap{attempt}.md"
        store.write(path, "base", "a")
        barrier = threading.Barrier(2)

        def work(tag: str) -> None:
            barrier.wait()
            store.append(path, tag, "a")

        ts = [threading.Thread(target=work, args=(t,)) for t in ("x-line", "y-line")]
        [t.start() for t in ts]
        [t.join() for t in ts]
        lines = store.read(path).split("\n")
        assert "x-line" in lines and "y-line" in lines and "base" in lines, (attempt, lines)


def test_set_status_not_lost_under_concurrent_append(store, vault):
    acl = Acl(
        users={"carol": {"grants": {"proj": "collaborator"}}},
        projects={"proj": {"vault": str(vault)}},
    )
    for attempt in range(30):
        path = f"st{attempt}.md"
        store.write(path, "base", "a")
        barrier = threading.Barrier(2)

        def a() -> None:
            barrier.wait()
            memory_set_status(acl, "carol", "proj", path, "verifierad")

        def b() -> None:
            barrier.wait()
            store.append(path, "extra-line", "a")

        ts = [threading.Thread(target=a), threading.Thread(target=b)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        content = store.read(path)
        assert "extra-line" in content and "verifierad" in content, (attempt, content)


def test_concurrent_reads_and_search_vs_write(store):
    store.write("base.md", "alpha beta", "a")
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader() -> None:
        try:
            while not stop.is_set():
                store.read("base.md")
                store.search("alpha")
                store.list_all()
                store.get_updated_at("base.md")
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    rs = [threading.Thread(target=reader) for _ in range(4)]
    [r.start() for r in rs]
    try:
        for i in range(60):
            store.write(f"w{i % 5}.md", f"alpha {i}", "a")
    finally:
        stop.set()
        [r.join() for r in rs]
    assert errors == []


def _commit_raw(store, vault, files: dict[str, bytes], msg: str) -> str:
    for name, data in files.items():
        (vault / "memory" / name).write_bytes(data)
    return gitvault.commit(vault, [f"memory/{n}" for n in files], msg)


def test_revert_non_utf8_does_not_block_other_notes(store, vault):
    _commit_raw(store, vault, {"a-bad.md": b"caf\xe9 one", "z-ok.md": b"orig kiwi"}, "raw1")
    store.reindex("a-bad.md")
    store.reindex("z-ok.md")
    second = _commit_raw(store, vault, {"a-bad.md": b"new", "z-ok.md": b"changed plum"}, "raw2")
    store.reindex("a-bad.md")
    store.reindex("z-ok.md")
    store.revert(second)  # must not raise
    assert store.read("z-ok.md") == "orig kiwi"
    assert store.search("plum") == []
    assert store.read("a-bad.md").startswith("caf")


def test_revert_index_failure_indexes_rest_and_reports(store, vault, monkeypatch):
    _commit_raw(store, vault, {"a.md": b"one", "b.md": b"two"}, "raw1")
    store.reindex("a.md")
    store.reindex("b.md")
    second = _commit_raw(store, vault, {"a.md": b"A2", "b.md": b"B2"}, "raw2")
    store.reindex("a.md")
    store.reindex("b.md")
    real = store._index

    def flaky(path, content):
        if path == "a.md":
            raise RuntimeError("boom")
        return real(path, content)

    monkeypatch.setattr(store, "_index", flaky)
    with pytest.raises(IndexSyncError) as ei:
        store.revert(second)
    msg = str(ei.value)
    assert "reverted" in msg.lower() or "revert was made" in msg
    assert "committad" in msg and "a.md" in msg and "b.md" not in msg
    assert "RuntimeError" in msg and "boom" not in msg  # type only, no detail leak
    assert store.read("b.md") == "two"  # others still indexed
    assert store.read("a.md") == "A2"  # stale, as reported
    monkeypatch.undo()
    store.reindex("a.md")  # recovery without a new revert
    assert store.read("a.md") == "one"
