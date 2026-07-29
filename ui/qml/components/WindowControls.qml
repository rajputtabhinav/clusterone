import QtQuick

Row {
    id: wc
    spacing: 2
    signal minimize()
    signal maximize()
    signal close()

    component CtrlButton: Item {
        property string glyph: ""
        property color hoverColor: Theme.hover
        property color glyphColor: Theme.textMuted
        signal activated()

        width: 38; height: 30

        Rectangle {
            anchors.fill: parent
            radius: 8
            color: ma.containsMouse ? hoverColor : "transparent"
            Behavior on color { ColorAnimation { duration: 120 } }
        }
        Text {
            anchors.centerIn: parent
            text: glyph
            color: ma.containsMouse && hoverColor === Theme.error ? "#FFFFFF" : glyphColor
            font.pixelSize: 13
        }
        MouseArea {
            id: ma
            anchors.fill: parent
            hoverEnabled: true
            onClicked: activated()
        }
    }

    CtrlButton { glyph: "–"; onActivated: wc.minimize() }
    CtrlButton { glyph: "□"; onActivated: wc.maximize() }
    CtrlButton { glyph: "×"; glyphColor: Theme.text; hoverColor: Theme.error; onActivated: wc.close() }
}
