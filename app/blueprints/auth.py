"""Authentication blueprint.

Handles login/logout, CSRF protection, request logging, and provides the
login_required + admin_required decorators.

Sentry is Dockd's identity provider: `/login` verifies credentials against
Sentry's auth API (see app/services/sentry_auth.py) and stores only
`{name, role}` in Dockd's signed-cookie session. Dockd holds no user store
and no passwords; user management, password rotation, and the failed-login
lockout all live in Sentry. Roles (admin / user) gate the admin-only
settings surface.
"""

import os
import time
import logging
from functools import wraps
from flask import Blueprint, request, jsonify, session, current_app
from app.extensions import limiter
from app.services.sentry_auth import (
    AccountLocked,
    InvalidCredentials,
    MustChangePassword,
    NotAuthorizedForDockd,
    ProviderUnavailable,
)

logger = logging.getLogger('dockd.auth')

auth_bp = Blueprint('auth', __name__)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            if (
                request.path.startswith('/api/')
                or request.path.startswith('/bins/')
                or request.is_json
            ):
                return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
            return '<script>window.location.href="/"</script>'
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Decorator that requires an authenticated user with role=admin."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = session.get('user')
        if not user:
            return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
        if user.get('role') != 'admin':
            return jsonify({'status': 'error', 'message': 'Admin required'}), 403
        return f(*args, **kwargs)
    return decorated


def override_exception_skus():
    """Settings-backed lookup, replaces the old CSV-on-disk pattern."""
    store = getattr(current_app, 'settings_store', None)
    if not store:
        return set()
    return set(store.get('override_exception_skus', []) or [])


@auth_bp.before_app_request
def _check_csrf():
    if request.method != 'POST':
        return
    if request.path == '/login':
        return
    origin = request.headers.get('Origin', '')
    # ALLOWED_ORIGINS env: comma-separated extra origins to accept (e.g.
    # the hosted ACA URL). Trailing slashes are normalized. Defaults to
    # empty so local-LAN behavior is unchanged.
    extra = [
        o.strip().rstrip('/')
        for o in (os.environ.get('ALLOWED_ORIGINS') or '').split(',')
        if o.strip()
    ]
    origin_norm = origin.rstrip('/')
    allowed = (
        origin.startswith('http://127.0.0.1')
        or origin.startswith('http://localhost')
        or origin.startswith('http://192.168.')
        or origin.startswith('http://10.')
        or origin_norm in extra
    )
    if origin and not allowed:
        logger.warning('CSRF: blocked POST from origin %s to %s', origin, request.path)
        return jsonify({'status': 'error', 'message': 'Invalid request origin'}), 403


@auth_bp.before_app_request
def _start_timer():
    request._start_time = time.time()


@auth_bp.after_app_request
def _log_request(response):
    duration = (time.time() - getattr(request, '_start_time', time.time())) * 1000
    user = session.get('user', {}).get('name', 'anonymous')
    logger.info("Request completed", extra={
        'method': request.method,
        'path': request.path,
        'status_code': response.status_code,
        'response_time_ms': round(duration, 1),
        'user': user,
    })
    return response


@auth_bp.route('/login', methods=['POST'])
@limiter.limit('5 per minute')
def login():
    """Verify credentials against Sentry and establish a Dockd session.

    Sentry owns identity: it checks the password, enforces the
    (IP, username) lockout, and reports forced-password-change. Dockd stores
    only {name, role}. The in-memory limiter above is a cheap front-line cap
    in front of Sentry's authoritative lockout.
    """
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()

    try:
        user = current_app.sentry_auth.login(
            username, password, client_ip=request.remote_addr,
        )
    except MustChangePassword:
        return jsonify({
            'status': 'error',
            'message': 'You must change your password in Sentry before using Dockd.',
        }), 403
    except NotAuthorizedForDockd:
        return jsonify({
            'status': 'error',
            'message': 'This account is not authorized to ship from the pack station.',
        }), 403
    except AccountLocked as exc:
        return jsonify({'status': 'error', 'message': exc.message}), 429
    except InvalidCredentials:
        return jsonify({'status': 'error', 'message': 'Invalid username or password'}), 401
    except ProviderUnavailable:
        return jsonify({
            'status': 'error',
            'message': 'Login is temporarily unavailable (identity provider unreachable).',
        }), 503

    # Mark the session permanent so PERMANENT_SESSION_LIFETIME (the hard 8h
    # cap, not refreshed per request) applies -- otherwise a kiosk browser
    # that never closes would hold the session, and its frozen role, forever.
    session.permanent = True
    session['user'] = {'name': user['name'], 'role': user['role']}
    return jsonify({'status': 'success', 'user': session['user']})


@auth_bp.route('/logout', methods=['POST'])
@login_required
def logout():
    session.pop('user', None)
    return jsonify({'status': 'success'})


@auth_bp.route('/shutdown', methods=['POST'])
@admin_required
def shutdown():
    logger.info("Shutdown requested by %s", session.get('user', {}).get('name'))
    os._exit(0)


@auth_bp.route('/health')
def health():
    return jsonify({'status': 'ok', 'version': current_app.config.get('VERSION', '1.0')})
