"""
aj_auth.py — Shared auth module for AJ internal tools.
Drop into any Flask app's root directory.

Validates sessions against HQ's centralized user store.
Uses token_redirect flow: HQ issues a short-lived token after login,
app validates it once, then caches the user in Flask session locally.

Requires:
  - PLATFORM_SECRET env var (same value as HQ)
  - /auth/validate proxy route in app.py

Roles: 'admin' | 'leadership' | 'staff'
  role is the permission ceiling — controls access to admin UIs and
  destructive operations.

Tags: 'admin' | 'leadership' | 'ad' | 'producer' | 'creative'
  Tags are stackable and drive per-app feature access. role controls
  the ceiling; tags control the lens. Set in the HQ admin UI.

Usage:
    from aj_auth import require_auth, get_current_user, has_tag

    @app.route('/page')
    @require_auth
    def page():
        user = get_current_user()  # { id, name, email, role, tags }

    @app.route('/admin-only')
    @require_auth(role='admin')
    def admin_only(): ...

    @app.route('/leadership-and-above')
    @require_auth(role='leadership')
    def leadership_page(): ...  # passes for 'admin' and 'leadership'

    @app.route('/ad-action')
    @require_auth
    def ad_action():
        if not has_tag('ad'):
            abort(403)
        ...
"""

import os
import functools
from urllib.parse import quote
from flask import request, redirect, g, session, abort

_HQ_BASE     = 'https://aj-hq.up.railway.app'
_HQ_TIMEOUT  = 5
_SESSION_KEY = '_aj_user'

# Role hierarchy — higher index = more permissive
_ROLE_LEVELS = {'staff': 0, 'leadership': 1, 'admin': 2}


def _validate_with_hq(token=None):
    """
    Call HQ /auth/validate server-side.
    Passes ?token= param if provided (cross-app token from URL).
    Returns user dict or None.
    """
    secret = os.environ.get('PLATFORM_SECRET', '')
    try:
        import requests as req
        params = {'token': token} if token else {}
        r = req.get(
            f'{_HQ_BASE}/auth/validate',
            headers={'X-AJ-Key': secret},
            params=params,
            timeout=_HQ_TIMEOUT
        )
        if r.status_code == 200:
            data = r.json()
            return data.get('user') if data.get('valid') else None
    except Exception:
        pass
    return None


def _get_or_validate_user():
    """
    Get the current user, using a two-tier approach:
    1. Local Flask session cache (fast — no HQ round-trip)
    2. Cross-app token in ?token= query param (first visit from HQ login)

    Caches validated user in Flask session so subsequent requests are fast.
    Result also cached on g for the duration of the request.

    Session cache is busted automatically when the aj_session cookie value
    changes — handles switching users on HQ without manual cookie clearing.
    """
    if hasattr(g, '_aj_user'):
        return g._aj_user

    # Detect user switch: if the aj_session cookie has changed since we cached
    # the user, bust the local Flask session and re-validate against HQ.
    current_aj_session = request.cookies.get('aj_session', '')
    cached_aj_session  = session.get('_aj_session_token', '')
    if current_aj_session != cached_aj_session:
        session.pop(_SESSION_KEY, None)
        session.pop('_aj_session_token', None)

    # Tier 1: local session cache
    cached = session.get(_SESSION_KEY)
    if cached:
        g._aj_user = cached
        return g._aj_user

    # Tier 2: cross-app token in URL (first visit after HQ login redirect)
    xapp_token = request.args.get('token')
    if xapp_token:
        user = _validate_with_hq(token=xapp_token)
        if user:
            session[_SESSION_KEY] = user  # cache locally
            session['_aj_session_token'] = current_aj_session
            session.permanent = True
            g._aj_user = user
            return g._aj_user

    # Tier 3: no local cache, no token — try HQ directly (handles page reload
    # after switching users on HQ without a token redirect)
    if current_aj_session and not cached:
        user = _validate_with_hq()
        if user:
            session[_SESSION_KEY] = user
            session['_aj_session_token'] = current_aj_session
            session.permanent = True
            g._aj_user = user
            return g._aj_user

    g._aj_user = None
    return None


def get_current_user():
    """Return the current user dict or None. Safe to call from any route."""
    return _get_or_validate_user()


def has_tag(tag):
    """Return True if the current user has the given functional tag.

    Tags drive per-app feature access and are stackable. They are set in the
    HQ admin UI and travel with the user dict from /auth/validate.

    Valid tags: 'admin' | 'leadership' | 'ad' | 'producer' | 'creative'

    Usage:
        if has_tag('ad'):
            ...  # AD-gated action
        if has_tag('producer') or has_tag('ad'):
            ...  # either can proceed
    """
    import json
    user = _get_or_validate_user()
    if not user:
        return False
    raw = user.get('tags') or '[]'
    try:
        tags = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except (ValueError, TypeError):
        tags = []
    return tag in tags


def require_auth(fn=None, *, role=None):
    """
    Decorator that requires a valid HQ session.

    @require_auth
    def my_view(): ...

    @require_auth(role='admin')
    def admin_view(): ...

    @require_auth(role='leadership')
    def leadership_view(): ...   # passes for admin + leadership

    Unauthenticated → redirects to HQ login with ?next= current URL.
    Wrong role → 403.
    Sets g.user for use in the route.
    """
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            user = _get_or_validate_user()
            if not user:
                login_url = f'{_HQ_BASE}/login'
                # Preserve the path without the token param to keep URLs clean
                next_url = request.url.split('?')[0] if request.args.get('token') else request.url
                return redirect(f'{login_url}?next={quote(next_url, safe="")}')
            if role:
                required_level = _ROLE_LEVELS.get(role, 0)
                user_level     = _ROLE_LEVELS.get(user.get('role', 'staff'), 0)
                if user_level < required_level:
                    abort(403)
            g.user = user
            return f(*args, **kwargs)
        return wrapper

    if fn is not None:
        return decorator(fn)
    return decorator
