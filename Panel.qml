import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui

// Keybloom in the bar: the icon shows whether the keyboard is connected, the popup sets its
// lighting. keybloom.py runs for as long as the widget is loaded and holds the keyboard open; this
// file sends it commands, shows its status and keeps your settings inline on Keybloom's shell.json
// entry (the helper reports them, this writes them). Nothing here writes the keyboard's flash
// except the "Store on keyboard" button.
Panel {
  id: root
  moduleName: "io.github.schmittcn.keybloom"
  ipcTarget: "io.github.schmittcn.keybloom"
  // own IpcHandler below (one per target): the base's calls plus next/prev
  manageIpc: false

  readonly property string tool: Qt.resolvedUrl("keybloom.py").toString().replace(/^file:\/\//, "")
  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property string fontName: bar ? bar.fontFamily : Style.font.family

  property var status: ({})
  readonly property var st: status.state || {}
  readonly property bool connected: !!status.connected
  readonly property bool usable: connected && !status.blocked   // blocked: Keybloom sends this keyboard nothing
  // the full story when something is wrong: why no keyboard, or what is off with this one
  readonly property string problem: !connected ? (status.error || "") : (status.warning || "")
  readonly property var effects: status.effects || []
  readonly property var typingEffects: status.typingEffects || []
  readonly property var colorEffects: status.colorEffects || []
  readonly property bool usesColor: colorEffects.indexOf(st.effect) >= 0
  // the keyboard's look on the bar icon: its color (cycling with Rainbow), plain when the effect brings
  // its own colors, dimmed when the lights are off
  readonly property bool lightsOff: !usable || st.effect === 0 || st.brightness === 0
    || (!!status.idle && st.idleSec > 0 && !st.music)
  property real rainbowHue: 0
  readonly property color keyColor: lightsOff || !usesColor ? fg
    : Qt.hsva(st.colorMode === "rainbow" ? rainbowHue : (st.hue || 0) / 255, (st.sat || 0) / 255, 1, 1)
  readonly property string effectName: effects[st.effect] || ("Effect " + st.effect)
  readonly property string statusText: !connected ? (status.error ? "Not connected" : "Starting…")
    : status.blocked ? "Not supported"
    : status.error ? status.error
    : status.idle && st.idleSec > 0 && !st.music ? "Idle · lights off"
    : [effectName, usesColor && st.colorMode === "theme" ? "theme color" : usesColor && st.colorMode === "rainbow" ? "rainbow" : "",
       st.music ? "music" : ""].filter(function(s) { return s !== "" }).join(" · ")

  function send(cmd) { if (helper.running) helper.write(JSON.stringify(cmd) + "\n") }

  // ---------- Settings: inline on Keybloom's shell.json entry ----------
  readonly property var settingKeys: ["effect", "speed", "brightness", "hue", "sat", "colorMode", "music",
    "musicTyping", "idleSec", "storedOnKeyboard", "boards"]
  property string persistedCanon: ""   // the settings as shell.json holds them, canonical JSON

  function pick(entry) {
    var mine = {}
    for (var i = 0; i < settingKeys.length; i++)
      if (entry && entry[settingKeys[i]] !== undefined) mine[settingKeys[i]] = entry[settingKeys[i]]
    return mine
  }
  // JSON with sorted keys: the same settings compare equal whatever order they come in
  function canon(v) {
    if (Array.isArray(v)) return "[" + v.map(canon).join(",") + "]"
    if (v && typeof v === "object")
      return "{" + Object.keys(v).sort().map(function(k) { return JSON.stringify(k) + ":" + canon(v[k]) }).join(",") + "}"
    return JSON.stringify(v)
  }
  function sendInit() {
    var mine = pick(root.settings)
    persistedCanon = canon(mine)
    send({ init: { settings: mine, boards: mine.boards } })
  }
  // Same as the shell's own Clock: keep the entry's other fields, replace ours, write through the shell's
  // scoped API (it can only touch this plugin's entry). Called only when the helper's settings changed:
  // a slider release, a toggle, a step; never a drag, an animation frame or a theme switch.
  function persist(values) {
    var entry = {}
    for (var k in root.settings) if (k !== "id" && settingKeys.indexOf(k) < 0) entry[k] = root.settings[k]
    for (var v in values) entry[v] = values[v]
    persistedCanon = canon(values)
    root.settings = entry
    if (!root.bar || !root.bar.shell || typeof root.bar.shell.updateEntryInline !== "function") {
      console.log("[keybloom] cannot write shell.json: settings last for this session only")
      return
    }
    root.bar.shell.updateEntryInline(root.moduleName, entry)
  }
  // shell.json edited by hand (or omarchy bar set): hand the new values to the helper
  onSettingsChanged: if (helper.running && canon(pick(root.settings)) !== persistedCanon) sendInit()
  function set(values) { send({ set: values }) }

  onOpenedChanged: if (opened) send({ read: true })   // pick up changes made with the Fn keys

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Process {
    id: helper
    command: ["/usr/bin/python3", "-I", root.tool, "serve"]
    running: true
    stdinEnabled: true
    onStarted: root.sendInit()   // the helper touches the keyboard only after this
    stdout: SplitParser {
      onRead: function(line) {
        try { root.status = JSON.parse(line) } catch (e) { return }
        if (root.status.settings && root.canon(root.status.settings) !== root.persistedCanon) root.persist(root.status.settings)
      }
    }
    stderr: SplitParser { onRead: function(line) { console.log("[keybloom] " + String(line).slice(0, 512)) } }
    onExited: function(code) {
      root.status = { error: "keybloom.py stopped (" + code + "), restarting…" }
      restart.start()
    }
  }
  // same speed as the helper's rainbow: one round in 30 s .. 3 s
  Timer {
    interval: 100
    repeat: true
    running: root.st.colorMode === "rainbow" && root.usesColor && !root.lightsOff
    onTriggered: root.rainbowHue = (Date.now() / 1000 / (30 - (root.st.speed || 0) / 255 * 27)) % 1
  }

  // ponytail: fixed 3 s retry, no backoff; a helper that crashes on start retries forever but quietly
  Timer { id: restart; interval: 3000; onTriggered: helper.running = true }

  // the theme switch has already swapped in the new theme folder when the accent changes
  Connections {
    target: Color
    function onAccentChanged() { root.send({ theme: true }) }
  }

  IdleMonitor {
    enabled: root.st.idleSec > 0
    timeout: Math.max(5, root.st.idleSec || 0)
    respectInhibitors: true
    onIsIdleChanged: root.send({ idle: isIdle })
  }

  IpcHandler {
    target: "io.github.schmittcn.keybloom"

    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    // omarchy-shell io.github.schmittcn.keybloom next|prev: bind them to keys to cycle effects
    function next(): void { root.send({ step: 1 }) }
    function prev(): void { root.send({ step: -1 }) }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰌌"
    slotSize: Style.bar.statusSlot
    dimmed: root.lightsOff
    foreground: root.keyColor
    tooltipText: root.opened ? "" : "Keybloom: " + root.statusText
    onPressed: function(b) { root.toggle() }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keys
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keys
      anchors.fill: parent
      onMoveRequested: function(dx, dy) { if (dx !== 0) root.send({ step: dx }) }
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(14)

        // ---------- Hero: icon · name / status ----------
        Item {
          width: parent.width
          implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight)

          Text {
            id: heroIcon
            text: "󰌌"
            color: root.keyColor
            opacity: root.lightsOff ? 0.5 : 1
            font.family: root.fontName
            font.pixelSize: Style.font.display
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
          }

          Column {
            id: heroLabels
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(14)
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {
              textFormat: Text.PlainText
              text: "Keybloom"
              color: root.fg
              font.family: root.fontName
              font.pixelSize: Style.font.title
              font.bold: true
            }
            Text {
              textFormat: Text.PlainText
              text: root.statusText.toUpperCase()
              color: Qt.darker(root.fg, 1.4)
              font.family: root.fontName
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              elide: Text.ElideRight
              width: parent.width
            }
          }
        }

        // ---------- What is wrong, in full (the status line above has room for a word or two) ----------
        Rectangle {
          visible: root.problem !== ""
          width: parent.width
          implicitHeight: problemText.implicitHeight + Style.space(16)
          radius: Style.cornerRadius
          color: Qt.rgba(Color.urgent.r, Color.urgent.g, Color.urgent.b, 0.15)
          border.width: 1
          border.color: Color.urgent

          Text {
            id: problemText
            anchors.fill: parent
            anchors.margins: Style.space(8)
            textFormat: Text.PlainText
            text: "󰀦  " + root.problem
            color: root.fg
            font.family: root.fontName
            font.pixelSize: Style.font.caption
            wrapMode: Text.Wrap
          }
        }

        PanelSeparator { foreground: root.fg }

        // ---------- Sync ----------
        Column {
          width: parent.width
          spacing: Style.space(6)
          enabled: root.usable
          opacity: enabled ? 1 : 0.4

          PanelSectionHeader { text: "SYNC"; foreground: root.fg; fontFamily: root.fontName }

          Toggle {
            width: parent.width
            label: "Music"
            description: root.typingEffects.indexOf(root.st.effect) < 0 ? "While something plays, the lights pulse up to your brightness; otherwise they stay as set"
              : root.st.musicTyping !== false ? "The key color shifts with what your speakers play (typing effects keep full brightness)"
              : "Pulses the other effects; this typing effect ignores the music (see below)"
            checked: !!root.st.music
            foreground: root.fg
            fontFamily: root.fontName
            onClicked: root.set({ music: !root.st.music })
          }
          Toggle {
            visible: !!root.st.music
            width: parent.width
            label: "Music on typing effects"
            description: root.st.musicTyping ? "Key color shifts with the beat; brightness stays full"
                                             : "Typing effects ignore the music; keys light up in your exact color"
            checked: root.st.musicTyping !== false
            foreground: root.fg
            fontFamily: root.fontName
            onClicked: root.set({ musicTyping: !(root.st.musicTyping !== false) })
          }
          // Off when idle: fixed stops, equally spaced, so 15 s is as easy to hit as 1 h; the far left is "never"
          Rectangle {
            id: idleBox
            readonly property var stops: [0, 10, 15, 30, 45, 60, 120, 180, 300, 600, 900, 1800, 2700, 3600]
            function label(sec) {
              return sec === 0 ? "Never" : sec < 60 ? sec + " s" : sec < 3600 ? sec / 60 + " min" : "1 h"
            }
            function nearest(sec) {   // a value from an older version may sit between stops
              var best = 0
              for (var i = 0; i < stops.length; i++) if (Math.abs(stops[i] - sec) < Math.abs(stops[best] - sec)) best = i
              return best
            }
            readonly property int liveSec: stops[Math.round(idleSlider.liveValue)]
            width: parent.width
            implicitHeight: idleColumn.implicitHeight + Style.space(20)
            radius: Style.cornerRadius
            color: "transparent"
            border.width: 1
            border.color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.25)

            Column {
              id: idleColumn
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.top: parent.top
              anchors.margins: Style.space(10)
              anchors.leftMargin: Style.space(12)
              anchors.rightMargin: Style.space(12)
              spacing: Style.space(4)

              Item {
                width: parent.width
                implicitHeight: idleTitle.implicitHeight
                Text {
                  id: idleTitle
                  text: "Off when idle"
                  color: root.fg
                  font.family: root.fontName
                  font.pixelSize: Style.font.subtitle
                  font.bold: true
                }
                Text {
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  text: idleBox.label(idleBox.liveSec)
                  color: root.fg
                  font.family: root.fontName
                  font.pixelSize: Style.font.bodySmall
                  font.bold: true
                }
              }
              Text {
                width: parent.width
                textFormat: Text.PlainText
                text: idleBox.liveSec === 0 ? "The lights stay on"
                  : "Lights out after " + idleBox.label(idleBox.liveSec) + " without input" + (root.st.music ? " and no music" : "") + ", back on your first key"
                color: root.fg
                opacity: 0.6
                font.family: root.fontName
                font.pixelSize: Style.font.caption
                wrapMode: Text.Wrap
              }
              PanelSlider {
                id: idleSlider
                bar: root.bar
                width: parent.width
                minimum: 0
                maximum: idleBox.stops.length - 1
                step: 1
                integer: true
                tickCount: idleBox.stops.length
                value: idleBox.nearest(root.st.idleSec || 0)
                onReleased: function(v) { root.set({ idleSec: idleBox.stops[Math.round(v)] }) }
              }
            }
          }
        }

        PanelSeparator { foreground: root.fg }

        // ---------- Effect ----------
        Column {
          width: parent.width
          spacing: Style.space(8)
          enabled: root.usable
          opacity: enabled ? 1 : 0.4

          PanelSectionHeader { text: "EFFECT"; foreground: root.fg; fontFamily: root.fontName }

          Item {
            width: parent.width
            implicitHeight: effectPick.implicitHeight

            ActionButton {
              id: prevButton
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              iconText: "󰒮"
              tooltipText: "Previous effect (←)"
              onClicked: root.send({ step: -1 })
            }
            Dropdown {
              id: effectPick
              anchors.left: prevButton.right
              anchors.right: nextButton.left
              anchors.leftMargin: Style.space(6)
              anchors.rightMargin: Style.space(6)
              showLabel: false
              foreground: root.fg
              fontFamily: root.fontName
              value: String(root.st.effect)
              // ⌨ marks the effects that react to typing
              options: root.effects.map(function(name, i) {
                return { value: String(i), label: name + (root.typingEffects.indexOf(i) >= 0 ? "  ⌨" : "") }
              })
              onChanged: function(v) { root.set({ effect: Number(v) }) }
            }
            ActionButton {
              id: nextButton
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              iconText: "󰒭"
              tooltipText: "Next effect (→)"
              onClicked: root.send({ step: 1 })
            }
          }

          // numbered effects: say how to name them (README "Adding your keyboard")
          Text {
            visible: root.connected && (root.status.effectsNote || !root.status.model)
            width: parent.width
            textFormat: Text.PlainText
            text: (root.status.effectsNote ? root.status.effectsNote + ". " : "")
              + (root.status.model ? "" : "Effect names for this keyboard are not known yet; you can add them in ~/.config/keybloom/effects.json (see the README).")
            color: root.fg
            opacity: 0.6
            font.family: root.fontName
            font.pixelSize: Style.font.caption
            wrapMode: Text.Wrap
          }

          LabeledSlider {
            label: "Speed"
            value: root.st.speed || 0
            key: "speed"
          }
          LabeledSlider {
            label: "Brightness"
            value: root.st.brightness || 0
            key: "brightness"
          }
        }

        PanelSeparator { foreground: root.fg }

        // ---------- Color ----------
        Column {
          width: parent.width
          spacing: Style.space(8)
          enabled: root.usable
          opacity: enabled ? 1 : 0.4

          Item {
            width: parent.width
            implicitHeight: colorHeader.implicitHeight
            PanelSectionHeader { id: colorHeader; text: "COLOR"; foreground: root.fg; fontFamily: root.fontName }
            Rectangle {
              visible: root.st.colorMode !== "rainbow"
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(28)
              height: Style.space(14)
              radius: height / 2
              color: Qt.hsva((root.st.hue || 0) / 255, (root.st.sat || 0) / 255, 1, 1)
            }
          }

          ButtonGroup {
            width: parent.width
            options: [
              { value: "theme", label: "Theme", tooltip: "The theme's keyboard color, follows every theme switch" },
              { value: "rainbow", label: "Rainbow", tooltip: "Runs through all colors; the Speed slider sets how fast" },
              { value: "own", label: "Own", tooltip: "The color you pick below" }
            ]
            value: root.st.colorMode || "own"
            foreground: root.fg
            fontFamily: root.fontName
            fontSize: Style.font.bodySmall
            onChanged: function(v) { root.set({ colorMode: v }) }
          }

          // a rainbow effect shows nothing of the color: say so instead of looking broken
          Text {
            visible: root.connected && !root.usesColor
            width: parent.width
            textFormat: Text.PlainText
            text: root.effectName + " brings its own colors. Solid color, Breathing, Band spiral and the Reactive effects show this one."
            color: root.fg
            opacity: 0.6
            font.family: root.fontName
            font.pixelSize: Style.font.caption
            wrapMode: Text.Wrap
          }

          // quick picks: hue/sat as the keyboard takes them (0..255)
          Flow {
            visible: root.st.colorMode === "own"
            width: parent.width
            spacing: Style.space(6)
            Repeater {
              model: [[0, 255], [21, 255], [43, 255], [85, 255], [128, 255], [170, 255], [191, 255], [213, 255], [0, 0]]
              Rectangle {
                required property var modelData
                width: Style.space(26)
                height: width
                radius: width / 2
                color: Qt.hsva(modelData[0] / 255, modelData[1] / 255, 1, 1)
                border.width: root.st.hue === modelData[0] && root.st.sat === modelData[1] ? 2 : 0
                border.color: root.fg
                MouseArea {
                  anchors.fill: parent
                  cursorShape: Qt.PointingHandCursor
                  onClicked: root.set({ hue: modelData[0], sat: modelData[1] })
                }
              }
            }
          }

          LabeledSlider {
            visible: root.st.colorMode === "own"
            label: "Hue"
            value: root.st.hue || 0
            key: "hue"
          }
          LabeledSlider {
            visible: root.st.colorMode !== "theme"
            label: "Saturation"
            value: root.st.sat || 0
            key: "sat"
          }
        }

        PanelSeparator { foreground: root.fg }

        // ---------- Footer: store · device ----------
        Item {
          width: parent.width
          implicitHeight: storeButton.implicitHeight

          ActionButton {
            id: storeButton
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: root.status.storedMatches ? "Stored on keyboard" : "Store on keyboard"
            iconText: "󰆓"
            tooltipText: "Writes these settings to the keyboard's memory (one flash write), so it shows them without\nKeybloom too. Not needed while Keybloom runs: it re-applies your settings to RAM on every replug."
            enabled: root.usable && !root.status.storedMatches && !root.status.idle
            onClicked: root.send({ store: true })
          }
          Text {
            anchors.right: parent.right
            anchors.left: storeButton.right
            anchors.leftMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            textFormat: Text.PlainText
            // an unknown keyboard: what README "Adding your keyboard" asks for, instead of the device path
            // the product id is what the udev rule and README "Adding your keyboard" ask for
            text: !root.connected ? "" : root.status.model ? "Keychron " + root.status.model + " · 3434:" + root.status.pid
              : "3434:" + root.status.pid + " · " + (root.effects.length - 1) + " effects"
            color: root.fg
            opacity: 0.45
            font.family: root.fontName
            font.pixelSize: Style.font.caption
            horizontalAlignment: Text.AlignRight
            elide: Text.ElideLeft
          }
        }
      }
    }
  }

  // A 0..255 slider for one setting: previews on the keyboard while dragging (at most 20 updates/s,
  // not written to the settings file), commits once on release.
  component LabeledSlider: Item {
    id: ls
    property string label: ""
    property string key: ""
    property int value: 0
    property int pending: 0

    function send(v, preview) {
      var values = {}
      values[ls.key] = v
      root.send({ set: values, preview: preview })
    }

    Timer { id: throttle; interval: 50; onTriggered: ls.send(ls.pending, true) }
    width: parent.width
    implicitHeight: Math.max(lsLabel.implicitHeight, slider.implicitHeight)

    Text {
      id: lsLabel
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(80)
      text: ls.label
      color: root.fg
      opacity: 0.7
      font.family: root.fontName
      font.pixelSize: Style.font.caption
    }
    PanelSlider {
      id: slider
      bar: root.bar
      anchors.left: lsLabel.right
      anchors.right: lsValue.left
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
      minimum: 0
      maximum: 255
      step: 1
      integer: true
      value: ls.value
      onMoved: function(v) { ls.pending = Math.round(v); if (!throttle.running) throttle.start() }
      onReleased: function(v) { throttle.stop(); ls.send(Math.round(v), false) }
    }
    Text {
      id: lsValue
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(28)
      horizontalAlignment: Text.AlignRight
      text: Math.round(slider.liveValue)
      color: root.fg
      font.family: root.fontName
      font.pixelSize: Style.font.caption
    }
  }

  component ActionButton: Button {
    opacity: enabled ? 1 : 0.4   // Button itself does not show that it is disabled
    fontSize: Style.font.bodySmall
    foreground: root.fg
    fontFamily: root.fontName
    bordered: true
  }
}
