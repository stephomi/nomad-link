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
check(numpy.allclose(mesh["color"][0], [1, 0, 0], atol=0.01), "vertex color decoded")

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
check(wait(lambda: any(h.get("type") == "request_mesh" for h, _ in nomad.received), 14.0),
      "unknown geometry triggers request_mesh once quiet (PROTOCOL.md section 8)")

nomad.send({"type": "object_state", "link_id": "cube1", "name": "Renamed", "visible": True})
check(wait(lambda: link.meshes["cube1"]["name"] == "Renamed"), "object_state renames")
nomad.send({"type": "object_state", "link_id": "cube1", "visible": False})
check(wait(lambda: link.meshes["cube1"]["visible"] is False), "object_state hides the mesh")
nomad.send(*cube_mesh_full())  # no "visible" in the header: leave the flag as-is
check(wait(lambda: link.meshes["cube1"]["name"] == "Nomad Cube"), "the mesh_full re-lands")
check(link.meshes["cube1"]["visible"] is False, "a mesh_full without visible keeps it hidden")
header, binary = cube_mesh_full()
header["visible"] = True
nomad.send(header, binary)
check(wait(lambda: link.meshes["cube1"]["visible"] is True), "mesh_full visible is applied")

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

# ---- scene objects: material, texture, light, camera (PROTOCOL.md section 10)
nomad.send({"type": "material", "mesh_id": "cube1", "live_sync": False, "material": {
    "color": [0.8, 0.1, 0.1], "roughness": 0.4, "metalness": 1.0,
    "textures": {"color": {"texture_id": "tex1", "name": "skin.png", "scale": [2.0, 2.0]}},
}})
check(wait(lambda: "cube1" in link.materials), "material lands in the cache")
check(link.materials["cube1"]["roughness"] == 0.4, "material values stored")
# the request is deferred until the transfer is quiet: anything sent mid-transfer
# stalls Nomad's sender
check(wait(lambda: any(h.get("type") == "request_texture" for h, _ in nomad.received), 14.0),
      "an unknown texture_id is requested once the link is properly quiet (section 10.2)")

nomad.send({"type": "texture", "texture_id": "tex1", "name": "skin.png", "binary_size": 4},
           b"\x89PNG")
check(wait(lambda: "tex1" in link.textures), "texture blob is cached")
cached = link.textures["tex1"]["path"]
check(os.path.isfile(cached) and open(cached, "rb").read() == b"\x89PNG",
      "the blob was written to disk verbatim for USD to reference")
check(cached.endswith(".png"), "the extension follows the name: %s" % os.path.basename(cached))

# only edited fields travel: a second material message must merge, not replace
nomad.send({"type": "material", "mesh_id": "cube1", "material": {"roughness": 0.9}})
check(wait(lambda: link.materials["cube1"]["roughness"] == 0.9), "material update applies")
check(link.materials["cube1"]["metalness"] == 1.0, "untouched material fields survive")
check(link.materials["cube1"]["textures"]["color"]["texture_id"] == "tex1",
      "an absent textures block keeps the current assignment")

nomad.send({"type": "light", "link_id": "l1", "name": "Key", "light_type": "spot",
            "color": [1.0, 0.9, 0.8], "power": 40.0, "spot_angle": 0.6,
            "world_matrix": list(convert.IDENTITY)})
check(wait(lambda: "l1" in link.lights), "light lands in the cache")
check(link.lights["l1"]["light_type"] == "spot", "light type stored")
nomad.send({"type": "light", "link_id": "l1", "power": 80.0})
check(wait(lambda: link.lights["l1"]["power"] == 80.0), "light update applies")
check(link.lights["l1"]["spot_angle"] == 0.6, "untouched light fields survive")

nomad.send({"type": "camera_object", "link_id": "c1", "name": "Shot", "fov_y": 35.0,
            "world_matrix": list(convert.IDENTITY)})
check(wait(lambda: "c1" in link.cameras), "camera_object lands in the cache")

nomad.send({"type": "object_state", "link_id": "l1", "name": "Key Light", "visible": False})
check(wait(lambda: link.lights["l1"]["name"] == "Key Light"), "object_state renames a light")
check(link.lights["l1"]["visible"] is False, "object_state hides a light")

# state can arrive before the object it describes: it must not be dropped
nomad.send({"type": "object_state", "link_id": "later", "name": "Late", "visible": False})
nomad.send(*cube_mesh_full("later", "latergeo"))
check(wait(lambda: "later" in link.meshes), "the late mesh arrives")
check(link.meshes["later"]["visible"] is False,
      "an object_state that preceded its mesh is applied when the mesh lands")

nomad.send({"type": "shading_config", "live_sync": False,
            "shading": {"shader_type": "pbr", "environment_enable": True}})
check(wait(lambda: link.display.get("environment_enable") is True), "shading_config stored")

nomad.send({"type": "camera", "fov_y": 50.0, "pivot": [0, 1, 0],
            "world_from_view": list(convert.IDENTITY)})
check(wait(lambda: link.working_camera.get("fov_y") == 50.0), "the working view is tracked")

nomad.send({"type": "object_delete", "link_id": "l1"})
check(wait(lambda: "l1" not in link.lights), "object_delete removes a light")

for name in ("material", "light", "camera_object", "texture", "shading_config"):
    check(name in nomad.hello["capabilities"], "we advertise `%s`" % name)
check("camera" not in nomad.hello["capabilities"],
      "we do not advertise `camera`: that would promise our working view")

os.remove(cached)
link.disconnect()
os.remove(client_module._token_path())
print("\nall good")
