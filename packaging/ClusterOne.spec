# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for ClusterOne.

Build (from project root, in the venv)::

    pyinstaller --clean --noconfirm packaging/ClusterOne.spec

Produces ``dist/ClusterOne/ClusterOne.exe`` (one-dir bundle). The matching
Inno Setup script (packaging/installer.iss) then wraps it into the installer.
"""
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent

datas = [
    (str(ROOT / "ui" / "qml"),                    "ui/qml"),
    (str(ROOT / "assets"),                        "assets"),
    (str(ROOT / "plugins"),                       "plugins"),
    (str(ROOT / "data" / "migrations"),           "data/migrations"),
    (str(ROOT / "data" / "autoinstall_templates"), "data/autoinstall_templates"),
]

# Every OEM plugin needs an explicit hiddenimport so PyInstaller's static
# analyzer pulls it into the bundle. PluginRegistry.load_bundled() globs
# the data dir at runtime, so without these lines the modules would be
# absent and OEM matching would silently fall back to the generic plugin.
hiddenimports = [
    "plugins.generic_redfish.plugin",
    "plugins.supermicro.plugin",
    "plugins.dell.plugin",
    "plugins.hpe.plugin",
    "plugins.lenovo.plugin",
    "plugins.gigabyte.plugin",
    "plugins.asrock.plugin",
    "plugins.asus.plugin",
    "plugins.msi.plugin",
]

a = Analysis(
    [str(ROOT / "app" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PyQt5"],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz, a.scripts,
    [],
    exclude_binaries=True,
    name="ClusterOne",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,             # GUI app — no console window
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,                 # drop ClusterOne.ico here when available
    version=str(ROOT / "packaging" / "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ClusterOne",
)
