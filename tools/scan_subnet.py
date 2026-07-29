"""Subnet-wide BMC discovery for 172.16.0.0/16 (or any CIDR).

Two-pass scan, both async with high concurrency:
  Pass 1 — TCP connect to :443 across the whole subnet (fast filter)
  Pass 2 — GET /redfish/v1/ on each port-443 hit with provided creds
           and dump vendor / model / firmware versions

Usage:
    python tools/scan_subnet.py 172.16.0.0/16 admin "<password>"
    python tools/scan_subnet.py 172.16.12.0/24 admin "<password>"   (smaller test)

Sensible defaults: 800 concurrent TCP probes, 2.5s timeout, 50 concurrent
Redfish probes with 6s timeout. Results stream to stdout as they come in.
"""
from __future__ import annotations

import asyncio
import ipaddress
import ssl
import sys
import time
from contextlib import suppress

import aiohttp


TCP_CONCURRENCY = 800
TCP_TIMEOUT = 2.5
RF_CONCURRENCY = 50
RF_TIMEOUT = 6.0


async def tcp_probe(ip: str, port: int = 443, timeout: float = TCP_TIMEOUT) -> bool:
    try:
        fut = asyncio.open_connection(ip, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()
        return True
    except (asyncio.TimeoutError, OSError):
        return False


async def pass1_tcp(subnet: ipaddress.IPv4Network) -> list[str]:
    """Find every host with TCP 443 open in the subnet."""
    total = subnet.num_addresses
    print(f"Pass 1 — TCP :443 scan across {total} addresses "
          f"(concurrency={TCP_CONCURRENCY}, timeout={TCP_TIMEOUT}s)")
    sem = asyncio.Semaphore(TCP_CONCURRENCY)
    found: list[str] = []
    progress = {"done": 0, "found": 0, "started": time.time()}

    async def one(ip: str) -> None:
        async with sem:
            ok = await tcp_probe(ip)
            progress["done"] += 1
            if ok:
                progress["found"] += 1
                found.append(ip)
                print(f"  + {ip:15}  open  (#{progress['found']})")
            # Show heartbeat every 2000 hosts so it doesn't look frozen
            if progress["done"] % 2000 == 0:
                rate = progress["done"] / max(time.time() - progress["started"], 0.001)
                eta = (total - progress["done"]) / rate if rate else 0
                print(f"    ...progress {progress['done']}/{total}  "
                      f"({rate:.0f}/s, ETA {eta:.0f}s)  hits={progress['found']}")

    hosts = [str(h) for h in subnet.hosts()]
    await asyncio.gather(*(one(h) for h in hosts))
    elapsed = time.time() - progress["started"]
    print(f"Pass 1 done in {elapsed:.1f}s — {len(found)} host(s) with :443 open")
    return sorted(found, key=lambda s: tuple(int(p) for p in s.split(".")))


async def _safe_json(r) -> dict | None:
    """Tolerant JSON read — many non-Redfish HTTPS services on port 443
    (switches, UPSes, IPMI web UIs) return HTML/text and the strict JSON
    decoder would crash the whole probe. Return None on any parse failure."""
    try:
        return await r.json(content_type=None)
    except Exception:
        return None


async def redfish_probe(sess, ip: str, user: str, password: str) -> dict:
    """Single Redfish probe with the given creds. Returns a row dict.
    Catches every exception — must never crash the outer gather()."""
    base = f"https://{ip}"
    out = {"ip": ip, "redfish": False, "auth": None, "vendor": "?",
           "model": "?", "bios": "?", "bmc": "?", "error": ""}
    auth = aiohttp.BasicAuth(user, password)
    timeout = aiohttp.ClientTimeout(total=RF_TIMEOUT)
    try:
        # 1) Is the ServiceRoot served at all?
        async with sess.get(base + "/redfish/v1/", timeout=timeout) as r:
            if r.status != 200:
                out["error"] = f"ServiceRoot HTTP {r.status}"
                return out
            root = await _safe_json(r)
            # Without explicit grouping, Python parses this as:
            #   not isinstance(...) or ("@odata.type" not in root and "RedfishVersion" not in root)
            # which is what we want — BUT the original line lacked the
            # explicit parens around the AND clause, and that combined
            # with the OR is easy to misread on review. Parenthesize
            # explicitly so the intent is unambiguous and matches the
            # behavior on whatever Python interpreter ships.
            if not isinstance(root, dict) or (
                "@odata.type" not in root and "RedfishVersion" not in root
            ):
                out["error"] = "200 but not Redfish JSON (HTML/text)"
                return out
            out["redfish"] = True
            out["vendor"] = (root.get("Vendor") or "?")
        # 2) Auth check + read Systems[0] for model/BIOS
        async with sess.get(base + "/redfish/v1/Systems", auth=auth, timeout=timeout) as r:
            if r.status == 401:
                out["auth"] = False
                out["error"] = "401 — bad creds"
                return out
            if r.status != 200:
                out["error"] = f"Systems HTTP {r.status}"
                return out
            out["auth"] = True
            sysc = await _safe_json(r) or {}
            members = sysc.get("Members") or []
        if members:
            async with sess.get(base + members[0]["@odata.id"], auth=auth, timeout=timeout) as r:
                if r.status == 200:
                    s = await _safe_json(r) or {}
                    out["model"] = s.get("Model") or "?"
                    out["bios"] = s.get("BiosVersion") or "?"
        # 3) Managers[0] for BMC version
        async with sess.get(base + "/redfish/v1/Managers", auth=auth, timeout=timeout) as r:
            if r.status == 200:
                mc = await _safe_json(r) or {}
                ms = mc.get("Members") or []
                if ms:
                    async with sess.get(base + ms[0]["@odata.id"], auth=auth, timeout=timeout) as r2:
                        if r2.status == 200:
                            m = await _safe_json(r2) or {}
                            out["bmc"] = m.get("FirmwareVersion") or "?"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:100]}"
    return out


async def pass2_redfish(hosts: list[str], user: str, password: str) -> list[dict]:
    """Probe Redfish on every host that passed pass 1."""
    print(f"\nPass 2 — Redfish probe on {len(hosts)} candidate(s) "
          f"as {user!r}  (concurrency={RF_CONCURRENCY})")
    if not hosts:
        return []
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    sem = asyncio.Semaphore(RF_CONCURRENCY)
    results: list[dict] = []

    # Use limit_per_host=1 so we don't pool a million connections at once
    conn = aiohttp.TCPConnector(ssl=ctx, limit=RF_CONCURRENCY * 2, limit_per_host=2)
    async with aiohttp.ClientSession(connector=conn) as sess:
        async def one(ip: str) -> None:
            async with sem:
                try:
                    row = await redfish_probe(sess, ip, user, password)
                except Exception as exc:
                    row = {"ip": ip, "redfish": False, "auth": None,
                           "vendor": "?", "model": "?", "bios": "?", "bmc": "?",
                           "error": f"{type(exc).__name__}: {str(exc)[:80]}"}
                results.append(row)
                if row["redfish"]:
                    auth_tag = ("AUTH-OK" if row["auth"] else
                                ("AUTH-FAIL" if row["auth"] is False else "no-auth-yet"))
                    print(f"  + {ip:15}  Redfish={row['vendor']:18}  "
                          f"{auth_tag}  model={row['model']:25}  "
                          f"BIOS={row['bios']:15}  BMC={row['bmc']}", flush=True)
                else:
                    print(f"  - {ip:15}  not Redfish  ({row['error'][:80]})", flush=True)
        # return_exceptions so a single rogue probe never kills the batch
        await asyncio.gather(*(one(h) for h in hosts), return_exceptions=True)
    return results


def summary(rows: list[dict]) -> None:
    rf_ok = [r for r in rows if r["redfish"]]
    auth_ok = [r for r in rf_ok if r["auth"]]
    print(f"\n=========== SUMMARY ===========")
    print(f"  Hosts probed for Redfish:  {len(rows)}")
    print(f"  Redfish-speaking BMCs:     {len(rf_ok)}")
    print(f"  Authenticated successfully: {len(auth_ok)}")
    if auth_ok:
        print(f"\n  Flashable inventory (auth OK):")
        print(f"  {'IP':16}{'Vendor':18}{'Model':28}{'BIOS':18}{'BMC':18}")
        print(f"  {'-'*16}{'-'*18}{'-'*28}{'-'*18}{'-'*18}")
        for r in sorted(auth_ok, key=lambda x: tuple(int(p) for p in x["ip"].split("."))):
            print(f"  {r['ip']:16}{r['vendor'][:17]:18}{r['model'][:27]:28}"
                  f"{r['bios'][:17]:18}{r['bmc'][:17]:18}")
    print(f"================================\n")


async def main(cidr_or_file: str, user: str, password: str) -> int:
    t0 = time.time()
    # If arg is a file path, treat each line as a pre-scanned IP and skip Pass 1.
    # Saves ~3 minutes if we already have the candidate list.
    import os.path
    if os.path.isfile(cidr_or_file):
        with open(cidr_or_file) as f:
            candidates = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        print(f"\n=== Skipping Pass 1 — using {len(candidates)} candidates from {cidr_or_file} ===\n")
    else:
        try:
            subnet = ipaddress.IPv4Network(cidr_or_file, strict=False)
        except ValueError as e:
            print(f"Invalid CIDR or file: {e}"); return 2
        print(f"\n=== Subnet scan: {subnet} ({subnet.num_addresses} addresses) ===\n")
        candidates = await pass1_tcp(subnet)
        # Persist the list so a Pass 2 failure can be retried instantly
        cand_file = "scan_candidates.txt"
        with open(cand_file, "w") as f:
            f.write("# TCP :443 open hosts — re-runnable via\n")
            f.write(f"# python tools/scan_subnet.py {cand_file} {user} '<password>'\n")
            for ip in candidates:
                f.write(ip + "\n")
        print(f"Candidate list saved to {cand_file}")

    rows = await pass2_redfish(candidates, user, password)
    summary(rows)
    print(f"Total elapsed: {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python tools/scan_subnet.py <CIDR|candidate_file> <user> [password]")
        print('Example: python tools/scan_subnet.py 172.16.0.0/16 admin')
        print()
        print("Password sources, in order of preference:")
        print("  * BMC_PASSWORD environment variable   (safest)")
        print("  * stdin if not a TTY                   (good for CI)")
        print("  * positional arg                       (logged in shell history!)")
        sys.exit(2)
    cidr = sys.argv[1]
    user = sys.argv[2]
    # Prefer environment / stdin over the positional arg to keep the
    # password out of shell history and `ps` output.
    import os
    import getpass
    password = os.environ.get("BMC_PASSWORD") or ""
    if not password and len(sys.argv) >= 4:
        password = sys.argv[3]
    if not password:
        if sys.stdin.isatty():
            password = getpass.getpass(f"Password for {user}: ")
        else:
            password = sys.stdin.read().rstrip("\n")
    if not password:
        print("No password supplied (env BMC_PASSWORD, stdin, or arg).")
        sys.exit(2)
    sys.exit(asyncio.run(main(cidr, user, password)))
