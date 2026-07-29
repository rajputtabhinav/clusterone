import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "." as L

Popup {
    id: dlg

    width: 460
    x: parent ? Math.round((parent.width - width) / 2) : 0
    y: parent ? Math.round((parent.height - height) / 3) : 0
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

    onOpened: { labelField.text = ""; userField.text = ""; passField.text = ""; scopeField.text = "default"; labelField.forceActiveFocus() }

    contentItem: ColumnLayout {
        spacing: 14

        Text {
            Layout.margins: 20
            Layout.bottomMargin: 0
            text: "Add Credential"
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: 16
            font.weight: Font.DemiBold
        }
        Text {
            Layout.leftMargin: 20
            Layout.rightMargin: 20
            Layout.fillWidth: true
            text: Credentials.isVaultSecure
                  ? "Password is encrypted at rest with Windows DPAPI."
                  : "⚠ pywin32 not installed — password will be stored in a dev fallback. Install pywin32 for production."
            color: Credentials.isVaultSecure ? Theme.textMuted : Theme.warning
            font.family: Theme.fontFamily
            font.pixelSize: 11
            wrapMode: Text.WordWrap
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.leftMargin: 20
            Layout.rightMargin: 20
            spacing: 10

            L.SearchField { id: labelField; Layout.fillWidth: true; placeholderText: "Label (e.g. Lab BMC admin)" }
            L.SearchField { id: userField;  Layout.fillWidth: true; placeholderText: "Username (required)" }
            L.SearchField { id: passField;  Layout.fillWidth: true; placeholderText: "Password"; echoMode: TextInput.Password }
            L.SearchField {
                id: scopeField
                Layout.fillWidth: true
                placeholderText: "Scope: default · host:10.10.10.5 · oem:supermicro"
                text: "default"
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.margins: 20
            Layout.topMargin: 4
            Item { Layout.fillWidth: true }
            PrimaryButton { label: "Cancel"; primary: false; onClicked: dlg.close() }
            PrimaryButton {
                id: saveBtn
                label: "Save"
                enabled: userField.text.length > 0
                onClicked: {
                    Credentials.add(labelField.text, userField.text, passField.text, scopeField.text)
                    dlg.close()
                }
            }
        }
    }

    // Enter submits, Esc closes (Esc already handled by closePolicy).
    Shortcut {
        sequence: "Return"
        enabled: dlg.opened
        onActivated: if (saveBtn.enabled) saveBtn.clicked()
    }
    Shortcut {
        sequence: "Enter"
        enabled: dlg.opened
        onActivated: if (saveBtn.enabled) saveBtn.clicked()
    }
}
