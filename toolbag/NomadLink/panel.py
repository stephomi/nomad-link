# SPDX-License-Identifier: MIT
"""The Nomad Link window inside Toolbag.

Toolbag's UI is retained-mode, so the controls are built once and their labels
are refreshed from the client every periodic tick. Constructors are called
through `_make` because a Toolbag build that wants no constructor argument
should cost a default label, not the panel.
"""
try:
    import mset
except ImportError:  # unit tests inject a stand-in
    mset = None

import client

# the diagnostic row: how often Toolbag calls back, and what a mesh costs once it
# does. Off = the row is never built (a Toolbag control cannot be hidden later)
TIMING = True


def _make(class_name, *args):
    factory = getattr(mset, class_name, None)
    if factory is None:
        return None
    try:
        return factory(*args)
    except Exception:
        try:
            return factory()
        except Exception:
            return None


class Panel:
    def __init__(self, link, folder=""):
        self.link = link
        self.folder = folder
        self._host_edited = False   # False = nothing typed since we last looked
        self._typed = ""            # the address as typed, before the field mangles it
        self.window = _make("UIWindow", "Nomad Link")
        self.status = _make("UILabel", "Disconnected")
        self.detail = _make("UILabel", "")
        self.timing = _make("UILabel", "") if TIMING else None
        self.host = _make("UITextField", "")
        self.port = _make("UITextFieldInt", 48312)
        self.find_button = _make("UIButton", "Find Nomad")
        # one button, not two: a Toolbag UIControl cannot be hidden or disabled,
        # but its text is writable, so the label follows the connection
        self.connect_button = _make("UIButton", "Connect")
        # the Blender panel's transfer row, minus Send: geometry travels one way
        self.get_button = _make("UIButton", "Get scene")
        self.selection_button = _make("UIButton", "Get selection")
        self.replace_button = _make("UIButton", "Replace all")
        self.follow = _make("UICheckBox", "Follow Nomad's view")
        self._build()

    def _build(self):
        if self.window is None:
            return
        # the address the last session used, so this is usually ready to Connect
        host, port = client.saved_address()
        if self.port is not None:
            self.port.value = port
            self.port.width = 60
        if self.host is not None:
            self.host.value = host
            self.host.width = 110
            self.host.onChange = self.on_host_edited
            self._host_edited = False   # False = the field still holds what we wrote
        if self.find_button is not None:
            self.find_button.onClick = self.on_find
        if self.connect_button is not None:
            self.connect_button.onClick = self.on_toggle
        for button, action in ((self.get_button, self.on_get),
                               (self.selection_button, self.on_get_selection),
                               (self.replace_button, self.on_replace)):
            if button is not None:
                button.onClick = action
        if self.follow is not None:
            self.follow.value = False
            self.follow.onChange = self.on_follow

        probe_button = _make("UIButton", "Run probe")
        if probe_button is not None:
            probe_button.onClick = self.on_probe
        close = _make("UIButton", "Close")
        if close is not None:
            close.onClick = self.on_close

        for row in (
            [_make("UILabel", "Nomad address")],
            [self.host, self.port, self.find_button],
            [self.connect_button],
            [self.get_button, self.selection_button, self.replace_button],
            [self.status],
            [self.detail],
            [self.timing],
            [self.follow],
            [probe_button, close],
        ):
            present = [element for element in row if element is not None]
            if not present:
                continue        # a row this Toolbag build has nothing for
            for element in present:
                self.window.addElement(element)
            self.window.addReturn()

    # ----------------------------------------------------------------- actions

    def on_find(self):
        if self.link.find(self._port()):
            self._host_edited = False   # Find picked the address, not the field
        self.refresh_host()

    def on_toggle(self):
        """The same button disconnects, including while a retry is pending."""
        if self.link.wanted:
            self.link.disconnect()
        else:
            self.on_connect()

    def on_host_edited(self):
        """Keep what is typed as it is typed: by the time a button is clicked the
        field has reformatted its own text through a float ("192.2")."""
        typed = str(self.host.value if self.host is not None else "").strip()
        if typed:
            self._host_edited = True
            self._typed = typed

    def on_connect(self):
        host = self._typed if self._host_edited else self.link.host
        self.link.connect(host, self._port())
        self.refresh_host()

    def _port(self):
        try:
            return int(self.port.value) if self.port is not None else 48312
        except (TypeError, ValueError):
            return 48312

    def on_get(self):
        self.link.request("scene")

    def on_get_selection(self):
        self.link.request("selection")

    def on_replace(self):
        """Everything the bridge put here goes, then Nomad's scene comes back."""
        self.link.request("scene", replace=True)

    def refresh_host(self):
        """Only the port: writing the host back would let Toolbag's text field
        reformat it through a float ("192.168.1.16" -> "192.2"). The address in
        charge is always in the status line instead."""
        if self.port is not None and self.link.port:
            self.port.value = int(self.link.port)

    def on_follow(self):
        """Shared with Nomad and every other bridge, so this asks rather than sets."""
        self.link.set_sync_view(bool(self.follow.value) if self.follow else False)

    def on_probe(self):
        """Report what this Toolbag build actually does. Console has the detail."""
        import probe
        try:
            probe.run(self.folder)
            self.link.note("probe done: see the console and nomad_probe_report.txt")
        except Exception as exc:
            self.link.note("probe failed: %s" % exc)

    def on_close(self):
        self.shutdown()
        try:
            mset.shutdownPlugin()
        except Exception:
            pass

    # ------------------------------------------------------------------ ticking

    def update(self):
        """Toolbag's periodic callback: drain the socket, then repaint labels."""
        self.link.pump()
        if self.status is not None:
            self.status.text = self.link.message
        if self.detail is not None:
            self.detail.text = self._detail()
        if self.timing is not None:
            self.timing.text = self._timing()
        if self.connect_button is not None:
            self.connect_button.text = "Disconnect" if self.link.wanted else "Connect"
        # the flag is shared: Nomad or another bridge can move it under us. Write it
        # back only when it really differs, so a value write cannot echo as an edit
        if self.follow is not None and bool(self.follow.value) != self.link.scene.follow_view:
            self.follow.value = self.link.scene.follow_view

    def _timing(self):
        """Callback rate first: it is the one number the Toolbag docs never give,
        and it decides whether a delay is the wait or the work."""
        stats = self.link.stats
        if not stats["rate"]:
            return "measuring..."
        return "%.1f ticks/s, %d%% busy, %d packets/s | last mesh: %.0f ms convert, %.0f ms write" % (
            stats["rate"], round(stats["busy"] * 100), stats["packets"],
            stats["convert"], stats["write"])

    def _detail(self):
        counts = self.link.counts
        if not self.link.connected:
            return self.link.log[-1] if self.link.log else ""
        source = self.link.session_config.get("source_name", "")
        live = "live" if self.link.session_config.get("live_sync") else "paused"
        return "%d meshes, %d updates - %s%s" % (
            counts["meshes"], counts["updates"], live,
            " from %s" % source if source else "")

    def shutdown(self):
        self.link.disconnect()
        try:
            mset.callbacks.onPeriodicUpdate = None
            mset.callbacks.onShutdownPlugin = None
        except Exception:
            pass
