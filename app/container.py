"""Composition root (manual dependency injection)."""
from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtGui import QGuiApplication

from app import config
from core.engine.async_engine import AsyncEngine
from core.plugins_api.registry import PluginRegistry
from core.security.credential_vault import CredentialVault
from core.services.activity_service import ActivityService
from core.services.credentials_service import CredentialsService
from core.services.discovery_service import DiscoveryService
from core.services.firmware_service import FirmwareService
from core.services.inventory_service import InventoryService
from core.services.iso_service import IsoService
from core.services.power_service import PowerService
from core.services.provisioning_orchestrator import ProvisioningOrchestrator
from core.services.jobs_service import JobsService
from core.services.reports_service import ReportsService
from core.services.settings_service import SettingsService
from core.services.update_orchestrator import UpdateOrchestrator
from data.database import Database
from data.repositories.activity_repo import ActivityRepo
from data.repositories.credentials_repo import CredentialsRepo
from data.repositories.disk_profile_repo import DiskProfileRepo
from data.repositories.firmware_repo import FirmwareRepo
from data.repositories.iso_repo import IsoRepo
from data.repositories.jobs_repo import JobsRepo
from data.repositories.provisioning_repo import ProvisioningRepo
from data.repositories.servers_repo import ServersRepo
from data.repositories.settings_repo import SettingsRepo
from ui.bridge.app_controller import AppController
from ui.bridge.controllers import (
    ActivityController,
    CredentialsController,
    DiscoveryController,
    FirmwareController,
    InventoryController,
    IsoController,
    PowerController,
    ProvisioningController,
    ReportsController,
    SettingsController,
    UpdateController,
)
from ui.bridge.theme_controller import ThemeController

log = logging.getLogger(__name__)


class Container:
    def __init__(
        self,
        app: QGuiApplication,
        font_family: str = "Segoe UI",
        mono_family: str = "Consolas",
    ) -> None:
        self.app = app
        self.config = config

        # --- Data layer ---
        self.db = Database()
        conn = self.db.connection
        wlock = self.db.write_lock                  # serialize cross-thread writes
        self.servers_repo = ServersRepo(conn, wlock)
        self.firmware_repo = FirmwareRepo(conn, wlock)
        self.iso_repo = IsoRepo(conn, wlock)
        self.disk_profile_repo = DiskProfileRepo(conn, wlock)
        self.provisioning_repo = ProvisioningRepo(conn, wlock)
        self.activity_repo = ActivityRepo(conn, wlock)
        self.settings_repo = SettingsRepo(conn, wlock)
        self.jobs_repo = JobsRepo(conn, wlock)
        self.credentials_repo = CredentialsRepo(conn, wlock)
        # NOTE: first-run seed is no longer applied automatically.
        # For a populated demo DB (screenshots, walkthroughs), call
        # `data.seed.seed_if_empty(...)` manually — see data/seed.py.

        # --- Security ---
        self.vault = CredentialVault()

        # --- Async engine ---
        self.engine = AsyncEngine()
        self.engine.start()

        # --- Plugin registry ---
        self.plugins = PluginRegistry()
        self.plugins.load_bundled(Path(__file__).resolve().parents[1] / "plugins")
        self.plugins.load_entry_points()

        # --- Service layer (Qt-agnostic) ---
        self.settings = SettingsService(self.settings_repo)
        self.inventory = InventoryService(self.servers_repo)
        self.firmware = FirmwareService(self.firmware_repo)
        self.iso = IsoService(self.iso_repo)
        self.activity = ActivityService(self.activity_repo)
        self.jobs = JobsService(self.jobs_repo)
        self.credentials = CredentialsService(self.credentials_repo, self.vault)
        self.reports = ReportsService(self.inventory, self.jobs)
        self.discovery = DiscoveryService(
            self.engine, self.plugins, self.inventory, self.activity,
            credentials=self.credentials,   # vault-backed creds for on-demand re-poll
        )
        self.update_orchestrator = UpdateOrchestrator(
            self.engine, self.jobs_repo, self.servers_repo, self.firmware_repo,
            self.plugins, self.activity,
            credentials=self.credentials,   # enables vault-backed BMC creds fallback
        )
        # Operator-initiated power control (on/off/restart/cycle) — vault-backed
        # BMC creds, same plugin layer as discovery/update.
        self.power = PowerService(
            self.engine, self.plugins, self.servers_repo, self.activity,
            credentials=self.credentials,
        )
        # ProvisioningOrchestrator is constructed FIRST so the FileServer
        # lambda can safely call self.provisioning_orchestrator at runtime
        # (the attribute exists by the time any BMC connects).
        self.provisioning_orchestrator = ProvisioningOrchestrator(
            self.engine,
            self.provisioning_repo,
            self.servers_repo,
            self.iso_repo,
            self.disk_profile_repo,
            self.plugins,
            self.activity,
            credentials=self.credentials,
            file_server=None,               # set below after FileServer is constructed
        )

        # Embedded HTTP server for BMC-side ISO + autoinstall fetches.
        from core.services.file_server import FileServer
        self.file_server = FileServer(
            iso_dir=config.iso_dir(),
            autoinstall_provider=lambda token: self.provisioning_orchestrator.autoinstall_provider(token),
        )
        # Wire the file server back into the orchestrator so signed URLs work.
        self.provisioning_orchestrator._file_server = self.file_server

        # --- Bridge layer (exposed to QML) ---
        self.theme = ThemeController(
            app, font_family=font_family, mono_family=mono_family, settings=self.settings
        )
        self.app_controller = AppController()
        self.fleet = InventoryController(self.inventory, plugins=self.plugins)
        self.fw_controller = FirmwareController(
            self.firmware, self.activity, self.app_controller, engine=self.engine
        )
        self.iso_controller = IsoController(
            self.iso, self.activity, self.app_controller, engine=self.engine
        )
        self.event_log = ActivityController(self.activity)
        self.discovery_controller = DiscoveryController(
            self.discovery, self.fleet, self.event_log,
            settings=self.settings,   # persist last-scanned range across restarts
        )
        self.update_controller = UpdateController(
            self.update_orchestrator, self.jobs, self.fleet, self.event_log
        )
        self.power_controller = PowerController(
            self.power, self.fleet, self.event_log, self.app_controller
        )
        self.provisioning_controller = ProvisioningController(
            self.provisioning_orchestrator,
            self.provisioning_repo,
            self.event_log,
            self.fleet,
        )
        self.reports_controller = ReportsController(self.reports, self.activity, self.app_controller)
        self.credentials_controller = CredentialsController(
            self.credentials, self.activity, self.app_controller
        )
        self.settings_controller = SettingsController(self.settings, self.app_controller)

        # Recover any tasks that were mid-flash when the app last quit, and
        # settle any provisioning job/task stranded by a restart mid-install.
        self.update_orchestrator.resume_interrupted()
        self.provisioning_orchestrator.resume_interrupted()

        # Boot the embedded HTTP server on the engine loop.
        # ``bmc_http_bind`` restricts the listening interface (default all).
        # ``bmc_http_advertise_host`` is the IP the BMC will use in the signed
        # ISO URL — MUST be an address the BMC can reach. When unset (the
        # common case) we leave ``public_base_url=None`` so FileServer.start()
        # can auto-detect the management NIC via its outbound-interface probe.
        bind_host = self.settings.get("bmc_http_bind", "0.0.0.0") or "0.0.0.0"
        bind_port = self.settings.get_int("bmc_http_port", 8443)
        advertise_host = self.settings.get("bmc_http_advertise_host") or ""
        started = False
        attempted_ports = [bind_port]
        if bind_port > 0:
            attempted_ports.extend(bind_port + i for i in range(1, 6))
        last_exc: Exception | None = None
        for port in attempted_ports:
            advertise_url: str | None = (
                f"http://{advertise_host}:{port}" if advertise_host else None
            )
            try:
                fut = self.engine.submit(
                    self.file_server.start(host=bind_host, port=port,
                                           public_base_url=advertise_url)
                )
                fut.result(timeout=5)
                if port != bind_port:
                    log.warning(
                        "File server port %s unavailable; using fallback port %s",
                        bind_port, port,
                    )
                started = True
                break
            except OSError as exc:
                last_exc = exc
                log.warning(
                    "File server failed to bind %s:%s; trying next port",
                    bind_host, port,
                )
            except Exception as exc:
                last_exc = exc
                break
        if not started:
            # Clear the orchestrator back-reference BEFORE logging so a
            # concurrent provisioning call can't grab a half-initialized
            # FileServer mid-shutdown. The None-guard in _real_task then
            # surfaces a clean "no file server" error to the operator.
            self.provisioning_orchestrator._file_server = None
            self.file_server = None
            log.exception(
                "File server failed to start on %s:%s after trying %s",
                bind_host, bind_port, attempted_ports,
                exc_info=last_exc,
            )

    def shutdown(self) -> None:
        # Silence engine-thread callbacks before they fire on QObjects
        # that Qt is about to destroy.
        try:
            self.discovery_controller.mark_destroyed()
            self.update_controller.mark_destroyed()
            self.provisioning_controller.mark_destroyed()
            self.power_controller.mark_destroyed()
            self.fw_controller.mark_destroyed()
            self.iso_controller.mark_destroyed()
        except Exception:
            log.exception("Controller mark_destroyed failed")
        # Stop the file server on the engine loop before stopping the loop.
        if self.file_server is not None:
            try:
                fut = self.engine.submit(self.file_server.stop())
                fut.result(timeout=2)
            except Exception:
                log.exception("File server stop failed")
        try:
            self.engine.stop()
        except Exception:
            log.exception("AsyncEngine shutdown failed")
        try:
            self.db.close()
        except Exception:
            log.exception("Database close failed")
