import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: page

    property var selectedServer: null
    // Reactive mirror of the persisted ``welcome_dismissed`` setting. The
    // settings slot has no notify signal (it's a plain pyqtSlot), so a
    // visible-binding that calls AppSettings.getBool() never re-evaluates.
    // The dismiss button writes the setting AND flips this property so
    // every dependent visibility binding updates in one frame.
    property bool welcomeDismissed: AppSettings.getBool("welcome_dismissed", false)

    function statusLabel(s) {
        if (s === "online") return "Online"
        if (s === "needs_update") return "Needs Update"
        if (s === "failed") return "Failed"
        if (s === "offline") return "Offline"
        if (s === "updating") return "Updating"
        return s
    }
    function statusColor(s) {
        if (s === "online") return Theme.online
        if (s === "needs_update") return Theme.warning
        if (s === "failed") return Theme.error
        if (s === "updating") return Theme.accent
        return Theme.textMuted
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 24
        spacing: 16

        RowLayout {
            Layout.fillWidth: true
            spacing: 12
            SearchField {
                Layout.preferredWidth: 320
                placeholderText: "Search by IP, hostname, model…"
                onTextChanged: Fleet.model.filterText = text
            }
            Item { Layout.fillWidth: true }
            Chip { label: Fleet.model.count + " shown" }
            PrimaryButton {
                label: "Update " + Fleet.selectedCount + " selected"
                visible: Fleet.selectedCount > 0
                onClicked: App.navigateTo(2)
            }
            PrimaryButton { label: "+  Discover"; onClicked: discoveryDialog.open() }
        }

        Card {
            Layout.fillWidth: true
            Layout.fillHeight: true
            padding: 0

            ColumnLayout {
                anchors.fill: parent
                spacing: 0

                Item {
                    Layout.fillWidth: true
                    implicitHeight: 44
                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 20
                        anchors.rightMargin: 20
                        spacing: 16
                        SelectCheck {
                            Layout.alignment: Qt.AlignVCenter
                            selected: Fleet.selectedCount > 0 && Fleet.selectedCount === Fleet.model.count
                            onToggled: Fleet.selectedCount > 0 ? Fleet.clearSelection() : Fleet.selectAll()
                        }
                        HeaderCell { label: "IP Address"; field: "ip"; pw: 120 }
                        HeaderCell { label: "Hostname"; field: "hostname"; fill: true }
                        HeaderCell { label: "OEM"; field: "oem"; pw: 110 }
                        HeaderCell { label: "Model"; field: "model_name"; pw: 130 }
                        HeaderCell { label: "BMC"; field: "bmc_version"; pw: 84 }
                        HeaderCell { label: "BIOS"; field: "bios_version"; pw: 70 }
                        HeaderCell { label: "Status"; field: "status"; pw: 120 }
                        Item { Layout.preferredWidth: 28 }   // remove (✕) column
                    }
                    Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.border }
                }

                Item {
                    Layout.fillWidth: true
                    Layout.fillHeight: true

                    ListView {
                        id: list
                        anchors.fill: parent
                        clip: true
                        model: Fleet.model
                        boundsBehavior: Flickable.StopAtBounds
                        delegate: Item {
                            width: ListView.view.width
                            implicitHeight: 50

                            // Row body click → open detail drawer (SelectCheck's own
                            // MouseArea consumes clicks on the checkbox).
                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: page.selectedServer = ({
                                    id: model.id, ip: model.ip, hostname: model.hostname,
                                    manufacturer: model.manufacturer, oem: model.oem,
                                    oem_override: model.oem_override,   // so the drawer shows the saved pick + Clear
                                    model_name: model.model_name, serial: model.serial,
                                    bmc_version: model.bmc_version, bios_version: model.bios_version,
                                    power_state: model.power_state, status: model.status,
                                    protocol: model.protocol
                                })
                            }

                            Rectangle { anchors.fill: parent; color: hover.hovered ? Theme.hover : "transparent"; Behavior on color { ColorAnimation { duration: 120 } } }
                            HoverHandler { id: hover }

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 20
                                anchors.rightMargin: 20
                                spacing: 16
                                SelectCheck {
                                    Layout.alignment: Qt.AlignVCenter
                                    selected: Fleet.selectedIds.indexOf(model.id) >= 0
                                    onToggled: Fleet.toggleSelection(model.id)
                                }
                                BodyCell { text: model.ip; pw: 120; mono: true }
                                BodyCell { text: model.hostname; fill: true; strong: true }
                                // OEM — INLINE picker right in the row. Manual-only
                                // routing: click to set / change / clear the vendor
                                // without opening the drawer (its own MouseArea
                                // consumes the click so the row-open doesn't fire).
                                Item {
                                    id: oemCell
                                    Layout.preferredWidth: 110
                                    Layout.alignment: Qt.AlignVCenter
                                    implicitHeight: 30
                                    property int sid: model.id
                                    property string current: model.oem_override ? model.oem_override : ""

                                    Rectangle {
                                        anchors.verticalCenter: parent.verticalCenter
                                        width: parent.width; height: 28; radius: 7
                                        color: oemCellMa.containsMouse ? Theme.hover : "transparent"
                                        border.color: oemCell.current ? Theme.border : Theme.warning
                                        border.width: 1
                                        Behavior on color { ColorAnimation { duration: 100 } }
                                        Row {
                                            anchors.fill: parent
                                            anchors.leftMargin: 8; anchors.rightMargin: 6
                                            spacing: 4
                                            Text {
                                                anchors.verticalCenter: parent.verticalCenter
                                                width: parent.width - 16
                                                elide: Text.ElideRight
                                                text: oemCell.current ? oemCell.current : "Select OEM…"
                                                color: oemCell.current ? Theme.text : Theme.warning
                                                font.family: Theme.fontFamily; font.pixelSize: 12
                                            }
                                            Text {
                                                anchors.verticalCenter: parent.verticalCenter
                                                text: "▾"; color: Theme.textMuted; font.pixelSize: 9
                                            }
                                        }
                                        MouseArea {
                                            id: oemCellMa
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: cellPopup.open()
                                        }
                                    }

                                    Popup {
                                        id: cellPopup
                                        y: oemCell.height + 2
                                        width: 200
                                        padding: 0
                                        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
                                        background: Rectangle { color: Theme.card; radius: 10; border.color: Theme.border; border.width: 1 }
                                        contentItem: ColumnLayout {
                                            spacing: 0
                                            Repeater {
                                                model: Fleet.oemOptions
                                                delegate: Rectangle {
                                                    required property var modelData
                                                    Layout.fillWidth: true
                                                    implicitHeight: 32
                                                    color: itemMa.containsMouse ? Theme.hover : "transparent"
                                                    Behavior on color { ColorAnimation { duration: 80 } }
                                                    Row {
                                                        anchors.fill: parent
                                                        anchors.leftMargin: 12; anchors.rightMargin: 12
                                                        Text {
                                                            anchors.verticalCenter: parent.verticalCenter
                                                            width: parent.width - 16
                                                            elide: Text.ElideRight
                                                            text: modelData.name
                                                            color: Theme.text
                                                            font.family: Theme.fontFamily; font.pixelSize: 13
                                                        }
                                                        Text {
                                                            anchors.verticalCenter: parent.verticalCenter
                                                            visible: oemCell.current === modelData.key
                                                            text: "✓"; color: Theme.accent
                                                            font.pixelSize: 13; font.weight: Font.Bold
                                                        }
                                                    }
                                                    MouseArea {
                                                        id: itemMa
                                                        anchors.fill: parent
                                                        hoverEnabled: true
                                                        cursorShape: Qt.PointingHandCursor
                                                        onClicked: {
                                                            var nm = modelData.name, key = modelData.key, sid = oemCell.sid
                                                            cellPopup.close()
                                                            Fleet.setOemOverride(sid, key)
                                                            App.showToast("OEM set to " + nm, "success")
                                                        }
                                                    }
                                                }
                                            }
                                            Rectangle {
                                                visible: oemCell.current !== ""
                                                Layout.fillWidth: true
                                                implicitHeight: 34
                                                color: clearMa.containsMouse ? Theme.hover : "transparent"
                                                Rectangle { width: parent.width; height: 1; color: Theme.border; opacity: 0.6 }
                                                Text {
                                                    anchors.verticalCenter: parent.verticalCenter
                                                    x: 12
                                                    text: "✕  Clear selection"
                                                    color: Theme.error
                                                    font.family: Theme.fontFamily; font.pixelSize: 13
                                                }
                                                MouseArea {
                                                    id: clearMa
                                                    anchors.fill: parent
                                                    hoverEnabled: true
                                                    cursorShape: Qt.PointingHandCursor
                                                    onClicked: {
                                                        var sid = oemCell.sid
                                                        cellPopup.close()
                                                        Fleet.setOemOverride(sid, "")
                                                        App.showToast("OEM selection cleared", "info")
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                                BodyCell { text: model.model_name; pw: 130; mono: true }
                                BodyCell { text: model.bmc_version; pw: 84; mono: true }
                                BodyCell { text: model.bios_version; pw: 70; mono: true }
                                Item {
                                    Layout.preferredWidth: 120
                                    Layout.alignment: Qt.AlignVCenter
                                    StatusPill {
                                        anchors.verticalCenter: parent.verticalCenter
                                        label: page.statusLabel(model.status)
                                        pillColor: page.statusColor(model.status)
                                    }
                                }
                                // Per-row remove (✕) — hover-revealed, deletes the
                                // server (re-discoverable). Own MouseArea consumes the
                                // click so the row-open/drawer doesn't fire.
                                Item {
                                    Layout.preferredWidth: 28
                                    Layout.alignment: Qt.AlignVCenter
                                    implicitHeight: 28
                                    Rectangle {
                                        anchors.centerIn: parent
                                        width: 24; height: 24; radius: 7
                                        visible: hover.hovered
                                        color: delMa.containsMouse ? Theme.error : "transparent"
                                        Text { anchors.centerIn: parent; text: "✕"; color: delMa.containsMouse ? "#FFFFFF" : Theme.textMuted; font.pixelSize: 12 }
                                        MouseArea {
                                            id: delMa
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: {
                                                var nm = model.hostname || model.ip
                                                var sid = model.id
                                                Fleet.remove(sid)
                                                App.showToast("Removed " + nm, "info")
                                            }
                                        }
                                    }
                                }
                            }
                            Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.border; opacity: 0.6 }
                        }
                    }

                    // Welcome card — only shown to genuinely new users
                    // (no servers AND no credentials). After they discover
                    // anything OR add a credential, we fall back to the
                    // smaller empty state below.
                    Item {
                        anchors.fill: parent
                        anchors.margins: 24
                        visible: list.count === 0
                                 && Fleet.total === 0
                                 && Credentials.count === 0
                                 && !page.welcomeDismissed
                        Rectangle {
                            anchors.centerIn: parent
                            implicitWidth: Math.min(parent.width, 640)
                            implicitHeight: welcomeCol.implicitHeight + 48
                            color: Theme.elevated
                            radius: 14
                            border.color: Theme.border
                            border.width: 1
                            ColumnLayout {
                                id: welcomeCol
                                anchors.centerIn: parent
                                width: parent.width - 56
                                spacing: 12
                                Text {
                                    text: "Welcome to ClusterOne"
                                    color: Theme.text
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 18
                                    font.weight: Font.DemiBold
                                    Layout.alignment: Qt.AlignHCenter
                                }
                                Text {
                                    text: "Three steps to a working fleet view."
                                    color: Theme.textMuted
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 13
                                    Layout.alignment: Qt.AlignHCenter
                                }
                                Item { Layout.preferredHeight: 6 }
                                WelcomeStep {
                                    Layout.fillWidth: true
                                    n: "1"; title: "Save a BMC credential"
                                    hint: "Default username + password for your servers."
                                    actionLabel: "Open Settings"
                                    onActivate: App.openAddCredential()
                                }
                                WelcomeStep {
                                    Layout.fillWidth: true
                                    n: "2"; title: "Run discovery"
                                    hint: "Scan a subnet, CIDR, or simulator port range. "
                                          + "Try \"127.0.0.1:8000-8007\" for the bundled simulator."
                                    actionLabel: "Run Discovery"
                                    onActivate: discoveryDialog.open()
                                }
                                WelcomeStep {
                                    Layout.fillWidth: true
                                    n: "3"; title: "Add a firmware image or ISO"
                                    hint: "Then head to Updates or Provision to roll changes across the fleet."
                                    actionLabel: "Add Firmware"
                                    onActivate: App.openAddFirmware()
                                }
                                Item { Layout.preferredHeight: 4 }
                                RowLayout {
                                    Layout.fillWidth: true
                                    Item { Layout.fillWidth: true }
                                    PrimaryButton {
                                        label: "Dismiss"
                                        primary: false
                                        onClicked: {
                                            AppSettings.setBool("welcome_dismissed", true)
                                            // Flip the reactive mirror so dependent
                                            // visible-bindings update without a full
                                            // Fleet.refresh() (which was O(N) on the
                                            // list model and triggered every delegate
                                            // to rebuild).
                                            page.welcomeDismissed = true
                                        }
                                    }
                                }
                            }
                        }
                    }

                    Column {
                        anchors.centerIn: parent
                        spacing: 10
                        visible: list.count === 0
                                 && !(Fleet.total === 0
                                      && Credentials.count === 0
                                      && !page.welcomeDismissed)
                        Text { anchors.horizontalCenter: parent.horizontalCenter; text: "No servers"; color: Theme.text; font.family: Theme.fontFamily; font.pixelSize: 15; font.weight: Font.DemiBold }
                        Text { anchors.horizontalCenter: parent.horizontalCenter; text: "Run discovery to populate the inventory"; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 13 }
                        Item {
                            anchors.horizontalCenter: parent.horizontalCenter
                            implicitHeight: emptyCta.implicitHeight
                            implicitWidth: emptyCta.implicitWidth
                            PrimaryButton { id: emptyCta; label: "Run Discovery"; onClicked: discoveryDialog.open() }
                        }
                    }

                    // Compact welcome-step row, used 3× above.
                    component WelcomeStep: RowLayout {
                        id: ws
                        property string n: ""
                        property string title: ""
                        property string hint: ""
                        property string actionLabel: ""
                        signal activate()
                        spacing: 14
                        Rectangle {
                            implicitWidth: 26; implicitHeight: 26; radius: 13
                            color: Theme.hover
                            border.color: Theme.border; border.width: 1
                            Text {
                                anchors.centerIn: parent
                                text: ws.n
                                color: Theme.accent
                                font.family: Theme.monoFamily
                                font.pixelSize: 12
                                font.weight: Font.Bold
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 1
                            Text {
                                text: ws.title
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: 13
                                font.weight: Font.DemiBold
                            }
                            Text {
                                text: ws.hint
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: 11
                                wrapMode: Text.WordWrap
                                Layout.fillWidth: true
                            }
                        }
                        PrimaryButton {
                            label: ws.actionLabel
                            primary: false
                            onClicked: ws.activate()
                        }
                    }
                }
            }
        }
    }

    DiscoveryDialog { id: discoveryDialog }

    // Global "Run Discovery" entry point — fired by the Inventory Welcome
    // card so the third step always opens the right dialog regardless of
    // which page hosts it.
    Connections {
        target: App
        function onDiscoveryRequested() { discoveryDialog.open() }
    }

    ServerDetailDrawer { data: page.selectedServer }

    // ---- inline helpers ----
    component Chip: Rectangle {
        property string label: ""
        implicitHeight: 34
        implicitWidth: t.implicitWidth + 28
        radius: 10
        color: ma.containsMouse ? Theme.hover : "transparent"
        border.color: Theme.border
        border.width: 1
        Behavior on color { ColorAnimation { duration: 120 } }
        Text { id: t; anchors.centerIn: parent; text: parent.label; color: Theme.text; font.family: Theme.fontFamily; font.pixelSize: 12 }
        MouseArea { id: ma; anchors.fill: parent; hoverEnabled: true }
    }

    component HeaderCell: Item {
        property string label: ""
        property string field: ""
        property int pw: 0
        property bool fill: false
        // Fixed columns never shrink below their declared width; the fill
        // (Hostname) column gets a generous minimum so it never collapses
        // to zero and forces OEM on top of it.
        Layout.fillWidth:      fill
        Layout.preferredWidth: pw > 0 ? pw : txt.implicitWidth
        Layout.minimumWidth:   fill ? 80 : (pw > 0 ? pw : txt.implicitWidth)
        Layout.maximumWidth:   fill ? 99999 : (pw > 0 ? pw : 9999)
        Layout.alignment: Qt.AlignVCenter
        implicitHeight: 20
        clip: true
        Text {
            id: txt
            anchors.verticalCenter: parent.verticalCenter
            text: label
            color: hh.hovered ? Theme.text : Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: 11
            font.weight: Font.Medium
            elide: Text.ElideRight
            width: parent.width
        }
        HoverHandler { id: hh }
        TapHandler { onTapped: if (field) Fleet.model.sortByField(field) }
    }

    component BodyCell: Text {
        property int pw: 0
        property bool fill: false
        property bool strong: false
        property bool mono: false
        color: strong ? Theme.text : Theme.textMuted
        font.family: mono ? Theme.monoFamily : Theme.fontFamily
        font.pixelSize: 13
        font.weight: strong ? Font.DemiBold : Font.Normal
        elide: Text.ElideRight
        verticalAlignment: Text.AlignVCenter
        Layout.fillWidth:    fill
        Layout.preferredWidth: pw > 0 ? pw : implicitWidth
        Layout.minimumWidth:   fill ? 80 : (pw > 0 ? pw : 0)
        Layout.maximumWidth:   fill ? 99999 : (pw > 0 ? pw : 9999)
        Layout.alignment: Qt.AlignVCenter
        clip: true
    }

    component SelectCheck: Rectangle {
        property bool selected: false
        signal toggled()
        implicitWidth: 18; implicitHeight: 18; radius: 4
        color: selected ? Theme.accent : "transparent"
        border.color: selected ? Theme.accent : Theme.border
        border.width: 1
        Behavior on color { ColorAnimation { duration: 120 } }
        Text {
            anchors.centerIn: parent
            visible: parent.selected
            text: "✓"
            color: Theme.accentText
            font.pixelSize: 11
            font.weight: Font.Bold
        }
        // MouseArea (not TapHandler) so clicks are consumed and don't
        // propagate to the row-body click handler that opens the drawer.
        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: toggled()
        }
    }
}
