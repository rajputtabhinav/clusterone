# ClusterOne Full QA and Bug Audit Prompt

Use this prompt with a coding or QA agent to perform a complete feature test and bug audit of ClusterOne.

```text
You are a senior QA engineer and code reviewer auditing the ClusterOne codebase.

Repository root:
C:\Users\asus\Desktop\ClusterOne

Product summary:
ClusterOne is a Python 3.12+ / PyQt6 / QML Windows desktop app for server discovery, Redfish/BMC firmware lifecycle management, power control, and bulk OS provisioning. It uses a Qt-agnostic Python service core, SQLite WAL persistence, DPAPI credential vaulting on Windows, an embedded aiohttp file server for ISO/autoinstall delivery, and a plugin system for Generic Redfish, Supermicro, Dell, HPE, Lenovo, ASRock, ASUS, Gigabyte, and MSI.

Scope rules:
- Treat first-party source as truth: app, core, data, plugins, ui, tests, tools, packaging, docs.
- Do not treat build, dist, .venv, __pycache__, .pytest_cache, or reference as source of truth.
- Do not perform a real firmware flash, power action, disk wipe, or OS install on production hardware.
- If real lab hardware is used, require explicit operator confirmation and document device/IP/firmware.
- Prefer simulator-backed tests first.
- Use a throwaway CLUSTERONE_DATA_DIR for manual app tests so existing user data is not modified.
- Report every bug with severity, exact reproduction, expected result, actual result, logs/screenshots if applicable, and file/line references.

Current baseline from this repo read:
- Unit tests: 178 passed with:
  $env:CLUSTERONE_ALLOW_PLAINTEXT_VAULT='1'
  $env:QT_QPA_PLATFORM='offscreen'
  .\.venv\Scripts\python.exe -m pytest tests\unit -q
- Integration tests: 1 passed with:
  .\.venv\Scripts\python.exe -m pytest tests\integration -q
- QML smoke: root loaded and warnings were 0, but startup logged a real file-server issue when port 8443 was already in use:
  OSError [WinError 10048] bind failed on 0.0.0.0:8443
  Verify whether the UI surfaces this and whether provisioning is clearly disabled or recoverable.

Mandatory automated checks:
1. Run the unit test suite.
2. Run the integration suite.
3. Run tests\smoke_qml.py with QT_QPA_PLATFORM=offscreen.
4. Run the whole pytest suite, not only unit/integration slices:
   .\.venv\Scripts\python.exe -m pytest -q
5. Run ruff if installed, or install the dev extra and run:
   .\.venv\Scripts\python.exe -m ruff check app core data plugins ui tests tools
6. Search for critical markers:
   rg -n "TODO|FIXME|BUG|HACK|XXX|pass$|NotImplemented|verify=False|shell=True|except Exception" app core data ui plugins tests docs
7. Run a QML import/load check and fail on any QML TypeError, binding loop, undefined property, or missing context property.
8. Add or propose missing tests for any confirmed bug.

Simulator setup:
1. Start simulator:
   .\.venv\Scripts\python.exe -m tests.sim.redfish_sim
2. Optional auth simulator:
   .\.venv\Scripts\python.exe -m tests.sim.redfish_sim --auth admin:password
3. In the app discovery dialog, use:
   http://127.0.0.1:8000-8007
4. Expected simulated fleet includes Supermicro, Dell, HPE, Lenovo, Gigabyte, ASRock Rack, ASUS, and MSI.

Manual app launch:
1. Use a clean data dir:
   $env:CLUSTERONE_DATA_DIR="$env:TEMP\ClusterOne-QA"
   $env:CLUSTERONE_ALLOW_PLAINTEXT_VAULT='1'
   .\.venv\Scripts\python.exe -m app
2. Verify logs under:
   $env:CLUSTERONE_DATA_DIR\logs\clusterone.log
   $env:CLUSTERONE_DATA_DIR\logs\audit.log
3. Verify DB under:
   $env:CLUSTERONE_DATA_DIR\db\clusterone.db

Feature map and test checklist:

1. Startup, app shell, and persistence
- App launches without QML errors.
- Splash overlay fades and stops intercepting clicks.
- Frameless window controls work: minimize, maximize/restore, close, resize edges/corners.
- Window geometry persists across restart except maximized state.
- Theme defaults to dark, switches Light/Dark/System, and persists.
- Top status ribbon updates from Fleet counts and Discovery.lastRange.
- Click outside a text field clears focus and commits the edit.
- Toasts appear above sticky save bars when needed.
- Confirm no command palette bug: docs claim Ctrl+K opens command palette, but no obvious QML command palette implementation was found. Press Ctrl+K and file doc or implementation bug if missing.

2. Navigation and QML context wiring
- Nav rail has 8 pages: Dashboard, Inventory, Updates, Provision, Firmware Library, ISO Library, Activity, Settings.
- App.navigateTo, App.openDiscovery, App.openAddFirmware, App.openAddIso, App.openAddCredential, App.exportInventory, App.exportAuditLog, and App.cancelRunningJob route correctly.
- Regression check: Updates.qml uses a context object named Updates while the page type is also Updates.qml. Verify Updates.isRunning, Updates.start, Updates.cancel, Updates.model, and Updates.jobsModel resolve to the Python UpdateController, not the QML page/component.
- Verify every QML call to Python slots has the correct argument count and types.

3. Discovery and inventory
- Test range parser syntaxes:
  - 10.10.10.5
  - 10.10.10.1-254
  - 10.10.10.1-10.10.10.254
  - 10.10.10.0/24
  - 127.0.0.1:8000-8007
  - http://127.0.0.1:8000-8007
  - comma-separated mixed targets
  - invalid ranges and reversed ranges
- Discovery dialog validates empty/invalid input and displays useful errors.
- Simulator discovery streams rows live, updates progress, records activity, and updates Dashboard/Inventory counts.
- Discovery cancel stops cleanly and does not leave scanning state stuck.
- Inventory search filters IP, hostname, OEM, model, and serial.
- Selection toggles work, Select All works, deletion removes selected IDs from Fleet.selectedIds.
- Manual OEM override is required before flash/provision/power; picker lists loaded vendor plugins and Generic Redfish last.
- OEM override persists across rediscovery.
- If the same IP is rediscovered with a different serial, override and disk cache must be cleared and activity should warn.
- Server detail drawer opens, closes, displays identity/firmware/status, and supports OEM selection/removal.
- TLS TOFU capture stores fingerprint for HTTPS hosts and logs warning on fingerprint change.

4. Credentials and security
- Settings can add and delete credentials.
- Credential metadata displays label, username, and scope but never password.
- Scope precedence works: host:<ip> beats oem:<key>, which beats default.
- If a more-specific credential cannot decrypt, app must not silently fall back to a less-specific credential.
- On Windows with pywin32, DPAPI is used and Credentials.isVaultSecure is true.
- Without DPAPI, encryption refuses unless CLUSTERONE_ALLOW_PLAINTEXT_VAULT=1.
- No plaintext BMC passwords appear in SQLite, app logs, audit logs, reports, exceptions, or toasts.
- Add malformed scopes and empty passwords; verify behavior is safe and documented.

5. Firmware Library
- Add BMC, BIOS, and HGX firmware files.
- Hash/copy must happen off the GUI thread; UI should remain responsive with large files.
- SHA-256 dedupe works and shows a clear duplicate toast.
- Imported files are copied to CLUSTERONE_DATA_DIR\firmware\<sha>\.
- Delete removes DB row and on-disk blob only under the firmware dir.
- Import rejects missing paths and reports a useful error.
- Metadata should be normalized: trim OEM/version/model, normalize OEM aliases, and avoid storing trailing spaces.
- Regression check: missing firmware blobs referenced by DB should be surfaced in UI and blocked before flash.

6. ISO Library
- Add ISO/img files for rocky, rhel, ubuntu, esxi, windows.
- Hash/copy happens off the GUI thread.
- SHA dedupe works.
- Delete removes DB row and on-disk blob only under the ISO dir.
- Large file import should not freeze QML.
- ISO selection persists on Provision page via AppSettings.

7. Updates / firmware flashing workflow
- Starting a job requires selected servers, selected firmware, saved credentials, valid firmware blob, and manual OEM selection per server.
- UI gates should produce actionable toasts before calling backend.
- Verify docs mismatch: User Guide promises Dry-run mode, but Updates.qml appears to only expose applyNow and starts real update jobs. File as bug if no safe dry-run exists in UI/backend.
- Verify applyNow toggle persists and only appears for BIOS.
- Firmware dropdown search/selection/restoration works after restart.
- Target chips include/exclude servers correctly.
- Job model seeds live tasks immediately after start.
- Progress states render: queued, precheck, uploading, flashing, rebooting, verifying, completed, failed.
- Cancel updates UI and DB consistently.
- Recent jobs lazy-load, refresh after job completion, and export reports.
- SHA preflight rejects missing, changed, or seed/demo firmware.
- Compatibility rejects OEM/model/version mismatch and same-version updates.
- Compatibility must support aliases/rebadges such as Tyrone Systems mapping to Supermicro when the selected plugin is Supermicro.
- Preflight checks UpdateService, upload URI, FirmwareInventory target, and active tasks.
- Regression bug to verify/fix: core/services/update_orchestrator.py _run_job references cancel_evt during finalization even though cancel_evt is assigned inside run_one. This can crash with NameError, especially for empty task lists or after gather. Add a test that runs _run_job end-to-end.
- Regression bug to verify/fix: update_tasks.redfish_task exists and resume_interrupted depends on it, but plugin Result does not appear to return task_uri and orchestrator does not persist it. Closing app mid-flash may mark tasks failed instead of resuming.
- Regression bug to verify/fix: active-task preflight currently only checks whether "update" appears in TaskService member URI; real task URIs often do not. It should fetch task details and inspect TaskState/Name/Payload/Messages.
- Regression bug to verify/fix: CapabilityMap selection should ignore FirmwareInventory members with Updateable=False for BIOS/BMC/HGX.
- Verify RedfishClient absolute URL handling for TaskMonitor Location headers.
- Verify HPE iLO multipart upload sends required auth/session data.

8. Provisioning workflow
- Provision page lists all servers without requiring Inventory selection.
- ISO picker works and stores provision_last_iso_id.
- Cached drive inventory displays per host.
- Refresh disk button uses vault credentials; inline credentials override vault for refresh.
- If no OEM is selected, disk refresh and provisioning should fail with clear message.
- Disk resolver strategies work:
  first_nvme, first_m2, smallest_ssd, largest_disk, smallest_disk,
  by_size:480GB, by_size:480GB+/-5%, by_model:INTEL*, by_slot:0,
  by_serial:SN12345, manual_per_host.
- Ambiguous/unmatched/requires_override states are visible and actionable.
- Per-host drive override wins over profile strategy.
- WIPE confirmation requires typing WIPE and records confirm_token for audit.
- Autoinstall renderer refuses unsafe disk targets when no serial or valid Linux device handle exists.
- Rocky/RHEL kickstart and Ubuntu cloud-init templates render correct hostname, disk, serial targeting, and root password/SSH keys.
- FileServer signed ISO/autoinstall URLs reject tampering and expiry.
- FileServer supports range requests for ISO fetches.
- Port conflict on 8443 must be handled: UI should expose or document bmc_http_bind, bmc_http_port, and bmc_http_advertise_host, and provisioning should fail clearly if file server is not running.
- Virtual media mount/eject/set boot/power cycle path should use plugin capability map and not hardcoded paths where capability data exists.
- Restart during provisioning should mark stranded tasks/jobs failed with clear audit trail because in-memory signed URLs/configs cannot be resumed.

9. Power control
- Drawer power buttons ask for confirmation.
- Actions: Power On, Shut Down, Force Off, Restart, Graceful Restart if exposed, Power Cycle if exposed.
- Manual OEM override is required; no override should produce a clear error.
- Credential precedence mirrors update path.
- Results refresh Fleet model and drawer power state.
- Verify reset type selection respects BMC AllowableValues and never silently maps graceful actions to destructive force actions except for explicit force_off.

10. Reports and audit
- Dashboard export writes inventory DOCX to Documents\ClusterOne and opens it.
- Recent job export writes job DOCX and includes server, OEM, firmware, before/after, PASS/FAIL, notes.
- Missing python-docx produces actionable error only on export.
- Audit log export handles missing audit.log gracefully.
- Activity page refreshes after discovery, credential changes, firmware/ISO import/delete, update/provision/power/report actions.
- Log sanitizer prevents newline/control-character log injection.

11. Settings
- Appearance theme segmented control works.
- Credentials section reflects DPAPI vs dev fallback.
- Number settings clamp and persist:
  update_concurrency 1..999
  connect_timeout_s 1..120
  operation_timeout_min 1..480
  log_retention_days 1..365
- Dirty state, Save Changes, Discard, and "Saved" feedback all work.
- Settings page should expose or document BMC file server bind/port/advertise settings used by Container; if not, file a usability/configuration bug.

12. Plugin system and OEM behavior
- Registry loads every bundled manifest and entry point plugins without crashing.
- Plugin match scores are deterministic with priority/key tie-breaks.
- Manual-only routing via PluginRegistry.resolve requires override_key and returns None when missing/unknown.
- Generic Redfish:
  discovery, validation, BMC/BIOS/HGX update, storage, virtual media, boot, power.
- AMI MegaRAC family:
  Supermicro, ASRock, ASUS, Gigabyte, MSI inject correct ImageType OEM parameters.
- Dell:
  empty Targets flow for iDRAC.
- HPE:
  HttpPushUri multipart path, compsig requirement, Oem.Hpe.State polling, eject-before-insert.
- Lenovo:
  XCC target selection and no OEM parameters.
- HGX:
  active-bank HGX components only, no Image2/backup/recovery, bundle vs per-component filename matching, no AMI ImageType for HGX.
- Storage parsing:
  skip absent/empty bays, do not fabricate Linux device names from Redfish bay indexes, disambiguate duplicate names.

13. Redfish client and network behavior
- Session auth captures X-Auth-Token and session URI.
- Requests use token after login and avoid repeated Basic auth where possible.
- 401 triggers exactly one re-login retry.
- close() deletes Redfish session.
- TLS fingerprint pinning works and warns/fails on mismatch.
- Retry-After parsing handles seconds, HTTP-date, invalid, negative, inf/nan, and caps large values.
- post_json/post_multipart stream large files without loading whole firmware into memory.
- Absolute and relative URLs both work.
- Timeouts and transient BMC disconnects surface actionable errors.

14. Persistence and migrations
- Fresh DB applies migrations 0001 through 0005 once.
- schema_version current version is max applied migration, not first row.
- Reopen does not rerun migrations or wipe disk cache.
- WAL, foreign_keys, synchronous NORMAL, and busy_timeout are active.
- Deleting server/firmware/credential respects FK behavior and does not break historical jobs/tasks.
- Activity log remains append-only enough for audit purposes.
- Runtime data stays under CLUSTERONE_DATA_DIR or LOCALAPPDATA, not install dir.

15. Packaging, docs, and tools
- PyInstaller spec includes QML files, plugins, manifests, templates, fonts if present, and runtime deps.
- Inno installer version metadata matches app/config.py.
- Packaged app resolves resources with config.resource_path.
- Tools are safe:
  tools/redfish_verify.py is read-only.
  tools/bmc_probe.py is read-only.
  tools/test_flash_path.py is dry-run/read-only until explicitly documented otherwise.
  tools/flash_now.py refuses real flash unless --commit FLASH is provided.
- Docs match implementation. Specifically verify User Guide claims about Ctrl+K, dry-run, resume-on-restart, page count, and file server configuration.

Bug report output format:
For each finding, use:
- ID: P0/P1/P2/P3-short-title
- Severity: P0 data loss/security/destructive or cannot use core workflow; P1 major workflow break; P2 correctness/usability/regression; P3 polish/docs.
- Area: UI, service, plugin, persistence, security, packaging, docs, tests.
- Files/lines:
- Reproduction:
- Expected:
- Actual:
- Evidence: command output, log excerpt, screenshot, DB row, or test name.
- Root cause:
- Suggested fix:
- Missing test to add:

Final deliverables:
1. Executive summary with pass/fail status.
2. Automated test results and exact commands.
3. Feature coverage matrix: tested, partially tested, not tested, blocked.
4. Findings ordered by severity.
5. Regression list from docs/FLASH_FAILURE_REPORT.md: mark each as fixed, still present, or superseded.
6. Recommended fix order.
7. New/updated test plan with priority.
8. Do not claim a feature works unless it was tested or there is direct code evidence.
```

