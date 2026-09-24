# SPDX-License-Identifier: AGPL-3.0-or-later
"""The IMAP-criteria parser the REST mail adapters share. The contract is
tools/email.py's output — every criterion it builds must round-trip, and
anything else must be refused rather than read as "ALL"."""

from __future__ import annotations

import datetime

import pytest

from memaix_gateway.connectors.adapters.mail_criteria import Criteria, UnsupportedCriteria, parse
from memaix_gateway.tools.email import _imap_quote, _to_imap_date


def test_all_and_empty_mean_no_filter():
    assert parse("ALL") == Criteria()
    assert parse("") == Criteria()


def test_uid_takes_the_rest_of_the_string():
    assert parse("UID 18f2c0ab") == Criteria(uid="18f2c0ab")


def test_what_email_search_builds_round_trips():
    """The exact string email_search emits for every argument at once."""
    criteria = " ".join([
        f"SINCE {_to_imap_date('2026-09-01')}",
        f"BEFORE {_to_imap_date('2026-10')}",
        f'FROM "{_imap_quote("noreply@loopia.se")}"',
        f'TEXT "{_imap_quote(chr(34) + "quoted" + chr(34) + " and a back" + chr(92) + "slash")}"',
    ])
    assert parse(criteria) == Criteria(
        since=datetime.date(2026, 9, 1),
        before=datetime.date(2026, 10, 1),
        from_="noreply@loopia.se",
        text='"quoted" and a back\\slash',
    )


@pytest.mark.parametrize(
    "criteria",
    [
        "UID ",
        'BODY "x"',
        "UNSEEN",
        "SINCE",
        "SINCE 2026-09-01",
        "SINCE 31-Foo-2026",
        "FROM bare",
        'TEXT "never closed',
    ],
)
def test_everything_else_is_refused(criteria):
    with pytest.raises(UnsupportedCriteria):
        parse(criteria)
