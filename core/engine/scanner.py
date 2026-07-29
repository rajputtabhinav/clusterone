"""IP-range parser + parallel discovery driver.

The parser accepts the full set of syntaxes a datacenter operator types in
practice (full ranges, shorthand, CIDR, explicit ports for simulator dev). The
driver runs a TCP liveness probe per target, then delegates to the matched
OEM plugin to read identity over Redfish.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from core.models.server import Credentials, ServerInfo
from core.plugins_api.base import RedfishProbe
from core.plugins_api.redfish_client import RedfishClient, RedfishError

log = logging.getLogger(__name__)


@dataclass
class Target:
    ip: str
    port: int = 443
    scheme: str = "https"

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://{self.ip}:{self.port}"


def parse_range(spec: str, default_port: int = 443, default_scheme: str = "https") -> list[Target]:
    """Parse an operator-facing range spec into :class:`Target` objects.

    Comma-separable; each token may be one of::

        10.10.10.5                       # single
        10.10.10.1-10.10.10.254          # full range
        10.10.10.1-254                   # shorthand range
        10.10.10.0/24                    # CIDR
        host:port                        # explicit port (e.g. simulator)
        host:8000-8009                   # port range, single host
        http://host:port                 # explicit scheme
    """
    out: list[Target] = []
    for raw in spec.split(","):
        raw = raw.strip()
        if not raw:
            continue
        scheme = default_scheme
        if raw.startswith("http://"):
            scheme, raw = "http", raw[7:]
        elif raw.startswith("https://"):
            scheme, raw = "https", raw[8:]
        out.extend(_parse_one(raw, default_port, scheme))
    return out


def _parse_one(spec: str, default_port: int, scheme: str) -> list[Target]:
    # host:port (or host:port-port)
    if ":" in spec and "/" not in spec:
        host, port_spec = spec.rsplit(":", 1)
        if "-" in port_spec:
            lo_s, hi_s = port_spec.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
            if hi < lo:
                raise ValueError(f"Port range '{port_spec}' has end before start")
            return [Target(host, p, scheme) for p in range(lo, hi + 1)]
        return [Target(host, int(port_spec), scheme)]
    # CIDR
    if "/" in spec:
        net = ipaddress.ip_network(spec, strict=False)
        return [Target(str(ip), default_port, scheme) for ip in net.hosts()]
    # range
    if "-" in spec:
        lo, _, hi = spec.partition("-")
        lo = lo.strip()
        hi = hi.strip()
        if "." in hi:
            lo_i = int(ipaddress.IPv4Address(lo))
            hi_i = int(ipaddress.IPv4Address(hi))
        else:
            base = lo.rsplit(".", 1)[0]
            lo_i = int(ipaddress.IPv4Address(lo))
            hi_i = int(ipaddress.IPv4Address(f"{base}.{int(hi)}"))
        if hi_i < lo_i:
            raise ValueError(f"IP range '{spec}' has end before start")
        return [
            Target(str(ipaddress.IPv4Address(i)), default_port, scheme)
            for i in range(lo_i, hi_i + 1)
        ]
    # single
    return [Target(spec, default_port, scheme)]


async def scan(
    targets: list[Target],
    creds: Credentials,
    registry,
    on_found: Callable[[ServerInfo], None],
    on_progress: Callable[[int, int, int], None],   # scanned, found, total
    concurrency: int = 64,
    cancel_event: asyncio.Event | None = None,
) -> dict:
    """Probe each target; identify Redfish hosts; call plugin.discover()."""
    total = len(targets)
    sem = asyncio.Semaphore(concurrency)
    counters = {"scanned": 0, "found": 0}

    def tick(found_one: bool) -> None:
        counters["scanned"] += 1
        if found_one:
            counters["found"] += 1
        try:
            on_progress(counters["scanned"], counters["found"], total)
        except Exception:  # pragma: no cover
            log.exception("on_progress callback failed")

    async def probe(target: Target) -> None:
        if cancel_event is not None and cancel_event.is_set():
            tick(False)
            return
        async with sem:
            if cancel_event is not None and cancel_event.is_set():
                tick(False)
                return
            client = RedfishClient(
                target.ip, port=target.port, scheme=target.scheme,
                username=creds.username, password=creds.password,
            )
            # RedfishClient now keeps a persistent aiohttp session for its
            # lifetime — make sure we close it whatever path we take out.
            try:
                # TCP liveness — fast skip for dead hosts
                if not await client.tcp_alive():
                    tick(False)
                    return
                # Probe service root. Only catch the specific transport /
                # protocol errors we expect; let real bugs (KeyError,
                # AttributeError, …) propagate so they don't masquerade as
                # "no Redfish here".
                try:
                    root = await client.get_json("/redfish/v1/")
                except (RedfishError, OSError, asyncio.TimeoutError) as exc:
                    log.debug("%s no Redfish service root: %s", target.ip, exc)
                    tick(False)
                    return
                manufacturer = root.get("Vendor") or _first_oem_key(root)
                probe_obj = RedfishProbe(
                    ip=target.ip,
                    manufacturer=manufacturer,
                    redfish_root=client.base_url + "/redfish/v1/",
                    oem_blocks=tuple((root.get("Oem") or {}).keys()),
                )
                # MANUAL-ONLY OEM routing: scan-time identity is read via the
                # generic DMTF plugin (no OEM plugin overrides discover(), so
                # nothing is lost). The operator then pins the vendor per
                # server in the UI; operations refuse to run until they do.
                by_key = getattr(registry, "by_key", None)
                plugin = by_key("generic_redfish") if callable(by_key) else None
                if plugin is None:
                    plugin = registry.match(probe_obj)
                if plugin is None:
                    log.warning("%s no plugin available for identity read", target.ip)
                    tick(False)
                    return
                try:
                    info = await plugin.discover(client.base_url, creds)
                except Exception as exc:
                    log.warning("%s discover failed via %s: %s", target.ip, plugin.key, exc)
                    tick(False)
                    return
                # Storage discovery is best-effort: failures don't drop the
                # host. The provisioning workflow can re-poll on demand if
                # the cached `info.drives` is empty.
                if hasattr(plugin, "get_storage_inventory"):
                    try:
                        info.drives = await plugin.get_storage_inventory(client.base_url, creds)
                    except Exception as exc:
                        log.debug("%s storage discovery failed: %s", target.ip, exc)
                try:
                    on_found(info)
                except Exception:  # pragma: no cover
                    log.exception("on_found callback failed")
                tick(True)
            finally:
                await client.close()

    # ``return_exceptions=True``: a single rogue probe coroutine raising
    # (e.g. transient MemoryError, asyncio bug) used to tear down the
    # whole gather() and discard every result collected up to that point.
    # The caller's progress callback already records partial results; we
    # just need gather() to keep going on individual failures.
    results = await asyncio.gather(*(probe(t) for t in targets),
                                   return_exceptions=True)
    # Surface unexpected exceptions to the log so they don't vanish.
    for r, t in zip(results, targets):
        if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
            log.warning("scan probe for %s raised unhandled %s: %s",
                        t.ip, type(r).__name__, r)
    return {"scanned": counters["scanned"], "found": counters["found"], "total": total}


def _first_oem_key(d: dict) -> str | None:
    oem = d.get("Oem") or {}
    keys = list(oem.keys())
    return keys[0] if keys else None
