import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

import "components"
import "pages"

ApplicationWindow {
    id: win
    width: 1240
    height: 800
    minimumWidth: 980
    minimumHeight: 620
    visible: true
    title: App.appName
    flags: Qt.Window | Qt.FramelessWindowHint
    color: "transparent"

    readonly property bool maximized: win.visibility === Window.Maximized
                                      || win.visibility === Window.FullScreen
    // Rounded corners stay on in every visibility state (normal, maximized,
    // fullscreen). The frameless window is transparent, so the radius simply
    // masks the inner gradient — the desktop shows through the cutouts the
    // same way modern macOS / Arc / Linear keep their pill silhouette even
    // when filling the screen.
    readonly property int outerRadius: 14

    function toggleMaximize() {
        if (maximized) win.showNormal(); else win.showMaximized()
    }

    Component.onCompleted: {
        x = Math.round((Screen.width - width) / 2)
        y = Math.round((Screen.height - height) / 2)
    }

    Rectangle {
        id: root
        anchors.fill: parent
        radius: win.outerRadius
        border.color: Theme.border
        border.width: 1
        antialiasing: true
        gradient: Gradient {
            GradientStop { position: 0.0; color: Theme.backgroundTop }
            GradientStop { position: 1.0; color: Theme.backgroundBottom }
        }

        RowLayout {
            anchors.fill: parent
            anchors.margins: 1
            spacing: 0

            NavRail {
                id: nav
                Layout.fillHeight: true
                Layout.preferredWidth: 240
                // -1 accounts for the 1px outer border + 1px content inset
                leftRadius: Math.max(0, win.outerRadius - 1)
                currentIndex: stack.currentIndex
                onNavigate: (i) => stack.currentIndex = i
            }

            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 0

                // ---- Title / top bar (drag region) ----
                Item {
                    id: topBar
                    Layout.fillWidth: true
                    Layout.preferredHeight: 52

                    MouseArea {
                        anchors.fill: parent
                        onPressed: win.startSystemMove()
                        onDoubleClicked: win.toggleMaximize()
                    }

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 24
                        anchors.rightMargin: 10
                        spacing: 12

                        Label {
                            text: nav.currentTitle
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: 18
                            font.weight: Font.DemiBold
                        }

                        Item { Layout.fillWidth: true }

                        ThemeToggle {
                            ToolTip.visible: themeToggleHover.hovered
                            ToolTip.text: Theme.isDark ? "Switch to light mode" : "Switch to dark mode"
                            ToolTip.delay: 600
                            HoverHandler { id: themeToggleHover }
                        }
                        WindowControls {
                            onMinimize: win.showMinimized()
                            onMaximize: win.toggleMaximize()
                            onClose: win.close()
                        }
                    }
                }

                // ---- Signature: mission-control status ribbon ----
                Item {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 32

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 24
                        anchors.rightMargin: 16
                        spacing: 20

                        RibbonStat { dotColor: Theme.online;  text: Fleet.online + " ONLINE" }
                        RibbonStat { dotColor: Theme.warning; text: Fleet.needsUpdate + " NEED UPDATE" }
                        RibbonStat { dotColor: Theme.error;   text: Fleet.failed + " FAILED" }

                        Item { Layout.fillWidth: true }

                        Text {
                            // Mirrors the last scan target the operator
                            // ran. Empty until the first discovery, in
                            // which case we show a neutral hint instead.
                            text: Discovery.lastRange
                                  ? "SUBNET " + Discovery.lastRange
                                  : "NO SCANS YET"
                            color: Theme.textMuted
                            font.family: Theme.monoFamily
                            font.pixelSize: 11
                            font.letterSpacing: 0.3
                        }
                    }
                    Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.border }
                }

                // ---- Page stack ----
                StackLayout {
                    id: stack
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    currentIndex: 0

                    Dashboard {}
                    Inventory {}
                    Updates {}
                    Provision {}
                    FirmwareLibrary {}
                    IsoLibrary {}
                    Activity {}
                    Settings {}
                }
            }
        }

        // ---- Frameless resize hit-areas ----
        MouseArea {
            height: 6; cursorShape: Qt.SizeVerCursor; z: 100; enabled: !win.maximized
            anchors { top: parent.top; left: parent.left; right: parent.right }
            onPressed: win.startSystemResize(Qt.TopEdge)
        }
        MouseArea {
            height: 6; cursorShape: Qt.SizeVerCursor; z: 100; enabled: !win.maximized
            anchors { bottom: parent.bottom; left: parent.left; right: parent.right }
            onPressed: win.startSystemResize(Qt.BottomEdge)
        }
        MouseArea {
            width: 6; cursorShape: Qt.SizeHorCursor; z: 100; enabled: !win.maximized
            anchors { left: parent.left; top: parent.top; bottom: parent.bottom }
            onPressed: win.startSystemResize(Qt.LeftEdge)
        }
        MouseArea {
            width: 6; cursorShape: Qt.SizeHorCursor; z: 100; enabled: !win.maximized
            anchors { right: parent.right; top: parent.top; bottom: parent.bottom }
            onPressed: win.startSystemResize(Qt.RightEdge)
        }
        MouseArea {
            width: 12; height: 12; cursorShape: Qt.SizeFDiagCursor; z: 101; enabled: !win.maximized
            anchors { left: parent.left; top: parent.top }
            onPressed: win.startSystemResize(Qt.LeftEdge | Qt.TopEdge)
        }
        MouseArea {
            width: 12; height: 12; cursorShape: Qt.SizeBDiagCursor; z: 101; enabled: !win.maximized
            anchors { right: parent.right; top: parent.top }
            onPressed: win.startSystemResize(Qt.RightEdge | Qt.TopEdge)
        }
        MouseArea {
            width: 12; height: 12; cursorShape: Qt.SizeBDiagCursor; z: 101; enabled: !win.maximized
            anchors { left: parent.left; bottom: parent.bottom }
            onPressed: win.startSystemResize(Qt.LeftEdge | Qt.BottomEdge)
        }
        MouseArea {
            width: 12; height: 12; cursorShape: Qt.SizeFDiagCursor; z: 101; enabled: !win.maximized
            anchors { right: parent.right; bottom: parent.bottom }
            onPressed: win.startSystemResize(Qt.RightEdge | Qt.BottomEdge)
        }
    }

    // ---- cross-page navigation (e.g. Inventory → Updates) ----
    Connections {
        target: App
        function onNavigateRequested(index) { stack.currentIndex = index }
    }

    // ============================================================
    //  STARTUP SPLASH — letter-by-letter typewriter animation
    //
    //  Full-window overlay that types "ClusterOne" and the tagline
    //  character by character, then fades out after 3 seconds total.
    //  Sits above everything (z: 99999) so the underlying UI loads
    //  behind it; the user sees a polished entry rather than a blank
    //  or half-loaded frame.
    // ============================================================
    Rectangle {
        id: splash
        anchors.fill: parent
        z: 99999
        radius: win.outerRadius

        // Match the app's own gradient so it feels like the window
        // itself is waking up, not a separate overlay.
        gradient: Gradient {
            GradientStop { position: 0.0; color: Theme.backgroundTop }
            GradientStop { position: 1.0; color: Theme.backgroundBottom }
        }

        // Visible until the exit animation completes. The 0.01 threshold
        // matters: NumberAnimation ends at floating-point ~1e-9, not exact
        // 0.0, so `opacity > 0` stays true and the Rectangle keeps eating
        // clicks (it's still on top at z=99999). `enabled` is gated on
        // BOTH visibility AND the sequenceComplete flag — once the exit
        // animation kicks off (set true at exitAnim.start()), the splash
        // immediately stops hit-testing even though it's still painting
        // the fade. Without this, clicks during the 520ms fade are still
        // swallowed.
        visible: opacity > 0.01
        property bool sequenceComplete: false
        enabled: visible && !sequenceComplete

        // ---- State machine ----
        //  "idle"   — hidden before the sequence starts (instant)
        //  "typing" — letters appear one by one
        //  "hold"   — brief pause before fade
        //  "done"   — invisible; sequence complete
        property int    charCount:  0          // how many chars shown so far
        property bool   cursorOn:   true       // blinking cursor toggle
        readonly property string fullName: "ClusterOne"
        readonly property string fullTag:  App.tagline  // "One Platform. Every Server."

        // ---- Enter: begin the typewriter after a tiny delay so Qt
        //            finishes first-frame rendering behind the splash ----
        Component.onCompleted: startTimer.start()

        Timer {
            id: startTimer
            interval: 120          // 120 ms — let the UI settle
            repeat: false
            onTriggered: {
                splash.charCount = 0
                typeTimer.start()
                cursorTimer.start()
            }
        }

        // One letter every ~170 ms → 10 chars × 170 ms = 1700 ms typing
        Timer {
            id: typeTimer
            interval: 170
            repeat: true
            onTriggered: {
                if (splash.charCount < splash.fullName.length) {
                    splash.charCount++
                } else {
                    typeTimer.stop()
                    holdTimer.start()
                }
            }
        }

        // Blinking cursor — 480 ms on/off cycle
        Timer {
            id: cursorTimer
            interval: 480
            repeat: true
            onTriggered: splash.cursorOn = !splash.cursorOn
        }

        // Hold for ~1 s after typing completes, then fade
        Timer {
            id: holdTimer
            interval: 900
            repeat: false
            onTriggered: {
                cursorTimer.stop()
                splash.cursorOn = false   // leave cursor off during fade
                // Mark sequence complete BEFORE starting the fade so the
                // splash stops hit-testing immediately. Clicks land on the
                // real UI from this moment on, even though the splash is
                // still painting its fade to 0 over the next 520ms.
                splash.sequenceComplete = true
                exitAnim.start()
            }
        }

        // ---- Smooth fade-out ----
        NumberAnimation {
            id: exitAnim
            target: splash
            property: "opacity"
            from: 1.0
            to: 0.0
            duration: 520
            easing.type: Easing.InCubic
        }

        // ---- Content: centred brand block ----
        Column {
            anchors.centerIn: parent
            spacing: 16

            // App icon — the same 4-dot grid as the NavRail brand logo
            Item {
                width: 52; height: 52
                anchors.horizontalCenter: parent.horizontalCenter
                Rectangle {
                    anchors.centerIn: parent
                    width: 52; height: 52; radius: 14
                    color: Theme.elevated
                    border.color: Theme.border
                    border.width: 1
                    Grid {
                        anchors.centerIn: parent
                        columns: 2
                        rowSpacing: 7
                        columnSpacing: 7
                        Repeater {
                            model: 4
                            delegate: Rectangle {
                                width: 12; height: 12; radius: 3
                                color: Theme.accent
                                // Each dot fades in with a small stagger
                                // based on how many chars have been typed.
                                opacity: splash.charCount >= (index * 2 + 1) ? 1.0 : 0.0
                                Behavior on opacity { NumberAnimation { duration: 200 } }
                            }
                        }
                    }
                }
            }

            // App name — typed out character by character
            Row {
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: 0

                Text {
                    text: splash.fullName.substring(0, splash.charCount)
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: 42
                    font.weight: Font.Bold
                    font.letterSpacing: -0.5
                }

                // Blinking amber cursor — same accent as everything else
                Rectangle {
                    width: 3
                    height: 46
                    radius: 2
                    color: Theme.accent
                    anchors.verticalCenter: undefined
                    y: 6
                    opacity: splash.cursorOn && splash.charCount < splash.fullName.length
                             ? 1.0 : 0.0
                    Behavior on opacity { NumberAnimation { duration: 80 } }
                }
            }

            // Tagline — fades in once the name is fully typed
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: App.tagline
                color: Theme.textMuted
                font.family: Theme.monoFamily
                font.pixelSize: 13
                font.letterSpacing: 0.4
                opacity: splash.charCount >= splash.fullName.length ? 1.0 : 0.0
                Behavior on opacity { NumberAnimation { duration: 400 } }
            }
        }

        // Subtle version badge bottom-right — same treatment as window bottom
        Text {
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            anchors.margins: 18
            text: "v" + App.version
            color: Theme.textMuted
            font.family: Theme.monoFamily
            font.pixelSize: 10
            opacity: 0.5
        }
    }

    // ---- Global toast notifications + page-agnostic action signals ----
    // The action signals (exportInventory / exportAudit / cancelJob) are
    // still emitted by the Inventory Welcome card and other internal
    // call sites, so we route them centrally here.
    Toast { id: globalToast; z: 9999 }
    Connections {
        target: App
        function onToastRequested(msg, kind) { globalToast.display(msg, kind) }
        function onExportInventoryRequested() { Reports.exportInventory() }
        function onExportAuditRequested() { Reports.exportAuditLog() }
        function onCancelJobRequested() {
            if (Updates.isRunning) Updates.cancel()
            else if (Provisioning.isRunning) Provisioning.cancel()
            else App.showToast("No job is currently running", "info")
        }
    }

    // ---- inline helper ----
    component RibbonStat: RowLayout {
        id: rstat
        property color dotColor: Theme.online
        property string text: ""
        spacing: 7
        StatusDot { Layout.alignment: Qt.AlignVCenter; dotColor: rstat.dotColor; coreSize: 6 }
        Text {
            Layout.alignment: Qt.AlignVCenter
            text: rstat.text
            color: Theme.textMuted
            font.family: Theme.monoFamily
            font.pixelSize: 11
            font.letterSpacing: 0.3
        }
    }
}
