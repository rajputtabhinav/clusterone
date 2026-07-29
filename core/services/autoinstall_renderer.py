"""Per-host autoinstall config renderer.

Turns an ``OS Profile`` template + per-host context (hostname, resolved
disk, network, SSH keys) into the literal kickstart / cloud-init / etc.
config that the running installer fetches over HTTP.

Substitution uses Python's ``string.Template`` with ``${var}`` placeholders.
This keeps templates JSON-clean (no Jinja dependency) and the variable
surface tiny + auditable.

Required variables at render time:

    ${hostname}          Resolved host name
    ${ip}                Server's mgmt IP (for activity log / cloud-init)
    ${disk}              Resolved drive name (e.g. 'nvme0n1' or '/dev/sda')
    ${disk_path}         '/dev/' + disk
    ${root_password}     Plaintext from vault (rendered into the installer)
    ${ssh_authorized_keys}  Newline-joined keys, or empty
    ${gateway}           Network gateway, or empty
    ${nameserver}        DNS server, or empty

Bundled minimal templates for Rocky 9 (kickstart) and Ubuntu 24
(autoinstall / cloud-init) live in ``data/autoinstall_templates/`` and
load by ``os_family`` if the OS Profile didn't ship its own body.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from string import Template

from app import config

log = logging.getLogger(__name__)

_TEMPLATE_DIR = config.resource_path("data", "autoinstall_templates")

# A genuine Linux block-device handle (NOT a BMC bay label or a disambiguated
# display name like "nvme0n1 (960 GB)").
_DEVICE_RE = re.compile(r"^(nvme\d+n\d+|sd[a-z]+|vd[a-z]+)$")


def _disk_snippets(os_family: str, *, device: str, serial: str) -> dict[str, str]:
    """Build the OS-specific disk-targeting snippet(s).

    Targets the disk by **serial** whenever the BMC reported one — a serial is
    stable across the BMC-vs-host enumeration mismatch that makes a Redfish bay
    index an unsafe source for a Linux device name. Falls back to a literal
    device name ONLY when it's a genuine Linux handle. Raises ``ValueError`` if
    neither is usable, so the caller fails the task cleanly instead of wiping a
    guessed disk.
    """
    fam = (os_family or "").lower()
    device = (device or "").strip()
    serial = (serial or "").strip()
    is_abs = device.startswith("/dev/")
    bare = device[len("/dev/"):] if is_abs else device
    is_device = is_abs or bool(_DEVICE_RE.match(device))
    if not serial and not is_device:
        raise ValueError(
            f"cannot target a disk safely: no serial reported and {device!r} is "
            "not a Linux device handle — refusing to guess which disk to wipe"
        )

    if fam in ("ubuntu", "debian"):
        path = device if is_abs else f"/dev/{device}"
        match = f"serial: {serial}" if serial else f"path: {path}"
        return {"disk_premount": "", "disk_directives": "", "disk_match": match}

    # kickstart families (rocky / rhel / alma)
    if serial:
        premount = (
            "%pre --interpreter=/bin/bash --log=/tmp/clusterone-disk-resolve.log\n"
            f"TARGET_SERIAL='{serial}'\n"
            "DEV=''\n"
            "for d in /dev/nvme*n1 /dev/sd? /dev/vd?; do\n"
            '  [ -b "$d" ] || continue\n'
            '  sn=$(lsblk -ndo SERIAL "$d" 2>/dev/null)\n'
            '  if [ "$sn" = "$TARGET_SERIAL" ]; then DEV="${d#/dev/}"; break; fi\n'
            "done\n"
            'if [ -z "$DEV" ]; then echo "ClusterOne: no disk with serial '
            '$TARGET_SERIAL found" >&2; exit 1; fi\n'
            "cat > /tmp/clusterone-disk.ks <<EOF\n"
            "ignoredisk --only-use=$DEV\n"
            "clearpart --drives=$DEV --all --initlabel\n"
            "bootloader --boot-drive=$DEV --location=mbr\n"
            "EOF\n"
            "%end"
        )
        directives = "%include /tmp/clusterone-disk.ks"
    else:
        premount = ""
        directives = (
            f"ignoredisk --only-use={bare}\n"
            f"clearpart --drives={bare} --all --initlabel\n"
            f"bootloader --boot-drive={bare} --location=mbr"
        )
    return {"disk_premount": premount, "disk_directives": directives, "disk_match": ""}


def _builtin_template(os_family: str) -> str:
    """Load a bundled template by OS family; raises if none exists."""
    fname = {
        "rocky":  "rocky9_minimal.ks",
        "rhel":   "rocky9_minimal.ks",       # close enough
        "alma":   "rocky9_minimal.ks",
        "ubuntu": "ubuntu24_minimal.yaml",
        "debian": "ubuntu24_minimal.yaml",   # autoinstall-compatible
    }.get(os_family.lower())
    if not fname:
        raise ValueError(f"No bundled autoinstall template for OS family '{os_family}'")
    path = _TEMPLATE_DIR / fname
    if not path.is_file():
        raise FileNotFoundError(f"Bundled template missing on disk: {path}")
    return path.read_text(encoding="utf-8")


def render(
    template_body: str | None,
    os_family: str,
    *,
    hostname: str,
    ip: str,
    disk: str,
    root_password: str,
    serial: str = "",
    ssh_authorized_keys: str = "",
    gateway: str = "",
    nameserver: str = "",
) -> str:
    """Substitute per-host context into the template.

    If ``template_body`` is empty/None, falls back to the bundled
    minimal template for ``os_family``.

    Uses ``Template.safe_substitute`` so a typo in the operator's
    template (an unknown ${var}) leaves a literal placeholder rather
    than raising — operators can spot the broken substitution in the
    preview before any install fires.
    """
    body = template_body if (template_body or "").strip() else _builtin_template(os_family)
    # Build the disk-targeting snippets first — raises ValueError (caught by the
    # orchestrator → clean task failure) rather than wiping a guessed disk.
    snippets = _disk_snippets(os_family, device=disk, serial=serial)
    clean = (disk or "").strip()
    disk_path = f"/dev/{clean}" if not clean.startswith("/") else clean
    ctx = {
        "hostname": hostname or "unnamed-host",
        "ip": ip or "",
        "disk": clean,
        "disk_path": disk_path,
        "disk_serial": (serial or "").strip(),
        "disk_match": snippets["disk_match"],
        "disk_premount": snippets["disk_premount"],
        "disk_directives": snippets["disk_directives"],
        "root_password": root_password or "",
        "ssh_authorized_keys": ssh_authorized_keys or "",
        "gateway": gateway or "",
        "nameserver": nameserver or "",
    }
    return Template(body).safe_substitute(ctx)


def template_dir() -> Path:
    """Where bundled templates live (exposed for tests + plugin SDK docs)."""
    return _TEMPLATE_DIR
