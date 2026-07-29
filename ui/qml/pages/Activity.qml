import QtQuick
import QtQuick.Layouts
import "../components"

Item {
    id: page

    function kindColor(k) {
        if (k === "success") return Theme.success
        if (k === "error") return Theme.error
        if (k === "warning") return Theme.warning
        return Theme.accent
    }

    Card {
        anchors.fill: parent
        anchors.margins: 24
        padding: 0

        ColumnLayout {
            anchors.fill: parent
            spacing: 0

            Item {
                Layout.fillWidth: true
                implicitHeight: 52
                Text {
                    anchors.left: parent.left; anchors.leftMargin: 20; anchors.verticalCenter: parent.verticalCenter
                    text: "Activity Timeline"
                    color: Theme.text; font.family: Theme.fontFamily; font.pixelSize: 15; font.weight: Font.DemiBold
                }
                Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.border }
            }

            ListView {
                id: list
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                model: EventLog.model
                boundsBehavior: Flickable.StopAtBounds
                delegate: Item {
                    width: ListView.view.width
                    implicitHeight: 54
                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 20
                        anchors.rightMargin: 20
                        spacing: 16
                        Text {
                            text: model.time
                            Layout.preferredWidth: 48
                            color: Theme.textMuted; font.family: Theme.monoFamily; font.pixelSize: 12
                        }
                        StatusDot {
                            Layout.alignment: Qt.AlignVCenter
                            dotColor: page.kindColor(model.kind)
                            coreSize: 7
                        }
                        Text {
                            text: model.message
                            Layout.fillWidth: true
                            elide: Text.ElideRight
                            color: Theme.text; font.family: Theme.fontFamily; font.pixelSize: 13
                        }
                    }
                }
            }
        }
    }
}
