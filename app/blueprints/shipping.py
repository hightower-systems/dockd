"""Shipping blueprint - thin controllers that delegate to ShippingService."""

import logging
from flask import Blueprint, request, jsonify, session, render_template, current_app
from app.blueprints.auth import login_required, override_exception_skus
from app.config import Config

logger = logging.getLogger('dockd.routes.shipping')

shipping_bp = Blueprint('shipping', __name__)


@shipping_bp.route('/')
def index():
    user = session.get('user')
    return render_template('index.html', current_user=user, config=Config,
                           override_exceptions=list(override_exception_skus()))


@shipping_bp.route('/get_order_details', methods=['GET'])
@login_required
def get_order_details():
    ticket = request.args.get('ticket', '')
    selected_ff = request.args.get('selected_ff')
    result = current_app.shipping_service.load_order(ticket, selected_ff)
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
    data = request.json
    result = current_app.shipping_service.ship_order(
        fulfillment_id=data.get('fulfillment_id'),
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
    data = request.json
    result = current_app.shipping_service.manual_link(
        ticket=data.get('ticket', ''),
        tracking=data.get('tracking', ''),
        user=session.get('user', {}).get('name', 'Dockd User'),
    )
    return jsonify(result)


@shipping_bp.route('/reprint_label', methods=['POST'])
@login_required
def reprint_label():
    data = request.json
    result = current_app.shipping_service.reprint(
        ticket=data.get('ticket', ''),
        client_ip=request.remote_addr,
    )
    return jsonify(result)


@shipping_bp.route('/void_label', methods=['POST'])
@login_required
def void_label():
    data = request.json
    result = current_app.shipping_service.void(ticket=data.get('ticket', ''))
    return jsonify(result)
