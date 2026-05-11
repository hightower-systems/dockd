"""Settings blueprint.

Admin-gated CRUD on operational settings (settings.json) and user
records (users.json), plus a small writable surface for the secrets
that live in .env.

Public endpoints (logged-in, any role):
    GET  /api/settings/public   -- subset the operator UI needs

Admin endpoints:
    GET    /api/settings                -- full settings dict
    PUT    /api/settings                -- replace settings wholesale
    PATCH  /api/settings                -- merge partial update
    POST   /api/settings/secrets        -- write .env keys + reload env
    GET    /api/users                   -- list (username, role)
    POST   /api/users                   -- add user
    DELETE /api/users/<username>        -- remove user
    PUT    /api/users/<username>/password
    PUT    /api/users/<username>/role
    GET    /settings                    -- HTML settings page
"""

import os
import logging

from flask import Blueprint, current_app, jsonify, render_template, request, session
from dotenv import set_key, find_dotenv

from app.blueprints.auth import admin_required, login_required
from app.services.users_store import UsersStoreError

logger = logging.getLogger('dockd.settings')

settings_bp = Blueprint('settings', __name__)


# Secrets that the settings UI is allowed to write to .env. Anything
# else is rejected so a misconfigured client cannot overwrite arbitrary
# env vars on disk.
_WRITABLE_SECRETS = {
    'SHIPRUSH_TOKEN',
    'SHIPRUSH_ENDPOINT',
    'SECRET_KEY',
    'SENTRY_BASE_URL',
    'BACKEND',
}


def _env_path():
    """Locate the .env file dockd loads from. Falls back to project
    root if find_dotenv comes up empty (fresh checkout, no .env yet)."""
    discovered = find_dotenv(usecwd=True)
    if discovered:
        return discovered
    return os.path.join(os.getcwd(), '.env')


# -------------------- public (logged-in) --------------------------------


@settings_bp.route('/api/settings/public', methods=['GET'])
@login_required
def public_settings():
    return jsonify(current_app.settings_store.public_subset())


# -------------------- HTML page (admin) ---------------------------------


@settings_bp.route('/settings')
@admin_required
def settings_page():
    user = session.get('user') or {}
    if user.get('must_change_password'):
        # Bounce back to root; the index page renders the forced
        # password-change overlay before anything else is reachable.
        return '<script>window.location.href="/"</script>'
    return render_template('settings.html', current_user=user)


# -------------------- admin: operational settings ------------------------


@settings_bp.route('/api/settings', methods=['GET'])
@admin_required
def get_settings():
    return jsonify(current_app.settings_store.all())


@settings_bp.route('/api/settings', methods=['PUT'])
@admin_required
def put_settings():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({'status': 'error', 'message': 'body must be a JSON object'}), 400
    updated = current_app.settings_store.replace(body)
    return jsonify({'status': 'ok', 'settings': updated})


@settings_bp.route('/api/settings', methods=['PATCH'])
@admin_required
def patch_settings():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({'status': 'error', 'message': 'body must be a JSON object'}), 400
    updated = current_app.settings_store.patch(body)
    return jsonify({'status': 'ok', 'settings': updated})


# -------------------- admin: secrets (.env) ------------------------------


@settings_bp.route('/api/settings/secrets', methods=['POST'])
@admin_required
def write_secrets():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({'status': 'error', 'message': 'body must be a JSON object'}), 400

    invalid = [k for k in body.keys() if k not in _WRITABLE_SECRETS]
    if invalid:
        return jsonify({
            'status': 'error',
            'message': f'keys not in writable allow-list: {invalid}',
        }), 400

    env_path = _env_path()
    if not os.path.exists(env_path):
        # Create an empty .env so set_key has something to append to.
        open(env_path, 'a', encoding='utf-8').close()
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass

    written = []
    for key, value in body.items():
        if value is None or value == '':
            continue
        set_key(env_path, key, str(value), quote_mode='never')
        os.environ[key] = str(value)
        written.append(key)

    return jsonify({'status': 'ok', 'updated': written, 'env_path': env_path})


@settings_bp.route('/api/settings/secrets/presence', methods=['GET'])
@admin_required
def secrets_presence():
    """Report which secret keys are populated. Never returns values."""
    return jsonify({
        k: bool((os.environ.get(k) or '').strip())
        for k in sorted(_WRITABLE_SECRETS)
    })


# -------------------- admin: users ---------------------------------------


@settings_bp.route('/api/users', methods=['GET'])
@admin_required
def list_users():
    return jsonify(current_app.users_store.list_users())


@settings_bp.route('/api/users', methods=['POST'])
@admin_required
def add_user():
    body = request.get_json(silent=True) or {}
    try:
        result = current_app.users_store.add_user(
            username=body.get('username', ''),
            password=body.get('password', ''),
            role=body.get('role', 'user'),
        )
    except UsersStoreError as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 400
    return jsonify({'status': 'ok', 'user': result})


@settings_bp.route('/api/users/<username>', methods=['DELETE'])
@admin_required
def delete_user(username):
    if username == session.get('user', {}).get('name'):
        return jsonify({'status': 'error', 'message': 'cannot delete yourself'}), 400
    try:
        current_app.users_store.remove_user(username)
    except UsersStoreError as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 400
    return jsonify({'status': 'ok'})


@settings_bp.route('/api/users/<username>/password', methods=['PUT'])
@admin_required
def reset_password(username):
    body = request.get_json(silent=True) or {}
    try:
        current_app.users_store.set_password(username, body.get('password', ''))
    except UsersStoreError as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 400
    return jsonify({'status': 'ok'})


@settings_bp.route('/api/users/<username>/role', methods=['PUT'])
@admin_required
def set_role(username):
    body = request.get_json(silent=True) or {}
    try:
        current_app.users_store.set_role(username, body.get('role', 'user'))
    except UsersStoreError as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 400
    return jsonify({'status': 'ok'})
