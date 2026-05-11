"""Local label cache for reprint and void operations.

Stores ZPL files on disk with a JSON index. Labels expire after a
configurable number of hours (default 8).
"""

import os
import json
import base64
import datetime
import logging

logger = logging.getLogger('dockd.label_cache')


class LabelCache:

    def __init__(self, history_file='ship_history.json', label_dir='label_history',
                 max_age_hours=8):
        self.history_file = history_file
        self.label_dir = label_dir
        self.max_age_hours = max_age_hours
        self._ensure_setup()

    def _ensure_setup(self):
        if not os.path.exists(self.label_dir):
            os.makedirs(self.label_dir)
        if not os.path.exists(self.history_file):
            with open(self.history_file, 'w') as f:
                json.dump({}, f)

    def save(self, order_num, shipment_id, tracking, zpl_b64):
        """Save a label for later reprint/void."""
        self._ensure_setup()

        zpl_path = os.path.join(self.label_dir, f"{order_num}.zpl")
        try:
            zpl_bytes = base64.b64decode(zpl_b64)
            with open(zpl_path, 'wb') as f:
                f.write(zpl_bytes)
        except Exception as e:
            logger.warning("Failed to save ZPL file for %s: %s", order_num, e)
            zpl_path = None

        try:
            with open(self.history_file, 'r') as f:
                history = json.load(f)
        except Exception:
            history = {}

        record = {
            'shipment_id': shipment_id,
            'tracking': tracking,
            'date': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'zpl_path': zpl_path,
        }
        history[order_num] = record
        history[tracking] = {'link_to_order': order_num}
        if not order_num.startswith('SO'):
            history[f'SO{order_num}'] = {'link_to_order': order_num}

        with open(self.history_file, 'w') as f:
            json.dump(history, f)

        self.cleanup_expired()

    def lookup(self, scan_input):
        """Find an order by order number, tracking number, or SO-prefixed variant."""
        try:
            with open(self.history_file, 'r') as f:
                history = json.load(f)
        except Exception:
            return None

        scan = str(scan_input).strip()

        record = history.get(scan) or history.get(f'SO{scan}')
        if not record and scan.startswith('SO'):
            record = history.get(scan[2:])

        if not record:
            return None

        if 'link_to_order' in record:
            record = history.get(record['link_to_order'])

        return record

    def get_zpl_b64(self, scan_input):
        """Get base64-encoded ZPL for a previously generated label."""
        record = self.lookup(scan_input)
        if not record:
            return None
        zpl_path = record.get('zpl_path')
        if not zpl_path or not os.path.exists(zpl_path):
            return None
        with open(zpl_path, 'rb') as f:
            return base64.b64encode(f.read()).decode('ascii')

    def get_shipment_id(self, scan_input):
        """Get ShipRush shipment ID for void operations."""
        record = self.lookup(scan_input)
        if record:
            return record.get('shipment_id')
        return None

    def cleanup_expired(self):
        """Remove labels older than max_age_hours."""
        try:
            with open(self.history_file, 'r') as f:
                history = json.load(f)
        except Exception:
            return

        now = datetime.datetime.now()
        expired_orders = []

        for key, record in history.items():
            if 'date' not in record:
                continue
            try:
                record_date = datetime.datetime.strptime(record['date'], '%Y-%m-%d %H:%M:%S')
                age_hours = (now - record_date).total_seconds() / 3600
                if age_hours > self.max_age_hours:
                    zpl_path = record.get('zpl_path')
                    if zpl_path and os.path.exists(zpl_path):
                        try:
                            os.remove(zpl_path)
                        except Exception:
                            pass
                    expired_orders.append(key)
            except Exception:
                continue

        if not expired_orders:
            return

        # Also remove link entries that point to expired orders
        link_keys = []
        for key, record in history.items():
            if record.get('link_to_order') in expired_orders:
                link_keys.append(key)

        for key in expired_orders + link_keys:
            history.pop(key, None)

        with open(self.history_file, 'w') as f:
            json.dump(history, f)
