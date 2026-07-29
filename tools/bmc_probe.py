"""Probe a live BMC and dump every Redfish endpoint that matters for
firmware update, so you can SEE what the BMC actually advertises instead
of guessing from vendor docs.

Usage:
    python tools/bmc_probe.py <ip> <user> <pass>

Prints the URI tree, the UpdateService capabilities, FirmwareInventory IDs,
OEM actions, ApplyTimeSupport, and any TaskService tasks currently running.
This is the answer to "how do I know the real API path?" — ask the BMC.
"""
from __future__ import annotations

import asyncio
import json
import ssl
import sys
from typing import Any

import aiohttp


async def get(sess: aiohttp.ClientSession, url: str) -> tuple[int, Any]:
    try:
        async with sess.get(url) as r:
            try:
                body = await r.json(content_type=None)
            except Exception:
                body = (await r.text())[:300]
            return r.status, body
    except Exception as e:
        return -1, str(e)


def show(label: str, value: Any) -> None:
    """Pretty-print a single value or a short dict."""
    if isinstance(value, (dict, list)):
        out = json.dumps(value, indent=2, default=str)
        if len(out) > 1200:
            out = out[:1200] + "\n  ...(truncated)"
        print(f"  {label}:\n{out}")
    else:
        print(f"  {label}: {value}")


async def probe(ip: str, user: str, password: str) -> None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    auth = aiohttp.BasicAuth(user, password)
    timeout = aiohttp.ClientTimeout(total=15)
    base = f"https://{ip}"

    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=ctx),
        auth=auth, timeout=timeout,
    ) as sess:
        print(f"\n=== BMC at {ip} ===\n")

        # ServiceRoot — reveals vendor, version, top-level URIs
        s, root = await get(sess, f"{base}/redfish/v1/")
        if s != 200:
            print(f"  ServiceRoot HTTP {s}: {root}")
            return
        print("Service Root:")
        show("Vendor", root.get("Vendor"))
        show("RedfishVersion", root.get("RedfishVersion"))
        show("Oem keys", list((root.get("Oem") or {}).keys()))
        print()

        # UpdateService — THE critical surface for flashes
        s, us = await get(sess, f"{base}/redfish/v1/UpdateService")
        print(f"UpdateService (HTTP {s}):")
        if s == 200:
            show("ServiceEnabled", us.get("ServiceEnabled"))
            show("MultipartHttpPushUri", us.get("MultipartHttpPushUri"))
            show("HttpPushUri", us.get("HttpPushUri"))
            show("HttpPushUriTargetsBusy", us.get("HttpPushUriTargetsBusy"))
            show("MaxImageSizeBytes", us.get("MaxImageSizeBytes"))
            # ApplyTimeSupport tells you what timing values your BMC accepts
            ats = us.get("MultipartHttpPushUri@Redfish.OperationApplyTimeSupport") \
                  or us.get("HttpPushUri@Redfish.OperationApplyTimeSupport")
            show("OperationApplyTimeSupport", ats)
            # Standard actions
            std = [k for k in (us.get("Actions") or {}) if not k.startswith("Oem")]
            show("Standard Actions", std)
            # OEM actions — vendor-specific install triggers live here
            show("OEM Actions", (us.get("Actions") or {}).get("Oem"))
            show("Oem top-level keys", list((us.get("Oem") or {}).keys()))
        print()

        # FirmwareInventory — the canonical Targets[] members
        s, fi = await get(sess, f"{base}/redfish/v1/UpdateService/FirmwareInventory")
        print(f"FirmwareInventory (HTTP {s}):")
        members = (fi.get("Members") or []) if isinstance(fi, dict) else []
        for m in members:
            uri = m.get("@odata.id", "?")
            leaf = uri.rsplit("/", 1)[-1]
            s2, item = await get(sess, base + uri)
            ver = str((item.get("Version") if isinstance(item, dict) else "") or "")
            updatable = (item.get("Updateable") if isinstance(item, dict) else "")
            print(f"  - {leaf:30}  Version={ver:36}  Updateable={updatable}  ({uri})")
        print()

        # Supermicro InstallActionInfo — what params does Install want?
        s, info = await get(sess, f"{base}/redfish/v1/UpdateService/Oem/Supermicro/InstallActionInfo")
        if s == 200 and isinstance(info, dict):
            print("Supermicro Install ActionInfo:")
            show("Parameters", info.get("Parameters"))
            print()

        # Systems & Managers — what /Systems/<id> and /Managers/<id> resolve to
        for path in ("/redfish/v1/Systems", "/redfish/v1/Managers"):
            s, coll = await get(sess, base + path)
            members = (coll.get("Members") or []) if isinstance(coll, dict) else []
            print(f"{path} (HTTP {s}): {[m.get('@odata.id') for m in members]}")
        print()

        # Active tasks — if a flash is mid-flight, it shows up here
        s, ts = await get(sess, f"{base}/redfish/v1/TaskService/Tasks")
        members = (ts.get("Members") or []) if isinstance(ts, dict) else []
        print(f"TaskService/Tasks (HTTP {s}): {len(members)} active")
        for m in members[:5]:
            uri = m.get("@odata.id", "")
            s2, t = await get(sess, base + uri)
            if isinstance(t, dict):
                print(f"  - {t.get('Name','?')} State={t.get('TaskState')} Pct={t.get('PercentComplete')} ({uri})")

        # System.Reset action info — what ResetType values does this BMC accept?
        s, sys1 = await get(sess, f"{base}/redfish/v1/Systems/1")
        if isinstance(sys1, dict):
            print(f"\nSystems/1.PowerState: {sys1.get('PowerState')}")
            reset_info = ((sys1.get("Actions") or {}).get("#ComputerSystem.Reset") or {})
            show("ComputerSystem.Reset",
                 {k: reset_info.get(k) for k in ("target", "ResetType@Redfish.AllowableValues")})


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python tools/bmc_probe.py <ip> <user> <pass>")
        sys.exit(2)
    asyncio.run(probe(sys.argv[1], sys.argv[2], sys.argv[3]))
