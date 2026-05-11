# Changelog

All notable changes to Dockd will be documented in this file.

## [v0.1.0] - 2026-05-11

"Foundation" release. First public-source cut of dockd as a backend-agnostic
shipping station orchestrator. The legacy NetSuite-direct integration is
removed from `main` (preserved off-tree for future re-introduction), the
service graph is reshaped around a pluggable `OrderBackend` placeholder, and
every previously-hardcoded deployment value (boxes, stations, ShipRush
account GUIDs, shipper origin, carrier-optimization thresholds,
high-value-order rule, Amazon ship-method list, override SKUs) moves into a
read-through `SettingsStore` editable from a new admin-only settings page.

The order-backend is wired as `backend=None`; `/get_order_details`,
`/ship_order`, and `/manual_link_tracking` return a structured
"backend not configured" error until `v0.2.0` lands the `SentryBackend`
implementation. ShipRush label generation, the carrier-optimization engine,
the printer service, label reprint, ShipRush void, and the override audit
log all work today without an order backend.

### Added -- Settings system

- **`SettingsStore`** (`app/services/settings.py`): JSON-backed,
  read-through-with-mtime-cache operational settings store at
  `settings.json` (override via `SETTINGS_PATH`). Atomic writes via
  tempfile + `os.replace`; `chmod 600` enforced on every write. Auto-created
  on first boot from `app/services/default_settings.py` with deliberately
  empty / neutral defaults so the open-source repo carries no deployment
  data. `replace()` merges with defaults so the UI cannot drop unrelated
  keys; `patch()` updates a subset. `public_subset()` exposes just the
  fields the operator UI needs (`high_value_threshold`, `amazon_methods`,
  box id+label).
- **`UsersStore`** (`app/services/users_store.py`): separate JSON file
  (`users.json`, override via `USERS_PATH`) so the highest-sensitivity data
  has the narrowest set of endpoints that touch it. Scrypt hashes via
  `werkzeug.security.generate_password_hash`. Bootstraps with a single
  `admin` user, password `admin`, `must_change_password=True`. New users
  default to `must_change_password=True`; admin password resets re-arm the
  flag. Guards "cannot remove the only admin" and "cannot demote the only
  admin".
- **Settings blueprint** (`app/blueprints/settings.py`): admin-gated
  `GET / PUT / PATCH /api/settings`, `POST /api/settings/secrets` (env-var
  writer with allow-list), `GET /api/settings/secrets/presence` (reports
  which keys are set without returning values), and full user CRUD
  (`GET / POST /api/users`, `DELETE`, `PUT password`, `PUT role`). Public
  read at `GET /api/settings/public` for the operator UI.
- **Settings HTML panel** (`app/templates/settings.html`): nine tabs
  covering Boxes (CRUD), Carrier rules, Stations, Shipper origin, ShipRush
  accounts + service catalog, Amazon methods, Override SKUs (with CSV
  import), Users, and Secrets (.env). All edits round-trip through PATCH
  and apply immediately to the live carrier engine, printer service, and
  ShipRush client.
- **CSV import for override SKUs**: one-column CSV (`SKU` header detected
  + skipped, BOM-stripped, deduped) imports into the textarea in either
  replace or append mode; admin reviews and clicks Save to commit.

### Added -- Forced password change

- **`must_change_password` user flag** carried through `UsersStore.verify()`
  into the session.
- **`/api/change-password`** endpoint verifies the current password, sets
  the new one, clears the flag, and refreshes the session.
- **Before-request gate in `auth_bp`** returns
  `403 { must_change_password: true }` for every path outside the
  allow-list (`/`, `/login`, `/logout`, `/api/change-password`, `/health`,
  `/static/*`) while the flag is set.
- **Forced-change modal in `index.html`** drops over the operator UI on
  login or page reload when the flag is set; locks input behind
  current + new + confirm fields.
- **Self-service vs admin reset distinction:** self-service change clears
  the flag, admin reset (`PUT /api/users/<u>/password`) leaves it set so
  the target user rotates on next login.

### Added -- Services + factory

- **`OrderBackend` placeholder** -- `ShippingService.__init__` accepts a
  `backend` parameter; v0.1.0 wires `backend=None`. The order-fetch /
  ship-write / manual-link methods return
  `{'status': 'error', 'message': 'Order backend not configured...'}`
  until the Sentry implementation lands.
- **Generic input validators** at `app/services/validation.py`
  (`validate_ticket`, `validate_upc`) replace the NetSuite-coupled
  helpers.
- **CarrierEngine, PrinterService, ShipRushClient** all take a
  `settings_store` and read the relevant collections (boxes, FedEx
  boxes, stations, accounts, services, shipper origin) on every call.
  No restart needed after a settings PATCH.

### Changed

- **ShipRush token + endpoint moved to env vars** (`SHIPRUSH_TOKEN`,
  `SHIPRUSH_ENDPOINT`); `ShipRushClient` reads them lazily so the
  settings page's `/api/settings/secrets` endpoint can rotate them
  live (writes to `.env` via `dotenv.set_key`, updates `os.environ`,
  the next request reads the new values).
- **Login decorator** treats `/api/*` paths as API endpoints and returns
  `401 { status: 'error' }` for unauthenticated requests instead of the
  legacy HTML redirect.
- **`override_exception_skus`** moved off the previous on-disk
  `override_exceptions.csv` (now gone) into a settings field, populated
  by the admin via the Override SKUs tab and the new CSV import.

### Removed

- **NetSuite-direct integration**: `app/services/netsuite.py`, the
  `NETSUITE_ACCOUNT_ID` / `NS_*` env vars, the `'ns_id'` session field,
  the "UPDATE NETSUITE" frontend button, and the per-ShippingService
  NetSuite call sites. Preserved off-tree for future re-introduction
  behind the `OrderBackend` interface.
- **`requests-oauthlib`** dependency.
- **Per-laptop `AGENT_API_KEY`** env var (the scale-agent refactor in
  `v0.4.0` will replace it with `127.0.0.1`-bind + CORS pinned to the
  dockd origin).
- **`override_exceptions.csv`** root file; data lives in
  `settings.override_exception_skus` instead.
- **Hardcoded shipper origin** in the `shiprush.py` XML payload (was a
  per-deployment return address baked into the source). The open-source
  repo ships with an empty shipper-origin block; admin sets per-deployment
  values via the Shipper Origin settings tab.

### Security

- `settings.json` and `users.json` written atomically with `chmod 600`
  on every write.
- Scrypt password hashes; legacy hardcoded admin scrypt hash removed.
- Forced password rotation on first login enforced server-side, not just
  in the UI; a buggy client that skips the modal still hits the 403 gate.
- `POST /api/settings/secrets` accepts only an allow-list of writable env
  vars (`SHIPRUSH_TOKEN`, `SHIPRUSH_ENDPOINT`, `SECRET_KEY`,
  `SENTRY_BASE_URL`, `BACKEND`).
- `.env`, `settings.json`, `users.json`, runtime SQLite (`*.db`),
  `ship_history.json`, the `label_history/` ZPL cache, and `logs/`
  are all gitignored.

### Migrations

None. v0.1.0 is the first public-source release; the on-disk SQLite
schema is created by `init_all_dbs()` on boot.

### Open

- `OrderBackend` Protocol formalization + `SentryBackend` implementation
  (planned for `v0.2.0`).
- `ship_attempts` SQLite for crash-recovery idempotency (`v0.3.0`).
- Scale agent v2 with `/whoami` + CORS + 127.0.0.1 bind (`v0.4.0`).
- Health-check polling + connectivity indicator in the operator UI
  (`v0.5.0`).

See [README.md](README.md#project-status) for the full version roadmap.
