"""Labels blueprint -- bin sticker and item barcode printing.

Server-side responsibilities are intentionally thin: validate input,
hit Sentry for item lookup when needed, build ZPL, and return it
base64-encoded. The browser hands the ZPL to the local scale agent
(`agentPrintZpl` in index.html), which is the same path the shipping
label flow uses. Server-side PrinterService is bypassed for these
labels because the operator's browser already owns a token-authenticated
channel to its station's printer.

Routes:

    GET  /bins                    -- legacy landing redirect to /
    POST /bins/print              -- {scan_data} -> bin sticker ZPL
    GET  /labels/catalog          -- thread catalog JSON
    POST /labels/print            -- {upc|sku, quantity} -> 1.5x1 label ZPL
"""

import base64
import logging
import re

from flask import Blueprint, current_app, jsonify, request

from app.blueprints.auth import login_required
from app.services.backend import (
    BackendError,
    ItemData,
    NetworkError,
    NotFoundError,
)
from app.services.labels import (
    bin_sticker_zpl,
    item_barcode_zpl_bulk,
)

logger = logging.getLogger('dockd.routes.labels')

labels_bp = Blueprint('labels', __name__)


_BARCODE_RE = re.compile(r'^[A-Za-z0-9\-_./]{2,64}$')


def _clean_scan(value: str) -> str:
    return (value or '').strip()


def _b64(zpl: str) -> str:
    return base64.b64encode(zpl.encode('utf-8')).decode('ascii')


def _backend_required():
    backend = current_app.shipping_service.backend
    if backend is None:
        return None, (
            jsonify({
                'status': 'error',
                'message': 'Sentry backend is not configured. Set BACKEND=sentry.',
            }),
            503,
        )
    return backend, None


@labels_bp.route('/bins/print', methods=['POST'])
@login_required
def bins_print():
    """Look up an item by scanned UPC/SKU and return a 4x2 bin sticker."""
    data = request.json or {}
    scan = _clean_scan(data.get('scan_data') or data.get('scan') or '')
    if not scan or not _BARCODE_RE.match(scan):
        return jsonify({
            'status': 'error',
            'message': 'Invalid or empty scan.',
        }), 400

    backend, err = _backend_required()
    if err:
        return err

    try:
        item: ItemData = backend.lookup_item(scan)
    except NotFoundError:
        return jsonify({
            'status': 'error',
            'message': f"Item not found for '{scan}'.",
        }), 404
    except NetworkError as exc:
        logger.warning("Bin lookup network error: %s", exc.message)
        return jsonify({
            'status': 'error',
            'message': 'Sentry is unreachable. Try again in a moment.',
        }), 502
    except BackendError as exc:
        logger.error("Bin lookup backend error: %s", exc.message)
        return jsonify({
            'status': 'error',
            'message': 'Sentry returned an unexpected error.',
        }), 502

    sku = item.sku or scan
    upc = item.upc or ''
    zpl = bin_sticker_zpl(sku, upc)

    logger.info("Bin sticker: %s (UPC=%s, qty=%d)", sku, upc or '-', item.quantity_on_hand)
    return jsonify({
        'status': 'success',
        'sku': sku,
        'upc': upc,
        'item_name': item.item_name,
        'quantity': item.quantity_on_hand,
        'zpl_b64': _b64(zpl),
    })


@labels_bp.route('/labels/catalog', methods=['GET'])
@login_required
def labels_catalog():
    catalog = current_app.thread_catalog
    if catalog.is_empty():
        return jsonify({
            'status': 'error',
            'message': "Thread catalog not loaded. Add app/data/thread_catalog.csv.",
        }), 404
    return jsonify(catalog.as_response())


@labels_bp.route('/labels/print', methods=['POST'])
@login_required
def labels_print():
    """Build a 1.5x1 item barcode ZPL.

    Two ways to call it:
      - {upc, quantity}                 -- thread-catalog flow; UPC known
      - {sku, quantity} or {scan, qty}  -- item-lookup flow; resolves to UPC via Sentry
    """
    data = request.json or {}
    upc = _clean_scan(data.get('upc') or '')
    sku = _clean_scan(data.get('sku') or data.get('scan') or '')
    try:
        quantity = int(data.get('quantity', 1))
    except (TypeError, ValueError):
        quantity = 1
    quantity = max(1, min(100, quantity))

    label_sku = sku

    if not upc:
        if not sku or not _BARCODE_RE.match(sku):
            return jsonify({
                'status': 'error',
                'message': 'Provide a UPC, or a SKU to look up.',
            }), 400

        backend, err = _backend_required()
        if err:
            return err

        try:
            item: ItemData = backend.lookup_item(sku)
        except NotFoundError:
            return jsonify({
                'status': 'error',
                'message': f"Item not found for '{sku}'.",
            }), 404
        except NetworkError as exc:
            logger.warning("Label lookup network error: %s", exc.message)
            return jsonify({
                'status': 'error',
                'message': 'Sentry is unreachable. Try again in a moment.',
            }), 502
        except BackendError as exc:
            logger.error("Label lookup backend error: %s", exc.message)
            return jsonify({
                'status': 'error',
                'message': 'Sentry returned an unexpected error.',
            }), 502

        upc = item.upc or ''
        label_sku = item.sku or sku
        if not upc:
            return jsonify({
                'status': 'error',
                'message': f"Item '{label_sku}' has no UPC on file.",
            }), 422

    if not _BARCODE_RE.match(upc):
        return jsonify({
            'status': 'error',
            'message': 'Invalid UPC format.',
        }), 400

    zpl = item_barcode_zpl_bulk(upc, quantity)
    logger.info("Item label: %dx %s (UPC=%s)", quantity, label_sku or '-', upc)
    return jsonify({
        'status': 'success',
        'sku': label_sku,
        'upc': upc,
        'quantity': quantity,
        'zpl_b64': _b64(zpl),
    })
