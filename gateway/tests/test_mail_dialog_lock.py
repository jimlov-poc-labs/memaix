# SPDX-License-Identifier: AGPL-3.0-or-later
"""Behaviour locks for the mail dialog helpers (Sonar refactor 2026-10-26).

Written against the code before it was split into smaller functions, so a
refactor that reorders a check, changes an error text or rebinds a closure
fails here. Each test pins one branch, its order relative to the others,
or the exact message a user sees.
"""

from __future__ import annotations

import argparse
import email
import io
from email import policy
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from memaix_gateway import config, server
from memaix_gateway.acl import Acl
from memaix_gateway.backends.token_store import TokenStore
from memaix_gateway.connectors import mail_accounts
from memaix_gateway.connectors.registry import ConnectorRegistry, ConnectorSpec
from memaix_gateway.notify import channels as ch
from memaix_gateway.tools import mail_client as mc
from memaix_gateway.tools import mail_unlock as mu

_TOPIC = "Kq7xV2mPz9LwR4tYb8NcH3sJ"


@pytest.fixture()
def acl():
    return Acl(
        users={
            "jimmy": {"grants": {"alfa": "owner", "beta": "owner", "gamma": "reader"}},
            "eve": {"grants": {"alfa": "reader"}},
        },
        projects={
            "alfa": {"mailbox": {"host": "imap.alfa.se", "user": "delad@alfa.se", "password_ref": "env:ALFA_PW"}},
            "beta": {"mailbox": {"type": "gmailish", "user": "x@beta.se"}},
            "gamma": {"mailbox": {"host": "imap.gamma.se", "user": "g@gamma.se"}},
        },
    )


@pytest.fixture()
def store(tmp_path):
    return TokenStore.for_path(tmp_path / "tokens.db", Fernet.generate_key())


# ------------------------------------------------------------------
# cli.cmd_link_imap
# ------------------------------------------------------------------


def _args(**kw):
    base = dict(user="jimmy", account="ny@box.se", host="imap.box.se", port=None, imap_user=None,
                smtp_host=None, smtp_port=None, projects="alfa", verify=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _link(acl, store, *, stdin="pw\n", verify=None, **kw):
    from memaix_gateway import cli

    extra = {"verify": verify} if verify else {}
    return cli.cmd_link_imap(_args(**kw), stdin=io.StringIO(stdin), acl=acl, store=store, **extra)


def test_cli_empty_password_links_nothing(acl, store, capsys):
    assert _link(acl, store, stdin="\n") == 1
    assert "Tomt lösenord" in capsys.readouterr().err
    assert store.list_accounts("jimmy") == []


def test_cli_unknown_user_is_refused(acl, store, capsys):
    assert _link(acl, store, user="mallory") == 1
    assert "Okänd användare: mallory" in capsys.readouterr().err
    assert store.list_accounts("mallory") == []


@pytest.mark.parametrize("projects", ["", " , ,", None])
def test_cli_no_projects_is_refused(acl, store, capsys, projects):
    assert _link(acl, store, projects=projects) == 1
    assert "Ange --projects" in capsys.readouterr().err
    assert store.list_accounts("jimmy") == []


def test_cli_star_skips_the_access_check_and_scopes_everything(acl, store, capsys):
    assert _link(acl, store, user="eve", projects="*") == 0
    assert store.is_allowed("eve", "imap", "ny@box.se", "mail", "beta")
    assert "mejl delas med: *" in capsys.readouterr().out


def test_cli_access_check_runs_for_every_listed_project(acl, store, capsys):
    # first project fine, second not: nothing linked, the failing one named
    assert _link(acl, store, user="eve", projects="alfa, beta") == 1
    assert "eve har ingen åtkomst till projektet beta" in capsys.readouterr().err
    assert store.list_accounts("eve") == []


def test_cli_reader_grant_is_enough(acl, store):
    assert _link(acl, store, user="eve", projects="alfa") == 0


def test_cli_verify_gets_imap_user_default_and_port(acl, store):
    from memaix_gateway import cli

    calls = []
    rc = cli.cmd_link_imap(_args(verify=True, port=1993), stdin=io.StringIO("pw\n"), acl=acl, store=store,
                           verify=lambda *a: calls.append(a))
    assert rc == 0
    assert calls == [("imap.box.se", "ny@box.se", "pw", 1993)]


def test_cli_verify_uses_explicit_imap_user(acl, store):
    from memaix_gateway import cli

    calls = []
    rc = cli.cmd_link_imap(_args(verify=True, imap_user="login"), stdin=io.StringIO("pw\n"), acl=acl,
                           store=store, verify=lambda *a: calls.append(a))
    assert rc == 0 and calls[0][1] == "login"
    assert store.load_one("jimmy", "imap", "ny@box.se")["user"] == "login"


def test_cli_verify_is_skipped_without_flag(acl, store):
    from memaix_gateway import cli

    calls = []
    cli.cmd_link_imap(_args(), stdin=io.StringIO("pw\n"), acl=acl, store=store,
                      verify=lambda *a: calls.append(a))
    assert calls == []


def test_cli_access_denied_never_reaches_verify(acl, store):
    from memaix_gateway import cli

    calls = []
    rc = cli.cmd_link_imap(_args(user="eve", projects="beta", verify=True), stdin=io.StringIO("pw\n"),
                           acl=acl, store=store, verify=lambda *a: calls.append(a))
    assert rc == 1 and calls == []


def test_cli_loads_acl_and_store_from_server_when_not_given(acl, store, monkeypatch):
    from memaix_gateway import cli

    monkeypatch.setenv("TOKEN_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(server, "_get_acl", lambda: acl)
    monkeypatch.setattr(server, "_get_token_store", lambda: store)
    assert cli.cmd_link_imap(_args(), stdin=io.StringIO("pw\n")) == 0
    assert store.is_allowed("jimmy", "imap", "ny@box.se", "mail", "alfa")


def test_cli_master_key_is_checked_before_reading_stdin(monkeypatch):
    from memaix_gateway import cli

    monkeypatch.delenv("TOKEN_MASTER_KEY", raising=False)
    stdin = io.StringIO("pw\n")
    assert cli.cmd_link_imap(_args(), stdin=stdin) == 1
    assert stdin.tell() == 0


def test_cli_scopes_are_stripped(acl, store, capsys):
    assert _link(acl, store, projects=" alfa , beta ,") == 0
    assert capsys.readouterr().out.strip().endswith("mejl delas med: alfa, beta")


# ------------------------------------------------------------------
# mail_accounts.plan_sources / build_source / _smtp_settings
# ------------------------------------------------------------------


@pytest.fixture()
def registry():
    built: list = []
    reg = ConnectorRegistry()
    reg.register(
        ConnectorSpec(type="imap", capability="mail", auth="shared",
                      factory=lambda acl, p, u, cfg, tok: built.append(("imap", p, cfg.get("user"))) or "shared"),
        ConnectorSpec(type="gmailish", capability="mail", auth="per_user", provider="google",
                      factory=lambda acl, p, u, cfg, tok: built.append(("google", p, tok)) or "google"),
        ConnectorSpec(type="imap_user", capability="mail", auth="per_user", provider="imap",
                      factory=lambda acl, p, u, cfg, tok: built.append(("imap_user", p, tok["user"])) or "user"),
    )
    reg.built = built
    return reg


def _plan(acl, store, registry, user="jimmy", projects=None):
    return [(s.project, s.label, s.account) for s in mail_accounts.plan_sources(acl, store, registry, user, projects)]


def test_plan_shared_only_for_collaborator_projects_with_shared_spec(acl, store, registry):
    # alfa: shared imap. beta: mailbox type is a per_user spec -> not shared.
    # gamma: reader only -> left out entirely.
    assert _plan(acl, store, registry) == [("alfa", "imap:alfa", "delad@alfa.se")]
    assert _plan(acl, store, registry, user="eve") == []


def test_plan_unknown_mailbox_type_is_skipped(acl, store, registry):
    acl.projects["alfa"]["mailbox"]["type"] = "okänd"
    assert _plan(acl, store, registry) == []


def test_plan_linked_account_once_under_first_allowed_project(acl, store, registry):
    store.store("jimmy", "imap", "ny@box.se", {"host": "h", "user": "ny@box.se", "password": "pw"})
    store.set_scopes("jimmy", "imap", "ny@box.se", "mail", ["beta", "alfa"])
    plan = _plan(acl, store, registry)
    assert plan == [("alfa", "imap:alfa", "delad@alfa.se"), ("alfa", "imap_user:ny@box.se", "ny@box.se")]
    assert _plan(acl, store, registry, projects=["beta"]) == [("beta", "imap_user:ny@box.se", "ny@box.se")]


def test_plan_account_not_shared_with_any_candidate_is_skipped(acl, store, registry):
    store.store("jimmy", "imap", "ny@box.se", {"host": "h", "user": "ny@box.se", "password": "pw"})
    store.set_scopes("jimmy", "imap", "ny@box.se", "mail", ["gamma"])
    assert _plan(acl, store, registry) == [("alfa", "imap:alfa", "delad@alfa.se")]


def test_plan_skips_needs_relink_and_other_provider(acl, store, registry):
    store.store("jimmy", "imap", "trasig@box.se", {"host": "h", "user": "trasig@box.se", "password": "pw"})
    store.set_scopes("jimmy", "imap", "trasig@box.se", "mail", ["*"])
    store.mark_needs_relink("jimmy", "imap", "trasig@box.se")
    store.store("jimmy", "google", "g@gmail.com", {"access_token": "x"})
    store.set_scopes("jimmy", "google", "g@gmail.com", "mail", ["*"])
    plan = _plan(acl, store, registry)
    assert ("alfa", "gmailish:g@gmail.com", "g@gmail.com") in plan
    assert not any("trasig" in label for _p, label, _a in plan)
    assert [label for _p, label, _a in plan].count("gmailish:g@gmail.com") == 1


def test_plan_shared_box_wins_over_linked_account_with_same_address(acl, store, registry):
    store.store("jimmy", "imap", "Delad@alfa.se", {"host": "h", "user": "Delad@alfa.se", "password": "pw"})
    store.set_scopes("jimmy", "imap", "Delad@alfa.se", "mail", ["*"])
    assert _plan(acl, store, registry) == [("alfa", "imap:alfa", "delad@alfa.se")]


def test_plan_without_token_store_has_only_shared_boxes(acl, registry):
    assert _plan(acl, None, registry) == [("alfa", "imap:alfa", "delad@alfa.se")]


def test_plan_shared_box_without_user_uses_label_as_account(acl, store, registry):
    del acl.projects["alfa"]["mailbox"]["user"]
    assert _plan(acl, store, registry) == [("alfa", "imap:alfa", "imap:alfa")]


def test_plan_build_closures_bind_their_own_label(acl, store, registry):
    store.store("jimmy", "imap", "ny@box.se", {"host": "h", "user": "ny@box.se", "password": "pw"})
    store.set_scopes("jimmy", "imap", "ny@box.se", "mail", ["*"])
    first, second = mail_accounts.plan_sources(acl, store, registry, "jimmy")
    assert first.build() == "shared" and second.build() == "user"
    assert registry.built == [("imap", "alfa", "delad@alfa.se"), ("imap_user", "alfa", "ny@box.se")]


def test_build_source_rechecks_everything(acl, store, registry):
    with pytest.raises(ValueError, match="okänd lådtyp"):
        mail_accounts.build_source(acl, store, registry, "jimmy", "alfa", "nope:x")
    with pytest.raises(PermissionError, match="hör inte till"):
        mail_accounts.build_source(acl, store, registry, "jimmy", "beta", "imap:alfa")
    with pytest.raises(PermissionError, match="inte länkat"):
        mail_accounts.build_source(acl, store, registry, "jimmy", "alfa", "imap_user:ny@box.se")
    store.store("jimmy", "imap", "ny@box.se", {"host": "h", "user": "ny@box.se", "password": "pw"})
    with pytest.raises(PermissionError, match="inte delat"):
        mail_accounts.build_source(acl, store, registry, "jimmy", "alfa", "imap_user:ny@box.se")
    store.set_scopes("jimmy", "imap", "ny@box.se", "mail", ["alfa"])
    assert mail_accounts.build_source(acl, store, registry, "jimmy", "alfa", "imap_user:ny@box.se") == "user"
    store.mark_needs_relink("jimmy", "imap", "ny@box.se")
    with pytest.raises(PermissionError, match="inte länkat"):
        mail_accounts.build_source(acl, store, registry, "jimmy", "alfa", "imap_user:ny@box.se")


@pytest.fixture()
def secrets_map(monkeypatch):
    values: dict = {}
    monkeypatch.setattr(config, "secret", lambda ref: values.get(ref) if ref else None)
    return values


def test_smtp_settings_linked_account_defaults(acl, store, secrets_map):
    store.store("jimmy", "imap", "ny@box.se", {"host": "imap.box.se", "user": "ny@box.se", "password": "pw"})
    got = mail_accounts._smtp_settings(acl, store, "jimmy", "alfa", "imap_user:ny@box.se")
    assert got == ("smtp.box.se", 587, "ny@box.se", "pw")


def test_smtp_settings_linked_account_overrides(acl, store, secrets_map):
    store.store("jimmy", "imap", "ny@box.se", {"host": "mail.box.se", "user": "ny@box.se", "password": "pw",
                                               "smtp_host": "out.box.se", "smtp_port": "465",
                                               "smtp_user": "utgående"})
    got = mail_accounts._smtp_settings(acl, store, "jimmy", "alfa", "imap_user:ny@box.se")
    assert got == ("out.box.se", 465, "utgående", "pw")


@pytest.mark.parametrize("token", [None, {"host": "imap.box.se", "user": "ny@box.se"},
                                   {"host": "imap.box.se", "user": "", "password": "pw"}])
def test_smtp_settings_linked_account_without_login_is_refused(acl, store, secrets_map, token):
    if token is not None:
        store.store("jimmy", "imap", "ny@box.se", token)
    with pytest.raises(ValueError):
        mail_accounts._smtp_settings(acl, store, "jimmy", "alfa", "imap_user:ny@box.se")


def test_smtp_settings_linked_account_missing_password_message(acl, store, secrets_map):
    store.store("jimmy", "imap", "ny@box.se", {"host": "imap.box.se", "user": "ny@box.se"})
    with pytest.raises(ValueError, match="saknar inloggning för SMTP"):
        mail_accounts._smtp_settings(acl, store, "jimmy", "alfa", "imap_user:ny@box.se")


def test_smtp_settings_project_box_defaults(acl, store, secrets_map):
    secrets_map["env:ALFA_PW"] = "boxpw"
    got = mail_accounts._smtp_settings(acl, store, "jimmy", "alfa", "imap:alfa")
    assert got == ("smtp.alfa.se", 587, "delad@alfa.se", "boxpw")


def test_smtp_settings_project_smtp_resource_wins(acl, store, secrets_map):
    acl.projects["alfa"]["smtp"] = {"host": "out.alfa.se", "port": "465", "user": "sändare",
                                    "password_ref": "env:SMTP_PW"}
    secrets_map["env:SMTP_PW"] = "smtppw"
    secrets_map["env:ALFA_PW"] = "boxpw"
    got = mail_accounts._smtp_settings(acl, store, "jimmy", "alfa", "imap:alfa")
    assert got == ("out.alfa.se", 465, "sändare", "smtppw")


def test_smtp_settings_project_without_password_is_refused(acl, store, secrets_map):
    with pytest.raises(ValueError, match="inget lösenord konfigurerat"):
        mail_accounts._smtp_settings(acl, store, "jimmy", "alfa", "imap:alfa")


def test_smtp_settings_project_with_underivable_host_is_refused(acl, store, secrets_map):
    with pytest.raises(ValueError, match="okänd SMTP-server"):
        mail_accounts._smtp_settings(acl, store, "jimmy", "beta", "imap:beta")


def test_smtp_settings_other_types_are_refused(acl, store, secrets_map):
    with pytest.raises(ValueError, match="bara från IMAP-lådor"):
        mail_accounts._smtp_settings(acl, store, "jimmy", "alfa", "gmailish:g@gmail.com")


# ------------------------------------------------------------------
# mail_unlock: readable_addresses / exposed_identities / check_channel
# ------------------------------------------------------------------


def test_readable_addresses_covers_mailbox_and_smtp_and_named_users(acl, store):
    acl.projects["beta"]["smtp"] = {"user": "Login@Beta.se", "from_addr": "Kund Tjänst <tjanst@beta.se>"}
    acl.projects["delta"] = None
    store.store("jimmy", "imap", "ny@box.se", {"user": "ny@box.se"})
    store.store("eve", "imap", "eve@box.se", {"user": "eve@box.se"})
    got = mu.readable_addresses(acl, store)
    assert got == {"delad@alfa.se", "x@beta.se", "g@gamma.se", "login@beta.se", "tjanst@beta.se",
                   "ny@box.se", "eve@box.se"}
    assert "eve@box.se" not in mu.readable_addresses(acl, store, users=["jimmy"])
    assert mu.readable_addresses(acl, None) == {"delad@alfa.se", "x@beta.se", "g@gamma.se",
                                                "login@beta.se", "tjanst@beta.se"}


def test_readable_addresses_drops_empty_values():
    acl = SimpleNamespace(projects={"p": {"mailbox": {"user": "<>"}, "smtp": {"user": ""}}}, users={})
    assert mu.readable_addresses(acl, None) == set()


class _NotifyStore:
    def __init__(self, rows):
        self._rows = rows

    def all_channels(self):
        return self._rows


def test_exposed_identities_collects_ntfy_and_webhooks_and_skips_broken(monkeypatch):
    monkeypatch.setattr(config, "secret", lambda ref: "https://hook.example/ref" if ref == "env:H" else None)
    rows = [
        [{"type": "ntfy", "topic": "abc"}, {"type": "ntfy"}, {"type": "email", "to": "a@b.se"}],
        None,
        [{"type": "webhook", "url_ref": "env:H"}, {"type": "discord", "url": "https://discord.com/api/webhooks/9/t"},
         {"type": "webhook"}, None],
    ]
    got = mu.exposed_identities(_NotifyStore(rows))
    assert got == {"ntfy:ntfy.sh/abc", "url:hook.example/ref", "discord:9"}
    assert mu.exposed_identities(None) == set()


def test_exposed_identities_survives_a_spec_that_raises(monkeypatch):
    def boom(ref):
        raise RuntimeError("trasig ref")

    monkeypatch.setattr(config, "secret", boom)
    rows = [[{"type": "webhook", "url_ref": "env:X"}, {"type": "ntfy", "topic": "ok"}]]
    assert mu.exposed_identities(_NotifyStore(rows)) == {"ntfy:ntfy.sh/ok"}


def test_check_channel_ntfy_order_and_texts():
    with pytest.raises(ValueError, match="behöver 'topic'"):
        mu.check_channel({"type": "ntfy"}, set())
    with pytest.raises(ValueError, match="minst 24"):
        mu.check_channel({"type": "ntfy", "topic": "kort", "server": "http://x"}, set())
    with pytest.raises(ValueError, match="minst 24"):
        mu.check_channel({"type": "ntfy", "topic": _TOPIC[:-1] + "!"}, set())
    with pytest.raises(ValueError, match="server måste vara https"):
        mu.check_channel({"type": "ntfy", "topic": _TOPIC, "server": "HTTP://ntfy.example"},
                         set(), {mu.ntfy_identity("http://ntfy.example", _TOPIC)})
    with pytest.raises(ValueError, match="används också"):
        mu.check_channel({"type": "ntfy", "topic": _TOPIC, "server": "https://NTFY.sh/"},
                         set(), {mu.ntfy_identity(None, _TOPIC)})
    mu.check_channel({"type": "ntfy", "topic": _TOPIC, "server": "HTTPS://ntfy.example"}, set())


def test_check_channel_webhook_and_discord_order_and_texts():
    with pytest.raises(ValueError, match="webhook-kanalen behöver 'url'"):
        mu.check_channel({"type": "webhook"}, set())
    with pytest.raises(ValueError, match="discord-kanalens URL måste vara https"):
        mu.check_channel({"type": "discord", "url": "http://example.com/x"}, set())
    with pytest.raises(ValueError, match="kräver en Discord-webhook"):
        mu.check_channel({"type": "discord", "url": "https://example.com/api/webhooks/1/t"}, set(),
                         {"url:example.com/api/webhooks/1/t"})
    hook = "https://hook.example/a"
    with pytest.raises(ValueError, match="^webhook-kanalen pekar på samma webhook"):
        mu.check_channel({"type": "webhook", "url": hook}, set(), {mu.webhook_identity(hook)})
    disc = "https://discordapp.com/api/webhooks/42/tok"
    with pytest.raises(ValueError, match="^discord-kanalen pekar på samma webhook"):
        mu.check_channel({"type": "discord", "url": disc}, set(), {"discord:42"})
    mu.check_channel({"type": "webhook", "url": hook}, set())
    mu.check_channel({"type": "discord", "url": disc}, set())


@pytest.mark.parametrize("ctype", [None, "email", "Discord", "NTFY"])
def test_check_channel_unknown_types(ctype):
    with pytest.raises(ValueError, match="stöds inte för upplåsningskoder"):
        mu.check_channel({"type": ctype, "to": "a@b.se", "from": "c@d.se"}, set())


def test_check_channel_smtp_order_and_texts():
    readable = {"läsbar@box.se"}
    with pytest.raises(ValueError, match="saknar 'to'"):
        mu.check_channel({"type": "smtp", "from": "läsbar@box.se"}, readable)
    with pytest.raises(ValueError, match="saknar 'from'"):
        mu.check_channel({"type": "smtp", "to": "a@annan.se"}, readable)
    with pytest.raises(ValueError, match=r"smtp-kanalens to \(läsbar@box.se\) är \(ett alias"):
        mu.check_channel({"type": "smtp", "to": "Läsbar@Box.se", "from": "läsbar@box.se"}, readable)
    with pytest.raises(ValueError, match=r"smtp-kanalens from \(x@box.se\) ligger på en domän"):
        mu.check_channel({"type": "smtp", "to": "a@annan.se", "from": "x@box.se"}, readable)
    with pytest.raises(ValueError, match=r"smtp-kanalens user \(läsbar@box.se\)"):
        mu.check_channel({"type": "smtp", "to": "a@annan.se", "from": "b@annan.se", "user": "läsbar@box.se"},
                         readable)
    mu.check_channel({"type": "smtp", "to": "a@annan.se", "from": "b@annan.se", "user": ""}, readable)


# ------------------------------------------------------------------
# mail_client: build_forward / reply_recipients / flag constants
# ------------------------------------------------------------------

_RAW = b"From: a@b.se\r\nTo: c@d.se\r\nSubject: Hej\r\n\r\nText\r\n"


def _m(**kw):
    base = dict(subject="Hej", from_="Kund <kund@ex.se>", to=("info@box.se",), cc=(), reply_to=(),
                date_str="idag", text="Originaltext", html="", attachments=[],
                obj=email.message_from_bytes(_RAW, policy=policy.default), headers={})
    base.update(kw)
    return SimpleNamespace(**base)


def test_forward_refuses_unknown_mode():
    with pytest.raises(ValueError, match="mode måste vara"):
        mc.build_forward(_m(), "me@box.se", ["x@y.se"], mode="bilaga")


def test_forward_attachment_mode_errors_and_note():
    with pytest.raises(ValueError, match="kan inte bifogas"):
        mc.build_forward(_m(obj=None), "me@box.se", ["x@y.se"], mode="attachment")
    big = SimpleNamespace(as_bytes=lambda: b"x" * (mc.MAX_FORWARD_BYTES + 1))
    with pytest.raises(ValueError, match="för stort"):
        mc.build_forward(_m(obj=big), "me@box.se", ["x@y.se"], mode="attachment")
    exact = SimpleNamespace(as_bytes=lambda: b"x" * mc.MAX_FORWARD_BYTES)
    mc.build_forward(_m(obj=exact), "me@box.se", ["x@y.se"], mode="attachment")
    msg = mc.build_forward(_m(), "me@box.se", ["x@y.se", "z@y.se"], note="Se nedan", mode="attachment")
    assert msg["To"] == "x@y.se, z@y.se" and msg["Subject"] == "Fwd: Hej"
    parts = list(msg.iter_parts())
    assert parts[0].get_content().strip() == "Se nedan"
    assert parts[1].get_filename() == "vidarebefordrat.eml"


def test_forward_inline_body_and_attachments():
    atts = [
        SimpleNamespace(payload=b"pdf", content_type="application/pdf", filename="a.pdf"),
        SimpleNamespace(payload=b"<html>", content_type="text/html", filename=None),
        SimpleNamespace(payload=b"x", content_type=None, filename="b"),
        SimpleNamespace(payload=b"y", content_type="image", filename="c"),
    ]
    msg = mc.build_forward(_m(attachments=atts, subject="VB: Hej"), "me@box.se", ["x@y.se"])
    assert msg["Subject"] == "VB: Hej"
    parts = list(msg.iter_parts())
    body = parts[0].get_content()
    assert body.startswith("---------- Vidarebefordrat meddelande ----------\nFrån: Kund <kund@ex.se>")
    assert "Till: info@box.se" in body and body.rstrip().endswith("Originaltext")
    got = [(p.get_content_type(), p.get_filename()) for p in parts[1:]]
    assert got == [("application/pdf", "a.pdf"), ("application/octet-stream", "bilaga"),
                   ("application/octet-stream", "b"), ("image/octet-stream", "c")]
    with_note = mc.build_forward(_m(), "me@box.se", ["x@y.se"], note="FYI").get_content()
    assert with_note.startswith("FYI\n\n---------- Vidarebefordrat")


def test_forward_inline_attachment_cap_is_cumulative():
    half = mc.MAX_FORWARD_BYTES // 2
    ok = [SimpleNamespace(payload=b"a" * half, content_type="application/pdf", filename="1"),
          SimpleNamespace(payload=b"b" * (mc.MAX_FORWARD_BYTES - half), content_type="application/pdf", filename="2")]
    mc.build_forward(_m(attachments=ok), "me@box.se", ["x@y.se"])
    over = ok + [SimpleNamespace(payload=b"c", content_type="application/pdf", filename="3")]
    with pytest.raises(ValueError, match="prova mode='attachment'"):
        mc.build_forward(_m(attachments=over), "me@box.se", ["x@y.se"])


def test_reply_recipients_reply_to_rules():
    m = _m(reply_to=("Annan <annan@ex.se>",))
    with pytest.raises(mc.ReplyToDiffers) as err:
        mc.reply_recipients(m, "info@box.se", False)
    assert err.value.from_addr == "Kund <kund@ex.se>" and err.value.reply_to == ["Annan <annan@ex.se>"]
    assert mc.reply_recipients(m, "info@box.se", False, confirm_reply_to=True) == (["Annan <annan@ex.se>"], [])
    same = _m(reply_to=("KUND@ex.se", ""))
    assert mc.reply_recipients(same, "info@box.se", False) == (["KUND@ex.se"], [])


def test_reply_recipients_reply_all_dedupes_and_drops_self():
    m = _m(from_="kund@ex.se", to=("INFO@box.se", "b@ex.se", "Kund@ex.se"), cc=("b@ex.se", "", "c@ex.se"))
    assert mc.reply_recipients(m, "Info@Box.se", True) == (["kund@ex.se"], ["b@ex.se", "c@ex.se"])
    assert mc.reply_recipients(m, "info@box.se", False) == (["kund@ex.se"], [])


def test_reply_recipients_promotes_first_cc_when_from_is_self():
    m = _m(from_="info@box.se", to=("a@ex.se",), cc=("b@ex.se",))
    assert mc.reply_recipients(m, "info@box.se", True) == (["a@ex.se"], ["b@ex.se"])
    with pytest.raises(ValueError, match="hittar ingen mottagare"):
        mc.reply_recipients(m, "info@box.se", False)
    with pytest.raises(ValueError, match="hittar ingen mottagare"):
        mc.reply_recipients(_m(from_="", to=()), "info@box.se", True)


def test_reply_recipients_cap_counts_after_promotion():
    cc = tuple(f"k{i}@ex.se" for i in range(4))
    from_self = _m(from_="info@box.se", to=(), cc=cc)
    assert mc.reply_recipients(from_self, "info@box.se", True, max_cc=3) == (["k0@ex.se"], list(cc[1:]))
    other = _m(to=(), cc=cc)
    with pytest.raises(ValueError, match="kopia till 4 mottagare, taket är 3"):
        mc.reply_recipients(other, "info@box.se", True, max_cc=3)
    assert mc.reply_recipients(other, "info@box.se", True, max_cc=4)[1] == list(cc)


def test_flag_actions_are_the_imap_system_flags():
    assert mc.FLAG_ACTIONS == {
        "read": ("\\Seen", True), "unread": ("\\Seen", False),
        "flag": ("\\Flagged", True), "unflag": ("\\Flagged", False),
    }


# ------------------------------------------------------------------
# notify.channels: mask_channel / build_channels
# ------------------------------------------------------------------


def test_mask_channel_every_branch():
    spec = {
        "type": "ntfy", "topic": "hemligtämne", "url": "https://hook.example:8443/x/y?q=1",
        "url_ref": "env:U", "token": "t", "password": "p", "server": "https://ntfy.sh", "port": 5,
    }
    assert ch.mask_channel(spec) == {
        "type": "ntfy", "topic": "he***", "url": "https://hook.example/***", "url_ref": "***",
        "token": "***", "password": "***", "server": "https://ntfy.sh", "port": 5,
    }
    assert ch.mask_channel({"url": "inget-schema", "topic": ""}) == {"url": "***", "topic": ""}
    assert ch.mask_channel({"url": None, "topic": 12345}) == {"url": None, "topic": 12345}
    assert ch.mask_channel({"url": "mailto:x@y.se"}) == {"url": "mailto:///***"}
    assert ch.mask_channel(None) == {}


def test_build_channels_reasons_and_order():
    acl = Acl(users={"jimmy": {"grants": {"alfa": "owner", "beta": "reader"}}},
              projects={"alfa": {}, "beta": {}})
    rejected: list = []
    specs = [
        {"type": "email", "to": "a@b.se", "project": "beta"},
        {"type": "email", "to": "a@b.se", "project": "alfa"},
        {"type": "webhook"},
        {"type": "webhook", "url": "https://hook.example/x"},
        {"type": "ntfy"},
        {"type": "ntfy", "topic": "t"},
        {"type": "sms"},
        {"type": "email", "project": "alfa"},
        {"type": "webhook", "url": "http://discord.com/api/webhooks/1/t"},
        {"type": "webhook", "url": "http://127.0.0.1/x"},
    ]
    built = ch.build_channels(specs, acl=acl, user="jimmy", _http=object(), rejected=rejected)
    assert [type(c).__name__ for c in built] == ["EmailChannel", "WebhookChannel", "NtfyChannel"]
    assert rejected == [
        {"index": 0, "type": "email", "reason": "du är inte ägare av projektet som e-posten skickas från"},
        {"index": 2, "type": "webhook", "reason": "kanalen kunde inte byggas (ValueError)"},
        {"index": 4, "type": "ntfy", "reason": "kanalen kunde inte byggas (KeyError)"},
        {"index": 6, "type": "okänd", "reason": "okänd kanaltyp"},
        {"index": 7, "type": "email", "reason": "kanalen kunde inte byggas (KeyError)"},
        {"index": 8, "type": "webhook", "reason": "Discord-webhooks måste vara https på standardport"},
        {"index": 9, "type": "webhook", "reason": "URL:en vägras (SSRF-skydd eller tvetydig adress)"},
    ]


def test_build_channels_email_needs_acl_user_and_project():
    acl = Acl(users={"jimmy": {"grants": {"alfa": "owner"}}}, projects={"alfa": {}})
    spec = [{"type": "email", "to": "a@b.se", "project": "alfa"}]
    assert ch.build_channels(spec, acl=None, user="jimmy") == []
    assert ch.build_channels(spec, acl=acl, user=None) == []
    assert ch.build_channels([{"type": "email", "to": "a@b.se"}], acl=acl, user="jimmy") == []
    assert len(ch.build_channels(spec, acl=acl, user="jimmy")) == 1
    assert ch.build_channels(None) == []


def test_build_channels_webhook_url_ref_and_no_rejected_list(monkeypatch):
    monkeypatch.setattr(config, "secret", lambda ref: "https://hook.example/ref" if ref == "env:H" else None)
    built = ch.build_channels([{"type": "webhook", "url_ref": "env:H"}, {"type": "sms"}], _http=object())
    assert len(built) == 1


# ------------------------------------------------------------------
# server._validate_channels
# ------------------------------------------------------------------


def test_validate_channels_refs_checked_before_type():
    with pytest.raises(ValueError, match=r"channel fields \['token_ref', 'url_ref'\] are not allowed"):
        server._validate_channels([{"type": "sms", "url_ref": "env:X", "token_ref": "env:Y"}])
    with pytest.raises(ValueError, match="unknown channel type 'sms'"):
        server._validate_channels([{"type": "sms"}])
    server._validate_channels(None)
    server._validate_channels([{"type": "email", "to": "a@b.se"}])


def test_validate_channels_ntfy_topic_and_https():
    with pytest.raises(ValueError, match="ntfy topic must be"):
        server._validate_channels([{"type": "ntfy", "topic": "har mellanslag"}])
    with pytest.raises(ValueError, match="ntfy topic must be"):
        server._validate_channels([{"type": "ntfy"}])
    with pytest.raises(ch.ChannelRefused, match="ntfy måste vara https"):
        server._validate_channels([{"type": "ntfy", "topic": "abc", "server": "http://ntfy.example"}])
    with pytest.raises(ValueError, match="channel url avvisad"):
        server._validate_channels([{"type": "ntfy", "topic": "abc", "server": "https://127.0.0.1"}])
    server._validate_channels([{"type": "ntfy", "topic": "abc"}])


def test_validate_channels_webhook_urls():
    server._validate_channels([{"type": "webhook"}])
    server._validate_channels([{"type": "webhook", "url": "http://hook.example/x"}])
    with pytest.raises(ValueError, match="channel url avvisad"):
        server._validate_channels([{"type": "webhook", "url": "http://127.0.0.1/x"}])
    with pytest.raises(ch.ChannelRefused, match="Discord-webhooks"):
        server._validate_channels([{"type": "webhook", "url": "http://discord.com/api/webhooks/1/t"}])
    with pytest.raises(ValueError, match="ntfy topic must be"):
        server._validate_channels([{"type": "webhook", "url": "https://hook.example/x"}, {"type": "ntfy"}])
