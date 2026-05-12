# Changelog

All notable changes to Dockd will be documented in this file.

## [v0.4.0] - 2026-05-11

"Crash-recovery idempotency" release. Every backend write -- ship,
void, manual-link -- now opens a row in a new `ship_attempts` SQLite
table BEFORE the network call, then transitions the row to
`success`, `unknown`, or `rejected` based on the outcome. On dockd
restart (opt-in via `DOCKD_RETRY_PENDING_ON_BOOT=true`), any row
still in `pending` or `unknown` state is retried with the same
UUID4 key. Sentry's own `dockd_idempotency` table either replays
the cached response (the original committed before the crash) or
re-executes the write (the original rolled back) -- so retry is
always safe, never double-ships.

The local `ship_history` table also gets a v0.4.0 expansion:
`external_id`, `customer_shipping_paid`, `order_total`,
`sentry_audit_log_id`, `sentry_fulfillment_id`, `manual_link`,
`idempotency_key`, `voided_at`, `void_reason` -- enough to
reconstruct the full ship + void timeline from a single row
without a Sentry round trip.

### Added -- ship_attempts table

- **`init_ship_attempts_db()`** in `app/models/database.py` creates
  `ship_attempts (id, idempotency_key UNIQUE, operation, so_number,
  request_body JSON, request_body_sha256, status CHECK, response_body,
  response_status, error_kind, attempt_count, last_attempt_at,
  created_at)` plus three indexes: status, (status, last_attempt_at)
  for the recoverable scan, and so_number for per-order audits.
- **`app/services/ship_attempts.py`** -- `ShipAttemptsStore` with
  `insert_pending`, `mark_success`, `mark_unknown`, `mark_rejected`,
  `get`, `find_recoverable`, `list_recent`, `prune_terminal`. Threading
  lock around writes; SHA-256 body hash via a stable JSON encoder
  (sort_keys + compact separators) so reorder-equivalent bodies
  hash identically. `new_idempotency_key()` mints UUID4 strings;
  centralized so all callers grep to one place.

### Added -- ShippingService backend-write lifecycle

- **`_classify_for_attempt(exc)`** maps backend exceptions to
  ship_attempts states:
  `NetworkError | IdempotencyLockTimeoutError | RateLimitedError -> unknown`
  (retryable on next restart);
  `AlreadyShippedError | NotInShippableStatusError |
  UnknownOperatorError | IdempotencyMismatchError | InvalidBodyError |
  NotFoundError | NotShippedError -> rejected` (backend has spoken);
  unmapped `BackendError -> unknown` (defensive: a transient
  surprise gets a retry).
- **`ship_order`** now calls `insert_pending(operation='ship', ...)`
  immediately before `backend.confirm_shipped`; the catch arm marks
  the row by classification; the success path marks success with
  the full response cached so a subsequent retry short-circuits.
- **`void`** same lifecycle around `backend.void_ship`. A
  `NotShippedError` (peer already voided) is treated as a successful
  retry: mark success with `{status: 'already_voided'}` body.
- **`manual_link`** same lifecycle around its `confirm_shipped`
  call.

### Added -- restart-time retry

- **`ShippingService.retry_recoverable_attempts(limit=50)`** drains
  pending / unknown rows via `find_recoverable`, dispatches on
  `operation` (`ship` and `manual_link` -> `confirm_shipped`;
  `void` -> `void_ship`), re-uses the original key, and marks the
  row based on the new outcome.
- **App factory boot hook** (`app/__init__.py`) calls
  `retry_recoverable_attempts()` when `DOCKD_RETRY_PENDING_ON_BOOT`
  is set to `1` / `true` / `yes`. Off by default so test runs and
  CI do not hammer the backend. Logs a one-line summary of the
  drain. Safe when `backend is None` (returns `[]`).

### Added -- ship_history expansion

- New columns: `external_id`, `customer_shipping_paid`,
  `order_total`, `sentry_audit_log_id`, `sentry_fulfillment_id`,
  `manual_link`, `idempotency_key`, `voided_at`, `void_reason`.
  Idempotent migration via `PRAGMA table_info` + per-column
  `ALTER TABLE ADD COLUMN` for pre-v0.4.0 databases.
- New index `idx_ship_history_idem` on `idempotency_key` so the
  ship-attempts row can be cross-referenced to the local history
  row in one indexed lookup.
- **`_log_to_db`** signature expanded; populated with the Sentry
  IDs returned by `confirm_shipped`, the order's external_id,
  customer_shipping_paid, order_total from the `OrderData` fetch,
  and the per-ship `idempotency_key` from `ship_attempts`.
- **`_mark_history_voided(so_number, voided_at, reason)`** updates
  the matching ship_history row with `voided_at` + `void_reason`
  after a successful void; best-effort, an exception is logged but
  does not fail the void.

### Tests

- 11 new `ShipAttemptsStore` tests
  (`tests/services/test_ship_attempts.py`): canonical-body hashing
  (order-independent, value-sensitive), full lifecycle
  (insert -> success / unknown / rejected), invalid-operation
  rejection, UNIQUE-key-reuse `IntegrityError`,
  `find_recoverable` filters to pending+unknown, `prune_terminal`
  honors the never-prune-unknown rule.
- 5 new retry-integration tests (`tests/services/test_ship_retry.py`)
  with a per-test cleanup fixture so the session-scoped
  ship_attempts table starts each test empty: pending-ship -> success,
  unknown-ship + network-error -> still unknown,
  pending-ship + AlreadyShipped -> rejected,
  void dispatch, no-backend safe.
- Total: 153 passing (137 -> 153).

### Security

- `ship_attempts.request_body` is JSON; **idempotency_key is not a
  secret** (UUID4, single-use), but the body of a ship request
  contains the operator's tracking number and the Sentry
  fulfillment context. The file sits inside `shipping_history.db`
  which is gitignored, lives on the container's volume, and is
  unreadable from outside the container.
- `mark_unknown` / `mark_rejected` recorded `error_kind` only;
  raw `details` payloads land in `response_body` JSON. A future
  redaction filter (planned for `v0.5.0`) will scrub `wms_t_*`
  patterns from the error_message fields as defense in depth.

### Open

- **Health-check polling + connectivity indicator in the operator
  UI** (`v0.5.0`).
- **Log redaction** for `wms_t_*` tokens + PII (`v0.5.0`).
- **Periodic in-process retry** (background thread that drains
  unknown rows every N seconds while dockd is running). v0.4.0
  only retries at restart, which covers the crash case but not the
  case where a transient network blip recovers a few minutes later
  while dockd stays up.
- **Production integration test** against a real Sentry instance
  (`v1.0.0`).

## [v0.3.0] - 2026-05-11

"Scale agent v2 + browser bootstrap" release. Each pack station now
runs the dockd scale agent locally, bound to `127.0.0.1` and CORS-
pinned to the dockd origin. The browser fetches `/whoami` on page
load to capture the station identity + per-station Sentry bearer
token, then forwards `X-Sentry-Token` on every dockd API call. The
ship + reprint flows flip: dockd returns `zpl_b64` in the response;
the browser POSTs the bytes to its local agent's `/print` endpoint.
The dockd container itself no longer touches printer hardware.

This release reorders the v0.x roadmap: scale agent + browser
bootstrap was originally planned for v0.4.0; ship_attempts SQLite
idempotency was v0.3.0. Reordered because the agent file was
unblocked and getting end-to-end Sentry -> dockd -> agent working at
one station is more valuable than crash-recovery hardening on top of
a not-yet-deployed flow.

### Added -- Scale agent

- **`agent/agent.py` (v2.0)** -- per-station Python process that
  owns the USB HID scale, the Zebra ZPL label printer, and the HP
  LaserJet packing-slip printer. Five endpoints:
  - `GET /whoami` -- returns `station_id`, `station_label`,
    `sentry_token`, `agent_version`. Browser caches in module-scope
    memory; cleared on Chrome restart.
  - `GET /scale` -- USB HID weight read with three-retry loop and
    ounce-to-pound conversion when the scale reports unit code 11.
  - `POST /print` -- accepts raw ZPL bytes from the browser, writes
    to a temp file, sends to the Zebra via Windows
    `cmd /c copy /B`.
  - `POST /print-html` -- accepts HTML bytes, prints via
    `ShellExecuteW('printto', ..., HP_PRINTER)` with a default-printer
    fallback if the direct call fails.
  - `GET /health` -- unauthenticated liveness with scale-connected
    flag + agent version.
- **`agent/agent_config.example.json`** -- v2 schema:
  - Required: `station_id`, `station_label`, `dockd_origin`,
    `sentry_token`.
  - Optional: `agent_port` (5050), `zebra_printer` (auto-detect
    `\\{ip}\ZEBRA` when blank), `hp_printer` (`HPLASER`),
    `scale_vendor_id` / `scale_product_id` (Mettler / DYMO
    defaults), `log_dir` / `log_max_bytes` / `log_backup_count`.
  - Removed from the legacy v1 schema: `server_ip` (vestigial,
    never read), `api_key` (replaced by 127.0.0.1 bind + CORS pin).
- **`agent/requirements.txt`** -- `flask>=2.0`, `flask-cors>=4.0`,
  `hidapi>=0.14`.
- **`agent/README.md`** -- endpoint reference, Windows install
  steps, config schema, and the security rationale for dropping
  `X-Agent-Key`.

### Added -- Browser bootstrap

- **`/whoami` page-load fetch** in `index.html`. On reach: stashes
  `station_id` / `station_label` / `sentry_token` in module-scope
  globals; renders the station label in the sidebar footer. On
  fail: drops a fixed red banner across the top
  ("SCALE AGENT NOT RUNNING -- start agent.py on this station").
- **`dockdApi(path, options)`** -- wrapper around `fetch` that
  injects `X-Sentry-Token` on every dockd API call. All major
  dockd-side fetches (`/get_order_details`, `/ship_order`,
  `/manual_link_tracking`, `/reprint_label`, `/void_label`) now go
  through it. Session-auth endpoints (`/login`, `/logout`,
  `/api/change-password`, `/log_override`, `/ship_count`) stay on
  bare `fetch` -- they don't need the Sentry token.
- **`agentScale()`** -- replaces the server-side `/get_scale_weight`
  endpoint. The dockd container has no scale hardware; the agent
  on the laptop reads the USB scale directly.
- **`agentPrintZpl(zpl_b64)`** -- decodes base64 to raw bytes and
  POSTs to `http://localhost:5050/print`. Returns
  `{status, message}` to the caller.

### Changed -- Print flow flip

- **`ShippingService.ship_order`** -- no longer calls
  `self.printer.send()`. Includes `zpl_b64` in the success response
  for the browser to forward to its local agent. The dockd container
  is the wrong place to drive a station's printer; it lives in
  Azure and the printer is on the laptop's LAN.
- **`ShippingService.reprint`** -- same pattern. Returns
  `zpl_b64` from the local label cache; browser forwards.
- **`PrinterService`** -- still in the codebase but unused on the
  ship path. Kept for back-compat with any legacy call site; will
  be removed once nothing references it.
- **`/get_scale_weight` route** -- still wired but unused. Frontend
  now calls `agentScale()` directly. Server-side `ScaleReader` only
  works if the dockd process and the USB scale are on the same
  host, which is no longer the deployment model.

### Added -- station_label persistence

- **`ship_history.station_id` + `ship_history.station_label`**
  columns. Added via `init_ship_db()` -- the CREATE includes them;
  for pre-v0.3.0 databases, an idempotent `PRAGMA table_info` +
  `ALTER TABLE ADD COLUMN` block adds them in place.
- **`/ship_order` blueprint** accepts `station_id` + `station_label`
  in the payload; the browser sources both from the agent's
  `/whoami` response and forwards on every ship.
- **`ShippingService._log_to_db`** writes both into `ship_history`.

### Added -- Documentation

- **`docs/STATION_SETUP.md`** -- full deployment + per-station
  setup walkthrough in the same shape as the Sentry-WMS docs:
  Part 1 dockd deploy, Part 2 station setup, Part 3 daily startup,
  Part 4 troubleshooting, Part 5 admin operations, Quick Reference
  Card at the end designed to be printed and taped to the wall.

### Security

- Scale agent binds `127.0.0.1` only -- no off-laptop reach.
- CORS on the agent pinned to `dockd_origin` from config; a
  third-party site an operator stumbles onto cannot script the
  local hardware.
- `agent_config.json` carries the per-station Sentry bearer token
  and is now in `.gitignore` (alongside `agent/logs/` and
  `agent/_temp_*`). Setup guide includes Windows `icacls` command
  to lock it to the current user (chmod 600 equivalent).
- Browser holds the bearer token in module-scope JS memory after
  `/whoami` -- a malicious browser extension is the realistic
  exposure surface; the recommended mitigation is dedicated kiosk
  hardware with a controlled browser, plus token rotation when
  exposure is suspected.

### Removed -- legacy assumptions

- **`X-Agent-Key` header on the agent** -- replaced by the
  127.0.0.1 bind + CORS pin combination.
- **`server_ip` config field on the agent** -- the central-server-
  to-station-IP call direction is gone; the browser drives the
  agent.
- **Server-side print path in `ShippingService.ship_order` and
  `.reprint`** -- the browser forwards now.

### Tests

- Updated `test_ship_order_happy_path` to assert on the new
  `zpl_b64` field in the response (the browser-forwarding contract)
  and to pass `station_id` + `station_label` in the request body.
- 137 passing.

### Open

- **`ship_attempts` SQLite for crash-recovery idempotency** --
  moved to `v0.4.0`. Persists `(idempotency_key, request_body,
  status)` before any backend call so a crash mid-ship does not
  lose the key. Restart retries the same UUID4 against Sentry,
  which replays the cached response.
- **Health-check polling + connectivity dot** in the operator UI
  (`v0.5.0`).
- **Log redaction** for `wms_t_*` tokens, PII (`v0.5.0`).

## [v0.2.0] - 2026-05-11

"Sentry backend wired" release. The `OrderBackend` Protocol takes its
first implementation, `SentryBackend`, against Sentry-WMS's existing
v1.9 dockd surface (`GET /api/v1/dockd/orders/<so_number>`,
`POST .../ship`, `POST .../void-ship`, `GET /api/health`). No
Sentry-side changes; dockd conforms to the contract that already
ships. `ShippingService` is refactored to drive load / ship / void /
manual-link through the backend; ShipRush remains the label generator
and the carrier-optimization engine + printer service are unchanged.

The previously-placeholder routes (`/get_order_details`, `/ship_order`,
`/manual_link_tracking`, backend-void on `/void_label`) now respond
when `BACKEND=sentry` and `SENTRY_BASE_URL` are set in `.env`. With no
backend env, the v0.1.0 "backend not configured" error is still
returned so a fresh install boots without crashing.

The legacy frontend was reading several response keys that v0.1.0's
backend did not return (`data.customer.entityId`,
`data.orderTotal`, `data.amazonOrderId`, `i.quantity`); the high-value
modal silently never fired and address rendering produced blank
fields. The v0.2.0 frontend reads the actual returned shape
(`data.address.*`, `data.order_total`, `data.items[*].qty`), so those
two pre-existing bugs are also fixed in this release.

### Added -- Order backend abstraction

- **`app/services/backend/__init__.py`** -- `OrderBackend` Protocol
  (PEP 544 structural typing, no inheritance required for mocks) +
  `OrderData` / `OrderItem` / `ShippingAddress` / `ShipResult` /
  `VoidResult` frozen dataclasses with `from_dict` constructors.
  Typed exceptions keyed on Sentry's `error_kind` values:
  `NotFoundError`, `AlreadyShippedError`,
  `NotInShippableStatusError`, `IdempotencyMismatchError`,
  `IdempotencyLockTimeoutError`, `UnknownOperatorError`,
  `NotShippedError`, `InvalidBodyError`, `RateLimitedError`,
  `NetworkError`, `BackendError`.
- **`app/services/backend/sentry.py`** -- `SentryBackend` HTTP client
  using `httpx`. `X-WMS-Token` header on every request, supplied
  per-call via a `get_token` callable so the same backend instance
  serves multiple stations once scale-agent v2 lands. `verify=True`
  hardcoded; no env knob disables TLS validation by design. Optional
  ship-body fields (`ship_method` / `shipping_cost` / `weight` /
  `dims`) are omitted when None to keep payloads tight against
  Sentry's `extra='forbid'` Pydantic model. Token rotation does not
  require client rebuild.
- **21 SentryBackend unit tests** (`tests/services/test_backend_sentry.py`)
  using `httpx.MockTransport`: `get_order` happy path + 404 + 409 +
  timeout, `confirm_shipped` happy path + every mapped `error_kind` +
  500 + unmapped status, `void_ship` happy path + `not_shipped`,
  `health` (200 / 503 / timeout), construction sanity (empty
  `base_url` rejected, trailing slash stripped).

### Added -- ShippingService backend wiring

- **`load_order(so_number)`** -- calls `backend.get_order`, adapts
  `OrderData` into the dict shape the operator UI consumes today.
  Renames `fulfillment_id` to `so_number` (the primary identifier);
  `amazon_order_id` becomes an empty string (deprecated, kept for
  back-compat until the frontend stops reading it).
- **`ship_order`** -- refreshes from backend, runs the existing
  carrier-optimization engine + ShipRush label generation + printer
  send, then calls `backend.confirm_shipped(...,
  idempotency_key=uuid4())`. Typed exceptions surface user-facing
  messages: `AlreadyShippedError` carries the existing tracking,
  `NotInShippableStatusError` carries the current + allowed statuses,
  network errors instruct the operator that the label printed but the
  upstream confirm did not land (tracking shown for manual cleanup).
- **`void(*, reason, operator_username, idempotency_key)`** -- voids
  the ShipRush label first (refunds the carrier cost), then calls
  `backend.void_ship` to revert the SO on the upstream side. A
  `NotShippedError` from the backend (already voided by a peer) is
  treated as success. A non-success backend response after a
  successful ShipRush refund surfaces a clear "label refunded but
  upstream not reverted; retry the void" error.
- **`manual_link(ticket, tracking, user, *, carrier, ship_method,
  idempotency_key)`** -- calls `backend.confirm_shipped(...,
  manual_link=True)` with no ShipRush call. Carrier inferred from the
  tracking number prefix (`1Z*` -> `UPS`, USPS 20-22 digit
  pattern -> `USPS`, otherwise `UNKNOWN`) when not supplied.

### Added -- Factory + blueprint

- **`BACKEND` env selector** -- `BACKEND=sentry` + `SENTRY_BASE_URL`
  in `.env` triggers `SentryBackend` construction. Unset, empty, or
  unknown values leave the backend at None (v0.1.0 placeholder
  behavior).
- **`_resolve_sentry_token`** -- reads `flask.g.sentry_token` first
  (set by the shipping blueprint's `before_app_request` hook from
  the incoming `X-Sentry-Token` header), then falls back to
  `DOCKD_SENTRY_TOKEN` env. The env-var path is the v0.2.0 stopgap;
  scale-agent v2 in v0.4.0 replaces it with a per-station token
  delivered through the browser via `/whoami`.
- **`shipping_bp.before_app_request`** -- captures the `X-Sentry-Token`
  header onto `flask.g.sentry_token` for the duration of the request.
  The frontend does not send this header today (no token source yet);
  the wiring is in place for v0.4.0 to flip on without further
  backend changes.

### Changed -- Frontend (`app/templates/index.html`)

- **`fulfillment_id` -> `so_number`** in every request payload
  (`/ship_order`, `/manual_link_tracking`, `/reprint_label`,
  `/void_label`). `currentSoNumber` is the new primary identifier
  variable; the legacy `currentFulfillmentId` is preserved as an
  alias that mirrors it for any straggler reference.
- **`/get_order_details?ticket=...` -> `?so_number=...`** with the
  legacy `ticket` query param still accepted server-side for a stale
  browser session.
- **Dropped `/check_cancellation`** -- the phantom frontend call had
  no backend handler and the legacy Amazon-cancellation feature
  cannot be rebuilt against Sentry's current dockd surface (Sentry's
  `external_id` is a UUID, not the marketplace order ID; no
  `marketplace_order_id` column exists on `sales_orders`). See the
  audit notes in commit history for the requirements a future
  Sentry-side schema change would have to meet.
- **Dropped `choice_needed` branch** -- the multi-fulfillment picker
  modal had no Sentry analog; Sentry returns one SO per scan. The
  `choice-modal` markup stays in the template (harmless if never
  shown) for back-compat with any external reference.
- **Fixed `data.orderTotal` -> `data.order_total`** -- the
  high-value-upgrade modal silently never fired in v0.1.0 (and pre-v1
  releases) because the frontend read a camelCase key the backend
  never returned. v0.2.0 reads the actual snake_case key.
- **Fixed `data.amazonOrderId` -> dropped** -- same root cause; the
  field never landed on the frontend's `currentAmazonOrderId`
  variable, which was always blank.
- **Fixed `data.customer.entityId` -> `data.address.name`** -- legacy
  NetSuite nested shape the v0.1.0 backend never returned. Now reads
  from the flat `data.address` object the backend actually sends.
- **Fixed `i.quantity` -> `i.qty`** in line-item map functions for
  both the ship + manual-link flows. The legacy code defaulted to
  `parseInt(undefined) || 1` so every line displayed qty=1; now
  reflects the picked quantity from `sales_order_lines.quantity_picked`.

### Removed

- **`/check_cancellation` route reference** in the frontend (the
  backend handler was already gone before v0.1.0; the frontend call
  was just-failed-silently).
- **`currentAmazonOrderId` variable** in the frontend (deprecated;
  Sentry exposes no marketplace-specific order ID through the dockd
  surface).

### Tests

- 21 new SentryBackend tests (`tests/services/test_backend_sentry.py`)
  exercising every wire-level success and failure path.
- 8 new shipping-route tests (`tests/blueprints/test_shipping_routes.py`)
  exercising the integrated load / ship / manual-link / void flows
  via a `mock_backend` fixture (replaces the previous
  `mock_netsuite`).
- Total: 137 passing (108 in v0.1.0 -> 137 in v0.2.0).

### Dependencies

- **`httpx>=0.27`** added to `requirements.txt` for the SentryBackend
  client.

### Open

- `ship_attempts` SQLite table for crash-recovery idempotency
  (planned for `v0.3.0`).
- Scale-agent v2 with `/whoami` + CORS + 127.0.0.1 bind, browser
  bootstrap that fetches the per-station token (`v0.4.0`).
- Health-check polling + connectivity indicator in the operator UI
  (`v0.5.0`).

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
