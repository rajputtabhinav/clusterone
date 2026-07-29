# ClusterOne Firmware Flash Failure Report

Date: 2026-06-06

Scope reviewed: first-party source under `app`, `core`, `data`, `plugins`, `ui`, `tools`, `tests`, local app logs, and the live ClusterOne SQLite DB under `%LOCALAPPDATA%\ClusterOne\db\clusterone.db`. Generated bundles (`build`, `dist`) and large reference projects were not treated as source of truth except where packaging warnings were useful.

## Executive Summary

ClusterOne currently has multiple independent blockers that explain why BIOS/BMC/HGX firmware flashing cannot work reliably.

The immediate UI failure in the local log is that the QML page `Updates.qml` calls `Updates.start(...)`, but `Updates` resolves to the QML page/component object rather than the Python `UpdateController`. The log shows `isRunning=undefined` and then `TypeError: Property 'start' of object Updates is not a function`, so the flash job never reaches Python in that run.

Even if that UI binding is fixed, the live DB has one BIOS firmware row whose `file_path` points to a missing file. The read-only tracer `tools/test_flash_path.py BIOS` confirms `file exists on disk: False` and exits with `FAIL: firmware blob missing - re-import the file`.

After that, the next likely backend blocker is OEM compatibility validation. The discovered server is stored as `oem='tyrone systems'`, while the firmware row is stored as `oem='supermicro '` with a trailing space. The Supermicro plugin intentionally matches Tyrone systems as rebadged Supermicro hardware, but inherited validation still compares raw strings exactly, so it would reject the firmware as an OEM mismatch.

There are also important reliability defects in the backend update flow: `_run_job` references a nested `cancel_evt` variable outside its scope during finalization, task monitor URIs are never persisted despite resume code depending on them, active-task preflight is too weak, QML smoke tests do not click the Flash path, and the capability map ignores `Updateable=False` when selecting firmware targets.

## Confirmed Runtime Evidence

### Local Server Row

From `%LOCALAPPDATA%\ClusterOne\db\clusterone.db`:

- Server id: `1`
- IP: `172.16.12.138`
- Manufacturer: `Tyrone Systems`
- Stored OEM: `tyrone systems`
- Model: `Super Server`
- BMC version: `09.05.07 beta`
- BIOS version: `BIOS Date: 03/05/2025 Ver 3.0a.V1`
- Redfish root: `https://172.16.12.138:443/redfish/v1/`
- TLS fingerprint is present.

### Local Firmware Row

Only one firmware row exists:

- Firmware id: `1`
- Type: `BIOS`
- OEM: `supermicro ` with trailing whitespace
- Version: `v6`
- File path: `C:\Users\asus\AppData\Local\ClusterOne\firmware\b2cc8b49771ffaf4518708e5bf237d5c9d6cee7fbc3803439c881a8d348cb79c\BIOS_H13DSH-1C84-NT00070T00_20250305_3.0a.V1_OEMsp 2.bin`
- File exists: `False`

The firmware directory under `%LOCALAPPDATA%\ClusterOne\firmware` is currently empty.

### Local Log Evidence

From `%LOCALAPPDATA%\ClusterOne\logs\clusterone.log`:

```text
2026-06-03 18:11:11,247 WARNING qml : ... Updates.qml Flash clicked: targetIds=[1] firmwareId=1 credsCount=1 applyNow=false isRunning=undefined
2026-06-03 18:11:11,251 WARNING qml : ... Updates.qml:543: TypeError: Property 'start' of object Updates is not a function
```

This proves the button click fired, but QML did not call the Python update controller.

### Read-Only Flash Path Tracer

Command run:

```powershell
.\.venv\Scripts\python.exe tools\test_flash_path.py BIOS
```

Result:

```text
[2] Load firmware from DB
    Firmware id=1  name=BIOS_H13DSH-1C84-NT00070T00_20250305_3.0a.V1_OEMsp 2.bin
            type=BIOS  version=v6
            file_path=C:\Users\asus\AppData\Local\ClusterOne\firmware\...\BIOS_H13DSH-1C84-NT00070T00_20250305_3.0a.V1_OEMsp 2.bin
            file exists on disk: False
    FAIL: firmware blob missing - re-import the file
```

No firmware upload was attempted.

### Tests

Full test suite:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Result:

```text
149 passed in 16.16s
```

Static lint could not be run because `ruff` is declared as a dev dependency but is not installed in the local `.venv`.

## Findings

### P0 - QML name collision prevents the Flash button from calling the backend

Files:

- `app/application.py`
- `ui/qml/Main.qml`
- `ui/qml/pages/Updates.qml`

The Python controller is exposed as the QML context property `Updates`:

```python
ctx.setContextProperty("Updates", container.update_controller)
```

But the page component is also named `Updates` because the file is `ui/qml/pages/Updates.qml`, and `Main.qml` instantiates it as:

```qml
Updates {}
```

Inside `Updates.qml`, the page calls:

```qml
Updates.start(...)
```

The runtime log shows that `Updates.isRunning` was `undefined` and `Updates.start` was not a function. That is consistent with `Updates` resolving to the page/component identity instead of the Python controller.

Impact:

- The flash job is not created.
- `UpdateController.start()` is never called.
- No `update_jobs` or `update_tasks` rows are created.
- The user sees a click/toast, but no flash happens.

Fix direction:

- Rename the context property to something that cannot collide with a page type, for example `UpdateJobs` or `UpdateController`.
- Update every reference in `Updates.qml` and `Main.qml` that is meant to call the controller.
- Add a QML smoke test that clicks the Flash button or directly verifies that the object has callable `start()` and bool `isRunning`.

### P0 - The only imported BIOS firmware blob is missing from disk

Files:

- `core/services/update_orchestrator.py`
- `core/services/firmware_service.py`
- Local DB and firmware library

Before any real flash, the orchestrator calls `_verify_firmware_sha()`:

```python
if not path.is_file():
    return False, f"firmware file missing: {path}"
```

The live DB points to a BIOS blob under `%LOCALAPPDATA%\ClusterOne\firmware\<sha>\...bin`, but that file does not exist. The read-only tracer confirms this.

Impact:

- Once the UI call reaches Python, the job will fail before contacting the BMC.
- BMC, BIOS, and HGX flashes all depend on valid local firmware blobs.

Fix direction:

- Re-import the BIOS image into Firmware Library.
- Add a library health check: if a row points to a missing file, mark it broken in the UI and disable selection or show "re-import required".
- Add startup validation or a Firmware Library "Repair" action.
- Avoid leaving DB rows that reference deleted local files.

### P0 - OEM compatibility validation rejects Tyrone-rebadged Supermicro firmware

Files:

- `plugins/supermicro/plugin.py`
- `plugins/generic_redfish/plugin.py`
- `core/services/update_orchestrator.py`

The Supermicro plugin correctly matches Tyrone systems:

```python
if "tyrone" in manuf:
    return 0.85
```

But validation is inherited from `GenericRedfishPlugin`:

```python
if firmware.oem and server.oem and firmware.oem != server.oem:
    return CompatResult(False, reason=f"OEM mismatch...")
```

The local server row is `tyrone systems`, while the firmware row is `supermicro ` including trailing whitespace. This will fail exact comparison.

Impact:

- The current BIOS image would be rejected even if the file existed.
- Rebadged platforms that the plugin intentionally supports cannot use their equivalent OEM firmware unless the DB stores exactly matching normalized keys.
- Trailing whitespace in firmware OEM input is enough to block flashing.

Fix direction:

- Normalize and strip firmware OEM values on import.
- Normalize discovered server OEM before persistence and before validation.
- Treat supported aliases as equivalent, especially `tyrone systems -> supermicro`.
- Prefer plugin-level compatibility semantics over raw string equality.

### P1 - `_run_job` can crash at finalization because `cancel_evt` is out of scope

File:

- `core/services/update_orchestrator.py`

Inside `_run_job`, `cancel_evt` is assigned inside the nested `run_one()` function:

```python
async def run_one(task_id):
    cancel_evt = self._cancel_event
```

After `gather()`, the outer function references it:

```python
final_state = self._compute_final_state(
    cancelled=cancel_evt.is_set(),
    succeeded=succeeded,
    failed=failed,
)
```

That variable is not defined in the outer scope. This path is not covered by the current unit tests because `start_job` tests use a no-op engine that closes the coroutine rather than running `_run_job`.

Impact:

- After tasks complete, the job can crash with `NameError`.
- `on_job_done` may not fire.
- The UI can show incorrect job state or route through the error handler.
- Successful flashes may be recorded as failed job-level outcomes.

Fix direction:

- Capture `job_cancel_evt = self._cancel_event` in `_run_job` before defining `run_one()`.
- Use that same outer variable both in workers and finalization.
- Add an async integration unit test for a successful one-task `_run_job`.

### P1 - Task monitor URIs are never persisted, so resume cannot work

Files:

- `core/services/update_orchestrator.py`
- `plugins/generic_redfish/plugin.py`
- `plugins/supermicro/plugin.py`
- `data/repositories/jobs_repo.py`

The DB has `update_tasks.redfish_task`, and `resume_interrupted()` expects it:

```python
no_uri = [t for t in tasks if not t.get("redfish_task")]
```

But neither the generic plugin nor the Supermicro plugin returns the task URI to the orchestrator, and the orchestrator never calls:

```python
jobs.update_task(..., redfish_task=task_uri)
```

Impact:

- Closing the app mid-flash will mark tasks failed on next startup because no Task URI was saved.
- The User Guide claim that jobs resume from persisted BMC TaskMonitor URI is not true for current code.

Fix direction:

- Extend `Result` or progress callback metadata to include task URI.
- Persist the task URI immediately after upload acceptance, before long polling begins.
- Add tests for restart/resume with a saved task URI.

### P1 - Active-task preflight probably misses real firmware tasks

File:

- `core/services/update_orchestrator.py`

Preflight checks for running tasks like this:

```python
running = [
    m for m in tasks.get("Members") or []
    if "update" in (m.get("@odata.id") or "").lower()
]
```

Most Redfish task URIs look like `/redfish/v1/TaskService/Tasks/1` or Dell `JID_...`; the URI often does not contain the word `update`. The code does not fetch each task and check `TaskState`, `Name`, `Messages`, or `Payload`.

Impact:

- The app can start a second flash while the BMC is already busy.
- Real BMCs can return 409, reject uploads, or behave inconsistently under concurrent firmware tasks.

Fix direction:

- Fetch task members and treat `Running`, `Pending`, `Starting`, or `New` update-related tasks as blockers.
- Use `cap.task_collection_uri` instead of hardcoded `/redfish/v1/TaskService/Tasks` where available.

### P1 - Capability target selection ignores `Updateable=False`

File:

- `core/plugins_api/capability_discoverer.py`

The discoverer reads `Updateable`:

```python
updateable=bool(mb.get("Updateable", True))
```

But `_pick_active()` checks only active bank and component class:

```python
if mb and mb.is_active_bank and mb.component_class == component_class:
    return mb.uri
```

The HGX pass similarly checks active bank but not `updateable`.

Impact:

- The app may target firmware inventory members the BMC explicitly says are not updateable.
- For HGX/GPU firmware, this can choose a member that exists for inventory only, leading to BMC rejection.

Fix direction:

- Require `mb.updateable` when selecting BIOS/BMC/HGX targets.
- Preserve non-updateable members in `firmware_inventory` for reporting, but never select them as flash targets.

### P1 - Generic `RedfishClient` cannot handle absolute TaskMonitor URLs

File:

- `core/plugins_api/redfish_client.py`
- `plugins/generic_redfish/plugin.py`

`_resolve_task_uri()` allows absolute `http://` or `https://` URIs through, but `get_json_and_headers()` blindly prepends `base_url` unless the path starts with `/`:

```python
if not path.startswith("/"):
    path = "/" + path
url = self.base_url + path
```

An absolute TaskMonitor URL would become something like:

```text
https://bmc:443/https://bmc:443/redfish/v1/TaskService/Tasks/1
```

Impact:

- BMCs that return absolute `Location` headers can fail task polling.

Fix direction:

- Teach `RedfishClient` request methods to detect absolute URLs and use them directly when host matches, or normalize absolute URLs to path-only form.

### P1 - HPE upload path likely drops authentication headers

File:

- `plugins/hpe/plugin.py`

The HPE-specific upload creates a raw `sess.post()` with custom headers:

```python
async with sess.post(
    url, data=form,
    timeout=aiohttp.ClientTimeout(total=30 * 60),
    headers={"Expect": "", "OData-Version": "4.0"},
) as resp:
```

It does not use `client._request_kwargs()`, so it does not attach `X-Auth-Token` or BasicAuth on that request. The code includes a `sessionKey` multipart field, but depending on iLO firmware this may be insufficient.

Impact:

- HPE firmware upload can fail with 401/403 even after successful `UpdateService` read.

Fix direction:

- Reuse `client._request_kwargs()` and merge required HPE headers, or expose a safe public helper for authenticated raw requests.
- Add an HPE upload test that asserts auth is sent.

### P2 - Firmware import accepts unnormalized user input

Files:

- `ui/qml/pages/FirmwareLibrary.qml`
- `core/services/firmware_service.py`

The firmware import form accepts free-text OEM and version values. The service stores them as-is:

```python
"oem": oem,
"version": version,
```

The local row demonstrates the issue: `oem='supermicro '` with a trailing space.

Impact:

- Compatibility checks fail due to whitespace/case.
- Version comparisons can behave unpredictably.
- Reports and filtering show inconsistent vendor labels.

Fix direction:

- Strip fields on import.
- Normalize OEM against the same alias map used by plugins.
- Consider selecting OEM from known plugin keys rather than raw text.

### P2 - Current tests pass but do not cover the failing paths

Files:

- `tests/unit/test_update_orchestrator.py`
- `tests/smoke_qml.py`

The full suite passes, but important behavior is untested:

- QML Flash button calling Python `UpdateController.start`.
- `_run_job` end-to-end successful completion.
- Job finalization callback.
- Persisting Redfish task URI.
- Live DB rows with missing firmware files.
- Tyrone/Supermicro OEM alias validation.
- HPE authenticated upload.

Impact:

- The exact failures seen in runtime can exist while CI remains green.

Fix direction:

- Add tests for the above paths.
- Run `tests/smoke_qml.py` in CI and fail on critical warnings.

### P2 - Documentation promises dry-run/resume features that current UI/backend do not fully provide

Files:

- `docs/USER_GUIDE.md`
- `tools/flash_now.py`
- `core/services/update_orchestrator.py`

The User Guide says the Updates page has Dry-run mode and that mid-flash jobs resume from persisted TaskMonitor URIs. Current `Updates.qml` starts real `Updates.start(...)` with no dry-run parameter, and task URIs are not persisted.

Impact:

- Operators may believe a safe dry-run path exists in the UI when it does not.
- Operators may believe long-running flashes survive app exit when current code marks no-URI tasks failed.

Fix direction:

- Either implement UI dry-run and task URI persistence, or update docs until those features exist.

## Why BIOS/BMC/HGX Flashing Fails Today

### BIOS on the current `172.16.12.138` target

Failure chain:

1. UI click currently resolves `Updates` incorrectly and never calls Python.
2. If UI is fixed, the only BIOS firmware file is missing, so `_verify_firmware_sha()` rejects it.
3. If the file is re-imported, OEM validation will likely reject `supermicro ` firmware against `tyrone systems` server unless normalization/aliasing is fixed.
4. If validation is fixed, Supermicro/Tyrone flashing uses the right general AMI multipart path, but job finalization and resume reliability still have bugs.

### BMC firmware

There is no BMC firmware row in the live DB. If one is imported, it will hit the same UI collision and OEM normalization risks. For Supermicro/Tyrone/AMI hardware, the BMC multipart recipe is broadly correct: `/redfish/v1/UpdateService/upload`, `Targets=[.../FirmwareInventory/BMC]`, `OemParameters={"ImageType":"BMC"}`, and `ApplyTime=Immediate`. Reliability issues remain around active-task detection, task URI persistence, and finalization.

### HGX/GPU firmware

The code has a dedicated `update_hgx()` path and tests for the audited HGX inventory shape on `172.16.11.193`. It uses multipart push, targets `cap.hgx_member_uris`, omits AMI `OemParameters`, and sets `ApplyTime=OnReset`. However:

- The UI collision prevents starting the job from the Updates page.
- No HGX firmware row exists in the live DB.
- Target selection ignores `Updateable=False`.
- Bundle targeting is heuristic and may upload to all active HGX members when the filename does not match a specific member.
- Task URI persistence is missing.

## Recommended Fix Order

1. Rename the QML context property from `Updates` to `UpdateJobs` or `UpdateController` and update all QML references.
2. Re-import the missing BIOS firmware; add missing-file health checks in Firmware Library.
3. Normalize firmware OEM input and discovered server OEMs; add alias compatibility for Tyrone/Supermicro.
4. Fix `_run_job` finalization by moving the cancel event snapshot into outer scope.
5. Persist Redfish task URIs immediately after upload acceptance.
6. Strengthen BMC busy-task preflight by fetching task details.
7. Respect `Updateable=False` for BIOS/BMC/HGX target selection.
8. Fix absolute URL handling in `RedfishClient`.
9. Fix HPE authenticated multipart upload.
10. Add end-to-end tests for QML Flash start, successful job completion, task URI persistence/resume, missing firmware file UI state, and OEM alias validation.

## Verification Performed

- Read relevant source across UI, bridge, services, plugins, Redfish client, repositories, tests, tools, docs, and app wiring.
- Queried live ClusterOne DB.
- Read local app logs.
- Ran read-only flash path tracer for BIOS.
- Ran focused firmware update tests.
- Ran full pytest suite.

Results:

- `tools/test_flash_path.py BIOS`: failed safely at missing firmware blob.
- Focused firmware tests: passed.
- Full test suite: `149 passed`.
- Ruff static lint: not run because `ruff` is not installed in `.venv`.
