import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

Item {
    id: page

    property int  selectedIsoId: AppSettings.getInt("provision_last_iso_id", -1)
    property var  overrides:     ({})
    property var  refreshing:    ({})
    property var  inlineCreds:   ({})
    // Servers the AUTO-fetch has already tried this session. Without this, a
    // host that keeps reporting 0 drives (e.g. powered OFF — its BMC can't
    // enumerate NVMe) is re-fetched on every Fleet.countsChanged forever,
    // hammering the BMC with auth and risking account lockout. The manual
    // "Fetch Drives" button bypasses this guard (calls fetchDrives directly).
    property var  autoAttempted: ({})

    // Table column x-positions (from card left edge including 20px left margin).
    // Defined at page root so delegate items can reference page.cXxx safely.
    readonly property int cHost:  20
    readonly property int cIP:   230
    readonly property int cDrive: 400
    readonly property int cRBtn:  52   // refresh button region width

    onSelectedIsoIdChanged: AppSettings.setInt("provision_last_iso_id", selectedIsoId)

    function getDrives(sid) {
        try { return JSON.parse(Fleet.listDrives(sid)) } catch(e) { return [] }
    }
    // Shallow clones (Object.assign) are sufficient here — we only
    // replace top-level entries, never mutate nested objects in place.
    // JSON.parse(JSON.stringify(...)) on every keystroke (the audit found
    // this on the credential field's onTextChanged) makes typing visibly
    // laggy at 100+ rows; shallow clone is O(N) keys instead of O(payload).
    function pickDrive(sid, name) {
        var c = Object.assign({}, page.overrides)
        if (!name) delete c[String(sid)]; else c[String(sid)] = name
        page.overrides = c
    }
    function setInlineCred(sid, user, pass) {
        var c = Object.assign({}, page.inlineCreds)
        c[String(sid)] = { user: user, pass: pass }; page.inlineCreds = c
    }
    function fetchDrives(sid) {
        var c = Object.assign({}, page.refreshing)
        c[String(sid)] = true; page.refreshing = c
        // Mark attempted so the auto-loop won't re-issue this fetch (the manual
        // button still works — it calls this function directly regardless).
        var a = Object.assign({}, page.autoAttempted)
        a[String(sid)] = true; page.autoAttempted = a
        var ic = page.inlineCreds[String(sid)]
        if (ic && ic.user)
            Discovery.refreshServerDisksWithCreds(sid, ic.user, ic.pass)
        else
            Discovery.refreshServerDisks(sid)
    }
    function driveIcon(proto, media) {
        if ((proto||"").toUpperCase()==="NVME") return "⚡"
        if ((media||"").toUpperCase()==="SSD")  return "▢"
        return "◷"
    }
    function humanSize(b) {
        if (!b||b<=0) return "?"
        var g = b/1e9
        return g>=1000 ? (g/1000).toFixed(1)+" TB" : g.toFixed(0)+" GB"
    }

    // Auto-fetch drives whenever this page becomes visible (navigated to)
    // or when the fleet changes (new server discovered).
    // Uses vault credentials — no prompt needed if creds were saved before.
    function autoFetchMissingDrives() {
        var ids = Fleet.allServerIds
        for (var i = 0; i < ids.length; i++) {
            var sid = ids[i]
            var drives = page.getDrives(sid)
            if (drives.length === 0 && !page.refreshing[String(sid)]
                    && !page.autoAttempted[String(sid)]) {
                page.fetchDrives(sid)
            }
        }
    }

    // Fire auto-fetch when the page becomes visible (every navigation to it)
    onVisibleChanged: { if (visible) Qt.callLater(page.autoFetchMissingDrives) }
    // Also fire when new servers appear in the fleet
    Connections {
        target: Fleet
        function onCountsChanged() {
            if (page.visible) Qt.callLater(page.autoFetchMissingDrives)
        }
    }

    Component.onCompleted: Qt.callLater(page.autoFetchMissingDrives)

    // ---- destructive wipe confirmation (typed token, audited) ----
    Popup {
        id: wipeConfirm
        modal: true; dim: true
        anchors.centerIn: Overlay.overlay
        width: 380; padding: 0
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: Rectangle { color: Theme.card; radius: 14; border.color: Theme.border; border.width: 1 }
        contentItem: ColumnLayout {
            spacing: 14
            Text {
                Layout.margins: 20; Layout.bottomMargin: 0
                text: "Confirm disk wipe"
                color: Theme.text; font.family: Theme.fontFamily
                font.pixelSize: 16; font.weight: Font.DemiBold
            }
            Text {
                Layout.leftMargin: 20; Layout.rightMargin: 20; Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: "This ERASES all data on " + Object.keys(page.overrides).length +
                      " server(s) and installs the selected OS — it cannot be undone.\n\n" +
                      "Type WIPE to confirm."
                color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 13
            }
            TextField {
                id: wipeField
                Layout.leftMargin: 20; Layout.rightMargin: 20; Layout.fillWidth: true
                placeholderText: "WIPE"
                color: Theme.text
                font.family: Theme.monoFamily; font.pixelSize: 14
                background: Rectangle {
                    color: Theme.hover; radius: 8
                    border.color: wipeField.activeFocus ? Theme.accent : Theme.border
                    border.width: 1
                }
            }
            RowLayout {
                Layout.fillWidth: true; Layout.margins: 20; Layout.topMargin: 4
                Item { Layout.fillWidth: true }
                PrimaryButton { label: "Cancel"; primary: false; onClicked: wipeConfirm.close() }
                PrimaryButton {
                    label: "Wipe & Install"
                    enabled: wipeField.text.toUpperCase() === "WIPE"
                    onClicked: {
                        var ids = Object.keys(page.overrides).map(function(k) { return Number(k) })
                        Provisioning.start(ids, page.selectedIsoId, 0, 0,
                            wipeField.text, 8,
                            "manual_per_host", JSON.stringify(page.overrides))
                        wipeConfirm.close()
                    }
                }
            }
        }
    }

    Connections {
        target: Discovery
        function onDisksRefreshed(sid, ok, msg) {
            var c = Object.assign({}, page.refreshing)
            delete c[String(sid)]; page.refreshing = c
            if (!ok) {
                App.showToast("Fetch failed: " + msg, "error")
            } else {
                var ic = page.inlineCreds[String(sid)]
                if (ic && ic.user) {
                    // Inline creds worked. We save them to the vault so the
                    // subsequent flash/eject calls don't re-prompt, BUT we
                    // tell the operator EXACTLY what just happened so a
                    // one-shot fetch doesn't silently grow the vault.
                    Credentials.add("BMC " + (ic.user), ic.user, ic.pass, "default")
                    var copy = Object.assign({}, page.inlineCreds)
                    delete copy[String(sid)]; page.inlineCreds = copy
                    App.showToast(
                        msg + " · '" + ic.user + "' saved to default credential — "
                        + "remove in Settings → Credentials if you didn't want this.",
                        "warning",
                    )
                } else {
                    App.showToast(msg + " found", "success")
                }
            }
        }
    }

    // ================================================================
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 24
        spacing: 14

        // ---- TOP BAR ----
        RowLayout {
            Layout.fillWidth: true; spacing: 10

            Text { text: "ISO"; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11; font.weight: Font.Medium; font.letterSpacing: 0.6 }

            Flow { spacing: 6; Layout.fillWidth: true
                Repeater {
                    model: Isos.model
                    delegate: Rectangle {
                        implicitHeight: 28; implicitWidth: isoTxt.implicitWidth + 18; radius: 8
                        color: model.id === page.selectedIsoId ? Theme.accent : Theme.hover
                        border.color: model.id === page.selectedIsoId ? Theme.accent : Theme.border; border.width: 1
                        Behavior on color { ColorAnimation { duration: 120 } }
                        Text { id: isoTxt; anchors.centerIn: parent; text: (model.os_family||"?").toUpperCase()+" "+(model.os_version||""); color: model.id === page.selectedIsoId ? Theme.accentText : Theme.text; font.family: Theme.monoFamily; font.pixelSize: 12 }
                        TapHandler { onTapped: page.selectedIsoId = model.id }
                    }
                }
                Text { visible: Isos.count===0; text: "No ISOs — add one in ISO Library"; color: Theme.warning; font.family: Theme.fontFamily; font.pixelSize: 12 }
            }

            PrimaryButton {
                label: Provisioning.isRunning ? "Cancel" : "Wipe & Install"
                primary: !Provisioning.isRunning
                enabled: Provisioning.isRunning || (Fleet.total > 0 && page.selectedIsoId > 0 && Object.keys(page.overrides).length > 0)
                onClicked: {
                    if (Provisioning.isRunning) { Provisioning.cancel(); return }
                    // Destructive: require a typed confirmation before any wipe.
                    // The button no longer starts the job directly — it opens
                    // wipeConfirm, which collects the audited confirm token.
                    wipeField.text = ""
                    wipeConfirm.open()
                }
            }
        }

        // ---- TABLE ----
        Card {
            Layout.fillWidth: true; Layout.fillHeight: true; padding: 0

            ColumnLayout {
                anchors.fill: parent; spacing: 0

                // Header
                Item {
                    Layout.fillWidth: true; implicitHeight: 36
                    Text { x: page.cHost;  y: 10; text: "Host";         color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11; font.weight: Font.Medium }
                    Text { x: page.cIP;   y: 10; text: "IP";            color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11; font.weight: Font.Medium }
                    Text { x: page.cDrive; y: 10; text: "Target Drive";  color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11; font.weight: Font.Medium }
                    Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.border }
                }

                // Server list
                Item {
                    Layout.fillWidth: true; Layout.fillHeight: true

                    ListView {
                        id: srv
                        anchors.fill: parent; clip: true
                        model: Fleet.model; boundsBehavior: Flickable.StopAtBounds

                        delegate: Item {
                            id: rowItem
                            width: ListView.view.width; implicitHeight: 54
                            property int sid: model.id

                            Rectangle {
                                anchors.fill: parent
                                color: rowHov.hovered ? Theme.hover : "transparent"
                                Behavior on color { ColorAnimation { duration: 100 } }
                            }
                            HoverHandler { id: rowHov }

                            // Host — fixed position
                            Column {
                                x: page.cHost
                                anchors.verticalCenter: parent.verticalCenter
                                width: page.cIP - page.cHost - 10
                                spacing: 1
                                Text { text: model.hostname||model.ip||""; color: Theme.text; elide: Text.ElideRight; width: parent.width; font.family: Theme.fontFamily; font.pixelSize: 13; font.weight: Font.DemiBold }
                                Text { text: (model.oem||"").charAt(0).toUpperCase()+(model.oem||"").slice(1); color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11 }
                            }

                            // IP + power dot — fixed position
                            Row {
                                x: page.cIP
                                anchors.verticalCenter: parent.verticalCenter
                                spacing: 8
                                StatusDot { dotColor: model.power_state==="On"||model.status==="online"?Theme.success:Theme.textMuted; coreSize: 6; anchors.verticalCenter: parent.verticalCenter }
                                Text { text: model.ip||""; color: Theme.text; font.family: Theme.monoFamily; font.pixelSize: 12 }
                            }

                            // Drive dropdown trigger — fills space between cDrive and refresh button
                            Rectangle {
                                id: driveBtn
                                x: page.cDrive
                                anchors.verticalCenter: parent.verticalCenter
                                width: parent.width - page.cDrive - page.cRBtn - 4
                                implicitHeight: 32; radius: 8
                                    color: driveMa.containsMouse ? Theme.hover : Theme.elevated
                                    border.color: drivePop.visible ? Theme.accent : Theme.border; border.width: 1
                                    Behavior on border.color { ColorAnimation { duration: 120 } }

                                    RowLayout {
                                        anchors.fill: parent; anchors.leftMargin: 12; anchors.rightMargin: 10; spacing: 8

                                        // Type icon + selected drive name
                                        Text {
                                            text: {
                                                var pick = page.overrides[String(rowItem.sid)]
                                                if (pick) {
                                                    var drives = page.getDrives(rowItem.sid)
                                                    for (var i=0;i<drives.length;i++) {
                                                        if (drives[i].name===pick)
                                                            return page.driveIcon(drives[i].protocol,drives[i].media_type)
                                                    }
                                                    return "▢"
                                                }
                                                return ""
                                            }
                                            color: Theme.accent
                                            font.family: Theme.monoFamily; font.pixelSize: 13
                                            visible: text !== ""
                                        }

                                        Text {
                                            Layout.fillWidth: true
                                            text: {
                                                var pick = page.overrides[String(rowItem.sid)]
                                                if (pick) return pick
                                                var cnt = page.getDrives(rowItem.sid).length
                                                return cnt > 0 ? "Select drive (" + cnt + " available)" : "Fetch drives from BMC"
                                            }
                                            color: page.overrides[String(rowItem.sid)] ? Theme.text : Theme.textMuted
                                            font.family: Theme.monoFamily; font.pixelSize: 12; elide: Text.ElideRight
                                        }

                                        Text { text: drivePop.visible?"▴":"▾"; color: Theme.textMuted; font.family: Theme.monoFamily; font.pixelSize: 11 }
                                    }

                                    MouseArea { id: driveMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: drivePop.visible ? drivePop.close() : drivePop.open() }

                                    // ---- DRIVE PICKER POPUP ----
                                    Popup {
                                        id: drivePop
                                        parent: driveBtn
                                        y: driveBtn.height + 4; x: 0
                                        width: Math.max(driveBtn.width, 520)
                                        // Cap height so the popup never extends off-screen.
                                        // Capped at 340px (~5 rows of 2 chips); scrollable inside.
                                        height: drivePop.drives.length === 0
                                                ? credRow.implicitHeight + 28
                                                : Math.min(driveFlow.implicitHeight + 28, 340)
                                        padding: 0
                                        modal: false
                                        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

                                        background: Rectangle {
                                            color: Theme.card; radius: 10
                                            border.color: Theme.border; border.width: 1
                                        }

                                        // ``refreshTrigger`` is touched inside the
                                        // binding so incrementing it forces a
                                        // re-evaluation without breaking the
                                        // property binding via imperative
                                        // assignment (the old code did
                                        // ``drivePop.drives = ...`` which
                                        // permanently severed the link to
                                        // ``page.getDrives`` for this popup).
                                        property int refreshTrigger: 0
                                        property var drives: { refreshTrigger; return page.getDrives(rowItem.sid) }
                                        Connections {
                                            target: Discovery
                                            function onDisksRefreshed(sid, ok, msg) {
                                                if (sid === rowItem.sid)
                                                    drivePop.refreshTrigger++
                                            }
                                        }

                                        contentItem: Item {
                                            // Credential row (no drives yet)
                                            RowLayout {
                                                id: credRow
                                                anchors { left: parent.left; right: parent.right; top: parent.top }
                                                anchors.margins: 12
                                                spacing: 8
                                                visible: drivePop.drives.length === 0

                                                Rectangle {
                                                    Layout.preferredWidth: 140; implicitHeight: 32; radius: 8; color: Theme.elevated
                                                    border.color: uf.activeFocus ? Theme.accent : Theme.border; border.width: 1
                                                    TextField {
                                                        id: uf; anchors.fill: parent; anchors.margins: 1
                                                        placeholderText: "Username"; placeholderTextColor: Theme.textMuted
                                                        color: Theme.text; font.family: Theme.fontFamily; font.pixelSize: 12
                                                        background: Item {}
                                                        leftPadding: 10
                                                        text: { var ic=page.inlineCreds[String(rowItem.sid)]; return ic?(ic.user||""):"" }
                                                        onTextChanged: page.setInlineCred(rowItem.sid, text, pf.text)
                                                    }
                                                }
                                                Rectangle {
                                                    Layout.preferredWidth: 140; implicitHeight: 32; radius: 8; color: Theme.elevated
                                                    border.color: pf.activeFocus ? Theme.accent : Theme.border; border.width: 1
                                                    TextField {
                                                        id: pf; anchors.fill: parent; anchors.margins: 1
                                                        placeholderText: "Password"; placeholderTextColor: Theme.textMuted
                                                        color: Theme.text; echoMode: TextInput.Password
                                                        font.family: Theme.fontFamily; font.pixelSize: 12
                                                        background: Item {}
                                                        leftPadding: 10
                                                        text: { var ic=page.inlineCreds[String(rowItem.sid)]; return ic?(ic.pass||""):"" }
                                                        onTextChanged: page.setInlineCred(rowItem.sid, uf.text, text)
                                                        Keys.onReturnPressed: { page.fetchDrives(rowItem.sid); drivePop.close() }
                                                    }
                                                }
                                                PrimaryButton {
                                                    label: page.refreshing[String(rowItem.sid)] ? "Fetching…" : "Fetch Drives"
                                                    enabled: !page.refreshing[String(rowItem.sid)] && uf.text.length > 0
                                                    onClicked: { page.fetchDrives(rowItem.sid); drivePop.close() }
                                                }
                                                Text {
                                                    text: "Auto-saved to vault"
                                                    color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 10
                                                    Layout.fillWidth: true
                                                }
                                            }

                                            // Scrollable drive chip grid
                                            Item {
                                                anchors.fill: parent
                                                visible: drivePop.drives.length > 0

                                                // Drive count badge + scroll hint
                                                Item {
                                                    id: popHeader
                                                    anchors { left: parent.left; right: parent.right; top: parent.top }
                                                    height: 32
                                                    visible: drivePop.drives.length > 0
                                                    Text {
                                                        anchors.left: parent.left; anchors.leftMargin: 14
                                                        anchors.verticalCenter: parent.verticalCenter
                                                        text: drivePop.drives.length + " drives available — scroll to see all"
                                                        color: Theme.textMuted
                                                        font.family: Theme.fontFamily; font.pixelSize: 11
                                                    }
                                                    Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.border; opacity: 0.6 }
                                                }

                                                // Flickable containing the chip grid
                                                Flickable {
                                                    id: driveFlickable
                                                    anchors {
                                                        left: parent.left; right: scrollBar.left
                                                        top: popHeader.bottom; bottom: parent.bottom
                                                    }
                                                    anchors.leftMargin: 10; anchors.rightMargin: 4
                                                    anchors.topMargin: 8; anchors.bottomMargin: 10
                                                    clip: true
                                                    contentHeight: driveFlow.implicitHeight
                                                    boundsBehavior: Flickable.StopAtBounds

                                                    Flow {
                                                        id: driveFlow
                                                        width: driveFlickable.width
                                                        spacing: 6

                                                        Repeater {
                                                            model: drivePop.drives
                                                            delegate: Rectangle {
                                                                property bool isSel: page.overrides[String(rowItem.sid)] === modelData.name
                                                                implicitHeight: 48
                                                                implicitWidth: Math.min(dc.implicitWidth + 20, (driveFlow.width - 6) / 2)
                                                                radius: 8
                                                                color: isSel ? Theme.accent : (dm.containsMouse ? Theme.hover : Theme.elevated)
                                                                border.color: isSel ? Theme.accent : Theme.border
                                                                border.width: isSel ? 0 : 1
                                                                Behavior on color { ColorAnimation { duration: 100 } }
                                                                clip: true

                                                                RowLayout {
                                                                    id: dc
                                                                    anchors.fill: parent
                                                                    anchors.leftMargin: 10; anchors.rightMargin: 8
                                                                    spacing: 8
                                                                    Text {
                                                                        text: page.driveIcon(modelData.protocol, modelData.media_type)
                                                                        color: parent.parent.isSel ? Theme.accentText : (modelData.protocol==="NVMe"?Theme.accent:Theme.textMuted)
                                                                        font.family: Theme.monoFamily; font.pixelSize: 14
                                                                    }
                                                                    ColumnLayout {
                                                                        spacing: 0; Layout.fillWidth: true
                                                                        Text {
                                                                            text: modelData.name||"?"
                                                                            color: parent.parent.parent.isSel?Theme.accentText:Theme.text
                                                                            font.family: Theme.monoFamily; font.pixelSize: 12; font.weight: Font.Bold
                                                                            elide: Text.ElideRight; Layout.fillWidth: true
                                                                        }
                                                                        Text {
                                                                            text: (modelData.protocol||"?")+" · "+page.humanSize(modelData.capacity_bytes)+(modelData.model?"  "+modelData.model:"")
                                                                            color: parent.parent.parent.isSel?Theme.accentText:Theme.textMuted
                                                                            font.family: Theme.fontFamily; font.pixelSize: 10
                                                                            elide: Text.ElideRight; Layout.fillWidth: true
                                                                        }
                                                                    }
                                                                    Text {
                                                                        visible: parent.parent.isSel; text: "✓"
                                                                        color: Theme.accentText
                                                                        font.family: Theme.monoFamily; font.pixelSize: 13; font.weight: Font.Bold
                                                                    }
                                                                }
                                                                MouseArea {
                                                                    id: dm; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                                                    onClicked: { page.pickDrive(rowItem.sid, parent.isSel?"":modelData.name); drivePop.close() }
                                                                }
                                                            }
                                                        }
                                                    }
                                                }

                                                // Slim scrollbar track
                                                Rectangle {
                                                    id: scrollBar
                                                    anchors { right: parent.right; top: popHeader.bottom; bottom: parent.bottom }
                                                    anchors.topMargin: 8; anchors.bottomMargin: 10; anchors.rightMargin: 4
                                                    width: 4; radius: 2
                                                    color: Theme.border
                                                    visible: driveFlickable.contentHeight > driveFlickable.height

                                                    // Thumb
                                                    Rectangle {
                                                        width: parent.width; radius: 2
                                                        color: thumbMa.containsMouse || thumbMa.pressed ? Theme.accent : Theme.textMuted
                                                        Behavior on color { ColorAnimation { duration: 100 } }
                                                        height: Math.max(24, scrollBar.height * driveFlickable.height / driveFlickable.contentHeight)
                                                        y: driveFlickable.contentHeight > 0
                                                           ? (driveFlickable.contentY / driveFlickable.contentHeight) * scrollBar.height
                                                           : 0
                                                        MouseArea {
                                                            id: thumbMa; anchors.fill: parent; hoverEnabled: true
                                                            cursorShape: Qt.PointingHandCursor
                                                            property real startY: 0; property real startContent: 0
                                                            onPressed: { startY = mouseY; startContent = driveFlickable.contentY }
                                                            onPositionChanged: {
                                                                if (!pressed) return
                                                                var delta = (mouseY - startY) * driveFlickable.contentHeight / scrollBar.height
                                                                driveFlickable.contentY = Math.max(0, Math.min(startContent + delta, driveFlickable.contentHeight - driveFlickable.height))
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }

                                // Refresh button — right-anchored
                                Rectangle {
                                    anchors.right: parent.right
                                    anchors.rightMargin: 12
                                    anchors.verticalCenter: parent.verticalCenter
                                    implicitWidth: 32; implicitHeight: 32; radius: 8
                                    color: rMa.containsMouse ? Theme.hover : "transparent"
                                    border.color: Theme.border; border.width: 1
                                    Text {
                                        anchors.centerIn: parent; text: "↻"
                                        color: page.refreshing[String(rowItem.sid)] ? Theme.accent : Theme.textMuted
                                        font.family: Theme.monoFamily; font.pixelSize: 14; font.weight: Font.Bold
                                        RotationAnimation on rotation {
                                            running: page.refreshing[String(rowItem.sid)] === true
                                            loops: Animation.Infinite; from: 0; to: 360; duration: 1000
                                        }
                                    }
                                    MouseArea { id: rMa; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: page.fetchDrives(rowItem.sid) }
                                }

                            Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.border; opacity: 0.5 }
                        }
                    }

                    // Empty state
                    Column {
                        anchors.centerIn: parent; spacing: 10; visible: Fleet.total === 0
                        Text { anchors.horizontalCenter: parent.horizontalCenter; text: "No servers discovered"; color: Theme.text; font.family: Theme.fontFamily; font.pixelSize: 15; font.weight: Font.DemiBold }
                        Text { anchors.horizontalCenter: parent.horizontalCenter; text: "Run Discovery in Inventory to add servers"; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 13 }
                        PrimaryButton { anchors.horizontalCenter: parent.horizontalCenter; label: "Open Inventory"; primary: false; onClicked: App.navigateTo(1) }
                    }
                }
            }
        }
    }
}
