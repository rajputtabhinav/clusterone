"""Audit the Redfish UpdateService surface on every authenticated BMC.

Goal: confirm whether every BMC in your fleet uses the SAME flash recipe
or if there are variations our app needs to handle. Output is grouped by
"flash recipe fingerprint" so you can see at a glance how many distinct
update paths exist across the fleet.

For each BMC we record:
  - Vendor / OEM keys
  - MultipartHttpPushUri vs HttpPushUri
  - Standard actions (SimpleUpdate / StartUpdate)
  - OEM actions (e.g. SmcUpdateService.Install)
  - FirmwareInventory member IDs + Updateable flag on BIOS/BMC
  - OperationApplyTimeSupport (if advertised)

Usage:
    python tools/audit_update_apis.py <ip_list_file> <user> <pass>
"""
from __future__ import annotations

import asyncio
import json
import ssl
import sys
from collections import defaultdict

import aiohttp


RF_CONCURRENCY = 30
RF_TIMEOUT = 8.0


async def safe_get(sess, url, auth, timeout):
    try:
        async with sess.get(url, auth=auth, timeout=timeout) as r:
            try:
                body = await r.json(content_type=None)
            except Exception:
                body = None
            return r.status, body
    except Exception as e:
        return -1, f"{type(e).__name__}: {str(e)[:80]}"


async def audit_one(sess, ip: str, user: str, password: str) -> dict:
    base = f"https://{ip}"
    auth = aiohttp.BasicAuth(user, password)
    t = aiohttp.ClientTimeout(total=RF_TIMEOUT)
    out = {"ip": ip, "ok": False, "fingerprint": "", "recipe": "", "detail": {}}

    s, root = await safe_get(sess, base + "/redfish/v1/", auth, t)
    if s != 200 or not isinstance(root, dict):
        out["detail"]["error"] = f"ServiceRoot HTTP {s}"
        return out
    out["detail"]["vendor"] = root.get("Vendor")
    out["detail"]["redfish_version"] = root.get("RedfishVersion")
    out["detail"]["oem_keys"] = list((root.get("Oem") or {}).keys())

    s, us = await safe_get(sess, base + "/redfish/v1/UpdateService", auth, t)
    if s != 200 or not isinstance(us, dict):
        out["detail"]["error"] = f"UpdateService HTTP {s}"
        return out

    multipart = us.get("MultipartHttpPushUri") or ""
    http_push = us.get("HttpPushUri") or ""
    enabled = bool(us.get("ServiceEnabled", True))
    std_actions = list((us.get("Actions") or {}).keys())
    oem_actions = list(((us.get("Actions") or {}).get("Oem") or {}).keys())
    apply_supp = (us.get("MultipartHttpPushUri@Redfish.OperationApplyTimeSupport") or
                  us.get("HttpPushUri@Redfish.OperationApplyTimeSupport"))
    out["detail"].update({
        "service_enabled": enabled,
        "multipart_push_uri": multipart,
        "http_push_uri": http_push,
        "standard_actions": [a for a in std_actions if a != "Oem"],
        "oem_actions": oem_actions,
        "apply_time_support": apply_supp,
    })

    # FirmwareInventory — list members + check BIOS/BMC Updateable
    fi_status, fi = await safe_get(sess, base + "/redfish/v1/UpdateService/FirmwareInventory", auth, t)
    inv_ids: list[str] = []
    bios_updateable = bmc_updateable = None
    if fi_status == 200 and isinstance(fi, dict):
        for m in (fi.get("Members") or []):
            uri = m.get("@odata.id", "")
            leaf = uri.rsplit("/", 1)[-1]
            inv_ids.append(leaf)
        # Spot-check BIOS / BMC members
        for name in ("BIOS", "BMC"):
            if name in inv_ids:
                s2, item = await safe_get(
                    sess, base + f"/redfish/v1/UpdateService/FirmwareInventory/{name}",
                    auth, t)
                if s2 == 200 and isinstance(item, dict):
                    if name == "BIOS":
                        bios_updateable = item.get("Updateable")
                    else:
                        bmc_updateable = item.get("Updateable")
    out["detail"].update({
        "firmware_inventory_count": len(inv_ids),
        "firmware_inventory_ids": inv_ids,
        "bios_updateable": bios_updateable,
        "bmc_updateable": bmc_updateable,
    })

    out["ok"] = True
    # Recipe fingerprint: what flow code do we use?
    has_bios = "BIOS" in inv_ids
    has_bmc = "BMC" in inv_ids
    if not multipart:
        recipe = "no-multipart (Dell iDRAC pull style OR HPE-style HttpPushUri)"
    elif "#SmcUpdateService.Install" in oem_actions:
        recipe = "Supermicro/AMI MegaRAC (modern)"
    elif oem_actions:
        recipe = f"AMI MegaRAC + OEM ({','.join(oem_actions)})"
    else:
        recipe = "DMTF-only multipart push"
    out["recipe"] = recipe

    # Compact fingerprint key for grouping
    fp = (
        f"vendor={out['detail']['vendor']!r:25}  "
        f"multipart={multipart!r:50}  "
        f"oem_actions={oem_actions}  "
        f"bios_inv={'yes' if has_bios else 'no'}  "
        f"bmc_inv={'yes' if has_bmc else 'no'}"
    )
    out["fingerprint"] = fp
    return out


async def main(ip_file: str, user: str, password: str):
    with open(ip_file) as f:
        ips = [line.strip() for line in f if line.strip()]
    print(f"\n=== Auditing UpdateService on {len(ips)} BMCs ===\n")

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    sem = asyncio.Semaphore(RF_CONCURRENCY)
    conn = aiohttp.TCPConnector(ssl=ctx, limit=RF_CONCURRENCY * 2, limit_per_host=3)

    results = []
    async with aiohttp.ClientSession(connector=conn) as sess:
        async def one(ip):
            async with sem:
                try:
                    return await audit_one(sess, ip, user, password)
                except Exception as e:
                    return {"ip": ip, "ok": False, "fingerprint": "",
                            "recipe": "", "detail": {"error": str(e)[:80]}}
        results = await asyncio.gather(*(one(ip) for ip in ips), return_exceptions=False)

    # Group by fingerprint
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        if r["ok"]:
            groups[r["fingerprint"]].append(r)
        else:
            groups[f"FAILED: {r['detail'].get('error','?')}"].append(r)

    print(f"=== {len(groups)} distinct recipe(s) across {len(results)} BMCs ===\n")
    for i, (fp, members) in enumerate(sorted(groups.items(), key=lambda x: -len(x[1])), 1):
        sample = members[0]
        print(f"--- Group {i}: {len(members)} BMC(s) ---")
        if sample.get("ok"):
            print(f"  Recipe:          {sample['recipe']}")
            d = sample["detail"]
            print(f"  Vendor:          {d.get('vendor')!r}")
            print(f"  Multipart URI:   {d.get('multipart_push_uri')!r}")
            print(f"  HttpPushUri:     {d.get('http_push_uri')!r}")
            print(f"  Standard actions:{d.get('standard_actions')}")
            print(f"  OEM actions:     {d.get('oem_actions')}")
            print(f"  ApplyTimeSupport:{d.get('apply_time_support')}")
            print(f"  FW inventory:    {d.get('firmware_inventory_count')} members — {d.get('firmware_inventory_ids')}")
            print(f"  BIOS Updateable: {d.get('bios_updateable')}")
            print(f"  BMC Updateable:  {d.get('bmc_updateable')}")
        else:
            print(f"  Error: {sample['detail'].get('error','?')}")
        print(f"  IPs ({len(members)}):")
        for m in members:
            print(f"    - {m['ip']}")
        print()

    # Save raw JSON for the next step (codifying any quirks)
    with open("audit_update_apis.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"Raw audit saved to audit_update_apis.json\n")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python tools/audit_update_apis.py <ip_file> <user> <pass>")
        sys.exit(2)
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3]))
