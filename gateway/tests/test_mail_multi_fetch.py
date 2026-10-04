# SPDX-License-Identifier: AGPL-3.0-or-later
"""Karakteriseringstester för MultiMailBackend.fetch (Sonar S3776-saneringen):
UID-routing, sammanslagning/sortering, limit, mark_seen, delvisa källfel,
source_errors och relink-poster. Låser dagens beteende — inklusive kvirkar."""

from __future__ import annotations

import datetime

import pytest

from memaix_gateway.connectors.adapters.mail_multi import (
    MultiMailBackend,
    message_time,
    source_address,
)

UTC = datetime.timezone.utc


class _Msg:
    def __init__(self, uid, day=None):
        self.uid = uid
        self.date = datetime.datetime(2025, 1, day, tzinfo=UTC) if day else None
        self.date_str = ""


class _Folder:
    def __init__(self, owner):
        self._owner = owner

    def set(self, name):
        if self._owner.refuse_folder is not None:
            raise self._owner.refuse_folder
        self._owner.folder_name = name


class _Adapter:
    """Fake källa. fetch() ger färska _Msg-kopior (backend skriver om .uid)."""

    def __init__(self, msgs=(), *, raises=None, refuse_folder=None, source_errors=None):
        self._msgs = list(msgs)
        self.raises = raises
        self.refuse_folder = refuse_folder
        self.folder_name = None
        self.calls = []
        if source_errors is not None:
            self.source_errors = source_errors
        self.folder = _Folder(self)

    def fetch(self, criteria="ALL", *, mark_seen=False, limit=None):
        self.calls.append((criteria, mark_seen, limit))
        if self.raises is not None:
            raise self.raises
        return iter([_Msg(m.uid, m.date.day if m.date else None) for m in self._msgs])


def _backend(*pairs, relink=None):
    return MultiMailBackend(list(pairs), relink)


# --- UID-vägen ---------------------------------------------------------------

def test_uid_without_label_asks_every_source_first_match_wins():
    a, b, c = _Adapter(), _Adapter([_Msg("7")]), _Adapter([_Msg("7")])
    mb = _backend(("imap:a", a), ("imap:b@x.se", b), ("imap:c", c))
    out = mb.fetch("UID 7", mark_seen=True)
    assert [m.uid for m in out] == ["imap:b@x.se|7"]
    assert out[0].inbox == "b@x.se"
    assert a.calls == [("UID 7", True, None)]
    assert b.calls == [("UID 7", True, None)]
    assert c.calls == []  # första träffen vinner


def test_uid_without_label_no_match_returns_empty_list():
    mb = _backend(("imap:a", _Adapter()), ("imap:b", _Adapter()))
    assert mb.fetch("UID 99") == []


def test_uid_with_label_routes_to_owner_only_and_splits_on_first_separator():
    a, b = _Adapter([_Msg("x")]), _Adapter([_Msg("INBOX|5")])
    mb = _backend(("imap:a", a), ("imap:b", b))
    out = mb.fetch("UID imap:b|INBOX|5")
    assert [m.uid for m in out] == ["imap:b|INBOX|5"]
    assert a.calls == []
    assert b.calls == [("UID INBOX|5", False, None)]
    assert not hasattr(out[0], "inbox")  # etiketten saknar adress -> ingen inbox satt


def test_uid_with_label_ignores_limit_and_passes_mark_seen():
    b = _Adapter([_Msg("5")])
    mb = _backend(("imap:a", _Adapter()), ("imap:b", b))
    mb.fetch("UID imap:b|5", mark_seen=True, limit=1)
    assert b.calls == [("UID 5", True, None)]


def test_uid_with_unknown_label_returns_empty_without_fetching():
    a = _Adapter([_Msg("5")])
    mb = _backend(("imap:a", a), ("imap:b", _Adapter()))
    assert mb.fetch("UID imap:zzz|5") == []
    assert a.calls == []


def test_uid_with_label_and_empty_result_returns_empty():
    mb = _backend(("imap:a", _Adapter()), ("imap:b", _Adapter()))
    assert mb.fetch("UID imap:a|5") == []


def test_uid_path_resets_source_errors_and_does_not_report_relink():
    mb = _backend(("imap:a", _Adapter(raises=RuntimeError("boom"))), ("imap:b", _Adapter([_Msg("1", 1)])),
                  relink=[("google", "z@x.se")])
    mb.fetch("ALL")
    assert mb.source_errors  # sattes av sökningen
    mb.fetch("UID imap:b|1")
    assert mb.source_errors == []


def test_uid_path_propagates_adapter_exception():
    mb = _backend(("imap:a", _Adapter(raises=RuntimeError("nere"))), ("imap:b", _Adapter()))
    with pytest.raises(RuntimeError, match="nere"):
        mb.fetch("UID imap:a|1")
    with pytest.raises(RuntimeError, match="nere"):
        mb.fetch("UID 1")


# --- sök/lista-vägen -----------------------------------------------------------

def test_search_merges_newest_first_and_labels_messages():
    a = _Adapter([_Msg("1", 5), _Msg("2", 1)])
    b = _Adapter([_Msg("9", 3)])
    mb = _backend(("imap_user:a@x.se", a), ("shared:proj", b))
    out = mb.fetch("ALL")
    assert [m.uid for m in out] == ["imap_user:a@x.se|1", "shared:proj|9", "imap_user:a@x.se|2"]
    assert out[0].inbox == "a@x.se"
    assert not hasattr(out[1], "inbox")
    assert mb.source_errors == []


def test_search_passes_criteria_mark_seen_and_limit_to_every_source():
    a, b = _Adapter(), _Adapter()
    mb = _backend(("s:a", a), ("s:b", b))
    mb.fetch("SUBJECT x", mark_seen=True, limit=4)
    assert a.calls == [("SUBJECT x", True, 4)]
    assert b.calls == [("SUBJECT x", True, 4)]
    mb.fetch()
    assert a.calls[-1] == ("ALL", False, None)


def test_limit_cuts_after_merge_not_before():
    a = _Adapter([_Msg("1", 1), _Msg("2", 2)])
    b = _Adapter([_Msg("3", 20), _Msg("4", 21)])
    mb = _backend(("s:a", a), ("s:b", b))
    assert [m.uid for m in mb.fetch("ALL", limit=3)] == ["s:b|4", "s:b|3", "s:a|2"]
    assert [m.uid for m in mb.fetch("ALL", limit=1)] == ["s:b|4"]


def test_limit_none_and_zero_do_not_cut():
    mb = _backend(("s:a", _Adapter([_Msg("1", 1), _Msg("2", 2), _Msg("3", 3)])), ("s:b", _Adapter()))
    assert len(mb.fetch("ALL", limit=None)) == 3
    assert len(mb.fetch("ALL", limit=0)) == 3


def test_criteria_starting_with_uid_lowercase_is_a_search_not_a_uid_lookup():
    a = _Adapter()
    mb = _backend(("s:a", a), ("s:b", _Adapter()))
    mb.fetch("uid 5")
    assert a.calls == [("uid 5", False, None)]


# --- delvisa källfel ------------------------------------------------------------

def test_one_failing_source_is_reported_and_others_still_returned():
    good = _Adapter([_Msg("1", 2)])
    bad = _Adapter(raises=RuntimeError("imap nere"))
    mb = _backend(("s:bad", bad), ("s:good", good))
    out = mb.fetch("ALL")
    assert [m.uid for m in out] == ["s:good|1"]
    assert mb.source_errors == [{"source": "s:bad", "error": "imap nere"}]


def test_failure_with_empty_message_reports_exception_type_name():
    mb = _backend(("s:bad", _Adapter(raises=TimeoutError())), ("s:ok", _Adapter()))
    mb.fetch("ALL")
    assert mb.source_errors == [{"source": "s:bad", "error": "TimeoutError"}]


def test_all_sources_failing_raises_first_exception_and_leaves_source_errors_empty():
    first, second = RuntimeError("första"), ValueError("andra")
    mb = _backend(("s:a", _Adapter(raises=first)), ("s:b", _Adapter(raises=second)))
    mb.source_errors = [{"source": "gammal", "error": "x"}]
    with pytest.raises(RuntimeError) as exc:
        mb.fetch("ALL")
    assert exc.value is first
    assert mb.source_errors == []  # nollas i början, sätts aldrig när allt föll


def test_single_failing_source_raises_even_with_relink_pending():
    mb = _backend(("s:a", _Adapter(raises=RuntimeError("nere"))), relink=[("google", "z@x.se")])
    with pytest.raises(RuntimeError, match="nere"):
        mb.fetch("ALL")


def test_relink_pending_is_reported_first_and_does_not_count_as_failure():
    mb = _backend(("s:a", _Adapter([_Msg("1", 1)])), relink=[("google_mail", "z@x.se"), ("ms", "q@y.se")])
    out = mb.fetch("ALL")
    assert [m.uid for m in out] == ["s:a|1"]
    assert mb.source_errors == [
        {"source": "google_mail", "error": "needs_relink: z@x.se måste kopplas om"},
        {"source": "ms", "error": "needs_relink: q@y.se måste kopplas om"},
    ]


def test_relink_errors_precede_fetch_errors():
    mb = _backend(("s:bad", _Adapter(raises=RuntimeError("e"))), ("s:ok", _Adapter()), relink=[("g", "z@x.se")])
    mb.fetch("ALL")
    assert [e["source"] for e in mb.source_errors] == ["g", "s:bad"]


def test_source_errors_are_reset_on_each_fetch():
    bad = _Adapter(raises=RuntimeError("e"))
    mb = _backend(("s:bad", bad), ("s:ok", _Adapter()))
    mb.fetch("ALL")
    assert len(mb.source_errors) == 1
    bad.raises = None
    mb.fetch("ALL")
    assert mb.source_errors == []


def test_partial_adapter_source_errors_are_merged_with_label_prefix():
    partial = _Adapter([_Msg("1", 1)], source_errors=[
        {"source": "Arkiv", "error": "folder saknas"},
        {"error": "utan källa"},
        {"source": "Skräp"},
    ])
    empty = _Adapter(source_errors=[])
    none = _Adapter(source_errors=None)
    none.source_errors = None
    mb = _backend(("s:p", partial), ("s:e", empty), ("s:n", none), ("s:plain", _Adapter()))
    out = mb.fetch("ALL")
    assert [m.uid for m in out] == ["s:p|1"]
    assert mb.source_errors == [
        {"source": "s:p Arkiv", "error": "folder saknas"},
        {"source": "s:p", "error": "utan källa"},
        {"source": "s:p Skräp", "error": ""},
    ]


def test_failing_adapters_own_source_errors_are_not_read():
    bad = _Adapter(raises=RuntimeError("e"), source_errors=[{"source": "x", "error": "y"}])
    mb = _backend(("s:bad", bad), ("s:ok", _Adapter()))
    mb.fetch("ALL")
    assert mb.source_errors == [{"source": "s:bad", "error": "e"}]


# --- folder.set-fel (hålls tills nästa fetch) -----------------------------------------

def test_refused_folder_is_reported_by_next_fetch_and_source_is_skipped():
    refuses = _Adapter([_Msg("1", 9)], refuse_folder=RuntimeError("ingen mapp"))
    accepts = _Adapter([_Msg("2", 1)])
    mb = _backend(("s:no", refuses), ("s:yes", accepts))
    mb.folder.set("Arkiv")
    assert accepts.folder_name == "Arkiv"
    out = mb.fetch("ALL")
    assert [m.uid for m in out] == ["s:yes|2"]
    assert refuses.calls == []
    assert mb.source_errors == [{"source": "s:no", "error": "ingen mapp"}]


def test_refused_folder_on_every_source_raises_first_set_error():
    e1, e2 = RuntimeError("a"), RuntimeError("b")
    mb = _backend(("s:1", _Adapter(refuse_folder=e1)), ("s:2", _Adapter(refuse_folder=e2)))
    mb.folder.set("Nope")
    with pytest.raises(RuntimeError) as exc:
        mb.fetch("ALL")
    assert exc.value is e1


def test_setting_folder_again_clears_earlier_refusals():
    a = _Adapter([_Msg("1", 1)], refuse_folder=RuntimeError("nej"))
    mb = _backend(("s:a", a), ("s:b", _Adapter()))
    mb.folder.set("X")
    a.refuse_folder = None
    mb.folder.set("INBOX")
    out = mb.fetch("ALL")
    assert [m.uid for m in out] == ["s:a|1"]
    assert mb.source_errors == []
    assert mb._folder == "INBOX"


# --- övrigt runt fetch -----------------------------------------------------------------

def test_constructor_requires_sources():
    with pytest.raises(ValueError, match="at least one source"):
        MultiMailBackend([])


def test_sources_property_returns_copy_in_registry_order():
    a, b = _Adapter(), _Adapter()
    mb = _backend(("s:a", a), ("s:b", b))
    assert mb.sources == [("s:a", a), ("s:b", b)]
    mb.sources.clear()
    assert len(mb.sources) == 2


def test_logout_calls_every_adapter_that_has_logout():
    class WithLogout(_Adapter):
        done = False

        def logout(self):
            self.done = True

    w = WithLogout()
    _backend(("s:a", w), ("s:b", _Adapter())).logout()
    assert w.done is True


def test_source_address():
    assert source_address("google_mail:jimmy@jimlov.se") == "jimmy@jimlov.se"
    assert source_address("shared:proj") == ""
    assert source_address("nolabel") == ""


@pytest.mark.parametrize(
    "attrs,expected",
    [
        ({"date": datetime.datetime(2025, 3, 1, 12, 0)}, datetime.datetime(2025, 3, 1, 12, 0, tzinfo=UTC)),
        ({"date": datetime.datetime(1, 1, 1), "date_str": ""}, datetime.datetime.min.replace(tzinfo=UTC)),
        ({"date_str": ""}, datetime.datetime.min.replace(tzinfo=UTC)),
        ({"date_str": None}, datetime.datetime.min.replace(tzinfo=UTC)),
        ({"date_str": "1700000000000"}, datetime.datetime.fromtimestamp(1700000000, tz=UTC)),
        ({"date_str": "Mon, 06 Jan 2025 10:00:00 +0000"}, datetime.datetime(2025, 1, 6, 10, tzinfo=UTC)),
        ({"date_str": "2025-01-06T10:00:00"}, datetime.datetime(2025, 1, 6, 10, tzinfo=UTC)),
        ({"date_str": "inte ett datum"}, datetime.datetime.min.replace(tzinfo=UTC)),
    ],
)
def test_message_time(attrs, expected):
    m = type("M", (), attrs)()
    assert message_time(m) == expected
