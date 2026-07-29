"""Qt list models exposed to QML.

A generic ``ObjectListModel`` (role-per-field over a list of dicts) sits behind a
``FilterSortProxy`` that QML binds to for live search + column sorting. Builders
wire each domain entity to its source loader.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from PyQt6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    pyqtProperty,
    pyqtSignal,
    pyqtSlot,
)

SERVER_FIELDS = [
    "id", "ip", "hostname", "oem", "oem_override", "manufacturer", "model_name",
    "serial", "bmc_version", "bios_version", "power_state", "status", "protocol",
]
FIRMWARE_FIELDS = ["id", "name", "type", "oem", "model_name", "version", "size_bytes", "uploaded_at"]
ISO_FIELDS = ["id", "name", "os_family", "os_version", "arch", "size_bytes", "uploaded_at", "source_url"]
ACTIVITY_FIELDS = ["id", "time", "message", "kind", "category"]


def _hhmm(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        # Rows are stored in UTC (activity_repo writes datetime.now(timezone.utc));
        # astimezone() converts to the operator's local time for display.
        return datetime.fromisoformat(iso).astimezone().strftime("%H:%M")
    except ValueError:
        return iso[11:16] if len(iso) >= 16 else iso


class ObjectListModel(QAbstractListModel):
    countChanged = pyqtSignal()

    def __init__(self, fields: list[str], rows: list[dict] | None = None, parent=None) -> None:
        super().__init__(parent)
        self._fields = list(fields)
        self._rows = list(rows or [])
        base = Qt.ItemDataRole.UserRole + 1
        self._role_to_field = {base + i: f for i, f in enumerate(self._fields)}
        self._field_to_role = {f: r for r, f in self._role_to_field.items()}

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        field = self._role_to_field.get(role)
        return None if field is None else self._rows[index.row()].get(field)

    def roleNames(self):
        return {r: f.encode() for r, f in self._role_to_field.items()}

    def role_of(self, field: str) -> int:
        return self._field_to_role.get(field, -1)

    def reset(self, rows: list[dict]) -> None:
        """Replace the row buffer and notify QML.

        Must be called from the GUI thread. Engine-thread callers should
        marshal through a Qt signal (every controller in ``controllers.py``
        already follows this pattern via ``_*FromEngine`` queued signals).
        Calling beginResetModel/endResetModel off the GUI thread is
        undefined behavior in Qt.
        """
        # Best-effort thread check — under PyQt the GUI thread owns the
        # default thread; mutating QAbstractListModel off-thread can crash
        # at random later. Log loudly so the misuse is visible during dev.
        try:
            from PyQt6.QtCore import QThread
            if QThread.currentThread() is not self.thread():
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "ObjectListModel.reset() called from non-GUI thread — "
                    "marshal via a Qt signal instead. Data: %d rows.",
                    len(rows),
                )
        except Exception:
            pass
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()
        self.countChanged.emit()

    def update_by_id(self, id_field: str, id_value, **changes) -> bool:
        """Patch a single row identified by ``id_field=id_value``; emit dataChanged."""
        for i, row in enumerate(self._rows):
            if row.get(id_field) == id_value:
                row.update(changes)
                idx = self.index(i, 0)
                self.dataChanged.emit(idx, idx)
                return True
        return False

    @pyqtProperty(int, notify=countChanged)
    def count(self) -> int:
        return len(self._rows)


class FilterSortProxy(QSortFilterProxyModel):
    filterTextChanged = pyqtSignal()
    countChanged = pyqtSignal()

    def __init__(self, search_fields: list[str], loader: Callable[[], list[dict]],
                 fields: list[str], parent=None) -> None:
        super().__init__(parent)
        self._text = ""
        self._search_fields = list(search_fields)
        self._loader = loader
        self._source = ObjectListModel(fields)
        self.setSourceModel(self._source)
        self.setDynamicSortFilter(True)
        for sig in (self.rowsInserted, self.rowsRemoved, self.modelReset):
            sig.connect(self.countChanged)
        self.reload()

    @pyqtSlot()
    def reload(self) -> None:
        self._source.reset(self._loader())

    @pyqtProperty(str, notify=filterTextChanged)
    def filterText(self) -> str:
        return self._text

    @filterText.setter
    def filterText(self, text: str) -> None:
        if text != self._text:
            self._text = text or ""
            self.invalidateFilter()
            self.filterTextChanged.emit()
            self.countChanged.emit()

    def filterAcceptsRow(self, src_row, src_parent) -> bool:
        if not self._text:
            return True
        src = self.sourceModel()
        idx = src.index(src_row, 0, src_parent)
        needle = self._text.lower()
        for field in self._search_fields:
            role = src.role_of(field)
            if role == -1:
                continue
            val = src.data(idx, role)
            if val is not None and needle in str(val).lower():
                return True
        return False

    @pyqtSlot(str)
    def sortByField(self, field: str) -> None:
        src = self.sourceModel()
        role = src.role_of(field)
        if role == -1:
            return
        order = Qt.SortOrder.AscendingOrder
        if self.sortRole() == role and self.sortOrder() == Qt.SortOrder.AscendingOrder:
            order = Qt.SortOrder.DescendingOrder
        self.setSortRole(role)
        self.sort(0, order)

    @pyqtProperty(int, notify=countChanged)
    def count(self) -> int:
        return self.rowCount()


def build_server_model(loader: Callable[[], list[dict]]) -> FilterSortProxy:
    def wrapped() -> list[dict]:
        # expose DB "model" as "model_name" so it doesn't clash with QML's
        # delegate `model` object.
        return [{**r, "model_name": r.get("model")} for r in loader()]
    return FilterSortProxy(["ip", "hostname", "oem", "model_name", "serial"], wrapped, SERVER_FIELDS)


def build_firmware_model(loader: Callable[[], list[dict]]) -> FilterSortProxy:
    def wrapped() -> list[dict]:
        # Rename DB column "model" → "model_name" so it doesn't shadow QML's
        # reserved `model` delegate object (same fix as build_server_model).
        return [{**r, "model_name": r.get("model")} for r in loader()]
    return FilterSortProxy(["name", "oem", "version", "type"], wrapped, FIRMWARE_FIELDS)


def build_iso_model(loader: Callable[[], list[dict]]) -> FilterSortProxy:
    return FilterSortProxy(["name", "os_family", "os_version", "arch"], loader, ISO_FIELDS)


def build_activity_model(service) -> FilterSortProxy:
    def loader() -> list[dict]:
        return [
            {"id": r["id"], "time": _hhmm(r["ts"]), "message": r["message"],
             "kind": r["level"], "category": r["category"]}
            for r in service.recent()
        ]
    return FilterSortProxy(["message", "category"], loader, ACTIVITY_FIELDS)
