import QtQuick
import QtQuick.Shapes

// Monochrome, theme-coloured nav icon. Set `kind` to one of:
//   "dashboard" | "inventory" | "updates" | "provision" | "firmware"
//   | "iso" | "activity" | "settings"
// Drawn from primitives (no font / image deps); inherits the theme accent.
Item {
    id: ico
    property string kind: ""
    property color color: Theme.textMuted
    property real size: 16

    implicitWidth: size
    implicitHeight: size

    // ---- Dashboard — 2x2 grid of small rounded squares ----
    Grid {
        visible: ico.kind === "dashboard"
        anchors.centerIn: parent
        columns: 2
        rowSpacing: 3
        columnSpacing: 3
        Repeater {
            model: 4
            delegate: Rectangle { width: 6; height: 6; radius: 1.5; color: ico.color }
        }
    }

    // ---- Inventory — 3 stacked rack pills with a status pin ----
    Column {
        visible: ico.kind === "inventory"
        anchors.centerIn: parent
        spacing: 2
        Repeater {
            model: 3
            delegate: Rectangle {
                width: 14; height: 3; radius: 1
                color: ico.color
                Rectangle {
                    width: 2; height: 2; radius: 1
                    color: ico.color
                    anchors.right: parent.right
                    anchors.rightMargin: 1
                    anchors.verticalCenter: parent.verticalCenter
                    opacity: 0.55
                }
            }
        }
    }

    // ---- Updates — upward chevron + shaft (push) ----
    Shape {
        visible: ico.kind === "updates"
        anchors.fill: parent
        antialiasing: true
        layer.enabled: true
        layer.samples: 4
        ShapePath {
            strokeColor: ico.color
            fillColor: "transparent"
            strokeWidth: 1.8
            capStyle: ShapePath.RoundCap
            joinStyle: ShapePath.RoundJoin
            startX: 3; startY: 8
            PathLine { x: 8; y: 3 }
            PathLine { x: 13; y: 8 }
        }
        ShapePath {
            strokeColor: ico.color
            fillColor: "transparent"
            strokeWidth: 1.8
            capStyle: ShapePath.RoundCap
            startX: 8; startY: 4
            PathLine { x: 8; y: 14 }
        }
    }

    // ---- Firmware Library — 3 stacked discs (database) ----
    Column {
        visible: ico.kind === "firmware"
        anchors.centerIn: parent
        spacing: 2
        Repeater {
            model: 3
            delegate: Rectangle {
                width: 14; height: 3; radius: 1.5
                color: "transparent"
                border.color: ico.color
                border.width: 1.2
            }
        }
    }

    // ---- Activity — clock face (ring + hand + pivot) ----
    Rectangle {
        visible: ico.kind === "activity"
        anchors.centerIn: parent
        width: 13; height: 13; radius: 6.5
        color: "transparent"
        border.color: ico.color
        border.width: 1.5

        // Minute hand (pointing up)
        Rectangle {
            width: 1.4; height: 5; radius: 0.7
            color: ico.color
            x: parent.width / 2 - width / 2
            y: parent.height / 2 - height + 1
        }
        // Pivot
        Rectangle {
            anchors.centerIn: parent
            width: 1.6; height: 1.6; radius: 0.8
            color: ico.color
        }
    }

    // ---- Provision — server outline + downward arrow into it ----
    Item {
        visible: ico.kind === "provision"
        anchors.centerIn: parent
        width: 16; height: 16
        // Server chassis outline
        Rectangle {
            width: 14; height: 7
            x: 1; y: 7
            radius: 1.5
            color: "transparent"
            border.color: ico.color
            border.width: 1.3
        }
        // Drive bay slits
        Rectangle { width: 1.6; height: 1.6; radius: 0.5; color: ico.color; x: 3.5; y: 10 }
        Rectangle { width: 1.6; height: 1.6; radius: 0.5; color: ico.color; x: 7;   y: 10 }
        Rectangle { width: 1.6; height: 1.6; radius: 0.5; color: ico.color; x: 10.5;y: 10 }
        // Arrow head pointing down into the server
        Shape {
            anchors.fill: parent
            antialiasing: true
            ShapePath {
                strokeColor: ico.color
                fillColor: "transparent"
                strokeWidth: 1.6
                capStyle: ShapePath.RoundCap
                joinStyle: ShapePath.RoundJoin
                startX: 5; startY: 4
                PathLine { x: 8; y: 7 }
                PathLine { x: 11; y: 4 }
            }
            ShapePath {
                strokeColor: ico.color
                fillColor: "transparent"
                strokeWidth: 1.6
                capStyle: ShapePath.RoundCap
                startX: 8; startY: 1
                PathLine { x: 8; y: 7 }
            }
        }
    }

    // ---- ISO Library — disc with concentric rings ----
    Item {
        visible: ico.kind === "iso"
        anchors.centerIn: parent
        width: 16; height: 16
        Rectangle {
            anchors.centerIn: parent
            width: 14; height: 14; radius: 7
            color: "transparent"
            border.color: ico.color
            border.width: 1.4
        }
        Rectangle {
            anchors.centerIn: parent
            width: 8; height: 8; radius: 4
            color: "transparent"
            border.color: ico.color
            border.width: 1
            opacity: 0.7
        }
        Rectangle {
            anchors.centerIn: parent
            width: 2.4; height: 2.4; radius: 1.2
            color: ico.color
        }
    }

    // ---- Settings — three sliders with knobs ----
    Column {
        visible: ico.kind === "settings"
        anchors.centerIn: parent
        spacing: 3
        Repeater {
            model: [9, 4, 6]   // knob x position per row
            delegate: Item {
                required property var modelData
                width: 14; height: 4
                Rectangle {
                    anchors.verticalCenter: parent.verticalCenter
                    width: parent.width
                    height: 1.4
                    radius: 0.7
                    color: ico.color
                }
                Rectangle {
                    width: 4; height: 4; radius: 2
                    color: ico.color
                    anchors.verticalCenter: parent.verticalCenter
                    x: modelData
                }
            }
        }
    }
}
