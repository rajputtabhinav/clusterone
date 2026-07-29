"""Bridge list-model behavior."""
from __future__ import annotations

from ui.bridge.models import ObjectListModel


def test_count_and_update_by_id(qcore):
    m = ObjectListModel(["id", "name", "state"], [
        {"id": 1, "name": "a", "state": "queued"},
        {"id": 2, "name": "b", "state": "queued"},
    ])
    assert m.count == 2
    assert m.update_by_id("id", 2, state="completed", name="b!")
    rows = m._rows  # white-box for the assert
    assert rows[1]["state"] == "completed"
    assert rows[1]["name"] == "b!"


def test_update_by_id_missing(qcore):
    m = ObjectListModel(["id", "state"], [{"id": 1, "state": "x"}])
    assert m.update_by_id("id", 999, state="y") is False


def test_reset_swaps_rows(qcore):
    m = ObjectListModel(["id"], [{"id": 1}])
    m.reset([{"id": 10}, {"id": 11}])
    assert m.count == 2
