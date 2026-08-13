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
        cook_in,
        cook_out,
        disconnect_button,
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
    "connect_button", "disconnect_button", "get_scene", "get_selection",
    "send_button", "send_geometry", "cook_in", "cook_out", "mesh_menu",
    "refresh_inputs", "status_text", "store_mesh_id", "answer_request",
]


def connect(host="", port=DEFAULT_PORT):
    """Connect to Nomad; empty host discovers it (UDP broadcast + Bonjour)."""
    return client().connect(host, port)


def disconnect():
    client().disconnect()
