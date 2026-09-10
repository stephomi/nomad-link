# SPDX-License-Identifier: MIT
"""Nomad Sculpt <-> Houdini link (see PROTOCOL.md in stephomi/nomad-link).

Everything the HDAs call lives here:

    import nomad_link
    nomad_link.connect()                 # discover and connect
    nomad_link.client().meshes           # decoded meshes, keyed by mesh_id
"""
from . import convert, transport  # noqa: F401  (importable without Houdini)
from .client import DEFAULT_PORT, PROTOCOL, client

try:
    from .nodes import (
        answer_request,
        connect_button,
        clear_button,
        cook_import,
        cook_in,
        cook_out,
        disconnect_button,
        enable_sync,
        get_scene,
        get_selection,
        mesh_menu,
        refresh_inputs,
        send_button,
        send_geometry,
        status_text,
        store_mesh_id,
    )
except ImportError:  # no hou: the codecs and the client still work
    pass

__all__ = [
    "DEFAULT_PORT", "PROTOCOL", "client", "connect", "disconnect",
    "connect_button", "disconnect_button", "get_scene", "get_selection", "enable_sync",
    "clear_button", "clear",
    "sync_all",
    "send_button", "send_geometry", "cook_in", "cook_out", "cook_import", "mesh_menu",
    "refresh_inputs", "status_text", "store_mesh_id", "answer_request",
    "watch", "report", "material", "display", "bindings", "ping", "advertise", "drop",
]


def connect(host="", port=DEFAULT_PORT):
    """Connect to Nomad; empty host discovers it (UDP broadcast + Bonjour)."""
    return client().connect(host, port)


def disconnect():
    client().disconnect()


def clear():
    """Forget the cached scene, textures and display settings."""
    client().clear()
    return "cache cleared"


def sync_all():
    """Ask Nomad to stream every channel: objects, lights, materials, cameras.

    Nomad ships with sync_lights and sync_materials off, so light and material
    edits only appear on an explicit Get Scene until this is set.
    """
    client().set_session(live_sync=True, sync_objects=True, sync_lights=True,
                         sync_materials=True, sync_cameras=True)
    return "asked Nomad to enable every sync channel"


def material(which=None):
    """Print the raw material block Nomad sent, to see what actually arrives.

    `which` is a mesh name or mesh_id; omit it to list what has materials.
    """
    link = client()
    by_name = {}
    for mesh_id, block in link.materials.items():
        mesh = link.meshes.get(mesh_id)
        by_name[mesh["name"] if mesh else mesh_id] = (mesh_id, block)
    if which is None:
        print("materials for: %s" % ", ".join(sorted(by_name)[:20]))
        print("call nomad_link.material('<name>') for one of them")
        return
    found = by_name.get(which) or (which, link.materials.get(which))
    mesh_id, block = found
    if not block:
        print("no material cached for %r" % which)
        return
    print("material for %s (%s)" % (which, mesh_id))
    for key in sorted(block):
        if key == "textures":
            for channel, values in sorted(block[key].items()):
                print("  texture.%-22s %s" % (channel, values))
        else:
            print("  %-30s %r" % (key, block[key]))


def display(kind="env"):
    """Print cached shading settings. `kind` filters by prefix, "" for all."""
    settings = client().display
    if not settings:
        print("no shading_config received yet -- press Get Scene while connected")
        return
    keys = sorted(key for key in settings if key.startswith(kind))
    if not keys:
        print("no %r keys; the full set is: %s" % (kind, ", ".join(sorted(settings))))
        return
    for key in keys:
        print("  %-28s %r" % (key, settings[key]))


def bindings():
    """Show which material block each mesh resolved to, and why.

    Link keys materials by mesh_id with no sharing, so a copy has to borrow the
    original's. This prints what that resolution decided.
    """
    from . import usd
    link = client()
    keys = usd.material_keys(link)
    for mesh_id in link.order:
        mesh = link.meshes.get(mesh_id)
        if mesh is None:
            continue
        key = keys.get(mesh_id)
        if key is None:
            reason = "no material"
        elif key == mesh_id:
            reason = "its own" if mesh_id in link.materials else "vertex paint only"
        elif key == mesh.get("material_source"):
            reason = "instance of %s" % (link.meshes.get(key, {}).get("name", key))
        else:
            reason = "shares geometry with %s" % (link.meshes.get(key, {}).get("name", key))
        print("  %-24s -> %-24s %s" % (
            mesh["name"][:24], (link.meshes.get(key, {}).get("name", key or "-"))[:24], reason))


def _module():
    """nomad_link.client is the accessor function, so reach the module explicitly."""
    import importlib
    return importlib.import_module("nomad_link.client")


def ping(seconds=None):
    """Set the keepalive interval, 0 to stop pinging. Applies immediately.

    Worth turning off entirely if a transfer stalls: it isolates whether our
    traffic is interfering with Nomad's sending.
    """
    _client_module = _module()
    if seconds is None:
        if not _client_module.PING_INTERVAL:
            return "off (no reference client sends one)"
        return "every %.2fs (%.2fs while a transfer is expected)" % (
            _client_module.PING_INTERVAL, _client_module.PING_INTERVAL_RECEIVING)
    _client_module.PING_INTERVAL = float(seconds)
    _client_module.PING_INTERVAL_RECEIVING = float(seconds)
    return "ping every %.2fs" % float(seconds) if seconds else "keepalive off"


def advertise(*names):
    """Replace what we advertise in the hello; call with nothing to restore.

    Reconnect for it to take effect -- capabilities are only sent at hello. Use
    it to bisect a misbehaving peer: drop a capability and see what changes.
    """
    _client_module = _module()
    link = client()
    link.connection.capabilities = list(names) if names else list(_client_module.CAPABILITIES)
    return "advertising: %s" % ", ".join(sorted(link.connection.capabilities))


def drop(*names):
    """Stop advertising these capabilities. Reconnect to apply."""
    link = client()
    link.connection.capabilities = [c for c in link.connection.capabilities
                                    if c not in names]
    return "advertising: %s" % ", ".join(sorted(link.connection.capabilities))


def watch(enable=True):
    """Log every message Nomad sends, so `report()` can show what actually arrived."""
    client().verbose = enable
    return "logging every message" if enable else "logging errors only"


def report(lines=40, meshes=12):
    """Print what the link is doing. Paste this when something is not syncing."""
    link = client()
    print("status      : %s - %s" % (link.status, link.message))
    print("nomad       : %s at %s:%s" % (link.nomad_version, link.host, link.port))
    print("nomad can   : %s" % ", ".join(sorted(link.peer_capabilities)))
    config = {key: value for key, value in link.session_config.items()
              if key not in ("type", "peers", "source_name")}
    print("session     : %s" % config)
    peers = link.session_config.get("peers") or []
    # another client shares Nomad's sender: a stalled one can hold up everybody,
    # and a dropped session can linger in this list
    print("other peers : %s" % (", ".join(peers) if peers else "none"))
    print("cached      : %d meshes, %d materials, %d lights, %d cameras, %d textures"
          % (len(link.meshes), len(link.materials), len(link.lights),
             len(link.cameras), len(link.textures)))
    channels = ("color", "alpha", "rough", "metallic", "mask", "density",
                "texcoords", "face_group")
    shared = {}
    for mesh_id in link.order:
        mesh = link.meshes.get(mesh_id)
        if mesh is not None:
            shared.setdefault(mesh.get("geometry_id", mesh_id), []).append(mesh)
    reused = sum(len(group) for group in shared.values() if len(group) > 1)
    print("geometry    : %d unique, %d meshes reuse a shared geometry (instances)"
          % (len(shared), reused))
    for index, mesh_id in enumerate(link.order):
        mesh = link.meshes.get(mesh_id)
        if mesh is None:
            continue
        if index == meshes and len(link.order) > meshes:
            print("  ... and %d more meshes" % (len(link.order) - meshes))
            break
        if index >= meshes:
            break
        layers = mesh.get("layers") or []
        note = ""
        if layers:
            note = "  [%d layers, %d applied: %s]" % (
                len(layers), mesh.get("layers_applied", 0),
                ", ".join("%s x%.2f%s" % (layer["name"], layer["weight"],
                                          "" if layer["visible"] else " off")
                          for layer in layers[:4]))
        print("  mesh     %-24s %6d pts  %-7s  %s%s" % (
            mesh["name"][:24], len(mesh["positions"]),
            "visible" if mesh.get("visible", True) else "HIDDEN",
            " ".join(key for key in channels if key in mesh) or "positions only", note))
    for name, store in (("lights", link.lights), ("cameras", link.cameras)):
        for link_id, entry in store.items():
            print("  %-8s %-24s %s" % (name[:-1], entry.get("name", "?"), link_id))
    stats = link.stats
    span = (stats["last"] - stats["first"]) or 0.0
    print("traffic     : %d messages, %.1f MB in %.1fs%s"
          % (stats["messages"], stats["bytes"] / 1048576.0, span,
             " (%.1f MB/s)" % (stats["bytes"] / 1048576.0 / span) if span > 0.01 else ""))
    # a long gap between pumps means Houdini was busy, not that the link was idle:
    # that distinguishes a slow network from a starved main thread
    import time as _time
    connection = link.connection
    recent = getattr(connection, "rx_recent", [])
    window = [n for when, n in recent if _time.monotonic() - when < 5.0]
    since = _time.monotonic() - connection.rx_last if getattr(connection, "rx_last", 0) else -1
    print("socket      : %.1f MB read, %.0f KB in the last 5s, last byte %s"
          % (getattr(connection, "rx_bytes", 0) / 1048576.0, sum(window) / 1024.0,
             ("%.1fs ago" % since) if since >= 0 else "never"))
    print("pump        : %d calls, worst gap %.2fs, worst single pump %.2fs"
          % (stats["pumps"], stats["worst_gap"], stats["worst_pump"]))
    print("last rebuild: %.2fs (waits %.1fs of quiet before rebuilding again)"
          % (link.last_author, link._quiet_for()))
    print("keepalive   : %s" % ping())
    print("recent      :")
    for line in link.log[-lines:]:
        print("  " + line)
