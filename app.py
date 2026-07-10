"""
Bookings — AJ internal scheduling/booking tool.

Admin-internal app (any logged-in AJ user, no role restriction):
event setup, manual + cadence-generated slots, custom form fields.
Public booking surface (/book/<slug>): calendar-based slot picking,
booking/waitlist, cancel + reschedule via token links. Confirmed/
promoted bookings get a real calendar invite (METHOD:REQUEST); cancels
and reschedule-releases get METHOD:CANCEL for the same UID — see
ics_builder.py. Email routes through HQ's /api/email/send proxy.

Architecture: same pattern as Invoice Tracker — standalone Railway app,
aj_auth.py for admin auth, HQ proxy block for shared reference data.
"""

import os
import re
import io
import csv
import json
import base64
import logging
import secrets
import sqlite3
from html import escape as _esc
from datetime import datetime, date, timedelta

import requests as req
from flask import Flask, request, jsonify, g, session, render_template, Response
from flask_cors import CORS
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.pdfbase import pdfmetrics

from aj_auth import require_auth, get_current_user
import ics_builder

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static', static_url_path='/static', template_folder='templates')

# ── CORS — restrict to known AJ origins ──────────────────────────────────────
CORS(app, origins=[
    r"https://.*\.up\.railway\.app",
    r"https://.*\.netlify\.app",
    r"http://localhost:.*",
    r"http://127\.0\.0\.1:.*",
], supports_credentials=True)

# ── Hard-fail on missing secrets in production ───────────────────────────────
_FLASK_ENV = os.environ.get('FLASK_ENV', 'production')
_IS_PROD   = _FLASK_ENV == 'production'

_secret = os.environ.get('FLASK_SECRET_KEY')
if not _secret:
    if _IS_PROD:
        raise RuntimeError(
            "FLASK_SECRET_KEY is not set — refusing to start in production. "
            "Generate one (openssl rand -hex 32) and set it in Railway env vars."
        )
    _secret = 'dev-secret-change-in-prod'
    logger.warning("FLASK_SECRET_KEY not set — using insecure dev key (FLASK_ENV != production)")
app.secret_key = _secret

PLATFORM_SECRET = os.environ.get('PLATFORM_SECRET', '')
if not PLATFORM_SECRET and _IS_PROD:
    raise RuntimeError(
        "PLATFORM_SECRET is not set — refusing to start in production. "
        "Same value as HQ and every other AJ app."
    )

DB_PATH = os.environ.get('DATABASE_PATH', '/app/data/bookings.db')

# Current schema version — MUST equal the highest `if current < N` migration
# block below.
SCHEMA_VERSION = 6

_HQ_BASE    = 'https://aj-hq.up.railway.app'
_HQ_TIMEOUT = 5
_HQ_UPLOAD_TIMEOUT = 15  # multipart forwarding (feedback screenshots, email attachments) needs more headroom

# When set, every outbound email's recipients are redirected here instead of
# the real address — applied in send_email() before calling HQ, per the
# standard AJ pattern (HQ always delivers to whatever it receives; the
# override is each calling app's responsibility). Leave unset in production.
TEST_EMAIL_OVERRIDE = os.environ.get('TEST_EMAIL_OVERRIDE', '').strip()

# Days-of-week convention: Python date.weekday() — Monday=0 ... Sunday=6.
# Used consistently in slot_rules.days_of_week and the cadence generator.
VALID_FIELD_TYPES = ('text', 'select', 'checkbox')
VALID_EVENT_STATUSES = ('draft', 'active', 'closed')
VALID_MESSAGE_TYPES = ('confirmation', 'waitlist', 'cancellation')

# Cap how much a single cadence rule can generate in one call — guards
# against a typo'd date range (e.g. wrong century) silently trying to
# create tens of thousands of rows.
MAX_CADENCE_DAYS = 366


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def get_cols(db, table):
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute('PRAGMA foreign_keys = ON')

    db.executescript("""
        CREATE TABLE IF NOT EXISTS schema_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            job_number TEXT NOT NULL,
            owner_user_id INTEGER,
            owner_name TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            location TEXT,
            notes TEXT,
            allow_waitlist INTEGER NOT NULL DEFAULT 0,
            allow_reschedule INTEGER NOT NULL DEFAULT 0,
            max_bookings_per_email INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS slot_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            days_of_week TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            slot_length_minutes INTEGER NOT NULL,
            capacity INTEGER NOT NULL DEFAULT 1,
            slots_generated INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            capacity INTEGER NOT NULL DEFAULT 1,
            location_override TEXT,
            notes_override TEXT,
            source TEXT NOT NULL DEFAULT 'manual',
            slot_rule_id INTEGER REFERENCES slot_rules(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            custom_field_answers TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'confirmed',
            cancel_token TEXT UNIQUE,
            reschedule_token TEXT UNIQUE,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS form_fields (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            label TEXT NOT NULL,
            field_type TEXT NOT NULL,
            options TEXT NOT NULL DEFAULT '[]',
            required INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_slots_event ON slots(event_id);
        CREATE INDEX IF NOT EXISTS idx_slot_rules_event ON slot_rules(event_id);
        CREATE INDEX IF NOT EXISTS idx_bookings_slot ON bookings(slot_id);
        CREATE INDEX IF NOT EXISTS idx_form_fields_event ON form_fields(event_id);
    """)
    db.commit()

    # ── Versioned migrations (PRAGMA table_info pattern — never NOT NULL
    #    on ALTER TABLE ADD COLUMN, per reference-app-standards) ──────────────
    row = db.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()
    current = row[0] if row else 0

    if current < 2:
        cols = get_cols(db, 'events')
        if 'cover_image_url' not in cols:
            db.execute("ALTER TABLE events ADD COLUMN cover_image_url TEXT")
        if 'brand_color' not in cols:
            db.execute("ALTER TABLE events ADD COLUMN brand_color TEXT")
        if 'directions' not in cols:
            db.execute("ALTER TABLE events ADD COLUMN directions TEXT")
        current = 2

    if current < 3:
        cols = get_cols(db, 'events')
        if 'cover_image_position' not in cols:
            # Horizontal focal point (0-100) for the hero image crop —
            # object-position on the public page. Default 50 = center.
            db.execute("ALTER TABLE events ADD COLUMN cover_image_position INTEGER DEFAULT 50")
        current = 3

    if current < 4:
        cols = get_cols(db, 'events')
        if 'cover_image_position_y' not in cols:
            # Vertical focal point (0-100), added alongside the horizontal
            # one so the admin editor can support full drag-to-reposition
            # instead of a left/right-only slider. Default 50 = center —
            # existing events with only an X position keep their old
            # horizontal framing and default to vertically centered.
            db.execute("ALTER TABLE events ADD COLUMN cover_image_position_y INTEGER DEFAULT 50")
        current = 4

    if current < 5:
        cols = get_cols(db, 'events')
        if 'custom_messages' not in cols:
            # JSON object, keyed by message type: 'confirmation', 'waitlist',
            # 'cancellation'. Each value (if present) is producer-written
            # copy that gets PREPENDED above the system-generated event
            # info + cancel/reschedule links in the matching email/invite —
            # never a replacement, so a producer can't accidentally ship an
            # email with no way to manage the booking. Missing/blank keys
            # mean "no custom message for this type," not an error.
            db.execute("ALTER TABLE events ADD COLUMN custom_messages TEXT DEFAULT '{}'")
        current = 5

    if current < 6:
        cols = get_cols(db, 'events')
        if 'timezone' not in cols:
            # IANA zone the event's slot times are wall-clock in. Default
            # matches ics_builder's long-standing hardcoded assumption, so
            # every event created before this column existed keeps behaving
            # exactly as it already did — nothing changes for them.
            db.execute(
                f"ALTER TABLE events ADD COLUMN timezone TEXT NOT NULL DEFAULT '{ics_builder.DEFAULT_TIMEZONE}'"
            )
        current = 6

    db.execute(
        "INSERT INTO schema_meta (id, version) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET version = ?",
        (SCHEMA_VERSION, SCHEMA_VERSION)
    )
    db.commit()
    db.close()


init_db()


# ── Small helpers ─────────────────────────────────────────────────────────────

def _now():
    return datetime.utcnow().isoformat()


def _slugify(text):
    s = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')
    return s or 'event'


def _unique_slug(db, base_slug):
    slug = base_slug
    n = 2
    while db.execute("SELECT 1 FROM events WHERE slug = ?", (slug,)).fetchone():
        slug = f"{base_slug}-{n}"
        n += 1
    return slug


def _row_to_dict(row):
    return dict(row) if row else None


def is_valid_job_number(val):
    """7-digit job number — same validation rule enforced everywhere in
    the AJ ecosystem (mirrors isValidJobNumber() from aj-utils.js)."""
    return bool(re.fullmatch(r'\d{7}', str(val or '')))


def is_valid_hex_color(val):
    return bool(re.fullmatch(r'#[0-9a-fA-F]{6}', str(val or '').strip()))


def _normalize_dropbox_url(url):
    """Dropbox shared links default to dl=0, which renders Dropbox's preview
    page rather than the raw file — useless in an <img src>. Swap to raw=1
    so the file loads directly. Only touches dropbox.com links; anything
    else (Cloudinary, S3, direct hosting) passes through unchanged.
    Same transformation belongs anywhere else in the ecosystem that accepts
    a pasted Dropbox link (e.g. Projects) — this is a one-off local fix,
    not yet centralized on HQ."""
    url = (url or '').strip()
    if not url or 'dropbox.com' not in url:
        return url
    if 'dl=0' in url:
        return url.replace('dl=0', 'raw=1')
    if 'dl=1' in url:
        return url.replace('dl=1', 'raw=1')
    if 'raw=1' in url:
        return url
    sep = '&' if '?' in url else '?'
    return f'{url}{sep}raw=1'


def _hq_get(path):
    try:
        r = req.get(
            f'{_HQ_BASE}{path}',
            headers={'X-AJ-Key': PLATFORM_SECRET},
            timeout=_HQ_TIMEOUT
        )
        return r.json(), r.status_code
    except Exception as e:
        return {'error': str(e)}, 502


def _gen_token():
    return secrets.token_urlsafe(24)


# ── HQ Proxy Block (verbatim per reference-app-standards) ────────────────────

@app.route('/api/apps')
def proxy_apps():
    data, status = _hq_get('/api/apps')
    return jsonify(data), status


@app.route('/auth/validate')
def proxy_auth_validate():
    cached = session.get('_aj_user')
    if cached:
        return jsonify({'valid': True, 'user': cached}), 200
    token = request.args.get('token', '')
    try:
        r = req.get(
            f'{_HQ_BASE}/auth/validate',
            headers={'X-AJ-Key': PLATFORM_SECRET},
            params={'token': token} if token else {},
            timeout=_HQ_TIMEOUT
        )
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'valid': False, 'error': str(e)}), 502


@app.route('/api/users')
def proxy_users():
    data, status = _hq_get('/api/users')
    return jsonify(data), status


@app.route('/api/rates')
def proxy_rates():
    client = request.args.get('client', '')
    path = f'/api/rates?client={client}' if client else '/api/rates'
    data, status = _hq_get(path)
    return jsonify(data), status


@app.route('/api/rates/lookup')
def proxy_rates_lookup():
    qs = request.query_string.decode()
    data, status = _hq_get(f'/api/rates/lookup?{qs}')
    return jsonify(data), status


@app.route('/api/people')
def proxy_people():
    item_type = request.args.get('item_type', '')
    path = f'/api/people?item_type={item_type}' if item_type else '/api/people'
    data, status = _hq_get(path)
    return jsonify(data), status


@app.route('/api/codes')
def proxy_codes():
    data, status = _hq_get('/api/codes')
    return jsonify(data), status


@app.route('/api/codes/fees')
def proxy_codes_fees():
    data, status = _hq_get('/api/codes/fees')
    return jsonify(data), status


@app.route('/api/codes/expenses')
def proxy_codes_expenses():
    data, status = _hq_get('/api/codes/expenses')
    return jsonify(data), status


@app.route('/api/jobs')
def proxy_jobs():
    qs = request.query_string.decode()
    path = f'/api/jobs?{qs}' if qs else '/api/jobs'
    data, status = _hq_get(path)
    return jsonify(data), status


@app.route('/api/jobs/<job_number>')
def proxy_jobs_single(job_number):
    data, status = _hq_get(f'/api/jobs/{job_number}')
    return jsonify(data), status


@app.route('/api/users/me/password', methods=['POST'])
def proxy_user_change_password():
    try:
        r = req.post(
            f'{_HQ_BASE}/api/users/me/password',
            headers={
                'X-AJ-Key': PLATFORM_SECRET,
                'Content-Type': 'application/json',
                'Cookie': f'aj_session={request.cookies.get("aj_session", "")}',
            },
            json=request.get_json(force=True, silent=True) or {},
            timeout=_HQ_TIMEOUT
        )
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.route('/api/feedback', methods=['POST'])
def proxy_feedback():
    """Forwards a feedback widget submission (multipart, optional screenshot)
    to HQ. No local session check here — submitter name/email travel as
    form fields the widget already pulled from /auth/validate client-side;
    this route's only job is adding the platform secret and relaying bytes.
    Uses _HQ_UPLOAD_TIMEOUT, not _HQ_TIMEOUT — screenshot uploads need more
    headroom than a plain reference-data GET."""
    files = None
    if 'screenshot' in request.files and request.files['screenshot'].filename:
        f = request.files['screenshot']
        files = {'screenshot': (f.filename, f.stream, f.mimetype)}
    try:
        r = req.post(
            f'{_HQ_BASE}/api/feedback',
            headers={'X-AJ-Key': PLATFORM_SECRET},
            data=request.form.to_dict(),
            files=files,
            timeout=_HQ_UPLOAD_TIMEOUT
        )
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 502


# ── Email — wired to HQ's Mailgun proxy ────────────────────────────────────────
# Calls HQ directly (same pattern as _hq_get()) rather than through a local
# self-proxy route — this app runs --workers 1 for SQLite safety, and a
# synchronous self-HTTP-call would deadlock the single worker against itself.

def send_email(to, subject, html_body, attachments=None):
    """Sends via HQ's Mailgun proxy. `to` may be a single address or a list.
    `attachments` is [{filename, data_b64, content_type}] — content_type is
    optional (HQ defaults to application/octet-stream if omitted); calendar
    invites must set it to 'text/calendar; method=REQUEST' or
    'text/calendar; method=CANCEL' or they'll arrive as plain file downloads
    instead of interactive invites. TEST_EMAIL_OVERRIDE, if set, redirects
    all recipients before this call reaches HQ — HQ itself has no concept
    of a test mode and always delivers to whatever it receives.
    """
    to_addrs = [to] if isinstance(to, str) else list(to or [])
    if TEST_EMAIL_OVERRIDE:
        logger.info("[EMAIL] TEST_EMAIL_OVERRIDE active — redirecting %s -> %s", to_addrs, TEST_EMAIL_OVERRIDE)
        to_addrs = [TEST_EMAIL_OVERRIDE]

    try:
        r = req.post(
            f'{_HQ_BASE}/api/email/send',
            headers={'X-AJ-Key': PLATFORM_SECRET, 'Content-Type': 'application/json'},
            json={
                'to': to_addrs,
                'subject': subject,
                'html_body': html_body,
                'attachments': attachments or [],
            },
            timeout=_HQ_UPLOAD_TIMEOUT
        )
        if r.status_code != 200:
            logger.error("[EMAIL] HQ proxy returned %s: %s", r.status_code, r.text[:500])
            return False
        data = r.json()
        if data.get('stub'):
            logger.info("[EMAIL] HQ is in stub mode (SMTP creds not set) — to=%s subject=%r", to_addrs, subject)
        return bool(data.get('ok'))
    except Exception as e:
        logger.error("[EMAIL] send failed: %s", e)
        return False


# ── Calendar invite emails ───────────────────────────────────────────────────
# Shared by all three real send sites: booking confirmed, waitlist promoted
# to confirmed, and booking cancelled (explicit cancel + reschedule-releases-
# old-slot). Keeping the ICS-building + email copy in one place so the three
# sites can't quietly drift from each other.

BOOKINGS_BASE_URL = 'https://ajbookings.up.railway.app'


def _slot_location(event, slot):
    return (slot['location_override'] if slot['location_override'] else event['location']) or ''


def _custom_message(event, message_type):
    """Pulls a producer-written message for this event/type, if set. Never
    raises on malformed JSON — a bad value here should degrade to 'no custom
    message', not break the send."""
    try:
        msgs = json.loads(event['custom_messages'] or '{}')
    except (ValueError, TypeError):
        return ''
    return (msgs.get(message_type) or '').strip()


def _manage_links_text(event, cancel_url, reschedule_url=None):
    """Plain-text manage-booking lines shared by the ICS description and,
    in HTML form, the email body. Reschedule only appears when the event
    allows it and a reschedule_url was actually passed in."""
    lines = [f"Cancel: {BOOKINGS_BASE_URL}{cancel_url}"]
    if event['allow_reschedule'] and reschedule_url:
        lines.append(f"Reschedule: {BOOKINGS_BASE_URL}{reschedule_url}")
    return lines


def _invite_description(event, cancel_url, reschedule_url=None):
    lines = []
    custom = _custom_message(event, 'confirmation')
    if custom:
        lines.append(custom)
        lines.append('')  # blank line separating producer copy from system info
    lines.append(f"Booking for {event['name']}.")
    if event['notes']:
        lines.append(event['notes'])
    if event['directions']:
        lines.append(f"Directions: {event['directions']}")
    lines.extend(_manage_links_text(event, cancel_url, reschedule_url))
    return '\n'.join(lines)


def send_booking_invite(*, booking_id, name, email, event, slot, cancel_url, reschedule_url=None):
    """Sends the confirmation email with a METHOD:REQUEST calendar invite
    attached. Called when a booking is created as 'confirmed' and when a
    waitlisted booking is promoted to confirmed — never for waitlisted
    status itself, since nothing is actually booked yet at that point."""
    ics_bytes = ics_builder.build_invite_ics(
        uid=ics_builder.booking_uid(booking_id),
        summary=event['name'],
        start=slot['start_time'],
        end=slot['end_time'],
        attendee_email=email,
        attendee_name=name,
        location=_slot_location(event, slot),
        description=_invite_description(event, cancel_url, reschedule_url),
        timezone=event['timezone'],
    )
    custom = _custom_message(event, 'confirmation')
    custom_html = f"<p>{_esc(custom)}</p>" if custom else ''
    manage_html = f'<p>Need to make a change? <a href="{BOOKINGS_BASE_URL}{cancel_url}">Cancel</a>'
    if event['allow_reschedule'] and reschedule_url:
        manage_html += f' or <a href="{BOOKINGS_BASE_URL}{reschedule_url}">reschedule</a>'
    manage_html += ' your booking.</p>'
    html_body = (
        f"{custom_html}"
        f"<p>You're confirmed for <strong>{_esc(event['name'])}</strong>.</p>"
        f"<p>A calendar invite is attached — accept it to add this to your calendar.</p>"
        f"{manage_html}"
    )
    return send_email(
        email, f"You're confirmed — {event['name']}", html_body,
        attachments=[{
            'filename': 'invite.ics',
            'data_b64': base64.b64encode(ics_bytes).decode(),
            'content_type': 'text/calendar; method=REQUEST',
        }],
    )


def send_booking_waitlist(*, name, email, event, cancel_url):
    """Sent when a new booking lands on the waitlist instead of being
    confirmed outright — no calendar invite yet, since nothing is actually
    booked until a producer promotes it."""
    custom = _custom_message(event, 'waitlist')
    custom_html = f"<p>{_esc(custom)}</p>" if custom else ''
    html_body = (
        f"{custom_html}"
        f"<p>You're on the waitlist for <strong>{_esc(event['name'])}</strong>.</p>"
        f"<p>We'll send a calendar invite if a spot opens up.</p>"
        f"<p>Need to step off the waitlist? <a href=\"{BOOKINGS_BASE_URL}{cancel_url}\">Cancel</a>.</p>"
    )
    return send_email(email, f"You're on the waitlist — {event['name']}", html_body)


def send_booking_cancel(*, booking_id, name, email, event, slot):
    """Sends the cancellation email with a METHOD:CANCEL calendar invite for
    the same UID as the original booking invite, so an accepted invite gets
    pulled off the recipient's calendar rather than left orphaned."""
    ics_bytes = ics_builder.build_cancel_ics(
        uid=ics_builder.booking_uid(booking_id),
        summary=event['name'],
        start=slot['start_time'],
        end=slot['end_time'],
        attendee_email=email,
        attendee_name=name,
        location=_slot_location(event, slot),
        timezone=event['timezone'],
    )
    custom = _custom_message(event, 'cancellation')
    custom_html = f"<p>{_esc(custom)}</p>" if custom else ''
    html_body = (
        f"{custom_html}"
        f"<p>Your booking for <strong>{_esc(event['name'])}</strong> has been cancelled.</p>"
        f"<p>This should also clear the event from your calendar.</p>"
    )
    return send_email(
        email, f"Cancelled — {event['name']}", html_body,
        attachments=[{
            'filename': 'cancel.ics',
            'data_b64': base64.b64encode(ics_bytes).decode(),
            'content_type': 'text/calendar; method=CANCEL',
        }],
    )


# ── Auth helper ───────────────────────────────────────────────────────────────

def _current_user_brief():
    user = get_current_user()
    if not user:
        return None, None
    return user.get('id'), user.get('name')


# ── Admin pages ───────────────────────────────────────────────────────────────

@app.route('/')
@require_auth
def index():
    return render_template('admin_events.html')


@app.route('/admin/events/<int:event_id>')
@require_auth
def admin_event_detail_page(event_id):
    user = get_current_user()
    return render_template('admin_event_detail.html', event_id=event_id,
                            current_user_email=(user.get('email') if user else ''))


# ── /api/summary (required by convention) ────────────────────────────────────

@app.route('/api/summary')
def api_summary():
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    active = db.execute("SELECT COUNT(*) FROM events WHERE status = 'active'").fetchone()[0]
    return jsonify({
        'app': 'Bookings',
        'status': 'ok',
        'counts': {'events': total, 'active_events': active}
    })


# ── Events CRUD ───────────────────────────────────────────────────────────────

@app.route('/api/events', methods=['GET'])
@require_auth
def list_events():
    db = get_db()
    rows = db.execute("""
        SELECT e.*,
            (SELECT COUNT(*) FROM slots s WHERE s.event_id = e.id) AS slot_count,
            (SELECT COUNT(*) FROM bookings b
                JOIN slots s2 ON s2.id = b.slot_id
                WHERE s2.event_id = e.id AND b.status != 'cancelled') AS booking_count
        FROM events e
        ORDER BY e.created_at DESC
    """).fetchall()
    return jsonify({'events': [_row_to_dict(r) for r in rows]})


@app.route('/api/events', methods=['POST'])
@require_auth
def create_event():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get('name') or '').strip()
    job_number = (body.get('job_number') or '').strip()
    timezone = body.get('timezone') or ics_builder.DEFAULT_TIMEZONE

    if not name:
        return jsonify({'error': 'name is required'}), 400
    if not is_valid_job_number(job_number):
        return jsonify({'error': 'job_number must be exactly 7 digits'}), 400
    if timezone not in ics_builder.VALID_TIMEZONES:
        return jsonify({'error': f'timezone must be one of {sorted(ics_builder.VALID_TIMEZONES)}'}), 400

    db = get_db()
    slug = _unique_slug(db, _slugify(name))
    user_id, user_name = _current_user_brief()
    now = _now()

    cur = db.execute("""
        INSERT INTO events
            (slug, name, job_number, owner_user_id, owner_name, status,
             location, notes, allow_waitlist, allow_reschedule,
             max_bookings_per_email, timezone, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        slug, name, job_number, user_id, user_name,
        body.get('location'), body.get('notes'),
        1 if body.get('allow_waitlist') else 0,
        1 if body.get('allow_reschedule') else 0,
        body.get('max_bookings_per_email'),
        timezone,
        now, now
    ))
    db.commit()

    event = _row_to_dict(db.execute("SELECT * FROM events WHERE id = ?", (cur.lastrowid,)).fetchone())
    return jsonify({'event': event}), 201


@app.route('/api/events/<int:event_id>', methods=['GET'])
@require_auth
def get_event(event_id):
    db = get_db()
    event = _row_to_dict(db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone())
    if not event:
        return jsonify({'error': 'not found'}), 404
    try:
        event['custom_messages'] = json.loads(event.get('custom_messages') or '{}')
    except (ValueError, TypeError):
        event['custom_messages'] = {}
    return jsonify({'event': event})


@app.route('/api/events/<int:event_id>', methods=['PATCH'])
@require_auth
def update_event(event_id):
    db = get_db()
    existing = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not existing:
        return jsonify({'error': 'not found'}), 404

    body = request.get_json(force=True, silent=True) or {}
    fields = {}

    if 'name' in body:
        if not (body['name'] or '').strip():
            return jsonify({'error': 'name cannot be empty'}), 400
        fields['name'] = body['name'].strip()

    if 'job_number' in body:
        jn = (body['job_number'] or '').strip()
        if not is_valid_job_number(jn):
            return jsonify({'error': 'job_number must be exactly 7 digits'}), 400
        fields['job_number'] = jn

    if 'status' in body:
        if body['status'] not in VALID_EVENT_STATUSES:
            return jsonify({'error': f'status must be one of {VALID_EVENT_STATUSES}'}), 400
        fields['status'] = body['status']

    if 'timezone' in body:
        tz = body['timezone']
        if tz not in ics_builder.VALID_TIMEZONES:
            return jsonify({'error': f'timezone must be one of {sorted(ics_builder.VALID_TIMEZONES)}'}), 400
        fields['timezone'] = tz

    for key in ('location', 'notes', 'directions'):
        if key in body:
            fields[key] = body[key]

    if 'cover_image_url' in body:
        url = (body['cover_image_url'] or '').strip()
        if url and not re.match(r'^https?://', url):
            return jsonify({'error': 'cover_image_url must start with http:// or https://'}), 400
        fields['cover_image_url'] = _normalize_dropbox_url(url) or None

    if 'cover_image_position' in body:
        pos = body['cover_image_position']
        try:
            pos = int(pos)
        except (TypeError, ValueError):
            return jsonify({'error': 'cover_image_position must be a number 0-100'}), 400
        if not (0 <= pos <= 100):
            return jsonify({'error': 'cover_image_position must be between 0 and 100'}), 400
        fields['cover_image_position'] = pos

    if 'cover_image_position_y' in body:
        pos_y = body['cover_image_position_y']
        try:
            pos_y = int(pos_y)
        except (TypeError, ValueError):
            return jsonify({'error': 'cover_image_position_y must be a number 0-100'}), 400
        if not (0 <= pos_y <= 100):
            return jsonify({'error': 'cover_image_position_y must be between 0 and 100'}), 400
        fields['cover_image_position_y'] = pos_y

    if 'brand_color' in body:
        color = (body['brand_color'] or '').strip()
        if color and not is_valid_hex_color(color):
            return jsonify({'error': 'brand_color must be a 6-digit hex value, e.g. #00B3B2'}), 400
        fields['brand_color'] = color or None

    for key in ('allow_waitlist', 'allow_reschedule'):
        if key in body:
            fields[key] = 1 if body[key] else 0

    if 'max_bookings_per_email' in body:
        fields['max_bookings_per_email'] = body['max_bookings_per_email']

    if 'custom_messages' in body:
        msgs = body['custom_messages']
        if not isinstance(msgs, dict):
            return jsonify({'error': 'custom_messages must be an object'}), 400
        cleaned = {}
        for k, v in msgs.items():
            if k not in VALID_MESSAGE_TYPES:
                return jsonify({'error': f'custom_messages keys must be one of {VALID_MESSAGE_TYPES}'}), 400
            if v is None:
                continue
            if not isinstance(v, str):
                return jsonify({'error': f'custom_messages.{k} must be a string'}), 400
            v = v.strip()
            if v:
                cleaned[k] = v
        fields['custom_messages'] = json.dumps(cleaned)

    if not fields:
        return jsonify({'error': 'no valid fields to update'}), 400

    fields['updated_at'] = _now()
    set_clause = ', '.join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE events SET {set_clause} WHERE id = ?", (*fields.values(), event_id))
    db.commit()

    event = _row_to_dict(db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone())
    return jsonify({'event': event})


@app.route('/api/events/<int:event_id>', methods=['DELETE'])
@require_auth
def delete_event(event_id):
    db = get_db()
    existing = db.execute("SELECT id FROM events WHERE id = ?", (event_id,)).fetchone()
    if not existing:
        return jsonify({'error': 'not found'}), 404

    booking_count = db.execute("""
        SELECT COUNT(*) FROM bookings b
        JOIN slots s ON s.id = b.slot_id
        WHERE s.event_id = ? AND b.status != 'cancelled'
    """, (event_id,)).fetchone()[0]

    if booking_count > 0:
        return jsonify({'error': f'event has {booking_count} active booking(s) — cannot delete'}), 409

    db.execute("DELETE FROM events WHERE id = ?", (event_id,))  # cascades via FK
    db.commit()
    return jsonify({'deleted': True})


# ── Slot Rules (cadence generator) ───────────────────────────────────────────

def _generate_slots_for_rule(db, event_id, rule_id, start_date, end_date,
                              days_of_week, start_time, end_time,
                              slot_length_minutes, capacity):
    """
    Walks the date range day by day; for each matching weekday, lays slots
    back-to-back from start_time to end_time. Inserts directly into `slots`
    tagged source='generated' + slot_rule_id. Returns count created.

    Does not check for overlap against existing slots from other rules or
    manual entries — v1 is additive only. If two rules overlap, both sets
    of slots exist independently. Fine for now; flag if this bites someone.
    """
    now = _now()
    created = 0
    rows = []

    cur_date = start_date
    while cur_date <= end_date:
        if cur_date.weekday() in days_of_week:
            slot_start = datetime.combine(cur_date, start_time)
            day_end = datetime.combine(cur_date, end_time)
            length = timedelta(minutes=slot_length_minutes)

            while slot_start + length <= day_end:
                slot_end = slot_start + length
                rows.append((
                    event_id, slot_start.isoformat(), slot_end.isoformat(),
                    capacity, 'generated', rule_id, now
                ))
                created += 1
                slot_start = slot_end
        cur_date += timedelta(days=1)

    if rows:
        db.executemany("""
            INSERT INTO slots (event_id, start_time, end_time, capacity, source, slot_rule_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, rows)

    return created


@app.route('/api/events/<int:event_id>/slot-rules', methods=['GET'])
@require_auth
def list_slot_rules(event_id):
    db = get_db()
    rows = db.execute(
        "SELECT * FROM slot_rules WHERE event_id = ? ORDER BY created_at DESC", (event_id,)
    ).fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        d['days_of_week'] = json.loads(d['days_of_week'])
        out.append(d)
    return jsonify({'slot_rules': out})


@app.route('/api/events/<int:event_id>/slot-rules', methods=['POST'])
@require_auth
def create_slot_rule(event_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone():
        return jsonify({'error': 'event not found'}), 404

    body = request.get_json(force=True, silent=True) or {}

    try:
        start_date = date.fromisoformat(body.get('start_date', ''))
        end_date = date.fromisoformat(body.get('end_date', ''))
    except (ValueError, TypeError):
        return jsonify({'error': 'start_date and end_date must be YYYY-MM-DD'}), 400

    if end_date < start_date:
        return jsonify({'error': 'end_date must be on or after start_date'}), 400

    if (end_date - start_date).days > MAX_CADENCE_DAYS:
        return jsonify({'error': f'date range cannot exceed {MAX_CADENCE_DAYS} days'}), 400

    days_of_week = body.get('days_of_week')
    if not isinstance(days_of_week, list) or not days_of_week:
        return jsonify({'error': 'days_of_week must be a non-empty list (0=Mon ... 6=Sun)'}), 400
    if not all(isinstance(d, int) and 0 <= d <= 6 for d in days_of_week):
        return jsonify({'error': 'days_of_week values must be integers 0-6 (0=Mon ... 6=Sun)'}), 400

    try:
        start_time = datetime.strptime(body.get('start_time', ''), '%H:%M').time()
        end_time = datetime.strptime(body.get('end_time', ''), '%H:%M').time()
    except (ValueError, TypeError):
        return jsonify({'error': 'start_time and end_time must be HH:MM (24-hour)'}), 400

    if end_time <= start_time:
        return jsonify({'error': 'end_time must be after start_time'}), 400

    try:
        slot_length_minutes = int(body.get('slot_length_minutes'))
        capacity = int(body.get('capacity', 1))
    except (TypeError, ValueError):
        return jsonify({'error': 'slot_length_minutes and capacity must be integers'}), 400

    if slot_length_minutes <= 0:
        return jsonify({'error': 'slot_length_minutes must be greater than 0'}), 400
    if capacity < 1:
        return jsonify({'error': 'capacity must be at least 1'}), 400

    now = _now()
    cur = db.execute("""
        INSERT INTO slot_rules
            (event_id, start_date, end_date, days_of_week, start_time, end_time,
             slot_length_minutes, capacity, slots_generated, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
    """, (
        event_id, start_date.isoformat(), end_date.isoformat(), json.dumps(days_of_week),
        start_time.strftime('%H:%M'), end_time.strftime('%H:%M'),
        slot_length_minutes, capacity, now
    ))
    rule_id = cur.lastrowid

    created = _generate_slots_for_rule(
        db, event_id, rule_id, start_date, end_date,
        set(days_of_week), start_time, end_time, slot_length_minutes, capacity
    )

    db.execute("UPDATE slot_rules SET slots_generated = ? WHERE id = ?", (created, rule_id))
    db.commit()

    rule = _row_to_dict(db.execute("SELECT * FROM slot_rules WHERE id = ?", (rule_id,)).fetchone())
    rule['days_of_week'] = json.loads(rule['days_of_week'])
    return jsonify({'slot_rule': rule, 'slots_created': created}), 201


@app.route('/api/slot-rules/<int:rule_id>', methods=['DELETE'])
@require_auth
def delete_slot_rule(rule_id):
    """
    Deletes the rule only. Already-generated slots are left in place
    (they're independent rows by this point — same logic as the Rooms
    rename/redirect asymmetry in HQ: deleting the generator is not the
    same as deleting what it generated). Pass ?cascade=true to also
    remove slots from this rule that have no active bookings.
    """
    db = get_db()
    rule = db.execute("SELECT * FROM slot_rules WHERE id = ?", (rule_id,)).fetchone()
    if not rule:
        return jsonify({'error': 'not found'}), 404

    cascade = request.args.get('cascade', '').lower() == 'true'
    removed_slots = 0

    if cascade:
        blocked = db.execute("""
            SELECT COUNT(*) FROM bookings b
            JOIN slots s ON s.id = b.slot_id
            WHERE s.slot_rule_id = ? AND b.status != 'cancelled'
        """, (rule_id,)).fetchone()[0]
        if blocked > 0:
            return jsonify({
                'error': f'{blocked} slot(s) from this rule have active bookings — cannot cascade delete'
            }), 409
        cur = db.execute("DELETE FROM slots WHERE slot_rule_id = ?", (rule_id,))
        removed_slots = cur.rowcount

    db.execute("DELETE FROM slot_rules WHERE id = ?", (rule_id,))
    db.commit()
    return jsonify({'deleted': True, 'slots_removed': removed_slots})


# ── Slots (manual + generated, unified) ──────────────────────────────────────

@app.route('/api/events/<int:event_id>/slots', methods=['GET'])
@require_auth
def list_slots(event_id):
    db = get_db()
    rows = db.execute("""
        SELECT s.*,
            (SELECT COUNT(*) FROM bookings b WHERE b.slot_id = s.id AND b.status = 'confirmed') AS confirmed_count,
            (SELECT COUNT(*) FROM bookings b WHERE b.slot_id = s.id AND b.status = 'waitlisted') AS waitlisted_count
        FROM slots s
        WHERE s.event_id = ?
        ORDER BY s.start_time ASC
    """, (event_id,)).fetchall()
    return jsonify({'slots': [_row_to_dict(r) for r in rows]})


@app.route('/api/events/<int:event_id>/slots', methods=['POST'])
@require_auth
def create_manual_slot(event_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone():
        return jsonify({'error': 'event not found'}), 404

    body = request.get_json(force=True, silent=True) or {}
    try:
        start_time = datetime.fromisoformat(body.get('start_time', ''))
        end_time = datetime.fromisoformat(body.get('end_time', ''))
    except (ValueError, TypeError):
        return jsonify({'error': 'start_time and end_time must be ISO datetimes'}), 400

    if end_time <= start_time:
        return jsonify({'error': 'end_time must be after start_time'}), 400

    try:
        capacity = int(body.get('capacity', 1))
    except (TypeError, ValueError):
        return jsonify({'error': 'capacity must be an integer'}), 400
    if capacity < 1:
        return jsonify({'error': 'capacity must be at least 1'}), 400

    now = _now()
    cur = db.execute("""
        INSERT INTO slots (event_id, start_time, end_time, capacity, location_override,
                            notes_override, source, slot_rule_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'manual', NULL, ?)
    """, (
        event_id, start_time.isoformat(), end_time.isoformat(), capacity,
        body.get('location_override'), body.get('notes_override'), now
    ))
    db.commit()
    slot = _row_to_dict(db.execute("SELECT * FROM slots WHERE id = ?", (cur.lastrowid,)).fetchone())
    return jsonify({'slot': slot}), 201


@app.route('/api/slots/<int:slot_id>', methods=['PATCH'])
@require_auth
def update_slot(slot_id):
    db = get_db()
    existing = db.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
    if not existing:
        return jsonify({'error': 'not found'}), 404

    body = request.get_json(force=True, silent=True) or {}
    fields = {}

    if 'start_time' in body or 'end_time' in body:
        try:
            start_time = datetime.fromisoformat(body.get('start_time', existing['start_time']))
            end_time = datetime.fromisoformat(body.get('end_time', existing['end_time']))
        except (ValueError, TypeError):
            return jsonify({'error': 'start_time and end_time must be ISO datetimes'}), 400
        if end_time <= start_time:
            return jsonify({'error': 'end_time must be after start_time'}), 400
        fields['start_time'] = start_time.isoformat()
        fields['end_time'] = end_time.isoformat()

    if 'capacity' in body:
        try:
            capacity = int(body['capacity'])
        except (TypeError, ValueError):
            return jsonify({'error': 'capacity must be an integer'}), 400
        if capacity < 1:
            return jsonify({'error': 'capacity must be at least 1'}), 400
        booked = db.execute(
            "SELECT COUNT(*) FROM bookings WHERE slot_id = ? AND status = 'confirmed'", (slot_id,)
        ).fetchone()[0]
        if capacity < booked:
            return jsonify({'error': f'cannot set capacity below {booked} existing confirmed booking(s)'}), 409
        fields['capacity'] = capacity

    for key in ('location_override', 'notes_override'):
        if key in body:
            fields[key] = body[key]

    if not fields:
        return jsonify({'error': 'no valid fields to update'}), 400

    set_clause = ', '.join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE slots SET {set_clause} WHERE id = ?", (*fields.values(), slot_id))
    db.commit()
    slot = _row_to_dict(db.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone())
    return jsonify({'slot': slot})


@app.route('/api/slots/<int:slot_id>', methods=['DELETE'])
@require_auth
def delete_slot(slot_id):
    db = get_db()
    existing = db.execute("SELECT id FROM slots WHERE id = ?", (slot_id,)).fetchone()
    if not existing:
        return jsonify({'error': 'not found'}), 404

    active = db.execute(
        "SELECT COUNT(*) FROM bookings WHERE slot_id = ? AND status != 'cancelled'", (slot_id,)
    ).fetchone()[0]
    if active > 0:
        return jsonify({'error': f'slot has {active} active booking(s) — cannot delete'}), 409

    db.execute("DELETE FROM slots WHERE id = ?", (slot_id,))
    db.commit()
    return jsonify({'deleted': True})


# ── Form Fields ───────────────────────────────────────────────────────────────

@app.route('/api/events/<int:event_id>/form-fields', methods=['GET'])
@require_auth
def list_form_fields(event_id):
    db = get_db()
    rows = db.execute(
        "SELECT * FROM form_fields WHERE event_id = ? ORDER BY sort_order ASC, id ASC", (event_id,)
    ).fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        d['options'] = json.loads(d['options'])
        out.append(d)
    return jsonify({'form_fields': out})


@app.route('/api/events/<int:event_id>/form-fields', methods=['POST'])
@require_auth
def create_form_field(event_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone():
        return jsonify({'error': 'event not found'}), 404

    body = request.get_json(force=True, silent=True) or {}
    label = (body.get('label') or '').strip()
    field_type = body.get('field_type')

    if not label:
        return jsonify({'error': 'label is required'}), 400
    if field_type not in VALID_FIELD_TYPES:
        return jsonify({'error': f'field_type must be one of {VALID_FIELD_TYPES}'}), 400

    options = body.get('options') or []
    if field_type == 'select' and not (isinstance(options, list) and len(options) >= 1):
        return jsonify({'error': 'select fields require at least one option'}), 400

    max_sort = db.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM form_fields WHERE event_id = ?", (event_id,)
    ).fetchone()[0]

    cur = db.execute("""
        INSERT INTO form_fields (event_id, label, field_type, options, required, sort_order)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        event_id, label, field_type, json.dumps(options if field_type == 'select' else []),
        1 if body.get('required') else 0, max_sort + 1
    ))
    db.commit()

    field = _row_to_dict(db.execute("SELECT * FROM form_fields WHERE id = ?", (cur.lastrowid,)).fetchone())
    field['options'] = json.loads(field['options'])
    return jsonify({'form_field': field}), 201


@app.route('/api/form-fields/<int:field_id>', methods=['PATCH'])
@require_auth
def update_form_field(field_id):
    db = get_db()
    existing = db.execute("SELECT * FROM form_fields WHERE id = ?", (field_id,)).fetchone()
    if not existing:
        return jsonify({'error': 'not found'}), 404

    body = request.get_json(force=True, silent=True) or {}
    fields = {}

    if 'label' in body:
        if not (body['label'] or '').strip():
            return jsonify({'error': 'label cannot be empty'}), 400
        fields['label'] = body['label'].strip()

    if 'field_type' in body:
        if body['field_type'] not in VALID_FIELD_TYPES:
            return jsonify({'error': f'field_type must be one of {VALID_FIELD_TYPES}'}), 400
        fields['field_type'] = body['field_type']

    if 'options' in body:
        fields['options'] = json.dumps(body['options'] or [])

    if 'required' in body:
        fields['required'] = 1 if body['required'] else 0

    if 'sort_order' in body:
        fields['sort_order'] = body['sort_order']

    if not fields:
        return jsonify({'error': 'no valid fields to update'}), 400

    set_clause = ', '.join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE form_fields SET {set_clause} WHERE id = ?", (*fields.values(), field_id))
    db.commit()

    field = _row_to_dict(db.execute("SELECT * FROM form_fields WHERE id = ?", (field_id,)).fetchone())
    field['options'] = json.loads(field['options'])
    return jsonify({'form_field': field})


@app.route('/api/form-fields/<int:field_id>', methods=['DELETE'])
@require_auth
def delete_form_field(field_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM form_fields WHERE id = ?", (field_id,)).fetchone():
        return jsonify({'error': 'not found'}), 404
    db.execute("DELETE FROM form_fields WHERE id = ?", (field_id,))
    db.commit()
    return jsonify({'deleted': True})


# ── Bookings — admin read + manual waitlist promotion ────────────────────────

@app.route('/api/events/<int:event_id>/bookings', methods=['GET'])
@require_auth
def list_bookings(event_id):
    db = get_db()
    rows = db.execute("""
        SELECT b.*, s.start_time, s.end_time, s.capacity AS slot_capacity
        FROM bookings b
        JOIN slots s ON s.id = b.slot_id
        WHERE s.event_id = ?
        ORDER BY s.start_time ASC
    """, (event_id,)).fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        d['custom_field_answers'] = json.loads(d['custom_field_answers'])
        out.append(d)
    return jsonify({'bookings': out})


# ── Bookings export — CSV + PDF ──────────────────────────────────────────────
# Both exporters read from the same field registry so adding a field later
# (e.g. exposing a custom form-field answer on the PDF) is a one-line flag
# flip here, not new plumbing in two places.

def _fmt_slot_time(row):
    """Full format — used by the CSV export, which has no column-width
    constraint, so weekday + year stay in for clarity."""
    start = datetime.fromisoformat(row['start_time'])
    end = datetime.fromisoformat(row['end_time'])
    return f"{start.strftime('%a, %b %-d, %Y %-I:%M %p')} – {end.strftime('%-I:%M %p')}"


def _fmt_slot_time_pdf(row):
    """Compact format for the printed PDF table only. The full CSV format
    (weekday + full year) measures ~2.36in at 9.5pt Helvetica — wider than
    the Time column ever had room for, which is what caused rows to
    overlap into the Location column. Dropping the year (redundant for a
    same-year event; the export is generated close to the event date
    anyway) and tightening the AM/PM spacing brings the worst realistic
    case (long month name, crosses AM/PM) to ~2.0in, safely inside the
    2.2in the column now gets — see _draw_pdf_section's col_x."""
    start = datetime.fromisoformat(row['start_time'])
    end = datetime.fromisoformat(row['end_time'])
    return f"{start.strftime('%a, %b %-d')}, {start.strftime('%-I:%M')}–{end.strftime('%-I:%M %p')}" \
        if start.strftime('%p') == end.strftime('%p') \
        else f"{start.strftime('%a, %b %-d')}, {start.strftime('%-I:%M %p')}–{end.strftime('%-I:%M %p')}"


def _truncate_to_width(text, font, size, max_width):
    """Pixel-width-aware truncation — replaces naive character-count
    slicing, which doesn't map to rendered width in a proportional font
    like Helvetica and was the root cause of the Time column overflowing
    into Location. Adds an ellipsis only when actually cut."""
    text = text or ''
    if pdfmetrics.stringWidth(text, font, size) <= max_width:
        return text
    ellipsis = '…'
    while text and pdfmetrics.stringWidth(text + ellipsis, font, size) > max_width:
        text = text[:-1]
    return (text + ellipsis) if text else ellipsis


# Each entry: id, label, value(row, event) -> str, and whether it belongs
# in the CSV / PDF export. `row` here is a joined booking+slot dict (as
# returned by the query below) — NOT the raw bookings table row.
BOOKING_EXPORT_FIELDS = [
    {'id': 'name',     'label': 'Name',     'value': lambda row, event: row['name'],
     'in_csv': True, 'in_pdf': True},
    {'id': 'email',    'label': 'Email',    'value': lambda row, event: row['email'],
     'in_csv': True, 'in_pdf': False},
    {'id': 'slot_time','label': 'Time',     'value': lambda row, event: _fmt_slot_time(row),
     'in_csv': True, 'in_pdf': True},
    {'id': 'location', 'label': 'Location', 'value': lambda row, event: (row['location_override'] or event['location'] or ''),
     'in_csv': True, 'in_pdf': True},
    {'id': 'status',   'label': 'Status',   'value': lambda row, event: row['status'],
     'in_csv': True, 'in_pdf': False},
]


def _export_bookings_query(db, event_id):
    """Bookings joined with slot fields the registry needs, excluding
    cancelled bookings — cancelled rows are noise in an export meant to
    tell a producer or a client who's actually showing up."""
    rows = db.execute("""
        SELECT b.*, s.start_time, s.end_time, s.location_override
        FROM bookings b
        JOIN slots s ON s.id = b.slot_id
        WHERE s.event_id = ? AND b.status != 'cancelled'
        ORDER BY s.start_time ASC
    """, (event_id,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def _export_form_fields(db, event_id):
    """Custom form fields become extra CSV columns dynamically — every
    event has a different set, so these can't live in the static registry
    above. Not included on the PDF by default (in_pdf=False) — flip that
    per-field here if a future event needs one surfaced there."""
    rows = db.execute(
        "SELECT * FROM form_fields WHERE event_id = ? ORDER BY sort_order ASC, id ASC", (event_id,)
    ).fetchall()
    out = []
    for f in rows:
        field_id = str(f['id'])
        out.append({
            'id': f'custom_{field_id}',
            'label': f['label'],
            'value': (lambda row, event, fid=field_id: (
                json.loads(row['custom_field_answers'] or '{}').get(fid, '')
            )),
            'in_csv': True,
            'in_pdf': False,
        })
    return out


@app.route('/api/events/<int:event_id>/export/csv')
@require_auth
def export_bookings_csv(event_id):
    db = get_db()
    event = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not event:
        return jsonify({'error': 'not found'}), 404

    fields = [f for f in BOOKING_EXPORT_FIELDS if f['in_csv']] + \
        [f for f in _export_form_fields(db, event_id) if f['in_csv']]
    rows = _export_bookings_query(db, event_id)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([f['label'] for f in fields])
    for row in rows:
        writer.writerow([f['value'](row, event) for f in fields])

    filename = f"{event['slug']}-bookings.csv"
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


def _draw_pdf_footer(c, page_num):
    c.setFont('Helvetica', 8)
    c.setFillColor(colors.HexColor('#8A8A8A'))
    c.drawString(0.75 * inch, 0.5 * inch, f"Generated {datetime.now().strftime('%B %-d, %Y')}")
    c.drawRightString(letter[0] - 0.75 * inch, 0.5 * inch, f"Page {page_num}")


def _draw_pdf_section(c, y, title, rows, event, fields, accent_hex, page_num=1):
    """Draws one section (Confirmed or Waitlist) starting at height y,
    returns (new_y, final_page_num). Caller skips calling this at all when
    a section has no rows — the PDF simply omits it rather than showing an
    empty heading with nothing underneath. page_num is passed in (not
    reset to 1) so a page break inside the Waitlist section continues
    numbering from wherever the Confirmed section left off.

    Column widths: Name 1.8in, Time 2.2in, Location gets the remainder
    (~3.0in on letter). Time needs the extra room — even the compact PDF
    time format (_fmt_slot_time_pdf) can hit ~2.0in on a long month name
    that crosses AM/PM, so 2.2in leaves a safety margin. A 0.15in gutter
    is subtracted from every column's truncation width so text can never
    visually touch the next column, even at the absolute limit."""
    page_w, page_h = letter
    margin = 0.75 * inch
    col_x = [margin, margin + 1.8 * inch, margin + 4.0 * inch]
    col_widths = [1.8 * inch, 2.2 * inch, (page_w - margin) - (margin + 4.0 * inch)]
    gutter = 0.15 * inch
    row_h = 0.28 * inch
    body_font, body_size = 'Helvetica', 9.5

    c.setFont('Helvetica-Bold', 11)
    c.setFillColor(colors.HexColor(accent_hex))
    c.drawString(margin, y, title.upper())
    y -= 6
    c.setStrokeColor(colors.HexColor(accent_hex))
    c.setLineWidth(1)
    c.line(margin, y, page_w - margin, y)
    y -= 20

    c.setFont('Helvetica-Bold', 9)
    c.setFillColor(colors.HexColor('#1a1a1a'))
    for label, x in zip(('Name', 'Time', 'Location'), col_x):
        c.drawString(x, y, label.upper())
    y -= 4
    c.setStrokeColor(colors.HexColor('#DADADA'))
    c.setLineWidth(0.5)
    c.line(margin, y, page_w - margin, y)
    y -= 16

    field_by_id = {f['id']: f for f in fields}
    shaded = False
    for row in rows:
        if y < 1 * inch:
            _draw_pdf_footer(c, page_num)
            c.showPage()
            page_num += 1
            y = page_h - 1 * inch

        if shaded:
            c.setFillColor(colors.HexColor('#F5F5F5'))
            c.rect(margin - 4, y - 6, (page_w - 2 * margin) + 8, row_h, fill=1, stroke=0)
        shaded = not shaded

        values = [
            field_by_id['name']['value'](row, event),
            _fmt_slot_time_pdf(row),
            field_by_id['location']['value'](row, event) or '—',
        ]

        c.setFont(body_font, body_size)
        c.setFillColor(colors.HexColor('#1a1a1a'))
        for val, x, w in zip(values, col_x, col_widths):
            c.drawString(x, y, _truncate_to_width(val, body_font, body_size, w - gutter))
        y -= row_h

    return y - 14, page_num


@app.route('/api/events/<int:event_id>/export/pdf')
@require_auth
def export_bookings_pdf(event_id):
    db = get_db()
    event = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not event:
        return jsonify({'error': 'not found'}), 404

    fields = BOOKING_EXPORT_FIELDS
    rows = _export_bookings_query(db, event_id)
    confirmed = [r for r in rows if r['status'] == 'confirmed']
    waitlisted = [r for r in rows if r['status'] == 'waitlisted']

    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=letter)
    page_w, page_h = letter
    margin = 0.75 * inch
    accent = '#00B3B2'

    # Header — white background (print-friendly, not the app's dark theme),
    # teal used only as a thin accent rule, never a filled block, so it
    # holds up on B&W/laser printers too. Job number intentionally omitted.
    y = page_h - 1 * inch
    c.setFont('Helvetica-Bold', 20)
    c.setFillColor(colors.HexColor('#1a1a1a'))
    c.drawString(margin, y, event['name'])
    y -= 20
    if event['location']:
        c.setFont('Helvetica', 10)
        c.setFillColor(colors.HexColor('#5E6E7E'))
        c.drawString(margin, y, event['location'])
        y -= 10
    y -= 6
    c.setStrokeColor(colors.HexColor(accent))
    c.setLineWidth(2)
    c.line(margin, y, page_w - margin, y)
    y -= 34

    page_num = 1
    if confirmed:
        y, page_num = _draw_pdf_section(c, y, 'Confirmed', confirmed, event, fields, accent, page_num=page_num)
    if waitlisted:
        if y < 1.5 * inch:
            _draw_pdf_footer(c, page_num)
            c.showPage()
            page_num += 1
            y = page_h - 1 * inch
        y, page_num = _draw_pdf_section(c, y, 'Waitlist', waitlisted, event, fields, '#5E6E7E', page_num=page_num)
    if not confirmed and not waitlisted:
        c.setFont('Helvetica', 11)
        c.setFillColor(colors.HexColor('#5E6E7E'))
        c.drawString(margin, y, 'No bookings yet.')

    _draw_pdf_footer(c, page_num)
    c.save()
    buf.seek(0)

    filename = f"{event['slug']}-schedule.pdf"
    return Response(
        buf.getvalue(),
        mimetype='application/pdf',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


@app.route('/api/bookings/<int:booking_id>/promote', methods=['POST'])
@require_auth
def promote_booking(booking_id):
    """
    Manual waitlist promotion only — no auto-promotion when a slot opens
    up. Admin sees the waitlist on the event detail page and triggers this
    explicitly. Confirms the booking if there's room, then sends the same
    calendar invite a normally-confirmed booking gets — nothing was on the
    recipient's calendar while waitlisted, so this is their first invite.
    """
    db = get_db()
    booking = db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if not booking:
        return jsonify({'error': 'not found'}), 404
    if booking['status'] != 'waitlisted':
        return jsonify({'error': 'only waitlisted bookings can be promoted'}), 400

    slot = db.execute("SELECT * FROM slots WHERE id = ?", (booking['slot_id'],)).fetchone()
    confirmed_count = db.execute(
        "SELECT COUNT(*) FROM bookings WHERE slot_id = ? AND status = 'confirmed'", (slot['id'],)
    ).fetchone()[0]
    if confirmed_count >= slot['capacity']:
        return jsonify({'error': 'slot is still full — no room to promote'}), 409

    event = db.execute("SELECT * FROM events WHERE id = ?", (slot['event_id'],)).fetchone()

    db.execute("UPDATE bookings SET status = 'confirmed' WHERE id = ?", (booking_id,))
    db.commit()

    cancel_url = f"/book/{event['slug']}/cancel/{booking['cancel_token']}"
    reschedule_url = (
        f"/book/{event['slug']}/reschedule/{booking['reschedule_token']}"
        if event['allow_reschedule'] else None
    )
    send_booking_invite(
        booking_id=booking['id'], name=booking['name'], email=booking['email'],
        event=event, slot=slot, cancel_url=cancel_url, reschedule_url=reschedule_url,
    )
    return jsonify({'promoted': True})


@app.route('/api/bookings/<int:booking_id>', methods=['DELETE'])
@require_auth
def delete_booking(booking_id):
    """Hard delete — no cancellation email, no ICS CANCEL sent. This is
    distinct from the public cancel flow (which soft-cancels and notifies
    the client); this route is for admin cleanup of junk/test/duplicate
    bookings, where the person doesn't expect or want an email at all.
    If a real client booking needs to be removed *with* notification, use
    the existing cancel flow instead — don't repurpose this route for that."""
    db = get_db()
    booking = db.execute("SELECT id FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if not booking:
        return jsonify({'error': 'not found'}), 404
    db.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
    db.commit()
    return jsonify({'deleted': True})


@app.route('/api/events/<int:event_id>/bookings', methods=['DELETE'])
@require_auth
def clear_bookings(event_id):
    """Hard delete every booking for this event — no cancellation emails
    sent for any of them. Same silent-cleanup rationale as delete_booking()
    above, just scoped to the whole event at once."""
    db = get_db()
    event = db.execute("SELECT id FROM events WHERE id = ?", (event_id,)).fetchone()
    if not event:
        return jsonify({'error': 'not found'}), 404
    cur = db.execute("""
        DELETE FROM bookings WHERE id IN (
            SELECT b.id FROM bookings b JOIN slots s ON s.id = b.slot_id WHERE s.event_id = ?
        )
    """, (event_id,))
    db.commit()
    return jsonify({'deleted': True, 'count': cur.rowcount})


@app.route('/api/admin/test-invite', methods=['POST'])
@require_auth
def send_test_invite():
    """
    Sends a real invite or cancel through the exact same send_email() /
    ics_builder path a live booking uses — the only thing synthetic is the
    slot (tomorrow 10:00-10:30 Central) and the UID, which is namespaced
    'test-...' so it can never collide with a real booking's UID.

    Two calls, same UID, is the actual test: send 'invite', accept it in
    your calendar app, then send 'cancel' and confirm it disappears. That
    proves the REQUEST/CANCEL pairing works end to end — a single send
    only proves email delivery, not that the invite behaves like one.

    Bug fix (2026-07-09): this handler previously had no route decorator —
    it was stranded as unreachable code after clear_bookings()'s return
    statement, so the admin panel's "Send Test Invite"/"Send Test Cancel"
    buttons 404'd silently. Extracted into its own route, unchanged
    otherwise.
    """
    user = get_current_user()
    body = request.get_json(force=True, silent=True) or {}
    event_id = body.get('event_id')
    action = body.get('action', 'invite')
    to_email = (body.get('to') or (user.get('email') if user else '') or '').strip()

    if action not in ('invite', 'cancel'):
        return jsonify({'error': "action must be 'invite' or 'cancel'"}), 400
    if not to_email or not _is_valid_email(to_email):
        return jsonify({'error': 'A valid recipient email is required'}), 400

    db = get_db()
    event = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not event:
        return jsonify({'error': 'event not found'}), 404

    tomorrow = date.today() + timedelta(days=1)
    test_start = datetime.combine(tomorrow, datetime.min.time().replace(hour=10))
    test_end = test_start + timedelta(minutes=30)
    uid = ics_builder.test_uid(event['id'], (user.get('id') if user else 'admin'))
    to_name = (user.get('name') if user else None) or to_email

    location = event['location'] or ''
    if action == 'cancel':
        ics_bytes = ics_builder.build_cancel_ics(
            uid=uid, summary=f"[TEST] {event['name']}", start=test_start, end=test_end,
            attendee_email=to_email, attendee_name=to_name, location=location,
            timezone=event['timezone'],
        )
        subject = f"[TEST] Cancelled — {event['name']}"
        html_body = (
            "<p>This is a <strong>test cancellation</strong> from the AJ Bookings invite pipeline.</p>"
            "<p>If the earlier test invite is on your calendar, it should now disappear or show as cancelled.</p>"
        )
        filename, content_type = 'test-cancel.ics', 'text/calendar; method=CANCEL'
    else:
        ics_bytes = ics_builder.build_invite_ics(
            uid=uid, summary=f"[TEST] {event['name']}", start=test_start, end=test_end,
            attendee_email=to_email, attendee_name=to_name, location=location,
            description="Test invite from AJ Bookings — safe to accept or decline. "
                        "Trigger a test cancel from the same admin panel afterward to confirm that path too.",
            timezone=event['timezone'],
        )
        subject = f"[TEST] Calendar invite — {event['name']}"
        html_body = (
            "<p>This is a <strong>test invite</strong> from the AJ Bookings calendar-invite pipeline.</p>"
            "<p>Accept it, then come back and send a test cancel to confirm it's pulled off your calendar.</p>"
        )
        filename, content_type = 'test-invite.ics', 'text/calendar; method=REQUEST'

    ok = send_email(to_email, subject, html_body, attachments=[{
        'filename': filename,
        'data_b64': base64.b64encode(ics_bytes).decode(),
        'content_type': content_type,
    }])
    if not ok:
        return jsonify({'error': 'send failed — check server logs'}), 502
    return jsonify({'sent': True, 'to': to_email, 'action': action})


# ── Public booking surface (/book/<slug>) ─────────────────────────────────────
# No AJ header/theme/auth — intentionally outside the AJ visual ecosystem.
# Anyone with the link can view and book. Cancel/reschedule are reached via
# unguessable tokens (secrets.token_urlsafe), not event-scoped auth.

def _slot_state(db, slot):
    """Returns 'open' | 'waitlist' | 'full' for a given slot row."""
    confirmed = db.execute(
        "SELECT COUNT(*) FROM bookings WHERE slot_id = ? AND status = 'confirmed'", (slot['id'],)
    ).fetchone()[0]
    if confirmed < slot['capacity']:
        return 'open', confirmed
    return 'full', confirmed


def _is_valid_email(val):
    return bool(re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', str(val or '').strip()))


@app.route('/book/<slug>')
def public_booking_page(slug):
    # ?rescheduled=1 is the redirect landing from the reschedule-release POST
    # (see public_reschedule_release) — drives the "pick a new time" banner.
    reschedule_released = request.args.get('rescheduled') == '1'
    return render_template('public_booking.html', slug=slug, reschedule_released=reschedule_released)


@app.route('/book/<slug>/cancel/<token>')
def public_cancel_page(slug, token):
    db = get_db()
    booking = db.execute("SELECT * FROM bookings WHERE cancel_token = ?", (token,)).fetchone()
    event = db.execute("SELECT * FROM events WHERE slug = ?", (slug,)).fetchone()
    return render_template(
        'public_cancel.html', slug=slug, token=token,
        booking_found=booking is not None,
        already_cancelled=(booking is not None and booking['status'] == 'cancelled'),
        event_name=(event['name'] if event else 'this event'),
        brand_color=(event['brand_color'] if event and event['brand_color'] else '#1a1a1a'),
    )


@app.route('/book/<slug>/reschedule/<token>')
def public_reschedule_page(slug, token):
    """
    Read-only confirm page — mirrors public_cancel_page's pattern exactly.
    Deliberately does NOT mutate anything on GET: this link goes out in an
    email, and enterprise mail security (Outlook Safe Links, Mimecast,
    Proofpoint, etc.) pre-fetches every URL in a message to scan it before
    the recipient ever opens it. A GET that mutated (the old behavior) would
    get silently consumed by that scan, burning the one-time token before
    the person ever clicked — which is exactly what "the reschedule link is
    broken" turned out to be. The actual release only happens via the POST
    below, fired by an explicit button tap.
    """
    db = get_db()
    booking = db.execute("SELECT * FROM bookings WHERE reschedule_token = ?", (token,)).fetchone()

    slot = None
    event = None
    if booking:
        slot = db.execute("SELECT * FROM slots WHERE id = ?", (booking['slot_id'],)).fetchone()
        event = db.execute("SELECT * FROM events WHERE id = ?", (slot['event_id'],)).fetchone() if slot else None

    return render_template(
        'public_reschedule.html',
        slug=(event['slug'] if event else slug),
        token=token,
        booking_found=booking is not None,
        already_released=(booking is not None and booking['status'] == 'cancelled'),
        not_allowed=(event is not None and not event['allow_reschedule']),
        event_name=(event['name'] if event else 'this event'),
        brand_color=(event['brand_color'] if event and event['brand_color'] else '#1a1a1a'),
        slot_start=(slot['start_time'] if slot else None),
    )


@app.route('/api/public/bookings/<token>/reschedule', methods=['POST'])
def public_reschedule_release(token):
    """
    Does the actual release. Re-validates everything server-side rather than
    trusting the GET page's earlier read — allow_reschedule could have been
    toggled off between page load and button tap, and this is the only place
    that's allowed to matter. The status-guarded UPDATE (WHERE status !=
    'cancelled') makes this idempotent: a double-click, a retried request, or
    the button somehow firing twice all land on the same outcome — one
    release, one cancel email, not two — rather than relying on a
    check-then-act pair that a race could slip through.
    """
    db = get_db()
    booking = db.execute("SELECT * FROM bookings WHERE reschedule_token = ?", (token,)).fetchone()
    if not booking:
        return jsonify({'error': 'invalid_token'}), 404

    slot = db.execute("SELECT * FROM slots WHERE id = ?", (booking['slot_id'],)).fetchone()
    event = db.execute("SELECT * FROM events WHERE id = ?", (slot['event_id'],)).fetchone() if slot else None
    if not event or not event['allow_reschedule']:
        return jsonify({'error': 'not_allowed'}), 400

    was_confirmed = booking['status'] == 'confirmed'
    cur = db.execute(
        "UPDATE bookings SET status = 'cancelled' WHERE id = ? AND status != 'cancelled'",
        (booking['id'],)
    )
    db.commit()

    if cur.rowcount == 1 and was_confirmed:
        send_booking_cancel(
            booking_id=booking['id'], name=booking['name'], email=booking['email'],
            event=event, slot=slot,
        )
    return jsonify({'ok': True, 'redirect': f"/book/{event['slug']}?rescheduled=1"})


# ── Public API — read ─────────────────────────────────────────────────────────

@app.route('/api/public/events/<slug>')
def public_get_event(slug):
    db = get_db()
    event = db.execute("SELECT * FROM events WHERE slug = ?", (slug,)).fetchone()
    if not event:
        return jsonify({'error': 'not found'}), 404
    if event['status'] != 'active':
        return jsonify({'error': 'not_bookable', 'status': event['status']}), 404

    slots = db.execute(
        "SELECT * FROM slots WHERE event_id = ? AND start_time > ? ORDER BY start_time ASC",
        (event['id'], _now())
    ).fetchall()

    slot_list = []
    for s in slots:
        state, confirmed = _slot_state(db, s)
        if state == 'full' and not event['allow_waitlist']:
            continue  # full + no waitlist = not offered at all
        slot_list.append({
            'id': s['id'],
            'start_time': s['start_time'],
            'end_time': s['end_time'],
            'capacity': s['capacity'],
            'confirmed_count': confirmed,
            'location_override': s['location_override'],
            'state': state if state == 'open' else ('waitlist' if event['allow_waitlist'] else 'full'),
        })

    fields = db.execute(
        "SELECT * FROM form_fields WHERE event_id = ? ORDER BY sort_order ASC, id ASC", (event['id'],)
    ).fetchall()
    field_list = []
    for f in fields:
        d = _row_to_dict(f)
        d['options'] = json.loads(d['options'])
        field_list.append(d)

    return jsonify({
        'event': {
            'name': event['name'],
            'location': event['location'],
            'notes': event['notes'],
            'directions': event['directions'],
            'cover_image_url': event['cover_image_url'],
            'cover_image_position': event['cover_image_position'] if event['cover_image_position'] is not None else 50,
            'cover_image_position_y': event['cover_image_position_y'] if event['cover_image_position_y'] is not None else 50,
            'brand_color': event['brand_color'] or '#1a1a1a',
            'allow_waitlist': bool(event['allow_waitlist']),
            'allow_reschedule': bool(event['allow_reschedule']),
            'timezone': event['timezone'],
        },
        'slots': slot_list,
        'form_fields': field_list,
    })


@app.route('/api/public/events/<slug>/bookings', methods=['POST'])
def public_create_booking(slug):
    db = get_db()
    event = db.execute("SELECT * FROM events WHERE slug = ?", (slug,)).fetchone()
    if not event or event['status'] != 'active':
        return jsonify({'error': 'This event is not currently accepting bookings'}), 404

    body = request.get_json(force=True, silent=True) or {}
    name = (body.get('name') or '').strip()
    email = (body.get('email') or '').strip().lower()
    slot_id = body.get('slot_id')
    answers = body.get('custom_field_answers') or {}

    if not name:
        return jsonify({'error': 'Name is required'}), 400
    if not _is_valid_email(email):
        return jsonify({'error': 'A valid email is required'}), 400

    slot = db.execute(
        "SELECT * FROM slots WHERE id = ? AND event_id = ?", (slot_id, event['id'])
    ).fetchone()
    if not slot:
        return jsonify({'error': 'That slot is no longer available'}), 404
    if datetime.fromisoformat(slot['start_time']) <= datetime.utcnow():
        return jsonify({'error': 'That slot has already passed'}), 400

    # Required custom fields
    fields = db.execute("SELECT * FROM form_fields WHERE event_id = ?", (event['id'],)).fetchall()
    for f in fields:
        if f['required']:
            val = answers.get(str(f['id']))
            if f['field_type'] == 'checkbox':
                if val is not True:
                    return jsonify({'error': f'"{f["label"]}" is required'}), 400
            elif not val or not str(val).strip():
                return jsonify({'error': f'"{f["label"]}" is required'}), 400

    # Per-event cap, checked by email, across all non-cancelled bookings for this event
    if event['max_bookings_per_email']:
        existing = db.execute("""
            SELECT COUNT(*) FROM bookings b
            JOIN slots s ON s.id = b.slot_id
            WHERE s.event_id = ? AND lower(b.email) = ? AND b.status != 'cancelled'
        """, (event['id'], email)).fetchone()[0]
        if existing >= event['max_bookings_per_email']:
            return jsonify({'error': 'You have reached the maximum number of bookings for this event'}), 400

    state, confirmed = _slot_state(db, slot)
    if state == 'full':
        if not event['allow_waitlist']:
            return jsonify({'error': 'That slot is full'}), 409
        status = 'waitlisted'
    else:
        status = 'confirmed'

    now = _now()
    cancel_token = _gen_token()
    reschedule_token = _gen_token()

    cur = db.execute("""
        INSERT INTO bookings (slot_id, name, email, custom_field_answers, status,
                               cancel_token, reschedule_token, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (slot['id'], name, email, json.dumps(answers), status, cancel_token, reschedule_token, now))
    db.commit()

    cancel_url = f'/book/{slug}/cancel/{cancel_token}'
    reschedule_url = f'/book/{slug}/reschedule/{reschedule_token}' if event['allow_reschedule'] else None

    if status == 'confirmed':
        send_booking_invite(
            booking_id=cur.lastrowid, name=name, email=email,
            event=event, slot=slot, cancel_url=cancel_url, reschedule_url=reschedule_url,
        )
    else:
        send_booking_waitlist(name=name, email=email, event=event, cancel_url=cancel_url)

    return jsonify({
        'booking': {
            'id': cur.lastrowid,
            'status': status,
            'cancel_url': cancel_url,
            'reschedule_url': reschedule_url,
        }
    }), 201


@app.route('/api/public/bookings/<token>/cancel', methods=['POST'])
def public_cancel_booking(token):
    db = get_db()
    booking = db.execute("SELECT * FROM bookings WHERE cancel_token = ?", (token,)).fetchone()
    if not booking:
        return jsonify({'error': 'not found'}), 404

    was_confirmed = booking['status'] == 'confirmed'
    cur = db.execute(
        "UPDATE bookings SET status = 'cancelled' WHERE id = ? AND status != 'cancelled'",
        (booking['id'],)
    )
    db.commit()

    if cur.rowcount == 0:
        return jsonify({'error': 'already cancelled'}), 400

    if was_confirmed:
        slot = db.execute("SELECT * FROM slots WHERE id = ?", (booking['slot_id'],)).fetchone()
        event = db.execute("SELECT * FROM events WHERE id = ?", (slot['event_id'],)).fetchone()
        send_booking_cancel(
            booking_id=booking['id'], name=booking['name'], email=booking['email'],
            event=event, slot=slot,
        )
    return jsonify({'cancelled': True})


if __name__ == '__main__':
    app.run(debug=not _IS_PROD, port=int(os.environ.get('PORT', 5000)))
