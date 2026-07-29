"""IP-range parser — the operator-facing surface for discovery."""
from __future__ import annotations

from core.engine.scanner import parse_range


def test_single_ip_default_port():
    targets = parse_range("10.10.10.5")
    assert len(targets) == 1
    assert targets[0].ip == "10.10.10.5"
    assert targets[0].port == 443
    assert targets[0].scheme == "https"


def test_shorthand_range():
    targets = parse_range("10.10.10.1-3")
    assert [t.ip for t in targets] == ["10.10.10.1", "10.10.10.2", "10.10.10.3"]


def test_full_range():
    targets = parse_range("10.10.10.1-10.10.10.4")
    assert len(targets) == 4
    assert targets[-1].ip == "10.10.10.4"


def test_cidr():
    targets = parse_range("10.0.0.0/30")
    ips = sorted(t.ip for t in targets)
    assert ips == ["10.0.0.1", "10.0.0.2"]


def test_port_range_single_host():
    targets = parse_range("127.0.0.1:8000-8002")
    assert len(targets) == 3
    assert [t.port for t in targets] == [8000, 8001, 8002]


def test_explicit_scheme():
    targets = parse_range("http://127.0.0.1:8000")
    assert targets[0].scheme == "http"
    assert targets[0].port == 8000


def test_comma_separated():
    targets = parse_range("10.0.0.1, 10.0.0.2 , 10.0.0.3")
    assert [t.ip for t in targets] == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
