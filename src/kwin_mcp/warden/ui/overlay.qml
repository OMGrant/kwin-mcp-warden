// kwin-mcp-warden's overlay (design/mock/index.html, "While Claude has it", "Your hands
// come first", "The access list"): the orange frame on the window Claude controls, the
// tag above it with Stop, the "Claude" label on the pointer, and the access list from
// the tray icon.
//
// Run by the warden's daemon as two processes: qml --apptype widget overlay.qml -- frames
// <overlay.json path> draws the click-through frames and the pointer label only, and
// "-- controls <path>" draws everything you can click (the tags, the tray, the list).
// The warden tells them apart by process: the frames process takes no input.
// It reads overlay.json four times a second and answers on stderr with lines
// "WARDEN:STOP", "WARDEN:END <grant id>" and "WARDEN:LOG". It never grants anything.
import QtQuick
import QtQuick.Window
import Qt.labs.platform as Platform
import org.kde.kirigami as Kirigami
import org.kde.layershell as LayerShell

QtObject {
    id: root

    readonly property string statePath: {
        const args = Qt.application.arguments;
        return args[args.length - 1];
    }
    readonly property bool framesPart: {
        const args = Qt.application.arguments;
        return args[args.length - 2] === "frames";
    }
    property var st: ({ grants: [], who: "", plural: false, listTitle: "Access", frames: [], a11y: null, paused: false, pausedFor: "pointer", resumeIn: 0, pointer: null, log: [], stopKey: "Meta+Alt+Esc" })
    // Typing waits only for your clicks and keys; the pointer, shared with you, for any use.
    readonly property string pausedLine: st.pausedFor === "presses" ? "Paused while you click or type"
                                                                    : "Paused while you use the mouse"
    property bool listOpen: false
    // A click on the tray icon moves focus around it: Plasma's panel takes it on the press,
    // and Plasma hands the app a focus token for the list (Qt puts it in
    // XDG_ACTIVATION_TOKEN). So focus lost in the list's first moment isn't the person
    // clicking away, and the click that just closed the list mustn't reopen it.
    // (Without this guard, the tray icon's first click did nothing.)
    // The first moment counts from the list's first focus after it opens, not from the click:
    // on a busy machine the list can show late, and Plasma's focus shuffle comes after it
    // shows (2026-11-16: a full test run closed it this way).
    property double listActiveAt: 0
    property double listClosedAt: 0
    readonly property int listSettleMs: 600
    onListOpenChanged: {
        if (listOpen)
            listActiveAt = 0;
        else
            listClosedAt = Date.now();
    }

    function send(command) {
        console.info("WARDEN:" + command);
    }

    property Timer poll: Timer {
        running: true
        repeat: true
        triggeredOnStart: true
        interval: 250
        onTriggered: {
            const xhr = new XMLHttpRequest();
            xhr.open("GET", "file://" + root.statePath, false);
            try {
                xhr.send();
                if (xhr.responseText)
                    root.st = JSON.parse(xhr.responseText);
            } catch (e) {
                // a half-written file; the next read gets it
            }
        }
    }

    // The tray icon: orange while any session holds a grant.
    property Platform.SystemTrayIcon tray: Platform.SystemTrayIcon {
        visible: !root.framesPart
        icon.source: Qt.resolvedUrl(root.st.grants.length ? "icons/tray-on.svg" : "icons/tray.svg")
        tooltip: root.st.grants.length ? (root.st.who || "Claude") + (root.st.plural ? " hold " : " holds ")
                 + root.st.grants.length + (root.st.grants.length === 1 ? " grant" : " grants")
                 : "No agent holds a grant"
        onActivated: {
            if (!root.listOpen && Date.now() - root.listClosedAt < 400)
                return; // this click already closed it, by taking its focus
            root.listOpen = !root.listOpen;
        }
    }

    // One click-through layer per screen for the frames and the pointer label.
    property Instantiator frames: Instantiator {
        model: root.framesPart ? Qt.application.screens : []
        delegate: Window {
            id: frameWin
            required property var modelData
            readonly property Item tokens: frameTokens
            Tokens { id: frameTokens }
            screen: modelData
            x: modelData.virtualX // its own monitor's origin (card.qml, the screens' windows)
            y: modelData.virtualY
            color: "transparent"
            flags: Qt.FramelessWindowHint | Qt.WindowTransparentForInput
            // The warden skips this layer when it asks what is under a point or what a
            // picture may show: it takes no input and covers nothing.
            title: "kwin-mcp-warden-frame"
            width: modelData.width
            height: modelData.height
            LayerShell.Window.scope: "kwin-mcp-warden-frame"
            LayerShell.Window.layer: LayerShell.Window.LayerOverlay
            LayerShell.Window.anchors: LayerShell.Window.AnchorTop | LayerShell.Window.AnchorBottom
                                       | LayerShell.Window.AnchorLeft | LayerShell.Window.AnchorRight
            LayerShell.Window.exclusionZone: -1
            LayerShell.Window.keyboardInteractivity: LayerShell.Window.KeyboardInteractivityNone
            Component.onCompleted: {
                screen = modelData; // set last, as card.qml does (Qt 6.8 order)
                x = modelData.virtualX;
                y = modelData.virtualY;
                visible = true;
            }

            Repeater {
                model: root.st.frames
                delegate: Rectangle {
                    required property var modelData
                    // inset -6px, radius 12, a 3 px ring in Claude's orange (paused: 40%)
                    x: modelData.frame[0] - frameWin.modelData.virtualX - 6 - 3
                    y: modelData.frame[1] - frameWin.modelData.virtualY - 6 - 3
                    width: modelData.frame[2] + 18
                    height: modelData.frame[3] + 18
                    radius: 15
                    color: "transparent"
                    border.width: 3
                    border.color: root.st.paused ? Qt.rgba(0.843, 0.467, 0.341, 0.4) : frameWin.tokens.claude
                    Behavior on border.color { ColorAnimation { duration: 150 } }
                }
            }

            // The agent's name rides the pointer while the agent moves it.
            Rectangle {
                visible: !!root.st.pointer && !root.st.paused
                x: root.st.pointer ? root.st.pointer.x - frameWin.modelData.virtualX + 18 : 0
                y: root.st.pointer ? root.st.pointer.y - frameWin.modelData.virtualY + 20 : 0
                height: 24
                width: label.implicitWidth + 16
                radius: 6
                color: frameWin.tokens.claude
                Text {
                    id: label
                    anchors.centerIn: parent
                    text: root.st.pointer ? (root.st.pointer.agent || "Claude") : ""
                    Accessible.role: Accessible.StaticText; Accessible.name: text
                    color: "#0a0a0a"
                    font.family: frameWin.tokens.sans; font.pixelSize: 12; font.weight: Font.DemiBold
                }
            }
        }
    }

    // The tag above each framed window: its own small layer, so only it takes clicks.
    property Instantiator tags: Instantiator {
        model: root.framesPart ? [] : root.st.frames
        delegate: Window {
            id: tagWin
            required property var modelData
            readonly property Item tokens: tagTokens
            Tokens { id: tagTokens }
            readonly property var home: {
                const screens = Qt.application.screens;
                const x = modelData.frame[0], y = modelData.frame[1];
                for (let i = 0; i < screens.length; i++) {
                    const s = screens[i];
                    if (x >= s.virtualX && x < s.virtualX + s.width && y >= s.virtualY - 60 && y < s.virtualY + s.height)
                        return s;
                }
                return screens[0];
            }
            screen: home
            x: home.virtualX // its own monitor's origin (card.qml, the screens' windows)
            y: home.virtualY
            // A window moved to another monitor takes its tag there (the surface's monitor is
            // fixed when it's made, so it's made again).
            readonly property string homeName: home ? home.name : "" // by name, as the list
            onHomeNameChanged: if (visible) {
                visible = false;
                screen = home;
                x = home.virtualX;
                y = home.virtualY;
                visible = true;
            }
            color: "transparent"
            flags: Qt.FramelessWindowHint
            width: tag.width
            height: tag.height
            LayerShell.Window.scope: "kwin-mcp-warden-tag"
            LayerShell.Window.layer: LayerShell.Window.LayerOverlay
            LayerShell.Window.anchors: LayerShell.Window.AnchorTop | LayerShell.Window.AnchorLeft
            LayerShell.Window.margins.left: Math.max(0, modelData.frame[0] - home.virtualX - 6)
            LayerShell.Window.margins.top: Math.max(0, modelData.frame[1] - home.virtualY - 50)
            LayerShell.Window.keyboardInteractivity: LayerShell.Window.KeyboardInteractivityNone
            LayerShell.Window.exclusionZone: -1
            Component.onCompleted: {
                screen = home; // set last, as card.qml does (Qt 6.8 order)
                x = home.virtualX;
                y = home.virtualY;
                visible = true;
            }

            Rectangle {
                id: tag
                width: row.implicitWidth + 12 + 4
                height: 36
                radius: 8
                color: tagWin.tokens.pop
                border.width: 1
                border.color: tagWin.tokens.popEdge
                Row {
                    id: row
                    x: 12
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 10
                    Rectangle {
                        anchors.verticalCenter: parent.verticalCenter
                        width: 8; height: 8; radius: 4
                        color: root.st.paused ? tagWin.tokens.fgSubtle : tagWin.tokens.claude
                        Rectangle {
                            visible: !root.st.paused
                            anchors.centerIn: parent
                            width: 16; height: 16; radius: 8
                            color: Qt.rgba(0.843, 0.467, 0.341, 0.22)
                            z: -1
                        }
                    }
                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        text: root.st.paused
                              ? (root.st.handsAgo < 0.5 ? root.pausedLine
                                                        : (tagWin.modelData.agent || "Claude") + " continues in " + Math.max(1, Math.ceil(root.st.resumeIn)) + " s")
                              : (tagWin.modelData.agent || "Claude") + " is controlling " + tagWin.modelData.app
                        Accessible.role: Accessible.StaticText; Accessible.name: text
                        color: tagWin.tokens.fg
                        font.family: tagWin.tokens.sans; font.pointSize: 9.375 // 12.5 px at 96 dpi
                    }
                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        text: tagWin.modelData.session + " · " + tagWin.modelData.left
                        color: tagWin.tokens.fgSubtle
                        font.family: tagWin.tokens.sans; font.pixelSize: 13
                    }
                    Rectangle { anchors.verticalCenter: parent.verticalCenter; width: 1; height: 18; color: tagWin.tokens.hair }
                    WardenButton {
                        anchors.verticalCenter: parent.verticalCenter
                        tokens: tagWin.tokens
                        icon: "stop"
                        label: "Stop"
                        hint: root.st.stopKey || ""
                        onClicked: root.send("STOP")
                    }
                }
            }
        }
    }

    // The corner notice (design/mock/index.html, "In control"): top right of the primary
    // monitor while any session can click or type on your screen, with the one button that
    // ends it and its key. Its own small layer, so only it takes clicks.
    property Window notice: Window {
        id: noticeWin
        readonly property Item tokens: noticeTokens
        Tokens { id: noticeTokens }
        readonly property var home: {
            const screens = Qt.application.screens;
            for (let i = 0; i < screens.length; i++) {
                if (screens[i].name === root.st.primary)
                    return screens[i];
            }
            return screens[0];
        }
        readonly property bool paused: !!root.st.paused
        readonly property real motion: root.st.motion === undefined ? 1 : root.st.motion
        // Shown only once it sits on its monitor: a window that turns visible before its
        // screen and position follow gets its surface on the old monitor (layer-shell-qt
        // reads the monitor when the surface is made), and one that changes monitor while
        // shown is shown again there (otherwise the list could open far from its monitor).
        readonly property bool shown: !root.framesPart && !!root.st.notice
        function place() {
            screen = home;
            x = home.virtualX;
            y = home.virtualY;
        }
        onShownChanged: {
            if (shown)
                place();
            visible = shown;
        }
        // By name: the state is read again four times a second, and each read hands back a
        // new screen object for the same monitor.
        readonly property string homeName: home ? home.name : ""
        onHomeNameChanged: if (visible) {
            visible = false;
            place();
            visible = true;
        }
        Component.onCompleted: {
            place();
            visible = shown;
        }

        // Claude's actions: the warden writes the time of each one Claude delivered into
        // "acted", next to overlay.json; read ten times a second while the notice is up.
        property string lastActed: ""
        property Timer actedPoll: Timer {
            running: noticeWin.visible
            repeat: true
            interval: 100
            onTriggered: {
                const xhr = new XMLHttpRequest();
                const path = root.statePath.substring(0, root.statePath.lastIndexOf("/")) + "/acted";
                xhr.open("GET", "file://" + path, false);
                try {
                    xhr.send();
                } catch (e) {
                    return;
                }
                const value = (xhr.responseText || "").trim();
                if (!value || value === noticeWin.lastActed)
                    return;
                noticeWin.lastActed = value;
                // An action from before the notice came up is old news, not a ring.
                if (Date.now() / 1000 - parseFloat(value) > 1.5)
                    return;
                if (!noticeWin.paused && noticeWin.motion > 0) {
                    marker.act();
                    console.info("WARDEN-NOTICE-RING " + value);
                }
            }
        }
        color: "transparent"
        flags: Qt.FramelessWindowHint
        width: noticeCard.width + 2 * 24
        height: noticeCard.height + 2 * 24
        LayerShell.Window.scope: "kwin-mcp-warden-notice"
        LayerShell.Window.layer: LayerShell.Window.LayerOverlay
        LayerShell.Window.anchors: LayerShell.Window.AnchorTop | LayerShell.Window.AnchorRight
        LayerShell.Window.keyboardInteractivity: LayerShell.Window.KeyboardInteractivityNone
        LayerShell.Window.exclusionZone: -1

        // 0 18px 40px -12px rgba(0,0,0,.7), as the mock draws it
        Loader {
            anchors.fill: noticeCard
            Component.onCompleted: setSource(Qt.resolvedUrl("Shadow.qml"), {
                offset: Qt.vector2d(0, 18), blur: 40, spread: -12, radius: 12,
                color: Qt.rgba(0, 0, 0, 0.7) })
        }
        Rectangle {
            id: noticeCard
            x: 24; y: 24
            // The box fits its words: the right gutter equals the bottom one, 14 px. A long session name stops growing it at 460 px and ends in "…".
            readonly property real gutter: 14
            width: Math.min(460, noticeBody.x + noticeBody.fit + gutter)
            height: noticeBody.implicitHeight + 14 + gutter
            // Left corners concentric with the marker: its 32 px radius plus the 22 px gutter;
            // right corners as before.
            radius: 12
            topLeftRadius: Math.min(54, height / 2)
            bottomLeftRadius: Math.min(54, height / 2)
            color: noticeWin.tokens.pop
            border.width: 1
            border.color: noticeWin.tokens.popEdge

            // The marker: a 64 px disc beside the copy, centered on it; Claude's orange ring and core.
            // Its motion is E, "Live, inward" (design/mock/notice-motion.html):
            // lit and still while Claude only holds control; each click, keypress
            // or scroll Claude delivers draws one ring in from just outside the marker, closing
            // on the core over 0.9 s and speeding up; the core brightens as it lands. Paused:
            // no motion, gray. Plasma's animation speed scales it; animations off: still.
            Rectangle {
                id: marker
                x: 22  // a little more gutter on the left
                anchors.verticalCenter: parent.verticalCenter
                width: 64; height: 64; radius: 32
                readonly property color tone: noticeWin.paused ? noticeWin.tokens.fgSubtle : noticeWin.tokens.claude
                color: noticeWin.paused ? noticeWin.tokens.tint : Qt.rgba(tone.r, tone.g, tone.b, 0.18)
                border.width: 1
                border.color: noticeWin.paused ? noticeWin.tokens.hair : Qt.rgba(tone.r, tone.g, tone.b, 0.35)
                // The mock's CSS curves: the ring's cubic-bezier(.5,0,.75,.4), the core's ease-out.
                readonly property var closing: [0.5, 0, 0.75, 0.4, 1, 1]
                readonly property var easeOut: [0, 0, 0.58, 1, 1, 1]
                readonly property real factor: noticeWin.motion
                property var landings: []

                function act() {
                    if (noticeWin.paused || factor <= 0)
                        return;
                    inward.restart();
                    landings.push(Date.now() + 900 * factor);
                    land.start();
                }
                Connections {
                    target: noticeWin
                    function onPausedChanged() {
                        if (noticeWin.paused) {
                            inward.stop();
                            ring.opacity = 0;
                            marker.landings = [];
                            core.glow = 0;
                        }
                    }
                }

                Rectangle {
                    id: core
                    anchors.centerIn: parent
                    width: 24; height: 24; radius: 12
                    // CSS brightness(1.3) at the peak
                    property real glow: 0
                    readonly property real lift: 1 + 0.3 * glow
                    color: Qt.rgba(Math.min(1, parent.tone.r * lift), Math.min(1, parent.tone.g * lift),
                                   Math.min(1, parent.tone.b * lift), 1)
                    Behavior on glow {
                        enabled: marker.factor > 0
                        NumberAnimation { duration: 250 * marker.factor; easing.type: Easing.Bezier; easing.bezierCurve: marker.easeOut }
                    }
                }
                // A 2 px ring just outside the 64 px marker at the start (scale 1.18), the core's
                // edge at the end (scale 0.38); gone between actions.
                Rectangle {
                    id: ring
                    anchors.centerIn: parent
                    width: 68; height: 68; radius: 34
                    color: "transparent"
                    border.width: 2
                    border.color: noticeWin.tokens.claude
                    opacity: 0
                    scale: 1.18
                }
                ParallelAnimation {
                    id: inward
                    NumberAnimation {
                        target: ring; property: "scale"; from: 1.18; to: 0.38
                        duration: 900 * marker.factor
                        easing.type: Easing.Bezier; easing.bezierCurve: marker.closing
                    }
                    SequentialAnimation {
                        NumberAnimation {
                            target: ring; property: "opacity"; from: 0; to: 0.55
                            duration: 225 * marker.factor
                            easing.type: Easing.Bezier; easing.bezierCurve: marker.closing
                        }
                        NumberAnimation {
                            target: ring; property: "opacity"; from: 0.55; to: 0.95
                            duration: 675 * marker.factor
                            easing.type: Easing.Bezier; easing.bezierCurve: marker.closing
                        }
                    }
                    onFinished: ring.opacity = 0
                }
                // Each action's landing, 0.9 s after it: the core brightens for 180 ms, then settles.
                Timer {
                    id: land
                    interval: 16
                    repeat: true
                    onTriggered: {
                        const now = Date.now();
                        let hit = false;
                        while (marker.landings.length && marker.landings[0] <= now) {
                            marker.landings.shift();
                            hit = true;
                        }
                        if (hit) {
                            core.glow = 1;
                            settle.restart();
                        }
                        if (!marker.landings.length)
                            stop();
                    }
                }
                Timer { id: settle; interval: 180; onTriggered: core.glow = 0 }
            }
            Column {
                id: noticeBody
                x: 22 + 64 + 24; y: 14  // 24 px from the marker
                readonly property real fit: Math.max(noticeTitle.implicitWidth, noticeLine.implicitWidth,
                                                     endControl.width)
                width: noticeCard.width - x - noticeCard.gutter
                Text {
                    id: noticeTitle
                    width: parent.width
                    elide: Text.ElideRight
                    text: !root.st.notice ? ""
                          : !noticeWin.paused ? (root.st.notice.who || "Claude") + (root.st.notice.plural ? " are" : " is") + " in control"
                          : (root.st.handsAgo < 0.5 ? root.pausedLine
                             : (root.st.notice.who || "Claude") + (root.st.notice.plural ? " continue" : " continues")
                               + " in " + Math.max(1, Math.ceil(root.st.resumeIn)) + " s")
                    Accessible.role: Accessible.StaticText; Accessible.name: text
                    color: noticeWin.tokens.fg
                    font.family: noticeWin.tokens.sans; font.pixelSize: 14; font.weight: Font.DemiBold
                    lineHeight: 20; lineHeightMode: Text.FixedHeight
                }
                Text {
                    id: noticeLine
                    width: parent.width
                    elide: Text.ElideRight
                    text: root.st.notice ? root.st.notice.line : ""
                    textFormat: Text.PlainText
                    color: noticeWin.tokens.fgSubtle
                    font.family: noticeWin.tokens.sans; font.pointSize: 9.375 // 12.5 px at 96 dpi
                    lineHeight: 18; lineHeightMode: Text.FixedHeight
                    topPadding: 1
                }
                Item { width: 1; height: 12 }
                WardenButton {
                    id: endControl
                    tokens: noticeWin.tokens
                    large: true
                    icon: "stop"
                    label: root.st.notice && root.st.notice.sessions > 1 ? "End All Control" : "End Control"
                    hint: root.st.stopKey || ""
                    onClicked: root.send("STOP")
                    // Where the button is, for the warden's own tests (the daemon writes it down).
                    onWidthChanged: Qt.callLater(noticeWin.reportGeometry)
                }
            }
        }
        function reportGeometry() {
            if (!visible)
                return;
            const p = endControl.mapToItem(null, 0, 0);
            console.info("WARDEN-NOTICE-GEOMETRY " + JSON.stringify({
                screen: home.name, screenX: home.virtualX, screenY: home.virtualY,
                screenWidth: home.width, window: [width, height],
                button: [Math.round(p.x), Math.round(p.y), endControl.width, endControl.height] }));
        }
        onVisibleChanged: Qt.callLater(reportGeometry)
    }

    // The access list, from the tray icon: above the bottom-right corner of the primary screen.
    property Window list: Window {
        id: listWin
        readonly property Item tokens: listTokens
        Tokens { id: listTokens }
        // st.showList opens it without the tray (the warden's picture tests; showing it grants nothing)
        // Shown only once it sits on its monitor: a window that turns visible before its
        // screen and position follow gets its surface on the old monitor (layer-shell-qt
        // reads the monitor when the surface is made), and one that changes monitor while
        // shown is shown again there (otherwise the list could open far from its monitor).
        readonly property bool shown: (root.listOpen || !!root.st.showList) && !root.framesPart
        function place() {
            screen = home;
            x = home.virtualX;
            y = home.virtualY;
        }
        onShownChanged: {
            if (shown)
                place();
            visible = shown;
        }
        // By name: the state is read again four times a second, and each read hands back a
        // new screen object for the same monitor.
        readonly property string homeName: home ? home.name : ""
        onHomeNameChanged: if (visible) {
            visible = false;
            place();
            visible = true;
        }
        Component.onCompleted: {
            place();
            visible = shown;
        }
        // Above the tray: the primary monitor's bottom right, as the notice uses its top right.
        readonly property var home: {
            const screens = Qt.application.screens;
            for (let i = 0; i < screens.length; i++) {
                if (screens[i].name === root.st.primary)
                    return screens[i];
            }
            return screens[0];
        }
        color: "transparent"
        flags: Qt.FramelessWindowHint
        width: 420
        height: pop.height
        LayerShell.Window.scope: "kwin-mcp-warden-list"
        LayerShell.Window.layer: LayerShell.Window.LayerOverlay
        LayerShell.Window.anchors: LayerShell.Window.AnchorBottom | LayerShell.Window.AnchorRight
        LayerShell.Window.margins.right: 40
        LayerShell.Window.margins.bottom: 80
        LayerShell.Window.keyboardInteractivity: LayerShell.Window.KeyboardInteractivityOnDemand
        LayerShell.Window.exclusionZone: -1
        onActiveChanged: {
            if (active) {
                closeLater.stop();
                if (!root.listActiveAt)
                    root.listActiveAt = Date.now();
            } else if (visible && root.listActiveAt && Date.now() - root.listActiveAt > root.listSettleMs) {
                closeLater.start();
            }
        }

        property Timer closeLater: Timer { interval: 150; onTriggered: root.listOpen = false }

        Rectangle {
            id: pop
            width: 420
            height: col.implicitHeight + 8
            radius: 12
            color: listWin.tokens.pop
            border.width: 1
            border.color: listWin.tokens.popEdge
            focus: true
            Keys.onEscapePressed: root.listOpen = false

            Column {
                id: col
                width: parent.width
                // header: whose access ("Claude's access") and the count
                Item {
                    width: parent.width; height: 44
                    Text {
                        x: 20; anchors.verticalCenter: parent.verticalCenter
                        text: root.st.listTitle || "Access"
                        Accessible.role: Accessible.StaticText; Accessible.name: text
                        color: listWin.tokens.fg
                        font.family: listWin.tokens.sans; font.pixelSize: 14; font.weight: Font.DemiBold
                    }
                    Text {
                        anchors.right: parent.right; anchors.rightMargin: 20
                        anchors.verticalCenter: parent.verticalCenter
                        text: root.st.grants.length + (root.st.grants.length === 1 ? " grant" : " grants")
                        color: listWin.tokens.fgSubtle
                        font.family: listWin.tokens.sans; font.pixelSize: 12
                    }
                    Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: listWin.tokens.hair }
                }
                SectionHead { tokens: listWin.tokens; text: "Grants" }
                Text {
                    visible: !root.st.grants.length
                    x: 20; width: parent.width - 40
                    text: "No agent holds a grant right now."
                    Accessible.role: Accessible.StaticText; Accessible.name: text
                    color: listWin.tokens.fgSubtle
                    font.family: listWin.tokens.sans; font.pixelSize: 13
                    bottomPadding: 6
                }
                Repeater {
                    model: root.st.grants
                    delegate: Rectangle {
                        id: grantRow
                        required property var modelData
                        x: 8
                        width: col.width - 16
                        height: 56
                        radius: 10
                        color: rowArea.containsMouse ? listWin.tokens.panelHover : "transparent"
                        MouseArea { id: rowArea; anchors.fill: parent; hoverEnabled: true }
                        Kirigami.Icon {
                            x: 12; anchors.verticalCenter: parent.verticalCenter
                            width: 28; height: 28
                            source: grantRow.modelData.kind === "window" ? grantRow.modelData.icon
                                                                      : Qt.resolvedUrl("icons/desk.svg")
                            isMask: grantRow.modelData.kind !== "window"
                            color: listWin.tokens.fgMuted
                        }
                        Column {
                            x: 12 + 28 + 12; anchors.verticalCenter: parent.verticalCenter
                            width: parent.width - x - endButton.width - 24
                            Text {
                                width: parent.width; elide: Text.ElideRight
                                text: grantRow.modelData.kind === "window" ? grantRow.modelData.label
                                      : grantRow.modelData.kind === "full_desktop" ? "Your whole desktop" : "A screen of its own"
                                color: listWin.tokens.fg
                                font.family: listWin.tokens.sans; font.pixelSize: 14; font.weight: Font.Medium
                            }
                            Text {
                                width: parent.width; elide: Text.ElideRight
                                textFormat: Text.StyledText
                                text: (grantRow.modelData.who || grantRow.modelData.session) + " · <font color='"
                                      + listWin.tokens.levelColor(grantRow.modelData.level) + "'>"
                                      + grantRow.modelData.levelName + "</font> · "
                                      + grantRow.modelData.left
                                Accessible.role: Accessible.StaticText
                                Accessible.name: (grantRow.modelData.who || grantRow.modelData.session) + " · " + grantRow.modelData.levelName + " · " + grantRow.modelData.left
                                color: listWin.tokens.fgSubtle
                                font.family: listWin.tokens.sans; font.pixelSize: 12
                            }
                        }
                        WardenButton {
                            id: endButton
                            anchors.right: parent.right; anchors.rightMargin: 12
                            anchors.verticalCenter: parent.verticalCenter
                            tokens: listWin.tokens
                            label: "End"
                            onClicked: root.send("END " + grantRow.modelData.id)
                        }
                    }
                }
                // The accessibility switch the warden turned on (design/mock/index.html,
                // "Accessibility"): it goes back off with the last grant, or here.
                Rectangle {
                    id: a11yRow
                    visible: !!(root.st.a11y && root.st.a11y.ours)
                    x: 8
                    width: col.width - 16
                    height: visible ? Math.max(56, a11yText.implicitHeight + 20) : 0
                    radius: 10
                    color: a11yArea.containsMouse ? listWin.tokens.panelHover : "transparent"
                    MouseArea { id: a11yArea; anchors.fill: parent; hoverEnabled: true }
                    Kirigami.Icon {
                        x: 12; anchors.verticalCenter: parent.verticalCenter
                        width: 28; height: 28
                        source: Qt.resolvedUrl("icons/a11y.svg")
                        isMask: true
                        color: listWin.tokens.fgMuted
                    }
                    Column {
                        id: a11yText
                        x: 12 + 28 + 12; anchors.verticalCenter: parent.verticalCenter
                        width: parent.width - x - offButton.width - 24
                        Text {
                            width: parent.width; elide: Text.ElideRight
                            text: "Accessibility"
                            color: listWin.tokens.fg
                            font.family: listWin.tokens.sans; font.pixelSize: 14; font.weight: Font.Medium
                        }
                        Text {
                            width: parent.width; wrapMode: Text.WordWrap
                            text: "On for " + (root.st.who || "the agents") + " · turns off when the last grant ends"
                            Accessible.role: Accessible.StaticText; Accessible.name: text
                            color: listWin.tokens.fgSubtle
                            font.family: listWin.tokens.sans; font.pixelSize: 12
                        }
                    }
                    WardenButton {
                        id: offButton
                        anchors.right: parent.right; anchors.rightMargin: 12
                        anchors.verticalCenter: parent.verticalCenter
                        tokens: listWin.tokens
                        label: "Turn Off"
                        onClicked: root.send("A11Y-OFF")
                    }
                }
                SectionHead { tokens: listWin.tokens; text: "Latest" }
                Repeater {
                    model: root.st.log
                    delegate: Row {
                        required property string modelData
                        readonly property var parts: modelData.split("  ")
                        x: 20
                        spacing: 8
                        Text {
                            width: 64
                            text: (parts[0] || "").split(" ")[1] || ""
                            color: listWin.tokens.fgSubtle
                            font.family: listWin.tokens.mono; font.pixelSize: 12
                            lineHeight: 20; lineHeightMode: Text.FixedHeight
                        }
                        Text {
                            width: 420 - 40 - 72
                            elide: Text.ElideRight
                            text: (parts[1] || "") + ": " + parts.slice(3).join(" ")
                                  + (parts[2] === "did" ? "" : ", " + (parts[2] || ""))
                            color: /refused|never|denied/.test(parts[2] || "") ? listWin.tokens.error : listWin.tokens.fgMuted
                            font.family: listWin.tokens.sans; font.pixelSize: 12
                            lineHeight: 20; lineHeightMode: Text.FixedHeight
                        }
                    }
                }
                Item { width: 1; height: 6 }
                Rectangle { x: 0; width: parent.width; height: 1; color: listWin.tokens.hair }
                Item {
                    width: parent.width; height: 44
                    WardenButton {
                        x: 12; anchors.verticalCenter: parent.verticalCenter; anchors.verticalCenterOffset: 4
                        tokens: listWin.tokens; kind: "ghost"; label: "Open Log"
                        onClicked: root.send("LOG")
                    }
                    WardenButton {
                        anchors.right: parent.right; anchors.rightMargin: 12
                        anchors.verticalCenter: parent.verticalCenter; anchors.verticalCenterOffset: 4
                        tokens: listWin.tokens; icon: "stop"; label: "End All"; hint: root.st.stopKey || ""
                        onClicked: { root.send("STOP"); root.listOpen = false; }
                    }
                }
            }
        }
    }

    component SectionHead: Text {
        required property Item tokens
        x: 20
        topPadding: 14; bottomPadding: 8
        font.family: tokens.sans; font.pixelSize: 11; font.weight: Font.DemiBold
        font.letterSpacing: 0.9; font.capitalization: Font.AllUppercase
        color: tokens.fgSubtle
    }

    component WardenButton: Rectangle {
        id: button
        required property Item tokens
        property string kind: "outline"
        property string label: ""
        property string icon: ""
        property string hint: ""
        property bool large: false
        signal clicked()
        // A real button to a screen reader. Every button of the overlay only takes access
        // away (Stop, End, Turn Off) or opens the log; the card, which grants, is card.qml.
        Accessible.role: Accessible.Button
        Accessible.name: label || hint
        Accessible.onPressAction: clicked()
        width: inner.implicitWidth + (large ? 28 : 20)
        height: large ? 32 : 28
        radius: large ? 7 : 6
        color: kind === "ghost" ? (area.containsMouse ? tokens.panelHover : "transparent")
                                : (area.containsMouse ? tokens.panelHover : tokens.tint)
        border.width: kind === "ghost" ? 0 : 1
        border.color: tokens.hair
        Row {
            id: inner
            anchors.centerIn: parent
            spacing: 6
            Kirigami.Icon {
                visible: !!button.icon
                anchors.verticalCenter: parent.verticalCenter
                width: 14; height: 14
                source: button.icon ? Qt.resolvedUrl("icons/" + button.icon + ".svg") : ""
                isMask: true
                color: button.tokens.fg
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: button.label
                color: button.kind === "ghost" && !area.containsMouse ? button.tokens.fgSubtle : button.tokens.fg
                font.family: button.tokens.sans; font.pixelSize: button.large ? 13 : 12; font.weight: Font.Medium
            }
            Text {
                visible: !!button.hint
                anchors.verticalCenter: parent.verticalCenter
                text: button.hint
                color: button.tokens.fg
                opacity: 0.8
                font.family: button.tokens.mono; font.pixelSize: 11; font.weight: Font.Medium
            }
        }
        MouseArea {
            id: area
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: button.clicked()
        }
    }

    // Theme tokens, as the card takes them; one inside each window, where Kirigami can see the theme.
    component Tokens: Item {
        Kirigami.Theme.colorSet: Kirigami.Theme.Window
        Kirigami.Theme.inherit: false
        readonly property color pop: Kirigami.Theme.backgroundColor
        readonly property color fg: Kirigami.Theme.textColor
        readonly property color fgMuted: Kirigami.Theme.disabledTextColor
        readonly property color fgSubtle: Kirigami.ColorUtils.tintWithAlpha(pop, fgMuted, 0.8)
        readonly property color bgLift: Kirigami.ColorUtils.tintWithAlpha(pop, fg, 0.08)
        readonly property color panelHover: Kirigami.ColorUtils.tintWithAlpha(pop, fg, 0.04)
        readonly property color hair: Qt.rgba(fg.r, fg.g, fg.b, 0.1)
        readonly property color popEdge: Qt.rgba(fg.r, fg.g, fg.b, 0.2)
        readonly property color tint: Qt.rgba(fg.r, fg.g, fg.b, 0.08)
        readonly property color error: Kirigami.Theme.negativeTextColor
        readonly property color claude: "#d77757"
        readonly property string sans: Kirigami.Theme.defaultFont.family
        readonly property string mono: Kirigami.Theme.fixedWidthFont.family
        function levelColor(level) {
            return level === "full_desktop" ? error : level === "control" ? claude : fgMuted;
        }
    }
}
