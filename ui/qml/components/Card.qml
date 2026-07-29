import QtQuick

Rectangle {
    id: card
    radius: 14
    color: Theme.card
    border.color: Theme.border
    border.width: 1
    antialiasing: true
    implicitHeight: 120
    Behavior on color { ColorAnimation { duration: 200 } }

    // Subtle material edge highlight along the top (depth, dark theme only).
    Rectangle {
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.topMargin: 1
        anchors.leftMargin: 14
        anchors.rightMargin: 14
        height: 1
        color: Theme.topHighlight
        visible: Theme.isDark
    }

    default property alias content: inner.data
    property alias padding: inner.anchors.margins

    Item {
        id: inner
        anchors.fill: parent
        anchors.margins: 20
    }
}
