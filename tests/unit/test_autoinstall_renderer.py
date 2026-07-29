"""Autoinstall renderer — per-host substitution into bundled / custom templates."""
from __future__ import annotations

import pytest

from core.services.autoinstall_renderer import render, template_dir


def test_bundled_templates_exist_on_disk():
    """Sprint 3 ships Rocky 9 + Ubuntu 24 — both must be present."""
    assert (template_dir() / "rocky9_minimal.ks").is_file()
    assert (template_dir() / "ubuntu24_minimal.yaml").is_file()


def test_render_rocky_minimal_substitutes_disk_and_hostname():
    out = render(
        template_body=None,
        os_family="rocky",
        hostname="web-01",
        ip="10.0.0.5",
        disk="nvme0n1",
        root_password="HUNTER2",
        ssh_authorized_keys="ssh-ed25519 AAAA…",
    )
    assert "${" not in out, "unresolved placeholders leaked into output"
    assert "web-01" in out
    assert "nvme0n1" in out
    assert "HUNTER2" in out
    assert "ssh-ed25519 AAAA…" in out


def test_render_ubuntu_minimal_substitutes_disk_path():
    out = render(
        template_body=None,
        os_family="ubuntu",
        hostname="db-01",
        ip="10.0.0.6",
        disk="nvme1n1",
        root_password="passwd-hash",
    )
    assert "/dev/nvme1n1" in out
    assert "db-01" in out
    # storage layout must reference the resolved disk, not a bare default
    assert "path: /dev/nvme1n1" in out


def test_render_uses_custom_template_when_provided():
    template = "hostname=${hostname} target=${disk_path}\n"
    out = render(template, os_family="rocky",
                 hostname="myhost", ip="1.2.3.4",
                 disk="sda", root_password="x")
    assert out == "hostname=myhost target=/dev/sda\n"


def test_render_disk_absolute_path_is_passed_through():
    """If the resolver gave us an absolute path already, don't double-/dev/-it."""
    template = "disk=${disk_path}\n"
    out = render(template, os_family="ubuntu",
                 hostname="x", ip="1.1.1.1",
                 disk="/dev/sda", root_password="p")
    assert "disk=/dev/sda\n" == out


def test_render_unknown_var_in_custom_template_is_preserved():
    """safe_substitute leaves unknown placeholders intact so an operator
    can spot a typo in the preview output rather than crashing."""
    template = "ok=${hostname} bad=${not_a_real_var}\n"
    out = render(template, os_family="rocky",
                 hostname="abc", ip="", disk="sda", root_password="")
    assert "ok=abc" in out
    assert "${not_a_real_var}" in out


def test_render_unknown_os_family_raises():
    with pytest.raises(ValueError, match="No bundled autoinstall template"):
        render(template_body=None, os_family="haiku",
               hostname="x", ip="", disk="sda", root_password="")


def test_render_empty_ssh_keys_renders_cleanly():
    out = render(template_body=None, os_family="rocky",
                 hostname="x", ip="", disk="sda", root_password="p")
    # Template should still render (empty placeholder for keys is fine).
    assert "${" not in out


# ---- serial-based disk targeting (wrong-disk-wipe hardening) ----

def test_render_rocky_targets_by_serial_via_pre_resolver():
    out = render(template_body=None, os_family="rocky",
                 hostname="x", ip="", disk="nvme0n1", serial="S64ABC123",
                 root_password="p")
    # NB: the %pre block legitimately contains bash like ${d#/dev/}; that's
    # literal shell, not an unresolved template var (safe_substitute leaves it).
    # serial resolved at install time by a %pre lsblk lookup, then %include'd
    assert "%pre" in out
    assert "S64ABC123" in out
    assert "lsblk -ndo SERIAL" in out
    assert "%include /tmp/clusterone-disk.ks" in out


def test_render_ubuntu_targets_by_serial_match():
    out = render(template_body=None, os_family="ubuntu",
                 hostname="x", ip="", disk="nvme0n1", serial="S64ABC123",
                 root_password="p")
    assert "serial: S64ABC123" in out
    assert "path: /dev/" not in out   # serial wins over path


def test_render_refuses_unsafe_disk_target():
    """No serial AND a non-device label (collision display name / opaque BMC
    id) must RAISE rather than emit a guessed /dev path to wipe."""
    with pytest.raises(ValueError, match="refusing to guess"):
        render(template_body=None, os_family="rocky",
               hostname="x", ip="", disk="Disk.Bay.3 (960 GB)", serial="",
               root_password="p")
