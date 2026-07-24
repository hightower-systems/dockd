"""Fake station agent for testing dockd without a scale or a Zebra.

Why this has to run on YOUR machine, not the server: dockd's browser code
calls the agent at a hardcoded http://localhost:5050. The agent is a
same-machine peripheral bridge, so when dockd is served from dell-01 over
the tailnet, the agent still has to be local to the browser.

Serves the three endpoints index.html actually calls:

    GET  /whoami   station identity + the per-station Sentry token
    GET  /scale    a weight
    POST /print    swallows ZPL and reports success

The weight is settable at runtime, which is the point. All of dockd's
carrier prompts now fire after the scale read, and several of them key off
weight -- the USPS-to-UPS conflict needs a package over max_weight_usps_lb
(15 lb) to trigger at all. A fixed weight could only ever reproduce one
branch.

    GET  /setweight?lb=22       next scale read returns 22 lb
    GET  /setweight?random=1    random 0.5-30 lb per read

Real hardware use is agent/agent.py. This is deliberately not that.
"""

import json
import random
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 5050

STATE = {
    'weight': 4.82,
    'random': False,
    'station_id': '2',
    'station_label': 'DELL-01 TEST BENCH',
    # Filled from the CLI. dockd forwards this upstream as X-Sentry-Token
    # so the container can call Sentry as the operator.
    'sentry_token': '',
}


class Handler(BaseHTTPRequestHandler):

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        # The page is served from an https tailnet origin while the agent
        # is plain http on localhost, so this is cross-origin.
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        # Chrome Private Network Access: a public/https origin reaching a
        # private (localhost) target is blocked unless the target opts in.
        # The real agent grew this same header for the same reason.
        self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send({})

    def do_GET(self):
        route = urlparse(self.path)
        q = parse_qs(route.query)

        if route.path == '/whoami':
            return self._send({
                'station_id': STATE['station_id'],
                'station_label': STATE['station_label'],
                'sentry_token': STATE['sentry_token'],
                'agent_version': 'stub-1.0',
            })

        if route.path == '/scale':
            w = (round(random.uniform(0.5, 30.0), 2) if STATE['random']
                 else STATE['weight'])
            print(f'  scale read -> {w} lb', flush=True)
            return self._send({'status': 'success', 'weight': w})

        if route.path == '/setweight':
            if 'random' in q:
                STATE['random'] = q['random'][0] not in ('0', 'false', '')
            if 'lb' in q:
                try:
                    STATE['weight'] = float(q['lb'][0])
                    STATE['random'] = False
                except ValueError:
                    return self._send({'status': 'error',
                                       'message': 'lb must be a number'}, 400)
            print(f"  weight set -> {STATE['weight']} lb "
                  f"(random={STATE['random']})", flush=True)
            return self._send({'status': 'success', 'weight': STATE['weight'],
                               'random': STATE['random']})

        return self._send({'status': 'error', 'message': 'not found'}, 404)

    def do_POST(self):
        if urlparse(self.path).path == '/print':
            n = int(self.headers.get('Content-Length', 0))
            self.rfile.read(n)
            print(f'  print -> swallowed {n} bytes of ZPL', flush=True)
            return self._send({'status': 'success', 'message': 'printed (stub)'})
        return self._send({'status': 'error', 'message': 'not found'}, 404)

    def log_message(self, *args):
        pass  # the prints above are the useful log


if __name__ == '__main__':
    STATE['sentry_token'] = sys.argv[1] if len(sys.argv) > 1 else ''
    if not STATE['sentry_token']:
        print('WARNING: no Sentry token passed. dockd will fall back to its '
              'own DOCKD_SENTRY_TOKEN env if one is set.\n')
    print(f'Stub station agent on http://127.0.0.1:{PORT}')
    print(f"  station : {STATE['station_label']} (id {STATE['station_id']})")
    print(f"  weight  : {STATE['weight']} lb")
    print()
    print('  change the weight without restarting:')
    print(f'    curl "http://127.0.0.1:{PORT}/setweight?lb=22"     # trips the '
          'USPS 15 lb cap -> USPS-to-UPS conflict')
    print(f'    curl "http://127.0.0.1:{PORT}/setweight?lb=2"      # light, '
          'stays on USPS')
    print(f'    curl "http://127.0.0.1:{PORT}/setweight?random=1"  # random '
          'each read')
    print()
    HTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
