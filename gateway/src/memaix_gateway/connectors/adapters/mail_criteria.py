# SPDX-License-Identifier: AGPL-3.0-or-later
"""Parse the IMAP search criteria tools/email.py builds, for the REST mail
adapters (Gmail API, Microsoft Graph) that have to translate them.

tools/email.py speaks imap_tools: a real IMAP server receives its criteria
string verbatim. The REST adapters do not have an IMAP server behind them,
so they parse the string into a `Criteria` and build their own query from
it. The grammar is exactly what tools/email.py emits and nothing more:

    ALL
    UID <id>                     (the whole rest of the string is the id)
    SINCE <dd-Mon-yyyy>          (inclusive, as in IMAP)
    BEFORE <dd-Mon-yyyy>         (exclusive, as in IMAP)
    FROM "<quoted>"
    TEXT "<quoted>"

in any combination, space separated. Anything else raises
UnsupportedCriteria instead of silently degrading to "ALL" — that fallback
is how email_search used to return the newest N messages unfiltered while
looking like an answer.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1,
)}


class UnsupportedCriteria(ValueError):
    """The criteria string uses something this adapter cannot translate."""


@dataclass(frozen=True)
class Criteria:
    uid: str | None = None
    since: datetime.date | None = None
    before: datetime.date | None = None
    from_: str | None = None
    text: str | None = None


def _parse_date(value: str) -> datetime.date:
    try:
        day, mon, year = value.split("-")
        return datetime.date(int(year), _MONTHS[mon.lower()], int(day))
    except (ValueError, KeyError) as exc:
        raise UnsupportedCriteria(f"invalid IMAP date {value!r}") from exc


def _read_atom(s: str, i: int) -> tuple[str, int]:
    j = i
    while j < len(s) and not s[j].isspace():
        j += 1
    if j == i:
        raise UnsupportedCriteria(f"missing value at position {i} in {s!r}")
    return s[i:j], j


def _read_quoted(s: str, i: int) -> tuple[str, int]:
    """Read an IMAP quoted-string starting at s[i] == '"', undoing the
    backslash escaping tools/email.py's _imap_quote applied."""
    if i >= len(s) or s[i] != '"':
        raise UnsupportedCriteria(f"expected a quoted string at position {i} in {s!r}")
    out: list[str] = []
    j = i + 1
    while j < len(s):
        ch = s[j]
        if ch == "\\" and j + 1 < len(s):
            out.append(s[j + 1])
            j += 2
            continue
        if ch == '"':
            return "".join(out), j + 1
        out.append(ch)
        j += 1
    raise UnsupportedCriteria(f"unterminated quoted string in {s!r}")


def _parse_term(s: str, i: int, dates: dict[str, datetime.date], strings: dict[str, str]) -> int:
    """Parse one `KEYWORD [value]` term starting at s[i]; return the index after it."""
    keyword, i = _read_atom(s, i)
    keyword = keyword.upper()
    while i < len(s) and s[i].isspace():
        i += 1
    if keyword == "ALL":
        return i
    if keyword in ("SINCE", "BEFORE"):
        value, i = _read_atom(s, i)
        dates[keyword] = _parse_date(value)
        return i
    if keyword in ("FROM", "TEXT"):
        value, i = _read_quoted(s, i)
        strings[keyword] = value
        return i
    raise UnsupportedCriteria(
        f"unsupported mail search criterion {keyword!r} "
        "(supported: ALL, UID, SINCE, BEFORE, FROM, TEXT)"
    )


def parse(criteria: str) -> Criteria:
    """Parse `criteria` into a Criteria, or raise UnsupportedCriteria."""
    s = (criteria or "").strip()
    if s.upper().startswith("UID "):
        uid = s[len("UID "):].strip()
        if not uid:
            raise UnsupportedCriteria("UID criterion without an id")
        return Criteria(uid=uid)

    dates: dict[str, datetime.date] = {}
    strings: dict[str, str] = {}
    i = 0
    while i < len(s):
        if s[i].isspace():
            i += 1
        else:
            i = _parse_term(s, i, dates, strings)
    return Criteria(
        since=dates.get("SINCE"), before=dates.get("BEFORE"),
        from_=strings.get("FROM"), text=strings.get("TEXT"),
    )
