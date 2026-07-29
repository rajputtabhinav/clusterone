import QtQuick
import QtQuick.Controls

Item {
    id: btn
    implicitHeight: 40

    property string label: ""
    property string iconKind: ""
    property bool selected: false
    signal clicked()

    Rectangle {
        anchors.fill: parent
        radius: 10
        color: btn.selected ? Theme.selected : (hover.hovered ? Theme.hover : "transparent")
        Behavior on color { ColorAnimation { duration: 150 } }
    }

    // Left accent indicator when selected
    Rectangle {
        anchors.verticalCenter: parent.verticalCenter
        anchors.left: parent.left
        anchors.leftMargin: 3
        width: 3
        height: btn.selected ? 18 : 0
        radius: 2
        color: Theme.accent
        Behavior on height { NumberAnimation { duration: 150; easing.type: Easing.OutCubic } }
    }

    NavIcon {
        id: navIcon
        anchors.verticalCenter: parent.verticalCenter
        anchors.left: parent.left
        anchors.leftMargin: 18
        kind: btn.iconKind
        color: btn.selected ? Theme.text : Theme.textMuted
        size: 16
    }

    Label {
        anchors.verticalCenter: parent.verticalCenter
        anchors.left: navIcon.right
        anchors.leftMargin: 12
        text: btn.label
        color: btn.selected ? Theme.text : Theme.textMuted
        font.family: Theme.fontFamily
        font.pixelSize: 14
        font.weight: btn.selected ? Font.DemiBold : Font.Normal
    }

    HoverHandler { id: hover }
    TapHandler {
        // Move focus to the nav button so any active TextField on the
        // outgoing page commits + clears its cursor before navigation.
        onPressedChanged: if (pressed) btn.forceActiveFocus()
        onTapped: btn.clicked()
    }
}
