"""Shipping blueprint - thin controllers that delegate to ShippingService."""

import logging
from flask import Blueprint, request, jsonify, session, render_template, current_app, g
from app.blueprints.auth import login_required, override_exception_skus
from app.config import Config

logger = logging.getLogger('dockd.routes.shipping')

shipping_bp = Blueprint('shipping', __name__)


@shipping_bp.before_app_request
def _capture_sentry_token():
    """Stash the per-request Sentry token on `g`.

    The browser at a pack station forwards its station-scoped token as
    `X-Sentry-Token`; the order-backend reads it from `g` via the
    `_resolve_sentry_token` helper in the app factory. In v0.2.0 the
    browser does not yet send this header (scale-agent v2 / /whoami
    bootstrap lands in v0.4.0); the factory falls back to the
    DOCKD_SENTRY_TOKEN env var when the header is absent.
    """
    g.sentry_token = request.headers.get('X-Sentry-Token', '') or ''


@shipping_bp.route('/')
def index():
    user = session.get('user')
    return render_template('index.html', current_user=user, config=Config,
                           override_exceptions=list(override_exception_skus()))


@shipping_bp.route('/get_order_details', methods=['GET'])
@login_required
def get_order_details():
    # Accept either `so_number` (canonical, v0.2.0+) or the legacy
    # `ticket` query param so a stale browser session still works.
    so_number = request.args.get('so_number') or request.args.get('ticket', '')
    result = current_app.shipping_service.load_order(so_number)
    status_code = 400 if result.get('status') == 'error' and 'Invalid' in result.get('message', '') else 200
    return jsonify(result), status_code


@shipping_bp.route('/get_scale_weight', methods=['GET'])
@login_required
def get_scale_weight():
    result = current_app.scale_reader.read_weight()
    return jsonify(result)


@shipping_bp.route('/ship_order', methods=['POST'])
@login_required
def ship_order():
    data = request.json or {}
    # Accept `so_number` (canonical) or the legacy `fulfillment_id` /
    # `order_number` (frontend back-compat).
    so_number = (
        data.get('so_number')
        or data.get('fulfillment_id')
        or data.get('order_number')
        or ''
    )
    result = current_app.shipping_service.ship_order(
        so_number=so_number,
        box_id=data.get('box_id', ''),
        weight=data.get('weight', 0),
        order_number=data.get('order_number'),
        carrier_override=data.get('carrier_override'),
        ca_shipping_paid=data.get('ca_shipping_paid', 0),
        ob_dims=data.get('ob_dims'),
        client_ip=request.remote_addr,
        user=session.get('user', {}).get('name', 'Unknown'),
        order_loaded_at=data.get('order_loaded_at', ''),
        ff_created_at=data.get('ff_created_at', ''),
        station_id=data.get('station_id'),
        station_label=data.get('station_label'),
    )
    return jsonify(result)


@shipping_bp.route('/ship_count')
@login_required
def get_ship_count():
    user = session.get('user', {}).get('name', '')
    count = current_app.shipping_service.get_ship_count(user)
    return jsonify({'count': count})


@shipping_bp.route('/log_override', methods=['POST'])
@login_required
def log_override():
    data = request.json or {}
    station = current_app.shipping_service.printer.resolve_station(request.remote_addr)
    current_app.shipping_service.log_override(
        order_number=data.get('order_number', ''),
        user=data.get('user', ''),
        items=data.get('items', []),
        station=station,
        override_type=data.get('override_type', 'single'),
    )
    return jsonify({'status': 'ok'})


@shipping_bp.route('/manual_link_tracking', methods=['POST'])
@login_required
def manual_link_tracking():
    data = request.json or {}
    so_number = data.get('so_number') or data.get('ticket', '')
    result = current_app.shipping_service.manual_link(
        ticket=so_number,
        tracking=data.get('tracking', ''),
        user=session.get('user', {}).get('name', 'Dockd User'),
        carrier=data.get('carrier'),
        ship_method=data.get('ship_method'),
    )
    return jsonify(result)


@shipping_bp.route('/reprint_label', methods=['POST'])
@login_required
def reprint_label():
    data = request.json or {}
    result = current_app.shipping_service.reprint(
        ticket=data.get('so_number') or data.get('ticket', ''),
        client_ip=request.remote_addr,
    )
    return jsonify(result)


@shipping_bp.route('/void_label', methods=['POST'])
@login_required
def void_label():
    data = request.json or {}
    result = current_app.shipping_service.void(
        ticket=data.get('so_number') or data.get('ticket', ''),
        reason=data.get('reason'),
        operator_username=session.get('user', {}).get('name'),
    )
    return jsonify(result)


@shipping_bp.route('/api/health/backend', methods=['GET'])
@login_required
def backend_health():
    """Operator-UI connectivity dot polls this every 30s.

    Read-through cache on the server side so 5 stations polling in
    parallel do not turn into 5 simultaneous probes of Sentry.
    """
    return jsonify(current_app.backend_health.status())
