# SPDX-License-Identifier: MIT
"""Nomad Link for Marmoset Toolbag.

Receives a live scene from Nomad Sculpt: geometry, vertex paint, UVs, materials,
textures and the environment. Run it from Toolbag's Plugin menu, connect, then
press Send in Nomad's Link menu.
"""
import os
import sys

import mset


def plugin_folder():
    """This folder. Toolbag execs plugins as a string, so __file__ may not exist."""
    path = globals().get("__file__") or ""
    if not path:
        try:
            path = mset.getPluginPath() or ""
        except Exception:
            path = ""
    if not path:
        return ""
    path = os.path.abspath(path)
    return path if os.path.isdir(path) else os.path.dirname(path)


def main():
    folder = plugin_folder()
    if folder and folder not in sys.path:
        sys.path.insert(0, folder)

    import client as client_module
    import panel as panel_module

    link = client_module.client()
    if folder:
        link.scene.trace_path = os.path.join(folder, "nomad_link_last.txt")
    ui = panel_module.Panel(link, folder)
    mset.callbacks.onPeriodicUpdate = ui.update
    mset.callbacks.onShutdownPlugin = ui.shutdown
    link.autoconnect()  # silent redial when a Nomad is already paired
    ui.refresh_host()


main()
