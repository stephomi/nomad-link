# SPDX-License-Identifier: MIT
"""The Houdini side: SOP cooks, parameter callbacks and menus.

`Nomad Link In` builds geometry from the cache, `Nomad Link Out` pushes its
input geometry to Nomad. Both are thin: all network work happens in client.py.

Cook-relevant parameters live on the inner Python SOP as channel references to
the asset (see build_hda.py) so Houdini tracks them as real cook dependencies;
everything else is read from the asset node.
"""
import uuid

import hou
import numpy

from . import convert
from .client import DEFAULT_PORT, client

IN_TYPE = "nomad_link_in"
OUT_TYPE = "nomad_link_out"
ID_NAMESPACE = uuid.UUID("6f9c1f2a-0e3d-4f1b-9a77-1a2b3c4d5e6f")

# helper attributes the HDA's wrangles create, so topology reads as flat arrays
# instead of a Python loop over primitives
VERTEX_POINT = "nomad_vtxpt"
PRIM_SIZE = "nomad_nvtx"

CHANNELS = (("Cd", "color", 3), ("Alpha", "alpha", 1), ("rough", "rough", 1),
            ("metallic", "metallic", 1), ("mask", "mask", 1), ("density", "density", 1))


# ------------------------------------------------------------------ utilities

def _instances(type_name):
    node_type = hou.nodeType(hou.sopNodeTypeCategory(), type_name)
    return node_type.instances() if node_type else ()


def _eval(node, name, default=0):
    """Parm from the node itself, else from the asset above it."""
    parm = node.parm(name) or (node.parent().parm(name) if node.parent() else None)
    return parm.eval() if parm is not None else default


def _set_attrib(geo, kind, name, values, size, default=0.0, integer=False):
    fallback = tuple([default] * size) if size > 1 else default
    geo.addAttrib(kind, name, int(default) if integer and size == 1 else fallback)
    setters = {
        (hou.attribType.Point, False): geo.setPointFloatAttribValues,
        (hou.attribType.Vertex, False): geo.setVertexFloatAttribValues,
        (hou.attribType.Prim, False): geo.setPrimFloatAttribValues,
        (hou.attribType.Prim, True): geo.setPrimIntAttribValues,
    }
    setter = setters[(kind, integer)]
    flat = numpy.ascontiguousarray(values, "i4" if integer else "f8").ravel()
    try:
        setter(name, flat)
    except (TypeError, hou.OperationFailed):
        setter(name, flat.tolist())


def status_text():
    link = client()
    return "%s - %s" % (link.status, link.message)


def refresh_inputs(revision):
    """Called from the pump when the cache changed: dirty every In SOP."""
    for node in _instances(IN_TYPE):
        parm = node.parm("revision")
        if parm is not None and parm.eval() != revision:
            parm.set(revision)
    for node in list(_instances(IN_TYPE)) + list(_instances(OUT_TYPE)):
        parm = node.parm("status")
        if parm is not None and parm.evalAsString() != status_text():
            parm.set(status_text())


def store_mesh_id(node_path, mesh_id):
    node = hou.node(node_path)
    if node is not None and mesh_id and node.evalParm("meshid") != mesh_id:
        node.parm("meshid").set(mesh_id)


def mesh_menu():
    """Menu script for the In SOP's source list."""
    link = client()
    items = ["__all__", "All meshes"]
    for mesh_id in link.order:
        mesh = link.meshes.get(mesh_id)
        if mesh:
            items += [mesh_id, mesh["name"]]
    return items


# ---------------------------------------------------------------- parm buttons

def connect_button(kwargs):
    node = kwargs["node"]
    link = client()
    link.connect(node.evalParm("host").strip(), node.evalParm("port") or DEFAULT_PORT)
    refresh_inputs(link.revision)


def disconnect_button(kwargs):
    client().disconnect()
    refresh_inputs(client().revision)


def get_selection(kwargs):
    client().request("request_selection")


def get_scene(kwargs):
    client().request("request_scene")


def send_button(kwargs):
    node = kwargs["node"]
    send_geometry(node, node.node("OUT").geometry())


# --------------------------------------------------------------- In SOP (read)

def cook_in(sop):
    geo = sop.geometry()
    link = client()
    _eval(sop, "revision")  # cook dependency: new Nomad data bumps this

    source = _eval(sop, "source", "__all__")
    scale = _eval(sop, "scale", 1.0) or 1.0
    world = _eval(sop, "applyxform", 1)
    reverse = _eval(sop, "reverse", 1)
    want_uv = _eval(sop, "importuv", 1)
    want_color = _eval(sop, "importcolor", 1)
    want_groups = _eval(sop, "importgroups", 1)

    ids = list(link.order) if source == "__all__" else [source]
    meshes = [link.meshes[i] for i in ids if i in link.meshes]
    meshes = [m for m in meshes if m.get("visible", True)]
    if not meshes:
        return

    positions, sizes, corners, uvs, names, prim_counts = [], [], [], [], [], []
    point_base = 0
    for mesh in meshes:
        points = mesh["positions"]
        if world:
            points = convert.transform_points(points, mesh["world_matrix"])
        positions.append(points * scale)

        mesh_sizes = numpy.asarray(mesh["sizes"], numpy.int32)
        mesh_corners = numpy.asarray(mesh["corners"], numpy.int64)
        permutation = convert.reverse_permutation(mesh_sizes) if reverse else None
        if permutation is not None:
            mesh_corners = mesh_corners[permutation]
        sizes.append(mesh_sizes)
        corners.append(mesh_corners + point_base)
        names.append(mesh["name"])
        prim_counts.append(len(mesh_sizes))

        corner_count = int(mesh_sizes.sum())
        if want_uv and "texcoords" in mesh:
            corner_uv = numpy.asarray(mesh["corner_uv"], numpy.int64)
            if permutation is not None:
                corner_uv = corner_uv[permutation]
            texcoords = mesh["texcoords"][corner_uv]
            uvs.append(numpy.column_stack((
                texcoords[:, 0],
                1.0 - texcoords[:, 1],  # Nomad's v origin is top-left
                numpy.zeros(corner_count, numpy.float32),
            )))
        else:
            uvs.append(numpy.zeros((corner_count, 3), numpy.float32))
        point_base += len(points)

    positions = numpy.concatenate(positions)
    sizes = numpy.concatenate(sizes)
    corners = numpy.concatenate(corners)

    _create_points(geo, positions)
    unique = numpy.unique(sizes)
    if len(unique) == 1:
        faces = corners.reshape(-1, int(unique[0])).tolist()
    else:
        faces = [part.tolist() for part in numpy.split(corners, numpy.cumsum(sizes)[:-1])]
    geo.createPolygons(faces)

    if want_uv and any("texcoords" in m for m in meshes):
        _set_attrib(geo, hou.attribType.Vertex, "uv", numpy.concatenate(uvs), 3)
    if want_color:
        _point_channel(geo, meshes, "color", "Cd", 3, 1.0)
        _point_channel(geo, meshes, "alpha", "Alpha", 1, 1.0)
        _point_channel(geo, meshes, "rough", "rough", 1, 0.3)
        _point_channel(geo, meshes, "metallic", "metallic", 1, 0.0)
    _point_channel(geo, meshes, "mask", "mask", 1, 1.0)
    _point_channel(geo, meshes, "density", "density", 1, 0.0)
    if want_groups:
        _face_groups(geo, meshes)

    # one string per primitive: Split/Group by `name` gives you each Nomad mesh
    geo.addAttrib(hou.attribType.Prim, "name", "")
    labels = []
    for name, count in zip(names, prim_counts):
        labels += [name] * count
    try:
        geo.setPrimStringAttribValues("name", labels)
    except (TypeError, hou.OperationFailed):
        pass

    geo.addAttrib(hou.attribType.Global, "nomad_mesh_ids", "")
    geo.setGlobalAttribValue("nomad_mesh_ids", " ".join(m["mesh_id"] for m in meshes))


def _create_points(geo, positions):
    values = numpy.ascontiguousarray(positions, "f8")
    try:
        geo.createPoints(values)
    except TypeError:
        geo.createPoints(values.tolist())


def _point_channel(geo, meshes, key, attrib, size, default):
    if not any(key in mesh for mesh in meshes):
        return
    blocks = []
    for mesh in meshes:
        count = len(mesh["positions"])
        values = mesh.get(key)
        if values is None:
            blocks.append(numpy.full((count, size), default, numpy.float32))
        else:
            blocks.append(numpy.asarray(values, numpy.float32).reshape(count, size))
    _set_attrib(geo, hou.attribType.Point, attrib, numpy.concatenate(blocks), size, default)


def _face_groups(geo, meshes):
    if not any("face_group" in mesh for mesh in meshes):
        return
    values, base = [], 0
    for mesh in meshes:
        count = len(mesh["sizes"])
        indices = numpy.asarray(mesh.get("face_group", numpy.zeros(count, numpy.int32)), numpy.int32)
        values.append(indices + base)
        base += max(len(mesh.get("face_group_names", ())), int(indices.max()) + 1 if count else 0)
    _set_attrib(geo, hou.attribType.Prim, "nomad_face_group",
                numpy.concatenate(values), 1, 0.0, integer=True)


# -------------------------------------------------------------- Out SOP (write)

def cook_out(sop):
    """Pass-through cook; sends when Auto Send is on and the input changed."""
    if _eval(sop, "autosend", 0) and client().connected:
        send_geometry(sop.parent(), sop.geometry())


def answer_request(header):
    """Nomad pressed Get: every Out SOP that opted in replies with its geometry."""
    link_id = header.get("link_id")
    for node in _instances(OUT_TYPE):
        if not node.evalParm("answer"):
            continue
        if link_id and link_id != _mesh_id(node):
            continue
        try:
            geo = node.node("OUT").geometry()
        except hou.Error:
            continue
        send_geometry(node, geo, request_id=header.get("request_id", ""))


def _mesh_id(node):
    """Stable per-node id: the parm once Nomad has acked, else derived from the path."""
    return node.evalParm("meshid") or uuid.uuid5(ID_NAMESPACE, node.path()).hex


def _geometry_id(node):
    return node.evalParm("geoid") or uuid.uuid5(ID_NAMESPACE, node.path() + "/geo").hex


def send_geometry(node, geo, request_id=""):
    link = client()
    if not link.connected:
        link.message = "Not connected"
        return False
    if geo is None or geo.intrinsicValue("pointcount") == 0:
        link.note("%s: nothing to send" % node.path())
        return False

    scale = node.evalParm("scale") or 1.0
    reverse = node.evalParm("reverse")

    positions = numpy.array(geo.pointFloatAttribValues("P"), numpy.float32).reshape(-1, 3) * scale
    sizes = numpy.array(geo.primIntAttribValues(PRIM_SIZE), numpy.int32)
    corners = numpy.array(geo.vertexIntAttribValues(VERTEX_POINT), numpy.int32)
    permutation = convert.reverse_permutation(sizes) if reverse else None
    if permutation is not None:
        corners = corners[permutation]

    texcoords = corner_uv = None
    if node.evalParm("senduv"):
        attrib = geo.findVertexAttrib("uv") or geo.findPointAttrib("uv")
        if attrib is not None:
            size = attrib.size()
            if attrib.type() == hou.attribType.Vertex:
                raw = numpy.array(geo.vertexFloatAttribValues("uv"), numpy.float32).reshape(-1, size)
            else:
                per_point = numpy.array(geo.pointFloatAttribValues("uv"), numpy.float32).reshape(-1, size)
                raw = per_point[numpy.array(geo.vertexIntAttribValues(VERTEX_POINT), numpy.int64)]
            texcoords = numpy.column_stack((raw[:, 0], 1.0 - raw[:, 1]))  # back to top-left v
            corner_uv = numpy.arange(len(corners), dtype=numpy.int32)
            if permutation is not None:
                corner_uv = corner_uv[permutation]

    point_attribs = {}
    if node.evalParm("sendcolor"):
        count = len(positions)
        for attrib, key, size in CHANNELS:
            if geo.findPointAttrib(attrib) is None:
                continue
            values = numpy.array(geo.pointFloatAttribValues(attrib), numpy.float32)
            point_attribs[key] = values.reshape(count, size) if size > 1 else values

    face_group = None
    group_names = ()
    if geo.findPrimAttrib("nomad_face_group") is not None:
        face_group = numpy.array(geo.primIntAttribValues("nomad_face_group"), numpy.int32)
        if len(face_group):
            group_names = ["Group %d" % (i + 1) for i in range(int(face_group.max()) + 1)]

    world_matrix = list(convert.IDENTITY)
    if node.evalParm("applyxform"):
        try:
            matrix = list(node.creator().worldTransform().asTuple())
            matrix[12] *= scale
            matrix[13] *= scale
            matrix[14] *= scale
            world_matrix = matrix
        except (hou.Error, AttributeError):
            pass

    header, binary = convert.encode_mesh(
        mesh_id=_mesh_id(node),
        geometry_id=_geometry_id(node),
        name=node.evalParm("meshname") or node.name(),
        positions=positions,
        sizes=sizes,
        corners=corners,
        texcoords=texcoords,
        corner_uv=corner_uv,
        point_attribs=point_attribs,
        face_group=face_group,
        face_group_names=group_names,
        world_matrix=world_matrix,
        ngon=link.peer_has("ngon"),
        request_id=request_id or uuid.uuid4().hex,
    )
    link.send_mesh(header, binary, node.path())
    link.message = "Sent %s: %d points, %d faces" % (
        header["name"], header["vertex_count"], header["face_count"],
    )
    return True
