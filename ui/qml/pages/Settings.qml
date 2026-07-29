import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window
import "../components"

Item {
    id: page

    // Lift the toast above the 56 px save bar while this page is on top.
    // Other pages get the default ``bottomInset: 24`` so the toast sits
    // close to the bottom edge. Restored on destruction.
    Component.onCompleted: if (typeof globalToast !== "undefined") globalToast.bottomInset = 88
    Component.onDestruction: if (typeof globalToast !== "undefined") globalToast.bottomInset = 24

    // ---- Page-level pending (uncommitted) state for ALL editable settings ----
    property int pConcurrency: AppSettings.updateConcurrency
    property int pConnect:     AppSettings.connectTimeoutS
    property int pOp:          AppSettings.operationTimeoutMin
    property int pRetention:   AppSettings.logRetentionDays

    readonly property int changeCount:
        (pConcurrency !== AppSettings.updateConcurrency ? 1 : 0)
        + (pConnect    !== AppSettings.connectTimeoutS    ? 1 : 0)
        + (pOp         !== AppSettings.operationTimeoutMin ? 1 : 0)
        + (pRetention  !== AppSettings.logRetentionDays   ? 1 : 0)
    readonly property bool dirty: changeCount > 0

    function _save() {
        var n = changeCount
        if (pConcurrency !== AppSettings.updateConcurrency) AppSettings.setUpdateConcurrency(pConcurrency)
        if (pConnect     !== AppSettings.connectTimeoutS)    AppSettings.setConnectTimeoutS(pConnect)
        if (pOp          !== AppSettings.operationTimeoutMin) AppSettings.setOperationTimeoutMin(pOp)
        if (pRetention   !== AppSettings.logRetentionDays)   AppSettings.setLogRetentionDays(pRetention)
        App.showToast(n === 1 ? "Setting saved" : (n + " settings saved"), "success")
    }
    function _discard() {
        pConcurrency = AppSettings.updateConcurrency
        pConnect     = AppSettings.connectTimeoutS
        pOp          = AppSettings.operationTimeoutMin
        pRetention   = AppSettings.logRetentionDays
    }

    // Resync pending when external setting changes AND nothing is mid-edit.
    Connections {
        target: AppSettings
        function onChanged() { if (!page.dirty) page._discard() }
    }

    Flickable {
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: saveBar.top
        contentHeight: col.implicitHeight + 48
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        ColumnLayout {
            id: col
            x: 24; y: 24
            width: page.width - 48
            spacing: 16

            // ---- Appearance (instant) ----
            Card {
                Layout.fillWidth: true
                implicitHeight: appCol.implicitHeight + 40
                ColumnLayout {
                    id: appCol
                    anchors { left: parent.left; right: parent.right; top: parent.top }
                    spacing: 16
                    SectionTitle { text: "Appearance" }
                    RowLayout {
                        Layout.fillWidth: true
                        SettingLabel { text: "Theme" }
                        Item { Layout.fillWidth: true }
                        ModeSegmented {}
                    }
                }
            }

            // ---- Credentials (instant) ----
            Card {
                Layout.fillWidth: true
                implicitHeight: credCol.implicitHeight + 40
                ColumnLayout {
                    id: credCol
                    anchors { left: parent.left; right: parent.right; top: parent.top }
                    spacing: 14
                    RowLayout {
                        Layout.fillWidth: true
                        SectionTitle { text: "Credentials" }
                        Item { Layout.fillWidth: true }
                        Text {
                            text: Credentials.isVaultSecure ? "DPAPI" : "DEV FALLBACK"
                            color: Credentials.isVaultSecure ? Theme.success : Theme.warning
                            font.family: Theme.monoFamily
                            font.pixelSize: 10
                            font.weight: Font.Medium
                            font.letterSpacing: 0.5
                        }
                        PrimaryButton { label: "+  Add"; primary: false; onClicked: addCredDialog.open() }
                    }

                    Repeater {
                        model: Credentials.model
                        delegate: RowLayout {
                            Layout.fillWidth: true
                            spacing: 12
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 1
                                Text {
                                    text: (model.label || "(unlabeled)") + "  ·  " + (model.username || "")
                                    color: Theme.text
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 13
                                    font.weight: Font.DemiBold
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                                Text {
                                    text: "scope: " + (model.scope || "default")
                                    color: Theme.textMuted
                                    font.family: Theme.monoFamily
                                    font.pixelSize: 11
                                }
                            }
                            Rectangle {
                                implicitWidth: 24; implicitHeight: 24; radius: 6
                                color: delMa.containsMouse ? Theme.error : "transparent"
                                Text { anchors.centerIn: parent; text: "✕"; color: delMa.containsMouse ? "#FFFFFF" : Theme.textMuted; font.pixelSize: 11 }
                                MouseArea {
                                    id: delMa
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: Credentials.remove(model.id)
                                }
                            }
                        }
                    }
                    Text {
                        visible: Credentials.count === 0
                        text: "No saved credentials yet."
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 12
                    }
                }
            }

            // ---- Performance (saved via bottom bar) ----
            Card {
                Layout.fillWidth: true
                implicitHeight: perfCol.implicitHeight + 40
                ColumnLayout {
                    id: perfCol
                    anchors { left: parent.left; right: parent.right; top: parent.top }
                    spacing: 16
                    SectionTitle { text: "Performance" }

                    RowLayout {
                        Layout.fillWidth: true
                        SettingLabel { text: "Update concurrency" }
                        Item { Layout.fillWidth: true }
                        NumberSetting {
                            value: page.pConcurrency
                            suffix: "servers"
                            minValue: 1; maxValue: 999
                            onCommitted: (v) => { page.pConcurrency = v }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        SettingLabel { text: "Connect timeout" }
                        Item { Layout.fillWidth: true }
                        NumberSetting {
                            value: page.pConnect
                            suffix: "s"
                            minValue: 1; maxValue: 120
                            onCommitted: (v) => { page.pConnect = v }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        SettingLabel { text: "Operation timeout" }
                        Item { Layout.fillWidth: true }
                        NumberSetting {
                            value: page.pOp
                            suffix: "min"
                            minValue: 1; maxValue: 480
                            onCommitted: (v) => { page.pOp = v }
                        }
                    }
                }
            }

            // ---- Maintenance (saved via bottom bar) ----
            Card {
                Layout.fillWidth: true
                implicitHeight: maintCol.implicitHeight + 40
                ColumnLayout {
                    id: maintCol
                    anchors { left: parent.left; right: parent.right; top: parent.top }
                    spacing: 16
                    SectionTitle { text: "Maintenance" }

                    RowLayout {
                        Layout.fillWidth: true
                        SettingLabel { text: "Log retention" }
                        Item { Layout.fillWidth: true }
                        NumberSetting {
                            value: page.pRetention
                            suffix: "days"
                            minValue: 1; maxValue: 365
                            onCommitted: (v) => { page.pRetention = v }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        SettingLabel { text: "Audit log" }
                        Item { Layout.fillWidth: true }
                        PrimaryButton {
                            label: "Export"
                            primary: false
                            onClicked: Reports.exportAuditLog()
                        }
                    }
                }
            }

            Item { Layout.preferredHeight: 8 }
        }
    }

    // ---- Sticky save bar at bottom of the page ----
    Rectangle {
        id: saveBar
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom }
        height: 56
        color: page.dirty ? Theme.elevated : Theme.card
        Behavior on color { ColorAnimation { duration: 200 } }

        // Mirror the window's outer rounded corner on the bottom-right so the
        // save bar doesn't paint a square over the window's bottom-right
        // pixel. The NavRail owns the bottom-LEFT corner, so we only round
        // this side. -1 backs out the 1px border inset on the window.
        bottomRightRadius: Window.window
                           ? Math.max(0, Window.window.outerRadius - 1)
                           : 0

        Rectangle { anchors.top: parent.top; width: parent.width; height: 1; color: Theme.border }

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 24
            anchors.rightMargin: 24
            spacing: 12

            Text {
                visible: page.dirty
                text: page.changeCount + (page.changeCount === 1 ? " unsaved change" : " unsaved changes")
                color: Theme.warning
                font.family: Theme.fontFamily
                font.pixelSize: 12
                font.weight: Font.Medium
                font.letterSpacing: 0.3
            }
            Text {
                visible: !page.dirty
                text: "All settings saved"
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: 12
            }
            Item { Layout.fillWidth: true }
            PrimaryButton {
                label: "Discard"
                primary: false
                enabled: page.dirty
                onClicked: page._discard()
            }
            PrimaryButton {
                id: saveBtn
                property bool justSaved: false
                label: justSaved ? "✓  Saved" : "Save Changes"
                // Stay enabled briefly after click so the success label is visible.
                enabled: page.dirty || justSaved
                onClicked: {
                    page._save()
                    justSaved = true
                    savedFeedbackTimer.restart()
                }
            }
            Timer {
                id: savedFeedbackTimer
                interval: 1800
                onTriggered: saveBtn.justSaved = false
            }
        }
    }

    AddCredentialDialog { id: addCredDialog }

    // Action entry point — used by global "Add Credential" affordances
    // (e.g. the Inventory Welcome card).
    Connections {
        target: App
        function onAddCredentialRequested() { addCredDialog.open() }
    }

    // ---- safe leaf inline helpers (no default-property redeclaration) ----
    component SectionTitle: Text {
        color: Theme.text
        font.family: Theme.fontFamily
        font.pixelSize: 15
        font.weight: Font.DemiBold
    }

    component SettingLabel: Text {
        color: Theme.textMuted
        font.family: Theme.fontFamily
        font.pixelSize: 13
    }

    component ModeSegmented: Rectangle {
        id: seg
        implicitWidth: 252
        implicitHeight: 34
        radius: 9
        color: Theme.hover
        border.color: Theme.border
        border.width: 1
        Row {
            anchors.fill: parent
            anchors.margins: 3
            spacing: 3
            Repeater {
                model: [ { k: "light", t: "Light" }, { k: "dark", t: "Dark" }, { k: "system", t: "System" } ]
                delegate: Rectangle {
                    required property var modelData
                    width: (seg.width - 12) / 3
                    height: parent.height
                    radius: 7
                    color: Theme.mode === modelData.k ? Theme.accent : "transparent"
                    Behavior on color { ColorAnimation { duration: 150 } }
                    Text {
                        anchors.centerIn: parent
                        text: modelData.t
                        color: Theme.mode === modelData.k ? Theme.accentText : Theme.textMuted
                        font.family: Theme.fontFamily; font.pixelSize: 12; font.weight: Font.Medium
                    }
                    TapHandler { onTapped: Theme.setMode(modelData.k) }
                }
            }
        }
    }

    // Editable int field. Commits on Enter / focus loss by emitting `committed(v)`.
    component NumberSetting: Rectangle {
        id: ns
        property int value: 0
        property string suffix: ""
        property int minValue: 1
        property int maxValue: 999
        signal committed(int newValue)

        implicitHeight: 32
        implicitWidth: 140
        radius: 9
        color: Theme.hover
        border.color: tfn.activeFocus ? Theme.accent : Theme.border
        border.width: 1
        Behavior on border.color { ColorAnimation { duration: 150 } }

        // Keep the input in sync with the bound value when not editing.
        // Use a Binding component so the text<-value link survives across
        // imperative assignments. Old code did `tfn.text = value` from
        // onValueChanged, which broke the property binding at TextField.text
        // permanently — so externally-changed values were silently lost
        // and the IntValidator could land in a feedback loop on clamp.
        onValueChanged: if (!tfn.activeFocus) tfn.text = String(value)

        Row {
            anchors.centerIn: parent
            spacing: 6
            TextField {
                id: tfn
                width: 60
                height: 28
                // No initial text binding here — set inside Component.onCompleted
                // so the imperative assignment above doesn't fight a live binding.
                Component.onCompleted: text = String(ns.value)
                color: Theme.text
                font.family: Theme.monoFamily
                font.pixelSize: 13
                background: Item {}
                verticalAlignment: TextInput.AlignVCenter
                horizontalAlignment: TextInput.AlignRight
                validator: IntValidator { bottom: ns.minValue; top: ns.maxValue }
                selectByMouse: true

                // Commit live (on each keystroke) when the text is a valid
                // integer in range. This keeps the page-level `dirty`
                // signal accurate the moment the user changes a value, so
                // the Save button enables immediately and doesn't have to
                // wait for the user to press Enter or click elsewhere.
                onTextChanged: {
                    if (text.length === 0) return
                    var v = parseInt(text)
                    if (isNaN(v)) return
                    if (v < ns.minValue || v > ns.maxValue) return
                    if (v !== ns.value) ns.committed(v)
                }
                // Also keep the on-blur path so empties get reset to the
                // bound value (the eager path above just returns on empty
                // input — without this, the field would stay blank).
                onEditingFinished: {
                    var v = parseInt(text)
                    if (isNaN(v)) { text = ns.value; return }
                    v = Math.max(ns.minValue, Math.min(ns.maxValue, v))
                    text = v
                    if (v !== ns.value) ns.committed(v)
                }
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: ns.suffix
                color: Theme.textMuted
                font.family: Theme.fontFamily
                font.pixelSize: 11
            }
        }
    }
}
