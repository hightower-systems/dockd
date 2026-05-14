"""Default operational settings (open-source distribution).

On first boot the SettingsStore writes this dict to `settings.json`
and the UsersStore writes `users.json`. Both are then editable
through the admin settings UI; this module is consulted only when no
file exists yet.

Defaults are deliberately blank / neutral so the repo can ship as
open source without leaking any one deployment's data. Operators add
their own boxes, stations, accounts, and shipper origin after first
login.

The one exception is `carrier_rules` -- weight crossovers, dim-weight
cutoffs, etc. -- which are generic shipping physics and have to be
non-zero for the carrier engine to function out of the box. Adjust
through the UI as needed.
"""

SETTINGS_SCHEMA_VERSION = 1


DEFAULT_SETTINGS = {
    "schema_version": SETTINGS_SCHEMA_VERSION,

    # High-value carrier-upgrade prompt threshold ($). 0 disables the
    # modal until an admin sets a non-zero value.
    "high_value_threshold": 0,

    # Local label cache lifetime for reprint/void (hours).
    "label_max_age_hours": 8,

    # Shipper origin used in every ShipRush label. Blank until the
    # admin fills it in via the settings UI; ShipRush will reject the
    # label if it's still blank when a ship runs, which is the
    # intended fail-loud behavior on an unconfigured install.
    "shipper_origin": {
        "company": "",
        "address1": "",
        "address2": "",
        "city": "",
        "state": "",
        "postal_code": "",
        "country": "US",
        "phone": "",
    },

    # Fallback phone when the order has no delivery phone.
    "fallback_customer_phone": "",

    # Warehouse-defined boxes (scannable codes). Operator must add
    # boxes in the settings UI before they can be scanned at pack time.
    "boxes": [],

    # FedEx One Rate flat-rate boxes. Same -- empty until configured.
    "fedex_boxes": [],
    "fedex_one_rate_order": [],

    # Carrier optimization knobs. Generic starting values that let
    # the engine produce sensible suggestions; tune via the UI.
    "carrier_rules": {
        "weight_crossover_lb": 3.0,
        "dim_weight_warn": 10,
        "dim_weight_override": 15,
        "longest_side_max_in": 30,
        "max_weight_usps_lb": 15,
        "rural_ca_shipping_max": 5.0,
        "usps_to_ups_ca_shipping_min": 5.0,
    },

    # User-facing reason text for each optimization branch. Generic
    # copy; admin can rewrite for their voice through the UI.
    "carrier_reasons": {
        "USPS_BOX":     "Small box - USPS is cheaper",
        "UPS_BOX":      "Large/oversized box - UPS is cheaper",
        "WEIGHT_USPS":  "Under 3 lb - USPS is cheaper for this box size",
        "WEIGHT_UPS":   "Over 3 lb - UPS is cheaper for this box size",
        "OB_USPS":      "Custom box dims favor USPS",
        "OB_UPS":       "Custom box dims favor UPS (high dim-weight or long side)",
        "DIM_OVERRIDE": "Package exceeds USPS size/weight limits - UPS recommended",
        "RURAL_DAS":    "Rural surcharge area ({tier}) - USPS has no area surcharge",
    },

    # Carrier method IDs used when overriding the order's ship method.
    # Empty by default; populated by the operator with backend-specific
    # IDs once a real ERP backend is wired.
    "carrier_methods": {},

    # ShipRush account GUIDs and service catalog. Empty by default;
    # the deploying admin pastes their per-account values in.
    "shiprush_accounts": {},
    "shiprush_services": {},

    # Lowercased ship-method strings that trigger the "marketplace
    # picked your carrier" prompt. Empty by default.
    "amazon_methods": [],

    # Pack stations. Empty until the admin defines them in the
    # settings UI.
    "stations": [],
    "default_station_id": "",

    # SKUs operators may scan-override.
    "override_exception_skus": [],

    # International shipping (v0.7.0). Disabled by default so the
    # repo ships as a domestic-only tool until an admin opts in. When
    # enabled, dockd will route orders with a non-US destination
    # through ShipRush's customs path. The four tax-ID fields appear
    # on outgoing customs declarations and are required by certain
    # destination countries (EORI for EU, IOSS for low-value EU,
    # UK VAT for UK <=GBP135). `default_duty_payer` drives the
    # ShipRush <IncotermsCode>: 'recipient' is DDU (customer pays
    # duty at delivery), 'sender' is DDP (shipper pre-pays).
    # `banned_countries` is a hard-block allow-deny list keyed on
    # ISO 3166 alpha-2; OFAC comprehensive-sanctions defaults are
    # seeded so a fresh install doesn't accidentally ship to a
    # sanctioned destination. Operators tune via the Settings UI.
    "international": {
        "enabled": False,
        "default_duty_payer": "recipient",
        "shipper_tax_ids": {
            "ein": "",
            "eori": "",
            "ioss": "",
            "vat_uk": "",
        },
        "banned_countries": ["CU", "IR", "KP", "SY"],
    },
}


# Bootstrap user spec. UsersStore hashes the plain password on first
# boot and writes the result to users.json. The
# `must_change_password` flag forces the operator to set a new
# password before any other endpoint will respond.
DEFAULT_USERS_BOOTSTRAP = {
    "schema_version": SETTINGS_SCHEMA_VERSION,
    "users": [
        {
            "username": "admin",
            "role": "admin",
            "password": "admin",
            "must_change_password": True,
        },
    ],
}


VALID_ROLES = ("admin", "user")
VALID_BOX_PREFERENCES = ("USPS", "UPS", "WEIGHT_THRESHOLD")
