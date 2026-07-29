import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "../components"

Item {
    id: page

    function typeColor(t) {
        if (t === "BMC") return Theme.accent
        if (t === "BIOS") return Theme.success
        return Theme.warning
    }

    // Format ISO timestamp → "01 Jun 2026  04:40"
    function fmtDate(iso) {
        if (!iso) return "—"
        try {
            var d = new Date(iso)
            if (isNaN(d.getTime())) return iso.substring(0, 10)
            var months = ["Jan","Feb","Mar","Apr","May","Jun",
                          "Jul","Aug","Sep","Oct","Nov","Dec"]
            var day  = ("0" + d.getDate()).slice(-2)
            var mon  = months[d.getMonth()]
            var year = d.getFullYear()
            var hh   = ("0" + d.getHours()).slice(-2)
            var mm   = ("0" + d.getMinutes()).slice(-2)
            return day + " " + mon + " " + year + "  " + hh + ":" + mm
        } catch (e) {
            return iso.substring(0, 10)
        }
    }

    // Truncate a long filename — keep first 18 + "…" + extension
    function shortName(n) {
        if (!n || n.length <= 28) return n || ""
        var dot = n.lastIndexOf(".")
        var ext = dot > 0 ? n.substring(dot) : ""
        return n.substring(0, 18) + "…" + ext
    }

    FileDialog {
        id: fileDialog
        title: "Select firmware image"
        onAccepted: {
            form.fileUrl = selectedFile
            form.fileName = ("" + selectedFile).split("/").pop()
            form.open()
        }
    }

    // Action entry point — used by global "Add Firmware" affordances
    // (e.g. the Inventory Welcome card).
    Connections {
        target: App
        function onAddFirmwareRequested() { fileDialog.open() }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 24
        spacing: 16

        RowLayout {
            Layout.fillWidth: true
            Text {
                text: Firmware.count + " firmware images"
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: 13
            }
            Item { Layout.fillWidth: true }
            PrimaryButton { label: "+  Add Firmware"; onClicked: fileDialog.open() }
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
                        anchors.fill: parent; anchors.leftMargin: 20; anchors.rightMargin: 20; spacing: 16
                        Text { text: "Name"; Layout.fillWidth: true; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11; font.weight: Font.Medium }
                        Text { text: "Type"; Layout.preferredWidth: 80; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11; font.weight: Font.Medium }
                        Text { text: "OEM"; Layout.preferredWidth: 120; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11; font.weight: Font.Medium }
                        Text { text: "Version"; Layout.preferredWidth: 100; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11; font.weight: Font.Medium }
                        Text { text: "Uploaded"; Layout.preferredWidth: 148; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11; font.weight: Font.Medium }
                        Item { Layout.preferredWidth: 28 }
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
                        model: Firmware.model
                        boundsBehavior: Flickable.StopAtBounds
                        delegate: Item {
                            width: ListView.view.width
                            implicitHeight: 50
                            Rectangle { anchors.fill: parent; color: hover.hovered ? Theme.hover : "transparent"; Behavior on color { ColorAnimation { duration: 120 } } }
                            HoverHandler { id: hover }
                            RowLayout {
                                anchors.fill: parent; anchors.leftMargin: 20; anchors.rightMargin: 20; spacing: 16
                                // Name — truncated cleanly with tooltip showing full name
                                Text {
                                    text: page.shortName(model.name || "")
                                    Layout.fillWidth: true
                                    elide: Text.ElideRight
                                    color: Theme.text
                                    font.family: Theme.monoFamily
                                    font.pixelSize: 13
                                    font.weight: Font.DemiBold
                                    ToolTip.visible: hover.hovered && (model.name || "").length > 28
                                    ToolTip.text: model.name || ""
                                    ToolTip.delay: 500
                                }
                                Item {
                                    Layout.preferredWidth: 80
                                    StatusPill { anchors.verticalCenter: parent.verticalCenter; label: model.type || ""; pillColor: page.typeColor(model.type || "") }
                                }
                                Text { text: (model.oem || "").charAt(0).toUpperCase() + (model.oem || "").slice(1); Layout.preferredWidth: 120; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 13 }
                                Text { text: model.version || ""; Layout.preferredWidth: 100; color: Theme.text; font.family: Theme.monoFamily; font.pixelSize: 13 }
                                Text { text: page.fmtDate(model.uploaded_at); Layout.preferredWidth: 148; color: Theme.textMuted; font.family: Theme.monoFamily; font.pixelSize: 12 }
                                Item {
                                    Layout.preferredWidth: 28
                                    implicitHeight: 28
                                    Rectangle {
                                        anchors.centerIn: parent
                                        width: 24; height: 24; radius: 7
                                        visible: hover.hovered
                                        color: del.containsMouse ? Theme.error : "transparent"
                                        Text { anchors.centerIn: parent; text: "✕"; color: del.containsMouse ? "#FFFFFF" : Theme.textMuted; font.pixelSize: 12 }
                                        MouseArea { id: del; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: Firmware.remove(model.id) }
                                    }
                                }
                            }
                            Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.border; opacity: 0.6 }
                        }
                    }

                    Column {
                        anchors.centerIn: parent
                        spacing: 6
                        visible: list.count === 0
                        Text { anchors.horizontalCenter: parent.horizontalCenter; text: "No firmware"; color: Theme.text; font.family: Theme.fontFamily; font.pixelSize: 15; font.weight: Font.DemiBold }
                        Text { anchors.horizontalCenter: parent.horizontalCenter; text: "Add a BMC, BIOS, or HGX image to get started"; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 13 }
                    }
                }
            }
        }
    }

    // ---- Add Firmware form ----
    Popup {
        id: form
        property url fileUrl: ""
        property string fileName: ""
        property string fwType: "BIOS"

        width: 440
        x: Math.round((page.width - width) / 2)
        y: Math.round((page.height - height) / 2)
        modal: true
        dim: true
        padding: 0
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

        background: Rectangle {
            color: Theme.card
            radius: 14
            border.color: Theme.border
            border.width: 1
        }

        contentItem: ColumnLayout {
            spacing: 14

            Text {
                Layout.margins: 20
                Layout.bottomMargin: 0
                text: "Add Firmware"
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: 16
                font.weight: Font.DemiBold
            }

            ColumnLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 20
                Layout.rightMargin: 20
                spacing: 12

                Text { text: form.fileName; color: Theme.textMuted; font.family: Theme.monoFamily; font.pixelSize: 12; elide: Text.ElideMiddle; Layout.fillWidth: true }

                // type segmented
                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: 34
                    radius: 9
                    color: Theme.hover
                    border.color: Theme.border
                    border.width: 1
                    Row {
                        anchors.fill: parent
                        anchors.margins: 3
                        spacing: 3
                        Repeater {
                            model: ["BMC", "BIOS", "HGX"]
                            delegate: Rectangle {
                                required property string modelData
                                width: (parent.width - 6) / 3
                                height: parent.height
                                radius: 7
                                color: form.fwType === modelData ? Theme.accent : "transparent"
                                Behavior on color { ColorAnimation { duration: 150 } }
                                Text { anchors.centerIn: parent; text: modelData; color: form.fwType === modelData ? Theme.accentText : Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 12; font.weight: Font.Medium }
                                TapHandler { onTapped: form.fwType = modelData }
                            }
                        }
                    }
                }

                SearchField { id: oemField; Layout.fillWidth: true; placeholderText: "OEM (e.g. supermicro)" }
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.margins: 20
                Layout.topMargin: 4
                Item { Layout.fillWidth: true }
                PrimaryButton { label: "Cancel"; primary: false; onClicked: form.close() }
                PrimaryButton {
                    id: fwAddBtn
                    label: "Add"
                    enabled: oemField.text.length > 0
                    onClicked: {
                        Firmware.add(form.fileUrl, form.fwType, oemField.text, "", "", "")
                        oemField.text = ""
                        form.close()
                    }
                }
            }
        }

        Shortcut {
            sequence: "Return"
            enabled: form.opened
            onActivated: if (fwAddBtn.enabled) fwAddBtn.clicked()
        }
        Shortcut {
            sequence: "Enter"
            enabled: form.opened
            onActivated: if (fwAddBtn.enabled) fwAddBtn.clicked()
        }
    }
}
