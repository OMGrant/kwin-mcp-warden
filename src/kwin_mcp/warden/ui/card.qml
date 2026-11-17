// kwin-mcp-warden's consent card (design/mock/index.html, "The request").
//
// Run by Qt's own qml program: qml --apptype widget card.qml -- '<request json>'.
// One layer-shell window per screen dims it; the primary screen's window also
// holds the card, centered, and takes the keyboard. The answer is the exit code:
// 42 = Allow (only ever a click on Allow, after the 1 s wait), 12 = Go to Session,
// 11 = no answer in time, 10 = Don't Allow (a click, or Esc). The warden treats
// every other ending (a crash, a kill) as Don't Allow. With Allow, the card also
// prints the length the person picked (WARDEN-CARD-LENGTH <minutes>); the warden
// takes it only when it's one the card offered.
//
// Colors, text and font come from the KDE theme through Kirigami.Theme; the
// shapes, sizes, buttons, scale and Claude's orange are kwin-mcp-warden's own.
import QtQuick
import QtQuick.Window
import org.kde.kirigami as Kirigami
import org.kde.layershell as LayerShell

QtObject {
    id: app

    readonly property var req: {
        const args = Qt.application.arguments;
        try {
            return JSON.parse(args[args.length - 1]);
        } catch (e) {
            console.warn("kwin-mcp-warden card: bad request, answering Don't Allow");
            Qt.exit(10);
            return {};
        }
    }
    readonly property int allowCode: 42
    // KWin's Scale effect, which used to open the card: 200 ms, OutCubic, from 80 % and
    // transparent, times Plasma's animation speed (0 = no animation).
    readonly property int entranceMs: Math.round(200 * (app.req.motion === undefined ? 1 : app.req.motion))
    property bool answered: false

    // The length picked on the card (minutes, 0 = this session): the agent's ask at first.
    property int length: app.req.length === undefined ? -1 : app.req.length
    readonly property var lengths: app.req.lengths || []
    readonly property string allowLabel: {
        const chosen = app.lengths.find(l => l.minutes === app.length);
        return chosen ? chosen.allow : (app.req.allow || "Allow");
    }

    function answer(code) {
        if (answered)
            return;
        answered = true;
        if (code === app.allowCode && app.length >= 0)
            console.info("WARDEN-CARD-LENGTH " + app.length);
        console.info("kwin-mcp-warden card: answer " + code);
        Qt.exit(code);
    }

    property Timer unanswered: Timer {
        running: true
        interval: (app.req.timeout_s || 120) * 1000
        onTriggered: app.answer(11)
    }

    property Instantiator screens: Instantiator {
        model: Qt.application.screens
        delegate: Window {
            id: win
            required property var modelData
            readonly property bool primary: {
                const names = Qt.application.screens.map(s => s.name);
                const wanted = app.req.primary || "";
                return names.indexOf(wanted) >= 0 ? modelData.name === wanted
                                                   : modelData.name === names[0];
            }
            screen: modelData
            // At its own monitor's origin: Qt moves a window to the monitor its geometry lies on
            // (QWindow::screen() changes with geometry, layer-shell-qt MR 89's note), and
            // layer-shell-qt puts the surface on that monitor. At the default 0,0 every window
            // went to the first monitor, so a smaller monitor's dim sat as a darker box in the
            // middle of the primary one (reproduced with two outputs in a
            // nested KWin). Set again in Component.onCompleted, right before it shows.
            x: modelData.virtualX
            y: modelData.virtualY
            color: "transparent"
            flags: Qt.FramelessWindowHint
            width: modelData.width
            height: modelData.height
            title: "kwin-mcp-warden"

            // KWin types a layer-shell window by its scope, and animates a "normal" one (any
            // name it doesn't know) with its Scale effect: the whole window, dim and all,
            // zoomed in (the dim animating with the card, which it shouldn't). A utility window gets no open animation
            // from KWin (kwin src/layershellv1window.cpp, scopeToType; effects scale and
            // fadingpopups), so the card plays Scale's entrance itself and the dim only fades.
            LayerShell.Window.scope: "utility"
            LayerShell.Window.layer: LayerShell.Window.LayerOverlay
            LayerShell.Window.anchors: LayerShell.Window.AnchorTop | LayerShell.Window.AnchorBottom
                                       | LayerShell.Window.AnchorLeft | LayerShell.Window.AnchorRight
            LayerShell.Window.exclusionZone: -1
            LayerShell.Window.keyboardInteractivity: primary ? LayerShell.Window.KeyboardInteractivityExclusive
                                                             : LayerShell.Window.KeyboardInteractivityNone

            Component.onCompleted: {
                // layer-shell-qt 6.3 and newer: the card on the primary screen takes the
                // keyboard as it opens (Esc is Don't Allow). Older versions don't have the
                // property and give the keyboard through Exclusive interactivity alone.
                try {
                    LayerShell.Window.activateOnShow = primary;
                } catch (e) {
                    console.info("kwin-mcp-warden: no activateOnShow in this layer-shell-qt");
                }
                // Its monitor and origin once more, last: on Qt 6.8 a size set after them
                // moved the window back to the first monitor (Debian 13).
                screen = modelData;
                x = modelData.virtualX;
                y = modelData.virtualY;
                visible = true;
            }

            // Every screen dims behind the card, as Windows does for its UAC prompt. It fades
            // in with the card's entrance and never zooms.
            Rectangle {
                anchors.fill: parent
                color: Qt.rgba(0, 0, 0, app.req.dim === undefined ? 0.6 : app.req.dim)
                NumberAnimation on opacity {
                    from: 0; to: 1; duration: app.entranceMs; easing.type: Easing.OutCubic
                }
                // A click on the dim answers nothing and reaches nothing behind it.
                MouseArea { anchors.fill: parent; acceptedButtons: Qt.AllButtons; hoverEnabled: true }
            }

            Loader {
                active: win.primary
                anchors.centerIn: parent
                sourceComponent: Card {}
            }
        }
    }

    // ── the card ────────────────────────────────────────────────────────────
    component Card: Item {
        id: card
        Kirigami.Theme.colorSet: Kirigami.Theme.Window
        Kirigami.Theme.inherit: false

        // Theme tokens (the mock's names), from Kirigami.Theme at run time.
        readonly property color pop: Kirigami.Theme.backgroundColor
        readonly property color fg: Kirigami.Theme.textColor
        readonly property color fgMuted: Kirigami.Theme.disabledTextColor
        readonly property color fgSubtle: Kirigami.ColorUtils.tintWithAlpha(pop, fgMuted, 0.8)
        readonly property color bgLift: Kirigami.ColorUtils.tintWithAlpha(pop, fg, 0.08)
        readonly property color panelHover: Kirigami.ColorUtils.tintWithAlpha(pop, fg, 0.04)
        readonly property color hair: Qt.rgba(fg.r, fg.g, fg.b, 0.1)
        readonly property color border: Qt.rgba(fg.r, fg.g, fg.b, 0.15)
        readonly property color popEdge: Qt.rgba(fg.r, fg.g, fg.b, 0.2)
        readonly property color tint: Qt.rgba(fg.r, fg.g, fg.b, 0.08)
        readonly property color warning: Kirigami.Theme.neutralTextColor
        readonly property color error: Kirigami.Theme.negativeTextColor
        readonly property color claude: "#d77757" // Claude Code's own orange
        readonly property string sans: Kirigami.Theme.defaultFont.family

        readonly property var levels: [["see", "See"], ["arrange", "Arrange"], ["control", "Control"], ["full_desktop", "Full desktop"]]
        readonly property int levelIndex: levels.findIndex(l => l[0] === (app.req.level || ""))
        readonly property color levelColor: levelIndex === 3 ? error : levelIndex === 2 ? claude : fgMuted

        // KWin's Scale entrance, played by the card itself (the dim behind it only fades).
        transformOrigin: Item.Center
        property bool entered: false
        ParallelAnimation {
            running: true
            NumberAnimation { target: card; property: "scale"; from: 0.8; to: 1; duration: app.entranceMs; easing.type: Easing.OutCubic }
            NumberAnimation { target: card; property: "opacity"; from: 0; to: 1; duration: app.entranceMs; easing.type: Easing.OutCubic }
            onFinished: card.entered = true
        }

        // Allow waits one second (Firefox's security.dialog_enable_delay).
        property real waited
        readonly property bool ready: waited >= 1
        // Where the buttons are, for the warden's own tests (it writes card-geometry.json),
        // once the card has its full size.
        readonly property bool placed: ready && entered
        onPlacedChanged: if (placed) {
            const s = Window.window ? Window.window.screen : null;
            const ox = s ? s.virtualX : 0, oy = s ? s.virtualY : 0;
            const out = {};
            const parts = [allowButton, denyButton, goButton];
            for (let i = 0; i < lengthChips.count; i++)
                parts.push(lengthChips.itemAt(i));
            for (const b of parts) {
                const p = b.mapToItem(null, 0, 0);
                out[b.objectName] = [Math.round(p.x + ox), Math.round(p.y + oy), b.width, b.height];
            }
            console.info("WARDEN-CARD-GEOMETRY " + JSON.stringify(out));
        }
        NumberAnimation on waited { from: 0; to: 1; duration: app.req.delay_ms === undefined ? 1000 : app.req.delay_ms }

        width: app.req.width || 540
        height: body.implicitHeight + 42
        focus: true
        Keys.onEscapePressed: app.answer(10)
        // Every other key is swallowed: no key answers the card except Esc.
        Keys.onPressed: event => { event.accepted = true; }
        Component.onCompleted: forceActiveFocus()

        // 0 24px 60px -12px rgba(0,0,0,.75), as the mock draws it.
        Loader {
            anchors.fill: surface
            Component.onCompleted: setSource(Qt.resolvedUrl("Shadow.qml"), {
                offset: Qt.vector2d(0, 24), blur: 60, spread: -12, radius: 12,
                color: Qt.rgba(0, 0, 0, 0.75) })
        }

        Rectangle {
            id: surface
            anchors.fill: parent
            radius: 12
            color: card.pop
            border.width: 1
            border.color: card.popEdge
        }

        // Two columns: the app's mark on the left, every word on one edge on the right.
        Item {
            id: mark
            x: 24; y: 22
            width: 48; height: 48

            Kirigami.Icon {
                anchors.fill: parent
                visible: !!app.req.icon && !app.req.tile
                source: app.req.icon || ""
            }
            Rectangle {
                anchors.fill: parent
                visible: !!app.req.tile || !app.req.icon
                radius: 12
                color: card.tint
                border.width: 1
                border.color: card.hair
                Kirigami.Icon {
                    anchors.centerIn: parent
                    width: 24; height: 24
                    source: Qt.resolvedUrl("icons/" + (app.req.tile || "desk") + ".svg")
                    isMask: true
                    color: card.fgMuted
                }
            }
            // The white hand on a gray disc: "waiting on you".
            Rectangle {
                visible: app.req.waiting !== false
                x: parent.width - 24 + 6; y: parent.height - 24 + 6
                width: 30; height: 30; radius: 15
                anchors.horizontalCenterOffset: 0
                color: card.pop // the 3 px ring in the card's own color
                Rectangle {
                    anchors.centerIn: parent
                    width: 24; height: 24; radius: 12
                    color: card.bgLift
                    border.width: 1
                    border.color: card.border
                    Kirigami.Icon {
                        anchors.centerIn: parent
                        width: 14; height: 14
                        source: Qt.resolvedUrl("icons/hand.svg")
                        isMask: true
                        color: card.fg
                    }
                }
                Component.onCompleted: { x = mark.width - 24 + 6 - 3; y = mark.height - 24 + 6 - 3; }
            }
        }

        Column {
            id: body
            x: 24 + 48 + 16; y: 22
            width: card.width - x - 24
            spacing: 0

            Text {
                width: parent.width
                text: "<span style='color:" + card.fgMuted + "; font-weight:500'>" + esc(app.req.session || app.req.agent || "An agent") + "</span>"
                      + (app.req.desktop ? " · " + esc(app.req.desktop) : "") + " · " + esc(app.req.state || "waiting on you")
                textFormat: Text.RichText
                color: card.fgSubtle
                font.family: card.sans; font.pixelSize: 12
                lineHeight: 16; lineHeightMode: Text.FixedHeight
                wrapMode: Text.Wrap
                function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
            }
            Item { width: 1; height: 3 }
            Text {
                width: parent.width
                text: app.req.title || ""
                textFormat: Text.PlainText
                color: card.fg
                font.family: card.sans; font.pixelSize: 18; font.weight: Font.DemiBold
                font.letterSpacing: -0.27
                lineHeight: 24; lineHeightMode: Text.FixedHeight
                wrapMode: Text.Wrap
            }
            Item { width: 1; height: 8; visible: !!(app.req.reason || app.req.note) }
            Text {
                width: parent.width
                visible: !!app.req.reason
                text: "“" + (app.req.reason || "") + "”"
                textFormat: Text.PlainText
                color: card.fgMuted
                font.family: card.sans; font.pointSize: 10.125 // 13.5 px at 96 dpi; pixelSize takes whole pixels only
                lineHeight: 21; lineHeightMode: Text.FixedHeight
                wrapMode: Text.Wrap
            }
            Text {
                width: parent.width
                visible: !!app.req.note
                text: app.req.note || ""
                textFormat: Text.PlainText
                color: card.fgMuted
                font.family: card.sans; font.pointSize: 10.125 // 13.5 px at 96 dpi; pixelSize takes whole pixels only
                lineHeight: 21; lineHeightMode: Text.FixedHeight
                wrapMode: Text.Wrap
            }

            // The terms: the scale, what it means, the risk.
            Item {
                width: parent.width
                visible: card.levelIndex >= 0 || !!app.req.means || !!app.req.risk
                height: visible ? terms.implicitHeight + 34 : 0
                Rectangle { y: 18; width: parent.width; height: 1; color: card.hair }
                Column {
                    id: terms
                    y: 34
                    width: parent.width
                    spacing: 12

                    // How much: four steps, filled up to the one asked for, in that level's color.
                    Item {
                        visible: card.levelIndex >= 0
                        width: parent.width
                        height: visible ? 6 + 6 + 16 : 0
                        Repeater {
                            model: card.levels
                            delegate: Item {
                                required property var modelData
                                required property int index
                                readonly property real w: (terms.width - 12) / 4
                                x: index * (w + 4)
                                width: w; height: 28
                                Rectangle {
                                    width: parent.w; height: 6; radius: 3
                                    color: index <= card.levelIndex ? card.levelColor : card.bgLift
                                }
                                Text {
                                    y: 12
                                    text: modelData[1]
                                    color: index === card.levelIndex ? card.levelColor : card.fgSubtle
                                    font.family: card.sans; font.pixelSize: 12
                                    lineHeight: 16; lineHeightMode: Text.FixedHeight
                                }
                            }
                        }
                    }
                    Text {
                        visible: !!app.req.means
                        width: parent.width
                        text: (app.req.means || "") + (app.req.means_more
                              ? " <font color='" + card.fgSubtle + "'>" + app.req.means_more + "</font>" : "")
                        textFormat: Text.StyledText
                        color: card.fg
                        font.family: card.sans; font.pixelSize: 13
                        lineHeight: 20; lineHeightMode: Text.FixedHeight
                        wrapMode: Text.Wrap
                    }
                    // The risk, only when there is one.
                    Rectangle {
                        visible: !!app.req.risk
                        width: parent.width
                        height: visible ? riskText.implicitHeight + 18 : 0
                        radius: 8
                        readonly property color tone: app.req.hard ? card.error : card.warning
                        color: Qt.rgba(tone.r, tone.g, tone.b, 0.14)
                        Kirigami.Icon {
                            x: 12; y: 9 + 3
                            width: 14; height: 14
                            source: Qt.resolvedUrl("icons/warn.svg")
                            isMask: true
                            color: parent.tone
                        }
                        Text {
                            id: riskText
                            x: 12 + 14 + 10; y: 9
                            width: parent.width - x - 12
                            text: app.req.risk || ""
                            textFormat: Text.PlainText
                            color: card.fg
                            font.family: card.sans; font.pixelSize: 13
                            lineHeight: 20; lineHeightMode: Text.FixedHeight
                            wrapMode: Text.Wrap
                        }
                    }
                }
            }

            // How long: the person picks, shorter or longer than the agent asked. A click
            // here only picks; it never answers the card.
            Item { width: 1; height: 16; visible: lengthRow.visible }
            Row {
                id: lengthRow
                visible: app.req.buttons !== false && app.lengths.length > 1
                width: parent.width
                spacing: 6
                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: "For"
                    color: card.fgSubtle
                    font.family: card.sans; font.pixelSize: 12
                    rightPadding: 4
                }
                Repeater {
                    id: lengthChips
                    model: app.lengths
                    delegate: Rectangle {
                        id: lengthChip
                        required property var modelData
                        objectName: "length-" + modelData.minutes
                        readonly property bool chosen: app.length === modelData.minutes
                        width: chipText.implicitWidth + 20
                        height: 26
                        radius: 13
                        color: chosen ? card.fg : (chipArea.containsMouse ? card.panelHover : "transparent")
                        border.width: chosen ? 0 : 1
                        border.color: card.border
                        Text {
                            id: chipText
                            anchors.centerIn: parent
                            text: lengthChip.modelData.chip
                            color: lengthChip.chosen ? Kirigami.Theme.backgroundColor : card.fg
                            font.family: card.sans; font.pixelSize: 12; font.weight: Font.Medium
                        }
                        Accessible.role: Accessible.RadioButton
                        Accessible.name: lengthChip.modelData.chip
                        Accessible.checked: lengthChip.chosen
                        MouseArea {
                            id: chipArea
                            anchors.fill: parent
                            hoverEnabled: true
                            acceptedButtons: Qt.LeftButton
                            cursorShape: Qt.PointingHandCursor
                            onClicked: app.length = lengthChip.modelData.minutes
                        }
                    }
                }
            }

            // The answer: Allow (says how long), Don't Allow, and Go to Session.
            Item { width: 1; height: 18; visible: app.req.buttons !== false }
            Row {
                visible: app.req.buttons !== false
                width: parent.width
                spacing: 8
                CardButton {
                    card: card
                    id: allowButton
                    objectName: "allow"
                    kind: card.ready ? "primary" : "wait"
                    label: app.allowLabel
                    onClicked: if (card.ready) app.answer(app.allowCode)
                }
                CardButton {
                    id: denyButton
                    card: card
                    objectName: "deny"
                    kind: "outline"
                    label: app.req.deny || "Don't Allow"
                    onClicked: app.answer(10)
                }
                Item {
                    width: Math.max(0, parent.width - allowButton.width - denyWidth.width - goButton.width - 24)
                    height: 1
                    CardButton { id: denyWidth; card: card; visible: false; kind: "outline"; label: app.req.deny || "Don't Allow" }
                }
                CardButton {
                    card: card
                    id: goButton
                    objectName: "go"
                    visible: app.req.go !== false
                    kind: "ghost"
                    label: "Go to Session"
                    onClicked: app.answer(12)
                }
            }
        }

    }

    component CardButton: Rectangle {
        id: button
        required property Item card
        property string kind: "outline"
        property string label: ""
        signal clicked()
        readonly property bool hovered: area.containsMouse
        width: text.implicitWidth + 28
        height: 32
        radius: 7
        clip: true
        color: kind === "primary" ? (hovered ? button.card.fgMuted : card.fg)
             : kind === "wait" || kind === "outline" ? (kind === "outline" && hovered ? card.panelHover : card.tint)
             : (hovered ? card.panelHover : "transparent")
        border.width: kind === "wait" || kind === "outline" ? 1 : 0
        border.color: card.hair
        Text {
            id: text
            anchors.centerIn: parent
            text: button.label
            color: button.kind === "primary" ? Kirigami.Theme.backgroundColor
                 : button.kind === "wait" ? card.fgSubtle
                 : button.kind === "ghost" && !button.hovered ? card.fgSubtle : card.fg
            font.family: card.sans; font.pixelSize: 13; font.weight: Font.Medium
        }
        // The filling line while Allow waits.
        Rectangle {
            visible: button.kind === "wait"
            anchors.bottom: parent.bottom
            height: 2
            width: parent.width * button.card.waited
            color: card.fgMuted
        }
        MouseArea {
            id: area
            anchors.fill: parent
            hoverEnabled: true
            acceptedButtons: Qt.LeftButton
            cursorShape: Qt.PointingHandCursor
            onClicked: button.clicked()
        }
    }
}
