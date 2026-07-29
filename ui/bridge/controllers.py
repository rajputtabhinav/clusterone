"""QML-facing controllers.

Exposed to QML as ``Fleet`` (inventory), ``Firmware`` (library), ``EventLog``
(activity), ``Discovery`` (scan controller), and ``Updates`` (job controller).
"""
from __future__ import annotations

import logging
from pathlib import Path

import json

from PyQt6.QtCore import QObject, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from core.models.server import Credentials
from core.services.activity_service import ActivityService
from core.services.credentials_service import CredentialsService
from core.services.discovery_service import DiscoveryService
from core.services.firmware_service import FirmwareService
from core.services.inventory_service import InventoryService
from core.services.iso_service import IsoService
from core.services.provisioning_orchestrator import ProvisioningOrchestrator
from core.services.jobs_service import JobsService
from core.services.reports_service import ReportsService
from core.services.settings_service import SettingsService
from core.services.update_orchestrator import UpdateOrchestrator
from ui.bridge.models import (
    ObjectListModel,
    build_activity_model,
    build_firmware_model,
    build_iso_model,
    build_server_model,
)

_CRED_FIELDS = ["id", "label", "username", "scope", "created_at"]

log = logging.getLogger(__name__)


def _safe_emit(controller: "QObject", destroyed_flag: str, signal_name: str, *args) -> None:
    """Emit ``signal_name`` on ``controller`` from an engine-thread callback,
    swallowing the two failure modes that bite long-lived async callbacks:

    1. The controller's ``mark_destroyed()`` flag is set — Qt shutdown is
       in progress; emitting now would land on a soon-to-die slot.
    2. The underlying QObject has already been finalized by Qt
       (``RuntimeError: wrapped C/C++ object of type … has been deleted``).
       Happens if a background task finishes after the window closed.

    Without this, both cases dump unrecoverable tracebacks into the log
    and may crash the engine thread mid-job.
    """
    try:
        if getattr(controller, destroyed_flag, False):
            return
        signal = getattr(controller, signal_name, None)
        if signal is None:
            return
        signal.emit(*args)
    except RuntimeError as exc:
        # "wrapped C/C++ object of type X has been deleted" — Qt collected
        # the controller before this engine-thread callback fired. Treat
        # as a soft shutdown signal; don't log every occurrence (callbacks
        # may fire dozens of times after teardown).
        log.debug("safe_emit: controller gone for signal %s: %s",
                  signal_name, exc)
    except Exception:
        log.exception("safe_emit unexpected failure on signal %s", signal_name)


class InventoryController(QObject):
    countsChanged = pyqtSignal()
    selectionChanged = pyqtSignal()

    def __init__(self, service: InventoryService, plugins=None, parent=None) -> None:
        super().__init__(parent)
        self._svc = service
        self._plugins = plugins   # PluginRegistry — feeds the OEM-override picker
        self._model = build_server_model(service.list)
        self._counts = service.status_counts()
        self._selected: list[int] = []

    @pyqtProperty(QObject, constant=True)
    def model(self) -> QObject:
        return self._model

    @pyqtSlot()
    def refresh(self) -> None:
        self._model.reload()
        self._counts = self._svc.status_counts()
        self.countsChanged.emit()

    @pyqtSlot(int)
    def remove(self, server_id: int) -> None:
        self._svc.delete(server_id)
        # Purge the deleted id from the selection so the "N selected" pill and
        # select-all count don't keep counting a server that no longer exists.
        sid = int(server_id)
        if sid in self._selected:
            self._selected.remove(sid)
            self.selectionChanged.emit()
        self.refresh()

    # ---- manual OEM override -------------------------------------------
    @pyqtProperty("QVariantList", constant=True)
    def oemOptions(self):
        """Picker entries for the per-server OEM selection (MANUAL-ONLY —
        no Auto entry): vendor plugins alphabetically, generic DMTF last.
        Every server must have one picked before flash/provision/power."""
        vendors: list[dict] = []
        generic: dict | None = None
        for p in (self._plugins.all() if self._plugins else []):
            key = getattr(p, "key", "") or ""
            if not key:
                continue
            if key == "generic_redfish":
                generic = {"key": key, "name": "Generic (DMTF Redfish)"}
            else:
                vendors.append({"key": key,
                                "name": getattr(p, "display_name", key) or key})
        vendors.sort(key=lambda d: d["name"].lower())
        opts = vendors
        if generic:
            opts.append(generic)
        return opts

    @pyqtSlot(int, str)
    def setOemOverride(self, server_id: int, oem_key: str) -> None:
        """Pin the OEM for one server; empty key restores auto-detection."""
        self._svc.set_oem_override(int(server_id), (oem_key or "").strip() or None)
        self.refresh()

    # ---- counts ----
    @pyqtProperty(int, notify=countsChanged)
    def total(self) -> int:
        return self._counts.get("total", 0)

    @pyqtProperty(int, notify=countsChanged)
    def online(self) -> int:
        return self._counts.get("online", 0)

    @pyqtProperty(int, notify=countsChanged)
    def needsUpdate(self) -> int:
        return self._counts.get("needs_update", 0)

    @pyqtProperty(int, notify=countsChanged)
    def failed(self) -> int:
        return self._counts.get("failed", 0)

    # ---- selection (drives the Updates wizard) ----
    @pyqtProperty("QVariantList", notify=selectionChanged)
    def selectedIds(self) -> list[int]:
        return list(self._selected)

    @pyqtProperty(int, notify=selectionChanged)
    def selectedCount(self) -> int:
        return len(self._selected)

    @pyqtSlot(int)
    def toggleSelection(self, server_id: int) -> None:
        if server_id in self._selected:
            self._selected = [i for i in self._selected if i != server_id]
        else:
            self._selected = [*self._selected, server_id]
        self.selectionChanged.emit()

    @pyqtSlot()
    def clearSelection(self) -> None:
        if self._selected:
            self._selected = []
            self.selectionChanged.emit()

    @pyqtSlot()
    def selectAll(self) -> None:
        self._selected = [row["id"] for row in self._svc.list()]
        self.selectionChanged.emit()

    @pyqtProperty("QVariantList", notify=countsChanged)
    def allServerIds(self) -> list[int]:
        """All discovered server IDs — used by Provision page so the
        operator doesn't need to manually select servers in Inventory first."""
        return [r["id"] for r in self._svc.list()]

    @pyqtSlot(int, result=bool)
    def isSelected(self, server_id: int) -> bool:
        return server_id in self._selected

    @pyqtSlot(int, result=str)
    def listDrives(self, server_id: int) -> str:
        """Return the cached disk inventory for a server as JSON.

        The Provision page calls this lazily when expanding a host row to
        render its per-server disk picker. Avoids bloating ``preview()``
        responses with full drive lists for every selected host.
        """
        row = self._svc.list()
        for r in row:
            if int(r.get("id") or 0) == int(server_id):
                return r.get("disks_json") or "[]"
        return "[]"


class FirmwareController(QObject):
    changed = pyqtSignal()
    _importDone = pyqtSignal(str, str)   # kind ('added'|'dup'|'error'), message

    def __init__(self, service: FirmwareService, activity: ActivityService,
                 app_controller=None, engine=None, parent=None) -> None:
        super().__init__(parent)
        self._svc = service
        self._activity = activity
        self._app = app_controller
        self._engine = engine          # run heavy hash+copy off the GUI thread
        self._destroyed = False
        self._model = build_firmware_model(service.list)
        self._importDone.connect(self._on_import_done)

    def mark_destroyed(self) -> None:
        self._destroyed = True

    @pyqtProperty(QObject, constant=True)
    def model(self) -> QObject:
        return self._model

    @pyqtProperty(int, notify=changed)
    def count(self) -> int:
        return self._svc.count()

    @pyqtSlot(int, result=str)
    def getById(self, fw_id: int) -> str:
        """Return JSON string ``{id, name, type, oem, version}`` for the given
        firmware id, or ``'{}'`` if not found.  Used by the Updates page to
        restore the selected firmware label after a restart without scanning
        the proxy model from QML."""
        # Public service API — was reaching into ``_svc._repo`` which would
        # AttributeError the moment FirmwareService gets refactored.
        row = self._svc.get(int(fw_id))
        if row is None:
            return "{}"
        return json.dumps({
            "id":      row.get("id"),
            "name":    row.get("name") or "",
            "type":    row.get("type") or "",
            "oem":     row.get("oem") or "",
            "version": row.get("version") or "",
        })

    @pyqtSlot(str, str, str, str, str, str)
    def add(self, file_url: str, fw_type: str, oem: str, version: str, model: str, notes: str) -> None:
        path = QUrl(file_url).toLocalFile() if file_url.startswith("file:") else file_url
        name = Path(path).name

        def work():
            return self._svc.import_file(
                path, fw_type=fw_type, oem=oem, version=version or "unknown",
                model=model or None, notes=notes or None,
            )

        # A firmware blob is 30-300 MB; hashing + copying it on the GUI thread
        # froze the window. Offload to the async engine; the result marshals
        # back via _importDone (queued → GUI thread) to reload the model + toast.
        if self._engine is None:
            self._finish_import(work, name)   # tests / no engine: synchronous
            return
        if self._app:
            self._app.showToast(f"Importing {name}…", "info")

        async def _job():
            import asyncio
            try:
                fw_id = await asyncio.to_thread(work)
            except Exception as exc:
                log.error("Firmware import failed: %s", exc)
                _safe_emit(self, "_destroyed", "_importDone", "error", str(exc))
                return
            if fw_id is not None:
                self._activity.log("info", "firmware", f"Firmware {name} uploaded")
            _safe_emit(self, "_destroyed", "_importDone",
                       "added" if fw_id is not None else "dup", name)

        self._engine.submit(_job())

    def _finish_import(self, work, name: str) -> None:
        try:
            fw_id = work()
        except Exception as exc:
            log.error("Firmware import failed: %s", exc)
            self._on_import_done("error", str(exc))
            return
        if fw_id is not None:
            self._activity.log("info", "firmware", f"Firmware {name} uploaded")
        self._on_import_done("added" if fw_id is not None else "dup", name)

    @pyqtSlot(str, str)
    def _on_import_done(self, kind: str, message: str) -> None:
        if self._app:
            if kind == "added":
                self._app.showToast(f"Added {message}", "success")
            elif kind == "dup":
                self._app.showToast("Firmware already in library (matching SHA)", "info")
            else:
                self._app.showToast(f"Firmware import failed: {message}", "error")
        self._model.reload()
        self.changed.emit()

    @pyqtSlot(int)
    def remove(self, fw_id: int) -> None:
        row = self._svc.delete(fw_id)
        if row:
            self._activity.log("info", "firmware", f"Firmware {row.get('name', '')} deleted")
            if self._app:
                self._app.showToast(f"Removed {row.get('name', 'firmware')}", "success")
        self._model.reload()
        self.changed.emit()


class IsoController(QObject):
    """ISO Library bridge — same UX shape as FirmwareController.

    Exposed to QML as ``Isos`` (see ``app/container.py``).
    """
    changed = pyqtSignal()
    _importDone = pyqtSignal(str, str)   # kind ('added'|'dup'|'error'), message

    def __init__(self, service: IsoService, activity: ActivityService,
                 app_controller=None, engine=None, parent=None) -> None:
        super().__init__(parent)
        self._svc = service
        self._activity = activity
        self._app = app_controller
        self._engine = engine          # multi-GB ISO hash+copy off the GUI thread
        self._destroyed = False
        self._model = build_iso_model(service.list)
        self._importDone.connect(self._on_import_done)

    def mark_destroyed(self) -> None:
        self._destroyed = True

    @pyqtProperty(QObject, constant=True)
    def model(self) -> QObject:
        return self._model

    @pyqtProperty(int, notify=changed)
    def count(self) -> int:
        return self._svc.count()

    @pyqtSlot(str, str, str, str, str, str)
    def add(self, file_url: str, os_family: str, os_version: str,
            arch: str, source_url: str, notes: str) -> None:
        path = QUrl(file_url).toLocalFile() if file_url.startswith("file:") else file_url
        name = Path(path).name

        def work():
            return self._svc.import_file(
                path,
                os_family=os_family or "rhel",
                os_version=os_version or "unknown",
                arch=arch or "x86_64",
                source_url=source_url or None,
                notes=notes or None,
            )

        # ISOs are multi-GB — hashing + copying on the GUI thread froze the
        # window for minutes. Offload to the async engine; result marshals back
        # via _importDone (queued → GUI thread).
        if self._engine is None:
            self._finish_import(work, name)
            return
        if self._app:
            self._app.showToast(f"Importing {name}…", "info")

        async def _job():
            import asyncio
            try:
                iso_id = await asyncio.to_thread(work)
            except Exception as exc:
                log.error("ISO import failed: %s", exc)
                _safe_emit(self, "_destroyed", "_importDone", "error", str(exc))
                return
            if iso_id is not None:
                self._activity.log("info", "provisioning", f"ISO {name} added to library")
            _safe_emit(self, "_destroyed", "_importDone",
                       "added" if iso_id is not None else "dup", name)

        self._engine.submit(_job())

    def _finish_import(self, work, name: str) -> None:
        try:
            iso_id = work()
        except Exception as exc:
            log.error("ISO import failed: %s", exc)
            self._on_import_done("error", str(exc))
            return
        if iso_id is not None:
            self._activity.log("info", "provisioning", f"ISO {name} added to library")
        self._on_import_done("added" if iso_id is not None else "dup", name)

    @pyqtSlot(str, str)
    def _on_import_done(self, kind: str, message: str) -> None:
        if self._app:
            if kind == "added":
                self._app.showToast(f"Added {message}", "success")
            elif kind == "dup":
                self._app.showToast("ISO already in library (matching SHA)", "info")
            else:
                self._app.showToast(f"ISO import failed: {message}", "error")
        self._model.reload()
        self.changed.emit()

    @pyqtSlot(int)
    def remove(self, iso_id: int) -> None:
        row = self._svc.delete(iso_id)
        if row:
            self._activity.log("info", "provisioning",
                               f"ISO {row.get('name', '')} removed from library")
        self._model.reload()
        self.changed.emit()


class PowerController(QObject):
    """Power-control bridge — exposed to QML as ``Power``.

    Per-server or bulk power actions run on the async engine; each result
    crosses back to the GUI thread via a queued internal signal, refreshes the
    inventory model so the new PowerState shows, and raises a toast. Mirrors
    the engine->GUI marshaling used by DiscoveryController/UpdateController.
    """
    busyChanged = pyqtSignal()
    # server_id, action, ok, message, power_state
    resultReceived = pyqtSignal(int, str, bool, str, str)

    _resultFromEngine = pyqtSignal(int, str, bool, str, str)
    _doneFromEngine = pyqtSignal(int, int)              # ok_count, fail_count

    _UI_LABELS = {
        "on": "Power On", "off": "Shut Down", "force_off": "Force Off",
        "restart": "Restart", "graceful_restart": "Graceful Restart",
        "cycle": "Power Cycle",
    }

    def __init__(self, service, fleet: "InventoryController",
                 event_log: "ActivityController", app_controller=None,
                 parent=None) -> None:
        super().__init__(parent)
        self._svc = service
        self._fleet = fleet
        self._event_log = event_log
        self._app = app_controller
        self._inflight = 0
        # Set by Container.shutdown so late engine callbacks don't emit on a
        # QObject Qt is about to destroy.
        self._destroyed = False
        self._resultFromEngine.connect(self._on_result_main)
        self._doneFromEngine.connect(self._on_done_main)

    def mark_destroyed(self) -> None:
        self._destroyed = True

    @pyqtProperty(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._inflight > 0

    @pyqtSlot(int, str)
    def applyOne(self, server_id: int, action: str) -> None:
        """Convenience for the per-server drawer: power one host."""
        self.apply([int(server_id)], action)

    @pyqtSlot("QVariantList", str)
    def apply(self, server_ids, action: str) -> None:
        ids = [int(s) for s in server_ids if s is not None]
        if not ids:
            return
        fut = self._svc.apply(
            ids, action,
            on_result=lambda sid, act, ok, msg, st: _safe_emit(
                self, "_destroyed", "_resultFromEngine", sid, act, ok, msg, st),
            on_done=lambda summary: _safe_emit(
                self, "_destroyed", "_doneFromEngine",
                int(summary.get("ok", 0)), int(summary.get("failed", 0))),
        )
        if fut is None:
            if self._app:
                self._app.showToast("Unknown power action", "error")
            return
        self._inflight += 1
        self.busyChanged.emit()

    @pyqtSlot(int, str, bool, str, str)
    def _on_result_main(self, sid: int, action: str, ok: bool,
                        message: str, state: str) -> None:
        self._fleet.refresh()        # new PowerState into the model + counts
        self._event_log.refresh()    # audit entry into the activity feed
        if self._app:
            label = self._UI_LABELS.get(action, action)
            if ok:
                self._app.showToast(
                    f"{label}: {message}" if message else f"{label} issued", "success")
            else:
                self._app.showToast(f"{label} failed: {message}", "error")
        self.resultReceived.emit(sid, action, ok, message, state)

    @pyqtSlot(int, int)
    def _on_done_main(self, ok: int, failed: int) -> None:
        if self._inflight > 0:
            self._inflight -= 1
            self.busyChanged.emit()


_PROV_TASK_FIELDS = ["id", "server_ip", "server_hostname", "state",
                     "progress", "message", "resolved_disk"]
_PROV_JOB_FIELDS = ["id", "state", "total", "succeeded", "failed", "created_at"]

# Settings keys used by Provision / Updates pages for wizard-state
# persistence. The actual values are read/written through
# SettingsController.getInt / setInt / getBool / setBool so the QML pages
# don't need bespoke controller slots per key.
#   provision_last_iso_id      int     last ISO chip picked
#   provision_last_strategy    string  last disk-strategy chip
#   provision_last_concurrency int     last power-cycle batch size
#   updates_last_firmware_id   int     last firmware chip picked
#   updates_last_apply_now     bool    last apply-timing toggle position


class ProvisioningController(QObject):
    """Bulk OS install bridge — exposed to QML as ``Provisioning``.

    Two-phase UX:

    1. ``previewSelection(serverIds, diskProfileId)`` — synchronous; returns a
       JSON list of per-host preview rows so the wizard can render the
       confirmation screen before any wipe is committed.
    2. ``start(...)`` — asynchronous; takes the operator's typed confirm
       string and dispatches the job onto the AsyncEngine.
    """
    isRunningChanged = pyqtSignal()
    statsChanged = pyqtSignal()
    errorChanged = pyqtSignal()

    _taskUpdateFromEngine = pyqtSignal(int, str, int, str)
    _jobDoneFromEngine = pyqtSignal(int, int, str)
    _errorFromEngine = pyqtSignal(str)

    def __init__(
        self,
        orchestrator: ProvisioningOrchestrator,
        provisioning_repo,
        event_log: "ActivityController",
        fleet: InventoryController,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._orch = orchestrator
        self._jobs = provisioning_repo
        self._event_log = event_log
        self._fleet = fleet
        self._running = False
        self._succeeded = 0
        self._failed = 0
        self._total = 0
        self._current_job_id = 0
        self._error = ""
        self._tasks = ObjectListModel(_PROV_TASK_FIELDS)
        self._destroyed = False
        self._taskUpdateFromEngine.connect(self._on_task_update_main)
        self._jobDoneFromEngine.connect(self._on_job_done_main)
        self._errorFromEngine.connect(self._on_error_main)

    def mark_destroyed(self) -> None:
        self._destroyed = True

    @pyqtProperty(QObject, constant=True)
    def model(self) -> QObject:
        return self._tasks

    @pyqtProperty(bool, notify=isRunningChanged)
    def isRunning(self) -> bool:
        return self._running

    @pyqtProperty(int, notify=statsChanged)
    def total(self) -> int:
        return self._total

    @pyqtProperty(int, notify=statsChanged)
    def succeeded(self) -> int:
        return self._succeeded

    @pyqtProperty(int, notify=statsChanged)
    def failed(self) -> int:
        return self._failed

    @pyqtProperty(int, notify=statsChanged)
    def currentJobId(self) -> int:
        return self._current_job_id

    @pyqtProperty(str, notify=errorChanged)
    def error(self) -> str:
        return self._error

    @pyqtSlot("QVariantList", int, result=str)
    def previewSelection(self, server_ids, disk_profile_id: int) -> str:
        """Return JSON list of per-host preview rows for the wizard.

        Resolves disks using the named profile (id > 0) or skips disk
        resolution entirely. For inline-strategy use (the v1.1 page passes
        its own strategy string) prefer ``previewWithStrategy`` instead.
        """
        ids = [int(x) for x in server_ids if x is not None]
        prof_id = int(disk_profile_id) if int(disk_profile_id) > 0 else None
        rows = self._orch.preview(ids, prof_id)
        return json.dumps(rows)

    @pyqtSlot("QVariantList", str, result=str)
    def previewWithStrategy(self, server_ids, strategy: str) -> str:
        """Like ``previewSelection`` but takes a strategy string directly so
        the QML wizard can prototype strategies without persisting them."""
        ids = [int(x) for x in server_ids if x is not None]
        rows = self._orch.preview(ids, None, strategy_override=strategy or None)
        return json.dumps(rows)

    @pyqtSlot("QVariantList", str, str, result=str)
    def previewWithOverrides(self, server_ids, strategy: str,
                             overrides_json: str) -> str:
        """Per-server preview: ``overrides_json`` is ``{"<sid>": "drive_name"}``.

        The picker UI calls this whenever the operator manually selects a
        drive for a specific server. Overrides win over ``strategy`` per host.
        """
        ids = [int(x) for x in server_ids if x is not None]
        try:
            overrides_raw = json.loads(overrides_json or "{}") or {}
        except Exception:
            overrides_raw = {}
        # JSON keys are strings; normalize to int sid for the orchestrator.
        overrides = {int(k): str(v) for k, v in overrides_raw.items() if v}
        rows = self._orch.preview(
            ids, None,
            strategy_override=strategy or None,
            overrides=overrides,
        )
        return json.dumps(rows)

    @pyqtSlot("QVariantList", int, int, int, str, int, str, str)
    def start(self, server_ids, iso_id: int, os_profile_id: int,
              disk_profile_id: int, confirm_token: str,
              concurrency: int,
              strategy: str = "", overrides_json: str = "{}") -> None:
        if self._running:
            return
        ids = [int(x) for x in server_ids if x is not None]
        if not ids:
            self._error = "No servers selected"
            self.errorChanged.emit()
            return
        try:
            overrides_raw = json.loads(overrides_json or "{}") or {}
        except Exception:
            overrides_raw = {}
        overrides = {int(k): str(v) for k, v in overrides_raw.items() if v}
        self._error = ""; self.errorChanged.emit()
        job_id = self._orch.start_job(
            server_ids=ids,
            iso_id=int(iso_id),
            os_profile_id=int(os_profile_id) if int(os_profile_id) > 0 else None,
            disk_profile_id=int(disk_profile_id) if int(disk_profile_id) > 0 else None,
            confirm_token=confirm_token,
            concurrency=int(concurrency),
            strategy_override=strategy or None,
            overrides=overrides or None,
            on_task_update=lambda tid, st, pct, msg:
                _safe_emit(self, "_destroyed", "_taskUpdateFromEngine", tid, st, pct, msg),
            on_job_done=lambda stats:
                _safe_emit(self, "_destroyed", "_jobDoneFromEngine",
                           stats.get("succeeded", 0), stats.get("failed", 0), stats.get("state", "")),
            on_error=lambda m: _safe_emit(self, "_destroyed", "_errorFromEngine", m),
        )
        if job_id is None:
            return
        self._current_job_id = job_id
        self._running = True
        self._succeeded = 0; self._failed = 0
        # Seed the task model from DB so the rows have the real task ids the
        # orchestrator will reference in its progress callbacks.
        seeded = []
        for t in self._jobs.list_tasks(job_id):
            seeded.append({
                "id": t["id"],
                "server_ip": t.get("server_ip") or "",
                "server_hostname": t.get("server_hostname") or t.get("server_ip") or "",
                "state": t.get("state", "queued"),
                "progress": t.get("progress", 0),
                "message": t.get("message") or "",
                "resolved_disk": t.get("resolved_disk") or "",
            })
        self._tasks.reset(seeded)
        self._total = len(seeded)
        self.isRunningChanged.emit(); self.statsChanged.emit()

    @pyqtSlot()
    def cancel(self) -> None:
        self._orch.cancel()

    @pyqtSlot(int, str, int, str)
    def _on_task_update_main(self, task_id: int, state: str, pct: int, message: str) -> None:
        self._tasks.update_by_id("id", task_id, state=state, progress=pct, message=message)
        if state in ("completed", "failed", "cancelled"):
            if state == "completed":
                self._succeeded += 1
            else:
                self._failed += 1
            self.statsChanged.emit()

    @pyqtSlot(int, int, str)
    def _on_job_done_main(self, succ: int, failed: int, state: str) -> None:
        self._succeeded = succ
        self._failed = failed
        self._running = False
        self.isRunningChanged.emit()
        self.statsChanged.emit()
        self._event_log.refresh()
        self._fleet.refresh()

    @pyqtSlot(str)
    def _on_error_main(self, msg: str) -> None:
        self._error = msg
        self._running = False
        self.isRunningChanged.emit()
        self.errorChanged.emit()


class ActivityController(QObject):
    def __init__(self, service: ActivityService, parent=None) -> None:
        super().__init__(parent)
        self._model = build_activity_model(service)

    @pyqtProperty(QObject, constant=True)
    def model(self) -> QObject:
        return self._model

    @pyqtSlot()
    def refresh(self) -> None:
        self._model.reload()


class DiscoveryController(QObject):
    isScanningChanged = pyqtSignal()
    progressChanged = pyqtSignal()
    errorChanged = pyqtSignal()
    lastRangeChanged = pyqtSignal()
    finished = pyqtSignal()
    disksRefreshed = pyqtSignal(int, bool, str)   # server_id, ok, message

    _disksRefreshedFromEngine = pyqtSignal(int, bool, str)

    _foundFromEngine = pyqtSignal()
    _progressFromEngine = pyqtSignal(int, int, int)
    _doneFromEngine = pyqtSignal()
    _errorFromEngine = pyqtSignal(str)

    # Persisted under this settings key so the ribbon (and Discover dialog)
    # show the user's most-recent scan target even across app restarts.
    _RANGE_SETTING = "last_discovery_range"

    def __init__(self, service: DiscoveryService, fleet: InventoryController,
                 event_log: ActivityController,
                 settings: SettingsService | None = None, parent=None) -> None:
        super().__init__(parent)
        self._svc = service
        self._fleet = fleet
        self._event_log = event_log
        self._settings = settings
        self._scanning = False
        self._scanned = 0
        self._found = 0
        self._total = 0
        self._error = ""
        self._last_range = (
            settings.get(self._RANGE_SETTING, "") or "" if settings else ""
        )
        # Set by Container.shutdown so engine-thread callbacks stop trying to
        # emit signals on a QObject that's about to be destroyed.
        self._destroyed = False
        self._foundFromEngine.connect(self._on_found_main)
        self._progressFromEngine.connect(self._on_progress_main)
        self._doneFromEngine.connect(self._on_done_main)
        self._errorFromEngine.connect(self._on_error_main)
        self._disksRefreshedFromEngine.connect(self._on_disks_refreshed_main)

    def mark_destroyed(self) -> None:
        self._destroyed = True

    @pyqtProperty(bool, notify=isScanningChanged)
    def isScanning(self): return self._scanning
    @pyqtProperty(int, notify=progressChanged)
    def scanned(self): return self._scanned
    @pyqtProperty(int, notify=progressChanged)
    def found(self): return self._found
    @pyqtProperty(int, notify=progressChanged)
    def total(self): return self._total
    @pyqtProperty(str, notify=errorChanged)
    def error(self): return self._error
    @pyqtProperty(str, notify=lastRangeChanged)
    def lastRange(self) -> str:
        """Most recent scan target (e.g. ``10.10.10.0/24`` or
        ``127.0.0.1:8000-8009``). Empty before the first scan."""
        return self._last_range

    @pyqtSlot(str, str, str)
    def start(self, range_spec: str, username: str, password: str) -> None:
        if self._scanning:
            return
        self._scanning = True
        self._scanned = 0; self._found = 0; self._total = 0; self._error = ""
        self.isScanningChanged.emit(); self.progressChanged.emit(); self.errorChanged.emit()
        # Remember this range so the ribbon + dialog reflect it across restarts.
        clean = (range_spec or "").strip()
        if clean and clean != self._last_range:
            self._last_range = clean
            if self._settings is not None:
                self._settings.set(self._RANGE_SETTING, clean)
            self.lastRangeChanged.emit()
        creds = Credentials(username=username, password=password)
        self._svc.start(
            range_spec, creds,
            on_found=lambda _info: _safe_emit(self, "_destroyed", "_foundFromEngine"),
            on_progress=lambda s, f, t: _safe_emit(self, "_destroyed", "_progressFromEngine", s, f, t),
            on_done=lambda _stats: _safe_emit(self, "_destroyed", "_doneFromEngine"),
            on_error=lambda msg: _safe_emit(self, "_destroyed", "_errorFromEngine", msg),
        )

    @pyqtSlot()
    def cancel(self) -> None:
        self._svc.cancel()

    @pyqtSlot(int)
    def refreshServerDisks(self, server_id: int) -> None:
        """Re-poll one host's Redfish Storage using vault-stored credentials."""
        sid = int(server_id)
        self._svc.refresh_disks_for(
            sid,
            on_done=lambda ok, msg: _safe_emit(
                self, "_destroyed", "_disksRefreshedFromEngine", sid, ok, msg),
        )

    @pyqtSlot(int, str, str)
    def refreshServerDisksWithCreds(self, server_id: int,
                                    username: str, password: str) -> None:
        """Re-poll one host's Redfish Storage using the supplied credentials.

        Used by the Provision page inline credential prompt so the operator
        doesn't have to navigate to Settings → Credentials before fetching
        disk info for a server with no saved vault entry.
        """
        sid = int(server_id)
        self._svc.refresh_disks_for(
            sid,
            username=username or "",
            password=password or "",
            on_done=lambda ok, msg: _safe_emit(
                self, "_destroyed", "_disksRefreshedFromEngine", sid, ok, msg),
        )

    @pyqtSlot(int, bool, str)
    def _on_disks_refreshed_main(self, sid: int, ok: bool, msg: str) -> None:
        self._fleet.refresh()
        self.disksRefreshed.emit(sid, ok, msg)

    @pyqtSlot()
    def _on_found_main(self) -> None:
        self._fleet.refresh()

    @pyqtSlot(int, int, int)
    def _on_progress_main(self, scanned: int, found: int, total: int) -> None:
        self._scanned, self._found, self._total = scanned, found, total
        self.progressChanged.emit()

    @pyqtSlot()
    def _on_done_main(self) -> None:
        self._scanning = False
        self.isScanningChanged.emit()
        self._fleet.refresh()
        self._event_log.refresh()
        self.finished.emit()

    @pyqtSlot(str)
    def _on_error_main(self, msg: str) -> None:
        self._error = msg
        self._scanning = False
        self.isScanningChanged.emit()
        self.errorChanged.emit()


# ---- Updates (firmware job controller) ----

_TASK_FIELDS = ["id", "host", "oem", "change", "state", "progress", "message"]
_JOB_FIELDS = ["id", "fw_type", "state", "total", "succeeded", "failed", "created_at"]


class UpdateController(QObject):
    isRunningChanged = pyqtSignal()
    statsChanged = pyqtSignal()
    errorChanged = pyqtSignal()

    _taskUpdateFromEngine = pyqtSignal(int, str, int, str)
    _jobDoneFromEngine = pyqtSignal(int, int, str)        # succeeded, failed, state
    _errorFromEngine = pyqtSignal(str)

    def __init__(
        self,
        orchestrator: UpdateOrchestrator,
        jobs: JobsService,
        fleet: InventoryController,
        event_log: ActivityController,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._orch = orchestrator
        self._jobs = jobs
        self._fleet = fleet
        self._event_log = event_log
        self._running = False
        self._succeeded = 0
        self._failed = 0
        self._total = 0
        self._current_job_id = 0
        self._error = ""

        self._tasks = ObjectListModel(_TASK_FIELDS)
        # Lazy-init the jobs model — populated on first QML access via
        # ``jobsModel`` property OR explicit ``refreshJobs()``. Eagerly
        # loading at controller construction would do a synchronous DB
        # read inside the Qt main thread → splash freeze on large job
        # tables (audit found this with hundreds of historical jobs).
        self._jobs_model = ObjectListModel(_JOB_FIELDS)
        self._jobs_loaded = False
        # Set by Container.shutdown to silence engine-thread callbacks after teardown.
        self._destroyed = False
        self._taskUpdateFromEngine.connect(self._on_task_update_main)
        self._jobDoneFromEngine.connect(self._on_job_done_main)
        self._errorFromEngine.connect(self._on_error_main)

    def mark_destroyed(self) -> None:
        self._destroyed = True

    def _ensure_jobs_loaded(self) -> None:
        if not self._jobs_loaded:
            try:
                rows = self._jobs.list_jobs(limit=20)
            except Exception as exc:
                log.warning("jobsModel lazy-load failed: %s", exc)
                rows = []
            self._jobs_model.reset(rows)
            self._jobs_loaded = True

    # ---- QML properties ----
    @pyqtProperty(QObject, constant=True)
    def model(self) -> QObject:
        return self._tasks

    @pyqtProperty(QObject, constant=True)
    def jobsModel(self) -> QObject:
        # First access triggers the DB read. Subsequent QML reads return
        # the cached model object (constant property).
        self._ensure_jobs_loaded()
        return self._jobs_model

    @pyqtSlot()
    def refreshJobs(self) -> None:
        self._jobs_model.reset(self._jobs.list_jobs(limit=20))

    @pyqtProperty(bool, notify=isRunningChanged)
    def isRunning(self) -> bool:
        return self._running

    @pyqtProperty(int, notify=statsChanged)
    def total(self) -> int:
        return self._total

    @pyqtProperty(int, notify=statsChanged)
    def succeeded(self) -> int:
        return self._succeeded

    @pyqtProperty(int, notify=statsChanged)
    def failed(self) -> int:
        return self._failed

    @pyqtProperty(int, notify=statsChanged)
    def currentJobId(self) -> int:
        return self._current_job_id

    @pyqtProperty(str, notify=errorChanged)
    def error(self) -> str:
        return self._error

    # ---- QML actions ----
    @pyqtSlot("QVariantList", int, int, str, str, bool)
    def start(self, server_ids, firmware_id: int, concurrency: int,
              username: str, password: str, apply_now: bool = False) -> None:
        # Breadcrumb at debug — no credential material in the log file.
        log.debug("UpdateController.start: server_ids=%r firmware_id=%r "
                  "concurrency=%r apply_now=%r",
                  list(server_ids), firmware_id, concurrency, apply_now)

        if self._running:
            log.debug("  -> rejected: a job is already running")
            return
        ids = [int(x) for x in server_ids if x is not None]
        if not ids:
            self._error = "No servers selected"
            self.errorChanged.emit()
            log.warning("  -> rejected: server_ids was empty")
            return

        self._error = ""
        self.errorChanged.emit()

        job_id = self._orch.start_job(
            server_ids=ids,
            firmware_id=int(firmware_id),
            concurrency=max(1, int(concurrency)),
            username=username,
            password=password,
            apply_now=bool(apply_now),
            on_task_update=lambda tid, st, pct, msg:
                _safe_emit(self, "_destroyed", "_taskUpdateFromEngine", tid, st, pct, msg),
            on_job_done=lambda stats:
                _safe_emit(self, "_destroyed", "_jobDoneFromEngine",
                           stats.get("succeeded", 0), stats.get("failed", 0), stats.get("state", "")),
            on_error=lambda m: _safe_emit(self, "_destroyed", "_errorFromEngine", m),
        )
        if job_id is None:
            return

        self._current_job_id = job_id
        self._running = True
        self._succeeded = 0
        self._failed = 0

        # Seed the live task model from DB so the UI shows rows immediately.
        rows = []
        for t in self._jobs.list_tasks(job_id):
            change = f"{t.get('firmware_type', '?')} {t.get('firmware_version', '?')}"
            rows.append({
                "id": t["id"],
                "host": t.get("server_hostname") or t.get("server_ip") or "?",
                "oem": (t.get("server_oem") or "").title(),
                "change": change,
                "state": t.get("state", "queued"),
                "progress": t.get("progress", 0),
                "message": t.get("message") or "",
            })
        self._tasks.reset(rows)
        self._total = len(rows)
        self.isRunningChanged.emit()
        self.statsChanged.emit()

    @pyqtSlot()
    def cancel(self) -> None:
        self._orch.cancel()

    # ---- internal main-thread slots ----
    @pyqtSlot(int, str, int, str)
    def _on_task_update_main(self, task_id: int, state: str, pct: int, message: str) -> None:
        self._tasks.update_by_id("id", task_id, state=state, progress=pct, message=message)

    @pyqtSlot(int, int, str)
    def _on_job_done_main(self, succeeded: int, failed: int, _state: str) -> None:
        self._running = False
        self._succeeded = succeeded
        self._failed = failed
        self.isRunningChanged.emit()
        self.statsChanged.emit()
        self._fleet.refresh()       # versions/status may have changed
        self._event_log.refresh()   # activity rows from the job
        self._jobs_model.reset(self._jobs.list_jobs(limit=20))

    @pyqtSlot(str)
    def _on_error_main(self, msg: str) -> None:
        self._error = msg
        self._running = False
        self.errorChanged.emit()
        self.isRunningChanged.emit()


class ReportsController(QObject):
    statusChanged = pyqtSignal()

    def __init__(self, service: ReportsService, activity: ActivityService,
                 app_controller=None, parent=None) -> None:
        super().__init__(parent)
        self._svc = service
        self._activity = activity
        self._app = app_controller
        self._last_status = ""

    def _toast(self, msg: str, kind: str) -> None:
        if self._app is not None:
            self._app.showToast(msg, kind)

    @pyqtProperty(str, notify=statusChanged)
    def lastStatus(self) -> str:
        return self._last_status

    @pyqtSlot(result=str)
    def exportInventory(self) -> str:
        try:
            path = self._svc.write_inventory_snapshot()
            self._activity.log("info", "system", f"Inventory report saved: {path.name}")
            self._last_status = f"Saved {path.name}"
            self.statusChanged.emit()
            self._toast(f"Saved {path.name}", "success")
            self._open_in_default_app(path)
            return str(path)
        except Exception as exc:
            self._activity.log("error", "system", f"Inventory report failed: {exc}")
            self._last_status = f"Error: {exc}"
            self.statusChanged.emit()
            self._toast(f"Export failed: {exc}", "error")
            return ""

    @pyqtSlot(int, result=str)
    def exportJob(self, job_id: int) -> str:
        try:
            path = self._svc.write_update_job_report(int(job_id))
            self._activity.log("info", "system", f"Job #{job_id} report saved: {path.name}")
            self._last_status = f"Saved {path.name}"
            self.statusChanged.emit()
            self._toast(f"Saved {path.name}", "success")
            self._open_in_default_app(path)
            return str(path)
        except Exception as exc:
            self._activity.log("error", "system", f"Job #{job_id} report failed: {exc}")
            self._last_status = f"Error: {exc}"
            self.statusChanged.emit()
            self._toast(f"Export failed: {exc}", "error")
            return ""

    @pyqtSlot(result=str)
    def exportAuditLog(self) -> str:
        try:
            path = self._svc.export_audit_log()
            if path is None:
                self._toast("No audit log yet — nothing to export", "warning")
                return ""
            self._activity.log("info", "system", f"Audit log exported: {path.name}")
            self._last_status = f"Saved {path.name}"
            self.statusChanged.emit()
            self._toast(f"Saved {path.name}", "success")
            self._open_in_default_app(path)
            return str(path)
        except Exception as exc:
            self._activity.log("error", "system", f"Audit export failed: {exc}")
            self._toast(f"Audit export failed: {exc}", "error")
            return ""

    @staticmethod
    def _open_in_default_app(path) -> None:
        try:
            from PyQt6.QtCore import QUrl
            from PyQt6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except Exception:
            pass


class CredentialsController(QObject):
    """Vault-backed credential management exposed to QML as ``Credentials``."""
    changed = pyqtSignal()

    def __init__(
        self,
        service: CredentialsService,
        activity: ActivityService,
        app_controller=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._svc = service
        self._activity = activity
        self._app = app_controller
        self._model = ObjectListModel(_CRED_FIELDS, service.list())

    @pyqtProperty(QObject, constant=True)
    def model(self) -> QObject:
        return self._model

    @pyqtProperty(int, notify=changed)
    def count(self) -> int:
        return self._svc.count()

    @pyqtProperty(bool, constant=True)
    def isVaultSecure(self) -> bool:
        return self._svc.vault_is_secure

    @pyqtSlot()
    def refresh(self) -> None:
        self._model.reset(self._svc.list())
        self.changed.emit()

    @pyqtSlot(str, str, str, str)
    def add(self, label: str, username: str, password: str, scope: str) -> None:
        if not username:
            if self._app: self._app.showToast("Username required", "warning")
            return
        try:
            self._svc.add(
                label=label or "(unlabeled)",
                username=username,
                password=password,
                scope=scope or "default",
            )
            self._activity.log("info", "auth", f"Credential added (scope={scope or 'default'}, user={username})")
            self._model.reset(self._svc.list())
            self.changed.emit()
            if self._app: self._app.showToast(f"Saved credential for {username}", "success")
        except Exception as exc:
            log.exception("Credential add failed")
            self._activity.log("error", "auth", f"Credential add failed: {exc}")
            if self._app: self._app.showToast(f"Failed: {exc}", "error")

    @pyqtSlot(int)
    def remove(self, cred_id: int) -> None:
        try:
            self._svc.delete(int(cred_id))
            self._activity.log("info", "auth", f"Credential #{cred_id} removed")
            self._model.reset(self._svc.list())
            self.changed.emit()
            if self._app: self._app.showToast("Credential removed", "info")
        except Exception as exc:
            log.exception("Credential remove failed")
            if self._app: self._app.showToast(f"Failed: {exc}", "error")


class SettingsController(QObject):
    """Typed accessors for user-tunable settings, persisted via SettingsService.

    Exposed to QML as ``AppSettings`` (named to avoid the Settings.qml page type).
    """
    changed = pyqtSignal()

    # Keys + sensible defaults
    KEYS = {
        "update_concurrency":   ("int", 4,    1,  999),    # parallel updates
        "connect_timeout_s":    ("int", 10,   1,  120),    # Redfish connect/read seconds
        "operation_timeout_min":("int", 30,   1,  480),    # long-op (flash) ceiling
        "log_retention_days":   ("int", 90,   1,  365),    # retained app log days
    }

    def __init__(self, service: SettingsService, app_controller=None, parent=None) -> None:
        super().__init__(parent)
        self._svc = service
        self._app = app_controller

    def _int(self, key: str) -> int:
        _, default, _, _ = self.KEYS[key]
        return self._svc.get_int(key, default)

    def _set_int(self, key: str, value: int) -> None:
        # Toast intentionally not emitted here — the UI fires one summary toast
        # after a Save (which may set several keys at once).
        _, _, lo, hi = self.KEYS[key]
        value = max(lo, min(hi, int(value)))
        self._svc.set(key, value)
        self.changed.emit()

    # ---- generic accessors for QML pages that need to persist their
    # own page-local state (Provision wizard, Updates page, etc.) without
    # forcing every key through the typed KEYS map. Used for the
    # ``provision_last_*`` / ``updates_last_*`` family.
    @pyqtSlot(str, str, result=str)
    def getString(self, key: str, default: str = "") -> str:
        v = self._svc.get(key, default)
        return v if v is not None else default

    @pyqtSlot(str, str)
    def setString(self, key: str, value: str) -> None:
        self._svc.set(key, value)

    @pyqtSlot(str, int, result=int)
    def getInt(self, key: str, default: int = 0) -> int:
        return self._svc.get_int(key, default)

    @pyqtSlot(str, int)
    def setInt(self, key: str, value: int) -> None:
        self._svc.set(key, int(value))

    @pyqtSlot(str, bool, result=bool)
    def getBool(self, key: str, default: bool = False) -> bool:
        v = self._svc.get(key)
        if v is None:
            return default
        return str(v).lower() in ("1", "true", "yes", "on")

    @pyqtSlot(str, bool)
    def setBool(self, key: str, value: bool) -> None:
        self._svc.set(key, "1" if value else "0")

    # ---- QML properties ----
    @pyqtProperty(int, notify=changed)
    def updateConcurrency(self) -> int:
        return self._int("update_concurrency")

    @pyqtProperty(int, notify=changed)
    def connectTimeoutS(self) -> int:
        return self._int("connect_timeout_s")

    @pyqtProperty(int, notify=changed)
    def operationTimeoutMin(self) -> int:
        return self._int("operation_timeout_min")

    @pyqtProperty(int, notify=changed)
    def logRetentionDays(self) -> int:
        return self._int("log_retention_days")

    # ---- QML setters ----
    @pyqtSlot(int)
    def setUpdateConcurrency(self, v: int) -> None:
        self._set_int("update_concurrency", v)

    @pyqtSlot(int)
    def setConnectTimeoutS(self, v: int) -> None:
        self._set_int("connect_timeout_s", v)

    @pyqtSlot(int)
    def setOperationTimeoutMin(self, v: int) -> None:
        self._set_int("operation_timeout_min", v)

    @pyqtSlot(int)
    def setLogRetentionDays(self, v: int) -> None:
        self._set_int("log_retention_days", v)
