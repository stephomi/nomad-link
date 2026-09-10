# SPDX-License-Identifier: MIT
"""End-to-end inside Houdini, against a mock Nomad on localhost:

    hython tests/test_houdini.py

Builds the real HDAs, cooks them, and checks the geometry that comes out of
each direction. No Nomad and no network beyond loopback.
"""
import os
import sys

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "python"))

import hou  # noqa: E402
from mock_nomad import MockNomad, wait  # noqa: E402

import nomad_link  # noqa: E402
from nomad_link import convert, nodes  # noqa: E402

PORT = 48398


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("ok  " + message)


def nomad_quad_and_tri():
    """A quad plus a triangle, with uvs, color, and face groups."""
    points = numpy.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [2, 0, 0]], "f4")
    texcoords = numpy.array([[0, 0], [1, 0], [1, 1], [0, 1], [0, 0], [1, 0], [1, 1]], "f4")
    return convert.encode_mesh(
        mesh_id="m1", geometry_id="g1", name="Sculpt",
        positions=points, sizes=numpy.array([4, 3], "i4"),
        corners=numpy.array([0, 1, 2, 3, 1, 4, 2], "i4"), texcoords=texcoords,
        point_attribs={"color": numpy.tile([0.2, 0.4, 0.6], (5, 1)),
                       "mask": numpy.linspace(0, 1, 5)},
        face_group=numpy.array([0, 1], "i4"), face_group_names=("Head", "Body"),
        world_matrix=[1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 10, 0, 1],  # +10 in Y
        ngon=True,
    )


hou.hda.installFile(os.path.join(ROOT, "otls", "nomad_link.hda"))
check(hou.nodeType(hou.sopNodeTypeCategory(), "nomad_link_in") is not None,
      "nomad_link_in is installed")
check(hou.nodeType(hou.sopNodeTypeCategory(), "nomad_link_out") is not None,
      "nomad_link_out is installed")

container = hou.node("/obj").createNode("geo", "test")

# why Flip Winding defaults on: Houdini's front faces are wound clockwise, glTF
# (and so Nomad) counter-clockwise. Measured, not assumed.
probe = container.createNode("box").geometry().prims()[0]
corners = [numpy.array(vertex.point().position()) for vertex in probe.vertices()]
cross = numpy.cross(corners[1] - corners[0], corners[2] - corners[0])
handedness = float(numpy.dot(cross / numpy.linalg.norm(cross), numpy.array(probe.normal())))
check(handedness < 0, "Houdini winding is clockwise-front, so the flip is needed (%.1f)" % handedness)

node_in = container.createNode("nomad_link_in")
node_out = container.createNode("nomad_link_out")

for name in ("host", "port", "connect", "disconnect", "status", "getsel", "getscene",
             "source", "applyxform", "reverse", "scale", "importuv", "importcolor",
             "importgroups", "revision"):
    check(node_in.parm(name) is not None, "In has a `%s` parm" % name)
for name in ("meshname", "send", "autosend", "answer", "applyxform", "reverse",
             "scale", "senduv", "sendcolor", "meshid", "geoid"):
    check(node_out.parm(name) is not None, "Out has a `%s` parm" % name)

inner = node_in.node("build")
check(inner is not None and inner.parm("revision") is not None,
      "the inner Python SOP got its spare parms")
check(inner.parm("revision").expression() == 'ch("../revision")',
      "spare parms reference the asset: %s" % inner.parm("revision").expression())
check(node_in.node("build").parm("source").expression() == 'chs("../source")',
      "string parms use chs()")

check(len(node_in.geometry().points()) == 0, "In cooks empty while disconnected")

nomad = MockNomad(PORT)
nomad.start()
link = nomad_link.client()
link.connect("127.0.0.1", PORT)
check(wait(link, lambda: link.connected and link.nomad_version == "2.0"),
      "Houdini connects and pairs")

# ---------------------------------------------------------------- Nomad -> Houdini
nomad.send(*nomad_quad_and_tri())
check(wait(link, lambda: "m1" in link.meshes), "mesh_full lands in the cache")
# the refresh is coalesced, so it lands a pump or two after the data does
check(wait(link, lambda: node_in.evalParm("revision") > 0),
      "the In SOP's revision parm was bumped")

geo = node_in.geometry()
check(len(geo.points()) == 5, "In built 5 points, got %d" % len(geo.points()))
check(len(geo.prims()) == 2, "In built 2 primitives")
check(abs(geo.points()[0].position()[1] - 10.0) < 1e-5,
      "world_matrix baked in: y=%.3f" % geo.points()[0].position()[1])
sizes = sorted(len(prim.vertices()) for prim in geo.prims())
check(sizes == [3, 4], "the quad stayed a quad: %s" % sizes)
check([v.point().number() for v in geo.prims()[0].vertices()] == [3, 2, 1, 0],
      "winding flipped on the way in")
check(geo.findVertexAttrib("uv") is not None, "uv attribute created")
check(abs(geo.prims()[0].vertices()[0].attribValue("uv")[1] - 0.0) < 1e-5,
      "v flipped to Houdini's origin")
check(geo.findPointAttrib("Cd") is not None and geo.findPointAttrib("mask") is not None,
      "Cd and mask imported")
check(abs(geo.points()[0].attribValue("Cd")[2] - 0.6) < 0.01, "Cd values are right")
check([p.attribValue("name") for p in geo.prims()] == ["Sculpt", "Sculpt"],
      "prim name attribute carries the Nomad object name")
check([p.attribValue("nomad_face_group") for p in geo.prims()] == [0, 1], "face groups imported")

# a live stroke: the SOP must follow without anyone touching it
revision = link.revision
moved = numpy.array([[7.0, 7.0, 7.0]], "<f4")
nomad.send({"type": "mesh_delta", "mesh_id": "m1", "count": 1, "vertex_count": 5,
            "index_offset": 0, "position_offset": 4, "position_format": "float32x3",
            "binary_size": 16, "live_sync": True},
           numpy.array([0], "<u4").tobytes() + moved.tobytes())
check(wait(link, lambda: link.revision > revision), "mesh_delta arrives")
check(wait(link, lambda: abs(node_in.geometry().points()[0].position()[1] - 17.0) < 1e-4),
      "the In SOP recooked from the delta (y=%.3f, expected 17)"
      % node_in.geometry().points()[0].position()[1])

# ---------------------------------------------------------------- Houdini -> Nomad
box = container.createNode("box")
box.parm("type").set(1)  # polygon mesh, so there are real polygons to send
node_out.setInput(0, box)
out_geo = node_out.node("OUT").geometry()
check(out_geo.findVertexAttrib(nodes.VERTEX_POINT) is not None,
      "the vertex->point wrangle ran")
check(out_geo.findPrimAttrib(nodes.PRIM_SIZE) is not None, "the prim-size wrangle ran")
check(len(node_out.geometry().points()) == len(out_geo.points()),
      "Out passes its input through unchanged")

node_out.parm("meshname").set("Houdini Box")
node_out.parm("applyxform").set(0)
node_out.parm("send").pressButton()
check(wait(link, lambda: nomad.first("mesh_full")[0] is not None),
      "Send to Nomad delivered a mesh_full")
header, binary = nomad.first("mesh_full")
check(header["name"] == "Houdini Box", "the name parm is used")
check(header["binary_size"] == len(binary), "binary_size matches the payload")
check(header["face_format"] == "corners", "n-gon capable peer gets the corners format")
check(header["vertex_count"] == len(out_geo.points()),
      "all %d points were sent" % header["vertex_count"])

back = convert.decode_mesh(header, binary)
check(len(back["sizes"]) == len(out_geo.prims()), "face count survives")
houdini_corners = [v.point().number() for v in out_geo.prims()[0].vertices()]
check(back["corners"][:len(houdini_corners)].tolist() == houdini_corners[::-1],
      "winding flipped on the way out")

# Nomad's own Get must be answered
nomad.received[:] = []
nomad.send({"type": "request_scene", "request_id": "req9"})
check(wait(link, lambda: nomad.first("mesh_full")[0] is not None),
      "request_scene is answered by the Out SOP")
check(nomad.first("mesh_full")[0]["request_id"] == "req9", "the request_id is echoed")

# and Nomad's ack must stick to the node
nomad.send({"type": "mesh_ack", "mesh_id": "nomad_side_id", "request_id": "req9"})
check(wait(link, lambda: node_out.evalParm("meshid") == "nomad_side_id"),
      "mesh_ack stores Nomad's mesh id on the node")

# ---------------------------------------------------------------- Solaris (LOPs)
check(hou.nodeType(hou.lopNodeTypeCategory(), "nomad_link_import") is not None,
      "nomad_link_import is installed as a LOP")
lop = hou.node("/stage").createNode("nomad_link_import")

nomad.send({"type": "material", "mesh_id": "m1", "material": {
    "color": [0.8, 0.1, 0.1], "roughness": 0.35, "metalness": 0.0}})
nomad.send({"type": "light", "link_id": "l1", "name": "Key", "light_type": "spot",
            "color": [1.0, 0.9, 0.8], "power": 40.0, "spot_angle": 1.0,
            "world_matrix": list(convert.IDENTITY)})
camera_matrix = list(convert.IDENTITY)
camera_matrix[14] = 12.0
nomad.send({"type": "camera_object", "link_id": "c1", "name": "Shot", "fov_y": 35.0,
            "world_matrix": camera_matrix})
check(wait(link, lambda: link.materials and link.lights and link.cameras),
      "the scene objects reach the cache")

from pxr import UsdGeom, UsdLux, UsdShade  # noqa: E402

usd_stage = lop.stage()
paths = [p.GetPath().pathString for p in usd_stage.Traverse()]
check("/nomad/Sculpt" in paths, "the mesh is on the stage: %s" % paths)
usd_mesh = UsdGeom.Mesh(usd_stage.GetPrimAtPath("/nomad/Sculpt"))
check(list(usd_mesh.GetFaceVertexCountsAttr().Get()) == [4, 3], "quad survives into USD")
check(list(usd_mesh.GetFaceVertexIndicesAttr().Get()) == [0, 1, 2, 3, 1, 4, 2],
      "USD keeps Nomad's winding, unlike the SOP path")
translation = UsdGeom.Xformable(usd_mesh).GetLocalTransformation().ExtractTranslation()
check(abs(translation[1] - 10.0) < 1e-5, "the world_matrix became the prim transform")
check(abs(usd_mesh.GetPointsAttr().Get()[0][1] - 7.0) < 1e-4,
      "the live delta is in the USD points too")
check(len(UsdGeom.Subset.GetAllGeomSubsets(usd_mesh)) == 2, "face groups became GeomSubsets")

bound = UsdShade.MaterialBindingAPI(usd_mesh.GetPrim()).GetDirectBinding().GetMaterial()
check(bool(bound), "a material is bound to the mesh")
# the LOP defaults to MaterialX OpenPBR, which is what Karma wants
surface = UsdShade.Shader(usd_stage.GetPrimAtPath(bound.GetPath().pathString + "/OpenPBR"))
check(surface.GetIdAttr().Get() == "ND_open_pbr_surface_surfaceshader",
      "the material is an OpenPBR surface")
check(abs(surface.GetInput("specular_roughness").Get() - 0.35) < 1e-6,
      "material values reached USD")
check(bool(bound.GetSurfaceOutput("mtlx").GetConnectedSource()),
      "bound on the mtlx render context")
check(bool(UsdLux.SphereLight(usd_stage.GetPrimAtPath("/nomad/Key"))), "the spot light is a prim")
check(bool(UsdGeom.Camera(usd_stage.GetPrimAtPath("/nomad/Shot"))), "the camera is a prim")

# an instance must share the original's material, not lose it
instance_matrix = list(convert.IDENTITY)
instance_matrix[12] = 5.0
nomad.send({"type": "mesh_instance", "mesh_id": "m1-copy", "geometry_id": "g1",
            "name": "Sculpt Copy", "visible": True, "world_matrix": instance_matrix,
            "live_sync": False})
check(wait(link, lambda: "m1-copy" in link.meshes), "the instance arrives")
# rebuilds are coalesced, so wait for the stage to catch up rather than assume
check(wait(link, lambda: bool(lop.stage().GetPrimAtPath("/nomad/Sculpt_Copy"))),
      "the instance is on the stage")
copied = lop.stage()
copy_prim = copied.GetPrimAtPath("/nomad/Sculpt_Copy")
copy_material = UsdShade.MaterialBindingAPI(copy_prim).GetDirectBinding().GetMaterial()
check(bool(copy_material), "the instance has a material bound")
check(copy_material.GetPath() == bound.GetPath(),
      "and it is the original's material, not a bare one: %s vs %s"
      % (copy_material.GetPath(), bound.GetPath()))
materials_scope = copied.GetPrimAtPath("/nomad/Materials")
check(len(list(materials_scope.GetChildren())) == 1,
      "one material is authored for both, not one per instance")

# a texture: blob -> disk -> the material's image node (PROTOCOL.md 10.2)
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
       b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82")
nomad.send({"type": "material", "mesh_id": "m1", "material": {
    "textures": {"color": {"texture_id": "tex-e2e", "name": "skin.png",
                           "wrap_s": "clamp", "scale": [2.0, 1.0]}}}})
# requests are held until the link has been properly quiet, since asking during
# a transfer makes Nomad restart it
check(wait(link, lambda: any(h.get("type") == "request_texture" for h, _ in nomad.received), 14.0),
      "an unseen texture id is requested once the link is quiet")
nomad.send({"type": "texture", "texture_id": "tex-e2e", "name": "skin.png",
            "binary_size": len(PNG)}, PNG)
check(wait(link, lambda: "tex-e2e" in link.textures), "the blob arrives and is cached")
cached = link.textures["tex-e2e"]["path"]
check(os.path.isfile(cached) and open(cached, "rb").read() == PNG,
      "the image file is on disk byte for byte: %s" % cached)

def find_image():
    for prim in lop.stage().Traverse():
        shader = UsdShade.Shader(prim)
        if shader and shader.GetIdAttr().Get() in ("ND_image_color3", "UsdUVTexture"):
            return shader
    return None


check(wait(link, lambda: find_image() is not None), "the material has an image node")
image = find_image()
check(image.GetInput("file").Get().path == cached,
      "and it points at the cached blob: %s" % image.GetInput("file").Get().path)

# a second light must show up without anyone touching the node
revision = lop.evalParm("revision")
nomad.send({"type": "light", "link_id": "l2", "name": "Rim", "light_type": "directional",
            "intensity": 2.0, "world_matrix": list(convert.IDENTITY)})
check(wait(link, lambda: lop.evalParm("revision") != revision), "the LOP's revision is bumped")
check(wait(link, lambda: bool(UsdLux.DistantLight(lop.stage().GetPrimAtPath("/nomad/Rim")))),
      "the LOP recooked and the new light is on the stage")

# Clear Cache: the cache is a session singleton, so a node cannot own its reset
check(len(link.meshes) > 0 and len(link.textures) > 0, "there is something to clear")
lop.parm("clear").pressButton()
check(not link.meshes and not link.lights and not link.cameras and not link.textures,
      "Clear Cache empties the scene, lights, cameras and textures")
check(len(lop.stage().GetPrimAtPath("/nomad").GetChildren()) == 0
      or not lop.stage().GetPrimAtPath("/nomad/Sculpt"),
      "and the stage empties with it")
check(len(node_in.geometry().points()) == 0, "the In SOP empties too")

nomad.send(*nomad_quad_and_tri())
check(wait(link, lambda: "m1" in link.meshes), "and a fresh scene loads after clearing")

# ---------------------------------------------------------------- Out node identity
# a pasted copy must not inherit the ids, or it would replace the original's mesh
copy = hou.copyNodesTo([node_out], container)[0]
check(copy.evalParm("meshid") == "" and copy.evalParm("geoid") == "",
      "a pasted Out node starts without Nomad's ids (OnLoaded)")
check(node_out.evalParm("meshid") == "nomad_side_id", "the original keeps its id")

# a copy that did inherit them (made before that script existed) heals on send
copy.parm("meshid").set("nomad_side_id")
copy.setInput(0, box)
nomad.received[:] = []
copy.parm("send").pressButton()
check(wait(link, lambda: nomad.first("mesh_full")[0] is not None), "the copy sends")
check(nomad.first("mesh_full")[0]["mesh_id"] != "nomad_side_id",
      "the copy sends as a new mesh rather than over the original")
check(copy.evalParm("meshid") == "", "its inherited id was cleared")
check(node_out.evalParm("meshid") == "nomad_side_id", "the original's id is untouched")

link.disconnect()
print("\nall good")
