"""RedfishClient._url must pass absolute URLs through untouched.

Some BMCs return an absolute ``Location`` (TaskMonitor URI) in their 202
response. The old code did ``base_url + path`` unconditionally, producing a
malformed ``https://host:443http://host/...`` that failed to connect — so a
flash that was actually running got reported as a lost task.
"""
from __future__ import annotations

from core.plugins_api.redfish_client import RedfishClient


def test_url_builds_relative_paths():
    c = RedfishClient("10.0.0.5", port=443, scheme="https")
    assert c._url("/redfish/v1/Systems") == "https://10.0.0.5:443/redfish/v1/Systems"
    # missing leading slash is normalized
    assert c._url("redfish/v1/Systems") == "https://10.0.0.5:443/redfish/v1/Systems"


def test_url_passes_absolute_through():
    c = RedfishClient("10.0.0.5", port=443, scheme="https")
    abs_url = "http://10.0.0.5/redfish/v1/TaskService/Tasks/1"
    assert c._url(abs_url) == abs_url
    https_abs = "https://other-host:8443/redfish/v1/TaskService/Tasks/9"
    assert c._url(https_abs) == https_abs
