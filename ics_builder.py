"""
ics_builder.py — generates calendar invites (RFC 5545) for AJ Bookings.

Builds real invitations, not passive .ics attachments: METHOD:REQUEST for a
new/confirmed booking, METHOD:CANCEL to pull it back off the recipient's
calendar. Calendar clients (Gmail, Outlook, Apple Calendar) key off the
METHOD property to decide whether to show Accept/Decline buttons or to
retire an existing event — that only works if both invites share the same
UID and the CANCEL carries a higher SEQUENCE than the REQUEST it's replacing.

Timezone: all slot times in the Bookings DB are stored as naive datetimes
with no TZID anywhere in the schema. Each event now carries a `timezone`
column (IANA zone name, default 'America/Chicago' for backward compat with
events created before this field existed) — slot times for that event are
wall-clock time in that zone. This module emits an explicit VTIMEZONE block
matching the event's zone, rather than floating or UTC time — floating time
is interpreted inconsistently across clients, and UTC would silently shift
the displayed time for anyone outside the event's zone.

Only a curated set of US zones is supported (see TIMEZONE_CHOICES) — basic
selection for AJ's own events, not a full IANA database. Add a new
_VTIMEZONE_* block + TIMEZONE_CHOICES entry here to support another zone.
"""

import uuid as _uuid
from datetime import datetime


ORGANIZER_EMAIL = 'operations@augustjackson.com'
ORGANIZER_NAME = 'August Jackson'

DEFAULT_TIMEZONE = 'America/Chicago'

# Standard America/Chicago VTIMEZONE block (CST/CDT), embedded so clients
# that don't already know the IANA zone (notably older Outlook builds)
# still render the correct local time rather than falling back to UTC.
_VTIMEZONE_CHICAGO = """BEGIN:VTIMEZONE
TZID:America/Chicago
BEGIN:DAYLIGHT
TZOFFSETFROM:-0600
TZOFFSETTO:-0500
TZNAME:CDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0500
TZOFFSETTO:-0600
TZNAME:CST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE"""

_VTIMEZONE_NEW_YORK = """BEGIN:VTIMEZONE
TZID:America/New_York
BEGIN:DAYLIGHT
TZOFFSETFROM:-0500
TZOFFSETTO:-0400
TZNAME:EDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0400
TZOFFSETTO:-0500
TZNAME:EST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE"""

_VTIMEZONE_DENVER = """BEGIN:VTIMEZONE
TZID:America/Denver
BEGIN:DAYLIGHT
TZOFFSETFROM:-0700
TZOFFSETTO:-0600
TZNAME:MDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0600
TZOFFSETTO:-0700
TZNAME:MST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE"""

_VTIMEZONE_LOS_ANGELES = """BEGIN:VTIMEZONE
TZID:America/Los_Angeles
BEGIN:DAYLIGHT
TZOFFSETFROM:-0800
TZOFFSETTO:-0700
TZNAME:PDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0700
TZOFFSETTO:-0800
TZNAME:PST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE"""

_VTIMEZONE_PHOENIX = """BEGIN:VTIMEZONE
TZID:America/Phoenix
BEGIN:STANDARD
TZOFFSETFROM:-0700
TZOFFSETTO:-0700
TZNAME:MST
DTSTART:19700101T000000
END:STANDARD
END:VTIMEZONE"""

_VTIMEZONE_ANCHORAGE = """BEGIN:VTIMEZONE
TZID:America/Anchorage
BEGIN:DAYLIGHT
TZOFFSETFROM:-0900
TZOFFSETTO:-0800
TZNAME:AKDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0800
TZOFFSETTO:-0900
TZNAME:AKST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE"""

_VTIMEZONE_HONOLULU = """BEGIN:VTIMEZONE
TZID:Pacific/Honolulu
BEGIN:STANDARD
TZOFFSETFROM:-1000
TZOFFSETTO:-1000
TZNAME:HST
DTSTART:19700101T000000
END:STANDARD
END:VTIMEZONE"""

_VTIMEZONES = {
    'America/New_York':    _VTIMEZONE_NEW_YORK,
    'America/Chicago':     _VTIMEZONE_CHICAGO,
    'America/Denver':      _VTIMEZONE_DENVER,
    'America/Phoenix':     _VTIMEZONE_PHOENIX,
    'America/Los_Angeles': _VTIMEZONE_LOS_ANGELES,
    'America/Anchorage':   _VTIMEZONE_ANCHORAGE,
    'Pacific/Honolulu':    _VTIMEZONE_HONOLULU,
}

# (value, label) pairs — value is the IANA zone stored on the event and used
# as the ICS TZID; label is what producers see in the admin dropdown.
TIMEZONE_CHOICES = [
    ('America/New_York',    'Eastern (ET)'),
    ('America/Chicago',     'Central (CT)'),
    ('America/Denver',      'Mountain (MT)'),
    ('America/Phoenix',     'Arizona — no DST (MST)'),
    ('America/Los_Angeles', 'Pacific (PT)'),
    ('America/Anchorage',   'Alaska (AKT)'),
    ('Pacific/Honolulu',    'Hawaii — no DST (HST)'),
]

VALID_TIMEZONES = frozenset(v for v, _ in TIMEZONE_CHOICES)


def _resolve_timezone(timezone):
    """Falls back to the default zone for an unrecognized/missing value
    rather than raising — a bad stored value should degrade gracefully, not
    break invite sending. Returns the zone name to actually use, so callers
    stay consistent between the VTIMEZONE block and the DTSTART/DTEND TZID
    (using the raw unrecognized name for one and the fallback for the other
    would produce a VEVENT referencing a TZID with no matching VTIMEZONE)."""
    return timezone if timezone in _VTIMEZONES else DEFAULT_TIMEZONE


def booking_uid(booking_id):
    """Stable UID for a real booking — same UID on invite and cancel so a
    calendar client treats the cancel as retiring the same event."""
    return f'booking-{booking_id}@ajbookings.up.railway.app'


def test_uid(event_id, marker):
    """UID for admin test sends — namespaced so it can never collide with
    a real booking's UID."""
    return f'test-{event_id}-{marker}@ajbookings.up.railway.app'


def _fold(line):
    """RFC 5545 line folding — lines over 75 octets get continued with a
    leading space on the next line. Only matters for long DESCRIPTION/
    LOCATION values; short lines pass through untouched."""
    if len(line) <= 75:
        return line
    out = [line[:75]]
    rest = line[75:]
    while rest:
        out.append(' ' + rest[:74])
        rest = rest[74:]
    return '\r\n'.join(out)


def _escape(text):
    """Escape text per RFC 5545 (commas, semicolons, backslashes, newlines)."""
    if not text:
        return ''
    return (
        str(text)
        .replace('\\', '\\\\')
        .replace(',', '\\,')
        .replace(';', '\\;')
        .replace('\n', '\\n')
    )


def _dt(value):
    """Format a naive datetime as a local (TZID-relative) DATE-TIME value.
    Accepts a datetime or an ISO string (as stored in the slots table)."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.strftime('%Y%m%dT%H%M%S')


def _build(*, method, uid, sequence, summary, description, location,
           start, end, organizer_email, organizer_name,
           attendee_email, attendee_name, status, timezone=DEFAULT_TIMEZONE):
    timezone = _resolve_timezone(timezone)
    dtstamp = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'PRODID:-//August Jackson//AJ Bookings//EN',
        'CALSCALE:GREGORIAN',
        f'METHOD:{method}',
        _VTIMEZONES[timezone].replace('\n', '\r\n'),
        'BEGIN:VEVENT',
        f'UID:{uid}',
        f'DTSTAMP:{dtstamp}',
        f'DTSTART;TZID={timezone}:{_dt(start)}',
        f'DTEND;TZID={timezone}:{_dt(end)}',
        f'SEQUENCE:{sequence}',
        f'STATUS:{status}',
        _fold(f'SUMMARY:{_escape(summary)}'),
    ]
    if description:
        lines.append(_fold(f'DESCRIPTION:{_escape(description)}'))
    if location:
        lines.append(_fold(f'LOCATION:{_escape(location)}'))
    lines.append(_fold(f'ORGANIZER;CN={_escape(organizer_name)}:mailto:{organizer_email}'))
    if attendee_email:
        lines.append(_fold(
            f'ATTENDEE;CN={_escape(attendee_name or attendee_email)};'
            f'ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:'
            f'mailto:{attendee_email}'
        ))
    lines += [
        'END:VEVENT',
        'END:VCALENDAR',
    ]
    return ('\r\n'.join(lines) + '\r\n').encode('utf-8')


def build_invite_ics(*, uid, summary, start, end, attendee_email,
                      attendee_name=None, location=None, description=None,
                      sequence=0, timezone=DEFAULT_TIMEZONE):
    """METHOD:REQUEST — a new or re-sent invitation. sequence starts at 0
    for a booking's first invite; only bump it if the same UID is ever
    re-sent with changed details (not currently a Bookings use case, since
    reschedule cancels the old booking and creates a new one with a new
    UID/booking id). timezone is the event's IANA zone (TIMEZONE_CHOICES) —
    falls back to Chicago for an unrecognized value."""
    return _build(
        method='REQUEST', uid=uid, sequence=sequence,
        summary=summary, description=description, location=location,
        start=start, end=end,
        organizer_email=ORGANIZER_EMAIL, organizer_name=ORGANIZER_NAME,
        attendee_email=attendee_email, attendee_name=attendee_name,
        status='CONFIRMED', timezone=timezone,
    )


def build_cancel_ics(*, uid, summary, start, end, attendee_email,
                      attendee_name=None, location=None, description=None,
                      sequence=1, timezone=DEFAULT_TIMEZONE):
    """METHOD:CANCEL — retires the calendar entry matching `uid`. Must carry
    a SEQUENCE higher than the invite it's cancelling (default 1, since every
    real booking's invite goes out at sequence 0 and is only ever cancelled
    once — bookings aren't rescheduled in place, see module docstring).
    timezone must match the original invite's zone — a CANCEL for the same
    UID with a different TZID would tell some clients it's a different event."""
    return _build(
        method='CANCEL', uid=uid, sequence=sequence,
        summary=summary, description=description, location=location,
        start=start, end=end,
        organizer_email=ORGANIZER_EMAIL, organizer_name=ORGANIZER_NAME,
        attendee_email=attendee_email, attendee_name=attendee_name,
        status='CANCELLED', timezone=timezone,
    )
