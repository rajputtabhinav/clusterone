"""Live theme controller exposed to QML as the ``Theme`` context object.

Design language: **Graphite & Signal** — a dark-first mission-control aesthetic.
Deep graphite surfaces with subtle material depth, a warm signal-amber accent,
mint/coral semantic states, and monospace for technical data. A cohesive warm
light variant keeps the same identity (amber + mono) for users who want it.

QML binds to ``Theme.background``, ``Theme.accent``, ``Theme.monoFamily``, etc.
Switching mode re-emits ``changed`` so every binding updates instantly.
"""
from __future__ import annotations

from PyQt6.QtCore import QObject, Qt, pyqtProperty, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QGuiApplication

# Graphite & Signal — dark (the hero design).
_DARK = {
    "backgroundTop": "#0A0C10",
    "backgroundBottom": "#0E1117",
    "background": "#0C0F14",
    "sidebar": "#090C11",
    "card": "#14181F",
    "elevated": "#1B222C",
    "text": "#E7EAF0",
    "textMuted": "#7D8694",
    "accent": "#FFB020",          # signal amber
    "accentText": "#0A0C10",
    "border": "#222A34",
    "hover": "#1B222C",
    "selected": "#24FFB020",      # amber @ ~14%
    "success": "#3DDC97",         # mint
    "online": "#3DDC97",
    "warning": "#FFC34D",
    "error": "#FF6B6B",           # coral
    "info": "#5B8CFF",            # informational / operator-override blue
    "topHighlight": "#16FFFFFF",  # subtle material edge highlight
}

# Signal — warm light variant (same identity, legible on light).
_LIGHT = {
    "backgroundTop": "#F6F4EF",
    "backgroundBottom": "#EFEDE6",
    "background": "#F3F1EB",
    "sidebar": "#FBFAF6",
    "card": "#FFFFFF",
    "elevated": "#F1EEE7",
    "text": "#1A1D23",
    "textMuted": "#6A7280",
    "accent": "#B5730A",          # deep signal amber (contrast on light)
    "accentText": "#FFFFFF",
    "border": "#E5E1D8",
    "hover": "#EFECE4",
    "selected": "#1FB5730A",
    "success": "#149E72",
    "online": "#149E72",
    "warning": "#B5730A",
    "error": "#CC4A40",
    "info": "#2452C2",            # informational / operator-override (light)
    "topHighlight": "#00FFFFFF",  # no edge highlight needed on light
}

_VALID_MODES = ("light", "dark", "system")


class ThemeController(QObject):
    changed = pyqtSignal()
    modeChanged = pyqtSignal()

    def __init__(
        self,
        app: QGuiApplication,
        font_family: str = "Segoe UI",
        mono_family: str = "Consolas",
        settings=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._app = app
        self._font_family = font_family
        self._mono_family = mono_family
        self._settings = settings
        saved = settings.get("theme_mode") if settings else None
        self._mode = saved if saved in _VALID_MODES else "dark"  # dark-first
        try:
            app.styleHints().colorSchemeChanged.connect(self._on_system_scheme_changed)
        except Exception:  # pragma: no cover
            pass

    # ---- internal ----
    def _resolve_dark(self) -> bool:
        if self._mode == "dark":
            return True
        if self._mode == "light":
            return False
        try:
            return self._app.styleHints().colorScheme() == Qt.ColorScheme.Dark
        except Exception:
            return True

    def _on_system_scheme_changed(self, *_args) -> None:
        if self._mode == "system":
            self.changed.emit()

    def _c(self, key: str) -> str:
        return (_DARK if self._resolve_dark() else _LIGHT)[key]

    # ---- mode / meta ----
    @pyqtProperty(str, notify=modeChanged)
    def mode(self) -> str:
        return self._mode

    @pyqtSlot(str)
    def setMode(self, mode: str) -> None:
        mode = (mode or "").lower()
        if mode in _VALID_MODES and mode != self._mode:
            self._mode = mode
            if self._settings is not None:
                self._settings.set("theme_mode", mode)
            self.modeChanged.emit()
            self.changed.emit()

    @pyqtProperty(bool, notify=changed)
    def isDark(self) -> bool:
        return self._resolve_dark()

    @pyqtProperty(str, notify=changed)
    def fontFamily(self) -> str:
        return self._font_family

    @pyqtProperty(str, notify=changed)
    def monoFamily(self) -> str:
        return self._mono_family

    # ---- color tokens ----
    @pyqtProperty(str, notify=changed)
    def backgroundTop(self) -> str:
        return self._c("backgroundTop")

    @pyqtProperty(str, notify=changed)
    def backgroundBottom(self) -> str:
        return self._c("backgroundBottom")

    @pyqtProperty(str, notify=changed)
    def background(self) -> str:
        return self._c("background")

    @pyqtProperty(str, notify=changed)
    def sidebar(self) -> str:
        return self._c("sidebar")

    @pyqtProperty(str, notify=changed)
    def card(self) -> str:
        return self._c("card")

    @pyqtProperty(str, notify=changed)
    def elevated(self) -> str:
        return self._c("elevated")

    @pyqtProperty(str, notify=changed)
    def text(self) -> str:
        return self._c("text")

    @pyqtProperty(str, notify=changed)
    def textMuted(self) -> str:
        return self._c("textMuted")

    @pyqtProperty(str, notify=changed)
    def accent(self) -> str:
        return self._c("accent")

    @pyqtProperty(str, notify=changed)
    def accentText(self) -> str:
        return self._c("accentText")

    @pyqtProperty(str, notify=changed)
    def border(self) -> str:
        return self._c("border")

    @pyqtProperty(str, notify=changed)
    def hover(self) -> str:
        return self._c("hover")

    @pyqtProperty(str, notify=changed)
    def selected(self) -> str:
        return self._c("selected")

    @pyqtProperty(str, notify=changed)
    def success(self) -> str:
        return self._c("success")

    @pyqtProperty(str, notify=changed)
    def online(self) -> str:
        return self._c("online")

    @pyqtProperty(str, notify=changed)
    def warning(self) -> str:
        return self._c("warning")

    @pyqtProperty(str, notify=changed)
    def error(self) -> str:
        return self._c("error")

    @pyqtProperty(str, notify=changed)
    def info(self) -> str:
        """Informational accent — used for operator-supplied overrides
        (e.g. a manually-picked disk in the Provision preview) and any
        affordance that's neither success nor warning."""
        return self._c("info")

    @pyqtProperty(str, notify=changed)
    def topHighlight(self) -> str:
        return self._c("topHighlight")
