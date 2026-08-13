# SPDX-License-Identifier: MIT
"""Exercise the SOP code paths against a fake `hou`. Outside Houdini:

    python3 tests/test_nodes.py

This checks the array bookkeeping (winding, uv flip, attribute plumbing), not
Houdini's own behaviour -- the real nodes still need a smoke test in Houdini.
"""
import os
import sys

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "python"))

import fake_hou  # noqa: E402

hou = fake_hou.install()

from nomad_link import convert, nodes  # noqa: E402
from nomad_link.client import client  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("ok  " + message)


# a quad and a triangle, with per-corner uvs and vertex colour
POINTS = numpy.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [2, 0, 0]], "f4")
SIZES = numpy.array([4, 3], "i4")
CORNERS = numpy.array([0, 1, 2, 3, 1, 4, 2], "i4")
TEXCOORDS = numpy.array([[0, 0], [1, 0], [1, 1], [0, 1], [0, 0], [1, 0], [1, 1]], "f4")


def seed_cache():
    header, binary = convert.encode_mesh(
        mesh_id="m1", geometry_id="g1", name="Sculpt",
        positions=POINTS, sizes=SIZES, corners=CORNERS, texcoords=TEXCOORDS,
        point_attribs={"color": numpy.tile([0.2, 0.4, 0.6], (5, 1)),
                       "mask": numpy.linspace(0, 1, 5)},
        face_group=numpy.array([0, 1], "i4"), face_group_names=("Head", "Body"),
        world_matrix=[1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 10, 0, 1],  # +10 in Y
        ngon=True,
    )
    link = client()
    link.meshes.clear()
    del link.order[:]
    link._store(convert.decode_mesh(header, binary))
    return link


def in_node(**overrides):
    parms = {"source": "__all__", "scale": 1.0, "applyxform": 1, "reverse": 1,
             "importuv": 1, "importcolor": 1, "importgroups": 1, "revision": 0}
    parms.update(overrides)
    asset = fake_hou.Node("nomad_link_in1", parms)
    sop = fake_hou.Node("build", {}, parent=asset)
    return sop


def test_cook_in():
    seed_cache()
    sop = in_node()
    nodes.cook_in(sop)
    geo = sop.geometry()
    check(len(geo.points) == 5, "points built (%d)" % len(geo.points))
    check(numpy.allclose(geo.points[0], [0, 10, 0]), "world_matrix baked into the points")
    check(geo.polygons == [[3, 2, 1, 0], [2, 4, 1]], "winding flipped per face: %s" % geo.polygons)
    uv = geo.values[(hou.attribType.Vertex, "uv")].reshape(-1, 3)
    check(numpy.allclose(uv[0], [0, 0, 0]), "first corner uv follows the flipped winding")
    check(numpy.allclose(uv[:, 1], [1 - v for v in [1, 1, 0, 0, 1, 0, 0]]),
          "v flipped to Houdini's bottom-left origin")
    colour = geo.values[(hou.attribType.Point, "Cd")].reshape(-1, 3)
    check(numpy.allclose(colour[0], [0.2, 0.4, 0.6], atol=0.01), "Cd imported")
    check(numpy.allclose(geo.values[(hou.attribType.Point, "mask")][-1], 1.0, atol=0.01),
          "mask imported")
    check(geo.values[(hou.attribType.Prim, "name")] == ["Sculpt", "Sculpt"], "prim name attribute")
    check(list(geo.values[(hou.attribType.Prim, "nomad_face_group")]) == [0, 1], "face groups")
    check(geo.globals["nomad_mesh_ids"] == "m1", "mesh ids on the detail")


def test_cook_in_options():
    seed_cache()
    sop = in_node(applyxform=0, reverse=0, scale=2.0, importuv=0)
    nodes.cook_in(sop)
    geo = sop.geometry()
    check(numpy.allclose(geo.points[1], [2, 0, 0]), "scale applied, transform skipped")
    check(geo.polygons == [[0, 1, 2, 3], [1, 4, 2]], "winding kept when Flip Winding is off")
    check((hou.attribType.Vertex, "uv") not in geo.values, "uvs skipped when asked")


def test_two_meshes_merge():
    link = seed_cache()
    second = convert.decode_mesh(*convert.encode_mesh(
        mesh_id="m2", geometry_id="g2", name="Second",
        positions=POINTS + 5.0, sizes=SIZES, corners=CORNERS, ngon=True))
    link._store(second)
    sop = in_node(applyxform=0)
    nodes.cook_in(sop)
    geo = sop.geometry()
    check(len(geo.points) == 10, "both meshes merged")
    check(max(max(face) for face in geo.polygons) == 9, "second mesh's corners are offset")
    check(geo.values[(hou.attribType.Prim, "name")] == ["Sculpt"] * 2 + ["Second"] * 2,
          "per-mesh prim names")


def test_send_geometry():
    link = client()
    link.peer_capabilities = {"ngon"}
    sent = []
    link.send_mesh = lambda header, binary, path="": sent.append((header, binary))

    geo = fake_hou.Geometry()
    geo.createPoints(POINTS.astype("f8"))
    geo.addAttrib(hou.attribType.Prim, nodes.PRIM_SIZE, 0)
    geo.setPrimIntAttribValues(nodes.PRIM_SIZE, SIZES)
    geo.addAttrib(hou.attribType.Vertex, nodes.VERTEX_POINT, 0)
    geo._store(hou.attribType.Vertex, nodes.VERTEX_POINT, CORNERS)
    geo.addAttrib(hou.attribType.Vertex, "uv", (0.0, 0.0, 0.0))
    geo._store(hou.attribType.Vertex, "uv",
               numpy.column_stack((TEXCOORDS, numpy.zeros(len(TEXCOORDS)))))
    geo.addAttrib(hou.attribType.Point, "Cd", (1.0, 1.0, 1.0))
    geo._store(hou.attribType.Point, "Cd", numpy.tile([1.0, 0.5, 0.0], (5, 1)))

    node = fake_hou.Node("nomad_link_out1", {
        "scale": 1.0, "reverse": 1, "applyxform": 0, "senduv": 1, "sendcolor": 1,
        "meshname": "Houdini Mesh", "meshid": "", "geoid": "",
    })
    check(nodes.send_geometry(node, geo), "send_geometry reports success")
    header, binary = sent[-1]
    check(header["type"] == "mesh_full" and header["face_format"] == "corners",
          "n-gon capable peer gets the corners format")
    check(header["vertex_count"] == 5 and header["face_count"] == 2, "counts are right")
    check(header["binary_size"] == len(binary), "binary_size matches")

    back = convert.decode_mesh(header, binary)
    check(back["corners"].tolist() == [3, 2, 1, 0, 2, 4, 1],
          "winding flipped on the way out: %s" % back["corners"].tolist())
    check(numpy.allclose(back["texcoords"][back["corner_uv"]][:, 1],
                         [1 - v for v in [1, 1, 0, 0, 1, 0, 0]]),
          "v flipped back to Nomad's top-left origin")
    check(numpy.allclose(back["color"][0], [1.0, 0.5, 0.0], atol=0.01), "Cd travels as rgbm8")
    check(header["mesh_id"] == nodes._mesh_id(node), "a stable mesh id is derived from the path")

    # no ngon on the peer: the same geometry must arrive as tris/quads
    link.peer_capabilities = set()
    nodes.send_geometry(node, geo)
    header, binary = sent[-1]
    check(header["face_format"] == "int32x4", "peer without ngon gets int32x4")
    split = convert.decode_mesh(header, binary)
    check(sorted(split["sizes"].tolist()) == [3, 4], "quad stays a quad, triangle stays a triangle")


def test_send_empty():
    link = client()
    check(not nodes.send_geometry(fake_hou.Node("out", {}), fake_hou.Geometry()),
          "empty geometry is not sent")


if __name__ == "__main__":
    client().connection.status = "Connected"  # skip the socket for these tests
    for key, value in sorted(globals().items()):
        if key.startswith("test_"):
            print("--- %s" % key)
            value()
    print("\nall good")
