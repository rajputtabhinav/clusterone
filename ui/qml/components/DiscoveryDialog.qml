import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "." as L

Popup {
    id: dlg

    width: 500
    x: parent ? Math.round((parent.width - width) / 2) : 0
    y: parent ? Math.round((parent.height - height) / 3) : 0
    modal: true
    dim: true
    padding: 0
    closePolicy: Discovery.isScanning
                 ? Popup.NoAutoClose
                 : (Popup.CloseOnEscape | Popup.CloseOnPressOutside)

    // Pre-populate the range field with the operator's last scan target
    // so re-discovery is a single click. Persisted across restarts.
    onOpened: {
        if (rangeField.text.length === 0 && Discovery.lastRange.length > 0) {
            rangeField.text = Discovery.lastRange
        }
    }

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
            text: "Discover Servers"
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: 16
            font.weight: Font.DemiBold
        }
        Text {
            Layout.leftMargin: 20
            Layout.rightMargin: 20
            text: "Enter an IP range, CIDR, or simulator port range."
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: 12
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.leftMargin: 20
            Layout.rightMargin: 20
            spacing: 10

            L.SearchField {
                id: rangeField
                Layout.fillWidth: true
                placeholderText: "10.10.10.0/24  ·  127.0.0.1:8000-8007  ·  10.10.10.1-254"
                enabled: !Discovery.isScanning
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: 10
                L.SearchField {
                    id: userField
                    Layout.fillWidth: true
                    placeholderText: "Username (blank for simulator)"
                    enabled: !Discovery.isScanning
                }
                L.SearchField {
                    id: passField
                    Layout.fillWidth: true
                    placeholderText: "Password"
                    echoMode: TextInput.Password
                    enabled: !Discovery.isScanning
                }
            }
        }

        // ---- live progress ----
        ColumnLayout {
            Layout.fillWidth: true
            Layout.leftMargin: 20
            Layout.rightMargin: 20
            spacing: 6
            visible: Discovery.isScanning || Discovery.total > 0
            Rectangle {
                Layout.fillWidth: true
                height: 8; radius: 4
                color: Theme.hover
                Rectangle {
                    height: parent.height; radius: 4
                    color: Discovery.error.length > 0 ? Theme.error : Theme.accent
                    width: Discovery.total > 0 ? parent.width * Discovery.scanned / Discovery.total : 0
                    Behavior on width { NumberAnimation { duration: 150 } }
                }
            }
            Text {
                text: Discovery.scanned + " / " + Discovery.total + "  scanned   ·   "
                      + Discovery.found + " found"
                color: Theme.textMuted
                font.family: Theme.monoFamily
                font.pixelSize: 11
            }
        }

        Text {
            visible: Discovery.error.length > 0
            Layout.leftMargin: 20
            Layout.rightMargin: 20
            Layout.fillWidth: true
            text: Discovery.error
            color: Theme.error
            font.family: Theme.fontFamily
            font.pixelSize: 12
            wrapMode: Text.WordWrap
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.margins: 20
            Layout.topMargin: 4
            Item { Layout.fillWidth: true }
            PrimaryButton {
                label: "Close"
                primary: false
                enabled: !Discovery.isScanning
                onClicked: dlg.close()
            }
            PrimaryButton {
                label: "Cancel"
                primary: false
                visible: Discovery.isScanning
                onClicked: Discovery.cancel()
            }
            PrimaryButton {
                id: startBtn
                label: Discovery.isScanning ? "Scanning…" : "Start"
                enabled: !Discovery.isScanning && rangeField.text.length > 0
                onClicked: Discovery.start(rangeField.text, userField.text, passField.text)
            }
        }
    }

    Shortcut {
        sequence: "Return"
        enabled: dlg.opened
        onActivated: if (startBtn.enabled) startBtn.clicked()
    }
    Shortcut {
        sequence: "Enter"
        enabled: dlg.opened
        onActivated: if (startBtn.enabled) startBtn.clicked()
    }
}
