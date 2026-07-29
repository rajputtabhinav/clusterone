# ClusterOne — Release runbook

End-to-end checklist for shipping a signed `ClusterOne_Setup_<version>.exe`.

## 0. Prereqs (one-time)

- **Python 3.12+** with the project's `.venv`
  (`python -m venv .venv && .venv\Scripts\pip install -r requirements.txt`).
- **PyInstaller** in the venv: `pip install pyinstaller`.
- **Inno Setup 6+** on PATH (`iscc`): https://jrsoftware.org/isdl.php
- **EV Code-signing certificate** in the Windows certificate store (any
  approved CA — DigiCert, Sectigo). EV avoids SmartScreen friction.
- **signtool.exe** (from the Windows SDK) on PATH.

## 1. Bump version

Update both:

- `app/config.py` → `APP_VERSION = "x.y.z"`
- `packaging/version_info.txt` → `filevers` + `prodvers` + `FileVersion` + `ProductVersion`
- `packaging/installer.iss` → `#define MyAppVersion`

Commit (`git commit -am "release: x.y.z"`).

## 2. Run the test matrix

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.venv\Scripts\pytest tests\unit -v
.venv\Scripts\python tests\smoke_qml.py
.venv\Scripts\pytest tests\integration -v
```

CI must also be green on this commit (GitHub Actions, see
`.github/workflows/ci.yml`).

## 3. Build the bundle + installer

```powershell
.\packaging\build.ps1
```

Outputs:

- `dist\ClusterOne\ClusterOne.exe` — the unsigned binary + dependencies.
- `packaging\output\ClusterOne_Setup_<version>.exe` — the installer.

## 4. Sign

```powershell
$ts = "http://timestamp.digicert.com"
signtool sign /tr $ts /td sha256 /fd sha256 /a `
    dist\ClusterOne\ClusterOne.exe
signtool sign /tr $ts /td sha256 /fd sha256 /a `
    packaging\output\ClusterOne_Setup_<version>.exe
signtool verify /pa packaging\output\ClusterOne_Setup_<version>.exe
```

EV certs auto-bind to the strongest installed key.

## 5. Smoke-test the installer on a clean VM

1. Spin up a fresh Windows VM (no Python, no Qt, no SmartScreen overrides).
2. Copy `ClusterOne_Setup_<version>.exe`, run it.
3. Confirm: no SmartScreen warning, default install path is `C:\Program Files\ClusterOne`, Start-menu shortcut works.
4. Launch ClusterOne. Confirm the themed shell, that
   `%LOCALAPPDATA%\ClusterOne` is created with `db/`, `firmware/`, `logs/`.
5. (Optional) Point discovery at a known-good BMC, run a dry-run job
   end-to-end, export an Inventory report.
6. Uninstall via Apps & Features. Confirm clean removal (no leftover Program
   Files entries; data dir intentionally retained).

## 6. Tag + publish

```powershell
git tag v<version>
git push --tags
gh release create v<version> packaging\output\ClusterOne_Setup_<version>.exe `
    --title "ClusterOne <version>" --notes-file RELEASE_NOTES.md
```

## Notes

- The installer ships everything ClusterOne needs to launch (Qt runtime, PyQt6, aiohttp, etc.) — there are no external runtime prerequisites for end-users.
- ClusterOne keeps its data outside `Program Files`, so an in-place upgrade
  is non-destructive: the installer overwrites the install dir; existing
  DB/firmware/logs in `%LOCALAPPDATA%\ClusterOne` survive.
- For air-gapped customer sites: ship the signed installer + an MD5/SHA-256
  hash sheet so the customer can verify integrity before running.
