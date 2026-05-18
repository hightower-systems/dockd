"""Dockd scale agent (v2).

Runs on each pack-station laptop alongside the operator's Chrome
window. Owns the per-station hardware bridge: USB HID scale, Zebra
ZPL label printer, HP LaserJet packing-slip printer. Also serves
the per-station identity + Sentry bearer token to the browser via
`/whoami`.

Network shape:

    Browser at https://dockd.<your-domain>
       |
       |  fetch('http://localhost:5050/whoami')   <- first call on page load
       |  fetch('http://localhost:5050/scale')    <- every weight read
       |  fetch('http://localhost:5050/print')    <- POST ZPL bytes
       |  fetch('http://localhost:5050/print-html') <- POST HTML bytes
       v
    127.0.0.1:5050 (this process)
       |
       +-- USB HID scale  (hid library)
       +-- Zebra ZPL printer (Windows UNC share)
       +-- HP LaserJet printer (Windows ShellExecuteW 'printto')

The agent binds to `127.0.0.1` only -- nothing off the laptop can
reach it. CORS is pinned to `dockd_origin` from the config so a
third-party site that an operator stumbles onto cannot script the
local hardware.

Tested target: Python 3.11 on Windows 10/11.
"""

import ctypes
import json
import logging
import os
import socket
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler

import hid
from flask import Flask, jsonify, request
from flask_cors import CORS

AGENT_VERSION = '2.1'

# -- LOAD CONFIG -----------------------------------------------------------

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'agent_config.json',
)


def load_config():
    if not os.path.exists(CONFIG_PATH):
        print(f"ERROR: {CONFIG_PATH} not found.")
        print("Copy agent_config.example.json to agent_config.json and fill in values.")
        sys.exit(1)
    with open(CONFIG_PATH, 'r') as f:
        return json.load(f)


CFG = load_config()

AGENT_PORT       = int(CFG.get('agent_port', 5050))
STATION_ID       = (CFG.get('station_id') or '').strip()
STATION_LABEL    = (CFG.get('station_label') or '').strip()
DOCKD_ORIGIN     = (CFG.get('dockd_origin') or '').strip()
SENTRY_TOKEN     = (CFG.get('sentry_token') or '').strip()
HP_PRINTER       = CFG.get('hp_printer', 'HPLASER')
SCALE_VENDOR_ID  = int(CFG.get('scale_vendor_id', '0x0b67'), 16)
SCALE_PRODUCT_ID = int(CFG.get('scale_product_id', '0x555e'), 16)

if not DOCKD_ORIGIN:
    print("ERROR: agent_config.json is missing 'dockd_origin'.")
    print("Set it to the HTTPS URL of your dockd deployment (no trailing slash).")
    sys.exit(1)
if not STATION_ID:
    print("ERROR: agent_config.json is missing 'station_id'.")
    sys.exit(1)

# -- AUTO-DETECT ZEBRA PRINTER PATH ----------------------------------------
# When 'zebra_printer' is unset in config, the agent constructs a UNC
# path against this laptop's own IP (matches the legacy convention of
# a host-local ZEBRA share). Operators with a network-shared Zebra
# put the explicit \\server\queue value in config.
try:
    _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _s.connect(("8.8.8.8", 80))
    LOCAL_IP = _s.getsockname()[0]
    _s.close()
except Exception:
    LOCAL_IP = "127.0.0.1"

ZEBRA_PRINTER = CFG.get('zebra_printer', '') or f"\\\\{LOCAL_IP}\\ZEBRA"

# -- LOGGING ---------------------------------------------------------------
log_dir = CFG.get('log_dir', 'logs')
os.makedirs(log_dir, exist_ok=True)

_file_handler = RotatingFileHandler(
    os.path.join(log_dir, 'agent.log'),
    maxBytes=int(CFG.get('log_max_bytes', 5242880)),
    backupCount=int(CFG.get('log_backup_count', 3)),
)
_file_handler.setFormatter(logging.Formatter(
    '%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S',
))

logger = logging.getLogger('agent')
logger.addHandler(_file_handler)
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)

# -- FLASK APP + CORS ------------------------------------------------------
# CORS pinned to the dockd origin: only that origin can fetch from
# this process. localhost-bind plus this pin replaces the legacy
# X-Agent-Key header.
app = Flask(__name__)
CORS(app, origins=[DOCKD_ORIGIN])


# -- CHROME PRIVATE NETWORK ACCESS (CR-117+) --------------------------------
# When an HTTPS public-origin page (e.g. dockd on Azure Container Apps)
# tries to fetch http://127.0.0.1:5050, Chrome 117+ issues a CORS
# preflight that includes:
#     Access-Control-Request-Private-Network: true
# and refuses the call unless the server's preflight response carries:
#     Access-Control-Allow-Private-Network: true
#
# History: a prior @app.after_request hook tried to set this header,
# but flask-cors runs its own after_request that re-emits the header
# with value 'false', clobbering our value. Result: pack stations
# still saw the "SCALE AGENT NOT RUNNING" banner even with the agent
# running. WSGI middleware sits OUTSIDE the Flask response cycle, so
# flask-cors can't override us — we get the final word on the headers
# the browser actually sees.
class _PNAMiddleware:
    """Force Access-Control-Allow-Private-Network: true for the dockd
    origin. Must be WSGI middleware (not Flask after_request) because
    flask-cors's own hook would otherwise overwrite to 'false'.
    """

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        origin = environ.get('HTTP_ORIGIN', '')

        def _start(status, headers, exc_info=None):
            if origin == DOCKD_ORIGIN:
                headers = [
                    (k, v) for k, v in headers
                    if k.lower() != 'access-control-allow-private-network'
                ]
                headers.append(('Access-Control-Allow-Private-Network', 'true'))
            return start_response(status, headers, exc_info)

        return self.wsgi_app(environ, _start)


app.wsgi_app = _PNAMiddleware(app.wsgi_app)


# -- /whoami ---------------------------------------------------------------
@app.route('/whoami', methods=['GET'])
def whoami():
    """Tell the browser its station identity + Sentry bearer token.

    The browser caches the response in a module-scope variable for
    the rest of the session and forwards `X-Sentry-Token` on every
    call to dockd's API surface.
    """
    return jsonify({
        'agent_version': AGENT_VERSION,
        'station_id': STATION_ID,
        'station_label': STATION_LABEL,
        'sentry_token': SENTRY_TOKEN,
    })


# -- /scale ----------------------------------------------------------------
@app.route('/scale', methods=['GET'])
def read_scale():
    """Read weight from the USB HID scale. Returns JSON with weight in lbs."""
    h = None
    max_retries = 3
    for attempt in range(max_retries):
        try:
            h = hid.device()
            h.open(SCALE_VENDOR_ID, SCALE_PRODUCT_ID)
            h.set_nonblocking(1)
            data = []
            for _ in range(10):
                d = h.read(8)
                if d:
                    data = d
                    break
                time.sleep(0.01)
            h.close()

            if data:
                raw_val = data[4] + (data[5] * 256)
                if raw_val > 32767:
                    raw_val -= 65536
                exponent = data[3]
                if exponent > 127:
                    exponent -= 256
                weight = raw_val * (10 ** exponent)
                # Unit code 11 = ounces, convert to lbs
                if data[2] == 11:
                    weight = weight / 16.0
                weight = max(weight, 0.0625)
                logger.info(
                    "Scale read: %.4f lbs (stable=%s)", weight, data[1] == 4,
                )
                return jsonify({
                    'status': 'success',
                    'weight': round(weight, 4),
                    'unit': 'lbs',
                    'stable': (data[1] == 4),
                })

            return jsonify({
                'status': 'error',
                'message': 'Scale did not respond. Check power and USB.',
            }), 500

        except Exception as e:
            if h:
                try:
                    h.close()
                except Exception:
                    pass
            if attempt == max_retries - 1:
                logger.error("Scale error after %d retries: %s", max_retries, e)
                return jsonify({
                    'status': 'error',
                    'message': f'Scale error: {str(e)}',
                }), 500
            time.sleep(0.5)

    return jsonify({
        'status': 'error',
        'message': 'Scale unreachable after retries.',
    }), 500


# -- /print ----------------------------------------------------------------
@app.route('/print', methods=['POST'])
def print_label():
    """Receive raw ZPL bytes from the browser and send to the Zebra."""
    zpl_data = request.data
    if not zpl_data:
        return jsonify({'status': 'error', 'message': 'No ZPL data received'}), 400

    temp_file = "_temp_label.zpl"
    try:
        with open(temp_file, "wb") as f:
            f.write(zpl_data)
        proc = subprocess.run(
            ['cmd', '/c', 'copy', '/B', temp_file, ZEBRA_PRINTER],
            check=False, capture_output=True, timeout=10,
        )
        if proc.returncode == 0:
            logger.info("Label sent to Zebra (%d bytes)", len(zpl_data))
            return jsonify({'status': 'success', 'message': 'Label sent to printer'})
        stderr = proc.stderr.decode(errors='ignore').strip()
        logger.error("Zebra print failed (code %d): %s", proc.returncode, stderr)
        return jsonify({
            'status': 'error',
            'message': f'Print command failed (code {proc.returncode})',
        }), 500
    except subprocess.TimeoutExpired:
        logger.error("Zebra print timed out")
        return jsonify({
            'status': 'error',
            'message': 'Print timed out -- check Zebra printer',
        }), 500
    except Exception as e:
        logger.error("Zebra print error: %s", e)
        return jsonify({
            'status': 'error',
            'message': f'Printer error: {str(e)}',
        }), 500
    finally:
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass


# -- /print-html -----------------------------------------------------------
@app.route('/print-html', methods=['POST'])
def print_html():
    """Receive HTML from the browser and print to the HP LaserJet.

    Uses Windows ShellExecuteW with the 'printto' verb so the
    rendering happens locally; falls back to the default printer
    via os.startfile('print') if the direct call fails.
    """
    html_data = request.data
    if not html_data:
        return jsonify({'status': 'error', 'message': 'No HTML data received'}), 400

    temp_file = f"_temp_report_{int(time.time())}.html"
    try:
        with open(temp_file, 'wb') as f:
            f.write(html_data)

        abs_path = os.path.abspath(temp_file)

        # ShellExecuteW with 'printto' verb -> specific printer
        result = ctypes.windll.shell32.ShellExecuteW(
            0, 'printto', abs_path, HP_PRINTER, None, 0,
        )
        if result > 32:
            logger.info(
                "HTML sent to HP printer '%s' (%d bytes)", HP_PRINTER, len(html_data),
            )
            # Delay cleanup so the print spooler can read the file
            time.sleep(3)
            return jsonify({
                'status': 'success',
                'message': f'Report sent to {HP_PRINTER}',
            })

        # Fallback: print to default printer
        os.startfile(abs_path, 'print')
        logger.warning(
            "HP direct print failed (code %d), used default printer", result,
        )
        time.sleep(3)
        return jsonify({
            'status': 'success',
            'message': 'Report sent to default printer (HP fallback)',
        })
    except Exception as e:
        logger.error("HP print error: %s", e)
        return jsonify({
            'status': 'error',
            'message': f'HP printer error: {str(e)}',
        }), 500
    finally:
        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        except Exception:
            pass


# -- /health ---------------------------------------------------------------
@app.route('/health', methods=['GET'])
def health():
    """Quick liveness check. Reports scale connection + agent version."""
    scale_ok = False
    try:
        h = hid.device()
        h.open(SCALE_VENDOR_ID, SCALE_PRODUCT_ID)
        h.close()
        scale_ok = True
    except Exception:
        pass

    return jsonify({
        'status': 'ok',
        'agent_version': AGENT_VERSION,
        'station_id': STATION_ID,
        'station_label': STATION_LABEL,
        'zebra_printer': ZEBRA_PRINTER,
        'hp_printer': HP_PRINTER,
        'scale_connected': scale_ok,
        'local_ip': LOCAL_IP,
    })


# -- LAUNCH ----------------------------------------------------------------
if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("  Dockd Scale Agent v%s -- Port %d", AGENT_VERSION, AGENT_PORT)
    logger.info("  Station: %s (%s)", STATION_ID, STATION_LABEL or '(no label)')
    logger.info("  Dockd origin: %s", DOCKD_ORIGIN)
    logger.info("  Zebra: %s", ZEBRA_PRINTER)
    logger.info("  HP:    %s", HP_PRINTER)
    logger.info(
        "  Scale: vendor=0x%04x product=0x%04x",
        SCALE_VENDOR_ID, SCALE_PRODUCT_ID,
    )
    logger.info("=" * 50)
    # Bind to 127.0.0.1 only. Nothing off this laptop can reach the
    # agent; CORS pinning above narrows which web origin can script it.
    app.run(host='127.0.0.1', port=AGENT_PORT, debug=False, threaded=True)
