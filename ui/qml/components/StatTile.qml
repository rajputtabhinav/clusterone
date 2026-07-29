import QtQuick

Card {
    id: tile
    implicitHeight: 116

    property string label: ""
    property string value: "0"
    property color valueColor: Theme.text

    Column {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        spacing: 8

        Text {
            text: tile.value
            color: tile.valueColor
            font.family: Theme.monoFamily
            font.pixelSize: 34
            font.weight: Font.Bold
        }
        Text {
            text: tile.label.toUpperCase()
            color: Theme.textMuted
            font.family: Theme.fontFamily
            font.pixelSize: 11
            font.weight: Font.Medium
            font.letterSpacing: 0.6
        }
    }
}
