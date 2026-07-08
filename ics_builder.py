"""
ics_builder.py — generates calendar invites (RFC 5545) for AJ Bookings.

Builds real invitations, not passive .ics attachments: METHOD:REQUEST for a
new/confirmed booking, METHOD:CANCEL to pull it back off the recipient's
calendar. Calendar clients (Gmail, Outlook, Apple Calendar) key off the
METHOD property to decide whether to show Accept/Decline buttons or to
retire an existing event — that only works if both invites share the same
UID and the CANCEL carries a higher SEQUENCE than the REQUEST it's replacing.

Timezone: all slot times in the Bookings DB are stored as naive datetimes
with no TZID anywhere in the schema. There is no per-event or per-slot
timezone field today. This module assumes every stored time is America/
Chicago wall-clock time (AJ HQ's home timezone) and emits an explicit
VTIMEZONE block accordingly, rather than emitting floating or UTC time —
floating time is interpreted inconsistently across clients, and UTC would
silently shift the displayed time for anyone outside Chicago. If Bookings
ever supports events outside Central time, this assumption needs revisiting
alongside a real timezone column.
"""

import uuid as _uuid
from datetime import datetime


ORGANIZER_EMAIL = 'operations@augustjackson.com'
ORGANIZER_NAME = 'August Jackson'

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
           attendee_email, attendee_name, status):
    dtstamp = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'PRODID:-//August Jackson//AJ Bookings//EN',
        'CALSCALE:GREGORIAN',
        f'METHOD:{method}',
        _VTIMEZONE_CHICAGO.replace('\n', '\r\n'),
        'BEGIN:VEVENT',
        f'UID:{uid}',
        f'DTSTAMP:{dtstamp}',
        f'DTSTART;TZID=America/Chicago:{_dt(start)}',
        f'DTEND;TZID=America/Chicago:{_dt(end)}',
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
                      sequence=0):
    """METHOD:REQUEST — a new or re-sent invitation. sequence starts at 0
    for a booking's first invite; only bump it if the same UID is ever
    re-sent with changed details (not currently a Bookings use case, since
    reschedule cancels the old booking and creates a new one with a new
    UID/booking id)."""
    return _build(
        method='REQUEST', uid=uid, sequence=sequence,
        summary=summary, description=description, location=location,
        start=start, end=end,
        organizer_email=ORGANIZER_EMAIL, organizer_name=ORGANIZER_NAME,
        attendee_email=attendee_email, attendee_name=attendee_name,
        status='CONFIRMED',
    )


def build_cancel_ics(*, uid, summary, start, end, attendee_email,
                      attendee_name=None, location=None, description=None,
                      sequence=1):
    """METHOD:CANCEL — retires the calendar entry matching `uid`. Must carry
    a SEQUENCE higher than the invite it's cancelling (default 1, since every
    real booking's invite goes out at sequence 0 and is only ever cancelled
    once — bookings aren't rescheduled in place, see module docstring)."""
    return _build(
        method='CANCEL', uid=uid, sequence=sequence,
        summary=summary, description=description, location=location,
        start=start, end=end,
        organizer_email=ORGANIZER_EMAIL, organizer_name=ORGANIZER_NAME,
        attendee_email=attendee_email, attendee_name=attendee_name,
        status='CANCELLED',
    )
