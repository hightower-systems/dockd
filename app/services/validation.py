"""Generic input validators for dockd.

Backend-agnostic helpers; not coupled to any specific ERP/WMS shape.
"""

import re


def validate_ticket(value):
    """Validate ticket / order number format.

    Returns the sanitized string, or None when the value contains
    characters outside the allowed set. Allowed: ASCII letters, digits,
    hyphen.
    """
    s = str(value).strip()
    if not re.match(r'^[A-Za-z0-9\-]+$', s):
        return None
    return s


def validate_upc(value):
    """Validate UPC format: 8-14 digits."""
    s = str(value).strip()
    if not re.match(r'^[0-9]{8,14}$', s):
        return None
    return s
