import QtQuick

// Clean solid status dot (no glow).
Rectangle {
    property color dotColor: Theme.online
    property real coreSize: 8
    implicitWidth: coreSize
    implicitHeight: coreSize
    radius: coreSize / 2
    color: dotColor
}
