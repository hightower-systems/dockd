"""USB HID scale reader.

Wraps hidapi library for reading weight from the warehouse USB scale.
Gracefully handles missing hidapi (not all environments have USB access).
"""

import time
import logging

logger = logging.getLogger('dockd.scale')

try:
    import hid
    HID_AVAILABLE = True
except ImportError:
    hid = None
    HID_AVAILABLE = False


class ScaleReader:

    def __init__(self, vendor_id, product_id):
        self.vendor_id = vendor_id
        self.product_id = product_id

    @property
    def available(self):
        return HID_AVAILABLE

    def read_weight(self):
        """Read weight from USB scale.

        Returns dict with status, weight, unit, and stable flag.
        """
        if not HID_AVAILABLE:
            return {
                'status': 'error',
                'message': 'USB scale library (hidapi) not installed on this system.',
            }

        h = None
        max_retries = 3

        for attempt in range(max_retries):
            try:
                h = hid.device()
                h.open(self.vendor_id, self.product_id)
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
                    if data[2] == 11:
                        weight = weight / 16.0
                    weight = max(weight, 0.0625)
                    return {
                        'status': 'success',
                        'weight': round(weight, 4),
                        'unit': 'lbs',
                        'stable': (data[1] == 4),
                    }

                return {
                    'status': 'error',
                    'message': 'Scale did not respond. Check that the scale is on and connected.',
                }

            except Exception as e:
                if h:
                    try:
                        h.close()
                    except Exception:
                        pass
                if attempt == max_retries - 1:
                    return {
                        'status': 'error',
                        'message': f'Scale unreachable: {e}',
                    }
                time.sleep(0.5)

        return {
            'status': 'error',
            'message': 'Scale unreachable. Check USB connection and that the scale is on.',
        }
