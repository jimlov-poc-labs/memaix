# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pluggable meeting-form providers — memaix-src card 85854d2c.

resolve_meeting_detail() is booking_create's single entry point for
turning a host's configured meeting form (connectors/meeting_forms.py)
into what a visitor actually gets: a Google Meet link, a Zoom link, or a
phone number. Adding a new provider later is one adapter class + one
_PROVIDERS entry — nothing in booking_create's core flow changes.

Google Meet is the odd one out: its link is a side effect of the
calendar_create call itself (conferenceData), not an independent API call
like Zoom. _GoogleMeetProvider.resolve() reflects that — it expects the
calendar event (already created with want_conference=True) passed in via
calendar_event, and only extracts the link Google already returned;
booking_create must call calendar_create(want_conference=True) *before*
asking this provider to resolve, whereas Zoom/phone must resolve *before*
calendar_create so their detail can be embedded in the event's
location/description.
"""

from __future__ import annotations

from typing import Protocol, TypedDict


class MeetingProviderError(Exception):
    """Raised when a provider fails to produce a meeting detail. Callers
    (booking_create) should treat this as fail-closed — never silently
    create a booking with no working video/phone info."""


class _Provider(Protocol):
    def resolve(
        self, acl, project, host_user, form_config,
        *, start, end, title, calendar_event=None,
    ) -> "MeetingDetail": ...


class MeetingDetail(TypedDict):
    provider: str
    join_url: str | None
    phone_number: str | None
    display_text: str


class _PhoneProvider:
    def resolve(self, acl, project, host_user, form_config, **kw) -> MeetingDetail:  # NOSONAR: parameters are fixed by the shared provider interface
        phone_number = form_config.get("phone_number")
        if not phone_number:
            raise MeetingProviderError("phone form has no phone_number configured")
        return MeetingDetail(
            provider="phone", join_url=None, phone_number=phone_number,
            display_text=f"Ring: {phone_number}",
        )


class _GoogleMeetProvider:
    def resolve(self, acl, project, host_user, form_config, *, calendar_event=None, **kw) -> MeetingDetail:  # NOSONAR: parameters are fixed by the shared provider interface
        meet_url = (calendar_event or {}).get("meet_url")
        if not meet_url:
            raise MeetingProviderError(
                "google_meet form selected but calendar_create returned no meet_url — "
                "was it called with want_conference=True?"
            )
        return MeetingDetail(
            provider="google_meet", join_url=meet_url, phone_number=None,
            display_text=f"Google Meet: {meet_url}",
        )


class _ZoomProvider:
    def resolve(self, acl, project, host_user, form_config, *, start, end, title, **kw) -> MeetingDetail:  # NOSONAR: parameters are fixed by the shared provider interface
        from .. import config
        from .zoom_client import ZoomAPIError, ZoomAuthError, create_zoom_meeting, get_access_token

        cfg = (config.load().get("memaix", {}) or {}).get("zoom")
        if not cfg:
            raise MeetingProviderError("zoom not configured for this deployment")
        try:
            secret = config.secret(cfg.get("client_secret_ref"))
            if secret is None:
                raise MeetingProviderError("zoom client_secret_ref did not resolve to a value")
            token = get_access_token(cfg["account_id"], cfg["client_id"], secret)
            duration_min = max(1, int((end - start).total_seconds() // 60))
            meeting = create_zoom_meeting(
                token, topic=title, start_time=start.isoformat(), duration_min=duration_min,
            )
        except (ZoomAuthError, ZoomAPIError, KeyError) as exc:
            raise MeetingProviderError(f"zoom meeting creation failed: {exc}") from exc

        join_url = meeting.get("join_url")
        if not join_url:
            raise MeetingProviderError(f"zoom response had no join_url: {meeting!r}")
        return MeetingDetail(
            provider="zoom", join_url=join_url, phone_number=None,
            display_text=f"Zoom: {join_url}",
        )


_PROVIDERS: dict[str, _Provider] = {
    "phone": _PhoneProvider(),
    "google_meet": _GoogleMeetProvider(),
    "zoom": _ZoomProvider(),
}


def resolve_meeting_detail(
    provider: str, form_config: dict, acl, project: str, host_user: str,
    *, start, end, title: str, calendar_event: dict | None = None,
) -> MeetingDetail:
    """Single dispatch point. Raises MeetingProviderError (never a raw
    provider-specific exception) so booking_create has one thing to catch."""
    impl = _PROVIDERS.get(provider)
    if impl is None:
        raise MeetingProviderError(f"unknown meeting form provider {provider!r}")
    return impl.resolve(
        acl, project, host_user, form_config,
        start=start, end=end, title=title, calendar_event=calendar_event,
    )
