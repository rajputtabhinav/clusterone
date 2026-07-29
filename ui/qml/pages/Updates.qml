import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: page

    // Persisted across navigation so the operator's last choices survive.
    property int selectedFirmwareId: AppSettings.getInt("updates_last_firmware_id", -1)
    // Apply timing: false = stage + apply on next reboot (default, safe);
    // true = reboot the host now so the new BIOS takes effect immediately.
    property bool applyNow: AppSettings.getBool("updates_last_apply_now", false)

    // Self-contained server selection (no Inventory dependency).
    // All discovered servers are selected by default; a server is targeted
    // unless its id is in `deselected`.  Credentials come from the vault
    // (entered once in Settings → Credentials), never re-typed per flash.
    property var deselected: ({})

    function targetIds() {
        var all = Fleet.allServerIds
        var out = []
        for (var i = 0; i < all.length; i++)
            if (!page.deselected[String(all[i])]) out.push(all[i])
        return out
    }
    function toggleServer(sid) {
        // Shallow clone — values are bool primitives, no nested objects.
        var c = Object.assign({}, page.deselected)
        if (c[String(sid)]) delete c[String(sid)]; else c[String(sid)] = true
        page.deselected = c
    }
    // Inline expression (NOT a function call) so QML's binding engine sees
    // the direct reads of Fleet.allServerIds + page.deselected and tracks
    // them as dependencies. With targetIds() the binding ran exactly once
    // at page load — Fleet.allServerIds had not yet loaded → targetCount
    // stayed at 0 → the Flash button was permanently disabled.
    readonly property int targetCount: {
        var ids = Fleet.allServerIds
        var ds = page.deselected
        var n = 0
        for (var i = 0; i < ids.length; i++)
            if (!ds[String(ids[i])]) n++
        return n
    }

    onSelectedFirmwareIdChanged: {
        AppSettings.setInt("updates_last_firmware_id", selectedFirmwareId)
        syncFwLabel()
    }
    onApplyNowChanged: AppSettings.setBool("updates_last_apply_now", applyNow)

    // Does the currently-selected firmware apply on reboot? (BIOS yes, BMC no)
    readonly property bool selectedIsBios:
        fwPickerPopup.selectedType === "BIOS" || fwPickerPopup.selectedType === ""

    // Sync the dropdown trigger label from the firmware controller.
    // Called on id change and when the firmware model reloads.
    function syncFwLabel() {
        if (page.selectedFirmwareId > 0) {
            try {
                var fw = JSON.parse(Firmware.getById(page.selectedFirmwareId))
                if (fw && fw.name) {
                    fwPickerPopup.selectedType    = fw.type    || ""
                    fwPickerPopup.selectedLabel   = fw.name    || ""
                    fwPickerPopup.selectedVersion = fw.version || ""
                    return
                }
            } catch(e) {}
        }
        // Nothing selected — try to auto-select the first firmware
        if (Firmware.count > 0 && page.selectedFirmwareId <= 0) {
            // Trigger by reading row 0 from the model
            var idx = Firmware.model.index(0, 0)
            var roles = Firmware.model.roleNames()
        }
    }

    // Sync label when the page first loads and when firmware library changes
    Component.onCompleted: Qt.callLater(syncFwLabel)
    Connections {
        target: Firmware
        function onChanged() { syncFwLabel() }
    }

    function stateColor(s) {
        if (s === "completed") return Theme.success
        if (s === "failed") return Theme.error
        if (s === "queued") return Theme.textMuted
        return Theme.accent
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 24
        spacing: 16

        // ---- Setup card ----
        Card {
            Layout.fillWidth: true
            implicitHeight: setupCol.implicitHeight + 40

            ColumnLayout {
                id: setupCol
                anchors { left: parent.left; right: parent.right; top: parent.top }
                spacing: 16

                Text {
                    text: "New Update Job"
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: 15
                    font.weight: Font.DemiBold
                }

                // Servers row
                // Servers — inline picker (all selected by default; tap to toggle)
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 12
                    Text {
                        text: "SERVERS"
                        Layout.preferredWidth: 90
                        Layout.alignment: Qt.AlignTop
                        topPadding: 7
                        color: Theme.textMuted
                        font.family: Theme.fontFamily; font.pixelSize: 11
                        font.weight: Font.Medium; font.letterSpacing: 0.6
                    }
                    Flow {
                        Layout.fillWidth: true
                        spacing: 8
                        Repeater {
                            model: Fleet.model
                            delegate: Rectangle {
                                // Cache row identity at delegate root so children
                                // and the TapHandler don't dereference `model.id`
                                // mid-iteration — if the Fleet model resets while
                                // the delegate is still rendering (concurrent
                                // discovery refresh), `model.id` becomes undefined
                                // and the TapHandler crashes with TypeError.
                                readonly property int sid: model.id !== undefined ? model.id : -1
                                readonly property string shost: model.hostname || ""
                                readonly property string sip: model.ip || ""
                                property bool sel: sid > 0 && !page.deselected[String(sid)]
                                implicitHeight: 32
                                implicitWidth: srvRow.implicitWidth + 22
                                radius: 9
                                color: sel ? Theme.accent : Theme.hover
                                border.color: sel ? Theme.accent : Theme.border
                                border.width: 1
                                Behavior on color { ColorAnimation { duration: 120 } }
                                RowLayout {
                                    id: srvRow
                                    anchors.centerIn: parent
                                    spacing: 7
                                    Text {
                                        text: parent.parent.sel ? "✓" : "○"
                                        color: parent.parent.sel ? Theme.accentText : Theme.textMuted
                                        font.family: Theme.monoFamily; font.pixelSize: 12; font.weight: Font.Bold
                                    }
                                    Text {
                                        text: (parent.parent.shost || parent.parent.sip) + "  " + parent.parent.sip
                                        color: parent.parent.sel ? Theme.accentText : Theme.text
                                        font.family: Theme.monoFamily; font.pixelSize: 12
                                    }
                                }
                                TapHandler { onTapped: if (parent.sid > 0) page.toggleServer(parent.sid) }
                            }
                        }
                        Text {
                            visible: Fleet.total === 0
                            text: "No servers — run Discovery in Inventory first"
                            color: Theme.warning; font.family: Theme.fontFamily; font.pixelSize: 12
                        }
                    }
                }

                // Firmware row — compact dropdown so long filenames don't wrap
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 12
                    Text {
                        text: "FIRMWARE"
                        Layout.preferredWidth: 90
                        Layout.alignment: Qt.AlignVCenter
                        color: Theme.textMuted
                        font.family: Theme.fontFamily; font.pixelSize: 11
                        font.weight: Font.Medium; font.letterSpacing: 0.6
                    }

                    // Dropdown trigger
                    Rectangle {
                        id: fwDropTrigger
                        Layout.fillWidth: true
                        implicitHeight: 38
                        radius: 10
                        color: fwDropMa.containsMouse ? Theme.hover : Theme.elevated
                        border.color: fwPickerPopup.visible ? Theme.accent : Theme.border
                        border.width: 1
                        Behavior on border.color { ColorAnimation { duration: 120 } }

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 14
                            anchors.rightMargin: 14
                            spacing: 10

                            // When something is selected: show its type badge
                            Rectangle {
                                visible: page.selectedFirmwareId > 0 && fwPickerPopup.selectedType !== ""
                                implicitWidth: fwSelTypeText.implicitWidth + 14
                                implicitHeight: 20
                                radius: 5
                                color: fwPickerPopup.selectedType === "BMC"  ? Theme.warning
                                     : fwPickerPopup.selectedType === "BIOS" ? Theme.success
                                     : Theme.accent
                                Text {
                                    id: fwSelTypeText
                                    anchors.centerIn: parent
                                    text: fwPickerPopup.selectedType
                                    color: Theme.accentText
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 10
                                    font.weight: Font.Bold
                                }
                            }

                            // When nothing is selected: show mini type badges for available firmware
                            Row {
                                visible: page.selectedFirmwareId <= 0 && Firmware.count > 0
                                spacing: 5
                                Repeater {
                                    model: Firmware.model
                                    delegate: Rectangle {
                                        implicitWidth: previewLabel.implicitWidth + 12
                                        implicitHeight: 18
                                        radius: 4
                                        opacity: 0.7
                                        color: model.type === "BMC"  ? Theme.warning
                                             : model.type === "BIOS" ? Theme.success
                                             : Theme.accent
                                        Text {
                                            id: previewLabel
                                            anchors.centerIn: parent
                                            text: model.type || "?"
                                            color: Theme.accentText
                                            font.family: Theme.fontFamily
                                            font.pixelSize: 9
                                            font.weight: Font.Bold
                                        }
                                    }
                                }
                            }

                            Text {
                                Layout.fillWidth: true
                                text: page.selectedFirmwareId > 0
                                      ? fwPickerPopup.selectedLabel
                                      : Firmware.count > 0
                                        ? "Click to select firmware…"
                                        : "No firmware — add one in Firmware Library"
                                color: page.selectedFirmwareId > 0 ? Theme.text : Theme.textMuted
                                font.family: Theme.monoFamily
                                font.pixelSize: 12
                                elide: Text.ElideMiddle
                            }
                            Text {
                                text: "▾"
                                color: Theme.textMuted
                                font.family: Theme.monoFamily
                                font.pixelSize: 12
                                rotation: fwPickerPopup.visible ? 180 : 0
                                Behavior on rotation { NumberAnimation { duration: 150 } }
                            }
                        }
                        MouseArea {
                            id: fwDropMa
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: fwPickerPopup.visible ? fwPickerPopup.close() : fwPickerPopup.open()
                        }
                    }

                    // Firmware picker popup (dropdown)
                    Popup {
                        id: fwPickerPopup
                        parent: fwDropTrigger
                        y: fwDropTrigger.height + 4
                        x: 0
                        width: fwDropTrigger.width
                        padding: 6
                        modal: false
                        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

                        property string selectedLabel: ""
                        property string selectedType:  ""
                        property string selectedVersion: ""

                        background: Rectangle {
                            color: Theme.card
                            radius: 10
                            border.color: Theme.border
                            border.width: 1
                        }

                        contentItem: Column {
                            spacing: 2

                            Repeater {
                                model: Firmware.model
                                delegate: Rectangle {
                                    width: fwPickerPopup.width - 12
                                    implicitHeight: 44
                                    radius: 8
                                    color: model.id === page.selectedFirmwareId
                                           ? Theme.selected
                                           : (itemHover.containsMouse ? Theme.hover : "transparent")
                                    Behavior on color { ColorAnimation { duration: 80 } }

                                    RowLayout {
                                        anchors.fill: parent
                                        anchors.leftMargin: 10
                                        anchors.rightMargin: 10
                                        spacing: 10

                                        // Type badge
                                        Rectangle {
                                            implicitWidth: typeLabel.implicitWidth + 12
                                            implicitHeight: 18
                                            radius: 4
                                            color: model.type === "BMC"  ? Theme.warning
                                                 : model.type === "BIOS" ? Theme.success
                                                 : Theme.accent
                                            Text {
                                                id: typeLabel
                                                anchors.centerIn: parent
                                                text: model.type || "?"
                                                color: Theme.accentText
                                                font.family: Theme.fontFamily
                                                font.pixelSize: 10
                                                font.weight: Font.Bold
                                            }
                                        }

                                        // File name + OEM
                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 0
                                            Text {
                                                text: model.name || ""
                                                color: Theme.text
                                                font.family: Theme.monoFamily
                                                font.pixelSize: 12
                                                elide: Text.ElideMiddle
                                                Layout.fillWidth: true
                                            }
                                            Text {
                                                text: (model.oem || "") + "  ·  v" + (model.version || "")
                                                color: Theme.textMuted
                                                font.family: Theme.fontFamily
                                                font.pixelSize: 11
                                            }
                                        }

                                        // Checkmark when selected
                                        Text {
                                            text: "✓"
                                            color: Theme.accent
                                            font.family: Theme.monoFamily
                                            font.pixelSize: 13
                                            font.weight: Font.Bold
                                            visible: model.id === page.selectedFirmwareId
                                        }
                                    }

                                    MouseArea {
                                        id: itemHover
                                        anchors.fill: parent
                                        hoverEnabled: true
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: {
                                            page.selectedFirmwareId = model.id
                                            fwPickerPopup.selectedLabel = model.name || ""
                                            fwPickerPopup.selectedType  = model.type  || ""
                                            fwPickerPopup.selectedVersion = model.version || ""
                                            fwPickerPopup.close()
                                        }
                                    }
                                }
                            }

                            // Empty state
                            Item {
                                width: parent.width
                                implicitHeight: 36
                                visible: Firmware.count === 0
                                Text {
                                    anchors.centerIn: parent
                                    text: "No firmware in library — add one first"
                                    color: Theme.textMuted
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 12
                                }
                            }
                        }
                    }
                }

                // Sync selected label when page loads with a persisted firmware id
                // Resync the firmware picker label on Firmware.changed —
                // the dropdown's selectedLabel is owned by the popup which
                // re-binds when the model count changes. The earlier
                // version had an empty for-loop here that did nothing;
                // delete it (label resync happens via syncFwLabel() at
                // the page root, line 82-83).

                // BMC credentials — resolved from the vault automatically.
                // Saved once (Settings → Credentials); never re-typed here.
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 12
                    Text {
                        text: "BMC LOGIN"
                        Layout.preferredWidth: 90
                        Layout.alignment: Qt.AlignVCenter
                        color: Theme.textMuted
                        font.family: Theme.fontFamily; font.pixelSize: 11
                        font.weight: Font.Medium; font.letterSpacing: 0.6
                    }
                    // Saved → green check; missing → amber + a one-click add.
                    Text {
                        text: Credentials.count > 0
                              ? "✓ Using saved credential"
                              : "⚠ No saved credential"
                        color: Credentials.count > 0 ? Theme.success : Theme.warning
                        font.family: Theme.fontFamily; font.pixelSize: 13
                        font.weight: Font.Medium
                    }
                    Item { Layout.fillWidth: true }
                    PrimaryButton {
                        label: Credentials.count > 0 ? "Manage Credentials" : "Add Credential"
                        primary: Credentials.count === 0
                        onClicked: App.openAddCredential()
                    }
                }

                // Apply-timing row — only meaningful for BIOS (applies on reboot).
                // BMC firmware self-applies with a BMC reset, so this is hidden.
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 12
                    visible: page.selectedIsBios
                    Text {
                        text: "APPLY"
                        Layout.preferredWidth: 90
                        Layout.alignment: Qt.AlignVCenter
                        color: Theme.textMuted
                        font.family: Theme.fontFamily; font.pixelSize: 11
                        font.weight: Font.Medium; font.letterSpacing: 0.6
                    }
                    Text {
                        Layout.fillWidth: true
                        text: page.applyNow
                              ? "Reboot & apply now — host restarts immediately after flashing"
                              : "On next reboot — image is staged; reboot the host later to apply"
                        color: page.applyNow ? Theme.warning : Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: 13
                        font.weight: page.applyNow ? Font.DemiBold : Font.Normal
                    }
                    // Apply-timing toggle.
                    // OFF (left)  = apply on next reboot (default, safe)
                    // ON  (right) = reboot & apply now
                    Rectangle {
                        implicitWidth: 42; implicitHeight: 24; radius: 12
                        color: page.applyNow ? Theme.warning : Theme.hover
                        border.color: Theme.border; border.width: 1
                        Behavior on color { ColorAnimation { duration: 150 } }
                        Rectangle {
                            width: 18; height: 18; radius: 9
                            x: page.applyNow ? parent.width - width - 3 : 3
                            y: 3
                            color: Theme.card
                            border.color: Theme.border; border.width: 1
                            Behavior on x { NumberAnimation { duration: 150; easing.type: Easing.OutCubic } }
                        }
                        TapHandler { onTapped: page.applyNow = !page.applyNow }
                    }
                }

                // Action row
                RowLayout {
                    Layout.fillWidth: true
                    Layout.topMargin: 4
                    spacing: 12
                    // Why the button is disabled, when it is
                    Text {
                        visible: !Updates.isRunning && !(page.targetCount > 0 && page.selectedFirmwareId > 0 && Credentials.count > 0)
                        Layout.fillWidth: true
                        text: Credentials.count === 0 ? "Add a BMC credential first (button above)"
                            : page.selectedFirmwareId <= 0 ? "Select a firmware above"
                            : page.targetCount === 0 ? "Select at least one server"
                            : ""
                        color: Theme.warning
                        font.family: Theme.fontFamily; font.pixelSize: 11; font.weight: Font.Medium
                    }
                    Item { Layout.fillWidth: true }
                    PrimaryButton {
                        label: Updates.isRunning ? "Cancel"
                             : ("Flash " + page.targetCount + " server" + (page.targetCount === 1 ? "" : "s"))
                        primary: !Updates.isRunning
                        // No gating — let the backend reject with a real
                        // message rather than the button just sitting gray
                        // with no explanation.
                        enabled: true
                        onClicked: {
                            console.warn("Updates.qml Flash clicked: targetIds=" +
                                JSON.stringify(page.targetIds()) +
                                " firmwareId=" + page.selectedFirmwareId +
                                " credsCount=" + Credentials.count +
                                " applyNow=" + page.applyNow +
                                " isRunning=" + Updates.isRunning)
                            if (Updates.isRunning) {
                                Updates.cancel()
                                return
                            }
                            // Pre-check each gate and surface a clear toast
                            // BEFORE calling the backend. If everything's set,
                            // we fall through to the actual start.
                            if (Credentials.count === 0) {
                                App.showToast("Add a BMC credential first (Settings → Credentials)", "warning")
                                App.openAddCredential()
                                return
                            }
                            if (page.selectedFirmwareId <= 0) {
                                App.showToast("Pick a firmware from the dropdown above first", "warning")
                                return
                            }
                            if (page.targetCount === 0) {
                                App.showToast("No servers selected — tap a server chip to include it", "warning")
                                return
                            }
                            App.showToast("Flashing " + page.targetCount + " server" +
                                          (page.targetCount === 1 ? "" : "s") + "…", "info")
                            Updates.start(
                                page.targetIds(),
                                page.selectedFirmwareId,
                                AppSettings.updateConcurrency,
                                "", "",
                                page.applyNow
                            )
                        }
                    }
                }
            }
        }

        // ---- Live progress + Recent jobs (split row) ----
        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 16

        Card {
            Layout.fillWidth: true
            Layout.fillHeight: true
            padding: 0

            ColumnLayout {
                anchors.fill: parent
                spacing: 0

                Item {
                    Layout.fillWidth: true
                    implicitHeight: 52
                    Text {
                        anchors.left: parent.left; anchors.leftMargin: 20
                        anchors.verticalCenter: parent.verticalCenter
                        text: Updates.isRunning
                              ? "Live Progress"
                              : (Updates.total > 0 ? "Last Run · Job #" + Updates.currentJobId : "Progress")
                        color: Theme.text
                        font.family: Theme.fontFamily; font.pixelSize: 15; font.weight: Font.DemiBold
                    }
                    Text {
                        anchors.right: parent.right; anchors.rightMargin: 20
                        anchors.verticalCenter: parent.verticalCenter
                        text: Updates.total > 0
                              ? Updates.succeeded + " OK · " + Updates.failed + " FAILED · " + Updates.total + " TOTAL"
                              : ""
                        color: Theme.textMuted
                        font.family: Theme.monoFamily; font.pixelSize: 11; font.letterSpacing: 0.4
                    }
                    Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.border }
                }

                Item {
                    Layout.fillWidth: true
                    Layout.fillHeight: true

                    ListView {
                        id: tasksList
                        anchors.fill: parent
                        clip: true
                        model: Updates.model
                        boundsBehavior: Flickable.StopAtBounds
                        delegate: Item {
                            width: ListView.view.width
                            implicitHeight: 60

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 20
                                anchors.rightMargin: 20
                                spacing: 16

                                ColumnLayout {
                                    Layout.preferredWidth: 200
                                    spacing: 2
                                    Text {
                                        text: model.host || ""
                                        color: Theme.text
                                        font.family: Theme.fontFamily
                                        font.pixelSize: 14
                                        font.weight: Font.DemiBold
                                    }
                                    Text {
                                        text: (model.oem || "") + " · " + (model.change || "")
                                        color: Theme.textMuted
                                        font.family: Theme.monoFamily
                                        font.pixelSize: 12
                                    }
                                }

                                Rectangle {
                                    Layout.fillWidth: true
                                    height: 8; radius: 4
                                    color: Theme.hover
                                    Rectangle {
                                        height: parent.height; radius: 4
                                        color: page.stateColor(model.state)
                                        width: parent.width * (model.progress || 0) / 100
                                        Behavior on width { NumberAnimation { duration: 200 } }
                                    }
                                }

                                Text {
                                    Layout.preferredWidth: 100
                                    text: model.state ? String(model.state).toUpperCase() : ""
                                    horizontalAlignment: Text.AlignRight
                                    color: page.stateColor(model.state)
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 11
                                    font.weight: Font.Medium
                                    font.letterSpacing: 0.5
                                }
                                Text {
                                    Layout.preferredWidth: 42
                                    text: (model.progress || 0) + "%"
                                    horizontalAlignment: Text.AlignRight
                                    color: Theme.text
                                    font.family: Theme.monoFamily
                                    font.pixelSize: 12
                                }
                            }
                            Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.border; opacity: 0.6 }
                        }
                    }

                    Column {
                        anchors.centerIn: parent
                        spacing: 8
                        visible: tasksList.count === 0
                        Text {
                            anchors.horizontalCenter: parent.horizontalCenter
                            text: "No active update"
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: 15
                            font.weight: Font.DemiBold
                        }
                        Text {
                            anchors.horizontalCenter: parent.horizontalCenter
                            text: Fleet.total > 0
                                  ? "Pick a firmware above and hit Flash"
                                  : "Run Discovery in Inventory first"
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: 13
                        }
                    }
                }
            }
        }

        // Recent jobs panel
        Card {
            Layout.preferredWidth: 320
            Layout.fillHeight: true
            padding: 0

            ColumnLayout {
                anchors.fill: parent
                spacing: 0

                Item {
                    Layout.fillWidth: true
                    implicitHeight: 52
                    Text {
                        anchors.left: parent.left; anchors.leftMargin: 20
                        anchors.verticalCenter: parent.verticalCenter
                        text: "Recent Jobs"
                        color: Theme.text
                        font.family: Theme.fontFamily; font.pixelSize: 15; font.weight: Font.DemiBold
                    }
                    Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.border }
                }

                ListView {
                    id: jobsList
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    model: Updates.jobsModel
                    boundsBehavior: Flickable.StopAtBounds
                    delegate: Item {
                        width: ListView.view.width
                        implicitHeight: 64

                        Rectangle { anchors.fill: parent; color: jobHover.hovered ? Theme.hover : "transparent"; Behavior on color { ColorAnimation { duration: 100 } } }
                        HoverHandler { id: jobHover }

                        ColumnLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 20
                            anchors.rightMargin: 12
                            anchors.topMargin: 10
                            anchors.bottomMargin: 10
                            spacing: 4

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 8
                                Text {
                                    text: "#" + (model.id || "")
                                    color: Theme.textMuted
                                    font.family: Theme.monoFamily
                                    font.pixelSize: 11
                                    Layout.preferredWidth: 38
                                }
                                Text {
                                    text: (model.fw_type || "") + " flash"
                                    color: Theme.text
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 12
                                    font.weight: Font.DemiBold
                                    Layout.fillWidth: true
                                    elide: Text.ElideRight
                                }
                                Text {
                                    text: model.state ? String(model.state).toUpperCase() : ""
                                    color: model.state === "completed" ? Theme.success
                                           : model.state === "failed" ? Theme.error
                                           : Theme.accent
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 10
                                    font.weight: Font.Medium
                                    font.letterSpacing: 0.5
                                }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 8
                                Text {
                                    text: (model.succeeded || 0) + " ok · " + (model.failed || 0) + " fail · " + (model.total || 0) + " total"
                                    color: Theme.textMuted
                                    font.family: Theme.monoFamily
                                    font.pixelSize: 10
                                    Layout.fillWidth: true
                                }
                                Rectangle {
                                    implicitWidth: 22; implicitHeight: 22; radius: 6
                                    color: exportMa.containsMouse ? Theme.accent : "transparent"
                                    border.color: Theme.border
                                    border.width: exportMa.containsMouse ? 0 : 1
                                    Text { anchors.centerIn: parent; text: "↓"; color: exportMa.containsMouse ? Theme.accentText : Theme.textMuted; font.pixelSize: 11; font.weight: Font.Bold }
                                    MouseArea {
                                        id: exportMa
                                        anchors.fill: parent
                                        hoverEnabled: true
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: Reports.exportJob(model.id)
                                        ToolTip.visible: containsMouse
                                        ToolTip.text: "Export DOCX report"
                                        ToolTip.delay: 600
                                    }
                                }
                            }
                        }
                        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.border; opacity: 0.5 }
                    }

                    Column {
                        anchors.centerIn: parent
                        spacing: 6
                        visible: jobsList.count === 0
                        Text {
                            anchors.horizontalCenter: parent.horizontalCenter
                            text: "No jobs yet"
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: 13
                            font.weight: Font.DemiBold
                        }
                        Text {
                            anchors.horizontalCenter: parent.horizontalCenter
                            text: "Past jobs appear here"
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: 11
                        }
                    }
                }
            }
        }
        }  // close split RowLayout

        // Inline error surface (e.g. service rejected the job)
        Text {
            Layout.fillWidth: true
            visible: (Updates.error || "").length > 0
            text: Updates.error || ""
            color: Theme.error
            font.family: Theme.fontFamily
            font.pixelSize: 12
            wrapMode: Text.WordWrap
        }
    }
}
