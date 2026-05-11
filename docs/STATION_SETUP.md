# Dockd station setup guide

How to deploy dockd and stand up a new pack station. Written for
anyone -- no coding experience required.

## How the system works (the simple version)

Dockd runs in **one place**: a container (Azure App Service,
Docker, Fly.io, your laptop -- anywhere that serves HTTPS). The
container talks to your upstream WMS / ERP (Sentry-WMS is the
default; the backend interface is pluggable) over HTTPS and to
ShipRush for label generation.

Each pack station laptop runs **two things**:

1. **Chrome**, opened to your dockd URL via a kiosk shortcut.
2. **The scale agent** (this repo's `agent/` directory), a tiny
   Python process that owns the USB scale + the Zebra ZPL printer
   + the HP LaserJet packing-slip printer.

The browser is the only thing that talks to the scale agent, and
the only thing that talks to dockd. When the operator clicks
**Ship**, the browser:

1. POSTs the ship request to dockd over HTTPS.
2. Receives a ZPL label in the response.
3. Forwards the ZPL bytes to `http://localhost:5050/print` -- the
   scale agent on the same laptop -- which drops them on the
   Zebra.

```
+--------------------------+      +--------------------------+
| Pack station laptop      |      | dockd container          |
|                          |      |  (Azure / Docker / etc.) |
|  Chrome                  | HTTPS|                          |
|    |                     | <--> | /api/* + /ship_order +   |
|    +--> dockd UI (HTTPS) |      | settings page + ...      |
|    |                     |      |                          |
|    +--> 127.0.0.1:5050   |      | Outbound HTTPS to:       |
|         scale agent      |      |  - Sentry-WMS (orders)   |
|         /whoami          |      |  - ShipRush (labels)     |
|         /scale           |      +--------------------------+
|         /print  (ZPL)    |
|         /print-html (HP) |
+--------------------------+
```

The agent **binds to 127.0.0.1 only**. Nothing off the laptop can
reach the scale or the printer. CORS on the agent is pinned to
your dockd URL so a third-party site cannot script the local
hardware either.

## Part 1: Deploy dockd (admin, one time)

Skip this part if dockd is already running somewhere.

### Step 1: Pick where dockd will live

Dockd is a single Flask container. Common deployment targets:

- Azure App Service for Containers (push the image, set env vars, done)
- Docker on a small VM
- `docker compose up` on a local server
- Fly.io / Railway / Render

It needs:

- Outbound HTTPS to your upstream WMS (Sentry-WMS) + ShipRush
- HTTPS inbound from every pack station's browser
- A persistent volume for `settings.json`, `users.json`,
  `shipping_history.db`, `override.db`, `label_history/`, and `logs/`

### Step 2: Set environment variables

The container reads from `.env` (or process env). At minimum:

```
SECRET_KEY=<32-byte random hex>   # python -c "import secrets; print(secrets.token_hex(32))"
SHIPRUSH_TOKEN=<your ShipRush API token>
SHIPRUSH_ENDPOINT=https://api.my.shiprush.com/shipmentservice.svc/shipment/ship
BACKEND=sentry
SENTRY_BASE_URL=https://sentry.<your-domain>.com
DOCKD_SENTRY_TOKEN=<optional, interim before scale-agent v2>
```

See `.env.example` for the full set of supported variables.

### Step 3: First boot

Start the container. It will auto-create `settings.json` and
`users.json` at the project root (chmod 600) on first boot.

Open the deployed URL in a browser. The first login is
`admin` / `admin`. You will be **forced to set a new password**
before any other endpoint responds.

### Step 4: Configure operational data

In the admin settings page (`/settings`), walk the tabs:

- **Boxes** -- add every scannable box your warehouse uses (id,
  label, dimensions, USPS / UPS / WEIGHT_THRESHOLD preference).
  CSV import supported.
- **Carrier rules** -- the high-value threshold, weight crossover,
  dim-weight cutoffs, etc. The defaults work as starting values.
- **Stations** -- one row per pack station you'll set up in
  Part 2. The `station_id` here must match what you put in each
  laptop's `agent_config.json`.
- **Shipper origin** -- the return address that goes on every
  ShipRush label.
- **ShipRush** -- your account GUIDs and the per-carrier service
  catalog.
- **Amazon methods** -- ship-method strings that trigger the
  marketplace carrier modal.
- **Override SKUs** -- items without usable barcodes (CSV import
  supported).
- **Users** -- create per-operator accounts. Admins can do
  everything; users can ship only. Passwords assigned by the admin
  start with `must_change_password=true` so the operator rotates
  on first login.
- **Secrets (.env)** -- editable interface to the env vars above.
  Writes to the container's `.env` and reloads the live process.

### Step 5: Issue per-station tokens (Sentry side)

Each pack station needs a Sentry bearer token with the
`dockd.dispatch` slug. Generate one per station in your
Sentry-WMS admin panel and keep the token handy for Part 2.

## Part 2: Set up a pack station laptop

Do these steps on every laptop that will be a pack station.

### Step 1: Install Python

- Go to <https://python.org/downloads> and download Python 3.11.
- **IMPORTANT:** check **"Add Python to PATH"** on the installer.
  Without it, nothing works.
- Verify in Command Prompt:
  ```
  python --version
  ```
  You should see `Python 3.11.x`.

### Step 2: Copy the scale agent

You can either clone the dockd repo and use the `agent/`
sub-directory directly, or copy just that folder to the laptop.

Option A -- clone:

```
cd C:\
git clone https://github.com/hightower-systems/dockd.git
```

Option B -- copy `agent/` to `C:\dockd-agent\` via USB drive.

### Step 3: Install agent dependencies

In Command Prompt, in the agent directory:

```
cd C:\dockd\agent
pip install -r requirements.txt
```

### Step 4: Fill in `agent_config.json`

Copy the example:

```
copy agent_config.example.json agent_config.json
```

Open `agent_config.json` in Notepad and set:

| Field | What to put |
|---|---|
| `station_id` | Short slug for this station (e.g. `pack-station-1`). Must match an entry on the dockd Settings page. |
| `station_label` | Human-friendly name shown in the operator UI ("Pack Station 1"). |
| `dockd_origin` | The HTTPS URL of your dockd deployment, no trailing slash. CORS pins to exactly this string. |
| `sentry_token` | The per-station Sentry bearer token you issued in Part 1 Step 5. Treat like an SSH key. |
| `zebra_printer` | Leave blank to auto-detect a local `\\{ip}\ZEBRA` share, or specify a network UNC path. |
| `hp_printer` | Windows print queue name for the HP packing-slip printer. Default `HPLASER`. |
| `scale_vendor_id` / `scale_product_id` | USB VID/PID of your scale. The defaults match common Mettler / DYMO scales; check Device Manager for your unit. |

### Step 5: Lock down `agent_config.json`

The file contains the Sentry bearer token. From Command Prompt:

```
cd C:\dockd\agent
icacls agent_config.json /inheritance:r /grant:r "%USERNAME%:F"
```

This removes inherited permissions and grants read/write only to
the current user (Windows equivalent of `chmod 600`).

### Step 6: Create a startup batch file

Right-click Desktop -> New -> Text Document. Name it
`start_agent.bat` (make sure the extension is `.bat`, not
`.bat.txt`). Right-click -> Edit. Paste:

```
@echo off
title Dockd Scale Agent
cd C:\dockd\agent
python agent.py
pause
```

Save and close.

### Step 7: Create the Chrome kiosk shortcut

Right-click Desktop -> New -> Shortcut. Location:

```
"C:\Program Files\Google\Chrome\Application\chrome.exe" --app=https://dockd.example.com
```

Replace `https://dockd.example.com` with your real dockd URL.
Name the shortcut "Dockd". The `--app` flag opens a clean Chrome
window with no tabs / address bar -- looks like a desktop app.

### Step 8: First-time test

1. Plug in the USB scale and the barcode scanner.
2. Double-click `start_agent.bat` on the Desktop. A black window
   opens; you should see "Dockd Scale Agent v2.0 -- Port 5050".
3. Double-click the "Dockd" shortcut. Chrome opens to the login
   screen.
4. Log in as an operator account (created in Part 1 Step 4).
5. If the agent is reachable, the sidebar bottom shows the
   station label ("Pack Station 1"). If not, a red banner across
   the top says **SCALE AGENT NOT RUNNING** -- go back to step 2.
6. Scan a test order, scan a box, place a package on the scale,
   click Ship. The label should print on the Zebra.

## Part 3: Daily startup procedure

In this order:

| # | Action | Who |
|---|---|---|
| 1 | Turn on the laptop | Anyone |
| 2 | Plug in USB scale + barcode scanner | Anyone |
| 3 | Double-click `start_agent.bat` | Anyone |
| 4 | Double-click the "Dockd" shortcut | Anyone |
| 5 | Log in and start working | Each operator |

The black agent window must stay open while shipping. If someone
closes it, the red **SCALE AGENT NOT RUNNING** banner appears in
Chrome and ship / scale / print calls fail until the agent
restarts.

## Part 4: Troubleshooting

**Red "SCALE AGENT NOT RUNNING" banner across the top of dockd**
- Is the black agent window open? If not, double-click
  `start_agent.bat`.
- Is it bound to the right port? The agent logs the port at
  startup; it should be `5050`. Check `agent_config.json` if
  different.
- Is `dockd_origin` in `agent_config.json` exactly the same URL
  the browser is open to? CORS will refuse a mismatch.

**Scale not reading weight**
- Is the USB cable plugged in? Try a different port.
- Is the agent running? Check the black window for errors.
- Unplug the USB cable, wait 5 seconds, plug it back in.
- Restart the agent (close the window, double-click the .bat
  again).

**Label won't print on the Zebra**
- Is the Zebra powered on?
- Can you print a test page from Windows directly?
- Check the agent log (`agent/logs/agent.log`) for the
  "Print command failed" line; it will include the Windows error
  code.

**Packing slip won't print on the HP**
- Is the HP queue name in `agent_config.json` correct? In Windows,
  Control Panel -> Devices and Printers shows the queue name.
- Is the HP connected to the network?
- The agent's fallback prints to the Windows default printer if
  the named queue fails -- check that printer first.

**"401 invalid_token" or upstream auth error from dockd**
- The `sentry_token` in `agent_config.json` is wrong, expired, or
  revoked. Get a new token from your Sentry-WMS admin, paste it
  into the config, restart the agent.

**"Order backend not configured" on order load**
- The dockd container is not pointed at Sentry. Admin: set
  `BACKEND=sentry` + `SENTRY_BASE_URL=...` in the container env,
  restart the container.

**Browser says "Site can't be reached"**
- Is dockd up? Visit `https://your-dockd-url/health` directly --
  it should return `{"status": "ok"}`.
- Is your laptop on a network that can reach dockd?

**Can't log in / forgot password**
- Operator: ask an admin to reset your password from the Settings
  page (Users tab -> reset password).
- Admin lockout: see "Emergency admin reset" below.

**Mid-ship crash: label printed but order didn't confirm upstream**
- The dockd UI surfaces a clear error including the tracking
  number ("Label printed but the upstream system did not
  confirm"). Note the tracking and either:
  - Retry the void from the ship's success row, or
  - Manually link the tracking via the Link Tracking modal.

## Part 5: Admin operations

### Updating dockd

Dockd updates are container-side. Rebuild and redeploy the
container; pack stations pick up the new version on their next
browser refresh. No station-side work required.

### Rotating a station's Sentry token

1. Issue a new token in your Sentry-WMS admin panel (with the
   same `dockd.dispatch` slug).
2. On the laptop, edit `agent_config.json`, replace
   `sentry_token`.
3. Restart the agent (close + reopen `start_agent.bat`).
4. Revoke the old token in Sentry-WMS.

### Adding a new pack station

1. Add the new station's `station_id` + `station_label` on the
   dockd Settings page -> Stations tab.
2. Issue a Sentry token for it (Sentry admin).
3. Repeat **Part 2** on the new laptop.

### Emergency admin reset

If every admin loses their password, exec into the dockd container
and delete `users.json`. The next boot recreates it with
`admin` / `admin` + the forced-password-change flag, just like a
fresh install.

```
docker exec -it <dockd-container-id> rm /app/users.json
docker restart <dockd-container-id>
```

You will lose any non-admin user records. Re-add them after
logging in.

## Quick reference card

Print this and tape it to the wall near each pack station.

| Action | How |
|---|---|
| Start the agent | Desktop -> double-click `start_agent.bat` |
| Open dockd | Desktop -> double-click "Dockd" shortcut |
| Log in | Operator account assigned by the admin |
| Scan an order | Type or scan the order number in the main input |
| Ship | Scan a box -> place on scale -> click Ship |
| Reprint last label | Sidebar -> REPRINT LAST |
| Reprint earlier label | Sidebar -> REPRINT -> type order number |
| Void a label | Sidebar -> VOID -> type order number |
| Manually link tracking | Sidebar -> LINK |
| Red banner on top | Restart the agent: close the black window, double-click `start_agent.bat` |
| Scale not reading | Replug USB, restart the agent |
| Label won't print | Power-cycle the Zebra, check `agent/logs/agent.log` |
| Can't log in | Admin resets in Settings -> Users tab |

Questions? Open an issue at <https://github.com/hightower-systems/dockd/issues>.
