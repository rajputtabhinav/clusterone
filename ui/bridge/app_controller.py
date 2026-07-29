"""Top-level application controller exposed to QML as ``App``."""
from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtProperty, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QGuiApplication

from app import config


class AppController(QObject):
    connectionStatusChanged = pyqtSignal()
    navigateRequested = pyqtSignal(int)               # nav index (0=Dashboard ... 7=Settings)
    toastRequested = pyqtSignal(str, str)             # message, kind ("info"|"success"|"warning"|"error")

    # Command Palette / shortcut signals — emitted from `App.*` slots so the
    # palette and keyboard shortcuts can trigger page-local actions without
    # the issuer knowing which page hosts the dialog. Pages connect to the
    # signal that matches their feature.
    discoveryRequested = pyqtSignal()                 # open Discover dialog
    addFirmwareRequested = pyqtSignal()               # open Firmware Library + add form
    addIsoRequested = pyqtSignal()                    # open ISO Library + add form
    addCredentialRequested = pyqtSignal()             # open Settings + Add Credential dialog
    exportInventoryRequested = pyqtSignal()           # Dashboard's Export action
    exportAuditRequested = pyqtSignal()               # Settings' Export Audit
    cancelJobRequested = pyqtSignal()                 # cancel a running Updates / Provisioning job

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._connection_status = "Ready"

    @pyqtSlot(int)
    def navigateTo(self, index: int) -> None:
        self.navigateRequested.emit(index)

    @pyqtSlot(str, str)
    def showToast(self, message: str, kind: str = "info") -> None:
        self.toastRequested.emit(message, (kind or "info"))

    @pyqtSlot()
    def openDiscovery(self) -> None:
        """Navigate to Inventory and open the Discover dialog."""
        self.navigateRequested.emit(1)
        self.discoveryRequested.emit()

    @pyqtSlot()
    def openAddFirmware(self) -> None:
        """Navigate to Firmware Library and open the Add dialog."""
        self.navigateRequested.emit(4)
        self.addFirmwareRequested.emit()

    @pyqtSlot()
    def openAddIso(self) -> None:
        """Navigate to ISO Library and open the Add dialog."""
        self.navigateRequested.emit(5)
        self.addIsoRequested.emit()

    @pyqtSlot()
    def openAddCredential(self) -> None:
        """Navigate to Settings and open the Add Credential dialog."""
        self.navigateRequested.emit(7)
        self.addCredentialRequested.emit()

    @pyqtSlot()
    def exportInventory(self) -> None:
        self.exportInventoryRequested.emit()

    @pyqtSlot()
    def exportAuditLog(self) -> None:
        self.exportAuditRequested.emit()

    @pyqtSlot()
    def cancelRunningJob(self) -> None:
        self.cancelJobRequested.emit()

    @pyqtProperty(str, constant=True)
    def appName(self) -> str:
        return config.APP_NAME

    @pyqtProperty(str, constant=True)
    def tagline(self) -> str:
        return config.APP_TAGLINE

    @pyqtProperty(str, constant=True)
    def version(self) -> str:
        return config.APP_VERSION

    @pyqtProperty(str, notify=connectionStatusChanged)
    def connectionStatus(self) -> str:
        return self._connection_status

    @pyqtSlot()
    def quit(self) -> None:
        QGuiApplication.quit()
