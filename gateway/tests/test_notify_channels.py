# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for notify.channels — no real network/SMTP traffic."""

from __future__ import annotations

from memaix_gateway.acl import Acl
from memaix_gateway.notify.channels import (
    EmailChannel,
    WebhookChannel,
    build_channels,
)


class _FakeResponse:
    def __init__(self):
        self.raised = False

    def raise_for_status(self):
        self.raised = True


class _FakeHttp:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _FakeResponse()


class _FakeSmtp:
    def __init__(self):
        self.sent = []

    def send_message(self, msg):
        self.sent.append(msg)


def test_webhook_slack_format_posts_text_payload():
    http = _FakeHttp()
    ch = WebhookChannel("https://hooks.slack.example/xyz", "slack", _http=http)
    ch.send("Subject", "**md**", "plain text")
    url, kwargs = http.calls[0]
    assert url == "https://hooks.slack.example/xyz"
    assert "Subject" in kwargs["json"]["text"]
    assert "plain text" in kwargs["json"]["text"]


def test_webhook_discord_format_posts_embed_payload():
    http = _FakeHttp()
    ch = WebhookChannel("https://discord.com/api/webhooks/1/abc", "discord", _http=http)
    ch.send("Subject", "**md**", "plain text")
    _, kwargs = http.calls[0]
    embed = kwargs["json"]["embeds"][0]
    assert embed["title"] == "Subject"
    assert embed["description"] == "**md**"
    assert embed["color"] == 0x2ECC71


def test_webhook_discord_format_truncates_long_description():
    http = _FakeHttp()
    ch = WebhookChannel("https://discord.com/api/webhooks/1/abc", "discord", _http=http)
    ch.send("Subject", "x" * 5000, "plain text")
    _, kwargs = http.calls[0]
    desc = kwargs["json"]["embeds"][0]["description"]
    assert len(desc) == 4096
    assert desc.endswith("…")


def test_webhook_raw_format_posts_structured_payload():
    http = _FakeHttp()
    ch = WebhookChannel("https://example.com/hook", "raw", _http=http)
    ch.send("Subj", "md", "txt")
    _, kwargs = http.calls[0]
    assert kwargs["json"] == {"subject": "Subj", "text": "txt", "markdown": "md"}


def test_email_channel_uses_injected_smtp():
    acl = Acl(
        users={"alice": {"grants": {"shared": "owner"}}},
        projects={"shared": {"vault": "/v", "mailbox": {"host": "h", "user": "bot@x.com"}}},
    )
    smtp = _FakeSmtp()
    ch = EmailChannel(acl, {"to": "alice@personal.com", "project": "shared"}, _smtp=smtp)
    ch.send("Subj", "md", "text body")
    assert len(smtp.sent) == 1
    assert smtp.sent[0]["To"] == "alice@personal.com"
    assert smtp.sent[0]["Subject"] == "Subj"


def test_email_channel_requires_project_with_mailbox():
    acl = Acl(users={"alice": {"grants": {}}}, projects={})
    ch = EmailChannel(acl, {"to": "a@b.com"}, _smtp=_FakeSmtp())
    try:
        ch.send("s", "m", "t")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_build_channels_skips_broken_spec_but_builds_others():
    acl = Acl(
        users={"alice": {"grants": {}}},
        projects={},
    )
    specs = [
        {"type": "webhook"},  # missing url -> should be skipped, not raise
        {"type": "webhook", "url": "https://example.com/hook"},
    ]
    channels = build_channels(specs, acl=acl)
    assert len(channels) == 1
    assert isinstance(channels[0], WebhookChannel)


def test_build_channels_unknown_type_is_skipped():
    channels = build_channels([{"type": "carrier-pigeon"}])
    assert channels == []


# ── SSRF: redirects får inte följas ──────────────────────────────────────────
#
# validate_external_url() prövar bara URL:en vi skickar. Följer klienten en 3xx
# ser guarden aldrig målet, och en validerad publik värd kan svara
#   302 Location: http://169.254.169.254/latest/meta-data/...
# En 307 bevarar dessutom POST-body och skickar hela nyttolasten vidare.
#
# Testerna nedan låser fast att båda kanalerna skickar allow_redirects=False.
# Motsvarande anrop i tools/calendar.py:170 använder requests direkt utan
# injektionspunkt och täcks därför inte här — se safety/net.py:s docstring för
# den fullständiga listan över call sites.


def test_webhook_does_not_follow_redirects():
    http = _FakeHttp()
    ch = WebhookChannel("https://example.com/hook", "raw", _http=http)
    ch.send("Subj", "md", "txt")
    _, kwargs = http.calls[0]
    assert kwargs["allow_redirects"] is False
