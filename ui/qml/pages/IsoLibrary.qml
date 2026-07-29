import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "../components"

Item {
    id: page

    function familyColor(f) {
        if (f === "rhel" || f === "rocky" || f === "alma") return Theme.accent
        if (f === "ubuntu" || f === "debian") return Theme.success
        if (f === "esxi" || f === "proxmox") return Theme.warning
        if (f === "windows") return Theme.error
        return Theme.textMuted
    }

    function humanSize(bytes) {
        if (!bytes || bytes <= 0) return "—"
        var gb = bytes / (1000 * 1000 * 1000)
        if (gb >= 1) return gb.toFixed(2) + " GB"
        var mb = bytes / (1000 * 1000)
        return mb.toFixed(0) + " MB"
    }

    FileDialog {
        id: fileDialog
        title: "Select ISO image"
        nameFilters: ["ISO images (*.iso *.img)", "All files (*)"]
        onAccepted: {
            form.fileUrl = selectedFile
            form.fileName = ("" + selectedFile).split("/").pop()
            form.open()
        }
    }

    // Action entry point — used by global "Add ISO" affordances
    // (e.g. the Inventory Welcome card).
    Connections {
        target: App
        function onAddIsoRequested() { fileDialog.open() }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 24
        spacing: 16

        RowLayout {
            Layout.fillWidth: true
            Text {
                text: Isos.count + " ISO" + (Isos.count === 1 ? "" : "s") + " in library"
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: 13
            }
            Item { Layout.fillWidth: true }
            PrimaryButton { label: "+  Add ISO"; onClicked: fileDialog.open() }
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
                        anchors.leftMargin: 20; anchors.rightMargin: 20
                        spacing: 16
                        Text { text: "Name";    Layout.fillWidth: true; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11; font.weight: Font.Medium }
                        Text { text: "OS";      Layout.preferredWidth: 90;  color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11; font.weight: Font.Medium }
                        Text { text: "Version"; Layout.preferredWidth: 80;  color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11; font.weight: Font.Medium }
                        Text { text: "Arch";    Layout.preferredWidth: 60;  color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11; font.weight: Font.Medium }
                        Text { text: "Size";    Layout.preferredWidth: 80;  color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11; font.weight: Font.Medium }
                        Text { text: "Added";   Layout.preferredWidth: 110; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11; font.weight: Font.Medium }
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
                        model: Isos.model
                        boundsBehavior: Flickable.StopAtBounds
                        delegate: Item {
                            width: ListView.view.width
                            implicitHeight: 50
                            Rectangle {
                                anchors.fill: parent
                                color: hover.hovered ? Theme.hover : "transparent"
                                Behavior on color { ColorAnimation { duration: 120 } }
                            }
                            HoverHandler { id: hover }
                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 20; anchors.rightMargin: 20
                                spacing: 16
                                Text {
                                    text: model.name || ""
                                    Layout.fillWidth: true
                                    elide: Text.ElideRight
                                    color: Theme.text
                                    font.family: Theme.monoFamily
                                    font.pixelSize: 13
                                    font.weight: Font.DemiBold
                                }
                                Item {
                                    Layout.preferredWidth: 90
                                    StatusPill {
                                        anchors.verticalCenter: parent.verticalCenter
                                        label: (model.os_family || "?").toUpperCase()
                                        pillColor: page.familyColor(model.os_family || "")
                                    }
                                }
                                Text { text: model.os_version || ""; Layout.preferredWidth: 80; color: Theme.text;      font.family: Theme.monoFamily; font.pixelSize: 13 }
                                Text { text: model.arch || "";       Layout.preferredWidth: 60; color: Theme.textMuted; font.family: Theme.monoFamily; font.pixelSize: 12 }
                                Text { text: page.humanSize(model.size_bytes); Layout.preferredWidth: 80; color: Theme.text; font.family: Theme.monoFamily; font.pixelSize: 13 }
                                Text { text: model.uploaded_at || ""; Layout.preferredWidth: 110; color: Theme.textMuted; font.family: Theme.monoFamily; font.pixelSize: 13 }
                                Item {
                                    Layout.preferredWidth: 28
                                    implicitHeight: 28
                                    Rectangle {
                                        anchors.centerIn: parent
                                        width: 24; height: 24; radius: 7
                                        visible: hover.hovered
                                        color: del.containsMouse ? Theme.error : "transparent"
                                        Text {
                                            anchors.centerIn: parent
                                            text: "✕"
                                            color: del.containsMouse ? "#FFFFFF" : Theme.textMuted
                                            font.pixelSize: 12
                                        }
                                        MouseArea {
                                            id: del
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: Isos.remove(model.id)
                                        }
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
                        Text {
                            anchors.horizontalCenter: parent.horizontalCenter
                            text: "No ISOs yet"
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: 15
                            font.weight: Font.DemiBold
                        }
                        Text {
                            anchors.horizontalCenter: parent.horizontalCenter
                            text: "Add a Rocky 9, Ubuntu 24, RHEL 9, or ESXi installer to get started"
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: 13
                        }
                    }
                }
            }
        }
    }

    // ---- Add ISO form ----
    Popup {
        id: form
        property url fileUrl: ""
        property string fileName: ""
        property string osFamily: "rocky"

        width: 460
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
                text: "Add ISO"
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

                Text {
                    text: form.fileName
                    color: Theme.textMuted
                    font.family: Theme.monoFamily
                    font.pixelSize: 12
                    elide: Text.ElideMiddle
                    Layout.fillWidth: true
                }

                // OS family segmented
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
                            model: ["rocky", "rhel", "ubuntu", "esxi", "windows"]
                            delegate: Rectangle {
                                required property string modelData
                                width: (parent.width - 18) / 5
                                height: parent.height
                                radius: 7
                                color: form.osFamily === modelData ? Theme.accent : "transparent"
                                Behavior on color { ColorAnimation { duration: 150 } }
                                Text {
                                    anchors.centerIn: parent
                                    text: modelData.toUpperCase()
                                    color: form.osFamily === modelData ? Theme.accentText : Theme.textMuted
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 11
                                    font.weight: Font.Medium
                                }
                                TapHandler { onTapped: form.osFamily = modelData }
                            }
                        }
                    }
                }

            }

            RowLayout {
                Layout.fillWidth: true
                Layout.margins: 20
                Layout.topMargin: 4
                Item { Layout.fillWidth: true }
                PrimaryButton { label: "Cancel"; primary: false; onClicked: form.close() }
                PrimaryButton {
                    id: isoAddBtn
                    label: "Add"
                    enabled: true
                    onClicked: {
                        Isos.add(form.fileUrl, form.osFamily, "", "", "", "")
                        form.close()
                    }
                }
            }
        }

        Shortcut {
            sequence: "Return"
            enabled: form.opened
            onActivated: if (isoAddBtn.enabled) isoAddBtn.clicked()
        }
        Shortcut {
            sequence: "Enter"
            enabled: form.opened
            onActivated: if (isoAddBtn.enabled) isoAddBtn.clicked()
        }
    }
}
