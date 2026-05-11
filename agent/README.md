# Dockd scale agent

Per-station hardware bridge for the pack line. Runs on each station
laptop alongside Chrome. Handles the USB HID scale + Zebra ZPL
printer + HP LaserJet packing-slip printer, and serves the
per-station identity + Sentry bearer token to the browser at page
load.

The agent binds to **127.0.0.1 only** -- nothing off the laptop can
reach it. CORS is pinned to your dockd origin so a third-party site
an operator stumbles onto cannot script the local hardware.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/whoami` | Returns `station_id`, `station_label`, `sentry_token`, `agent_version`. The browser caches this in module-scope memory for the session. |
| GET | `/scale` | Reads weight from the USB HID scale. Returns `{weight, unit, stable}` in lbs. |
| POST | `/print` | Accepts raw ZPL bytes; sends to the Zebra (Windows UNC share). |
| POST | `/print-html` | Accepts HTML bytes; prints to the HP LaserJet via Windows `ShellExecuteW('printto')`. |
| GET | `/health` | Liveness check; reports `scale_connected` + agent version. No auth. |

## Install (Windows)

1. Install Python 3.11 (check "Add Python to PATH" in the installer).
2. Copy this entire `agent/` directory to `C:\dockd-agent\` (or any local path).
3. From a Command Prompt in that directory:
   ```
   pip install -r requirements.txt
   ```
4. Copy `agent_config.example.json` to `agent_config.json` and fill in:
   - `station_id` -- short slug (e.g. `pack-station-1`); must match an entry on the dockd Settings page so admins can attribute ships to a station.
   - `station_label` -- human-friendly name shown in the operator UI ("Pack Station 1").
   - `dockd_origin` -- the HTTPS URL of your dockd deployment (no trailing slash). CORS will be pinned to this origin.
   - `sentry_token` -- per-station bearer token issued by your Sentry-WMS admin. Treat this like an SSH key.
   - `zebra_printer` -- leave blank to auto-detect a local `\\{local-ip}\ZEBRA` share, or specify a network UNC path.
   - `hp_printer` -- Windows print queue name for the HP packing-slip printer (default `HPLASER`).
   - `scale_vendor_id` / `scale_product_id` -- USB VID/PID of your scale. The defaults match common Mettler / DYMO scales.
5. Lock down the config file. From Command Prompt:
   ```
   icacls agent_config.json /inheritance:r /grant:r "%USERNAME%:F"
   ```
   This removes inherited permissions and grants read/write only to the current user (Windows equivalent of `chmod 600`).
6. Start the agent:
   ```
   python agent.py
   ```

The full station setup walkthrough (Python install, Chrome kiosk shortcut, daily startup procedure, troubleshooting) is at `../docs/STATION_SETUP.md`.

## Config schema

```json
{
  "agent_port":        5050,
  "station_id":        "pack-station-1",
  "station_label":     "Pack Station 1",
  "dockd_origin":      "https://dockd.example.com",
  "sentry_token":      "wms_t_<...>",
  "zebra_printer":     "",
  "hp_printer":        "HPLASER",
  "scale_vendor_id":   "0x0b67",
  "scale_product_id":  "0x555e",
  "log_dir":           "logs",
  "log_max_bytes":     5242880,
  "log_backup_count":  3
}
```

## Logs

Rotating file handler writes to `logs/agent.log` (default 5 MB
files, 3 backups). Token values are not logged.

## Why no auth header anymore

Pre-v2 the agent listened on `0.0.0.0` and required an `X-Agent-Key`
header. The v2 model replaces that with two layered controls:

1. **Bind to 127.0.0.1** -- the agent's TCP socket is unreachable
   from anywhere except this laptop. A peer on the same LAN cannot
   even open a connection.
2. **CORS pinned to dockd_origin** -- the browser refuses to send a
   cross-origin request from any origin other than your dockd URL,
   so a third-party site cannot script the local hardware.

This matches the W3C "Secure Contexts" spec which treats `localhost`
as a secure origin even when fetched from an HTTPS page (no mixed-
content blocking).
