import QtQuick

// Global toast overlay. Show via `globalToast.display(msg, kind)`.
// Wired in Main.qml to AppController.toastRequested. Lifts well above any
// page-level footer (e.g. the Settings save bar at 56 px) so it isn't
// occluded.
Item {
    id: root
    anchors.fill: parent

    property string message: ""
    property string kind: "info"     // info | success | warning | error
    property int durationMs: 4500
    // Operator-tunable bottom inset so pages with floating footers
    // (Settings has a 56 px save bar) can push the toast above their
    // controls. Pages should bind this via:
    //     Component.onCompleted: globalToast.bottomInset = 88
    // and reset to 24 in Component.onDestruction.
    property int bottomInset: 24

    function display(msg, k) {
        message = msg || ""
        kind = (k || "info").toLowerCase()
        chip.opacity = 1
        chip.y = chip._restY                // slide-up entrance
        hideTimer.restart()
    }

    function _bgColor() {
        if (kind === "success") return Theme.success
        if (kind === "warning") return Theme.warning
        if (kind === "error")   return Theme.error
        return Theme.elevated
    }
    function _fgColor() {
        return kind === "info" ? Theme.text : Theme.accentText
    }

    Rectangle {
        id: chip
        // Bottom margin tracks ``root.bottomInset`` so pages with floating
        // footers can lift the toast above their controls without
        // hardcoding the height into this component. Defaults to 24 px.
        readonly property int _bottomMargin: root.bottomInset
        readonly property int _restY: root.height - height - _bottomMargin
        readonly property int _startY: root.height - height + 12   // slightly below
        anchors.horizontalCenter: parent.horizontalCenter
        y: _startY
        implicitWidth: row.implicitWidth + 44
        implicitHeight: 48
        radius: 12
        color: root._bgColor()
        border.color: Theme.border
        border.width: 1
        opacity: 0

        Behavior on opacity { NumberAnimation { duration: 200; easing.type: Easing.OutCubic } }
        Behavior on y       { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
        Behavior on color   { ColorAnimation  { duration: 150 } }

        Row {
            id: row
            anchors.centerIn: parent
            spacing: 12
            Rectangle {
                width: 10; height: 10; radius: 5
                anchors.verticalCenter: parent.verticalCenter
                color: root.kind === "info" ? Theme.accent : root._fgColor()
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: root.message
                color: root._fgColor()
                font.family: Theme.fontFamily
                font.pixelSize: 14
                font.weight: Font.DemiBold
            }
        }
    }

    Timer {
        id: hideTimer
        interval: root.durationMs
        onTriggered: { chip.opacity = 0; chip.y = chip._startY }
    }
}
