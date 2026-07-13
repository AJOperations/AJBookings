# Bookings

AJ-internal scheduling/booking tool. AJ team members create events with time slots (either
generated on a recurring cadence or added one-off), then share a public booking link.
Clients pick a slot on a standalone public page, get a confirmation with cancel/reschedule
links, and — for confirmed or waitlist-promoted bookings — a real calendar invite
(`METHOD:REQUEST`/`METHOD:CANCEL`, built by `ics_builder.py`) sent through HQ's Mailgun proxy.

## Status: retrofit complete

This app has been through the AJ unification wave and is merged to `main`:

- Auth and the HQ data proxy now come from the shared `aj_shared` package
  (`require_auth`, `get_current_user`, `register_proxy`, `require_env_secret`,
  `configure_session_security`, `register_error_handlers`) instead of hand-rolled copies.
- `aj_shared` is pinned in `requirements.txt` to `git+https://github.com/AJOperations/aj-shared@v1.2.0`
  (tagged release, not `main`).
- Admin pages (`admin.html`, `admin_events.html`, `admin_event_detail.html`) load the shared
  sidebar shell via HQ-served `aj-theme.css` and `aj-utils.js`, initialized with `ajInitShell()`.
  The public booking/cancel/reschedule pages intentionally do **not** use the shell — they're
  standalone branded pages with no AJ header.
- CORS is handled by `register_proxy()`'s fixed AJ-hostname allowlist, replacing this app's
  former broad `*.up.railway.app` / `*.netlify.app` regex match.
- Wave review fixed two issues before merge: `BOOKINGS_BASE_URL` was hardcoded to production
  (would have leaked prod links into cancel/reschedule/invite emails during staging testing) —
  it's now environment-driven; and an auth-decorator change that had strayed onto the slot-CRUD
  routes outside its intended carve-out was reverted.
- No role restriction on admin auth — any logged-in AJ user can manage any event (all events
  visible to all users; not per-owner filtered).
- Job numbers are validated as a 7-digit format check everywhere (`is_valid_job_number()` in
  `app.py`) — no live HQ registry lookup.

### Open bug (unreproduced)

A report of "cancel regenerates 8 slots" is open and parked — not yet reproduced from reading
the code. Needs a screenshot or timestamp from whoever hit it before it can be investigated
further.

### Also shipped outside the wave

- A dead test-invite route was removed.
- Per-event timezone selection was added (client-requested feature), on top of the existing
  hardcoded America/Chicago assumption in `ics_builder.py`'s `VTIMEZONE` block.

## Stack

- Python / Flask, SQLite, gunicorn
- `reportlab` for PDF export of bookings
- Deployed on Railway via `Dockerfile` (no Procfile in this repo) — hosting is unchanged by
  the retrofit, per wave rules.
- gunicorn runs `--workers 1 --preload --timeout 300` — required for SQLite safety; do not
  change the worker count.

## Running locally

```bash
pip install -r requirements.txt   # requires git (for the aj-shared pinned-tag install)
export FLASK_SECRET_KEY=$(openssl rand -hex 32)
export PLATFORM_SECRET=...        # same value as other AJ apps / HQ
export DATABASE_PATH=./data/bookings.db   # optional, defaults to /app/data/bookings.db
export FLASK_ENV=development       # omit/production hard-fails on missing secrets
python app.py                      # runs on $PORT or 5000
```

`FLASK_SECRET_KEY` and `PLATFORM_SECRET` are loaded via `aj_shared.require_env_secret()`,
which refuses to start in production if either is unset (no silent fallback).

## Key environment variables (as referenced in `app.py`)

| Var | Purpose |
|---|---|
| `FLASK_SECRET_KEY` | Flask session signing key; required, fail-loud in production |
| `PLATFORM_SECRET` | Shared HQ platform secret used by the `aj_shared` auth/proxy contract |
| `DATABASE_PATH` | SQLite file path; defaults to `/app/data/bookings.db` |
| `FLASK_ENV` | `production` (default) hard-fails on missing secrets; anything else relaxes that |
| `BOOKINGS_BASE_URL` | Base URL used to build cancel/reschedule/invite links; defaults to the production Railway URL if unset — **must be set per-environment** so staging doesn't emit prod links (this was a wave-review fix) |
| `TEST_EMAIL_OVERRIDE` | Optional; if set, redirects every outbound email recipient here before the call reaches HQ's Mailgun proxy. Leave unset in production |
| `PORT` | Local dev only; defaults to 5000 |

## HQ integration

- All HubSpot/external-service writes and reference-data reads go through HQ's proxy
  (`register_proxy()` from `aj_shared`) — this app has no external credentials of its own.
- `send_email()` calls HQ's `/api/email/send` directly (not through a local self-proxy route),
  since a synchronous self-call would deadlock this app's single gunicorn worker.

## Constraints (unchanged by the retrofit)

- SQLite + `--workers 1` — never touch the worker count.
- 7-digit job numbers, hard-validated everywhere.
- `client_name` is the FK for tool/job association; `client_slug` is for URLs only.
- Cadence generator is capped at 366 days per rule to guard against typo'd date ranges.
- Deleting a `slot_rule` does not cascade-delete its generated slots unless `?cascade=true`
  is passed, and cascade refuses if any of those slots have active bookings.
