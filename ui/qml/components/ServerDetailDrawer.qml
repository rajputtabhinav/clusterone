import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Slide-in detail panel for a single server. Bind `data` to a row dict (or
// null to close). Outer Item fills the parent page; the visible panel slides
// in from the right. Clicking the dim backdrop closes the drawer.
Item {
    id: drawer
    anchors.fill: parent
    z: 100

    property var data: null
    property bool isOpen: data !== null
    // Live PowerState override after an action (panel reflects the new state
    // without re-opening); cleared whenever a different server is shown.
    property string _livePower: ""
    property string _pendingAction: ""
    property string _pendingLabel: ""
    // Local OEM-override echo so the drawer reflects a new pick instantly
    // (data is a click-time snapshot). null = no local change yet.
    property var _liveOemKey: null
    onDataChanged: { drawer._livePower = ""; drawer._liveOemKey = null }

    // Effective override key for the open server ("" = auto-detect).
    function _oemKey() {
        if (drawer._liveOemKey !== null) return drawer._liveOemKey
        return (drawer.data && drawer.data.oem_override) ? drawer.data.oem_override : ""
    }
    function _oemName(key) {
        var opts = Fleet.oemOptions
        for (var i = 0; i < opts.length; i++)
            if (opts[i].key === key) return opts[i].name
        return key
    }
    function _oemLabel() {
        var k = drawer._oemKey()
        return k ? drawer._oemName(k) : "Select OEM…"   // manual-only: no auto
    }
    // Audit found: at first open, panel.x === parent.width so `panel.x < width`
    // is false → drawer briefly flickers before the slide-in animation starts.
    // The fix is to gate on a strict comparison that doesn't depend on the
    // panel's initial layout: visible whenever the drawer is open OR while
    // the panel is still on its way back to off-screen after a close.
    visible: isOpen || (panel.x < parent.width - 1)

    // Dim backdrop — click-outside-to-close
    Rectangle {
        anchors.fill: parent
        color: "#00000000"
        opacity: drawer.isOpen ? 0.35 : 0
        Behavior on opacity { NumberAnimation { duration: 180 } }
        Rectangle { anchors.fill: parent; color: "#000000" }
    }
    MouseArea {
        anchors.fill: parent
        enabled: drawer.isOpen
        onClicked: drawer.data = null
    }

    // The sliding panel
    Item {
        id: panel
        width: 400
        height: parent ? parent.height : 0
        x: parent ? (drawer.isOpen ? parent.width - width : parent.width) : 0
        Behavior on x { NumberAnimation { duration: 200; easing.type: Easing.OutCubic } }

        // Block clicks from reaching the backdrop
        MouseArea { anchors.fill: parent; onClicked: {} }

        // Background + left hairline
        Rectangle { anchors.fill: parent; color: Theme.card }
        Rectangle {
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            width: 1
            color: Theme.border
        }

        // Header
        Item {
            id: header
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.right: parent.right
            height: 64
            Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.border }

            ColumnLayout {
                anchors.left: parent.left
                anchors.leftMargin: 20
                anchors.verticalCenter: parent.verticalCenter
                anchors.right: closeBtn.left
                anchors.rightMargin: 12
                spacing: 2
                Text {
                    text: drawer.data ? (drawer.data.hostname || drawer.data.ip || "") : ""
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: 15
                    font.weight: Font.DemiBold
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
                Text {
                    text: drawer.data ? (drawer.data.ip || "") : ""
                    color: Theme.textMuted
                    font.family: Theme.monoFamily
                    font.pixelSize: 11
                }
            }

            Rectangle {
                id: closeBtn
                anchors.right: parent.right
                anchors.rightMargin: 14
                anchors.verticalCenter: parent.verticalCenter
                width: 28; height: 28; radius: 8
                color: closeMa.containsMouse ? Theme.hover : "transparent"
                Behavior on color { ColorAnimation { duration: 100 } }
                Text { anchors.centerIn: parent; text: "×"; color: Theme.text; font.pixelSize: 16 }
                MouseArea {
                    id: closeMa
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: drawer.data = null
                }
            }
        }

        // Body
        ColumnLayout {
            anchors.top: header.bottom
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: actions.top
            anchors.margins: 20
            spacing: 18

            SectionTitle { text: "IDENTITY" }
            InfoRow { label: "Manufacturer"; value: drawer._or(drawer.data && drawer.data.manufacturer) }
            InfoRow { label: "Model";        value: drawer._or(drawer.data && drawer.data.model_name); mono: true }
            InfoRow { label: "Serial";       value: drawer._or(drawer.data && drawer.data.serial); mono: true }
            // OEM routing — MANUAL-ONLY. Required before flash/provision/power;
            // unpinned servers show a warning chip until the operator picks.
            RowLayout {
                Layout.fillWidth: true
                spacing: 12
                Text {
                    text: "OEM"
                    Layout.preferredWidth: 100
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: 12
                }
                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: 30
                    radius: 8
                    color: oemMa.containsMouse ? Theme.hover : "transparent"
                    border.color: drawer._oemKey()
                                  ? (oemMa.containsMouse ? Theme.accent : Theme.border)
                                  : Theme.warning
                    border.width: 1
                    Behavior on color { ColorAnimation { duration: 100 } }
                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 10; anchors.rightMargin: 10
                        spacing: 8
                        Text {
                            Layout.fillWidth: true
                            elide: Text.ElideRight
                            text: drawer._oemLabel()
                            color: drawer._oemKey() ? Theme.text : Theme.warning
                            font.family: Theme.fontFamily
                            font.pixelSize: 13
                        }
                        Text {
                            text: drawer._oemKey() ? "SELECTED" : "REQUIRED"
                            color: drawer._oemKey() ? Theme.textMuted : Theme.warning
                            font.family: Theme.fontFamily
                            font.pixelSize: 9
                            font.weight: Font.Medium
                            font.letterSpacing: 0.5
                        }
                        Text { text: "▾"; color: Theme.textMuted; font.pixelSize: 10 }
                    }
                    MouseArea {
                        id: oemMa
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: oemPopup.open()
                    }
                }
            }

            SectionTitle { text: "FIRMWARE" }
            InfoRow { label: "BMC";  value: drawer._or(drawer.data && drawer.data.bmc_version); mono: true }
            InfoRow { label: "BIOS"; value: drawer._or(drawer.data && drawer.data.bios_version); mono: true }

            SectionTitle { text: "STATUS" }
            InfoRow { label: "State";    value: (drawer.data && drawer.data.status) ? String(drawer.data.status).toUpperCase() : "—" }
            InfoRow { label: "Power";    value: drawer._or(drawer._livePower || (drawer.data && drawer.data.power_state)) }
            InfoRow { label: "Protocol"; value: drawer._or(drawer.data && drawer.data.protocol) }

            SectionTitle { text: "POWER CONTROL" }
            GridLayout {
                columns: 2
                columnSpacing: 8
                rowSpacing: 8
                Layout.fillWidth: true
                PowerBtn { action: "on";        label: "Power On";  tint: Theme.success }
                PowerBtn { action: "restart";   label: "Restart";   tint: Theme.accent }
                PowerBtn { action: "off";       label: "Shut Down"; tint: Theme.warning }
                PowerBtn { action: "force_off"; label: "Force Off"; tint: Theme.error }
            }

            Item { Layout.fillHeight: true }
        }

        // Actions
        RowLayout {
            id: actions
            anchors.bottom: parent.bottom
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.margins: 16
            spacing: 8
            Item { Layout.fillWidth: true }
            PrimaryButton {
                label: "Remove"
                primary: false
                onClicked: {
                    if (drawer.data) {
                        var msg = "Removed " + (drawer.data.hostname || drawer.data.ip)
                        var sid = drawer.data.id
                        drawer.data = null
                        Fleet.remove(sid)
                        App.showToast(msg, "info")
                    }
                }
            }
        }
    }

    // ---- power control ----
    function requestPower(action, label) {
        drawer._pendingAction = action
        drawer._pendingLabel = label
        confirmPopup.open()
    }

    // When a power action completes for THIS server, reflect the new state.
    Connections {
        target: Power
        function onResultReceived(serverId, action, ok, message, powerState) {
            if (drawer.data && serverId === drawer.data.id && powerState)
                drawer._livePower = powerState
        }
    }

    // ---- manual OEM override picker ----
    Popup {
        id: oemPopup
        modal: true
        dim: true
        anchors.centerIn: Overlay.overlay
        width: 280
        padding: 0
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: Rectangle { color: Theme.card; radius: 14; border.color: Theme.border; border.width: 1 }
        contentItem: ColumnLayout {
            spacing: 0
            Text {
                Layout.margins: 16; Layout.bottomMargin: 10
                text: "OEM routing"
                color: Theme.text; font.family: Theme.fontFamily
                font.pixelSize: 14; font.weight: Font.DemiBold
            }
            Text {
                Layout.leftMargin: 16; Layout.rightMargin: 16; Layout.bottomMargin: 10
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: "Required: pick the vendor whose flash recipe this server uses. Detected manufacturer: " +
                      ((drawer.data && drawer.data.manufacturer) || "unknown")
                color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11
            }
            Repeater {
                model: Fleet.oemOptions
                delegate: Rectangle {
                    required property var modelData
                    Layout.fillWidth: true
                    implicitHeight: 34
                    color: optMa.containsMouse ? Theme.hover : "transparent"
                    Behavior on color { ColorAnimation { duration: 80 } }
                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 16; anchors.rightMargin: 16
                        spacing: 8
                        Text {
                            Layout.fillWidth: true
                            text: modelData.name
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: 13
                        }
                        Text {
                            visible: drawer._oemKey() === modelData.key
                            text: "✓"
                            color: Theme.accent
                            font.pixelSize: 13
                            font.weight: Font.Bold
                        }
                    }
                    MouseArea {
                        id: optMa
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            if (drawer.data) {
                                Fleet.setOemOverride(drawer.data.id, modelData.key)
                                drawer._liveOemKey = modelData.key
                                App.showToast(
                                    "OEM set to " + modelData.name + " for " +
                                    (drawer.data.hostname || drawer.data.ip),
                                    "success")
                            }
                            oemPopup.close()
                        }
                    }
                }
            }

            // Clear the selection — back to the REQUIRED state. IPs are
            // dynamic; the operator must be able to unbind a pick when the
            // address moves to different hardware.
            Rectangle {
                visible: drawer._oemKey() !== ""
                Layout.fillWidth: true
                Layout.topMargin: 4
                implicitHeight: 36
                color: clearMa.containsMouse ? Theme.hover : "transparent"
                Behavior on color { ColorAnimation { duration: 80 } }
                Rectangle { width: parent.width; height: 1; color: Theme.border; opacity: 0.6 }
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 16; anchors.rightMargin: 16
                    Text {
                        Layout.fillWidth: true
                        text: "✕  Clear selection"
                        color: Theme.error
                        font.family: Theme.fontFamily
                        font.pixelSize: 13
                    }
                }
                MouseArea {
                    id: clearMa
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        if (drawer.data) {
                            Fleet.setOemOverride(drawer.data.id, "")
                            drawer._liveOemKey = ""
                            App.showToast(
                                "OEM selection cleared for " +
                                (drawer.data.hostname || drawer.data.ip), "info")
                        }
                        oemPopup.close()
                    }
                }
            }
            Item { implicitHeight: 10 }
        }
    }

    Popup {
        id: confirmPopup
        modal: true
        dim: true
        anchors.centerIn: Overlay.overlay
        width: 320
        padding: 0
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: Rectangle { color: Theme.card; radius: 14; border.color: Theme.border; border.width: 1 }
        contentItem: ColumnLayout {
            spacing: 14
            Text {
                Layout.margins: 20; Layout.bottomMargin: 0
                text: "Confirm power action"
                color: Theme.text; font.family: Theme.fontFamily
                font.pixelSize: 15; font.weight: Font.DemiBold
            }
            Text {
                Layout.leftMargin: 20; Layout.rightMargin: 20; Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: drawer._pendingLabel + " — " +
                      (drawer.data ? (drawer.data.hostname || drawer.data.ip) : "this server") + "?"
                color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 13
            }
            RowLayout {
                Layout.fillWidth: true; Layout.margins: 20; Layout.topMargin: 4
                Item { Layout.fillWidth: true }
                PrimaryButton { label: "Cancel"; primary: false; onClicked: confirmPopup.close() }
                PrimaryButton {
                    label: "Confirm"
                    onClicked: {
                        if (drawer.data) Power.applyOne(drawer.data.id, drawer._pendingAction)
                        confirmPopup.close()
                    }
                }
            }
        }
    }

    // ---- helpers ----
    function _or(v) { return (v === undefined || v === null || v === "") ? "—" : v }

    component SectionTitle: Text {
        color: Theme.textMuted
        font.family: Theme.fontFamily
        font.pixelSize: 11
        font.weight: Font.Medium
        font.letterSpacing: 0.6
        Layout.bottomMargin: -8
    }

    component InfoRow: RowLayout {
        id: ir
        property string label: ""
        property string value: ""
        property bool mono: false
        Layout.fillWidth: true
        spacing: 12
        Text {
            text: ir.label
            Layout.preferredWidth: 100
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: 12
        }
        Text {
            text: ir.value
            Layout.fillWidth: true
            elide: Text.ElideRight
            color: Theme.text
            font.family: ir.mono ? Theme.monoFamily : Theme.fontFamily
            font.pixelSize: 13
        }
    }

    component PowerBtn: Rectangle {
        id: pb
        property string action: ""
        property string label: ""
        property color tint: Theme.accent
        Layout.fillWidth: true
        implicitHeight: 34
        radius: 9
        color: pbMa.containsMouse ? Qt.rgba(pb.tint.r, pb.tint.g, pb.tint.b, 0.16) : Theme.hover
        border.color: pbMa.containsMouse ? pb.tint : Theme.border
        border.width: 1
        Behavior on color { ColorAnimation { duration: 100 } }
        enabled: !Power.busy && drawer.data !== null
        opacity: enabled ? 1.0 : 0.5
        Text {
            anchors.centerIn: parent
            text: pb.label
            color: pb.tint
            font.family: Theme.fontFamily
            font.pixelSize: 12
            font.weight: Font.Medium
        }
        MouseArea {
            id: pbMa
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            enabled: pb.enabled
            onClicked: drawer.requestPower(pb.action, pb.label)
        }
    }
}
