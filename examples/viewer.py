# SPDX-License-Identifier: MIT
"""Minimal read-only Nomad Link viewer, and a reference for third-party bridges.

Usage: python3 viewer.py [host] [port] [pair_token]

Connects to Nomad, pairs (accept the connection request in Nomad's Link menu
the first time), then prints every scene message it receives. See PROTOCOL.md.
"""
import sys
import time

import transport

# honest hello (PROTOCOL.md §4): no selection/scene_transfer — this client never
# answers request_*; scene_edits + display_config so Nomad sends the live traffic
CAPABILITIES = ["scene_edits", "object_state", "material", "light", "camera_object",
                "session_config", "display_config"]


def describe(header, binary):
    kind = header.get("type", "?")
    if kind == "mesh_full":
        return f"mesh_full      {header.get('name')!r} vertices={header.get('vertex_count')} faces={header.get('face_count')} payload={len(binary)}B"
    if kind == "mesh_delta":
        return f"mesh_delta     mesh={header.get('mesh_id', '')[:8]} vertices={header.get('count')} payload={len(binary)}B"
    if kind == "mesh_attributes":
        return f"mesh_attributes mesh={header.get('mesh_id', '')[:8]} payload={len(binary)}B"
    if kind == "mesh_instance":
        return f"mesh_instance  {header.get('name')!r} geometry={header.get('geometry_id', '')[:8]}"
    if kind == "object_state":
        return f"object_state   {header.get('name')!r}"
    if kind in {"material", "light", "camera_object", "object_delete"}:
        return f"{kind:<14} {header.get('name', header.get('mesh_id', header.get('link_id', '')))!r}"
    if kind == "camera":
        return f"camera         fov={header.get('fov_y', 0):.1f}"
    if kind == "display_config":
        return f"display_config {len(header.get('display', {}))} settings"
    return kind


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else None
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 48312
    token = sys.argv[3] if len(sys.argv) > 3 else ""

    if host is None:
        print("searching for Nomad (broadcast + Bonjour)...")
        found = transport.discover(port, timeout=2.0)
        if not found:
            print("no Nomad found; pass a host explicitly")
            return 1
        host, port = found
        print(f"found Nomad at {host}:{port}")

    connection = transport.Connection(client_name="Viewer Example", capabilities=CAPABILITIES)
    connection.connect(host, port, token, transport.VERSION, 1)
    cameras = 0
    try:
        while True:
            if connection.status in {"Error", "Disconnected"}:
                print(f"connection closed: {connection.error or 'bye'}")
                return 0
            for header, binary in connection.poll():
                kind = header.get("type")
                if kind == "pairing_pending":
                    print("waiting for approval: accept the connection request in Nomad")
                elif kind == "hello":
                    print(f"connected to Nomad {header.get('nomad_version')}")
                    granted = header.get("pair_token")
                    if granted:
                        print(f"pair token (pass as 3rd argument to skip approval): {granted}")
                elif kind == "error":
                    print(f"nomad error: {header.get('message')}")
                elif kind == "camera":
                    cameras += 1  # too chatty to log each one
                    if cameras % 60 == 1:
                        print(describe(header, binary))
                elif kind not in {"session_config", "pong"}:
                    print(describe(header, binary))
            time.sleep(0.02)
    except KeyboardInterrupt:
        connection.disconnect()
        return 0


if __name__ == "__main__":
    sys.exit(main())
