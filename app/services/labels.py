"""Label builders: ZPL templates for the bin sticker and item barcode
features, plus the fly-tying thread catalog loader.

The label sizes are fixed (4x2 in for bin stickers, 1.5x1 in for item
barcodes) because the warehouse runs one Zebra TLP-style printer per
station; per-station label-stock variations are not modeled. ZPL is
emitted as text and base64-encoded at the route boundary so the
browser-side `agentPrintZpl` helper can hand it to the local scale
agent without further transformation.
"""

import csv
import logging
import os
import re
import threading
from typing import Dict, List, Optional

logger = logging.getLogger('dockd.labels')


# ---- ZPL builders ----------------------------------------------------------


def bin_sticker_zpl(sku: str, upc: Optional[str]) -> str:
    """4x2 inch (812x406 dots @ 203 DPI) bin sticker.

    SKU is centered at the top in large text; UPC renders below as a
    Code 128 barcode with human-readable digits. When no UPC is on the
    item the layout falls back to SKU-only with larger glyphs so the
    label doesn't look half-empty.
    """
    safe_sku = (sku or '').replace('"', '\\"').replace('^', ' ')
    upc_clean = (upc or '').strip()
    if upc_clean and upc_clean.lower() not in ('none', 'nan'):
        safe_upc = upc_clean.replace('"', '\\"').replace('^', ' ')
        return (
            "^XA\n"
            "^PW812\n"
            "^LL406\n"
            "^PON\n"
            "^FO20,40\n"
            "^A0N,80,80\n"
            "^FB772,3,0,C\n"
            f"^FD{safe_sku}^FS\n"
            "^FO60,210\n"
            "^BCN,130,Y,N,N\n"
            f"^FD{safe_upc}^FS\n"
            "^XZ"
        )
    return (
        "^XA\n"
        "^PW812\n"
        "^LL406\n"
        "^PON\n"
        "^FO20,138\n"
        "^A0N,100,100\n"
        "^FB772,3,0,C\n"
        f"^FD{safe_sku}^FS\n"
        "^XZ"
    )


def item_barcode_zpl(upc: str) -> str:
    """1.5x1 inch (304x203 dots @ 203 DPI) item barcode label.

    UPC-A symbology with a human-readable digit row. Used for thread
    spools and other small items whose vendor barcode is missing or
    too small to scan reliably.
    """
    safe_upc = (upc or '').strip().replace('"', '\\"').replace('^', ' ')
    return (
        "^XA^PW304^LL203^LH0,0"
        f"^BY2^FO57,15^BUN,140,Y,N,Y^FD{safe_upc}^FS"
        "^XZ\n"
    )


def item_barcode_zpl_bulk(upc: str, quantity: int) -> str:
    """Concatenate the same 1.5x1 label `quantity` times so a single
    Zebra job emits a strip; matches the legacy batch-print behavior."""
    qty = max(1, min(100, int(quantity)))
    return item_barcode_zpl(upc) * qty


# ---- Thread catalog --------------------------------------------------------


class ThreadCatalog:
    """In-memory thread catalog loaded from a CSV at construction.

    The CSV has columns: brand, size, color, upc, sku. The loader
    re-reads the file on disk every time the mtime advances so a hot
    edit in the data directory takes effect without a process restart.
    Thread-safe under the GIL: reads serialize through a lock against
    concurrent reloads, which only happens on a request after the
    file changes.
    """

    def __init__(self, csv_path: str):
        self._csv_path = csv_path
        self._catalog: Dict[str, Dict[str, List[Dict[str, str]]]] = {}
        self._mtime: float = 0.0
        self._lock = threading.Lock()
        self.reload()

    def reload(self) -> None:
        with self._lock:
            if not os.path.exists(self._csv_path):
                self._catalog = {}
                self._mtime = 0.0
                logger.warning(
                    "Thread catalog file missing: %s", self._csv_path,
                )
                return
            mtime = os.path.getmtime(self._csv_path)
            if mtime == self._mtime and self._catalog:
                return
            catalog: Dict[str, Dict[str, List[Dict[str, str]]]] = {}
            with open(self._csv_path, 'r', encoding='utf-8') as f:
                for row in csv.DictReader(f):
                    brand = (row.get('brand') or '').strip()
                    size = (row.get('size') or '').strip()
                    if not brand or not size:
                        continue
                    catalog.setdefault(brand, {}).setdefault(size, []).append({
                        'color': (row.get('color') or '').strip(),
                        'upc': (row.get('upc') or '').strip(),
                        'sku': (row.get('sku') or '').strip(),
                    })
            for sizes in catalog.values():
                for products in sizes.values():
                    products.sort(key=lambda x: x['color'])
            self._catalog = catalog
            self._mtime = mtime
            total = sum(
                len(products)
                for sizes in catalog.values()
                for products in sizes.values()
            )
            logger.info(
                "Thread catalog loaded: %d products across %d brands",
                total, len(catalog),
            )

    def is_empty(self) -> bool:
        self.reload()
        return not self._catalog

    def as_response(self) -> Dict:
        self.reload()
        brands = sorted(self._catalog.keys())
        result = {}
        for brand, sizes in self._catalog.items():
            result[brand] = {
                'sizes': sorted(sizes.keys(), key=_size_sort_key),
                'products': sizes,
            }
        return {'brands': brands, 'catalog': result}


# ---- Size sort -------------------------------------------------------------


_WIRE_ORDER = {'XS': 0, 'Small': 1, 'Brassie': 2, 'Medium': 3, 'Large': 4}
_SML_ORDER = {'S': 0, 'M': 1, 'L': 2}
_FRAC_RE = re.compile(r'^(\d+)/0$')
_DENIER_RE = re.compile(r'^(\d+)\s*Denier')
_MONO_RE = re.compile(r'^Monofilament\s+([\d.]+)')
_PD_RE = re.compile(r'^(.+?)\s+(\d+)D$')


def _size_sort_key(size: str):
    """Smart sort: wire gauge order, then thread fractions (6/0 < 8/0),
    then denier ascending, then alpha. Matches legacy UTC tab behavior."""
    s = (size or '').strip()
    if s in _WIRE_ORDER:
        return (0, _WIRE_ORDER[s], '')
    frac = _FRAC_RE.match(s)
    if frac:
        return (1, int(frac.group(1)), '')
    den = _DENIER_RE.match(s)
    if den:
        return (2, int(den.group(1)), s)
    mono = _MONO_RE.match(s)
    if mono:
        return (3, float(mono.group(1)), '')
    pd = _PD_RE.match(s)
    if pd:
        return (4, pd.group(1), int(pd.group(2)))
    parts = s.rsplit(' ', 1)
    if len(parts) == 2 and parts[1] in _SML_ORDER:
        return (5, parts[0], _SML_ORDER[parts[1]])
    return (6, s, 0)
