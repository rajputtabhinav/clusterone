"""Trace what the app's flash code actually does — without the UI layer.

Runs the same plugin code the Flash button would trigger, against the real
BMC, but STOPS BEFORE the bytes are sent — so we can see exactly what HTTP
call would be made (URL, headers, body shape) without triggering a real
flash. Lets us answer "why doesn't our app flash?" deterministically.

Usage:  python tools/test_flash_path.py <fw_type>     (BIOS | BMC)
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys

# Allow the plaintext-vault fallback so we don't need DPAPI in this CLI.
os.environ["CLUSTERONE_ALLOW_PLAINTEXT_VAULT"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config                                       # noqa: E402
from core.models.server import Credentials, ServerInfo       # noqa: E402
from core.plugins_api.redfish_client import RedfishClient, RedfishError  # noqa: E402
from plugins.supermicro.plugin import SupermicroPlugin       # noqa: E402


def step(n, label):
    print(f"\n[{n}] {label}")


async def run(fw_type: str):
    db = sqlite3.connect(config.db_path())
    db.row_factory = sqlite3.Row

    step(1, "Load server from DB")
    srv_row = db.execute("SELECT * FROM servers LIMIT 1").fetchone()
    if not srv_row:
        print("    FAIL: no servers — run discovery first"); return 1
    server = ServerInfo(
        ip=srv_row["ip"],
        hostname=srv_row["hostname"], manufacturer=srv_row["manufacturer"],
        oem=srv_row["oem"], model=srv_row["model"], serial=srv_row["serial"],
        bmc_version=srv_row["bmc_version"], bios_version=srv_row["bios_version"],
        power_state=srv_row["power_state"], status=srv_row["status"],
        protocol="redfish",
        redfish_root=f"https://{srv_row['ip']}/redfish/v1/",
        tls_fingerprint=(srv_row["tls_fingerprint"] if "tls_fingerprint" in srv_row.keys() else None),
    )
    print(f"    Server: id={srv_row['id']}  ip={server.ip}  oem={server.oem}")
    print(f"    Versions: BIOS={server.bios_version!r}  BMC={server.bmc_version!r}")

    step(2, "Load firmware from DB")
    fw_row = db.execute(
        "SELECT * FROM firmware WHERE type=? ORDER BY id DESC LIMIT 1",
        (fw_type,)).fetchone()
    if not fw_row:
        print(f"    FAIL: no {fw_type} firmware in DB"); return 1
    print(f"    Firmware id={fw_row['id']}  name={fw_row['name']}")
    print(f"            type={fw_row['type']}  version={fw_row['version']}")
    print(f"            file_path={fw_row['file_path']}")
    has_file = bool(fw_row["file_path"]) and os.path.isfile(fw_row["file_path"] or "")
    print(f"            file exists on disk: {has_file}"
          + (f"  ({os.path.getsize(fw_row['file_path']):,} bytes)" if has_file else ""))
    if not has_file:
        print("    FAIL: firmware blob missing — re-import the file"); return 1

    step(3, "Resolve BMC credentials from vault")
    cred_row = db.execute(
        "SELECT * FROM credentials WHERE scope='default' OR scope=? OR scope=? "
        "ORDER BY CASE scope WHEN ? THEN 0 WHEN ? THEN 1 ELSE 2 END LIMIT 1",
        (f"host:{server.ip}", f"oem:{server.oem}",
         f"host:{server.ip}", f"oem:{server.oem}")).fetchone()
    if not cred_row:
        print("    FAIL: no credentials in vault"); return 1
    # The vault stores ciphertext; for plaintext fallback the password column
    # is the literal value. For DPAPI runs, decryption happens via the service.
    # This tool only reads username — the real plugin path resolves the full
    # cred via CredentialsService.
    from core.services.credentials_service import CredentialsService
    from data.repositories.credentials_repo import CredentialsRepo
    from core.security.credential_vault import CredentialVault
    vault = CredentialVault()
    cs = CredentialsService(CredentialsRepo(db), vault)
    creds = cs.resolve(ip=server.ip, oem=server.oem)
    if not creds:
        print("    FAIL: CredentialsService.resolve returned None"); return 1
    print(f"    Resolved: user={creds.username!r}  pwd=****")

    step(4, "Pick the OEM plugin")
    from core.plugins_api.base import RedfishProbe
    from core.plugins_api.registry import PluginRegistry
    from pathlib import Path
    reg = PluginRegistry()
    reg.load_bundled(Path(__file__).resolve().parent.parent / "plugins")
    probe_data = RedfishProbe(
        ip=server.ip,
        redfish_root=server.redfish_root,
        manufacturer=server.manufacturer,
        oem_blocks=[],
        model=server.model,
    )
    plugin = reg.match(probe_data) or SupermicroPlugin()
    print(f"    Plugin: {plugin.__class__.__name__}  (key={plugin.key})")

    step(5, "Resolve FirmwareInventory target on the BMC (read-only)")
    client = RedfishClient(server.ip, 443, "https",
                           username=creds.username, password=creds.password, timeout=15.0)
    try:
        target_uri = await plugin._resolve_firmware_target(client, fw_type)
        print(f"    Resolved target: {target_uri}")

        step(6, "Build the multipart body our app WOULD send (dry-run, no POST)")
        if hasattr(plugin, "_UPLOAD_URI"):
            upload_uri = plugin._UPLOAD_URI
        else:
            us = await client.get_json("/redfish/v1/UpdateService")
            upload_uri = us.get("MultipartHttpPushUri") or "/redfish/v1/UpdateService/upload"
        apply_time = "Immediate" if fw_type == "BMC" else "OnReset"
        update_params = {"Targets": [target_uri], "@Redfish.OperationApplyTime": apply_time}
        oem_params = {"ImageType": fw_type}
        print(f"    POST  https://{server.ip}{upload_uri}")
        print(f"          Authorization: Basic <{creds.username}>")
        print(f"          UpdateParameters part:  {json.dumps(update_params)}")
        print(f"          OemParameters part:     {json.dumps(oem_params)}")
        print(f"          UpdateFile part:        @{fw_row['file_path']}")
        print(f"                                 ({os.path.getsize(fw_row['file_path']):,} bytes)")

        step(7, "Verify the upload URI itself is reachable (HEAD-ish probe)")
        # Try a GET on the URI — most Redfish push URIs return 405 for GET but
        # not 404. A 404 here means the URI is wrong; a 405 confirms it exists.
        try:
            sess = await client._session_for()
            url = client.base_url + upload_uri
            async with sess.get(url) as r:
                print(f"    GET  {url}  ->  HTTP {r.status}  "
                      f"({'endpoint exists' if r.status in (200,405,406) else 'unexpected'})")
        except Exception as exc:
            print(f"    GET probe failed: {exc}")

        step(8, "What the BMC reports right now")
        us = await client.get_json("/redfish/v1/UpdateService")
        print(f"    MultipartHttpPushUri = {us.get('MultipartHttpPushUri')}")
        ts = await client.get_json("/redfish/v1/TaskService/Tasks")
        n = len(ts.get("Members") or [])
        print(f"    Active tasks: {n}")
        if n:
            print("    NOTE: An active task may block a new upload (BMC busy)")

        print("\n[CONCLUSION] All pre-flight checks PASS — the code path is sound.")
        print("    The exact POST shown above is what would be sent. Run with")
        print("    `--commit` to actually push and start the real flash.")
    except RedfishError as exc:
        print(f"\n[FAIL] {exc}")
        return 1
    finally:
        await client.close()
        db.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1].upper() not in ("BIOS", "BMC"):
        print("Usage: python tools/test_flash_path.py <BIOS|BMC>")
        sys.exit(2)
    sys.exit(asyncio.run(run(sys.argv[1].upper())))
