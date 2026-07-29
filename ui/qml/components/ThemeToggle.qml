import QtQuick

// Binary light/dark switch (the 3-way Light/Dark/System control lives in Settings).
Item {
    id: tt
    implicitWidth: 46
    implicitHeight: 26

    Rectangle {
        id: track
        anchors.fill: parent
        radius: height / 2
        color: hover.hovered ? Theme.hover : Theme.elevated
        border.color: Theme.border
        border.width: 1
        Behavior on color { ColorAnimation { duration: 150 } }

        Rectangle {
            id: knob
            width: 20; height: 20; radius: 10
            y: 3
            x: Theme.isDark ? track.width - width - 3 : 3
            color: Theme.isDark ? Theme.elevated : "#FFFFFF"
            border.color: Theme.border
            border.width: 1
            Behavior on x { NumberAnimation { duration: 200; easing.type: Easing.OutCubic } }

            // sun (light)
            Rectangle {
                visible: !Theme.isDark
                anchors.centerIn: parent
                width: 9; height: 9; radius: 4.5
                color: Theme.accent
            }
            // moon (dark) — amber circle with a carved crescent
            Item {
                visible: Theme.isDark
                anchors.centerIn: parent
                width: 11; height: 11
                Rectangle { anchors.fill: parent; radius: width / 2; color: Theme.accent }
                Rectangle { width: 9; height: 9; radius: 4.5; x: 4; y: -1; color: knob.color }
            }
        }
    }

    HoverHandler { id: hover }
    TapHandler { onTapped: Theme.setMode(Theme.isDark ? "light" : "dark") }
}
