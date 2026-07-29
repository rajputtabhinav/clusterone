import QtQuick
import QtQuick.Controls

TextField {
    id: tf
    placeholderText: "Search…"
    color: Theme.text
    placeholderTextColor: Theme.textMuted
    font.family: Theme.fontFamily
    font.pixelSize: 13
    leftPadding: 14
    rightPadding: 14
    topPadding: 8
    bottomPadding: 8
    selectByMouse: true

    background: Rectangle {
        radius: 10
        color: Theme.hover
        border.color: tf.activeFocus ? Theme.accent : Theme.border
        border.width: 1
        Behavior on border.color { ColorAnimation { duration: 150 } }
        Behavior on color { ColorAnimation { duration: 200 } }
    }
}
