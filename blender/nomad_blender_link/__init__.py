# SPDX-License-Identifier: GPL-3.0-or-later

import json
import math
import os
from pathlib import Path
import tempfile
import time
import tomllib
import uuid

import numpy

import bpy
from bpy.app.handlers import persistent
from mathutils import Matrix, Vector

from .transport import Connection, discover, DEFAULT_CAPABILITIES


PROTOCOL_VERSION = 1
PACKAGE_ID = "nomad_blender_link"
VERSION = tomllib.loads((Path(__file__).resolve().parent / "blender_manifest.toml").read_text())["version"]
MESH_ID = "nomad_mesh_id"
GEOMETRY_ID = "nomad_geometry_id"
TRANSFORM_PARENT_ID = "nomad_transform_parent_id"
MATERIAL_ID = "nomad_link_material"
FACE_GROUP_ATTRIBUTE = "nomad_face_group"
FACE_SET_ATTRIBUTE = ".sculpt_face_set"  # Blender's own face sets, mirrored from the groups
HIDE_FACE_ATTRIBUTE = ".hide_poly"  # Nomad's hidden faces
MASK_ATTRIBUTE = ".sculpt_mask"  # inverted: Nomad stores 1 = unmasked, Blender 1 = masked
MODAL_GRACE = 0.5  # how long a queued edit waits on a modal operator before going out anyway
PENDING_TIMEOUT = 10.0  # a peer that never answers a request must not wedge the queue forever
MAX_FACE_GROUP = 32767
COLOR_ATTRIBUTE = "Nomad Color"
ROUGHNESS_ATTRIBUTE = "nomad_roughness"
METALNESS_ATTRIBUTE = "nomad_metalness"
DEFAULT_ROUGHNESS = 0.25
TO_NOMAD = Matrix.Rotation(math.radians(-90.0), 4, "X")
TO_BLENDER = TO_NOMAD.inverted()

# "texture" on top of the shared bridge defaults: only this client handles blobs
connection = Connection(client_name="Blender", capabilities=DEFAULT_CAPABILITIES + ["texture", "ngon"])
pending_objects = {}
pending_transfers = []
camera_pivot = None
last_camera = None
viewport_area_pointer = 0
update_required = ""
known_objects = {}
membership_ready = False
dirty_objects = {}
config_revision = -1
applying_config = False
config_desired = None
config_sent = None
active_source = "none"
claim_pending = False
activity_watch_running = False
activity_watch_operator = None
stale_objects = set()
stale_requested = {}
pairing_wait = False
force_full_ids = set()
sent_geometry = set()
delta_cache = {}
remote_capabilities = set()
session_devices = []  # connected device names, the Nomad host first (relayed peers after)
session_source = ""  # client_name of the device sending live edits (may be this Blender)
texture_images = {}  # texture_id -> Image datablock; ids are immutable pixel content
texture_requested = set()  # ids with an in-flight request_texture
pending_materials = {}  # link_id -> material settings waiting for texture blobs
sent_textures = set()  # ids whose blob already went to Nomad this session

VIEWPORT_SENSOR_WIDTH = 36.0
VIEWPORT_ZOOM = 2.0
SAFE_WRITE_MODES = {"OBJECT", "VERTEX_PAINT", "WEIGHT_PAINT", "TEXTURE_PAINT"}
STALE_MESSAGE = "Live geometry paused; it refreshes automatically outside Edit Mode, Dyntopo, and Multires"
MODIFIER_MESSAGE = "Objects sending their modifier results cannot receive; turn off Send Modifier Results"
LAYER_DTYPE = numpy.dtype([("index", "<u4"), ("offset", "<f4", 3)])


def preferences():
    addon = bpy.context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def supported_object(obj):
    return obj.type in {"MESH", "LIGHT", "CAMERA"} and not obj.get(TRANSFORM_PARENT_ID)


def channel_enabled(scene, obj):
    if obj.type == "MESH":
        return scene.nomad_link_sync_objects
    if obj.type == "LIGHT":
        return scene.nomad_link_sync_lights
    return scene.nomad_link_sync_cameras


def sends_live_scene(scene):
    return scene.nomad_link_live_sync and active_source == "client"


def receives_live_scene(scene):
    return scene.nomad_link_live_sync and active_source == "nomad"


def sends_view(scene):
    return scene.nomad_link_sync_view and sends_live_scene(scene)


def receives_view(scene):
    return scene.nomad_link_sync_view and receives_live_scene(scene)


def wants_blender_source(scene):
    return scene.nomad_link_live_sync and (
        scene.nomad_link_sync_mode == "CLIENT"
        or (
            scene.nomad_link_sync_mode == "AUTO"
            and (active_source == "client" or claim_pending)
        )
    )


def claim_blender_source():
    global claim_pending
    scene = bpy.context.scene
    if (
        connection.status != "Connected"
        or not scene.nomad_link_live_sync
        or scene.nomad_link_sync_mode == "NOMAD"
        or active_source == "client"
        or claim_pending
    ):
        return
    if stale_objects:
        connection.error = "Blender sends once the linked meshes finish refreshing"
        return
    claim_pending = connection.send({"type": "claim_sync", "source": "client"})


def ensure_link_id(obj):
    link_id = obj.get(MESH_ID)
    duplicate = link_id and any(
        other != obj and other.get(MESH_ID) == link_id for other in bpy.data.objects
    )
    if not link_id or duplicate:
        link_id = str(uuid.uuid4())
        obj[MESH_ID] = link_id
    return link_id


def ensure_geometry_id(mesh):
    gid = mesh.get(GEOMETRY_ID)
    duplicate = gid and any(
        other is not mesh and other.get(GEOMETRY_ID) == gid for other in bpy.data.meshes
    )
    if not gid or duplicate:
        gid = str(uuid.uuid4())
        mesh[GEOMETRY_ID] = gid
    return gid


def geometry_sibling(obj):
    """Another linked object sharing this object's mesh datablock (a Blender instance)."""
    for other in bpy.data.objects:
        if other != obj and other.type == "MESH" and other.data == obj.data and other.get(MESH_ID):
            return other
    return None


def find_geometry_object(gid):
    if not gid:
        return None
    for other in bpy.data.objects:
        if other.type == "MESH" and other.get(MESH_ID) and other.data.get(GEOMETRY_ID) == gid:
            return other
    return None


def remember_object(obj):
    known_objects[obj.as_pointer()] = (ensure_link_id(obj), obj.type)


def matrix_from_columns(values):
    return Matrix(tuple(tuple(values[column * 4 + row] for column in range(4)) for row in range(4)))


def matrix_to_columns(matrix):
    return [matrix[row][column] for column in range(4) for row in range(4)]


def find_linked_object(mesh_id):
    for obj in bpy.data.objects:
        if obj.get(MESH_ID) == mesh_id:
            return obj
    return None


def apply_object_transform(obj, header):
    generated_parent = (
        obj.parent
        if obj.parent is not None and obj.parent.get(TRANSFORM_PARENT_ID) == obj.get(MESH_ID)
        else None
    )
    if obj.type != "MESH":
        # Lights and cameras aim along their local -Z in both applications, so their
        # frames map as world geometry; only meshes conjugate (vertices are swizzled).
        if generated_parent is not None:
            obj.parent = None
            bpy.data.objects.remove(generated_parent, do_unlink=True)
        obj.matrix_world = TO_BLENDER @ matrix_from_columns(header["world_matrix"])
        return
    world = TO_BLENDER @ matrix_from_columns(header["world_matrix"]) @ TO_NOMAD
    if "world_matrix_parent" not in header or "local_matrix" not in header:
        if generated_parent is not None:
            obj.parent = None
            bpy.data.objects.remove(generated_parent, do_unlink=True)
        obj.matrix_world = world
        return

    if generated_parent is None:
        generated_parent = bpy.data.objects.new(f"{obj.name} Transform", None)
        collection = obj.users_collection[0] if obj.users_collection else bpy.context.collection
        collection.objects.link(generated_parent)
        generated_parent.empty_display_type = "PLAIN_AXES"
        generated_parent[TRANSFORM_PARENT_ID] = obj.get(MESH_ID)
        obj.parent = generated_parent
    parent = TO_BLENDER @ matrix_from_columns(header["world_matrix_parent"]) @ TO_NOMAD
    local = TO_BLENDER @ matrix_from_columns(header["local_matrix"]) @ TO_NOMAD
    generated_parent.matrix_world = parent
    obj.matrix_parent_inverse = Matrix.Identity(4)
    obj.matrix_basis = local


def sends_evaluated(obj):
    """True when Nomad gets the modifier results instead of obj.data. Those objects never receive:
    the evaluated mesh cannot be written back, and obj.data would re-run the stack on top."""
    return bool(getattr(obj, "modifiers", None)) and bpy.context.scene.nomad_link_send_modifiers


def geometry_write_mode(obj):
    """How obj.data arrays may be written right now: "safe", "sculpt", or None (unsafe)."""
    if obj.mode in SAFE_WRITE_MODES:
        return "safe"
    if obj.mode != "SCULPT" or obj.use_dynamic_topology_sculpting:
        return None
    if any(modifier.type == "MULTIRES" for modifier in obj.modifiers):
        return None
    return "sculpt"


def refresh_sculpt_session(obj):
    obj.update_tag(refresh={"DATA"})
    if obj.mode == "SCULPT" and bpy.context.view_layer.objects.active == obj:
        try:
            bpy.ops.sculpt.optimize()
        except RuntimeError:
            pass


def enter_object_mode(obj):
    """Leave a mode that cannot survive a topology rebuild; returns the mode to restore, or None."""
    if obj.mode == "OBJECT":
        return "OBJECT"
    if obj.mode == "EDIT" or bpy.context.view_layer.objects.active != obj:
        return None
    previous = obj.mode
    try:
        bpy.ops.object.mode_set(mode="OBJECT")
    except RuntimeError:
        return None
    return previous if obj.mode == "OBJECT" else None


def restore_object_mode(obj, previous):
    if previous in (None, "OBJECT") or bpy.context.view_layer.objects.active != obj:
        return
    try:
        bpy.ops.object.mode_set(mode=previous)
    except RuntimeError:
        pass


def read_array(binary, offset, count, dtype):
    dtype = numpy.dtype(dtype)
    offset = int(offset)
    if offset < 0 or count < 0 or offset + count * dtype.itemsize > len(binary):
        raise ValueError("Invalid mesh attribute offset")
    return numpy.frombuffer(binary, dtype=dtype, count=count, offset=offset)


def read_unit_array(binary, offset, count, dtype, scale):
    return read_array(binary, offset, count, dtype).astype(numpy.float32) / scale


# Nomad's native vertex color bytes (rgbm8): linear rgb = rgb * (m / 65025)
def decode_rgbm(binary, offset, count):
    packed = read_array(binary, offset, count * 4, "u1").reshape(-1, 4).astype(numpy.float32)
    return packed[:, :3] * (packed[:, 3:4] / 65025.0)


def encode_rgbm(rgb):
    rgb = numpy.clip(rgb.astype(numpy.float32), 0.0, 1.0)
    m = numpy.clip(numpy.ceil(rgb.max(axis=1) * 255.0), 1.0, 255.0)
    scaled = rgb * (65025.0 / m)[:, None] + 0.5
    return numpy.concatenate((scaled, m[:, None] + 0.5), axis=1).astype("u1")


def read_alpha(header, binary, count):
    if "opacity_offset" not in header:
        return None
    return read_unit_array(binary, header["opacity_offset"], count, "u1", 255.0)


def to_blender_vectors(values):
    return numpy.column_stack((values[:, 0], -values[:, 2], values[:, 1]))


def to_nomad_vectors(values):
    return numpy.column_stack((values[:, 0], values[:, 2], -values[:, 1]))


def foreach_get(collection, attribute, components=1, dtype=numpy.float32):
    values = numpy.empty(len(collection) * components, dtype)
    collection.foreach_get(attribute, values)
    return values.reshape(-1, components) if components > 1 else values


def pack_unit(values, scale, dtype):
    return numpy.round(numpy.clip(values, 0.0, 1.0).astype(numpy.float64) * scale).astype(dtype)


def set_float_attribute(mesh, name, values):
    previous = mesh.attributes.get(name)
    if previous is not None:
        mesh.attributes.remove(previous)
    attribute = mesh.attributes.new(name=name, type="FLOAT", domain="POINT")
    attribute.data.foreach_set("value", values)


def set_mapping_transform(mapping, offset, scale, rotation):
    sine = math.sin(rotation)
    cosine = math.cos(rotation)
    mapping.inputs["Location"].default_value = (
        offset[0] + sine * scale[1],
        1.0 - cosine * scale[1] - offset[1],
        0.0,
    )
    mapping.inputs["Rotation"].default_value = (0.0, 0.0, rotation)
    mapping.inputs["Scale"].default_value = (scale[0], scale[1], 1.0)


def connect_node(links, value, socket):
    if hasattr(value, "node"):
        links.new(value, socket)
    else:
        socket.default_value = value


def math_node(nodes, links, operation, first, second, location=(0.0, 0.0)):
    node = nodes.new("ShaderNodeMath")
    node.operation = operation
    node.location = location
    connect_node(links, first, node.inputs[0])
    connect_node(links, second, node.inputs[1])
    return node.outputs[0]


def vector_node(nodes, links, operation, first, second, location=(0.0, 0.0)):
    node = nodes.new("ShaderNodeVectorMath")
    node.operation = operation
    node.location = location
    connect_node(links, first, node.inputs[0])
    if second is not None:
        connect_node(links, second, node.inputs[1])
    return node.outputs["Vector"]


def triplanar_group(material, channel, image, entry, interpolation, extension):
    owner = material["nomad_group_owner"]
    group = bpy.data.node_groups.new(f"Nomad Triplanar {channel.title()}", "ShaderNodeTree")
    group["nomad_triplanar"] = True
    group["nomad_group_owner"] = owner
    offset = tuple(entry.get("offset", (0.0, 0.0)))[:2]
    scale = tuple(entry.get("scale", (1.0, 1.0)))[:2]
    hardness = entry.get("triplanar_hardness", (0.9, 0.9, 0.9))
    if isinstance(hardness, (int, float)):
        hardness = (hardness,) * 3
    hardness = tuple(hardness) or (0.9,)
    hardness = (hardness + (hardness[-1],) * 3)[:3]
    for name, socket_type, default in (
        ("Offset", "NodeSocketVector", (*offset, 0.0)),
        ("Scale", "NodeSocketVector", (*scale, 1.0)),
        ("Rotation", "NodeSocketFloat", float(entry.get("rotation", 0.0))),
        ("Hardness", "NodeSocketVector", hardness),
        ("Color", "NodeSocketColor", None),
        ("Alpha", "NodeSocketFloat", None),
    ):
        direction = "OUTPUT" if name in {"Color", "Alpha"} else "INPUT"
        socket = group.interface.new_socket(name=name, in_out=direction, socket_type=socket_type)
        if default is not None:
            socket.default_value = default

    nodes = group.nodes
    links = group.links
    group_input = nodes.new("NodeGroupInput")
    group_input.location = (-1560.0, -120.0)
    group_output = nodes.new("NodeGroupOutput")
    group_output.location = (960.0, 220.0)
    geometry = nodes.new("ShaderNodeNewGeometry")
    geometry.location = (-1560.0, 420.0)
    position = geometry.outputs["Position"]
    normal = geometry.outputs["Normal"]
    world = bool(entry.get("triplanar_world", True))
    if not world:
        local_position = nodes.new("ShaderNodeVectorTransform")
        local_position.vector_type = "POINT"
        local_position.convert_from = "WORLD"
        local_position.convert_to = "OBJECT"
        local_position.location = (-1380.0, 480.0)
        links.new(position, local_position.inputs["Vector"])
        position = local_position.outputs["Vector"]
        local_normal = nodes.new("ShaderNodeVectorTransform")
        local_normal.vector_type = "NORMAL"
        local_normal.convert_from = "WORLD"
        local_normal.convert_to = "OBJECT"
        local_normal.location = (-1380.0, 300.0)
        links.new(normal, local_normal.inputs["Vector"])
        normal = local_normal.outputs["Vector"]

    # Nomad's texture frame in one transform: the -90° X axis swizzle, the (-1,-1,1)
    # flip (a 180° Z rotation), and the +0.5 centering
    to_nomad = nodes.new("ShaderNodeMapping")
    to_nomad.vector_type = "POINT"
    to_nomad.label = "Nomad Frame"
    to_nomad.inputs["Location"].default_value = (0.5, 0.5, 0.5)
    to_nomad.inputs["Rotation"].default_value = (-math.pi / 2.0, 0.0, math.pi)
    to_nomad.location = (-1200.0, 480.0)
    links.new(position, to_nomad.inputs["Vector"])
    position_xyz = nodes.new("ShaderNodeSeparateXYZ")
    position_xyz.location = (-1020.0, 480.0)
    links.new(to_nomad.outputs["Vector"], position_xyz.inputs["Vector"])

    rotate_normal = nodes.new("ShaderNodeVectorRotate")
    rotate_normal.rotation_type = "X_AXIS"
    rotate_normal.inputs["Angle"].default_value = -math.pi / 2.0
    rotate_normal.location = (-1200.0, 240.0)
    links.new(normal, rotate_normal.inputs["Vector"])
    nomad_normal = rotate_normal.outputs["Vector"]

    # u flips on back faces like Nomad: sign = -n/|n| (0 on a zero component,
    # harmless because that plane's weight is zero there too)
    absolute = vector_node(nodes, links, "ABSOLUTE", nomad_normal, None, (-1020.0, 120.0))
    negated = vector_node(nodes, links, "MULTIPLY", nomad_normal, (-1.0, -1.0, -1.0), (-1020.0, 280.0))
    signs_xyz = nodes.new("ShaderNodeSeparateXYZ")
    signs_xyz.location = (-660.0, 280.0)
    links.new(
        vector_node(nodes, links, "DIVIDE", negated, absolute, (-840.0, 280.0)),
        signs_xyz.inputs["Vector"],
    )

    # Nomad's blend: w = |n| ^ (1 / (1 - hardness)), normalized
    absolute_xyz = nodes.new("ShaderNodeSeparateXYZ")
    absolute_xyz.location = (-840.0, 100.0)
    links.new(absolute, absolute_xyz.inputs["Vector"])
    softness = vector_node(
        nodes, links, "SUBTRACT", (1.0, 1.0, 1.0), group_input.outputs["Hardness"], (-1020.0, -80.0)
    )
    safe = vector_node(nodes, links, "MAXIMUM", softness, (0.0001, 0.0001, 0.0001), (-840.0, -80.0))
    exponents_xyz = nodes.new("ShaderNodeSeparateXYZ")
    exponents_xyz.location = (-480.0, -80.0)
    links.new(
        vector_node(nodes, links, "DIVIDE", (1.0, 1.0, 1.0), safe, (-660.0, -80.0)),
        exponents_xyz.inputs["Vector"],
    )
    powers = [
        math_node(
            nodes, links, "POWER",
            absolute_xyz.outputs[index], exponents_xyz.outputs[index],
            (-300.0, -40.0 - index * 90.0),
        )
        for index in range(3)
    ]
    weight_sum = math_node(
        nodes, links, "ADD",
        math_node(nodes, links, "ADD", powers[0], powers[1], (-120.0, -80.0)),
        powers[2],
        (-120.0, -180.0),
    )
    weights = [
        math_node(nodes, links, "DIVIDE", power, weight_sum, (60.0, -40.0 - index * 90.0))
        for index, power in enumerate(powers)
    ]

    # Nomad's uv transform with the image V flip folded in:
    # flip_y after (T + Rz(-r)·S) equals T' + Rz(r)·S' with T' = (Tx, 1-Ty), S' = (Sx, -Sy)
    uv_location = nodes.new("ShaderNodeVectorMath")
    uv_location.operation = "MULTIPLY_ADD"
    uv_location.location = (-660.0, -320.0)
    links.new(group_input.outputs["Offset"], uv_location.inputs[0])
    uv_location.inputs[1].default_value = (1.0, -1.0, 0.0)
    uv_location.inputs[2].default_value = (0.0, 1.0, 0.0)
    uv_scale = vector_node(
        nodes, links, "MULTIPLY", group_input.outputs["Scale"], (1.0, -1.0, 1.0), (-660.0, -480.0)
    )
    uv_rotation = nodes.new("ShaderNodeCombineXYZ")
    uv_rotation.location = (-660.0, -620.0)
    links.new(group_input.outputs["Rotation"], uv_rotation.inputs["Z"])

    colors = []
    alpha = None
    for index, (u, v) in enumerate((("Z", "Y"), ("X", "Z"), ("X", "Y"))):
        row = 620.0 - index * 280.0
        projected_u = math_node(
            nodes, links, "MULTIPLY",
            position_xyz.outputs[u], signs_xyz.outputs[index],
            (-480.0, row + 40.0),
        )
        combine = nodes.new("ShaderNodeCombineXYZ")
        combine.location = (-300.0, row)
        links.new(projected_u, combine.inputs["X"])
        links.new(position_xyz.outputs[v], combine.inputs["Y"])
        mapping = nodes.new("ShaderNodeMapping")
        mapping.vector_type = "POINT"
        mapping.label = "UV Transform"
        mapping.location = (-120.0, row)
        links.new(combine.outputs["Vector"], mapping.inputs["Vector"])
        links.new(uv_location.outputs["Vector"], mapping.inputs["Location"])
        links.new(uv_rotation.outputs["Vector"], mapping.inputs["Rotation"])
        links.new(uv_scale, mapping.inputs["Scale"])
        texture = nodes.new("ShaderNodeTexImage")
        texture["nomad_triplanar_image"] = True
        texture.image = image
        texture.projection = "FLAT"
        texture.interpolation = interpolation
        texture.extension = extension
        texture.location = (80.0, row)
        links.new(mapping.outputs["Vector"], texture.inputs["Vector"])
        weighted_color = nodes.new("ShaderNodeVectorMath")
        weighted_color.operation = "SCALE"
        weighted_color.location = (340.0, row)
        links.new(texture.outputs["Color"], weighted_color.inputs["Vector"])
        links.new(weights[index], weighted_color.inputs["Scale"])
        colors.append(weighted_color.outputs["Vector"])
        if alpha is None:
            alpha = math_node(
                nodes, links, "MULTIPLY", texture.outputs["Alpha"], weights[index], (560.0, row - 200.0)
            )
        else:
            fma = nodes.new("ShaderNodeMath")
            fma.operation = "MULTIPLY_ADD"
            fma.location = (560.0, row - 200.0)
            links.new(texture.outputs["Alpha"], fma.inputs[0])
            links.new(weights[index], fma.inputs[1])
            links.new(alpha, fma.inputs[2])
            alpha = fma.outputs[0]
    color = vector_node(
        nodes, links, "ADD",
        vector_node(nodes, links, "ADD", colors[0], colors[1], (560.0, 460.0)),
        colors[2],
        (760.0, 400.0),
    )
    links.new(color, group_output.inputs["Color"])
    links.new(alpha, group_output.inputs["Alpha"])
    group["nomad_triplanar_world"] = world
    return group


def find_texture_image(texture_id):
    image = texture_images.get(texture_id)
    if image is not None:
        try:
            image.name  # dead datablock probe
            return image
        except ReferenceError:
            del texture_images[texture_id]
    for image in bpy.data.images:
        if str(image.get("nomad_texture_id", "")) == texture_id:
            texture_images[texture_id] = image
            return image
    return None


def image_from_bytes(name, data):
    """Load the raw file bytes as a packed Image (Blender has no from-memory loader)."""
    suffix = Path(name).suffix.lower() or ".png"
    handle = tempfile.NamedTemporaryFile(prefix="nomad_link_", suffix=suffix, delete=False)
    try:
        handle.write(data)
        handle.close()
        image = bpy.data.images.load(handle.name)
        image.name = Path(name).stem or "Nomad Texture"
        image.pack()  # the pixels live in the .blend, the temp file can go
    finally:
        try:
            os.remove(handle.name)
        except OSError:
            pass
    return image


def image_bytes(image):
    """The original file bytes of an image, for the wire (no re-encode)."""
    if image is None:
        return None, ""
    packed = image.packed_file
    if packed is not None and packed.size:
        name = Path(image.filepath).name or image.name
        if not Path(name).suffix:
            name += ".png"
        return bytes(packed.data), name
    path = bpy.path.abspath(image.filepath) if image.filepath else ""
    if path and Path(path).is_file():
        return Path(path).read_bytes(), Path(path).name
    return None, ""  # generated/unsaved paint has no file to share


def ensure_texture_id(image):
    texture_id = str(image.get("nomad_texture_id", ""))
    if not texture_id:
        texture_id = uuid.uuid4().hex
        image["nomad_texture_id"] = texture_id
    texture_images[texture_id] = image
    return texture_id


def request_texture(texture_id):
    if texture_id in texture_requested:
        return
    texture_requested.add(texture_id)
    connection.send({"type": "request_texture", "texture_id": texture_id})


def apply_material_textures(material, shader, settings, link_id, has_uv):
    """Wire the Nomad texture channels into the Principled node graph; returns the set
    of channels an image node now feeds (a texture replaces the matching vertex paint).

    Ids missing from the blob cache are requested from Nomad; the material settings wait
    in pending_materials and are re-applied when the blobs arrive. Rows follow the
    Principled input order top to bottom so the links stay parallel.
    """
    textures = settings.get("textures", {})
    if not isinstance(textures, dict):
        textures = {}
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    missing = set()
    wired = set()
    triplanar_groups = {}

    def channel_node(channel, non_color, y, label=None):
        entry = textures.get(channel)
        if not isinstance(entry, dict):
            return None
        texture_id = str(entry.get("texture_id", ""))
        if not texture_id:
            return None
        image = find_texture_image(texture_id)
        if image is None:
            missing.add(texture_id)
            request_texture(texture_id)
            return None
        projection = str(entry.get("projection", "auto"))
        is_triplanar = projection == "triplanar" or (projection == "auto" and not has_uv)
        interpolation = "Closest" if entry.get("mag_filter") == "nearest" else "Linear"
        extension = {"repeat": "REPEAT", "clamp": "EXTEND", "mirror": "MIRROR"}.get(
            str(entry.get("wrap_s", "repeat")), "REPEAT"
        )
        if is_triplanar:
            tree = triplanar_groups.get(channel)
            if tree is None:
                tree = triplanar_group(material, channel, image, entry, interpolation, extension)
                triplanar_groups[channel] = tree
            node = nodes.new("ShaderNodeGroup")
            node.node_tree = tree
            node["nomad_triplanar"] = True
        else:
            node = nodes.new("ShaderNodeTexImage")
            node.image = image
            node.interpolation = interpolation
            node.extension = extension
        node.location = (-620.0, y)
        node.width = 220.0 if is_triplanar else 170.0
        node.label = label or channel.title()
        if label == "Color Alpha":
            node["nomad_color_alpha"] = True
        if non_color:
            image.colorspace_settings.name = "Non-Color"
        offset = tuple(entry.get("offset", (0.0, 0.0)))[:2]
        scale = tuple(entry.get("scale", (1.0, 1.0)))[:2]
        rotation = float(entry.get("rotation", 0.0))
        if not is_triplanar and (offset != (0.0, 0.0) or scale != (1.0, 1.0) or rotation):
            mapping = nodes.new("ShaderNodeMapping")
            mapping.location = (-800.0, y - 270.0)
            mapping.label = node.label
            set_mapping_transform(mapping, offset, scale, rotation)
            coords = nodes.new("ShaderNodeTexCoord")
            coords.location = (-980.0, y - 270.0)
            coords.label = node.label
            links.new(coords.outputs["UV"], mapping.inputs["Vector"])
            links.new(mapping.outputs["Vector"], node.inputs["Vector"])
        wired.add(channel)
        return node

    color = channel_node("color", False, 520.0)
    if color is not None:
        links.new(color.outputs["Color"], shader.inputs["Base Color"])
    node = channel_node("metalness", True, 220.0)
    if node is not None:
        links.new(node.outputs["Color"], shader.inputs["Metallic"])
    node = channel_node("roughness", True, -80.0)
    if node is not None:
        links.new(node.outputs["Color"], shader.inputs["Roughness"])
    color_alpha = (
        channel_node("color", False, -380.0, "Color Alpha")
        if settings.get("use_color_opacity_value", False)
        else None
    )
    opacity_y = -680.0 if color_alpha is not None else -380.0
    opacity = channel_node("opacity", True, opacity_y)
    if color_alpha is not None and opacity is not None:
        multiply = nodes.new("ShaderNodeMath")
        multiply.operation = "MULTIPLY"
        multiply.label = "Opacity"
        multiply.location = (-320.0, -530.0)
        links.new(color_alpha.outputs["Alpha"], multiply.inputs[0])
        links.new(opacity.outputs["Color"], multiply.inputs[1])
        links.new(multiply.outputs[0], shader.inputs["Alpha"])
    elif color_alpha is not None:
        links.new(color_alpha.outputs["Alpha"], shader.inputs["Alpha"])
    elif opacity is not None:
        links.new(opacity.outputs["Color"], shader.inputs["Alpha"])
    if color_alpha is not None or opacity is not None:
        material.surface_render_method = "DITHERED"
    normal_y = opacity_y - 300.0
    node = channel_node("normal", True, normal_y)
    if node is not None:
        normal_map = nodes.new("ShaderNodeNormalMap")
        normal_map.location = (-320.0, normal_y)
        normal_map.inputs["Strength"].default_value = float(textures.get("normal", {}).get("factor", 1.0))
        normal_map.convention = "DIRECTX" if textures.get("normal", {}).get("neg_y") else "OPENGL"
        links.new(node.outputs["Color"], normal_map.inputs["Color"])
        links.new(normal_map.outputs["Normal"], shader.inputs["Normal"])
    node = channel_node("emissive", False, normal_y - 300.0)
    if node is not None:
        links.new(node.outputs["Color"], shader.inputs["Emission Color"])
        shader.inputs["Emission Strength"].default_value = float(
            textures.get("emissive", {}).get("strength", 1.0)
        )
    # occlusion/displacement have no Principled slot; those channels stay Nomad-side

    if link_id:
        if missing:
            pending_materials[link_id] = settings
        else:
            pending_materials.pop(link_id, None)
    return wired


def make_material(mesh, header, has_color, has_roughness, has_metalness, link_id=""):
    settings = header.get("material", {})
    material = next((item for item in mesh.materials if item and item.get(MATERIAL_ID)), None)
    if material is None:
        if len(mesh.materials):
            return
        material = bpy.data.materials.new(f"{mesh.name} Material")
        material[MATERIAL_ID] = True
        mesh.materials.append(material)
    material.use_nodes = True
    material.diffuse_color = (*settings.get("color", (1.0, 1.0, 1.0)), settings.get("opacity", 1.0))
    nodes = material.node_tree.nodes
    if not material.get("nomad_group_owner"):
        material["nomad_group_owner"] = uuid.uuid4().hex
    # the sync rebuilds the tree from scratch: remember where the user put each node and
    # whether they uncollapsed it, so their arrangement survives every material update
    layout = {}
    if material.get("nomad_layout") == 4:
        for node in nodes:
            key = (node.bl_idname, node.label or getattr(node, "attribute_name", ""))
            layout[key] = (tuple(node.location), node.hide, node.width)
    material["nomad_layout"] = 4
    nodes.clear()
    owner = material["nomad_group_owner"]
    for group in list(bpy.data.node_groups):
        if group.get("nomad_group_owner") == owner:
            bpy.data.node_groups.remove(group)
    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (520.0, 0.0)
    shader = nodes.new("ShaderNodeBsdfPrincipled")
    shader.location = (80.0, 0.0)
    shader.width = 300.0
    shader.inputs["Base Color"].default_value = material.diffuse_color
    shader.inputs["Roughness"].default_value = float(settings.get("roughness", 0.25))
    shader.inputs["Metallic"].default_value = float(settings.get("metalness", 0.0))
    shader.inputs["Alpha"].default_value = float(settings.get("opacity", 1.0))
    shader.inputs["IOR"].default_value = float(settings.get("refraction_ior", 1.45))
    shader.inputs["Subsurface Weight"].default_value = (
        1.0 if settings.get("material_type") == "subsurface" else 0.0
    )
    shader.inputs["Subsurface Scale"].default_value = max(float(settings.get("subsurface_depth", 1.0)), 0.0)
    material.node_tree.links.new(shader.outputs["BSDF"], output.inputs["Surface"])

    # a texture replaces the matching vertex paint; the attributes only feed the
    # channels no image covers (a pending blob keeps its attribute until it lands)
    wired = apply_material_textures(material, shader, settings, link_id, bool(mesh.uv_layers))
    if has_color and "color" not in wired:
        node = nodes.new("ShaderNodeAttribute")
        node.location = (-320.0, 520.0)
        node.attribute_name = COLOR_ATTRIBUTE
        material.node_tree.links.new(node.outputs["Color"], shader.inputs["Base Color"])
        if "opacity" not in wired:
            material.node_tree.links.new(node.outputs["Alpha"], shader.inputs["Alpha"])
        material.surface_render_method = "DITHERED"
    if has_metalness and "metalness" not in wired:
        node = nodes.new("ShaderNodeAttribute")
        node.location = (-320.0, 220.0)
        node.attribute_name = METALNESS_ATTRIBUTE
        material.node_tree.links.new(node.outputs["Fac"], shader.inputs["Metallic"])
    if has_roughness and "roughness" not in wired:
        node = nodes.new("ShaderNodeAttribute")
        node.location = (-320.0, -80.0)
        node.attribute_name = ROUGHNESS_ATTRIBUTE
        material.node_tree.links.new(node.outputs["Fac"], shader.inputs["Roughness"])

    for node in nodes:
        key = (node.bl_idname, node.label or getattr(node, "attribute_name", ""))
        if key in layout:
            node.location, node.hide, node.width = layout[key]

    material["nomad_material_type"] = settings.get("material_type", "opaque")


def receive_mesh(header, binary):
    vertex_count = int(header["vertex_count"])
    face_count = int(header["face_count"])
    if int(header.get("binary_size", -1)) != len(binary):
        raise ValueError("Invalid mesh payload")

    positions = to_blender_vectors(read_array(binary, header["position_offset"], vertex_count * 3, "<f4").reshape(-1, 3))
    corners = header.get("face_format") == "corners"
    if corners:
        loop_totals = read_array(binary, header["face_size_offset"], face_count, "<i4")
        corner_count = int(header["corner_count"])
        if loop_totals.size and (loop_totals.min() < 3 or loop_totals.sum() != corner_count):
            raise ValueError("Invalid face payload")
        corner_mask = None
        corner_verts = read_array(binary, header["corner_vertex_offset"], corner_count, "<i4")
    else:
        faces = read_array(binary, header["face_offset"], face_count * 4, "<i4").reshape(-1, 4)
        corner_mask = numpy.ones((face_count, 4), bool)
        corner_mask[:, 3] = faces[:, 3] >= 0
        corner_verts = faces[corner_mask]
        loop_totals = corner_mask.sum(axis=1).astype(numpy.int32)
    if corner_verts.size and (corner_verts.min() < 0 or corner_verts.max() >= vertex_count):
        raise ValueError("Invalid face payload")
    loop_starts = numpy.zeros(face_count, numpy.int32)
    numpy.cumsum(loop_totals[:-1], out=loop_starts[1:])

    mesh_id = header["mesh_id"]
    obj = find_linked_object(mesh_id)
    if obj is not None and sends_evaluated(obj):
        connection.error = MODIFIER_MESSAGE
        return
    restore_mode = None
    if obj is not None and obj.mode != "OBJECT":
        restore_mode = enter_object_mode(obj)
        if restore_mode is None:
            if header.get("live_sync", False):
                stale_objects.add(mesh_id)
            raise RuntimeError("Leave Edit Mode on the linked object to receive the Nomad mesh")
    try:
        if obj is None:
            mesh = bpy.data.meshes.new(header.get("name", "Nomad Mesh"))
            obj = bpy.data.objects.new(header.get("name", "Nomad Mesh"), mesh)
            bpy.context.collection.objects.link(obj)
            obj[MESH_ID] = mesh_id
        else:
            mesh = obj.data
            if mesh.shape_keys:
                obj.shape_key_clear()
            mesh.clear_geometry()

        obj.name = header.get("name", obj.name)
        mesh.vertices.add(vertex_count)
        mesh.vertices.foreach_set("co", positions.astype(numpy.float32).ravel())
        mesh.loops.add(int(corner_verts.size))
        mesh.loops.foreach_set("vertex_index", corner_verts)
        mesh.polygons.add(face_count)
        mesh.polygons.foreach_set("loop_start", loop_starts)
        mesh.polygons.foreach_set("loop_total", loop_totals)

        texcoord_count = int(header.get("texcoord_count", 0))
        if texcoord_count:
            texcoords = read_array(binary, header["texcoord_offset"], texcoord_count * 2, "<f4").reshape(-1, 2)
            # Nomad's v origin is top-left (glTF style), Blender's bottom-left
            texcoords = numpy.column_stack((texcoords[:, 0], 1.0 - texcoords[:, 1]))
            if corners:
                corner_uvs = read_array(binary, header["corner_texcoord_offset"], len(corner_verts), "<i4")
            else:
                face_uv = read_array(binary, header["face_uv_offset"], face_count * 4, "<i4").reshape(-1, 4)
                corner_uvs = face_uv[corner_mask]
            if corner_uvs.size and (corner_uvs.min() < 0 or corner_uvs.max() >= texcoord_count):
                raise ValueError("Invalid UV payload")
            uv_layer = mesh.uv_layers.new(name="UVMap")
            uv_layer.data.foreach_set("uv", texcoords[corner_uvs].ravel())

        has_color = "color_offset" in header
        if has_color:
            rgb = decode_rgbm(binary, header["color_offset"], vertex_count)
            alpha = read_alpha(header, binary, vertex_count)
            if alpha is None:
                alpha = numpy.ones(vertex_count, numpy.float32)
            colors = numpy.column_stack((rgb, alpha))
            previous = mesh.color_attributes.get(COLOR_ATTRIBUTE)
            if previous is not None:
                mesh.color_attributes.remove(previous)
            attribute = mesh.color_attributes.new(name=COLOR_ATTRIBUTE, type="FLOAT_COLOR", domain="POINT")
            attribute.data.foreach_set("color", colors.ravel())

        has_roughness = "roughness_offset" in header
        if has_roughness:
            set_float_attribute(mesh, ROUGHNESS_ATTRIBUTE, read_unit_array(binary, header["roughness_offset"], vertex_count, "u1", 255.0))

        has_metalness = "metalness_offset" in header
        if has_metalness:
            set_float_attribute(mesh, METALNESS_ATTRIBUTE, read_unit_array(binary, header["metalness_offset"], vertex_count, "u1", 255.0))

        if "mask_offset" in header:
            set_sculpt_mask(mesh, 1.0 - read_unit_array(binary, header["mask_offset"], vertex_count, "<u2", 65535.0))

        if "face_group_offset" in header:
            values = read_array(binary, header["face_group_offset"], face_count, "<u2")
            set_face_groups(mesh, values.astype(numpy.int32))
            mesh["nomad_face_groups"] = json.dumps(header.get("face_groups", []))

        if "face_hidden_offset" in header:
            set_hidden_faces(mesh, read_array(binary, header["face_hidden_offset"], face_count, "u1") != 0)

        mesh.update(calc_edges=True)
        if "material" in header:
            make_material(mesh, header, has_color, has_roughness, has_metalness, link_id=mesh_id)
        if "smooth_shading" in header or "material" in header:
            smooth = bool(header.get("smooth_shading", False)) # top-level = resolved boolean
            mesh.polygons.foreach_set("use_smooth", [smooth] * len(mesh.polygons))

        layers = header.get("layers", [])
        if len(layers) > 256:
            raise ValueError("Too many sculpt layers")
        if layers:
            obj.shape_key_add(name="Basis")
            for layer in layers:
                key = obj.shape_key_add(name=layer.get("name") or "Nomad Layer")
                factor = float(layer.get("factor", 0.0)) * float(layer.get("factor_offset", 1.0))
                key.slider_min = min(-1.0, factor)
                key.slider_max = max(1.0, factor)
                key.value = factor
                key.mute = not (layer.get("visible", True) and layer.get("visible_offset", True))
                records = read_array(binary, layer.get("offset", -1), int(layer.get("count", 0)), LAYER_DTYPE)
                if records.size and records["index"].max() >= vertex_count:
                    raise ValueError("Invalid layer payload")
                key_positions = positions.copy()
                numpy.add.at(key_positions, records["index"], to_blender_vectors(records["offset"]))
                key.data.foreach_set("co", key_positions.astype(numpy.float32).ravel())
            active = int(header.get("layer_active", -1))  # -1 = base, key 0 = Basis
            obj.active_shape_key_index = min(max(active + 1, 0), len(layers))

        if "geometry_id" in header:
            mesh[GEOMETRY_ID] = header["geometry_id"]
            delta_cache.pop(header["geometry_id"], None)
            sent_geometry.add(header["geometry_id"])
        apply_object_transform(obj, header)
        remember_object(obj)
        dirty_objects.pop(obj.as_pointer(), None)
        stale_objects.discard(mesh_id)
        stale_requested.pop(mesh_id, None)
        if not header.get("live_sync", False):
            bpy.context.view_layer.objects.active = obj
            obj.select_set(True)
    finally:
        restore_object_mode(obj, restore_mode)


def receive_delta(header, binary):
    mesh_id = header["mesh_id"]
    obj = find_linked_object(mesh_id)
    if obj is not None:
        delta_cache.pop(obj.data.get(GEOMETRY_ID, ""), None)
    if obj is None:
        connection.send({"type": "request_mesh", "request_id": uuid.uuid4().hex, "link_id": mesh_id})
        return
    if sends_evaluated(obj):
        connection.error = MODIFIER_MESSAGE
        return
    write_mode = geometry_write_mode(obj)
    if write_mode is None:
        stale_objects.add(mesh_id)
        connection.error = STALE_MESSAGE
        return
    if mesh_id in stale_objects:
        connection.error = "Waiting for a fresh Nomad mesh before applying live updates"
        return
    count = int(header["count"])
    if count < 0 or int(header.get("binary_size", -1)) != len(binary):
        raise ValueError("Invalid mesh delta")
    mesh = obj.data
    indices = read_array(binary, header["index_offset"], count, "<u4").astype(numpy.intp)
    valid = indices < len(mesh.vertices)
    keys = mesh.shape_keys.key_blocks if mesh.shape_keys else ()
    if "position_offset" in header:
        targets = to_blender_vectors(read_array(binary, header["position_offset"], count * 3, "<f4").reshape(-1, 3))
        targets, moved = targets[valid], indices[valid]
        if keys:
            key_positions = [foreach_get(key.data, "co", 3) for key in keys]
            basis = key_positions[0][moved]
            visible_offset = numpy.zeros_like(targets)
            for key, positions in zip(keys[1:], key_positions[1:]):
                if not key.mute:
                    visible_offset += (positions[moved] - basis) * key.value
            delta = targets - visible_offset - basis
            for key, positions in zip(keys, key_positions):
                positions[moved] += delta
                key.data.foreach_set("co", positions.ravel())
        else:
            positions = foreach_get(mesh.vertices, "co", 3)
            positions[moved] = targets
            mesh.vertices.foreach_set("co", positions.ravel())

    if "mask_offset" in header:
        incoming = read_unit_array(binary, header["mask_offset"], count, "<u2", 65535.0)
        mask = sculpt_mask(mesh)
        if mask is None:
            mask = numpy.zeros(len(mesh.vertices), numpy.float32)
        mask[indices[valid]] = 1.0 - incoming[valid]
        set_sculpt_mask(mesh, mask)

    if "color_offset" in header:
        rgb = decode_rgbm(binary, header["color_offset"], count)
        alpha = read_alpha(header, binary, count)
        attribute = mesh.color_attributes.get(COLOR_ATTRIBUTE)
        if attribute is None:
            attribute = mesh.color_attributes.new(name=COLOR_ATTRIBUTE, type="FLOAT_COLOR", domain="POINT")
        if attribute.domain == "POINT":
            colors = foreach_get(attribute.data, "color", 4)
            colors[indices[valid], :3] = rgb[valid]
            if alpha is not None:
                colors[indices[valid], 3] = alpha[valid]
            attribute.data.foreach_set("color", colors.ravel())

    for offset_name, attribute_name in (
        ("roughness_offset", ROUGHNESS_ATTRIBUTE),
        ("metalness_offset", METALNESS_ATTRIBUTE),
    ):
        if offset_name not in header:
            continue
        packed = read_unit_array(binary, header[offset_name], count, "u1", 255.0)
        attribute = mesh.attributes.get(attribute_name)
        if attribute is None:
            attribute = mesh.attributes.new(name=attribute_name, type="FLOAT", domain="POINT")
        if attribute.domain == "POINT" and attribute.data_type == "FLOAT":
            values = foreach_get(attribute.data, "value")
            values[indices[valid]] = packed[valid]
            attribute.data.foreach_set("value", values)

    mesh.update()
    if "world_matrix" in header:
        apply_object_transform(obj, header)
    remember_object(obj)
    if write_mode == "sculpt":
        refresh_sculpt_session(obj)


def receive_attributes(header, binary):
    mesh_id = header["mesh_id"]
    obj = find_linked_object(mesh_id)
    if obj is not None:
        delta_cache.pop(obj.data.get(GEOMETRY_ID, ""), None)
    if obj is None:
        connection.send({"type": "request_mesh", "request_id": uuid.uuid4().hex, "link_id": mesh_id})
        return
    if sends_evaluated(obj):
        connection.error = MODIFIER_MESSAGE
        return
    write_mode = geometry_write_mode(obj)
    if write_mode is None:
        stale_objects.add(mesh_id)
        connection.error = STALE_MESSAGE
        return
    if mesh_id in stale_objects:
        connection.error = "Waiting for a fresh Nomad mesh before applying live updates"
        return
    vertex_count = int(header["vertex_count"])
    if vertex_count != len(obj.data.vertices) or int(header.get("binary_size", -1)) != len(binary):
        raise ValueError("Invalid mesh attributes")

    if "color_offset" in header:
        rgb = decode_rgbm(binary, header["color_offset"], vertex_count)
        alpha = read_alpha(header, binary, vertex_count)
        if alpha is None:
            alpha = numpy.ones(vertex_count, numpy.float32)
        colors = numpy.column_stack((rgb, alpha))
        attribute = obj.data.color_attributes.get(COLOR_ATTRIBUTE)
        if attribute is None:
            attribute = obj.data.color_attributes.new(
                name=COLOR_ATTRIBUTE, type="FLOAT_COLOR", domain="POINT"
            )
        if attribute.domain == "POINT":
            attribute.data.foreach_set("color", colors.ravel())

    for offset_name, attribute_name in (
        ("roughness_offset", ROUGHNESS_ATTRIBUTE),
        ("metalness_offset", METALNESS_ATTRIBUTE),
    ):
        if offset_name not in header:
            continue
        values = read_unit_array(binary, header[offset_name], vertex_count, "u1", 255.0)
        attribute = obj.data.attributes.get(attribute_name)
        if attribute is None:
            attribute = obj.data.attributes.new(name=attribute_name, type="FLOAT", domain="POINT")
        if attribute.domain == "POINT" and attribute.data_type == "FLOAT":
            attribute.data.foreach_set("value", values)

    layers = header.get("layers", [])
    keys = obj.data.shape_keys.key_blocks if obj.data.shape_keys else ()
    if len(keys) == len(layers) + 1:
        for key, layer in zip(keys[1:], layers):
            factor = float(layer.get("factor", 0.0)) * float(layer.get("factor_offset", 1.0))
            key.name = layer.get("name") or "Nomad Layer"
            key.slider_min = min(-1.0, factor)
            key.slider_max = max(1.0, factor)
            key.value = factor
            key.mute = not (layer.get("visible", True) and layer.get("visible_offset", True))
    obj.data.update()
    remember_object(obj)
    if write_mode == "sculpt":
        refresh_sculpt_session(obj)


def find_view3d():
    fallback = None
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((item for item in area.regions if item.type == "WINDOW"), None)
            if region is None:
                continue
            if fallback is None:
                fallback = (area.spaces.active, region)
            if area.as_pointer() == viewport_area_pointer:
                return area.spaces.active, region
    return fallback


def viewport_fit(region):
    return max(region.width / max(region.height, 1), 1.0)


def viewport_fovy(space, region):
    tan_half = VIEWPORT_SENSOR_WIDTH / max(space.lens * viewport_fit(region), 0.001)
    return 2.0 * math.atan(tan_half)


def viewport_ortho_scale(space, view, region):
    return (
        view.view_distance
        * VIEWPORT_SENSOR_WIDTH
        * VIEWPORT_ZOOM
        / max(space.lens * viewport_fit(region), 0.001)
    )


def set_viewport_projection(space, view, region, fovy, orthographic, ortho_scale):
    fit = viewport_fit(region)
    tan_half = max(math.tan(fovy * 0.5), 0.001)
    space.lens = VIEWPORT_SENSOR_WIDTH / (tan_half * fit)
    view.view_perspective = "ORTHO" if orthographic else "PERSP"
    if orthographic:
        view.view_distance = (
            ortho_scale * space.lens * fit / (VIEWPORT_SENSOR_WIDTH * VIEWPORT_ZOOM)
        )


def render_aspect(scene):
    render = scene.render
    return max(
        render.resolution_x * render.pixel_aspect_x
        / max(render.resolution_y * render.pixel_aspect_y, 0.001),
        0.001,
    )


def camera_vertical_fit(data, scene):
    if data.sensor_fit == "VERTICAL":
        return True
    if data.sensor_fit == "HORIZONTAL":
        return False
    return render_aspect(scene) < 1.0


def camera_fovy(data, scene):
    sensor = data.sensor_height if data.sensor_fit == "VERTICAL" else data.sensor_width
    fit = 1.0 if camera_vertical_fit(data, scene) else render_aspect(scene)
    return 2.0 * math.atan(sensor / max(2.0 * data.lens * fit, 0.001))


def camera_ortho_scale(data, scene):
    return data.ortho_scale if camera_vertical_fit(data, scene) else data.ortho_scale / render_aspect(scene)


def receive_instance(header):
    mesh_id = header["mesh_id"]
    source = find_geometry_object(header.get("geometry_id", ""))
    if source is None:
        connection.send({"type": "request_mesh", "request_id": uuid.uuid4().hex, "link_id": mesh_id})
        return
    obj = find_linked_object(mesh_id)
    if obj is None:
        obj = bpy.data.objects.new(header.get("name", source.name), source.data)
        bpy.context.collection.objects.link(obj)
        obj[MESH_ID] = mesh_id
    elif obj.data != source.data:
        if obj.mode != "OBJECT":
            stale_objects.add(mesh_id)
            connection.error = STALE_MESSAGE
            return
        if obj.data.shape_keys:
            obj.shape_key_clear()
        obj.data = source.data
    apply_object_state(obj, header)
    sent_geometry.add(header.get("geometry_id", ""))
    dirty_objects.pop(obj.as_pointer(), None)
    stale_objects.discard(mesh_id)
    stale_requested.pop(mesh_id, None)


def receive_camera(header):
    global camera_pivot, last_camera
    scene = bpy.context.scene
    if not receives_view(scene):
        return
    if claim_pending:
        # takeover in flight: trailing frames from the previous sender would
        # yank the view mid-gesture (visible as a jump on the first move)
        return
    matrix = TO_BLENDER @ matrix_from_columns(header["world_from_view"])
    camera_pivot = (TO_BLENDER @ Vector((*header["pivot"], 1.0))).to_3d()
    orthographic = header.get("orthographic", False)
    ortho_scale = max(float(header.get("ortho_scale", 1.0)), 0.001)
    fov = math.radians(float(header.get("fov_y", 50.0)))
    if scene.nomad_link_camera_target == "VIEWPORT":
        viewport = find_view3d()
        if viewport is None:
            return
        space, region = viewport
        view = space.region_3d
        view.view_rotation = matrix.to_quaternion()
        view.view_location = camera_pivot
        view.view_distance = max((matrix.translation - camera_pivot).length, 0.001)
        set_viewport_projection(space, view, region, fov, orthographic, ortho_scale)
    else:
        if scene.camera is None:
            data = bpy.data.cameras.new("Nomad Camera")
            scene.camera = bpy.data.objects.new("Nomad Camera", data)
            scene.collection.objects.link(scene.camera)
        camera = scene.camera
        if orthographic:
            distance = ortho_scale / max(2.0 * math.tan(fov * 0.5), 0.001)
            matrix.translation = camera_pivot + matrix.col[2].xyz * distance
        camera.matrix_world = matrix
        camera.data.sensor_fit = "VERTICAL"
        camera.data.lens = camera.data.sensor_height / max(2.0 * math.tan(fov * 0.5), 0.001)
        if orthographic:
            camera.data.type = "ORTHO"
            camera.data.ortho_scale = ortho_scale
        else:
            camera.data.type = "PERSP"
    last_camera = camera_signature()


def camera_signature():
    scene = bpy.context.scene
    if scene.nomad_link_camera_target == "VIEWPORT":
        viewport = find_view3d()
        if viewport is None:
            return None
        space, region = viewport
        view = space.region_3d
        return tuple(round(value, 6) for row in view.view_matrix for value in row) + (
            *(round(value, 6) for value in view.view_location),
            round(view.view_distance, 6),
            view.view_perspective,
            round(space.lens, 6),
            region.width,
            region.height,
        )
    camera = scene.camera
    if camera is None:
        return None
    return tuple(round(value, 6) for row in camera.matrix_world for value in row) + (
        camera.data.type,
        round(camera.data.lens, 6),
        round(camera.data.ortho_scale, 6),
        camera.data.sensor_fit,
        round(camera.data.sensor_width, 6),
        round(camera.data.sensor_height, 6),
        round(render_aspect(scene), 6),
    )


def send_camera():
    global last_camera
    scene = bpy.context.scene
    if not scene.nomad_link_sync_view or not wants_blender_source(scene):
        return
    signature = camera_signature()
    if signature is None:
        return
    if signature == last_camera:
        return
    if not sends_view(scene):
        return
    last_camera = signature

    if scene.nomad_link_camera_target == "VIEWPORT":
        space, region = find_view3d()
        view = space.region_3d
        world_from_view = view.view_matrix.inverted()
        pivot = view.view_location
        fov_y = math.degrees(viewport_fovy(space, region))
        orthographic = view.view_perspective == "ORTHO"
        ortho_scale = viewport_ortho_scale(space, view, region)
    else:
        camera = scene.camera
        world_from_view = camera.matrix_world
        pivot = camera_pivot
        if pivot is None:
            pivot = (
                camera.matrix_world.translation
                - camera.matrix_world.col[2].xyz * camera.data.dof.focus_distance
            )
        fov_y = math.degrees(camera_fovy(camera.data, scene))
        orthographic = camera.data.type == "ORTHO"
        ortho_scale = camera_ortho_scale(camera.data, scene)

    matrix = TO_NOMAD @ world_from_view
    nomad_pivot = (TO_NOMAD @ pivot.to_4d()).to_3d()
    header = {
        "type": "camera",
        "world_from_view": matrix_to_columns(matrix),
        "pivot": list(nomad_pivot),
        "fov_y": fov_y,
        "orthographic": orthographic,
        "ortho_scale": ortho_scale,
        "coordinate_system": "nomad_y_up",
    }
    connection.send(header)


def active_principled(obj):
    material = obj.active_material
    if material and material.use_nodes:
        for node in material.node_tree.nodes:
            if node.type == "BSDF_PRINCIPLED":
                return material, node
    return material, None


def shader_input(shader, name, default):
    return shader.inputs[name].default_value if shader else default


def linked_images(shader, name):
    """Texture nodes feeding a shader input, walked through small wrappers."""
    images = []
    socket = shader.inputs.get(name)
    if socket is None or not socket.is_linked:
        return images
    queue = [socket.links[0].from_node]
    for _ in range(16):
        if not queue:
            break
        node = queue.pop(0)
        if (node.type == "TEX_IMAGE" and node.image is not None) or node.get("nomad_triplanar"):
            if node not in images:
                images.append(node)
            continue
        for input_socket in node.inputs:
            if input_socket.is_linked:
                queue.append(input_socket.links[0].from_node)
    return images


def linked_image(shader, name):
    images = linked_images(shader, name)
    return images[0] if images else None


def image_mapping(image_node):
    socket = image_node.inputs.get("Vector") if image_node and image_node.type == "TEX_IMAGE" else None
    if socket and socket.is_linked and socket.links[0].from_node.type == "MAPPING":
        return socket.links[0].from_node
    return None


def texture_sample_node(texture_node):
    if texture_node is None:
        return None
    if texture_node.type == "TEX_IMAGE":
        return texture_node
    if texture_node.get("nomad_triplanar") and texture_node.node_tree:
        return next(
            (node for node in texture_node.node_tree.nodes if node.get("nomad_triplanar_image")),
            None,
        )
    return None


def material_textures(shader):
    """The texture channels Blender models, as blob references; queues unsent blobs.

    A channel without an image sends the explicit {} clear; channels Blender cannot
    represent (occlusion, displacement) stay absent so Nomad keeps them.
    """
    if shader is None or "texture" not in remote_capabilities:
        return None, None
    color_image = linked_image(shader, "Base Color")
    alpha_images = linked_images(shader, "Alpha")
    color_alpha = [node for node in alpha_images if node == color_image or node.get("nomad_color_alpha")]
    use_color_opacity = bool(color_alpha)
    opacity_image = next((node for node in alpha_images if node not in color_alpha), None)
    textures = {}
    for channel, name in (
        ("color", "Base Color"),
        ("roughness", "Roughness"),
        ("metalness", "Metallic"),
        ("normal", "Normal"),
        ("emissive", "Emission Color"),
        ("opacity", "Alpha"),
    ):
        image_node = color_image if channel == "color" else linked_image(shader, name)
        if channel == "opacity":
            image_node = opacity_image
        if channel == "opacity" and image_node is None and use_color_opacity:
            textures[channel] = {}
            continue
        sample_node = texture_sample_node(image_node)
        image = sample_node.image if sample_node else None
        data, name = image_bytes(image)
        if data is None:
            textures[channel] = {}  # no image (or nothing shareable): explicit clear
            continue
        texture_id = ensure_texture_id(image)
        if texture_id not in sent_textures:
            # the blob rides the same ordered queue, ahead of the message referencing it
            connection.send(
                {"type": "texture", "texture_id": texture_id, "name": name, "binary_size": len(data)},
                data,
            )
            sent_textures.add(texture_id)
        entry = {"texture_id": texture_id, "name": name}
        exact_triplanar = bool(image_node.get("nomad_triplanar"))
        if exact_triplanar:
            entry["projection"] = "triplanar"
            entry["offset"] = [float(value) for value in image_node.inputs["Offset"].default_value[:2]]
            entry["scale"] = [float(value) for value in image_node.inputs["Scale"].default_value[:2]]
            entry["rotation"] = float(image_node.inputs["Rotation"].default_value)
            entry["triplanar_hardness"] = [
                float(value) for value in image_node.inputs["Hardness"].default_value[:3]
            ]
            entry["triplanar_world"] = bool(image_node.node_tree.get("nomad_triplanar_world", True))
        elif image_node.projection == "BOX":
            entry["projection"] = "triplanar"
            entry["triplanar_hardness"] = [1.0 - float(image_node.projection_blend)] * 3
        else:
            entry["projection"] = "uv"
        mapping = image_mapping(image_node)
        if mapping is not None:
            location = mapping.inputs["Location"].default_value
            rotation = mapping.inputs["Rotation"].default_value
            scale = mapping.inputs["Scale"].default_value
            base = 0.5 if image_node.projection == "BOX" else 0.0
            angle = float(rotation[2])
            scale_y = float(scale[1])
            entry["offset"] = [
                float(location[0]) - base - math.sin(angle) * scale_y,
                1.0 - math.cos(angle) * scale_y - (float(location[1]) - base),
            ]
            entry["scale"] = [float(scale[0]), scale_y]
            entry["rotation"] = angle
            if image_node.projection == "BOX":
                vector = mapping.inputs["Vector"]
                source = vector.links[0].from_node if vector.is_linked else None
                entry["triplanar_world"] = source is not None and source.type == "NEW_GEOMETRY"
        entry["wrap_s"] = entry["wrap_t"] = {"REPEAT": "repeat", "EXTEND": "clamp", "MIRROR": "mirror"}.get(
            sample_node.extension, "repeat"
        )
        if sample_node.interpolation == "Closest":
            entry["min_filter"] = entry["mag_filter"] = "nearest"
        if channel == "normal":
            socket = shader.inputs.get("Normal")
            wrapper = socket.links[0].from_node if socket and socket.is_linked else None
            if wrapper is not None and wrapper.type == "NORMAL_MAP":
                entry["neg_y"] = wrapper.convention == "DIRECTX"
                entry["factor"] = float(wrapper.inputs["Strength"].default_value)
        textures[channel] = entry
    return textures, use_color_opacity


def material_settings(obj, has_color, has_roughness, has_metalness):
    material, shader = active_principled(obj)
    color = tuple(material.diffuse_color[:3]) if material else (1.0, 1.0, 1.0)
    opacity = float(material.diffuse_color[3]) if material else 1.0
    roughness = 0.25
    metalness = 0.0
    if shader:
        color = tuple(shader.inputs["Base Color"].default_value[:3])
        opacity = float(shader.inputs["Alpha"].default_value)
        roughness = float(shader.inputs["Roughness"].default_value)
        metalness = float(shader.inputs["Metallic"].default_value)
    subsurface = float(shader_input(shader, "Subsurface Weight", 0.0))
    material_type = material.get("nomad_material_type", "subsurface" if subsurface > 0.0 else "opaque") if material else "opaque"
    # only fields with a real Blender source: absent keys keep their Nomad-side values
    settings = {
        "color": color,
        "opacity": opacity,
        "roughness": roughness,
        "metalness": metalness,
        "material_type": material_type,
        "refraction_ior": float(shader_input(shader, "IOR", 1.45)),
        "subsurface_depth": float(shader_input(shader, "Subsurface Scale", 1.0)),
        # a bare _value (no _auto) is an explicit set; Blender always has a definite answer
        "smooth_shading_value": any(polygon.use_smooth for polygon in obj.data.polygons),
    }
    textures, use_color_opacity = material_textures(shader)
    if textures is not None:
        settings["textures"] = textures
        settings["use_color_opacity_value"] = use_color_opacity
    return settings


def vertex_colors(mesh):
    attribute = mesh.color_attributes.active_color
    if attribute is None:
        return None
    source = foreach_get(attribute.data, "color", 4)
    if attribute.domain == "POINT":
        return source
    if attribute.domain != "CORNER":
        return None
    vertex = foreach_get(mesh.loops, "vertex_index", dtype=numpy.int32)
    result = numpy.zeros((len(mesh.vertices), 4), numpy.float64)
    numpy.add.at(result, vertex, source)
    counts = numpy.bincount(vertex, minlength=len(mesh.vertices))[:, None]
    numpy.divide(result, counts, out=result, where=counts > 0)
    return result.astype(numpy.float32)


def point_floats(mesh, name):
    attribute = mesh.attributes.get(name)
    if attribute is None or attribute.domain != "POINT" or attribute.data_type != "FLOAT":
        return None
    return foreach_get(attribute.data, "value")


def int_face_attribute(mesh, name):
    attribute = mesh.attributes.get(name)
    if attribute is None or attribute.domain != "FACE" or attribute.data_type != "INT":
        return None
    return foreach_get(attribute.data, "value", dtype=numpy.int32)


def set_face_groups(mesh, values):
    """Nomad's groups also go into Blender's face sets, offset by one because 0 means "no face
    set" there while Nomad's group 0 is a real group. Sculpt mode can then isolate and mask
    them, and the untouched copy in FACE_GROUP_ATTRIBUTE is what tells us they were edited."""
    for name, data in ((FACE_GROUP_ATTRIBUTE, values), (FACE_SET_ATTRIBUTE, values + 1)):
        attribute = mesh.attributes.get(name)
        if attribute is None or attribute.domain != "FACE" or attribute.data_type != "INT":
            if attribute is not None:
                mesh.attributes.remove(attribute)
            attribute = mesh.attributes.new(name=name, type="INT", domain="FACE")
        attribute.data.foreach_set("value", data)


def sculpt_mask(mesh):
    attribute = mesh.attributes.get(MASK_ATTRIBUTE)
    if attribute is None or attribute.domain != "POINT" or attribute.data_type != "FLOAT":
        return None
    return foreach_get(attribute.data, "value")


def set_sculpt_mask(mesh, values):
    attribute = mesh.attributes.get(MASK_ATTRIBUTE)
    if attribute is None:
        attribute = mesh.attributes.new(name=MASK_ATTRIBUTE, type="FLOAT", domain="POINT")
    attribute.data.foreach_set("value", values)


def hidden_faces(mesh):
    attribute = mesh.attributes.get(HIDE_FACE_ATTRIBUTE)
    if attribute is None or attribute.domain != "FACE" or attribute.data_type != "BOOLEAN":
        return None
    return foreach_get(attribute.data, "value", dtype=bool)


def set_hidden_faces(mesh, hidden):
    attribute = mesh.attributes.get(HIDE_FACE_ATTRIBUTE)
    if attribute is None:
        if not hidden.any():
            return
        attribute = mesh.attributes.new(name=HIDE_FACE_ATTRIBUTE, type="BOOLEAN", domain="FACE")
    attribute.data.foreach_set("value", hidden)


def face_groups(mesh):
    """The face sets win once they stop matching the mirror: that is the user having edited
    them in sculpt mode. A mesh with no Nomad groups at all still sends its face sets."""
    values = int_face_attribute(mesh, FACE_GROUP_ATTRIBUTE)
    edited = int_face_attribute(mesh, FACE_SET_ATTRIBUTE)
    if edited is not None and (values is None or not numpy.array_equal(edited - 1, values)):
        values = edited - 1  # face set 0 is Blender's "none", it lands in Nomad's first group
    if values is None:
        return None
    return numpy.clip(values, 0, MAX_FACE_GROUP)


def paint_channels(mesh):
    """Blender zero-fills attributes on the geometry it creates (a boolean drops them even when
    both operands carry the layer), so those vertices arrive black and fully transparent. A zero
    in every channel is the marker, painted vertices keep alpha 1 and roughness 0.25. Grow the
    surrounding paint over them, a trim cap is one ring away from painted geometry."""
    colors = vertex_colors(mesh)
    roughness = point_floats(mesh, ROUGHNESS_ATTRIBUTE)
    metalness = point_floats(mesh, METALNESS_ATTRIBUTE)
    if colors is None:
        return colors, roughness, metalness
    only_zeros = ~colors.any(axis=1)
    if roughness is not None:
        only_zeros &= roughness == 0.0
    if metalness is not None:
        only_zeros &= metalness == 0.0
    if not only_zeros.any():
        return colors, roughness, metalness

    edges = foreach_get(mesh.edges, "vertices", 2, dtype=numpy.int32)
    edges = edges[only_zeros[edges[:, 0]] | only_zeros[edges[:, 1]]]  # the others never feed anything
    edges = numpy.concatenate((edges, edges[:, ::-1]))  # walk both ways
    for _ in range(8):
        grow = only_zeros[edges[:, 0]] & ~only_zeros[edges[:, 1]]
        if not grow.any():
            break
        target, source = edges[grow, 0], edges[grow, 1]
        colors[target] = colors[source]
        for values in (roughness, metalness):
            if values is not None:
                values[target] = values[source]
        only_zeros[target] = False

    colors[only_zeros] = 1.0  # nothing to grow from: Nomad's defaults, metalness is already 0
    if roughness is not None:
        roughness[only_zeros] = DEFAULT_ROUGHNESS
    return colors, roughness, metalness


def corner_table(loop_starts, quad, corners):
    table = numpy.full((len(loop_starts), 4), -1, "<i4")
    for column in range(3):
        table[:, column] = corners[loop_starts + column]
    table[quad, 3] = corners[loop_starts[quad] + 3]
    return table


def encode_object(obj, live=False, include_material=True, replace_topology=False):
    if not sends_evaluated(obj):
        return encode_mesh(obj, obj.data, live, include_material, replace_topology)
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    try:
        mesh = evaluated.to_mesh() or obj.data  # a stack that yields no mesh falls back to the base
        return encode_mesh(obj, mesh, live, include_material, replace_topology)
    finally:
        evaluated.to_mesh_clear()


def encode_mesh(obj, mesh, live, include_material, replace_topology):
    """mesh is obj.data, or a throwaway evaluated copy; ids and Nomad's own custom
    properties always come from obj.data, which outlives it."""
    loop_totals = foreach_get(mesh.polygons, "loop_total", dtype=numpy.int32)
    if loop_totals.size and loop_totals.min() < 3:
        raise ValueError("Nomad Link needs faces with at least 3 corners")
    ngon = bool(loop_totals.size and loop_totals.max() > 4)
    if ngon and "ngon" not in remote_capabilities:
        raise ValueError("This peer supports triangle and quad faces only")
    loop_starts = foreach_get(mesh.polygons, "loop_start", dtype=numpy.int32)
    quad = loop_totals == 4

    shape_keys = mesh.shape_keys.key_blocks if mesh.shape_keys else ()
    source = shape_keys[0].data if shape_keys else mesh.vertices
    blender_positions = foreach_get(source, "co", 3)
    binary = bytearray(to_nomad_vectors(blender_positions).astype("<f4").tobytes())

    header = {
        "type": "mesh_full",
        "request_id": uuid.uuid4().hex,
        "mesh_id": ensure_link_id(obj),
        "geometry_id": ensure_geometry_id(obj.data),
        "name": obj.name,
        "vertex_count": len(mesh.vertices),
        "face_count": len(mesh.polygons),
        "position_offset": 0,
        "position_format": "float32x3",
    }

    corner_verts = foreach_get(mesh.loops, "vertex_index", dtype=numpy.int32)
    if ngon:
        header["face_format"] = "corners"
        header["corner_count"] = len(mesh.loops)
        header["face_size_offset"] = len(binary)
        binary.extend(loop_totals.astype("<i4").tobytes())
        header["corner_vertex_offset"] = len(binary)
        binary.extend(corner_verts.astype("<i4").tobytes())
    else:
        header["face_offset"] = len(binary)
        header["face_format"] = "int32x4"
        binary.extend(corner_table(loop_starts, quad, corner_verts).tobytes())

    uv_layer = mesh.uv_layers.active
    if uv_layer:
        uv_values = foreach_get(uv_layer.data, "uv", 2)
        uv_values[:, 1] = 1.0 - uv_values[:, 1]  # back to Nomad's top-left v origin
        header["texcoord_count"] = len(mesh.loops)
        header["texcoord_offset"] = len(binary)
        header["texcoord_format"] = "float32x2"
        binary.extend(uv_values.astype("<f4").tobytes())
        loop_indices = numpy.arange(len(mesh.loops), dtype=numpy.int32)
        if ngon:
            header["corner_texcoord_offset"] = len(binary)
            binary.extend(loop_indices.astype("<i4").tobytes())
        else:
            header["face_uv_offset"] = len(binary)
            binary.extend(corner_table(loop_starts, quad, loop_indices).tobytes())

    colors, roughness, metalness = paint_channels(mesh)
    if colors is not None:
        header["color_offset"] = len(binary)
        header["color_format"] = "rgbm8"
        binary.extend(encode_rgbm(colors[:, :3]).tobytes())
        header["opacity_offset"] = len(binary)
        header["opacity_format"] = "uint8_norm"
        binary.extend(pack_unit(colors[:, 3], 255.0, "u1").tobytes())

    if roughness is not None:
        header["roughness_offset"] = len(binary)
        header["roughness_format"] = "uint8_norm"
        binary.extend(pack_unit(roughness, 255.0, "u1").tobytes())

    if metalness is not None:
        header["metalness_offset"] = len(binary)
        header["metalness_format"] = "uint8_norm"
        binary.extend(pack_unit(metalness, 255.0, "u1").tobytes())

    mask = sculpt_mask(mesh)
    if mask is not None:
        header["mask_offset"] = len(binary)
        header["mask_format"] = "uint16_norm"
        binary.extend(pack_unit(1.0 - mask, 65535.0, "<u2").tobytes())

    hidden = hidden_faces(mesh)
    if hidden is not None:
        header["face_hidden_offset"] = len(binary)
        header["face_hidden_format"] = "uint8"
        binary.extend(hidden.astype("u1").tobytes())

    groups = face_groups(mesh)
    if groups is not None:
        header["face_group_offset"] = len(binary)
        header["face_group_format"] = "uint16"
        binary.extend(groups.astype("<u2").tobytes())
        try:
            header["face_groups"] = json.loads(obj.data.get("nomad_face_groups", "[]"))
        except (TypeError, ValueError):
            header["face_groups"] = []

    header["layers"] = []
    if shape_keys:
        basis = foreach_get(shape_keys[0].data, "co", 3)
        for key in shape_keys[1:]:
            difference = foreach_get(key.data, "co", 3) - basis
            indices = numpy.flatnonzero(numpy.any(difference != 0.0, axis=1))
            records = numpy.empty(len(indices), LAYER_DTYPE)
            records["index"] = indices
            records["offset"] = to_nomad_vectors(difference[indices])
            header["layers"].append(
                {
                    "name": key.name,
                    "factor": key.value,
                    "factor_offset": 1.0,
                    "visible": not key.mute,
                    "visible_offset": True,
                    "offset": len(binary),
                    "format": "uint32_float32x3",
                    "count": len(indices),
                }
            )
            binary.extend(records.tobytes())
        header["layer_active"] = obj.active_shape_key_index - 1  # Basis = Nomad's base = -1

    # an evaluated mesh is nobody's shared geometry, and its vertices are not obj.data's
    if mesh is obj.data:
        sent_geometry.add(header["geometry_id"])
    if shape_keys or mesh is not obj.data:
        delta_cache.pop(header["geometry_id"], None)
    else:
        delta_cache[header["geometry_id"]] = {
            "counts": (len(mesh.vertices), len(mesh.loops), len(mesh.polygons)),
            "corner_verts": corner_verts.copy(),
            "positions": blender_positions.copy(),
            "colors": None if colors is None else colors.copy(),
            "masks": None if mask is None else mask.copy(),
            # face data has no delta section: remember it so a change falls back to a full send
            "faces": None if groups is None else groups.copy(),
            "hidden": None if hidden is None else hidden.copy(),
        }

    matrix = TO_NOMAD @ obj.matrix_world @ TO_BLENDER
    if include_material:
        header["material"] = material_settings(
            obj, colors is not None, roughness is not None, metalness is not None
        )
    header["binary_size"] = len(binary)
    header["coordinate_system"] = "nomad_y_up"
    header["world_matrix"] = matrix_to_columns(matrix)
    header["smooth_shading"] = any(polygon.use_smooth for polygon in mesh.polygons)
    header["live_sync"] = live
    header["replace_topology"] = replace_topology
    pending_objects[header["request_id"]] = (obj, time.monotonic() + PENDING_TIMEOUT)
    return header, bytes(binary)


def send_instance(obj, live):
    header = {
        "type": "mesh_instance",
        "request_id": uuid.uuid4().hex,
        "live_sync": live,
        "geometry_id": ensure_geometry_id(obj.data),
        "mesh_id": ensure_link_id(obj),
        **object_state(obj),
    }
    pending_objects[header["request_id"]] = (obj, time.monotonic() + PENDING_TIMEOUT)
    connection.send(header)


def send_object(obj, live=False, replace_topology=False):
    link_id = ensure_link_id(obj)
    if link_id in force_full_ids:
        force_full_ids.discard(link_id)
    elif (
        "mesh_instance" in remote_capabilities
        and not sends_evaluated(obj)
        and obj.data.get(GEOMETRY_ID) in sent_geometry
        and geometry_sibling(obj) is not None
    ):
        return send_instance(obj, live)
    header, binary = encode_object(
        obj,
        live=live,
        include_material=not live or bpy.context.scene.nomad_link_sync_materials,
        replace_topology=replace_topology,
    )
    connection.send(header, binary)


def send_sculpt_delta(obj):
    """Send only the vertices a sculpt session changed; False falls back to a full transfer."""
    if "mesh_delta_receive" not in remote_capabilities:
        return False
    mesh = obj.data
    if mesh.shape_keys or sends_evaluated(obj):
        return False
    mesh_id = ensure_link_id(obj)
    cache = delta_cache.get(ensure_geometry_id(mesh))
    if cache is None or cache["counts"] != (len(mesh.vertices), len(mesh.loops), len(mesh.polygons)):
        return False
    corner_verts = foreach_get(mesh.loops, "vertex_index", dtype=numpy.int32)
    if not numpy.array_equal(corner_verts, cache["corner_verts"]):
        return False
    colors = paint_channels(mesh)[0]
    if (colors is None) != (cache["colors"] is None):
        return False
    # face groups and hidden faces only travel in a full mesh: hiding or painting a face set
    # moves no vertex, so the delta below would report the mesh untouched and send nothing
    for key, values in (("faces", face_groups(mesh)), ("hidden", hidden_faces(mesh))):
        cached = cache.get(key)
        if (values is None) != (cached is None):
            return False
        if values is not None and not numpy.array_equal(values, cached):
            return False

    mask = sculpt_mask(mesh)
    if (mask is None) != (cache.get("masks") is None):
        return False

    positions = foreach_get(mesh.vertices, "co", 3)
    changed = numpy.any(positions != cache["positions"], axis=1)
    color_changed = None
    if colors is not None:
        color_changed = numpy.any(colors != cache["colors"], axis=1)
        changed |= color_changed
    mask_changed = None
    if mask is not None:
        mask_changed = mask != cache["masks"]
        changed |= mask_changed
    indices = numpy.flatnonzero(changed)
    if not len(indices):
        return True  # base mesh untouched (Multires sculpting, modifier tweaks)

    binary = bytearray()
    header = {
        "type": "mesh_delta",
        "request_id": uuid.uuid4().hex,
        "live_sync": True,
        "mesh_id": mesh_id,
        "count": int(len(indices)),
        "vertex_count": len(mesh.vertices),
        "index_offset": 0,
        "index_format": "uint32",
    }
    binary.extend(indices.astype("<u4").tobytes())
    header["position_offset"] = len(binary)
    header["position_format"] = "float32x3"
    binary.extend(to_nomad_vectors(positions[indices]).astype("<f4").tobytes())
    if color_changed is not None and color_changed.any():
        header["color_offset"] = len(binary)
        header["color_format"] = "rgbm8"
        binary.extend(encode_rgbm(colors[indices, :3]).tobytes())
        header["opacity_offset"] = len(binary)
        header["opacity_format"] = "uint8_norm"
        binary.extend(pack_unit(colors[indices, 3], 255.0, "u1").tobytes())
    if mask_changed is not None and mask_changed.any():
        header["mask_offset"] = len(binary)
        header["mask_format"] = "uint16_norm"
        binary.extend(pack_unit(1.0 - mask[indices], 65535.0, "<u2").tobytes())
    header["binary_size"] = len(binary)
    if not connection.send(header, bytes(binary)):
        return False
    cache["positions"][indices] = positions[indices]
    if colors is not None:
        cache["colors"][indices] = colors[indices]
    if mask is not None:
        cache["masks"][indices] = mask[indices]
    return True


def object_state(obj):
    matrix = TO_NOMAD @ obj.matrix_world
    if obj.type == "MESH":
        matrix = matrix @ TO_BLENDER
    return {
        "link_id": ensure_link_id(obj),
        "name": obj.name,
        # effective viewport visibility: eye icon + monitor icon + collection state
        "visible": obj.visible_get(),
        "world_matrix": matrix_to_columns(matrix),
    }


def send_object_state(obj):
    header = {"type": "object_state", "live_sync": True, **object_state(obj)}
    if obj.type == "MESH":
        header["smooth_shading"] = any(polygon.use_smooth for polygon in obj.data.polygons)
    connection.send(header)


def send_material(obj):
    colors = vertex_colors(obj.data)  # only their presence is reported, no paint travels here
    roughness = point_floats(obj.data, ROUGHNESS_ATTRIBUTE)
    metalness = point_floats(obj.data, METALNESS_ATTRIBUTE)
    connection.send(
        {
            "type": "material",
            "mesh_id": ensure_link_id(obj),
            "live_sync": True,
            "material": material_settings(
                obj, colors is not None, roughness is not None, metalness is not None
            ),
        }
    )


def kelvin_to_rgb(kelvin):
    """Blackbody color (Tanner Helland approximation), linearized for Blender."""
    t = max(1000.0, min(kelvin, 40000.0)) / 100.0
    red = 255.0 if t <= 66 else 329.698727446 * ((t - 60) ** -0.1332047592)
    green = 99.4708025861 * math.log(t) - 161.1195681661 if t <= 66 else 288.1221695283 * ((t - 60) ** -0.0755148492)
    blue = 255.0 if t >= 66 else 0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    return tuple((max(0.0, min(channel, 255.0)) / 255.0) ** 2.2 for channel in (red, green, blue))


def send_light(obj, live=False):
    data = obj.data
    light_type = data.type
    if obj.get("nomad_light_type") == "ENVIRONMENT":
        light_type = "ENVIRONMENT"
    header = {
        "type": "light",
        "live_sync": live,
        **object_state(obj),
        "light_type": light_type,
        "color": tuple(data.color),
        "shadow_cast": data.use_shadow,
    }
    # only fields the Blender light type actually has: the rest keep their Nomad values
    if data.type == "SPOT":
        header["spot_angle"] = data.spot_size
        header["spot_softness"] = data.spot_blend
    if data.type == "SUN":
        header["angle"] = data.angle
    if data.type in {"POINT", "SPOT"}:
        header["size"] = data.radius if hasattr(data, "radius") else data.shadow_soft_size
    # Nomad suns read "intensity" (normalized), the other types "power" (world space).
    # Fields Blender cannot represent (factor) are omitted so Nomad keeps them.
    header["intensity" if data.type == "SUN" else "power"] = data.energy
    header["kelvin"] = data.temperature
    tinted = any(abs(channel - 1.0) > 0.001 for channel in data.color)
    header["use_kelvin"] = data.use_temperature and not tinted
    if data.use_temperature and tinted:
        # Nomad has no tint on top of kelvin: bake blackbody * tint into a plain color
        header["color"] = tuple(t * b for t, b in zip(data.color, kelvin_to_rgb(data.temperature)))
    elif data.use_temperature:
        del header["color"]  # white tint carries no information, let Nomad keep its color
    connection.send(header)


def send_camera_object(obj, live=False):
    data = obj.data
    connection.send(
        {
            "type": "camera_object",
            "live_sync": live,
            **object_state(obj),
            "orthographic": data.type == "ORTHO",
            "fov_y": math.degrees(data.angle_y),
            "pivot": list((TO_NOMAD @ (obj.matrix_world.translation - obj.matrix_world.col[2].xyz * 5.0).to_4d()).to_3d()),
        }
    )


def send_supported(obj, live=False, replace_topology=False):
    if obj.type == "MESH":
        send_object(obj, live=live, replace_topology=replace_topology)
    elif obj.type == "LIGHT":
        send_light(obj, live)
    elif obj.type == "CAMERA":
        send_camera_object(obj, live)


def scope_objects(scene, scope):
    if scope == "SCENE":
        return [obj for obj in scene.objects if supported_object(obj)]
    return [obj for obj in bpy.context.selected_objects if supported_object(obj)]


def send_scope(scene, scope):
    objects = scope_objects(scene, scope)
    if not objects:
        raise RuntimeError(f"No supported {scope.lower()} objects")
    pending_transfers.extend(objects)


def replace_scene_objects(scene):
    """Drop every syncable object ahead of a scene pull, sync-invisibly: the membership
    baseline resets so no object_delete is broadcast (mirrors Nomad's replace-get)."""
    global membership_ready
    doomed = [obj for obj in scene.objects if supported_object(obj) or obj.get(TRANSFORM_PARENT_ID)]
    for obj in doomed:
        bpy.data.objects.remove(obj, do_unlink=True)
    known_objects.clear()
    membership_ready = False
    dirty_objects.clear()  # queued entries hold references into the removed objects
    pending_transfers.clear()
    pending_objects.clear()
    pending_materials.clear()
    stale_objects.clear()
    stale_requested.clear()
    delta_cache.clear()
    sent_geometry.clear()
    force_full_ids.clear()


def apply_object_state(obj, header):
    obj.name = header.get("name", obj.name)
    if "visible" in header:
        visible = bool(header["visible"])
        obj.hide_render = not visible
        if visible:
            obj.hide_viewport = False  # heal a stuck "disable in viewports" flag
        try:
            obj.hide_set(not visible)  # the eye icon is the single viewport switch
        except RuntimeError:
            pass
        visibility_states[obj.as_pointer()] = obj.visible_get()  # do not echo the applied state
    if "world_matrix" in header:
        apply_object_transform(obj, header)
    remember_object(obj)


def receive_object_state(header):
    obj = find_linked_object(header.get("link_id", ""))
    if obj is None:
        return
    apply_object_state(obj, header)
    if obj.type == "MESH" and "smooth_shading" in header:
        smooth = bool(header["smooth_shading"])
        obj.data.polygons.foreach_set("use_smooth", [smooth] * len(obj.data.polygons))
        obj.data.update()


def receive_material(header):
    obj = find_linked_object(header.get("mesh_id", ""))
    if obj is None or obj.type != "MESH":
        return
    make_material(
        obj.data,
        header,
        obj.data.color_attributes.get(COLOR_ATTRIBUTE) is not None,
        obj.data.attributes.get(ROUGHNESS_ATTRIBUTE) is not None,
        obj.data.attributes.get(METALNESS_ATTRIBUTE) is not None,
        link_id=str(header.get("mesh_id", "")),
    )
    settings = header.get("material", {})
    smooth = bool(settings.get("smooth_shading", False))
    obj.data.polygons.foreach_set("use_smooth", [smooth] * len(obj.data.polygons))
    obj.data.update()
    remember_object(obj)


def receive_texture(header, binary):
    """A texture blob: a cache fill keyed by immutable id, never an edit by itself."""
    texture_id = str(header.get("texture_id", ""))
    if not texture_id or not binary:
        return
    texture_requested.discard(texture_id)
    sent_textures.add(texture_id)  # Nomad holds what it just sent: never echo the blob back
    if find_texture_image(texture_id) is None:
        name = Path(str(header.get("name", ""))).name or "nomad_texture.png"
        image = image_from_bytes(name, bytes(binary))
        image["nomad_texture_id"] = texture_id
        texture_images[texture_id] = image
    for link_id in list(pending_materials):
        obj = find_linked_object(link_id)
        if obj is None or obj.type != "MESH":
            pending_materials.pop(link_id, None)
            continue
        make_material(
            obj.data,
            {"material": pending_materials[link_id]},
            obj.data.color_attributes.get(COLOR_ATTRIBUTE) is not None,
            obj.data.attributes.get(ROUGHNESS_ATTRIBUTE) is not None,
            obj.data.attributes.get(METALNESS_ATTRIBUTE) is not None,
            link_id=link_id,
        )


def send_requested_texture(header):
    texture_id = str(header.get("texture_id", ""))
    data, name = image_bytes(find_texture_image(texture_id))
    if data is None:
        connection.send(
            {"type": "error", "message": "Unknown texture", "request_id": header.get("request_id", "")}
        )
        return
    connection.send(
        {"type": "texture", "texture_id": texture_id, "name": name, "binary_size": len(data)}, data
    )


def receive_light(header):
    link_id = header.get("link_id", "")
    obj = find_linked_object(link_id)
    if obj is None:
        data = bpy.data.lights.new(header.get("name", "Nomad Light"), "POINT")
        obj = bpy.data.objects.new(header.get("name", "Nomad Light"), data)
        bpy.context.collection.objects.link(obj)
        obj[MESH_ID] = link_id
    if obj.type != "LIGHT":
        raise ValueError("Linked object is not a light")
    light_type = header.get("light_type", "POINT")
    obj["nomad_light_type"] = light_type
    obj.data.type = light_type if light_type in {"POINT", "SUN", "SPOT", "AREA"} else "AREA"
    use_kelvin = bool(header.get("use_kelvin", False))
    obj.data.use_temperature = use_kelvin
    if "kelvin" in header:
        obj.data.temperature = float(header["kelvin"])
    # Blender multiplies temperature by the color as a tint; Nomad's kelvin replaces
    # the color outright, so a Nomad kelvin light needs a white tint on this side
    if use_kelvin:
        obj.data.color = (1.0, 1.0, 1.0)
    else:
        obj.data.color = header.get("color", (1.0, 1.0, 1.0))
    if obj.data.type == "SUN":  # suns use Nomad's normalized intensity, same value both sides
        obj.data.energy = float(header.get("intensity", header.get("power", 1.0)))
    else:
        obj.data.energy = float(header.get("power", header.get("intensity", 10.0)))
    obj.data.use_shadow = bool(header.get("shadow_cast", True))
    if obj.data.type == "SPOT":
        obj.data.spot_size = float(header.get("spot_angle", obj.data.spot_size))
        obj.data.spot_blend = float(header.get("spot_softness", obj.data.spot_blend))
    if obj.data.type == "SUN":
        obj.data.angle = float(header.get("angle", obj.data.angle))
    if obj.data.type in {"POINT", "SPOT"}:
        obj.data.shadow_soft_size = float(header.get("size", obj.data.shadow_soft_size))
    apply_object_state(obj, header)


def receive_camera_object(header):
    link_id = header.get("link_id", "")
    obj = find_linked_object(link_id)
    if obj is None:
        data = bpy.data.cameras.new(header.get("name", "Nomad Camera"))
        obj = bpy.data.objects.new(header.get("name", "Nomad Camera"), data)
        bpy.context.collection.objects.link(obj)
        obj[MESH_ID] = link_id
    if obj.type != "CAMERA":
        raise ValueError("Linked object is not a camera")
    obj.data.type = "ORTHO" if header.get("orthographic", False) else "PERSP"
    obj.data.sensor_fit = "VERTICAL"
    fov = math.radians(float(header.get("fov_y", 50.0)))
    obj.data.lens = obj.data.sensor_height / (2.0 * max(math.tan(fov * 0.5), 0.001))
    apply_object_state(obj, header)


def receive_delete(header):
    link_id = header.get("link_id", "")
    obj = find_linked_object(link_id)
    if obj is None:
        return
    pointer = obj.as_pointer()
    parent = obj.parent if obj.parent and obj.parent.get(TRANSFORM_PARENT_ID) else None
    bpy.data.objects.remove(obj, do_unlink=True)
    if parent:
        bpy.data.objects.remove(parent, do_unlink=True)
    known_objects.pop(pointer, None)
    dirty_objects.pop(pointer, None)
    stale_objects.discard(link_id)
    stale_requested.pop(link_id, None)


def version_tuple(version):
    return tuple(int(value) for value in version.split("."))


def linked_object_count(scene):
    return sum(bool(obj.get(MESH_ID)) for obj in scene.objects if supported_object(obj))


def scene_config(scene):
    return {
        "live_sync": scene.nomad_link_live_sync,
        "sync_mode": scene.nomad_link_sync_mode.lower(),
        "sync_view": scene.nomad_link_sync_view,
        "sync_objects": scene.nomad_link_sync_objects,
        "sync_materials": scene.nomad_link_sync_materials,
        "sync_lights": scene.nomad_link_sync_lights,
        "sync_cameras": scene.nomad_link_sync_cameras,
    }


def send_desired_config():
    global config_sent
    if config_sent is not None or config_desired is None:
        return
    config_sent = config_desired.copy()
    connection.send(
        {
            "type": "set_session_config",
            "base_revision": config_revision,
            **config_sent,
        }
    )


def settings_changed(_self, context):
    global config_desired, membership_ready, last_camera
    if applying_config or config_revision < 0 or connection.status != "Connected":
        return
    scene = context.scene
    config_desired = scene_config(scene)
    dirty_objects.clear()
    membership_ready = False
    last_camera = None
    send_desired_config()


def receive_session_config(header):
    global active_source, applying_config, claim_pending, config_desired
    global config_revision, config_sent, membership_ready, last_camera, session_source
    revision = int(header.get("revision", -1))
    if revision < config_revision:
        return
    mode = header.get("sync_mode", "auto")
    incoming = {
        "live_sync": bool(header.get("live_sync", False)),
        "sync_mode": mode if mode in {"auto", "nomad", "client"} else "auto",
        "sync_view": bool(header.get("sync_view", True)),
        "sync_objects": bool(header.get("sync_objects", True)),
        "sync_materials": bool(header.get("sync_materials", False)),
        "sync_lights": bool(header.get("sync_lights", False)),
        "sync_cameras": bool(header.get("sync_cameras", False)),
    }
    sent = config_sent
    config_sent = None
    config_revision = revision
    if sent is not None and (incoming != sent or config_desired != sent):
        send_desired_config()
        return

    active = header.get("active_source", "none")
    active_source = active if active in {"none", "nomad", "client"} else "none"
    claim_pending = False
    session_devices[:] = (session_devices[:1] or ["Nomad"]) + [
        str(name) for name in header.get("peers", []) if name
    ]
    session_source = str(header.get("source_name") or "")
    scene = bpy.context.scene
    applying_config = True
    try:
        scene.nomad_link_live_sync = incoming["live_sync"]
        scene.nomad_link_sync_mode = incoming["sync_mode"].upper()
        scene.nomad_link_sync_view = incoming["sync_view"]
        scene.nomad_link_sync_objects = incoming["sync_objects"]
        scene.nomad_link_sync_materials = incoming["sync_materials"]
        scene.nomad_link_sync_lights = incoming["sync_lights"]
        scene.nomad_link_sync_cameras = incoming["sync_cameras"]
    finally:
        applying_config = False
    config_desired = incoming
    if not scene.nomad_link_live_sync:
        dirty_objects.clear()  # an Auto handover keeps them: they are edits Blender already made
    membership_ready = True
    last_camera = None
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def install_update():
    package_directory = Path(__file__).resolve().parent
    repository_directory = package_directory.parent
    repos = bpy.context.preferences.extensions.repos
    index = next(
        (
            i
            for i, repo in enumerate(repos)
            if Path(bpy.path.abspath(repo.directory)).resolve() == repository_directory
        ),
        -1,
    )
    if index < 0:
        connection.error = "Nomad extension repository is not configured"
        return False
    if not repos[index].remote_url:
        connection.error = "Nomad extension repository has no remote URL"
        return False
    try:
        result = bpy.ops.extensions.repo_sync(repo_index=index)
        if "FINISHED" not in result:
            connection.error = "Could not refresh the Nomad extension repository"
            return False
        result = bpy.ops.extensions.package_install(
            repo_index=index, pkg_id=PACKAGE_ID, enable_on_install=True
        )
        if "FINISHED" in result:
            return True
        connection.error = "Could not install the Nomad Blender Link update"
    except Exception as exc:
        connection.error = str(exc)
        return False
    return False


def auto_update():
    install_update()
    return None


def live_allowed(header, enabled):
    return not header.get("live_sync", False) or (
        receives_live_scene(bpy.context.scene) and enabled
    )


def receive_packet(header, binary):
    global active_source, claim_pending, config_desired, config_revision, config_sent, pairing_wait, update_required
    global session_source
    message_type = header.get("type")
    if message_type == "hello":
        pairing_wait = False
        config_revision = -1
        config_desired = None
        config_sent = None
        active_source = "none"
        claim_pending = False
        connection.error = ""
        remote_capabilities.clear()
        remote_capabilities.update(header.get("capabilities", []))
        session_devices[:] = [str(header.get("client_name") or "Nomad")]
        session_source = ""
        delta_cache.clear()
        force_full_ids.clear()
        sent_geometry.clear()
        texture_requested.clear()
        pending_materials.clear()
        sent_textures.clear()  # texture_images stays: ids are immutable, reconnects reuse them
        granted = header.get("pair_token", "")
        if granted and preferences() is not None:
            preferences().pair_token = granted
        minimum = header.get("minimum_bridge_version", VERSION)
        update_required = ""
        if version_tuple(VERSION) < version_tuple(minimum):
            update_required = minimum
            if bpy.context.scene.nomad_link_auto_update:
                bpy.app.timers.register(auto_update, first_interval=0.1)
    elif message_type == "pairing_pending":
        pairing_wait = True
    elif message_type == "session_config":
        receive_session_config(header)
    elif message_type == "mesh_full" and live_allowed(
        header, bpy.context.scene.nomad_link_sync_objects
    ):
        receive_mesh(header, binary)
        connection.error = ""
    elif message_type == "mesh_instance" and live_allowed(
        header, bpy.context.scene.nomad_link_sync_objects
    ):
        receive_instance(header)
        connection.error = ""
    elif message_type == "mesh_delta" and live_allowed(
        header, bpy.context.scene.nomad_link_sync_objects
    ):
        receive_delta(header, binary)
    elif message_type == "mesh_attributes" and live_allowed(
        header, bpy.context.scene.nomad_link_sync_objects
    ):
        receive_attributes(header, binary)
    elif message_type == "mesh_invalidated":
        connection.error = "Nomad topology changed; get the selected mesh again to refresh it"
    elif message_type == "mesh_ack":
        pending = pending_objects.pop(header.get("request_id", ""), None)
        obj = pending[0] if pending else None
        if obj:
            try:
                obj[MESH_ID] = header["mesh_id"]
                remember_object(obj)
            except ReferenceError:
                pass
        connection.error = ""
    elif message_type in {"request_active_mesh", "request_mesh", "request_selection"}:
        target = find_linked_object(header.get("link_id", ""))
        if target is not None:
            force_full_ids.add(target[MESH_ID])
            if target not in pending_transfers:
                pending_transfers.append(target)
        else:
            try:
                send_scope(bpy.context.scene, "SELECTION")
            except Exception as exc:
                connection.send({"type": "error", "message": str(exc)})
    elif message_type == "request_scene":
        try:
            send_scope(bpy.context.scene, "SCENE")
        except Exception as exc:
            connection.send({"type": "error", "message": str(exc)})
    elif message_type == "object_state" and live_allowed(
        header, bpy.context.scene.nomad_link_sync_objects
    ):
        receive_object_state(header)
    elif message_type == "material" and live_allowed(
        header, bpy.context.scene.nomad_link_sync_materials
    ):
        receive_material(header)
    elif message_type == "texture":
        receive_texture(header, binary)
    elif message_type == "request_texture":
        send_requested_texture(header)
    elif message_type == "light" and live_allowed(
        header, bpy.context.scene.nomad_link_sync_lights
    ):
        receive_light(header)
    elif message_type == "camera_object" and live_allowed(
        header, bpy.context.scene.nomad_link_sync_cameras
    ):
        receive_camera_object(header)
    elif message_type == "object_delete" and header.get("live_sync", False):
        obj = find_linked_object(header.get("link_id", ""))
        if obj and receives_live_scene(bpy.context.scene) and channel_enabled(
            bpy.context.scene, obj
        ):
            receive_delete(header)
    elif message_type == "camera":
        receive_camera(header)
    elif message_type == "error":
        pending_objects.pop(header.get("request_id", ""), None)
        delta_cache.clear()
        sent_geometry.clear()
        connection.error = header.get("message", "Nomad reported an error")


def channel_type_enabled(scene, object_type):
    if object_type == "MESH":
        return scene.nomad_link_sync_objects
    if object_type == "LIGHT":
        return scene.nomad_link_sync_lights
    return scene.nomad_link_sync_cameras


def poll_membership():
    global known_objects, membership_ready
    scene = bpy.context.scene
    objects = {obj.as_pointer(): obj for obj in scene.objects if supported_object(obj)}
    current = {
        pointer: (obj.get(MESH_ID, ""), obj.type) for pointer, obj in objects.items()
    }
    stale_objects.intersection_update(link_id for link_id, _kind in current.values() if link_id)
    if connection.status != "Connected" or not wants_blender_source(scene):
        known_objects = current
        membership_ready = True
        return
    if not membership_ready:
        known_objects = current
        membership_ready = True
        return
    if not sends_live_scene(scene) or stale_objects:
        return

    for pointer, obj in objects.items():
        if pointer in known_objects and known_objects[pointer] == current[pointer]:
            continue
        if channel_enabled(scene, obj):
            send_supported(obj, live=True, replace_topology=obj.type == "MESH")
            current[pointer] = (ensure_link_id(obj), obj.type)
    for pointer, (link_id, object_type) in known_objects.items():
        if (
            (pointer in current and current[pointer] == (link_id, object_type))
            or not link_id
            or not channel_type_enabled(scene, object_type)
        ):
            continue
        connection.send(
            {"type": "object_delete", "link_id": link_id, "live_sync": True}
        )
    known_objects = current


def modal_operator_running():
    """True while a brush stroke, transform, or any other modal operator is in flight."""
    for window in bpy.context.window_manager.windows:
        for operator in getattr(window, "modal_operators", ()):
            if operator.bl_idname != "NOMAD_OT_activity_watch":
                return True
    return False


def queue_dirty(obj, flag, delay):
    pointer = obj.as_pointer()
    item = dirty_objects.setdefault(
        pointer, {"object": obj, "flags": set(), "after": 0.0}
    )
    item["flags"].add(flag)
    item["after"] = max(item["after"], time.monotonic() + delay)


@persistent
def undo_redo_post(scene, _depsgraph=None):
    """Undo frees and rebuilds datablocks: the queued objects and the pointer keys of
    known_objects/visibility_states dangle, and a reused pointer would resolve to a dead entry."""
    global membership_ready
    dirty_objects.clear()
    known_objects.clear()
    visibility_states.clear()
    membership_ready = False


@persistent
def depsgraph_update(scene, depsgraph):
    if connection.status != "Connected" or not wants_blender_source(scene):
        return
    for update in depsgraph.updates:
        source = getattr(update.id, "original", update.id)
        if isinstance(source, bpy.types.Object):
            objects = (source,)
        elif isinstance(source, bpy.types.Material):
            objects = tuple(
                obj
                for obj in scene.objects
                if obj.type == "MESH"
                and any(material == source for material in obj.data.materials)
            )
        else:
            objects = tuple(
                obj
                for obj in scene.objects
                if supported_object(obj) and obj.data == source
            )
        for obj in objects:
            if not supported_object(obj):
                continue
            if obj.type == "MESH" and not (
                scene.nomad_link_sync_objects or scene.nomad_link_sync_materials
            ):
                continue
            if obj.type != "MESH" and not channel_enabled(scene, obj):
                continue
            if obj.type == "MESH":
                if isinstance(source, bpy.types.Material):
                    queue_dirty(obj, "material", 0.1)
                geometry = getattr(update, "is_updated_geometry", False)
                transform = getattr(update, "is_updated_transform", False)
                sculpt = (
                    isinstance(source, bpy.types.Object)
                    and obj.mode == "SCULPT"
                    and not transform
                )
                if geometry or sculpt:
                    if obj.mode != "SCULPT":
                        queue_dirty(obj, "geometry", 0.6)
                    elif obj.use_dynamic_topology_sculpting:
                        # Dyntopo sculpts an internal BMesh; obj.data stays a stale
                        # snapshot until Blender flushes it on Dyntopo off or mode exit.
                        connection.error = "Dyntopo edits reach Nomad once Dyntopo is turned off"
                    else:
                        queue_dirty(obj, "sculpt", 0.1)
                    if scene.nomad_link_sync_materials:
                        queue_dirty(obj, "material", 0.6)
                if transform:
                    queue_dirty(obj, "transform", 0.05)
                elif isinstance(source, bpy.types.Object) and not geometry and not sculpt:
                    queue_dirty(obj, "transform", 0.1)
            elif obj.type == "LIGHT":
                queue_dirty(obj, "light", 0.1)
            elif obj.type == "CAMERA":
                queue_dirty(obj, "camera", 0.1)


def flush_dirty():
    scene = bpy.context.scene
    if not scene.nomad_link_live_sync:
        dirty_objects.clear()
        return
    # every entry was queued while Blender owned the source, so a finished stroke still goes out
    # after Auto hands the source to Nomad; a remote apply pops its own object from the queue
    if stale_objects:
        return
    now = time.monotonic()
    for request_id, (_obj, deadline) in tuple(pending_objects.items()):
        if now >= deadline:
            pending_objects.pop(request_id, None)  # unanswered: never let it pin the queue
    if pending_objects or pending_transfers:
        return
    modal_running = None
    for pointer, item in tuple(dirty_objects.items()):
        if now < item["after"]:
            continue
        obj = item["object"]
        try:
            if obj.name not in scene.objects or not supported_object(obj):
                dirty_objects.pop(pointer, None)
                continue
        except ReferenceError:
            dirty_objects.pop(pointer, None)
            continue
        flags = item["flags"]
        if obj.type == "MESH" and flags & {"geometry", "sculpt"} and now < item["after"] + MODAL_GRACE:
            # A half-finished stroke must not be sent: wait for the pointer release, detected as
            # the modal brush operator leaving window.modal_operators. Only until the grace ends
            # though: every dab pushes "after" forward, so a queue that went quiet is a finished
            # stroke, and an unfocused Blender never processes the events that end the operator.
            if modal_running is None:
                modal_running = modal_operator_running()
            if modal_running:
                continue
        dirty_objects.pop(pointer, None)
        if obj.type == "MESH":
            geometry_flags = flags & {"geometry", "sculpt"}
            sent_full = False
            if geometry_flags and scene.nomad_link_sync_objects:
                if geometry_flags != {"sculpt"} or not send_sculpt_delta(obj):
                    send_object(obj, live=True, replace_topology=True)
                    sent_full = True
            elif "transform" in flags and scene.nomad_link_sync_objects:
                send_object_state(obj)
            if "material" in flags and scene.nomad_link_sync_materials and not sent_full:
                send_material(obj)
        elif "light" in flags and scene.nomad_link_sync_lights:
            send_light(obj, True)
        elif "camera" in flags and scene.nomad_link_sync_cameras:
            send_camera_object(obj, True)


def flush_transfers():
    if not pending_transfers or pending_objects or not connection.outgoing.empty():
        return
    obj = pending_transfers.pop(0)
    try:
        send_supported(obj, replace_topology=obj.type == "MESH")
    except ReferenceError:
        pass


def poll_stale_recovery(scene):
    for mesh_id in tuple(stale_requested):
        if mesh_id not in stale_objects:
            del stale_requested[mesh_id]
    if not stale_objects or not scene.nomad_link_live_sync:
        return
    now = time.monotonic()
    for mesh_id in tuple(stale_objects):
        obj = find_linked_object(mesh_id)
        if obj is None or obj.mode == "EDIT":
            continue
        if obj.mode != "OBJECT" and bpy.context.view_layer.objects.active != obj:
            continue
        if now - stale_requested.get(mesh_id, 0.0) < 5.0:
            continue
        stale_requested[mesh_id] = now
        connection.send({"type": "request_mesh", "request_id": uuid.uuid4().hex, "link_id": mesh_id})


visibility_states = {}


def poll_visibility(scene):
    """Queue state sends when effective visibility changes.

    Toggling the eye icon (or a collection) does not tag the object in the
    depsgraph, so visibility changes need their own sweep over linked objects.
    """
    global visibility_states
    seen = {}
    for obj in scene.objects:
        if not obj.get(MESH_ID) or not supported_object(obj):
            continue
        pointer = obj.as_pointer()
        visible = obj.visible_get()
        seen[pointer] = visible
        previous = visibility_states.get(pointer)
        if previous is not None and previous != visible:
            flag = {"LIGHT": "light", "CAMERA": "camera"}.get(obj.type, "transform")
            queue_dirty(obj, flag, 0.1)
    visibility_states = seen


def poll():
    if connection.status in {"Connecting", "Connected", "Listening"} and not activity_watch_running:
        try:
            bpy.ops.nomad.activity_watch("INVOKE_DEFAULT")
        except RuntimeError:
            pass  # no window yet: retried next poll
    latest_camera = None
    for header, binary in connection.poll():
        try:
            if header.get("type") == "camera":
                latest_camera = header
            else:
                receive_packet(header, binary)
        except Exception as exc:
            connection.error = str(exc)
    try:
        if latest_camera is not None:
            receive_camera(latest_camera)
        if connection.status == "Connected":
            flush_transfers()
            poll_membership()
            poll_visibility(bpy.context.scene)
            flush_dirty()
            poll_stale_recovery(bpy.context.scene)
            send_camera()
    except Exception as exc:
        connection.error = str(exc)
    return 1.0 / 60.0


class NomadLinkPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    host: bpy.props.StringProperty(name="Host", default="127.0.0.1")
    port: bpy.props.IntProperty(name="Port", default=48312, min=1024, max=65535)
    pair_token: bpy.props.StringProperty(name="Pairing Token", default="", options={"HIDDEN"})

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "host")
        layout.prop(self, "port")
        if self.pair_token:
            layout.operator("nomad.forget_pairing", icon="X")


class NOMAD_OT_forget_pairing(bpy.types.Operator):
    bl_idname = "nomad.forget_pairing"
    bl_label = "Forget Nomad Pairing"
    bl_description = "Remove the stored pairing token; the next connection asks Nomad for approval again"

    def execute(self, _context):
        prefs = preferences()
        if prefs is not None:
            prefs.pair_token = ""
        return {"FINISHED"}


class NOMAD_OT_activity_watch(bpy.types.Operator):
    bl_idname = "nomad.activity_watch"
    bl_label = "Nomad Link Activity Watch"
    bl_options = {"INTERNAL"}

    def invoke(self, context, _event):
        global activity_watch_operator, activity_watch_running
        if activity_watch_running:
            return {"CANCELLED"}
        activity_watch_running = True
        activity_watch_operator = self
        self.timer = context.window_manager.event_timer_add(0.25, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if connection.status not in {"Connecting", "Connected", "Listening"}:
            self.cancel(context)
            return {"CANCELLED"}
        navigation = event.type in {
            "TRACKPADPAN",
            "TRACKPADZOOM",
            "MOUSEROTATE",
            "MOUSESMARTZOOM",
            "NDOF_MOTION",
        }
        if navigation or (
            event.value == "PRESS"
            and event.type not in {"TIMER", "MOUSEMOVE", "INBETWEEN_MOUSEMOVE"}
        ):
            claim_blender_source()
        return {"PASS_THROUGH"}

    def cancel(self, context):
        global activity_watch_operator, activity_watch_running
        if getattr(self, "timer", None) is not None:
            context.window_manager.event_timer_remove(self.timer)
            self.timer = None
        activity_watch_running = False
        activity_watch_operator = None


class NOMAD_OT_reset_host(bpy.types.Operator):
    bl_idname = "nomad.reset_host"
    bl_label = "This Computer"
    bl_description = "Reset the host to 127.0.0.1 for a Nomad running on this computer"

    def execute(self, _context):
        prefs = preferences()
        if prefs is not None:
            prefs.host = "127.0.0.1"
        return {"FINISHED"}


class NOMAD_OT_discover(bpy.types.Operator):
    bl_idname = "nomad.discover"
    bl_label = "Find Nomad"

    def execute(self, _context):
        prefs = preferences()
        try:
            if prefs is None:
                raise RuntimeError("Nomad Blender Link preferences are unavailable")
            result = discover(prefs.port)
            if not result:
                raise RuntimeError("No Nomad Blender Link found")
            prefs.host, prefs.port = result
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


def reset_link_state():
    global active_source, claim_pending, config_desired, config_revision, config_sent, pairing_wait, session_source
    pending_objects.clear()
    pending_transfers.clear()
    delta_cache.clear()
    remote_capabilities.clear()
    session_devices.clear()
    texture_requested.clear()
    pending_materials.clear()
    sent_textures.clear()  # texture_images stays: ids are immutable, reconnects reuse them
    session_source = ""
    config_revision = -1
    config_desired = None
    config_sent = None
    active_source = "none"
    claim_pending = False
    pairing_wait = False


class NOMAD_OT_connect(bpy.types.Operator):
    bl_idname = "nomad.connect"
    bl_label = "Connect"

    def execute(self, context):
        prefs = preferences()
        if prefs is None:
            self.report({"ERROR"}, "Nomad Blender Link preferences are unavailable")
            return {"CANCELLED"}
        reset_link_state()
        connection.connect(prefs.host, prefs.port, prefs.pair_token, VERSION, PROTOCOL_VERSION)
        if not activity_watch_running:
            bpy.ops.nomad.activity_watch("INVOKE_DEFAULT")
        return {"FINISHED"}


class NOMAD_OT_listen(bpy.types.Operator):
    bl_idname = "nomad.listen"
    bl_label = "Listen for Nomad Web"
    bl_description = "Accept a connection from Nomad running in a web browser on this computer"

    def execute(self, context):
        prefs = preferences()
        if prefs is None:
            self.report({"ERROR"}, "Nomad Blender Link preferences are unavailable")
            return {"CANCELLED"}
        reset_link_state()
        connection.listen(prefs.port, prefs.pair_token, VERSION, PROTOCOL_VERSION)
        if not activity_watch_running:
            bpy.ops.nomad.activity_watch("INVOKE_DEFAULT")
        return {"FINISHED"}


class NOMAD_OT_disconnect(bpy.types.Operator):
    bl_idname = "nomad.disconnect"
    bl_label = "Disconnect"

    def execute(self, _context):
        reset_link_state()
        connection.disconnect()
        return {"FINISHED"}


SCOPE_ITEMS = (
    ("SELECTION", "Selection", "The selected meshes, lights, and cameras"),
    ("SCENE", "Scene", "All meshes, lights, and cameras"),
)


class NOMAD_OT_get(bpy.types.Operator):
    bl_idname = "nomad.get"
    bl_label = "Get from Nomad"
    bl_description = "Request the objects from Nomad"

    scope: bpy.props.EnumProperty(items=SCOPE_ITEMS, default="SELECTION")

    def execute(self, _context):
        message = "request_scene" if self.scope == "SCENE" else "request_selection"
        connection.send({"type": message, "request_id": uuid.uuid4().hex})
        return {"FINISHED"}


class NOMAD_OT_get_replace(bpy.types.Operator):
    bl_idname = "nomad.get_replace"
    bl_label = "Replace All from Nomad"
    bl_description = "Delete the scene's meshes, lights, and cameras, then get the peer's scene"
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, event):
        if any(supported_object(obj) for obj in context.scene.objects):
            return context.window_manager.invoke_confirm(self, event)
        return self.execute(context)

    def execute(self, context):
        replace_scene_objects(context.scene)
        connection.send({"type": "request_scene", "request_id": uuid.uuid4().hex})
        return {"FINISHED"}


class NOMAD_OT_send(bpy.types.Operator):
    bl_idname = "nomad.send"
    bl_label = "Send to Nomad"
    bl_description = "Send the objects to Nomad"

    scope: bpy.props.EnumProperty(items=SCOPE_ITEMS, default="SELECTION")

    def execute(self, context):
        try:
            send_scope(context.scene, self.scope)
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class NOMAD_OT_update(bpy.types.Operator):
    bl_idname = "nomad.update_extension"
    bl_label = "Update Nomad Blender Link"

    def execute(self, _context):
        if install_update():
            return {"FINISHED"}
        self.report(
            {"ERROR"},
            connection.error
            or "Install from a configured Nomad extension repository to enable automatic updates",
        )
        return {"CANCELLED"}


class NOMAD_PT_link(bpy.types.Panel):
    bl_label = "Nomad Blender Link"
    bl_idname = "NOMAD_PT_link"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Nomad"

    def draw(self, context):
        global viewport_area_pointer
        viewport_area_pointer = context.area.as_pointer()
        layout = self.layout
        scene = context.scene
        status = connection.status
        if status == "Connected" and session_devices:
            names = [
                name + (" (live)" if name and name == session_source else "")
                for name in session_devices
            ]
            status += " — " + ", ".join(names)
        layout.label(text=f"Status: {status}")
        layout.label(text=f"Extension: {VERSION}")
        if connection.error:
            layout.label(text=connection.error, icon="ERROR")
        if update_required:
            layout.label(text=f"Version {update_required}+ required", icon="ERROR")
            layout.operator("nomad.update_extension", icon="FILE_REFRESH")

        if connection.status == "Listening":
            layout.label(text="Waiting for Nomad Web on this computer", icon="INFO")
            row = layout.row()
            row.scale_y = 1.4
            row.operator("nomad.disconnect", text="Stop Listening", icon="UNLINKED")
            return

        if connection.status != "Connected":
            prefs = preferences()
            if prefs is None:
                layout.label(text="Preferences unavailable", icon="ERROR")
                return
            row = layout.row(align=True)
            row.prop(prefs, "host")
            row.operator("nomad.reset_host", text="", icon="HOME")
            row.operator("nomad.discover", text="", icon="VIEWZOOM")
            layout.prop(prefs, "port")
            row = layout.row()
            row.scale_y = 1.4
            row.operator("nomad.connect", icon="LINKED")
            layout.operator("nomad.listen", icon="WORLD")
            return

        row = layout.row()
        row.scale_y = 1.4
        row.operator("nomad.disconnect", icon="UNLINKED")
        if config_revision < 0:
            layout.label(
                text="Accept the connection in Nomad" if pairing_wait else "Waiting for Nomad",
                icon="INFO",
            )
            return
        live = layout.box()
        live.prop(scene, "nomad_link_live_sync")
        controls = live.column()
        controls.enabled = scene.nomad_link_live_sync
        controls.prop(scene, "nomad_link_sync_mode", expand=True)
        if scene.nomad_link_live_sync and scene.nomad_link_sync_mode == "AUTO":
            direction = {
                "nomad": "Nomad → Blender",
                "client": "Blender → Nomad",
            }.get(active_source, "Waiting for activity")
            controls.label(text=f"Auto: {direction}", icon="INFO")
        controls.prop(scene, "nomad_link_sync_view")
        target = controls.column()
        target.enabled = scene.nomad_link_sync_view
        target.prop(scene, "nomad_link_camera_target")
        controls.prop(scene, "nomad_link_sync_objects")
        controls.prop(scene, "nomad_link_sync_materials")
        controls.prop(scene, "nomad_link_sync_lights")
        controls.prop(scene, "nomad_link_sync_cameras")
        if scene.nomad_link_live_sync and not linked_object_count(scene):
            live.label(text="Use Send or Get once to link existing objects", icon="INFO")

        layout.prop(scene, "nomad_link_send_modifiers")
        layout.prop(scene, "nomad_link_auto_update")
        for scope, label, _description in SCOPE_ITEMS:
            count = len(scope_objects(scene, scope))
            column = layout.column(align=True)
            column.label(text=label)
            row = column.row(align=True)
            row.scale_y = 1.4
            send = row.row(align=True)
            send.enabled = count > 0
            send.operator("nomad.send", text=f"Send ({count})", icon="EXPORT").scope = scope
            row.operator("nomad.get", text="Get", icon="IMPORT").scope = scope
            if scope == "SCENE":
                replace = column.row(align=True)
                replace.scale_y = 1.4
                replace.operator("nomad.get_replace", text="Replace all", icon="FILE_REFRESH")


classes = (
    NomadLinkPreferences,
    NOMAD_OT_forget_pairing,
    NOMAD_OT_activity_watch,
    NOMAD_OT_reset_host,
    NOMAD_OT_discover,
    NOMAD_OT_connect,
    NOMAD_OT_listen,
    NOMAD_OT_disconnect,
    NOMAD_OT_get,
    NOMAD_OT_get_replace,
    NOMAD_OT_send,
    NOMAD_OT_update,
    NOMAD_PT_link,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.nomad_link_camera_target = bpy.props.EnumProperty(
        name="View Target",
        items=(
            ("VIEWPORT", "3D Viewport", "Use the 3D viewport shown with this panel"),
            ("SCENE_CAMERA", "Scene Camera", "Use the scene camera object"),
        ),
        default="VIEWPORT",
    )
    bpy.types.Scene.nomad_link_send_modifiers = bpy.props.BoolProperty(
        name="Send Modifier Results",
        description=(
            "Send the evaluated result of the modifier stack instead of the base mesh. When "
            "enabled, transfers are limited to Blender → Nomad; Nomad → Blender is unavailable"
        ),
        default=False,
    )
    bpy.types.Scene.nomad_link_live_sync = bpy.props.BoolProperty(
        name="Live Sync", default=False, update=settings_changed
    )
    bpy.types.Scene.nomad_link_sync_mode = bpy.props.EnumProperty(
        name="Source",
        items=(
            ("AUTO", "Auto", "The last application with meaningful input controls live synchronization"),
            ("NOMAD", "Nomad → Blender", "Nomad sends enabled live changes to Blender"),
            ("CLIENT", "Blender → Nomad", "Blender sends enabled live changes to Nomad"),
        ),
        default="AUTO",
        update=settings_changed,
    )
    bpy.types.Scene.nomad_link_sync_view = bpy.props.BoolProperty(
        name="Working View", default=True, update=settings_changed
    )
    bpy.types.Scene.nomad_link_sync_objects = bpy.props.BoolProperty(
        name="Objects & Geometry", default=True, update=settings_changed
    )
    bpy.types.Scene.nomad_link_sync_materials = bpy.props.BoolProperty(
        name="Materials", default=False, update=settings_changed
    )
    bpy.types.Scene.nomad_link_sync_lights = bpy.props.BoolProperty(
        name="Lights", default=False, update=settings_changed
    )
    bpy.types.Scene.nomad_link_sync_cameras = bpy.props.BoolProperty(
        name="Cameras", default=False, update=settings_changed
    )
    bpy.types.Scene.nomad_link_auto_update = bpy.props.BoolProperty(name="Automatic Updates", default=True)
    if depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(depsgraph_update)
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if undo_redo_post not in handlers:
            handlers.append(undo_redo_post)
    if not bpy.app.timers.is_registered(poll):
        bpy.app.timers.register(poll, first_interval=0.1, persistent=True)


def unregister():
    connection.disconnect()
    if activity_watch_operator is not None:
        activity_watch_operator.cancel(bpy.context)
    if bpy.app.timers.is_registered(poll):
        bpy.app.timers.unregister(poll)
    if depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(depsgraph_update)
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if undo_redo_post in handlers:
            handlers.remove(undo_redo_post)
    del bpy.types.Scene.nomad_link_auto_update
    del bpy.types.Scene.nomad_link_sync_cameras
    del bpy.types.Scene.nomad_link_sync_lights
    del bpy.types.Scene.nomad_link_sync_materials
    del bpy.types.Scene.nomad_link_sync_objects
    del bpy.types.Scene.nomad_link_sync_view
    del bpy.types.Scene.nomad_link_sync_mode
    del bpy.types.Scene.nomad_link_live_sync
    del bpy.types.Scene.nomad_link_send_modifiers
    del bpy.types.Scene.nomad_link_camera_target
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
