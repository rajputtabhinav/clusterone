import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: rail

    property int currentIndex: 0
    signal navigate(int index)

    // Round the LEFT corners to match the window's outer radius (bound from Main).
    // Right side is squared by the overlay below.
    property int leftRadius: 0

    readonly property var items: [
        { label: "Dashboard",        kind: "dashboard" },
        { label: "Inventory",        kind: "inventory" },
        { label: "Updates",          kind: "updates" },
        { label: "Provision",        kind: "provision" },
        { label: "Firmware Library", kind: "firmware" },
        { label: "ISO Library",      kind: "iso" },
        { label: "Activity",         kind: "activity" },
        { label: "Settings",         kind: "settings" }
    ]
    readonly property string currentTitle: items[currentIndex].label

    // Background — all four corners rounded; the right two are hidden by the overlay.
    Rectangle {
        anchors.fill: parent
        radius: rail.leftRadius
        color: Theme.sidebar
        antialiasing: true
        Behavior on color { ColorAnimation { duration: 200 } }
    }
    // Overlay covers the right side so only the LEFT corners look rounded.
    Rectangle {
        anchors {
            top: parent.top; bottom: parent.bottom; right: parent.right
            left: parent.left; leftMargin: rail.leftRadius
        }
        color: Theme.sidebar
        Behavior on color { ColorAnimation { duration: 200 } }
    }

    // right hairline divider
    Rectangle {
        anchors.right: parent.right
        width: 1; height: parent.height
        color: Theme.border
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 16
        anchors.topMargin: 18
        spacing: 4

        // brand
        RowLayout {
            Layout.fillWidth: true
            Layout.bottomMargin: 18
            spacing: 12
            Rectangle {
                width: 32; height: 32; radius: 9
                color: Theme.elevated
                border.color: Theme.border
                border.width: 1
                Grid {
                    anchors.centerIn: parent
                    columns: 2
                    rowSpacing: 4
                    columnSpacing: 4
                    Repeater {
                        model: 4
                        delegate: Rectangle { width: 7; height: 7; radius: 2; color: Theme.accent }
                    }
                }
            }
            ColumnLayout {
                spacing: 0
                Label {
                    text: App.appName
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: 16
                    font.weight: Font.Bold
                }
                Label {
                    text: App.tagline
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: 10
                }
            }
        }

        Repeater {
            model: rail.items
            delegate: NavButton {
                required property int index
                required property var modelData
                Layout.fillWidth: true
                label: modelData.label
                iconKind: modelData.kind
                selected: index === rail.currentIndex
                onClicked: rail.navigate(index)
            }
        }

        Item { Layout.fillHeight: true }
    }
}
