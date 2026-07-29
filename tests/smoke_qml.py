"""Headless QML smoke test.

Loads the full Main.qml tree under the offscreen platform, captures any QML
warnings/errors, and exits. Lets CI (and developers without a display) verify
the UI composes without opening a window.

    QT_QPA_PLATFORM=offscreen python tests/smoke_qml.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QTimer, QUrl  # noqa: E402
from PyQt6.QtGui import QGuiApplication  # noqa: E402
from PyQt6.QtQml import QQmlApplicationEngine  # noqa: E402

from app import config  # noqa: E402
from app.container import Container  # noqa: E402

warnings: list[str] = []

app = QGuiApplication(sys.argv)
container = Container(app, font_family="Segoe UI")

engine = QQmlApplicationEngine()
engine.warnings.connect(lambda ws: warnings.extend(w.toString() for w in ws))
ctx = engine.rootContext()
ctx.setContextProperty("Theme", container.theme)
ctx.setContextProperty("App", container.app_controller)
ctx.setContextProperty("Fleet", container.fleet)
ctx.setContextProperty("Firmware", container.fw_controller)
ctx.setContextProperty("Isos", container.iso_controller)
ctx.setContextProperty("EventLog", container.event_log)
ctx.setContextProperty("Discovery", container.discovery_controller)
ctx.setContextProperty("Updates", container.update_controller)
ctx.setContextProperty("Power", container.power_controller)
ctx.setContextProperty("Provisioning", container.provisioning_controller)
ctx.setContextProperty("Reports", container.reports_controller)
ctx.setContextProperty("Credentials", container.credentials_controller)
ctx.setContextProperty("AppSettings", container.settings_controller)

engine.load(QUrl.fromLocalFile(str(config.resource_path("ui", "qml", "Main.qml"))))
root_ok = bool(engine.rootObjects())

# Let bindings/Component.onCompleted run, then quit.
QTimer.singleShot(800, app.quit)
app.exec()

print(f"ROOT_LOADED {root_ok}")
print(f"WARNINGS {len(warnings)}")
for w in warnings:
    print("  WARN:", w)

sys.exit(0 if root_ok else 1)
