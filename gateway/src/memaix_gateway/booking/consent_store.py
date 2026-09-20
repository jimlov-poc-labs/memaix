# SPDX-License-Identifier: AGPL-3.0-or-later
"""GDPR consent log for public bookings — memaix-src card 01cf3b74.

booking_create writes no local record of a booking anywhere — the visitor's
PII (name, purpose, email as attendee) lives only on the calendar event
itself. That's a problem twice over: there is nothing to prove *that* and
*when* a visitor consented, and there is no reliable way to tell a booking
event apart from any other event on the host's calendar once it's time to
purge it. This store is the one place that solves both: it records consent
alongside the calendar event's id and the meeting's end time, so purge.py
can find events that are actually bookings and are actually old enough,
without ever having to guess from calendar content.

Mirrors _PendingStateSQLite (tools/account.py) and ActionQueue
(outbox/queue.py): SQLite, WAL mode, one threading.Lock, a fresh connection
per operation. Same conventions as everything else that needs
process/restart-durable local state in this gateway.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
import uuid
from pathlib import Path

from ..paths import data_dir as _data_dir

_DEFAULT_DB_PATH = str(_data_dir() / "memaix-consent.db")
RETENTION_DAYS = 365


class ConsentStore:
    """SQLite-backed log of booking consent, keyed by an internal row id
    (not the calendar event id, which purge.py deletes and which a
    Google/CalDAV adapter may not have even generated at record() time)."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS booking_consent (
                    id            TEXT PRIMARY KEY,
                    project       TEXT NOT NULL,
                    host_user     TEXT NOT NULL,
                    event_id      TEXT,
                    visitor_email TEXT,
                    consent_text  TEXT NOT NULL,
                    consent_at    INTEGER NOT NULL,
                    meeting_end   INTEGER NOT NULL,
                    purged_at     INTEGER
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_consent_due "
                "ON booking_consent(meeting_end) WHERE purged_at IS NULL"
            )
            # Additive migration for card 8056150d (reschedule/cancel).
            # SQLite has no "ADD COLUMN IF NOT EXISTS" — probe and ignore
            # the OperationalError on a database that already has them.
            for ddl in (
                "ALTER TABLE booking_consent ADD COLUMN slug TEXT",
                "ALTER TABLE booking_consent ADD COLUMN manage_token TEXT",
                "ALTER TABLE booking_consent ADD COLUMN status TEXT NOT NULL DEFAULT 'confirmed'",
                # Additive migration for card ecffcb5b (reminders). meeting_start
                # is nullable: rows written before this migration have none, and
                # reminders.py treats a NULL start as "can't schedule, skip" —
                # those bookings simply predate the feature. reminders_sent is a
                # comma-joined set of offsets (minutes) already dispatched, e.g.
                # "1440,60" — the durable idempotency ledger reminders.py claims
                # against before sending, so a reminder never double-fires across
                # restarts or overlapping ticks.
                "ALTER TABLE booking_consent ADD COLUMN meeting_start INTEGER",
                "ALTER TABLE booking_consent ADD COLUMN reminders_sent TEXT NOT NULL DEFAULT ''",
                # Additive migration for card 85854d2c (meeting forms). All
                # three nullable: rows written before this migration have
                # none, and every consumer (reminders, confirmation/.ics)
                # treats NULL as "no video/phone info to render" — same
                # pattern as meeting_start above. meeting_form_detail is the
                # value *resolved at booking time* (a Meet/Zoom URL or a
                # phone number), never re-resolved later — if a host swaps
                # Zoom accounts, past bookings still show what the visitor
                # actually received.
                "ALTER TABLE booking_consent ADD COLUMN meeting_form_slug TEXT",
                "ALTER TABLE booking_consent ADD COLUMN meeting_form_provider TEXT",
                "ALTER TABLE booking_consent ADD COLUMN meeting_form_detail TEXT",
            ):
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError:
                    pass
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_consent_manage_token "
                "ON booking_consent(manage_token) WHERE manage_token IS NOT NULL"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_reminder_due "
                "ON booking_consent(meeting_start) "
                "WHERE purged_at IS NULL AND status != 'cancelled'"
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def record(
        self,
        *,
        project: str,
        host_user: str,
        event_id: str | None,
        visitor_email: str,
        consent_text: str,
        consent_at: int,
        meeting_end: int,
        slug: str | None = None,
        meeting_start: int | None = None,
        meeting_form_slug: str | None = None,
        meeting_form_provider: str | None = None,
        meeting_form_detail: str | None = None,
    ) -> tuple[str, str]:
        """Returns (row_id, manage_token). manage_token is the capability a
        booker or host later presents to /booking/{token}/... to reschedule
        or cancel this booking — see card 8056150d."""
        row_id = uuid.uuid4().hex
        manage_token = secrets.token_urlsafe(32)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO booking_consent "
                "(id, project, host_user, event_id, visitor_email, consent_text, "
                " consent_at, meeting_end, purged_at, slug, manage_token, status, meeting_start, "
                " meeting_form_slug, meeting_form_provider, meeting_form_detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 'confirmed', ?, ?, ?, ?)",
                (row_id, project, host_user, event_id, visitor_email, consent_text,
                 consent_at, meeting_end, slug, manage_token, meeting_start,
                 meeting_form_slug, meeting_form_provider, meeting_form_detail),
            )
            conn.commit()
        return row_id, manage_token

    def get_by_manage_token(self, manage_token: str) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT id, project, host_user, event_id, visitor_email, slug, status, "
                "meeting_start, meeting_end, meeting_form_slug, meeting_form_provider, "
                "meeting_form_detail FROM booking_consent WHERE manage_token = ?",
                (manage_token,),
            ).fetchone()
        return dict(row) if row else None

    def update_booking(
        self, row_id: str, *, event_id: str | None, meeting_start: int | None, meeting_end: int, status: str,
    ) -> None:
        """Reschedule moves meeting_start/meeting_end (purge.py's due() and
        reminders.py's reminders_due() key off meeting_end/meeting_start
        respectively) and may keep the same event_id (an in-place
        calendar_update) or a new one; cancel just flips status so a
        cancelled booking can't be rescheduled later.

        reminders_sent is reset to '' on every call: a reschedule invalidates
        any reminder already fired for the old time (the new time needs its
        own fresh 24h/60min reminders), and a cancel takes the row out of
        reminders_due() entirely via its 'status != cancelled' predicate, so
        the reset there is inert but harmless."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE booking_consent SET event_id = ?, meeting_start = ?, meeting_end = ?, "
                "status = ?, reminders_sent = '' WHERE id = ?",
                (event_id, meeting_start, meeting_end, status, row_id),
            )
            conn.commit()

    def reminders_due(self, now_epoch: int, offsets_min: tuple[int, ...]) -> list[dict]:
        """Rows with a future-or-recent meeting_start that haven't been
        purged or cancelled. Coarse SQL filter only — reminders.due_offsets()
        does the per-offset "is this one due and unsent" decision in Python,
        same split as purge_due()/store.due()."""
        max_offset = max(offsets_min) if offsets_min else 0
        # Rows whose meeting started more than max_offset+1 day ago can never
        # have a still-due reminder — narrows the scan without affecting
        # correctness (due_offsets() re-checks precisely against now_epoch).
        floor = now_epoch - (max_offset + 1440) * 60
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id, project, host_user, event_id, visitor_email, slug, "
                "manage_token, meeting_start, meeting_end, reminders_sent, "
                "meeting_form_provider, meeting_form_detail FROM booking_consent "
                "WHERE meeting_start IS NOT NULL AND meeting_start > ? "
                "AND purged_at IS NULL AND status != 'cancelled'",
                (floor,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_reminder_sent(self, row_id: str, offset_min: int) -> bool:
        """Append offset_min to reminders_sent if not already present.
        Returns True if this call added it (caller should send), False if
        another tick/worker already claimed it (caller should skip). The
        check-and-append happens under self._lock so it's a real claim, same
        principle as notify/scheduler.py's claim()."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT reminders_sent FROM booking_consent WHERE id = ?", (row_id,),
            ).fetchone()
            if row is None:
                return False
            sent = {s for s in row["reminders_sent"].split(",") if s}
            if str(offset_min) in sent:
                return False
            sent.add(str(offset_min))
            conn.execute(
                "UPDATE booking_consent SET reminders_sent = ? WHERE id = ?",
                (",".join(sorted(sent, key=int)), row_id),
            )
            conn.commit()
            return True

    def due(self, now_epoch: int, retention_days: int = RETENTION_DAYS) -> list[dict]:
        """Rows whose meeting ended more than *retention_days* ago and
        haven't been purged yet."""
        cutoff = now_epoch - retention_days * 86400
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id, project, host_user, event_id, visitor_email "
                "FROM booking_consent WHERE meeting_end < ? AND purged_at IS NULL",
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_purged(self, row_id: str, when: int) -> None:
        """Nulls the visitor's email (the only PII this store ever holds)
        and stamps purged_at. The row itself stays — consent_text,
        consent_at and meeting_end remain as proof consent existed and was
        honored, without retaining the data that made it identifying."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE booking_consent SET visitor_email = NULL, purged_at = ? WHERE id = ?",
                (when, row_id),
            )
            conn.commit()


_store: ConsentStore | None = None


def get_consent_store() -> ConsentStore:
    global _store
    if _store is None:
        db_path = Path(os.environ.get("MEMAIX_CONSENT_DB", _DEFAULT_DB_PATH))
        _store = ConsentStore(db_path)
    return _store
