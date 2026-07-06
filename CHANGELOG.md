# Changelog

All notable changes to Dockd will be documented in this file.

## [v1.1.0] - 2026-07-06

Durability release. Dockd's operational state moves off the ephemeral,
SMB-backed SQLite files that corrupted four times in six weeks (a
Container Apps revision swap briefly runs two unlocked writers over Azure
Files) and onto the Postgres instance Sentry-WMS already runs on.
Settings move with it, out of the JSON file. And with Postgres in place,
Dockd stops keeping its own user table entirely: Sentry becomes the single
sign-on for all logins, so the pack station shares one credential set with
the rest of the platform.

### Added -- Postgres data layer

- **Alembic migrations** (`alembic/`, `alembic.ini`). `alembic upgrade
  head` runs at container boot before the app starts, so the schema is
  versioned instead of created ad hoc. A `postgres:16` service in
  `docker-compose.yml` matches prod for local dev.
- **`0001_baseline`** creates `ship_history`, `ship_attempts`, and
  `override_log` in Postgres (BIGSERIAL keys, TIMESTAMPTZ timestamps, the
  ship_attempts CHECK + UNIQUE constraints preserved).
  **`0003_dockd_settings`** adds a `dockd_settings` key/JSONB table.
- **`DATABASE_URL`** (a libpq DSN) is now required at runtime; the pool
  and the boot migration both fail loud if it is unset.

### Changed -- storage

- **`ship_history` / `ship_attempts` / `override_log`** are psycopg2 over a
  shared `ThreadedConnectionPool`. The crash-recovery idempotency contract
  is intact: a duplicate idempotency key still raises (now psycopg2
  `UniqueViolation`, a subclass of the old `IntegrityError`).
- **Operational settings** live in `dockd_settings` -- one JSONB row per
  top-level key, merged over the defaults, read fresh per request. The
  `SettingsStore` interface is unchanged, so every service that reads
  settings is untouched. Secrets that were inline in the JSON stay inline
  in the table, still hidden from non-admin callers by `public_subset`.

### Changed -- authentication (Sentry SSO)

- **Login verifies against Sentry.** `POST /login` proxies credentials to
  Sentry's `/api/auth/login`, maps the role (Sentry `ADMIN`/`USER` to
  dockd `admin`/`user`), and stores only `{name, role}` in the session.
  Sentry owns the password, the (IP, username) lockout (Dockd forwards the
  operator's real IP), and forced-password-change (Dockd blocks login with
  a "change it in Sentry" message instead of carrying its own rotation).
  Ship / void backend calls are unchanged -- they keep using the service
  `X-WMS-Token`.

### Removed

- **Dockd's local user store.** No more `users` table, bcrypt hashes,
  password policy, login-attempt lockout, `/api/users` admin CRUD, the
  settings-page user management, or the forced-password-change overlay --
  identity is Sentry's now.
- The SMB SQLite workarounds (`SQLITE_NOLOCK`, `SQLITE_JOURNAL_MODE`) and
  the JSON settings/users files, along with their chmod-600 handling.

## [v0.7.0] - 2026-05-14

"International shipping, dockd side" release. Dockd v0.6.x assumed
every destination was a US ZIP and hardcoded `<Country>US</Country>`
in the ShipRush XML; this release plumbs destination country through
end to end, builds the customs declaration block ShipRush requires
for non-US labels, gates banned destinations before any carrier
call, and adds a per-ship adult-signature toggle that works for all
three carriers. The complementary catalog work (HS codes, country
of origin, unit weight, unit value on the Sentry item master) is a
v1.11 Sentry release and is tracked separately; dockd is ready to
consume that data the moment Sentry surfaces it.

### Added -- International ship path

- **`CustomsData` dataclass** (`app/services/backend/__init__.py`).
  Frozen, all-optional fields: `description`, `hs_code`,
  `country_of_origin`, `unit_weight_oz`, `unit_value`. From-dict
  tolerantly normalizes (uppercase ISO 3166 alpha-2 country,
  numerics coerced or dropped on garbage, negatives dropped). Sentry
  emits this nested under each `OrderItem` only when the destination
  is non-US so domestic payloads stay byte-for-byte unchanged.
- **`OrderItem.customs: Optional[CustomsData]`** carries the per-line
  declaration. **`OrderData.currency`** (default `"USD"`, normalized
  to 3-letter uppercase ISO 4217) and **`OrderData.duties_paid_by`**
  (`"sender"` / `"recipient"`, default `None`) cover order-level
  customs context.
- **ShipRush XML: country passthrough.** `<DeliveryAddress><Country>`
  now reads from the order's address (previously hardcoded `US`).
  Helper `_normalize_country` is the single source of truth so the
  banned-country gate and the XML emit logic cannot drift.
- **ShipRush XML: `<Commodities>` block.** Emitted only when the
  destination is non-US. One `<Commodity>` per line item with
  `<Description>`, `<HarmonizedCode>`, `<CountryOfManufacture>`,
  `<Quantity>`, `<UnitWeight>` (ounces converted to pounds, the
  ShipRush convention), and `<UnitValue><Amount><Currency>`. Every
  upstream-supplied string is `xml.sax.saxutils.escape`d; numerics
  are formatted via `:f` so a malformed `qty` cannot break out of
  its tag.
- **ShipRush XML: customs envelope.** `<CustomsValue>` sums the
  per-line declared values. `<IncotermsCode>` resolves from
  `duties_paid_by`: `"sender"` -> `DDP` (sender pays duty),
  anything else -> `DAP` (recipient pays, the more common default).
  `<ContentType>Merchandise</ContentType>` emitted alongside.
- **Service catalog ready for international slots.** No code change
  needed to add `UPS_WORLDWIDE_SAVER`, `FEDEX_INTL_PRIORITY`,
  `USPS_PRIORITY_INTL`, etc.; admins paste them into the existing
  ShipRush services table in Settings, and `_resolve_carrier` picks
  them up via substring match on the order's ship-method string or
  via the operator carrier override.

### Added -- Banned-destination hard gate

- **`SettingsStore.is_country_banned(country)`** with case- and
  whitespace-tolerant normalization. Compares against
  `settings.international.banned_countries`, seeded on first boot
  with the OFAC comprehensive-sanctions defaults (`CU`, `IR`, `KP`,
  `SY`). Admins tune the list through the new Settings tab.
- **`ShippingService.ship_order` blocks the banned destination
  before any carrier or label work.** Runs immediately after
  `backend.get_order`; if the gate fires, ShipRush is never called,
  no ZPL is generated, no `ship_attempts` row is written, and the
  operator gets a compliance-flavored error message pointing them
  at the Settings tab. Server-side enforcement is the source of
  truth; the operator UI carries no override path.
- **Logging:** the block writes a `WARNING` line tagged with SO
  number + country code for audit-trail purposes (no PII / no token
  material in the line, redaction-filter-safe).

### Added -- Per-ship adult-signature toggle

- **Sidebar button** in `app/templates/index.html` (left rail,
  alongside CLEAR / VOID / LINK / REPRINT). Toggles `ADULT SIG: ON`
  / `OFF`; when armed the button paints red with bold white text
  so the operator cannot miss the upcharge state.
- **Wire shape.** Boolean rides on `/ship_order` request body as
  `adult_signature`. Frontend includes it in both ship-payload
  assembly sites (standard ship + OB-dim ship). `ShipRushClient.
  generate_label` accepts `adult_signature=False` kwarg and emits
  the carrier-correct `<DCISType>` inside `<Package>`:
  - **FedEx orders (account_key `FEDEX`) -> `F4`** (per ShipRush
    XSD `TDCIS` enum, "Adult Signature Required" on FedEx).
  - **UPS and USPS orders -> `ADS`** (same enum, the carrier-
    agnostic adult-signature value).
- **Auto-disarms on ship success** before the success modal
  renders, so order N+1 starts at OFF. Not persisted in
  localStorage; a page refresh also clears it.

### Added -- Operator UI surfacing

- **`INTL` pill in the order header** appears next to the order ID
  when the destination country is non-US. ISO country code appended
  to the address line (`123 Foo St, City, ON M5V (CA)`) as a
  secondary visual cue.
- **Settings UI `International` tab** (admin-only): enable toggle,
  default duty payer (DAP / DDP) dropdown, four tax-ID inputs
  (EIN, EORI, IOSS, UK VAT) marked `autocomplete="off"`, and a
  comma-separated banned-country input with client-side
  validation (dedupe, uppercase, 2-letter regex filter so a typo
  cannot accidentally lock down a region).

### Added -- Carrier-engine + tracking-inference intl awareness

- **`CarrierEngine.check_carrier_conflict` accepts `dest_country=`
  kwarg** and returns `None` (skips rural-ZIP / PO Box / USPS-vs-
  UPS swap heuristics) for non-US destinations. The legacy carrier
  picked upstream is authoritative; dockd does not second-guess
  international routing.
- **`_carrier_from_tracking` recognizes international prefixes** on
  the manual-link path: DHL Express (10 digits), DHL eCommerce
  (`GM` prefix), USPS-handoff S10 alphanumeric (`CP` / `LM` / `RA`
  / etc.), Royal Mail S10 (`LX` / `LE` / `LF`), Canada Post S10
  (`EA` / `EE` / `EC`). Unknown S10 prefixes fall through to
  `'INTL'` rather than `'UNKNOWN'` so the audit row still records a
  shape hint.

### Added -- Storage

- **`ship_history` schema** gains four columns:
  `destination_country`, `customs_value`, `customs_currency`,
  `hs_codes`. Idempotent ALTER block for upgrading existing DBs
  (SQLite has no `ADD COLUMN IF NOT EXISTS`, so the migration
  inspects `PRAGMA table_info` and adds only when absent). Columns
  remain `NULL` for domestic shipments to keep the row width
  light; international rows record the destination ISO code, total
  declared customs value, currency code, and a comma-separated HS
  code list for downstream audit.

### Security

- **`SettingsStore.public_subset()` does not leak tax IDs** (EIN /
  EORI / IOSS / UK VAT) or the banned-country list. Operator UI
  only gets `international_enabled` so it can render the pill +
  toggle button without admin scope.
- **All upstream-supplied customs strings (description, HS code,
  country of origin) are XML-escaped** in the ShipRush XML
  builder. Sentry is the trust boundary in principle, but defense
  in depth is cheap.
- **Banned-destination enforcement is server-side only.** The
  operator UI has no toggle, no override, and no client-side bypass
  path; the gate runs after `backend.get_order` so a direct POST to
  `/ship_order` with a known-banned SO is still rejected.

### Fixed -- Settings UI: in-progress edits no longer lost

- Clicking **"+ Add box"** (or "+ Add FedEx box", or "+ Add
  station") used to wipe any edits the operator had typed into
  existing rows. The push to `settings.boxes` happened before a DOM
  read, so `renderBoxes()` rebuilt the table from the stale array.
  Same bug fired when clicking "remove" on any row. Fix calls the
  existing `read*Table()` helper before the mutation in all six
  sites (three `add*Row` functions + three inline remove handlers).
  Operators can now add multiple rows in a single editing session
  without losing typed-but-unsaved values.

### Added -- Regression tests

- **`tests/services/test_international.py`** (39 tests). Coverage:
  `CustomsData` round-trip / normalization / negative-rejection /
  garbage-tolerance; `OrderItem`+`OrderData` round-trip with
  customs and currency; `_normalize_country` /
  `_is_international` / `_build_customs_items` helpers;
  `_build_shiprush_payload` country + currency + customs items
  propagation; ShipRush XML emission for domestic (no commodities)
  and international (commodities + CustomsValue + IncotermsCode);
  XML injection escaping; customs-value summation;
  `_carrier_from_tracking` international prefixes (DHL, USPS S10,
  Royal Mail, Canada Post, unknown-S10 fallthrough, plus the
  regression check that US prefixes still resolve); `SettingsStore.
  is_country_banned` (defaults seeded, case-insensitive, runtime
  update); `public_subset` secrecy for tax IDs + banned list.
- **`tests/services/test_international.py::TestAdultSignatureDcis
  Emission`** (4 tests). Off case emits no `<DCISType>` tag; UPS
  service emits `ADS`; USPS service emits `ADS`; FedEx service
  emits `F4`.
- **`tests/blueprints/test_shipping_routes.py::TestBannedDestination
  Gate`** (2 tests). KP destination blocks the ship and does NOT
  call ShipRush or `confirm_shipped`; CA destination passes through
  the gate normally and completes the ship.
- **`tests/blueprints/test_index_template.py::TestAdultSignature
  Toggle`** (5 tests). Sidebar button present in markup; state
  variable initialized false; payload carries `adult_signature` at
  both assembly sites; success branch resets to false; companion
  INTL-pill marker present.
- **`tests/blueprints/test_settings_routes.py::TestDynamicRowEdit
  Preservation`** (4 tests). Each of the three `add*Row` functions
  calls the matching `read*Table` before the array mutation; the
  inline remove handlers do the same.

### Tests

- 245 passing (191 -> 245). Net +54 across the new modules.
  Highlights:
  - 39 `test_international` (new module)
  - 11 `test_index_template` (5 new for adult-signature, was 6)
  - 25 `test_settings_routes` (4 new for dynamic-row preservation)
  - 19 `test_shipping_routes` (2 new for banned-destination gate)

### Operator notes

- **International orders will fail to ship until Sentry surfaces
  per-item customs data.** That's the next release on the Sentry
  side; dockd v0.7.0 is the receiving end of that wire. For
  international orders today, route them out-of-band (label printed
  via the carrier's web UI) and use dockd's manual-link path to
  attach the tracking number after the fact.
- **Banned-destination defaults are conservative.** If your
  compliance posture requires more (e.g., specific OFAC SDN-list
  enforcement, EAR end-user screening), treat the seeded `CU` /
  `IR` / `KP` / `SY` list as a starting point and extend in the
  Settings tab. Dockd does NOT perform OFAC SDN screening on the
  consignee name; that is a separate compliance layer.
- **Adult-signature toggle is per-ship by design.** Operators must
  re-arm the toggle for every order that needs it. The auto-
  disarm prevents the most common workflow error (forgetting to
  turn it off after the high-value order ships).

### Open / deferred

- **Sentry item-master extension** (`hs_code`, `country_of_origin`,
  `unit_weight_oz`, `unit_value`) ships in Sentry v1.11; dockd
  v0.7.0 is wire-ready for it.
- **CN22 / CN23 customs forms** (USPS international small-parcel
  paper forms) and **commercial invoice PDFs** for high-value
  shipments are deferred to v0.8.0. ShipRush returns the customs
  form image embedded in the label for most services, which covers
  the common case; the standalone PDF flow is for shipments
  >USD 2500 declared value or destinations that require a separate
  signed document.
- **DDP/DDU per-order toggle** (override the settings default at
  ship time) deferred. v0.7.0 uses the settings-level default for
  every ship.
- **Metric weight thresholds + dim-weight formula** for non-US
  carriers that bill on kg/cm. Today every dim-weight check uses
  inches + pounds; international carriers in metric markets get
  approximately-right answers via on-the-wire conversion.
- **OFAC SDN denied-party screening** on the consignee name (vs.
  the country-level block already in place). External vendor
  call, deferred.
- **Hazardous-goods / lithium-battery declaration** XML fields.
  ShipRush supports these (`<ContainsBattery>`,
  `<DangerousGoods>`); dockd does not surface them yet. Relevant
  if the catalog contains battery-driven SKUs (fish finders,
  headlamps).
- **International returns labels.** Deferred to v1.0+.

## [v0.6.1] - 2026-05-12

"UPC scan matching restored" patch. Surfaced during the first live
go-live: operators were getting "INVALID ITEM" on every UPC scan,
even though the order's items came back from Sentry with their UPCs
populated. Root cause was a pre-existing gap in dockd's scan
verifier -- it compared the scan against `i.sku`, `i.item.refName`,
and an unused-since-NetSuite `i.valid_scans` array, but never
`i.upc`. Fix is two lines + a render polish.

### Fixed

- **`verifyItem` and `verifyLinkItem` (`index.html`) now match
  scans against `i.upc`** in addition to SKU, display name, and the
  legacy `valid_scans` array. UPC values arrive from Sentry's GET
  `/api/v1/dockd/orders/<so>` response on every line and were
  already populated -- they just weren't being checked. One added
  line in each function.
- **`renderItems` and the link-modal item renderer** now show
  `<sku>  •  UPC <upc>` inline under each item so operators can
  visually confirm the scan target. UPC rendered in monospace.
  When the order's item row has no UPC populated upstream, the UPC
  segment is omitted (no empty bullet).

### Added -- Regression tests

- **`tests/blueprints/test_index_template.py::TestUpcScanMatching`**
  (3 tests):
  - `verifyItem` source contains `i.upc`
  - `verifyLinkItem` source contains `i.upc`
  - Rendered `/` includes the UPC label + render-path variable

### Tests

- 191 passing (188 -> 191).

### Operator note

If you're still seeing "INVALID ITEM" after upgrading, check that
the item's `upc` column is populated in Sentry's `items` table.
SQL to spot-check items on a specific order:

```sql
SELECT i.sku, i.upc, i.item_name
  FROM items i
  JOIN sales_order_lines sol ON sol.item_id = i.item_id
  JOIN sales_orders so ON so.so_id = sol.so_id
 WHERE so.so_number = '<so_number>';
```

If `upc` is NULL for some rows, load via Sentry's admin Imports
page (`POST /api/admin/import/items`) with a CSV containing
`sku,item_name,upc`. Each item only needs to be loaded once;
subsequent orders for that SKU pick up the UPC automatically.

## [v0.6.0] - 2026-05-12

"First end-to-end ship against a real Sentry instance" release. The
integration test ran through a complete ship + void roundtrip
against a fresh Sentry-WMS v1.10.1 deployment and a real ShipRush
account: dockd loaded `SO-2026-001` from Sentry, asked the
CarrierEngine to resolve box dims, generated a real USPS Ground
Advantage label (tracking `9434...`), wrote the ship to Sentry
(audit_log_id 3, item_fulfillments row 1), then voided -- ShipRush
refunded the label and Sentry reverted the SO to PACKED
(audit_log_id 4). Two bugs surfaced from that test are fixed here.

### Fixed

- **`ShipRushClient._resolve_carrier` returned `None` under empty
  `shiprush_services`** (#bug-1). With the open-source neutral
  default of `shiprush_services={}`, every ship attempt raised
  `TypeError: cannot unpack non-iterable NoneType object` at the
  caller's 4-tuple unpack in `generate_label`. Refactored to a
  `_resolve_or_fallback(*slots)` helper plus a class-level
  `_RESOLVE_FALLBACK = ('1', 'UPS', '03', False)` so the unpack
  always succeeds. ShipRush may still reject the request (unknown
  account, bad service code) but with a structured ShipRush error,
  not a Python 500.
- **Sidebar SETTINGS + EXIT buttons invisible after JS-driven
  login** (#bug-2). The pre-v0.6.0 template gated both buttons
  behind `{% if current_user.role == 'admin' %}`, a server-side
  Jinja conditional that runs at page-render time. The JS-driven
  login attached a session AFTER the HTML was already rendered, so
  the admin buttons were never in the DOM and only a full page
  reload surfaced them. Fix renders both buttons unconditionally
  with the `admin-only` class and `display:none`; a new JS helper
  `applyAdminVisibility(currentUser)` toggles them on. Called from
  three entry points: the auto-login bootstrap (server has
  session), the login success branch, and the post-password-change
  branch. No page reload needed.

### Changed -- Test isolation

- **`tests/conftest.py`** clears the integration-test env vars
  (`BACKEND`, `SENTRY_BASE_URL`, `DOCKD_SENTRY_TOKEN`,
  `DOCKD_RETRY_PENDING_ON_BOOT`, `DOCKD_RETRY_POLL_INTERVAL`)
  before importing `app`, so a developer running tests with a
  populated dev `.env` does not accidentally wire a real
  SentryBackend into the test fixture. Set to empty strings (not
  `pop`) so `python-dotenv`'s only-set-if-unset semantics do not
  re-import the dev values during `create_app()`.

### Added -- Regression tests

- **`tests/services/test_shiprush_resolve.py`** (6 tests):
  - 14 ship-method strings (every branch in `_resolve_carrier`)
    each return a 4-tuple under empty `shiprush_services`, never
    `None`.
  - Carrier overrides (`UPS`, `USPS`, `FEDEX_ONE_RATE_2DAY`) also
    fall back when their slots are absent.
  - When the catalog IS populated, the right slot wins (USPS
    Ground Advantage, UPS override, FedEx 2 Day one-rate, plus
    secondary-fallback chain for Priority -> Ground Advantage).
- **`tests/blueprints/test_index_template.py`** (3 tests):
  - Unauthenticated GET `/` ships SETTINGS + EXIT in the HTML with
    `admin-only` class + `display:none`.
  - `applyAdminVisibility` helper text appears, called from the
    auto-login block.
  - Authenticated GET `/` still includes the buttons.

### Tests

- 188 passing (179 -> 188). Net +9 from the two regression
  modules. Total breakdown:
  - 14 `test_auth`
  - 12 `test_index_template` (3 new)
  - 21 `test_settings_routes`
  - 17 `test_shipping_routes`
  - 9 `test_backend_health`
  - 21 `test_backend_sentry`
  - 36 `test_carrier`
  - 7 `test_label_cache`
  - 9 `test_logging_redaction`
  - 17 `test_settings`
  - 11 `test_ship_attempts`
  - 5 `test_ship_retry`
  - 6 `test_shiprush_resolve` (new)
  - 8 `test_security_regressions`

### Integration test data

Full step-by-step replay (Sentry seed + token issuance + dockd
ship + void) lives in commit history. Headline numbers from the
test:

| Layer | Evidence |
|---|---|
| Sentry audit_log | `SHIP` (log 3) -> `SHIP_VOID` (log 4), both attributed to admin, hash-chained. |
| Sentry sales_orders | SO-2026-001: PACKED -> SHIPPED -> PACKED (tracking + carrier nulled on void). |
| Sentry item_fulfillments | Row 1 created, void columns populated on reversal. |
| dockd ship_history | One row with sentry_fulfillment_id=1, sentry_audit_log_id=3, voided_at populated, void_reason captured, station_label='Pack Station Test'. |
| dockd ship_attempts | Two rows, both status=success, UUID4 idempotency keys, one per operation. |
| ShipRush | Real USPS label generated, then voided. Zero net carrier charge. |

### Open

- **Address-validation handling under real carrier checks**: UPS
  and FedEx reject / auto-correct addresses that don't match their
  databases. dockd surfaces ShipRush's error string verbatim today
  (e.g. "Address was corrected by the carrier..."); a future
  release could expose the corrected address so the operator can
  accept the change in one click. Out of v0.6.0 scope.
- **Integration test under a real Sentry instance, scripted**
  (`v1.0.0`).

## [v0.5.0] - 2026-05-11

"Observability + security hardening" release. The operator UI gets a
connectivity dot polling a new `/api/health/backend` endpoint; the
log pipeline gets a `RedactionFilter` that scrubs `wms_t_*` bearer
tokens and `Authorization: Bearer` strings out of every record;
ship_attempts gains an optional periodic-retry daemon so a
transient network blip recovering minutes after the original ship
doesn't have to wait for a dockd restart; and a top-level
`tests/test_security_regressions.py` suite codifies the threat-
model invariants that have to hold across every release.

### Added -- Backend health monitor

- **`app/services/backend_health.py`** -- `BackendHealth`
  read-through-with-lock cache. The first
  `/api/health/backend` poll after the cache TTL (30s default)
  triggers a real `backend.health()` call; concurrent polls share
  the result so five stations polling in parallel produce one
  upstream call, not five. Five states:
  - `ok` -- last probe succeeded within the TTL
  - `degraded` -- 1 or 2 consecutive failures (within
    `failure_threshold`)
  - `down` -- failures >= `failure_threshold` (3 by default)
  - `not_configured` -- no backend wired (`BACKEND` env empty)
  - `unknown` -- no probe has run yet
- **`GET /api/health/backend`** (login-required) returns the
  snapshot: state, consecutive_failures, last_success_seconds_ago,
  last_probe_seconds_ago, last_error, cache_ttl_seconds,
  failure_threshold. Cheap to call; safe to poll every 30s from
  every station.
- **Sidebar connectivity dot** in `index.html`: 9px colored circle
  next to the station label, polls every 30s, click for the
  details modal showing the full state snapshot.

### Added -- Log redaction

- **`RedactionFilter`** in `app/logging_config.py`. Scrubs three
  patterns from every record before the formatter sees it:
  - `wms_t_[A-Za-z0-9_\-]{6,}` -> `wms_t_<REDACTED>`
  - `Authorization: Bearer <value>` -> `Authorization: Bearer <REDACTED>` (case-insensitive)
  - `X-Sentry-Token: <value>` / `X-WMS-Token: <value>` -> header
    name kept, value replaced (case-insensitive)
- Filter attached to both the console handler and the rotating
  file handler so the stream-vs-file paths cannot drift.
- Operates on the rendered message (post-format-arg
  interpolation), so a `logger.info("Token=%s", secret)` call
  scrubs the substituted value, not just the literal `%s`.

### Added -- Periodic in-process retry

- **Daemon thread** that runs
  `ShippingService.retry_recoverable_attempts()` every
  `DOCKD_RETRY_POLL_INTERVAL` seconds while dockd is up.
  Defaults to **off** (interval 0); production deployments set
  the env to 300 (5 minutes) or so. Complements the boot-time
  retry from v0.4.0: covers the case where a transient network
  blip recovers a few minutes after the original ship and the
  process never restarts.
- Marked `daemon=True` so a clean process exit doesn't hang on
  the thread.
- Each tick re-reads `app.shipping_service` so a future config
  reload that swaps the backend would be picked up on the next
  iteration without restart.

### Added -- Security regression suite

- **`tests/test_security_regressions.py`** (8 cross-cutting
  tests) codifies the threat-model invariants:
  1. `wms_t_*` tokens, bearer strings, and `X-Sentry-Token`
     values never appear in log output after the RedactionFilter.
  2. `SentryBackend.__init__` has no `verify` parameter and the
     source contains exactly `verify=True` with no env-driven
     escape hatch.
  3. `GET /api/users` never returns the `password_hash` field;
     no string starting with `scrypt:` lands in the response.
  4. `GET /api/settings/secrets/presence` returns booleans only,
     never actual values.
  5. `settings.json` is `chmod 600` after a PATCH; `users.json`
     is `chmod 600` after a password-set.
  6. `SentryBackend` default timeout is finite and bounded
     (0 < timeout <= 60).
- The suite lives at the test root (`tests/`) so it shows up
  separately from per-module unit tests in CI output.

### Tests

- 26 new tests:
  - 9 `BackendHealth` (states, caching, recovery, exception
    handling, route integration, auth gating)
  - 9 `RedactionFilter` (scrub helper + filter wiring +
    pass-through behavior)
  - 8 cross-cutting security regressions
- Total: 179 passing (153 -> 179).

### Open

- **Integration test against a real Sentry instance**
  (`v1.0.0`).
- **Token rotation UI** (admin-facing button in the Stations tab
  to mark a station's token rotated). Deferred -- the rotation
  itself happens on the Sentry side; dockd just gets the new
  token in `agent_config.json`.
- **Health-monitor history** for the admin (sparkline of
  recent state transitions). Out of scope for v0.5.0.

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
