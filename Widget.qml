// Envelope with the inbox unread count; click opens the mail client. The client, `mail sort` and
// every `mail` command that changes mail rewrite the cached count, and the bar watches that file, so
// it changes the moment they do; `mail unread` polled every two minutes covers the client being closed.

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "petrzpav.mail"

  readonly property string script: Qt.resolvedUrl("bin/mail").toString().replace(/^file:\/\//, "")
  readonly property bool hideWhenRead: setting("hideWhenRead", false) === true
  property int unread: 0
  property bool failed: false

  visible: !(hideWhenRead && unread === 0 && !failed)
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function refresh(cached) {
    if (countProc.running) return
    countProc.command = cached ? [script, "unread", "--cached"] : [script, "unread"]
    countProc.running = true
  }

  function open() {
    if (root.bar) root.bar.run(script + "-window")
  }

  IpcHandler {
    target: "petrzpav.mail"
    function refresh(): void { root.broadcast("refresh") }
  }

  Process {
    id: countProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var n = parseInt(text.trim())
        root.failed = isNaN(n)
        if (!isNaN(n)) root.unread = n
      }
    }
  }

  FileView {
    path: (Quickshell.env("XDG_CACHE_HOME") || Quickshell.env("HOME") + "/.cache") + "/petrzpav-mail/unread"
    watchChanges: true
    printErrors: false
    // text() is stale inside the change signal, so both paths go through reload.
    onFileChanged: reload()
    onLoaded: {
      var n = parseInt(text().trim())
      if (!isNaN(n)) {        // a half-written file reads empty: keep the last count
        root.unread = n
        root.failed = false
      }
    }
  }

  Timer {
    interval: 120000
    running: true
    repeat: true
    triggeredOnStart: false
    onTriggered: root.refresh(false)
  }

  Component.onCompleted: { refresh(true); refresh(false) }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.failed ? "󰇮 !" : (root.unread > 0 ? "󰇮 " + root.unread : "󰇮")
    active: root.unread > 0
    fontSize: Style.font.caption
    horizontalMargin: 6
    tooltipText: root.failed ? "Mail: cannot reach Gmail" : (root.unread > 0 ? root.unread + " unread in Inbox" : "Inbox read")
    onPressed: root.open()
  }
}
