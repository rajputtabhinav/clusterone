import QtQuick
import QtQuick.Layouts

// Console-style status: glow dot + uppercase, letter-spaced label (no chrome).
RowLayout {
    id: pill
    property string label: ""
    property color pillColor: Theme.textMuted
    spacing: 8

    StatusDot {
        Layout.alignment: Qt.AlignVCenter
        dotColor: pill.pillColor
        coreSize: 7
    }
    Text {
        Layout.alignment: Qt.AlignVCenter
        text: pill.label.toUpperCase()
        color: pill.pillColor
        font.family: Theme.fontFamily
        font.pixelSize: 11
        font.weight: Font.DemiBold
        font.letterSpacing: 0.5
    }
}
