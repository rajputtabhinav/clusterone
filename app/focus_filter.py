"""Global "click-outside-clears-focus" filter for QML text inputs.

QML's default focus model keeps a ``TextField``'s ``activeFocus`` until
another Item explicitly takes focus. Most of our clickable controls
(``PrimaryButton``, ``NavButton``, chips, toggles, dialog backgrounds, …)
don't grab focus on press, so editing a field and then clicking elsewhere
leaves the input's blinking cursor + accent border visible — and the
field's value uncommitted until ``onEditingFinished`` finally fires.

Patching every interactive control to ``forceActiveFocus()`` is fragile
(easy to miss; new controls grow the surface area). This filter solves it
once at the framework boundary: it sits between Qt's event loop and QML,
catches every mouse press the window sees, and — if the press lands
outside the currently-focused text input — clears that input's focus
before letting the event continue to QML. The TextField's
``onEditingFinished`` then fires synchronously (committing the value)
and the cursor disappears.

The filter never consumes the event, so whatever was clicked still
processes the click normally; we just slot a focus release in front.
"""
from __future__ import annotations

import logging

from PyQt6.QtCore import QEvent, QObject

log = logging.getLogger(__name__)

# Qt class names we treat as "text inputs worth releasing on outside-click".
# All three derive from QQuickItem; matching by metaObject().className()
# avoids importing the QtQuick C++ types directly.
_TEXT_INPUT_CLASS_HINTS = ("TextField", "TextInput", "TextEdit", "TextArea")


def _is_text_input(item) -> bool:
    if item is None:
        return False
    try:
        name = item.metaObject().className()
    except Exception:
        return False
    return any(hint in name for hint in _TEXT_INPUT_CLASS_HINTS)


class GlobalFocusFilter(QObject):
    """Install on the main ``QQuickWindow`` after the QML root loads."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Only care about the start of a mouse interaction. Releases and
        # moves can stay untouched — the press is where focus normally
        # transfers in native UIs.
        if event.type() != QEvent.Type.MouseButtonPress:
            return False

        try:
            focused = watched.activeFocusItem()      # window-level focus
        except AttributeError:
            return False
        if not _is_text_input(focused):
            return False

        # Decide whether the press is inside the focused text input. If
        # so, leave focus alone — the user is repositioning the caret or
        # selecting text. If outside, clear focus so the field commits
        # and the cursor disappears.
        try:
            # Use scenePosition() (logical pixels) not position() (physical
            # pixels) so the hit-test works correctly at non-100% DPI on
            # Windows (125%, 150%, etc.).  mapFromScene also operates in
            # logical pixels, so both sides of the comparison match.
            scene_pt = event.scenePosition()
            local_pt = focused.mapFromScene(scene_pt)
            rect = focused.boundingRect()
        except Exception:
            return False

        if rect.contains(local_pt):
            return False

        # `focus = False` walks up the QML focus scope chain and removes
        # the active-focus assignment cleanly. Whatever was clicked still
        # gets the press event — we explicitly do NOT consume it.
        try:
            focused.setProperty("focus", False)
        except Exception:
            log.debug("Could not release focus from %r", focused, exc_info=True)
        return False
