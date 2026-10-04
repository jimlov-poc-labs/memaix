# SPDX-License-Identifier: AGPL-3.0-or-later
"""Karakteriseringstester för kalenderresolverarna.

Låser beteendet i server._resolve_calendar_dav, server._resolve_normal_calendar_dav
och booking.routes._resolve_dav_filtered — funktionerna som avgör vems kalender
en bokning läser och skriver. Alla grenar, felmeddelanden och beroendeanrop.
"""

from __future__ import annotations

import json
import time

import pytest

from memaix_gateway import server
from memaix_gateway.booking import routes as routes_mod
from memaix_gateway.connectors import calendar_sources
from memaix_gateway.tools import calendar as cal_mod
from memaix_gateway.tools.calendar import CalendarAuthRequired


# ------------------------------------------------------------------
# Gemensamma fakes
# ------------------------------------------------------------------


class FakeAcl:
    def __init__(self, resources=None):
        self.resources = resources or {}
        self.calls = []

    def resource(self, project, name):
        self.calls.append((project, name))
        return self.resources.get(name)


class FakeStore:
    def __init__(self, accounts=None, tokens=None):
        self.accounts = accounts or []
        self.tokens = tokens or {}
        self.relinked = []
        self.loads = []

    def list_accounts(self, user):
        return list(self.accounts)

    def load_one(self, user, provider, account):
        self.loads.append((user, provider, account))
        return self.tokens.get((provider, account))

    def mark_needs_relink(self, user, provider, account):
        self.relinked.append((user, provider, account))


class Tagged:
    """Adapter-stand-in som minns konstruktorargumenten."""

    def __init__(self, *args):
        self.args = args


class FakeSa(Tagged):
    pass


class FakeNormal(Tagged):
    pass


class FakeMulti:
    def __init__(self, adapters):
        self.adapters = adapters


SA_ENV = {
    "type": "google",
    "auth": "service_account",
    "service_account_ref": "env:FAKE_SA_JSON",
    "impersonate": "alice@example.com",
}


# ------------------------------------------------------------------
# server._resolve_calendar_dav
# ------------------------------------------------------------------


@pytest.fixture()
def resolver(monkeypatch):
    """Patchar ACL/config/token store och fångar anropen till normal-resolvern."""
    state = {"acl": FakeAcl(), "cfg": {"memaix": {}}, "store": FakeStore(), "normal_calls": [], "normal": None}

    monkeypatch.setattr(server, "_get_acl", lambda: state["acl"])
    monkeypatch.setattr(server.config, "load", lambda: state["cfg"])
    monkeypatch.setattr(server, "_get_token_store", lambda: state["store"])

    def fake_normal(*args):
        state["normal_calls"].append(args)
        result = state["normal"]
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(server, "_resolve_normal_calendar_dav", fake_normal)
    monkeypatch.setattr(server, "_ServiceAccountGoogleCalendarAdapter", FakeSa)
    monkeypatch.setattr(server, "_MultiCalendarAdapter", FakeMulti)
    return state


def test_no_sa_delegates_to_normal_with_all_arguments(resolver):
    resolver["acl"].resources = {"calendar": {"auth": "per_user"}}
    resolver["cfg"] = {"memaix": {"server": {"public_url": "https://mx.example"}}}
    resolver["store"] = FakeStore(accounts=[{"provider": "google", "account": "a@x"}])
    sentinel = object()
    resolver["normal"] = sentinel

    assert server._resolve_calendar_dav("proj", "alice") is sentinel

    (args,) = resolver["normal_calls"]
    acl, cfg, store, project, user, all_accounts, require_per_user, public_url = args
    assert acl is resolver["acl"]
    assert cfg is resolver["cfg"]
    assert store is resolver["store"]
    assert (project, user) == ("proj", "alice")
    assert all_accounts == [{"provider": "google", "account": "a@x"}]
    assert require_per_user is True
    assert public_url == "https://mx.example"


@pytest.mark.parametrize(
    "cal_cfg, expected",
    [
        ({"auth": "per_user"}, True),
        ({"auth": "shared"}, False),
        ({}, False),
        (None, False),
        ("per_user", False),
    ],
)
def test_require_per_user_only_for_dict_with_per_user_auth(resolver, cal_cfg, expected):
    resolver["acl"].resources = {"calendar": cal_cfg}
    resolver["normal"] = None
    server._resolve_calendar_dav("proj", "alice")
    assert resolver["normal_calls"][0][6] is expected


def test_public_url_defaults_to_empty_string(resolver):
    resolver["cfg"] = {}
    resolver["normal"] = None
    server._resolve_calendar_dav("proj", "alice")
    assert resolver["normal_calls"][0][7] == ""


@pytest.mark.parametrize(
    "sa_res",
    [
        None,
        "service_account",
        {"auth": "oauth"},
        {"type": "google"},
    ],
)
def test_non_service_account_resource_goes_straight_to_normal(resolver, sa_res):
    resolver["acl"].resources = {"calendar_sa": sa_res}
    resolver["normal"] = None
    assert server._resolve_calendar_dav("proj", "alice") is None
    assert len(resolver["normal_calls"]) == 1
    assert ("proj", "calendar_sa") in resolver["acl"].calls


def test_write_with_sa_skips_sa_and_returns_normal(resolver, monkeypatch):
    resolver["acl"].resources = {"calendar_sa": dict(SA_ENV)}
    monkeypatch.delenv("FAKE_SA_JSON", raising=False)  # skulle ge fel om SA lästes
    sentinel = object()
    resolver["normal"] = sentinel

    assert server._resolve_calendar_dav("proj", "alice", write=True) is sentinel
    assert len(resolver["normal_calls"]) == 1


def test_write_with_sa_propagates_normal_auth_required(resolver):
    resolver["acl"].resources = {"calendar_sa": dict(SA_ENV)}
    resolver["normal"] = CalendarAuthRequired("nope")
    with pytest.raises(CalendarAuthRequired) as ei:
        server._resolve_calendar_dav("proj", "alice", write=True)
    assert ei.value.link_url == "nope"


def test_sa_env_merged_with_normal_adapter(resolver, monkeypatch):
    resolver["acl"].resources = {"calendar_sa": dict(SA_ENV)}
    monkeypatch.setenv("FAKE_SA_JSON", '{"client_email": "sa@x"}')
    normal = FakeNormal()
    resolver["normal"] = normal

    dav = server._resolve_calendar_dav("proj", "alice")

    assert isinstance(dav, FakeMulti)
    sa, second = dav.adapters
    assert isinstance(sa, FakeSa)
    assert sa.args == ({"client_email": "sa@x"}, "alice@example.com")
    assert second is normal


def test_sa_alone_when_normal_returns_none(resolver, monkeypatch):
    resolver["acl"].resources = {"calendar_sa": dict(SA_ENV)}
    monkeypatch.setenv("FAKE_SA_JSON", "{}")
    resolver["normal"] = None

    dav = server._resolve_calendar_dav("proj", "alice")

    assert isinstance(dav, FakeSa)
    assert dav.args == ({}, "alice@example.com")


def test_sa_alone_when_normal_raises_auth_required(resolver, monkeypatch):
    resolver["acl"].resources = {"calendar_sa": dict(SA_ENV)}
    monkeypatch.setenv("FAKE_SA_JSON", "{}")
    resolver["normal"] = CalendarAuthRequired("needs link")

    assert isinstance(server._resolve_calendar_dav("proj", "alice"), FakeSa)


def test_other_errors_from_normal_propagate(resolver, monkeypatch):
    resolver["acl"].resources = {"calendar_sa": dict(SA_ENV)}
    monkeypatch.setenv("FAKE_SA_JSON", "{}")
    resolver["normal"] = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        server._resolve_calendar_dav("proj", "alice")


def test_sa_file_ref_is_read(resolver, tmp_path):
    path = tmp_path / "sa.json"
    path.write_text(json.dumps({"k": "v"}))
    resolver["acl"].resources = {"calendar_sa": {**SA_ENV, "service_account_ref": f"file:{path}"}}
    resolver["normal"] = None

    dav = server._resolve_calendar_dav("proj", "alice")

    assert isinstance(dav, FakeSa)
    assert dav.args == ({"k": "v"}, "alice@example.com")


def test_sa_env_var_missing_message(resolver, monkeypatch):
    resolver["acl"].resources = {"calendar_sa": dict(SA_ENV)}
    monkeypatch.delenv("FAKE_SA_JSON", raising=False)
    with pytest.raises(CalendarAuthRequired) as ei:
        server._resolve_calendar_dav("proj", "alice")
    assert ei.value.link_url == "Env-var FAKE_SA_JSON saknas för SA-kalender"
    assert ei.value.options == []


def test_sa_env_var_empty_counts_as_missing(resolver, monkeypatch):
    resolver["acl"].resources = {"calendar_sa": dict(SA_ENV)}
    monkeypatch.setenv("FAKE_SA_JSON", "")
    with pytest.raises(CalendarAuthRequired) as ei:
        server._resolve_calendar_dav("proj", "alice")
    assert ei.value.link_url == "Env-var FAKE_SA_JSON saknas för SA-kalender"


def test_sa_unknown_ref_scheme_message(resolver):
    resolver["acl"].resources = {"calendar_sa": {**SA_ENV, "service_account_ref": "vault:x"}}
    with pytest.raises(CalendarAuthRequired) as ei:
        server._resolve_calendar_dav("proj", "alice")
    assert ei.value.link_url == "Okänd service_account_ref: vault:x"


def test_sa_invalid_json_wrapped_with_project(resolver, monkeypatch):
    resolver["acl"].resources = {"calendar_sa": dict(SA_ENV)}
    monkeypatch.setenv("FAKE_SA_JSON", "{not json")
    with pytest.raises(CalendarAuthRequired) as ei:
        server._resolve_calendar_dav("proj", "alice")
    assert ei.value.link_url.startswith("SA-konfigfel för proj: ")
    assert isinstance(ei.value.__cause__, json.JSONDecodeError)


def test_sa_missing_file_wrapped_with_project(resolver, tmp_path):
    missing = tmp_path / "nope.json"
    resolver["acl"].resources = {"calendar_sa": {**SA_ENV, "service_account_ref": f"file:{missing}"}}
    with pytest.raises(CalendarAuthRequired) as ei:
        server._resolve_calendar_dav("proj", "alice")
    assert ei.value.link_url.startswith("SA-konfigfel för proj: ")
    assert isinstance(ei.value.__cause__, FileNotFoundError)


def test_sa_missing_ref_key_wrapped(resolver):
    sa = dict(SA_ENV)
    del sa["service_account_ref"]
    resolver["acl"].resources = {"calendar_sa": sa}
    with pytest.raises(CalendarAuthRequired) as ei:
        server._resolve_calendar_dav("proj", "alice")
    assert ei.value.link_url == "SA-konfigfel för proj: 'service_account_ref'"
    assert isinstance(ei.value.__cause__, KeyError)


def test_sa_missing_impersonate_raises_keyerror_unwrapped(resolver, monkeypatch):
    # Kvarlevande beteende: nyckeln läses utanför try-blocket, så KeyError
    # slinker igenom ombytt mot CalendarAuthRequired.
    sa = dict(SA_ENV)
    del sa["impersonate"]
    resolver["acl"].resources = {"calendar_sa": sa}
    monkeypatch.setenv("FAKE_SA_JSON", "{}")
    with pytest.raises(KeyError):
        server._resolve_calendar_dav("proj", "alice")
    assert resolver["normal_calls"] == []


def test_sa_error_does_not_reach_normal_resolver(resolver, monkeypatch):
    resolver["acl"].resources = {"calendar_sa": dict(SA_ENV)}
    monkeypatch.delenv("FAKE_SA_JSON", raising=False)
    with pytest.raises(CalendarAuthRequired):
        server._resolve_calendar_dav("proj", "alice")
    assert resolver["normal_calls"] == []


# ------------------------------------------------------------------
# server._resolve_normal_calendar_dav
# ------------------------------------------------------------------

FAR_FUTURE = time.time() + 10_000_000


@pytest.fixture()
def normal(monkeypatch):
    refresh_calls = []
    state = {"refresh_result": "new-token", "refresh_calls": refresh_calls, "link": {"link_url": "https://link.example/g"}, "link_calls": []}

    def fake_refresh(cfg, store, user, account, token_data):
        refresh_calls.append((cfg, store, user, account, token_data))
        return state["refresh_result"]

    def fake_link(acl, user, provider, public_url):
        state["link_calls"].append((acl, user, provider, public_url))
        result = state["link"]
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(server, "_refresh_google_token", fake_refresh)
    monkeypatch.setattr(server.t_account, "account_link", fake_link)
    monkeypatch.setattr(server, "_PerUserGoogleAdapter", lambda token: ("google", token))
    monkeypatch.setattr(server, "_ICalAdapter", lambda url: ("ical", url))
    monkeypatch.setattr(server, "_FreeBusyAdapter", lambda cal_id, key: ("freebusy", cal_id, key))
    return state


def _call(accounts=(), tokens=None, *, cfg=None, require_per_user=False, public_url="", store=None):
    store = store or FakeStore(accounts=list(accounts), tokens=tokens or {})
    acl = object()
    result = server._resolve_normal_calendar_dav(
        acl, cfg if cfg is not None else {}, store, "proj", "alice", store.accounts, require_per_user, public_url
    )
    return result, store, acl


G = {"provider": "google", "account": "g@x"}
ICAL = {"provider": "ical_secret", "account": "ical-1"}
FB = {"provider": "free_busy", "account": "fb-1"}


def test_google_fresh_token_used_without_refresh(normal):
    tokens = {("google", "g@x"): {"access_token": "tok", "expires_at": FAR_FUTURE}}
    result, store, _ = _call([G], tokens)
    assert result == ("google", "tok")
    assert normal["refresh_calls"] == []
    assert store.loads == [("alice", "google", "g@x")]


def test_google_only_first_account_is_used(normal):
    other = {"provider": "google", "account": "second@x"}
    tokens = {
        ("google", "g@x"): {"access_token": "first", "expires_at": FAR_FUTURE},
        ("google", "second@x"): {"access_token": "second", "expires_at": FAR_FUTURE},
    }
    result, store, _ = _call([G, other], tokens)
    assert result == ("google", "first")
    assert store.loads == [("alice", "google", "g@x")]


def test_google_expired_token_refreshed(normal):
    token_data = {"access_token": "old", "expires_at": 1}
    result, store, _ = _call([G], {("google", "g@x"): token_data}, cfg={"c": 1})
    assert result == ("google", "new-token")
    ((cfg, st, user, account, td),) = normal["refresh_calls"]
    assert cfg == {"c": 1} and st is store and (user, account) == ("alice", "g@x") and td is token_data
    assert store.relinked == []


def test_google_token_expiring_within_a_minute_is_refreshed(normal):
    tokens = {("google", "g@x"): {"access_token": "old", "expires_at": time.time() + 30}}
    result, _, _ = _call([G], tokens)
    assert result == ("google", "new-token")


def test_google_missing_access_token_is_refreshed_even_if_not_expired(normal):
    tokens = {("google", "g@x"): {"expires_at": FAR_FUTURE}}
    result, _, _ = _call([G], tokens)
    assert result == ("google", "new-token")
    assert len(normal["refresh_calls"]) == 1


def test_google_non_numeric_expiry_with_token_is_not_refreshed(normal):
    tokens = {("google", "g@x"): {"access_token": "tok", "expires_at": "tomorrow"}}
    result, _, _ = _call([G], tokens)
    assert result == ("google", "tok")
    assert normal["refresh_calls"] == []


def test_google_expiry_computed_from_created_at_and_expires_in(normal):
    fresh = {("google", "g@x"): {"access_token": "tok", "created_at": time.time(), "expires_in": 3600}}
    result, _, _ = _call([G], fresh)
    assert result == ("google", "tok")
    assert normal["refresh_calls"] == []

    stale = {("google", "g@x"): {"access_token": "tok", "created_at": time.time() - 4000, "expires_in": 3600}}
    result, _, _ = _call([G], stale)
    assert result == ("google", "new-token")


def test_google_expiry_defaults_to_one_hour_after_epoch_when_absent(normal):
    tokens = {("google", "g@x"): {"access_token": "tok"}}
    result, _, _ = _call([G], tokens)
    assert result == ("google", "new-token")


def test_google_refresh_failure_marks_relink_and_falls_through(normal):
    normal["refresh_result"] = None
    tokens = {("google", "g@x"): {"access_token": "old", "expires_at": 1}}
    result, store, _ = _call([G], tokens)
    assert result is None
    assert store.relinked == [("alice", "google", "g@x")]


def test_google_refresh_failure_falls_back_to_ical(normal):
    normal["refresh_result"] = ""
    tokens = {
        ("google", "g@x"): {"expires_at": 1},
        ("ical_secret", "ical-1"): {"ical_url": "https://ical"},
    }
    result, store, _ = _call([G, ICAL], tokens)
    assert result == ("ical", "https://ical")
    assert store.relinked == [("alice", "google", "g@x")]


def test_google_without_stored_token_falls_through_to_ical(normal):
    tokens = {("ical_secret", "ical-1"): {"ical_url": "https://ical"}}
    result, _, _ = _call([G, ICAL], tokens)
    assert result == ("ical", "https://ical")
    assert normal["refresh_calls"] == []


def test_google_beats_ical_and_freebusy(normal):
    tokens = {
        ("google", "g@x"): {"access_token": "tok", "expires_at": FAR_FUTURE},
        ("ical_secret", "ical-1"): {"ical_url": "https://ical"},
        ("free_busy", "fb-1"): {"calendar_id": "c"},
    }
    result, _, _ = _call([ICAL, FB, G], tokens, cfg={"memaix": {"google_api_key": "k"}})
    assert result == ("google", "tok")


def test_ical_uses_first_ical_account_only(normal):
    ical2 = {"provider": "ical_secret", "account": "ical-2"}
    tokens = {
        ("ical_secret", "ical-1"): {"ical_url": "https://one"},
        ("ical_secret", "ical-2"): {"ical_url": "https://two"},
    }
    result, store, _ = _call([ICAL, ical2], tokens)
    assert result == ("ical", "https://one")
    assert store.loads == [("alice", "ical_secret", "ical-1")]


@pytest.mark.parametrize("token_data", [None, {}, {"ical_url": ""}, {"other": 1}])
def test_ical_without_url_falls_through(normal, token_data):
    tokens = {("ical_secret", "ical-1"): token_data} if token_data is not None else {}
    fb_tokens = {("free_busy", "fb-1"): {"calendar_id": "cal"}}
    result, _, _ = _call([ICAL, FB], {**tokens, **fb_tokens}, cfg={"memaix": {"google_api_key": "k"}})
    assert result == ("freebusy", "cal", "k")


def test_freebusy_returned_with_calendar_and_api_key(normal):
    tokens = {("free_busy", "fb-1"): {"calendar_id": "me@gmail.com"}}
    result, store, _ = _call([FB], tokens, cfg={"memaix": {"google_api_key": "key"}})
    assert result == ("freebusy", "me@gmail.com", "key")
    assert store.loads == [("alice", "free_busy", "fb-1")]


@pytest.mark.parametrize(
    "token_data, cfg",
    [
        (None, {"memaix": {"google_api_key": "key"}}),
        ({"calendar_id": ""}, {"memaix": {"google_api_key": "key"}}),
        ({}, {"memaix": {"google_api_key": "key"}}),
        ({"calendar_id": "c"}, {"memaix": {}}),
        ({"calendar_id": "c"}, {}),
        ({"calendar_id": "c"}, {"memaix": {"google_api_key": ""}}),
    ],
)
def test_freebusy_incomplete_falls_through_to_none(normal, token_data, cfg):
    tokens = {("free_busy", "fb-1"): token_data} if token_data is not None else {}
    result, _, _ = _call([FB], tokens, cfg=cfg)
    assert result is None


def test_no_accounts_and_not_per_user_returns_none(normal):
    result, store, _ = _call([])
    assert result is None
    assert store.loads == []
    assert normal["link_calls"] == []


def test_per_user_without_config_raises_with_three_options_and_link(normal):
    with pytest.raises(CalendarAuthRequired) as ei:
        _call([], require_per_user=True, public_url="https://mx.example")
    exc = ei.value
    assert exc.link_url == "https://link.example/g"
    assert [o["mode"] for o in exc.options] == ["oauth", "ical_secret", "free_busy"]
    assert exc.options[0] == {
        "mode": "oauth",
        "label": "Google Calendar (full access, read+write)",
        "action": "Öppna https://link.example/g och logga in med Google",
    }
    assert exc.options[1] == {
        "mode": "ical_secret",
        "label": "iCal secret URL (read-only, alla providers)",
        "action": "calendar_setup(mode='ical_secret', ical_url='din-hemliga-ical-url')",
    }
    assert exc.options[2] == {
        "mode": "free_busy",
        "label": "FreeBusy (visar bara ledig/upptagen, kräver publik kalender)",
        "action": "calendar_setup(mode='free_busy', calendar_id='din@gmail.com')",
    }
    assert str(exc) == "auth_required: configure calendar via calendar_setup"
    ((acl, user, provider, public_url),) = normal["link_calls"]
    assert (user, provider, public_url) == ("alice", "google", "https://mx.example")


def test_per_user_without_public_url_skips_link_generation(normal):
    with pytest.raises(CalendarAuthRequired) as ei:
        _call([], require_per_user=True, public_url="")
    assert ei.value.link_url == ""
    assert ei.value.options[0]["action"] == "Öppna  och logga in med Google"
    assert normal["link_calls"] == []


def test_per_user_link_failure_is_swallowed(normal):
    normal["link"] = RuntimeError("no oauth")
    with pytest.raises(CalendarAuthRequired) as ei:
        _call([], require_per_user=True, public_url="https://mx.example")
    assert ei.value.link_url == ""
    assert len(normal["link_calls"]) == 1


def test_per_user_with_unusable_accounts_still_raises(normal):
    # Konton finns men ger inget användbart -> samma fel som utan konton.
    tokens = {("ical_secret", "ical-1"): {"ical_url": ""}}
    with pytest.raises(CalendarAuthRequired):
        _call([ICAL], tokens, require_per_user=True, public_url="")


def test_per_user_satisfied_by_adapter_does_not_raise(normal):
    tokens = {("ical_secret", "ical-1"): {"ical_url": "https://ical"}}
    result, _, _ = _call([ICAL], tokens, require_per_user=True, public_url="https://mx.example")
    assert result == ("ical", "https://ical")


# ------------------------------------------------------------------
# booking.routes._resolve_dav_filtered
# ------------------------------------------------------------------


@pytest.fixture()
def filtered(monkeypatch):
    state = {
        "disabled": [],
        "sources": [],
        "resolve_dav_calls": [],
        "selection_calls": [],
        "source_calls": [],
    }

    class FakeSelection:
        def __init__(self, acl, project, user):
            state["selection_calls"].append((acl, project, user))

        def list(self):
            return {"disabled": state["disabled"]}

    def fake_sources(acl, token_store, project, user):
        state["source_calls"].append((acl, token_store, project, user))
        return state["sources"]

    sentinel_store = object()
    state["token_store"] = sentinel_store

    monkeypatch.setattr(calendar_sources, "SourceSelectionStore", FakeSelection)
    monkeypatch.setattr(calendar_sources, "resolve_effective_sources", fake_sources)
    monkeypatch.setattr(server, "_get_token_store", lambda: sentinel_store)
    monkeypatch.setattr(cal_mod, "_MultiCalendarAdapter", FakeMulti)
    monkeypatch.setattr(cal_mod, "_ServiceAccountGoogleCalendarAdapter", FakeSa)

    def fake_resolve_dav(project, user, **kw):
        state["resolve_dav_calls"].append((project, user, kw))
        return "plain-dav"

    monkeypatch.setattr(routes_mod, "_resolve_dav", fake_resolve_dav)
    return state


def test_filtered_without_vault_falls_back_to_resolve_dav(filtered):
    acl = FakeAcl({"vault": None})
    assert routes_mod._resolve_dav_filtered("proj", "alice", acl) == "plain-dav"
    assert filtered["resolve_dav_calls"] == [("proj", "alice", {})]
    assert filtered["selection_calls"] == []
    assert ("proj", "vault") in acl.calls


def test_filtered_empty_vault_string_also_falls_back(filtered):
    assert routes_mod._resolve_dav_filtered("proj", "alice", FakeAcl({"vault": ""})) == "plain-dav"


def test_filtered_no_adapters_raises_with_project_and_user(filtered):
    acl = FakeAcl({"vault": "/v"})
    with pytest.raises(CalendarAuthRequired) as ei:
        routes_mod._resolve_dav_filtered("proj", "alice", acl)
    assert ei.value.link_url == "Inga aktiva kalenderadaptrar för proj/alice"
    assert filtered["selection_calls"] == [(acl, "proj", "alice")]
    assert filtered["resolve_dav_calls"] == []


def test_filtered_single_source_returned_unwrapped(filtered):
    acl = FakeAcl({"vault": "/v"})
    only = object()
    filtered["sources"] = [("label", only)]
    assert routes_mod._resolve_dav_filtered("proj", "alice", acl) is only
    assert filtered["source_calls"] == [(acl, filtered["token_store"], "proj", "alice")]


def test_filtered_multiple_sources_wrapped_in_order(filtered):
    a, b = object(), object()
    filtered["sources"] = [("one", a), ("two", b)]
    dav = routes_mod._resolve_dav_filtered("proj", "alice", FakeAcl({"vault": "/v"}))
    assert isinstance(dav, FakeMulti)
    assert dav.adapters == [a, b]


def test_filtered_sa_first_then_sources(filtered, monkeypatch):
    monkeypatch.setenv("FAKE_SA_JSON", '{"x": 1}')
    src = object()
    filtered["sources"] = [("lbl", src)]
    acl = FakeAcl({"vault": "/v", "calendar_sa": dict(SA_ENV)})

    dav = routes_mod._resolve_dav_filtered("proj", "alice", acl)

    assert isinstance(dav, FakeMulti)
    sa, second = dav.adapters
    assert isinstance(sa, FakeSa)
    assert sa.args == ({"x": 1}, "alice@example.com")
    assert second is src


def test_filtered_sa_alone_returned_unwrapped(filtered, monkeypatch):
    monkeypatch.setenv("FAKE_SA_JSON", "{}")
    dav = routes_mod._resolve_dav_filtered("proj", "alice", FakeAcl({"vault": "/v", "calendar_sa": dict(SA_ENV)}))
    assert isinstance(dav, FakeSa)


def test_filtered_sa_disabled_is_skipped_without_reading_env(filtered, monkeypatch):
    monkeypatch.delenv("FAKE_SA_JSON", raising=False)
    filtered["disabled"] = ["calendar_sa"]
    src = object()
    filtered["sources"] = [("lbl", src)]
    acl = FakeAcl({"vault": "/v", "calendar_sa": dict(SA_ENV)})
    assert routes_mod._resolve_dav_filtered("proj", "alice", acl) is src


def test_filtered_other_disabled_labels_do_not_disable_sa(filtered, monkeypatch):
    monkeypatch.setenv("FAKE_SA_JSON", "{}")
    filtered["disabled"] = ["something-else"]
    acl = FakeAcl({"vault": "/v", "calendar_sa": dict(SA_ENV)})
    assert isinstance(routes_mod._resolve_dav_filtered("proj", "alice", acl), FakeSa)


@pytest.mark.parametrize("sa_res", [None, "service_account", {"auth": "oauth"}, {}])
def test_filtered_non_service_account_resource_ignored(filtered, sa_res):
    acl = FakeAcl({"vault": "/v", "calendar_sa": sa_res})
    with pytest.raises(CalendarAuthRequired) as ei:
        routes_mod._resolve_dav_filtered("proj", "alice", acl)
    assert ei.value.link_url == "Inga aktiva kalenderadaptrar för proj/alice"


def test_filtered_sa_file_ref(filtered, tmp_path):
    path = tmp_path / "sa.json"
    path.write_text('{"f": true}')
    acl = FakeAcl({"vault": "/v", "calendar_sa": {**SA_ENV, "service_account_ref": f"file:{path}"}})
    dav = routes_mod._resolve_dav_filtered("proj", "alice", acl)
    assert isinstance(dav, FakeSa)
    assert dav.args == ({"f": True}, "alice@example.com")


def test_filtered_sa_env_missing_message(filtered, monkeypatch):
    monkeypatch.delenv("FAKE_SA_JSON", raising=False)
    acl = FakeAcl({"vault": "/v", "calendar_sa": dict(SA_ENV)})
    with pytest.raises(CalendarAuthRequired) as ei:
        routes_mod._resolve_dav_filtered("proj", "alice", acl)
    assert ei.value.link_url == "Env-var FAKE_SA_JSON saknas för SA-kalender"
    assert filtered["source_calls"] == []


def test_filtered_sa_unknown_ref_message(filtered):
    acl = FakeAcl({"vault": "/v", "calendar_sa": {**SA_ENV, "service_account_ref": "kms:x"}})
    with pytest.raises(CalendarAuthRequired) as ei:
        routes_mod._resolve_dav_filtered("proj", "alice", acl)
    assert ei.value.link_url == "Okänd service_account_ref: kms:x"


def test_filtered_sa_invalid_json_wrapped(filtered, monkeypatch):
    monkeypatch.setenv("FAKE_SA_JSON", "][")
    acl = FakeAcl({"vault": "/v", "calendar_sa": dict(SA_ENV)})
    with pytest.raises(CalendarAuthRequired) as ei:
        routes_mod._resolve_dav_filtered("proj", "alice", acl)
    assert ei.value.link_url.startswith("SA-konfigfel för proj: ")
    assert isinstance(ei.value.__cause__, json.JSONDecodeError)


def test_filtered_sa_missing_file_wrapped(filtered, tmp_path):
    acl = FakeAcl({"vault": "/v", "calendar_sa": {**SA_ENV, "service_account_ref": f"file:{tmp_path}/gone.json"}})
    with pytest.raises(CalendarAuthRequired) as ei:
        routes_mod._resolve_dav_filtered("proj", "alice", acl)
    assert ei.value.link_url.startswith("SA-konfigfel för proj: ")
    assert isinstance(ei.value.__cause__, FileNotFoundError)


def test_filtered_sa_missing_ref_key_wrapped(filtered):
    sa = dict(SA_ENV)
    del sa["service_account_ref"]
    acl = FakeAcl({"vault": "/v", "calendar_sa": sa})
    with pytest.raises(CalendarAuthRequired) as ei:
        routes_mod._resolve_dav_filtered("proj", "alice", acl)
    assert ei.value.link_url == "SA-konfigfel för proj: 'service_account_ref'"


def test_filtered_sa_missing_impersonate_raises_keyerror_unwrapped(filtered, monkeypatch):
    # Kvarlevande beteende: som i server-resolvern läses nyckeln utanför try.
    monkeypatch.setenv("FAKE_SA_JSON", "{}")
    sa = dict(SA_ENV)
    del sa["impersonate"]
    acl = FakeAcl({"vault": "/v", "calendar_sa": sa})
    with pytest.raises(KeyError):
        routes_mod._resolve_dav_filtered("proj", "alice", acl)
    assert filtered["source_calls"] == []
