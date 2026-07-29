import QtQuick

Item {
    id: btn
    property string label: ""
    property bool primary: true
    signal clicked()

    implicitHeight: 34
    implicitWidth: lbl.implicitWidth + 34

    Rectangle {
        anchors.fill: parent
        radius: 10
        color: btn.primary
               ? (ma.containsMouse ? Qt.darker(Theme.accent, 1.12) : Theme.accent)
               : (ma.containsMouse ? Theme.hover : "transparent")
        border.color: btn.primary ? "transparent" : Theme.border
        border.width: btn.primary ? 0 : 1
        Behavior on color { ColorAnimation { duration: 120 } }
    }
    Text {
        id: lbl
        anchors.centerIn: parent
        text: btn.label
        color: btn.primary ? Theme.accentText : Theme.text
        font.family: Theme.fontFamily
        font.pixelSize: 13
        font.weight: Font.DemiBold
    }
    MouseArea {
        id: ma
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        // Grab focus on press so any active TextField loses focus,
        // fires its `onEditingFinished`, and clears its blinking cursor.
        // Without this, clicking Save while a TextField is being edited
        // leaves the cursor visible and (worse) the field's value
        // uncommitted, so `enabled` bindings tied to dirty-state lag.
        onPressed: btn.forceActiveFocus()
        onClicked: btn.clicked()
    }
}
