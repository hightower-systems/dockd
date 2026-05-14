<div align="center">
  <h1>Dockd</h1>

  <p><em>Open-source shipping station orchestrator for warehouse pack lines</em></p>

  ![Version](https://img.shields.io/badge/version-0.7.0-8e2716)
  ![Tests](https://img.shields.io/badge/tests-245%20passing-34a853)
  ![License](https://img.shields.io/badge/license-Apache_2.0-blue)

  **[Releases](https://github.com/hightower-systems/dockd/releases)** | **[Changelog](CHANGELOG.md)** | **[Security](SECURITY.md)**
</div>

---

# Dockd

**Open-source shipping station orchestrator for warehouse pack lines.**

Dockd runs as a Flask web app in a container, serves browser-based pack
stations on a warehouse LAN, talks to a pluggable order backend, generates
labels through ShipRush, and drives USB scales + Zebra ZPL printers via
per-station agents.

## What Dockd Does

- **Order load on scan** -- operator scans an order number, dockd fetches it
  from the configured backend and renders pick info, customer address, and
  the order's ship method
- **Box library** -- scannable box codes resolve to warehouse-defined L/W/H
  dimensions; admin manages the library through the settings UI
- **Carrier optimization engine** -- picks USPS vs UPS vs FedEx based on box
  preference, dim-weight, longest side, package weight, destination ZIP
  (rural-DAS detection), customer-paid shipping, and a configurable
  high-value-order threshold
- **ShipRush label generation** -- generates ZPL labels via ShipRush's XML
  API; per-carrier service codes and account GUIDs configurable per
  deployment
- **USB scale + Zebra ZPL printing** -- per-station agent reads weight from
  HID-class scales and forwards labels to the station's Zebra printer
- **Forced-password-change first-login flow** -- bootstrap admin / admin is
  unusable until rotated; a before-request gate blocks every endpoint until
  the operator sets a new password
- **Local audit DBs** -- SQLite for shipping history (carrier, cost,
  fulfillment timing) and manual scan overrides

## What Dockd Is Not

Dockd is not an ERP or a WMS. It does not own the order, the inventory, or
the customer record; it talks to whatever upstream system does. The
canonical implementation targets
[Sentry-WMS](https://github.com/hightower-systems/sentry-wms) and the
`OrderBackend` interface keeps the door open for additional implementations
(a NetSuite-direct integration is preserved off-tree for re-introduction).

Dockd is not a marketplace or a shipping rate shopper. ShipRush handles the
carrier-side label generation; dockd decides which carrier to ask.

## Architecture

| Layer | Technology |
|-------|-----------|
| Web app | Python / Flask  -  factory-built, blueprint-organized (`auth`, `shipping`, `settings`); runs in a container (Azure / Docker / wherever) |
| Frontend | Server-rendered HTML + vanilla JavaScript (no framework dependency); on page load fetches `/whoami` from the local scale agent to capture station identity + Sentry token |
| Storage | SQLite for shipping history + override audit; JSON for settings + users (chmod 600, atomic writes) |
| Order backend | `OrderBackend` Protocol + `SentryBackend` HTTP client against Sentry-WMS v1.9 dockd surface; pluggable for other ERPs |
| Scale agent | Per-station Python process at `127.0.0.1:5050` (`agent/agent.py`); owns USB HID scale + Zebra ZPL printer + HP LaserJet packing-slip printer. CORS pinned to dockd origin |

## Quick Start

```sh
# Clone and install
git clone https://github.com/hightower-systems/dockd.git
cd dockd
pip install -r requirements.txt

# Copy environment config and set a secret
cp .env.example .env
# Edit .env; at minimum set SECRET_KEY
#   SECRET_KEY -- python -c "import secrets; print(secrets.token_hex(32))"

# Run
python run.py
```

Open `http://localhost:5001`. First login is `admin` / `admin`. You'll be
required to set a new password before any other endpoint will respond. Then
walk the **Settings** tabs to populate boxes, stations, ShipRush keys, and
shipper origin -- nothing in the repo is pre-populated with one
organization's data.

Docker:

```sh
docker compose up
```

## Configuration model

Three on-disk stores, all gitignored, all auto-created with empty / sensible
defaults on first boot.

| File | Holds | Created by |
|------|-------|-----------|
| `.env` | Secrets: `SECRET_KEY`, `SHIPRUSH_TOKEN`, `SHIPRUSH_ENDPOINT`, future `SENTRY_BASE_URL`. | Operator (`cp .env.example .env`); the settings UI can also write to it. |
| `settings.json` | Operational: box library, FedEx One Rate boxes, stations, shipper origin, carrier optimization rules, high-value threshold, label cache TTL, ShipRush accounts + service catalog, Amazon ship-method strings, override-scan SKU allow-list. | `SettingsStore` on first boot from `app/services/default_settings.py`. chmod 600. |
| `users.json` | Local dockd credentials: username, role (`admin` or `user`), scrypt password hash, `must_change_password` flag. | `UsersStore` on first boot with admin / admin + forced rotation. chmod 600. |

A first-time admin signs in, sets a new password, then walks the Settings
tabs to populate the deployment-specific data.

## Settings panel

Admin-only HTML panel at `/settings` with nine tabs:

- **Boxes** -- add / edit / remove warehouse boxes (ID, label, L/W/H, USPS / UPS / WEIGHT_THRESHOLD preference); separate card for FedEx One Rate boxes and the size-ordered list used to pick the smallest box that fits scanned dims.
- **Carrier rules** -- high-value threshold, weight crossover, dim-weight cutoffs, longest-side max, max USPS weight, rural shipping thresholds, and the per-branch reason text shown to the operator.
- **Stations** -- pack-station list (ID, label, host, port, local flag) + default station ID.
- **Shipper origin** -- the return address embedded in every ShipRush label, plus fallback customer phone.
- **ShipRush** -- account GUIDs, service catalog (per-carrier carrier_id / account_key / service_code / one-rate flag), and carrier method IDs.
- **Amazon methods** -- ship-method strings that trigger the marketplace-picked-carrier modal.
- **Override SKUs** -- scan-override allow-list, with one-column CSV import (replace or append).
- **Users** -- add / remove / reset password / change role. Admin password resets force the target user to rotate on next login.
- **Secrets (.env)** -- allow-listed env vars (`SHIPRUSH_TOKEN`, `SHIPRUSH_ENDPOINT`, `SECRET_KEY`, `SENTRY_BASE_URL`, `BACKEND`) writable through the UI; the page reports which are set without ever returning values.

A `PATCH /api/settings` round-trip applies immediately to the live carrier
engine, printer service, and ShipRush client; no restart required.

## Security

- Bootstrap admin uses the well-known password `admin` and is locked behind a server-enforced password-rotation gate; no endpoint except `/`, `/login`, `/logout`, `/api/change-password`, `/health`, and `/static/*` responds until the password is rotated.
- `settings.json` and `users.json` are written atomically and chmod 600 on every write.
- Scrypt password hashes via `werkzeug.security.generate_password_hash`.
- CSRF gate on every POST that rejects unknown origins; `/login` is explicitly exempt.
- Rate-limit on `/login` via Flask-Limiter (5 per minute).
- Secrets allow-list on the `/api/settings/secrets` endpoint so a misconfigured client cannot overwrite arbitrary env vars on disk.
- "Cannot remove the only admin" and "cannot demote the only admin" guards in `UsersStore`.

## Testing

```sh
python -m pytest
```

245 tests at v0.7.0 covering authentication + role gating, forced
password-change flow, CarrierEngine determinations, label-cache behavior,
settings store + user store CRUD, the settings blueprint surface, the
SentryBackend HTTP client (every wire-level success + failure path),
the backend-wired shipping routes (load / ship / manual-link / void),
ShipAttemptsStore lifecycle, the restart-time retry path for
pending + unknown rows, BackendHealth state transitions + caching,
the RedactionFilter (token scrub, bearer scrub, header scrub),
ShipRush `_resolve_carrier` fallback (no `None` returns under empty
settings), index-template admin-only markup, the dynamic-row Settings
UI edit-preservation fix, the international shipping path
(CustomsData parsing, country normalization, banned-country gate,
ShipRush XML commodities + CustomsValue + IncotermsCode emission,
XML injection escaping, international tracking-number prefix
inference, adult-signature DCISType emission across all three
carriers), and a cross-cutting security regression suite (no token
leak in logs, TLS validation hardcoded, no scrypt hashes in
responses, settings/users files chmod 600 after every write, no
shipper-tax-id leakage through the non-admin public-settings
endpoint).

## Project Status

**v0.7.0** -- International shipping, dockd side. Destination
country flows from Sentry through to ShipRush (no more hardcoded
`<Country>US</Country>`); a `<Commodities>` block + `<CustomsValue>`
+ `<IncotermsCode>` are emitted for non-US labels. A banned-
destination hard gate (seeded with OFAC defaults CU/IR/KP/SY) runs
server-side before any carrier call, so a sanctioned consignee
cannot get a label even if the operator UI is bypassed. A new
admin-only Settings tab exposes the banned list + shipper tax IDs
(EIN/EORI/IOSS/UK VAT). A per-ship "Adult Signature" sidebar
toggle emits the carrier-correct `<DCISType>` (UPS/USPS `ADS`,
FedEx `F4`) for the next ship and auto-disarms on success.
`ship_history` gains `destination_country`, `customs_value`,
`customs_currency`, `hs_codes` columns. Frontend gets an `INTL`
pill on intl orders. Dockd is wire-ready for Sentry's v1.11
item-master extension (`hs_code`, `country_of_origin`,
`unit_weight_oz`, `unit_value`); until that ships, intl orders
should be handled via the manual-link path.

| Version | Milestone | Status |
|---------|-----------|--------|
| **v0.1.0** | **Foundation -- backend-agnostic Flask app, SettingsStore + UsersStore + forced password change, ShipRush + carrier engine + printer settings-driven, NetSuite removed from main** | ✅ Released |
| **v0.2.0** | **Sentry backend wired -- `OrderBackend` Protocol + `SentryBackend` HTTP client against Sentry-WMS v1.9 dockd surface, ShippingService refactored, frontend so_number rename + dead-path cleanup** | ✅ Released |
| **v0.3.0** | **Scale agent v2 + browser bootstrap -- agent binds 127.0.0.1, CORS pinned to dockd origin, /whoami endpoint, browser forwards X-Sentry-Token on every dockd call, print flow flips so dockd returns ZPL and the browser forwards to localhost agent, station_label persisted in shipping_history, docs/STATION_SETUP.md walkthrough** | ✅ Released |
| **v0.4.0** | **Crash-recovery idempotency -- ship_attempts SQLite table with pending/success/unknown/rejected state machine wrapped around every backend write (ship / void / manual_link), opt-in restart-time retry of pending+unknown rows using the same UUID4 key, expanded ship_history with Sentry IDs + voided_at + idempotency_key cross-reference** | ✅ Released |
| **v0.5.0** | **Observability + security hardening -- backend health monitor + connectivity dot + admin details modal, RedactionFilter on every log handler (wms_t_*, Bearer, X-Sentry-Token), opt-in periodic in-process retry of unknown ship_attempts rows, security regression suite** | ✅ Released |
| **v0.6.0** | **First end-to-end ship against a real Sentry-WMS + real ShipRush -- bug fixes from the integration test (ShipRush `_resolve_carrier` fallback under empty settings; sidebar admin buttons visible after JS login without page reload), conftest env-isolation so dev `.env` does not bleed into the test suite** | ✅ Released |
| **v0.7.0** | **International shipping (dockd side) -- destination country passthrough, ShipRush `<Commodities>` + `<CustomsValue>` + `<IncotermsCode>` emission, banned-destination hard gate (OFAC defaults), per-ship adult-signature toggle for all three carriers (`DCISType` ADS/F4), Settings International tab, INTL pill in operator UI, `ship_history` schema extension, Settings UI dynamic-row edit-preservation fix** | ✅ Released |
| v0.8.0 | International phase 2 -- CN22 / CN23 customs forms, commercial invoice PDFs, DDP/DDU per-order toggle, metric weight thresholds | Planned |
| v1.0.0 | Production release -- scripted integration test against a real Sentry instance, migration playbook from `v0.x` deployments | Planned |

See [CHANGELOG.md](CHANGELOG.md) for detailed release notes.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

Apache License 2.0 -- see [LICENSE](LICENSE) and [NOTICE](NOTICE) for details.

Built by [Hightower Systems L.L.C.](https://github.com/hightower-systems) · v0.7.0
