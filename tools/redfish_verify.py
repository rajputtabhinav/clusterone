"""Read-only Redfish verification — PASS/FAIL report for a single BMC.

Confirms every prerequisite the firmware-update flow depends on:
* TCP/TLS reachable on 443
* Auth accepted (with creds) and rejected (without)
* ServiceRoot, UpdateService, FirmwareInventory all served
* MultipartHttpPushUri advertised + the BIOS/BMC inventory members exist
* TaskService reachable + power-control action present
* Optional: OEM install action wired

Does not POST anywhere — no risk of triggering a flash or reset.

Usage:  python tools/redfish_verify.py <ip> <user> <pass>
"""
from __future__ import annotations

import asyncio
import socket
import ssl
import sys

import aiohttp


PASS = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"
INFO = "[INFO]"


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.passes = 0
        self.fails = 0
        self.warns = 0

    def add(self, tag: str, msg: str) -> None:
        self.lines.append(f"  {tag}  {msg}")
        if tag == PASS:
            self.passes += 1
        elif tag == FAIL:
            self.fails += 1
        elif tag == WARN:
            self.warns += 1

    def section(self, title: str) -> None:
        self.lines.append("")
        self.lines.append(title)
        self.lines.append("-" * len(title))


async def tcp_probe(ip: str, port: int, timeout: float = 4.0) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def get(sess, url):
    try:
        async with sess.get(url) as r:
            try:
                body = await r.json(content_type=None)
            except Exception:
                body = await r.text()
            return r.status, body
    except aiohttp.ClientError as e:
        return -1, str(e)


def build_session(ip: str, user: str | None, password: str | None) -> aiohttp.ClientSession:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    auth = aiohttp.BasicAuth(user, password) if user else None
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=ctx),
        auth=auth, timeout=aiohttp.ClientTimeout(total=10),
    )


async def verify(ip: str, user: str, password: str) -> Report:
    r = Report()
    base = f"https://{ip}"

    # ---- 1. Network ----
    r.section("1. Network reachability")
    alive = await tcp_probe(ip, 443)
    r.add(PASS if alive else FAIL, f"TCP 443 on {ip}: {'open' if alive else 'unreachable'}")
    if not alive:
        return r

    # ---- 2. TLS ----
    r.section("2. TLS")
    try:
        async with build_session(ip, None, None) as s:
            st, _ = await get(s, base + "/redfish/v1/")
            r.add(PASS if st >= 0 else FAIL, f"TLS handshake completed (HTTP {st})")
    except Exception as e:
        r.add(FAIL, f"TLS handshake failed: {e}")
        return r

    # ---- 3. Authentication ----
    r.section("3. Authentication")
    async with build_session(ip, None, None) as s:
        st, _ = await get(s, base + "/redfish/v1/UpdateService")
        r.add(PASS if st == 401 else WARN,
              f"Anonymous /UpdateService returned HTTP {st} (expected 401)")
    async with build_session(ip, user, password) as s:
        st, _ = await get(s, base + "/redfish/v1/UpdateService")
        r.add(PASS if st == 200 else FAIL,
              f"Authenticated /UpdateService returned HTTP {st} as {user}/****")
        if st != 200:
            return r

    # ---- 4. Service Root + UpdateService ----
    async with build_session(ip, user, password) as s:
        r.section("4. Service Root")
        st, root = await get(s, base + "/redfish/v1/")
        r.add(PASS if st == 200 else FAIL, f"GET /redfish/v1/ HTTP {st}")
        if isinstance(root, dict):
            r.add(INFO, f"Vendor: {root.get('Vendor', '?')}")
            r.add(INFO, f"RedfishVersion: {root.get('RedfishVersion', '?')}")

        r.section("5. UpdateService")
        st, us = await get(s, base + "/redfish/v1/UpdateService")
        r.add(PASS if st == 200 else FAIL, f"GET /redfish/v1/UpdateService HTTP {st}")
        if isinstance(us, dict):
            enabled = us.get("ServiceEnabled")
            r.add(PASS if enabled else FAIL, f"ServiceEnabled = {enabled}")
            mp = us.get("MultipartHttpPushUri")
            r.add(PASS if mp else WARN, f"MultipartHttpPushUri = {mp or '(none)'}")
            hp = us.get("HttpPushUri")
            r.add(INFO, f"HttpPushUri = {hp or '(none)'}")
            std = list((us.get("Actions") or {}).keys())
            oem_acts = list(((us.get("Actions") or {}).get("Oem") or {}).keys())
            r.add(INFO, f"Standard actions: {std}")
            r.add(INFO, f"OEM actions: {oem_acts}")

        # ---- 6. FirmwareInventory ----
        r.section("6. FirmwareInventory")
        st, fi = await get(s, base + "/redfish/v1/UpdateService/FirmwareInventory")
        r.add(PASS if st == 200 else FAIL, f"GET FirmwareInventory HTTP {st}")
        members = (fi.get("Members") or []) if isinstance(fi, dict) else []
        leaves = [m.get("@odata.id", "").rsplit("/", 1)[-1] for m in members]
        r.add(INFO, f"{len(leaves)} member(s): {leaves}")

        # BIOS / BMC primary entries — required for the flash path
        for name in ("BIOS", "BMC"):
            uri = f"/redfish/v1/UpdateService/FirmwareInventory/{name}"
            st, item = await get(s, base + uri)
            if st != 200:
                r.add(FAIL, f"{name}: GET HTTP {st}")
                continue
            ver = item.get("Version") if isinstance(item, dict) else ""
            updatable = item.get("Updateable") if isinstance(item, dict) else None
            tag = PASS if updatable is True else WARN
            r.add(tag, f"{name}: Version={ver!r}  Updateable={updatable}")

        # ---- 7. Systems / Managers (for power + reset) ----
        r.section("7. Systems / Managers")
        for path in ("/redfish/v1/Systems", "/redfish/v1/Managers"):
            st, coll = await get(s, base + path)
            ms = [m.get("@odata.id") for m in (coll.get("Members") or [])] \
                 if isinstance(coll, dict) else []
            r.add(PASS if st == 200 and ms else FAIL, f"GET {path} HTTP {st}: {ms}")

        st, sys1 = await get(s, base + "/redfish/v1/Systems/1")
        if isinstance(sys1, dict):
            r.add(INFO, f"Systems/1.PowerState = {sys1.get('PowerState')}")
            allowed = ((sys1.get("Actions") or {})
                       .get("#ComputerSystem.Reset") or {}) \
                      .get("ResetType@Redfish.AllowableValues") or []
            for t in ("ForceOff", "On", "GracefulRestart"):
                r.add(PASS if t in allowed else WARN,
                      f"ComputerSystem.Reset supports '{t}': "
                      f"{'yes' if t in allowed else 'no'}")

        # ---- 8. TaskService ----
        r.section("8. TaskService")
        st, ts = await get(s, base + "/redfish/v1/TaskService/Tasks")
        nrun = len((ts.get("Members") or []) if isinstance(ts, dict) else [])
        r.add(PASS if st == 200 else FAIL,
              f"GET /TaskService/Tasks HTTP {st}  ({nrun} active)")

    return r


async def main(ip, user, password):
    r = await verify(ip, user, password)
    print(f"\n========== Redfish verification: {ip} ==========")
    for line in r.lines:
        print(line)
    print()
    total = r.passes + r.fails + r.warns
    verdict = "READY" if r.fails == 0 else "NOT READY"
    print(f"  Summary: {r.passes} PASS / {r.warns} WARN / {r.fails} FAIL "
          f"({total} checks)   Verdict: {verdict}")
    print(f"================================================\n")
    return 0 if r.fails == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python tools/redfish_verify.py <ip> <user> <pass>")
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3])))
