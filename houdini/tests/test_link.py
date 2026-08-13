# SPDX-License-Identifier: MIT
"""Drive the client against a mock Nomad: handshake, mesh_full, delta, instance,
object_state, and an outgoing mesh_full. Runs outside Houdini:

    python3 tests/test_link.py
"""
import importlib
import json
import os
import sys

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "python"))

from mock_nomad import MockNomad, wait as pump_until  # noqa: E402

from nomad_link import convert  # noqa: E402

# nomad_link.client is the accessor function, so reach the module explicitly
client_module = importlib.import_module("nomad_link.client")

PORT = 48399


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("ok  " + message)


def wait(predicate, seconds=3.0):
    return pump_until(link, predicate, seconds)


def cube_mesh_full(mesh_id="cube1", geometry_id="geo1"):
    points = numpy.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], "<f4")
    header, binary = convert.encode_mesh(
        mesh_id=mesh_id, geometry_id=geometry_id, name="Nomad Cube",
        positions=points, sizes=numpy.array([4], "i4"),
        corners=numpy.array([0, 1, 2, 3], "i4"),
        point_attribs={"color": numpy.tile([1.0, 0.0, 0.0], (4, 1))},
        ngon=True,
    )
    return header, binary


nomad = MockNomad(PORT)
nomad.start()

client_module._token_path = lambda: os.path.join(
    HERE, ".test_tokens.json")
link = client_module.Client()
link.connect("127.0.0.1", PORT)

check(wait(lambda: link.connected and link.nomad_version == "2.0"), "handshake completes")
check(nomad.hello["protocol"] == 1, "hello carries protocol 1")
check("ngon" in nomad.hello["capabilities"], "we advertise ngon")
check(link.peer_has("ngon"), "peer capabilities are recorded")
check(json.load(open(client_module._token_path()))["127.0.0.1"] == "token123",
      "pair token is stored for silent reconnects")

nomad.send(*cube_mesh_full())
check(wait(lambda: "cube1" in link.meshes), "mesh_full lands in the cache")
mesh = link.meshes["cube1"]
check(mesh["name"] == "Nomad Cube" and len(mesh["positions"]) == 4, "mesh decoded")
check(numpy.allclose(mesh["color"][0], [1, 0, 0], atol=0.01), "vertex colour decoded")

revision = link.revision
moved = numpy.array([[5.0, 5.0, 5.0]], "<f4")
nomad.send({"type": "mesh_delta", "mesh_id": "cube1", "count": 1, "vertex_count": 4,
            "index_offset": 0, "position_offset": 4, "position_format": "float32x3",
            "binary_size": 16, "live_sync": True},
           numpy.array([1], "<u4").tobytes() + moved.tobytes())
check(wait(lambda: link.revision > revision), "delta bumps the revision")
check(numpy.allclose(link.meshes["cube1"]["positions"][1], [5, 5, 5]), "delta patched the cache")

matrix = list(convert.IDENTITY)
matrix[13] = 3.0
nomad.send({"type": "mesh_instance", "mesh_id": "cube2", "geometry_id": "geo1",
            "name": "Copy", "visible": True, "world_matrix": matrix, "live_sync": False})
check(wait(lambda: "cube2" in link.meshes), "mesh_instance reuses the known geometry")
check(link.meshes["cube2"]["world_matrix"][13] == 3.0, "instance keeps its own transform")

nomad.send({"type": "mesh_instance", "mesh_id": "cube3", "geometry_id": "unknown",
            "name": "Orphan", "world_matrix": list(convert.IDENTITY)})
check(wait(lambda: any(h.get("type") == "request_mesh" for h, _ in nomad.received)),
      "unknown geometry triggers request_mesh (PROTOCOL.md section 8)")

nomad.send({"type": "object_state", "link_id": "cube1", "name": "Renamed", "visible": True})
check(wait(lambda: link.meshes["cube1"]["name"] == "Renamed"), "object_state renames")
nomad.send({"type": "object_delete", "link_id": "cube2"})
check(wait(lambda: "cube2" not in link.meshes), "object_delete removes the mesh")

header, binary = cube_mesh_full("out1", "outgeo")
header["request_id"] = "req1"
link.send_mesh(header, binary)
check(wait(lambda: any(h.get("type") == "mesh_full" for h, _ in nomad.received)),
      "outgoing mesh_full reaches Nomad")
sent, payload = next((h, b) for h, b in nomad.received if h.get("type") == "mesh_full")
check(sent["binary_size"] == len(payload), "outgoing binary_size matches the payload")
check(sent["face_format"] == "corners" and sent["coordinate_system"] == "nomad_y_up",
      "outgoing header follows the protocol")

link.disconnect()
os.remove(client_module._token_path())
print("\nall good")
