"""Printer service: routes ZPL labels to local or remote Zebra printers.

Station definitions live in the SettingsStore (id, label, host, port,
local). A settings PUT immediately changes the station map for the
next print without rebuilding the service.

Auto-detects station from the client IP and routes to either a local
subprocess (for stations marked local=true) or a remote agent over
HTTP.
"""

import os
import socket
import subprocess
import logging
import requests

logger = logging.getLogger('dockd.printer')


class PrinterService:

    def __init__(self, settings_store, config=None):
        self._settings = settings_store
        self.local_ip = self._detect_ip()
        self.local_zebra = f"\\\\{self.local_ip}\\ZEBRA"
        logger.info("PrinterService ready - local printer: %s", self.local_zebra)

    @staticmethod
    def _detect_ip():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return '127.0.0.1'

    def _stations(self):
        return self._settings.get('stations', []) or []

    def _station_map(self):
        return {str(s['id']): s for s in self._stations()}

    def _default_station(self):
        return str(self._settings.get('default_station_id', '2'))

    def resolve_station(self, client_ip):
        """Auto-detect station from the request's source IP."""
        ip = client_ip or ''
        if ip.startswith('::ffff:'):
            ip = ip[7:]
        for s in self._stations():
            if s.get('host') == ip:
                return str(s['id'])
        return self._default_station()

    def send(self, station, zpl_bytes):
        """Route ZPL label to the correct printer."""
        station = str(station) if station else self._default_station()
        station_map = self._station_map()
        info = station_map.get(station) or station_map.get(self._default_station())
        if not info:
            raise Exception(f"No station definition for '{station}'")

        if info.get('local'):
            self._print_local(zpl_bytes)
            logger.info("Label printed locally (station %s)", station)
        else:
            self._print_remote(info['host'], int(info.get('port', 5050)), zpl_bytes)
            logger.info("Label printed via agent on station %s", station)

    def _print_local(self, zpl_bytes):
        temp = '_temp_label.zpl'
        try:
            with open(temp, 'wb') as f:
                f.write(zpl_bytes)
            subprocess.run(
                ['cmd', '/c', 'copy', '/B', temp, self.local_zebra],
                check=False, capture_output=True, timeout=10,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Local printer command timed out")
        finally:
            if os.path.exists(temp):
                os.remove(temp)

    def _print_remote(self, host, port, zpl_bytes):
        url = f"http://{host}:{port}/print"
        try:
            resp = requests.post(
                url, data=zpl_bytes,
                headers={'Content-Type': 'application/octet-stream'},
                timeout=10,
            )
            if resp.status_code != 200:
                logger.error("Agent print failed on %s: %s", host, resp.text)
                try:
                    body = resp.json()
                    detail = body.get('message', 'unknown')
                except Exception:
                    detail = 'unknown'
                raise Exception(f"Agent print error: {detail}")
        except requests.exceptions.ConnectionError:
            logger.error("Cannot reach agent at %s:%d", host, port)
            raise Exception(f"Scale agent at {host} is not reachable. Is it running?")
