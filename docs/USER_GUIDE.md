# ClusterOne — User Guide

**One Platform. Every Server.**

ClusterOne is a Windows desktop app for discovering servers on your network and
managing their BMC / BIOS / HGX firmware lifecycle across multiple OEMs.

## Install

1. Run `ClusterOne_Setup_<version>.exe` and follow the prompts.
2. Launch ClusterOne from the Start menu (or the desktop shortcut).
3. On first launch ClusterOne creates `%LOCALAPPDATA%\ClusterOne` for its
   database, firmware blobs, and logs.

## First-run tour

The app opens on **Dashboard**, which after first discovery shows live counts
(Total / Online / Needs Update / Failed), firmware-compliance bars, and
recent activity. The graphite **status ribbon** at the top is the
mission-control summary that's visible everywhere.

The left **nav rail** has six pages: Dashboard, Inventory, Updates, Firmware
Library, Activity, Settings. Press **Ctrl+K** anywhere to open the **command
palette** for fast navigation.

## Discover servers

1. Go to **Inventory** → click **+ Discover** (top right) or use the
   empty-state CTA.
2. Enter a range — any of:
   * `10.10.10.0/24` (CIDR)
   * `10.10.10.1-254` (shorthand range)
   * `10.10.10.1-10.10.10.254` (full range)
   * `127.0.0.1:8000-8007` (port range — for the dev simulator)
   * `10.0.0.5, 10.0.0.6` (comma-separated)
3. Enter BMC credentials (or leave blank for the simulator).
4. Hit **Start**. Hosts stream into the inventory live as they're identified.

Each row is **selectable** via the checkbox. Click any column header to sort.
The search box filters across IP, hostname, OEM, model, and serial.

## Add firmware

1. Go to **Firmware Library** → click **+ Add Firmware**.
2. Pick the binary (BMC / BIOS / HGX image).
3. Fill in **Type**, **OEM**, **Version**, optional **Model**, click **Add**.

The file is hashed, deduplicated, and copied into
`%LOCALAPPDATA%\ClusterOne\firmware\<sha256>\`. Hover any row to reveal the
delete (✕) button.

## Run an update

1. **Inventory** → select one or more servers (checkboxes).
2. Click **Update N selected** (or **Updates** in the nav rail).
3. On **Updates**: pick a firmware image, leave **Mode = Dry-run** (green) for
   your first time, hit **Start Update**.
4. The live progress panel shows every task transitioning through
   `queued → precheck → uploading → flashing → rebooting → verifying →
   completed | failed`. Cancel anytime.

> **Real flash** (red mode) is destructive. Always run Dry-run first against a
> fleet you don't know intimately. ClusterOne re-verifies SHA-256, checks
> model compatibility, and refuses silent downgrades by default.

If ClusterOne is closed mid-flash, the job resumes monitoring on next launch
from the persisted BMC TaskMonitor URI.

## Export a report

* **Dashboard → Export Inventory Report** writes a DOCX snapshot to
  `%USERPROFILE%\Documents\ClusterOne\` and opens it in Word / LibreOffice.
* Update-run reports follow the same format (PASS/FAIL per task,
  signature blocks) — suitable for inclusion in your existing PASS/FAIL
  validation workflow.

## Theme

Top-bar **switch** toggles dark ↔ light. The full **Light / Dark / System**
control lives in **Settings → Appearance**. Both choice and window geometry
persist across restarts.

## Where things live

| | Path |
|---|---|
| Database | `%LOCALAPPDATA%\ClusterOne\db\clusterone.db` (SQLite, WAL) |
| Firmware blobs | `%LOCALAPPDATA%\ClusterOne\firmware\<sha256>\` |
| Logs | `%LOCALAPPDATA%\ClusterOne\logs\clusterone.log` + `audit.log` |
| Reports | `%USERPROFILE%\Documents\ClusterOne\` |
| Plugin manifests | `<install>\plugins\<oem>\manifest.json` |

Override the data directory with the `CLUSTERONE_DATA_DIR` environment
variable.

## Troubleshooting

* **Window won't open / blank** — check `%LOCALAPPDATA%\ClusterOne\logs\clusterone.log`. Look for the most recent `Failed to load QML root object` line.
* **No servers found** — verify the range syntax, your network reaches the BMCs (port 443), and credentials are correct. The Activity page records every discovery run.
* **"Compatibility check failed"** before flash — the Generic Redfish plugin refuses if the firmware OEM doesn't match the server, or if the server is already at the requested version, or if the model glob doesn't match. Use a different firmware image, or pin the matching one to the right model.
* **Theme not persisting** — the app needs write access to its data dir. If you set `CLUSTERONE_DATA_DIR`, confirm it's writable.

## Keyboard

| Shortcut | Action |
|---|---|
| `Ctrl+K` | Open command palette |
| `↑ ↓` (in palette) | Navigate commands |
| `Enter` (in palette) | Run the selected command |
| `Esc` (in dialogs / palette) | Dismiss |
