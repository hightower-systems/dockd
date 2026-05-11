"""Authentication blueprint.

Handles login/logout, forced-password-change gating, CSRF protection,
request logging, and provides the login_required + admin_required
decorators.

Users live in the UsersStore (users.json); roles gate the admin-only
settings surface. A `must_change_password` flag on the session forces
any user (including the bootstrap admin) through a password rotation
before any other endpoint will respond.
"""

import os
import time
import logging
from functools import wraps
from flask import Blueprint, request, jsonify, session, current_app
from app.extensions import limiter
from app.services.users_store import UsersStoreError

logger = logging.getLogger('dockd.auth')

auth_bp = Blueprint('auth', __name__)


# Paths that remain reachable while a user's session is flagged for
# password change. Everything else returns 403 until they rotate.
_PASSWORD_CHANGE_ALLOWED_PATHS = {
    '/',
    '/login',
    '/logout',
    '/api/change-password',
    '/health',
}


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
    allowed = (
        origin.startswith('http://127.0.0.1')
        or origin.startswith('http://localhost')
        or origin.startswith('http://192.168.')
        or origin.startswith('http://10.')
    )
    if origin and not allowed:
        logger.warning('CSRF: blocked POST from origin %s to %s', origin, request.path)
        return jsonify({'status': 'error', 'message': 'Invalid request origin'}), 403


@auth_bp.before_app_request
def _enforce_password_change():
    """Gate every endpoint behind a pending password rotation."""
    user = session.get('user')
    if not user or not user.get('must_change_password'):
        return
    if request.path in _PASSWORD_CHANGE_ALLOWED_PATHS:
        return
    if request.path.startswith('/static/'):
        return
    return jsonify({
        'status': 'error',
        'message': 'Password change required before continuing',
        'must_change_password': True,
    }), 403


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
    data = request.json
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()
    result = current_app.users_store.verify(username, password)
    if not result:
        return jsonify({'status': 'error', 'message': 'Invalid username or password'})
    session['user'] = {
        'name': result['username'],
        'role': result['role'],
        'must_change_password': result.get('must_change_password', False),
    }
    return jsonify({'status': 'success', 'user': session['user']})


@auth_bp.route('/logout', methods=['POST'])
@login_required
def logout():
    session.pop('user', None)
    return jsonify({'status': 'success'})


@auth_bp.route('/api/change-password', methods=['POST'])
@login_required
def change_password():
    """Self-service password change. Clears must_change_password."""
    body = request.get_json(silent=True) or {}
    current_password = body.get('current_password') or ''
    new_password = body.get('new_password') or ''
    username = session.get('user', {}).get('name')
    if not username:
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        current_app.users_store.change_own_password(
            username, current_password, new_password,
        )
    except UsersStoreError as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 400

    user = session['user']
    user['must_change_password'] = False
    session['user'] = user
    return jsonify({'status': 'ok'})


@auth_bp.route('/shutdown', methods=['POST'])
@admin_required
def shutdown():
    logger.info("Shutdown requested by %s", session.get('user', {}).get('name'))
    os._exit(0)


@auth_bp.route('/health')
def health():
    return jsonify({'status': 'ok', 'version': current_app.config.get('VERSION', '1.0')})
