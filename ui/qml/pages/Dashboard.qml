import QtQuick
import QtQuick.Layouts
import "../components"

Item {
    id: page

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 24
        spacing: 16

        // Header + actions
        RowLayout {
            Layout.fillWidth: true
            Text {
                text: "FLEET OVERVIEW"
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: 11
                font.weight: Font.Medium
                font.letterSpacing: 0.6
            }
            Item { Layout.fillWidth: true }
            PrimaryButton {
                label: "Export Inventory Report"
                primary: false
                onClicked: Reports.exportInventory()
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 16
            StatTile { Layout.fillWidth: true; label: "Total Servers"; value: Fleet.total }
            StatTile { Layout.fillWidth: true; label: "Online"; value: Fleet.online; valueColor: Theme.online }
            StatTile { Layout.fillWidth: true; label: "Needs Update"; value: Fleet.needsUpdate; valueColor: Theme.warning }
            StatTile { Layout.fillWidth: true; label: "Failed"; value: Fleet.failed; valueColor: Theme.error }
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
                    implicitHeight: 56
                    Text {
                        anchors.left: parent.left; anchors.leftMargin: 20; anchors.verticalCenter: parent.verticalCenter
                        text: "Recent Activity"
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: 15
                        font.weight: Font.DemiBold
                    }
                    Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.border }
                }
                ListView {
                    id: activityList
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    model: EventLog.model
                    boundsBehavior: Flickable.StopAtBounds
                    delegate: RowLayout {
                        width: ListView.view.width
                        height: 36
                        Text {
                            text: model.time
                            leftPadding: 20
                            Layout.preferredWidth: 64
                            color: Theme.textMuted
                            font.family: Theme.monoFamily
                            font.pixelSize: 12
                        }
                        StatusDot {
                            Layout.alignment: Qt.AlignVCenter
                            coreSize: 6
                            dotColor: model.kind === "success" ? Theme.success
                                      : model.kind === "error" ? Theme.error
                                      : model.kind === "warning" ? Theme.warning : Theme.accent
                        }
                        Text {
                            text: model.message
                            Layout.fillWidth: true
                            rightPadding: 20
                            elide: Text.ElideRight
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: 13
                        }
                    }

                    // Empty state when no activity yet
                    Column {
                        anchors.centerIn: parent
                        spacing: 6
                        visible: activityList.count === 0
                        Text {
                            anchors.horizontalCenter: parent.horizontalCenter
                            text: "No activity yet"
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: 14
                            font.weight: Font.DemiBold
                        }
                        Text {
                            anchors.horizontalCenter: parent.horizontalCenter
                            text: "Run discovery or import firmware to populate the timeline"
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: 12
                        }
                    }
                }
            }
        }
    }
}
