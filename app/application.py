"""Qt bootstrap: builds the DI container, exposes bridge objects to QML, loads Main.qml."""
from __future__ import annotations

import logging
import os
import sys

from PyQt6.QtCore import QtMsgType, QUrl, qInstallMessageHandler
from PyQt6.QtGui import QFontDatabase, QGuiApplication
from PyQt6.QtQml import QQmlApplicationEngine


def _qt_message_handler(mode: QtMsgType, ctx, message: str) -> None:
    """Route Qt/QML log messages into the Python logger so ``console.warn``
    inside QML files surfaces in the app log (otherwise lost under pythonw)."""
    qml_log = logging.getLogger("qml")
    text = f"{message}"
    if ctx and ctx.file:
        text = f"{ctx.file}:{ctx.line}: {text}"
    if mode == QtMsgType.QtDebugMsg:
        qml_log.debug(text)
    elif mode == QtMsgType.QtInfoMsg:
        qml_log.info(text)
    elif mode == QtMsgType.QtWarningMsg:
        qml_log.warning(text)
    elif mode in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
        qml_log.error(text)
    else:
        qml_log.info(text)

from app import config
from app.container import Container
from app.focus_filter import GlobalFocusFilter
from app.logging_setup import setup_logging

log = logging.getLogger(__name__)


def _load_fonts() -> tuple[str, str]:
    """Load bundled fonts if present; fall back to clean system faces.

    Returns ``(ui_family, mono_family)``. Defaults: Segoe UI + Consolas on
    Windows (both always present). Drop ``Inter*.ttf`` / ``IBMPlexMono*.ttf``
    (or JetBrains Mono) into ``assets/fonts`` for the exact Graphite & Signal look.
    """
    ui_family = "Segoe UI" if sys.platform == "win32" else "sans-serif"
    mono_family = "Consolas" if sys.platform == "win32" else "monospace"
    fonts_dir = config.resource_path("assets", "fonts")
    loaded: list[str] = []
    if fonts_dir.is_dir():
        for ttf in fonts_dir.glob("*.ttf"):
            fid = QFontDatabase.addApplicationFont(str(ttf))
            if fid != -1:
                loaded.extend(QFontDatabase.applicationFontFamilies(fid))
    if any("Inter" in name for name in loaded):
        ui_family = "Inter"
    for name in loaded:
        if "Plex Mono" in name or "JetBrains Mono" in name:
            mono_family = name
            break
    return ui_family, mono_family


def main() -> int:
    setup_logging()
    # Route QML/Qt log output to the Python logger so QML console.warn and
    # any QML/Quick warnings surface in the app log file. Without this they
    # vanish under pythonw.exe and we can't tell if a click handler fired.
    qInstallMessageHandler(_qt_message_handler)
    log.info("Starting %s %s", config.APP_NAME, config.APP_VERSION)

    # ClusterOne fully restyles every control, so use the unopinionated Basic
    # style (no native-style DLL dependency — also friendlier to packaging).
    # Must be set before the GUI application is constructed.
    os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")

    app = QGuiApplication(sys.argv)
    app.setApplicationName(config.APP_NAME)
    app.setOrganizationName(config.ORG_NAME)
    app.setApplicationVersion(config.APP_VERSION)

    ui_family, mono_family = _load_fonts()
    container = Container(app, font_family=ui_family, mono_family=mono_family)

    engine = QQmlApplicationEngine()
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

    qml_main = config.resource_path("ui", "qml", "Main.qml")
    engine.load(QUrl.fromLocalFile(str(qml_main)))
    roots = engine.rootObjects()
    if not roots:
        log.error("Failed to load QML root object: %s", qml_main)
        return 1

    window = roots[0]
    _restore_geometry(window, container.settings)

    # Universal "click-outside-clears-focus" for text inputs. Holding the
    # reference on the container keeps the QObject from being GC'd.
    container.focus_filter = GlobalFocusFilter()
    window.installEventFilter(container.focus_filter)

    app.aboutToQuit.connect(lambda: _save_geometry(window, container.settings))
    app.aboutToQuit.connect(container.shutdown)

    log.info("UI loaded; entering event loop")
    return app.exec()


def _restore_geometry(window, settings) -> None:
    w = settings.get_int("win_w", 0)
    h = settings.get_int("win_h", 0)
    if w > 200 and h > 200:
        window.setProperty("width", w)
        window.setProperty("height", h)
        x = settings.get_int("win_x", -1)
        y = settings.get_int("win_y", -1)
        if x >= 0 and y >= 0:
            window.setProperty("x", x)
            window.setProperty("y", y)


def _save_geometry(window, settings) -> None:
    try:
        if int(window.property("visibility")) == 4:  # maximized — keep the normal geometry
            return
        settings.set("win_w", int(window.property("width")))
        settings.set("win_h", int(window.property("height")))
        settings.set("win_x", int(window.property("x")))
        settings.set("win_y", int(window.property("y")))
    except (TypeError, ValueError):
        pass
