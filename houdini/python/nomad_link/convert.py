# SPDX-License-Identifier: MIT
"""Nomad Link mesh payloads <-> Houdini geometry.

Nomad and Houdini are both right-handed Y-up, so positions travel unchanged.
Winding is the one convention that differs (Houdini is clockwise-front, glTF is
counter-clockwise-front), hence the `reverse` flags below.

Nothing in this module imports hou except the geometry builder, so the codecs
can be unit tested outside Houdini.
"""
import numpy

IDENTITY = [1.0 if i % 5 == 0 else 0.0 for i in range(16)]

# point attributes carried both ways: houdini name -> (nomad channel, format)
SCALAR_CHANNELS = (
    ("rough", "roughness", "uint8_norm", 255.0),
    ("metallic", "metalness", "uint8_norm", 255.0),
    ("mask", "mask", "uint16_norm", 65535.0),
    ("density", "density", "uint8_norm", 255.0),
)


# ---------------------------------------------------------------- primitives

def _read(binary, offset, count, dtype):
    return numpy.frombuffer(binary, dtype, count, int(offset))


def decode_rgbm(binary, offset, count):
    """Nomad's rgbm8 vertex colours: linear rgb = rgb * (m / 65025)."""
    packed = _read(binary, offset, count * 4, "u1").reshape(-1, 4).astype(numpy.float32)
    return packed[:, :3] * (packed[:, 3:4] / 65025.0)


def encode_rgbm(rgb):
    rgb = numpy.clip(numpy.asarray(rgb, numpy.float32), 0.0, 1.0)
    m = numpy.clip(numpy.ceil(rgb.max(axis=1) * 255.0), 1.0, 255.0)
    scaled = rgb * (65025.0 / m)[:, None] + 0.5
    return numpy.concatenate((scaled, m[:, None] + 0.5), axis=1).astype("u1")


def face_starts(sizes):
    return numpy.concatenate(([0], numpy.cumsum(sizes)[:-1])).astype(numpy.int64)


def reverse_permutation(sizes):
    """Index permutation that flips the corner order of every face."""
    total = int(sizes.sum())
    starts = numpy.repeat(face_starts(sizes), sizes)
    within = numpy.arange(total, dtype=numpy.int64) - starts
    return starts + numpy.repeat(sizes.astype(numpy.int64), sizes) - 1 - within


def quads_to_corners(quads):
    """int32x4 faces (triangle = 4th index -1) -> (sizes, flat corners)."""
    sizes = numpy.where(quads[:, 3] >= 0, 4, 3).astype(numpy.int32)
    return sizes, quads[quads >= 0].astype(numpy.int32)


def corners_to_quads(sizes, *arrays):
    """(sizes, corner arrays) -> int32x4 faces, fanning anything past a quad.

    Returns (quads, face_map) per input array: quads is int32 (n, 4) and
    face_map maps each output face back to its source face (for face groups).
    Faces are grouped by their original size, so ordering changes.
    """
    sizes = numpy.asarray(sizes, numpy.int32)
    starts = face_starts(sizes)
    out = [[] for _ in arrays]
    face_map = []
    for size in numpy.unique(sizes):
        faces = numpy.flatnonzero(sizes == size)
        gather = starts[faces][:, None] + numpy.arange(size)[None, :]
        for slot, values in enumerate(arrays):
            block = numpy.asarray(values)[gather]
            if size == 3:
                quads = numpy.column_stack((block, numpy.full(len(block), -1, numpy.int32)))
            elif size == 4:
                quads = block
            else:  # fan: (0, i, i+1) triangles
                fan = [
                    numpy.column_stack(
                        (block[:, 0], block[:, i], block[:, i + 1],
                         numpy.full(len(block), -1, numpy.int32))
                    )
                    for i in range(1, size - 1)
                ]
                quads = numpy.concatenate(fan) if fan else numpy.empty((0, 4), numpy.int32)
            out[slot].append(quads.astype(numpy.int32))
        repeats = 1 if size <= 4 else size - 2
        face_map.append(numpy.tile(faces, repeats) if repeats > 1 else faces)
    stacked = [numpy.concatenate(part) if part else numpy.empty((0, 4), numpy.int32) for part in out]
    mapping = numpy.concatenate(face_map) if face_map else numpy.empty(0, numpy.int64)
    return stacked, mapping


# ------------------------------------------------------------------- decoding

def decode_mesh(header, binary):
    """mesh_full -> a plain dict the SOP can rebuild from (and deltas patch)."""
    count = int(header["vertex_count"])
    faces = int(header["face_count"])
    mesh = {
        "mesh_id": header.get("mesh_id", ""),
        "geometry_id": header.get("geometry_id", ""),
        "name": header.get("name", "nomad"),
        "world_matrix": list(header.get("world_matrix", IDENTITY)),
        "visible": True,
        "smooth_shading": bool(header.get("smooth_shading", True)),
        "positions": _read(binary, header["position_offset"], count * 3, "<f4").reshape(-1, 3).copy(),
    }

    if header.get("face_format") == "corners":
        corner_count = int(header["corner_count"])
        mesh["sizes"] = _read(binary, header["face_size_offset"], faces, "<i4").copy()
        mesh["corners"] = _read(binary, header["corner_vertex_offset"], corner_count, "<i4").copy()
        uv_offset = header.get("corner_texcoord_offset")
        corner_uv = _read(binary, uv_offset, corner_count, "<i4").copy() if uv_offset is not None else None
    else:
        quads = _read(binary, header["face_offset"], faces * 4, "<i4").reshape(-1, 4)
        mesh["sizes"], mesh["corners"] = quads_to_corners(quads)
        corner_uv = None
        if "face_uv_offset" in header:
            uv_quads = _read(binary, header["face_uv_offset"], faces * 4, "<i4").reshape(-1, 4)
            corner_uv = uv_quads[quads >= 0].astype(numpy.int32).copy()

    if "texcoord_count" in header and corner_uv is not None:
        texcoords = _read(binary, header["texcoord_offset"], int(header["texcoord_count"]) * 2, "<f4")
        mesh["texcoords"] = texcoords.reshape(-1, 2).copy()
        mesh["corner_uv"] = corner_uv

    if "color_offset" in header:
        mesh["color"] = decode_rgbm(binary, header["color_offset"], count)
    if "opacity_offset" in header:
        mesh["alpha"] = _read(binary, header["opacity_offset"], count, "u1").astype(numpy.float32) / 255.0
    for houdini_name, channel, _fmt, scale in SCALAR_CHANNELS:
        offset = header.get(channel + "_offset")
        if offset is None:
            continue
        dtype = "u2" if scale > 255.0 else "u1"
        mesh[houdini_name] = _read(binary, offset, count, dtype).astype(numpy.float32) / scale

    if "face_group_offset" in header:
        mesh["face_group"] = _read(binary, header["face_group_offset"], faces, "<u2").astype(numpy.int32)
        mesh["face_group_names"] = [
            str(group.get("name", "group%d" % i))
            for i, group in enumerate(header.get("face_groups", []))
        ]
    return mesh


def apply_delta(mesh, header, binary):
    """mesh_delta -> patch the cached mesh in place. False if topology moved on."""
    if int(header.get("vertex_count", len(mesh["positions"]))) != len(mesh["positions"]):
        return False
    count = int(header["count"])
    indices = _read(binary, header["index_offset"], count, "<u4").astype(numpy.int64)
    if "position_offset" in header:
        mesh["positions"][indices] = _read(
            binary, header["position_offset"], count * 3, "<f4"
        ).reshape(-1, 3)
    if "color_offset" in header and "color" in mesh:
        mesh["color"][indices] = decode_rgbm(binary, header["color_offset"], count)
    if "opacity_offset" in header and "alpha" in mesh:
        mesh["alpha"][indices] = _read(binary, header["opacity_offset"], count, "u1").astype(numpy.float32) / 255.0
    for houdini_name, channel, _fmt, scale in SCALAR_CHANNELS:
        offset = header.get(channel + "_offset")
        if offset is None or houdini_name not in mesh:
            continue
        dtype = "u2" if scale > 255.0 else "u1"
        mesh[houdini_name][indices] = _read(binary, offset, count, dtype).astype(numpy.float32) / scale
    if "world_matrix" in header:
        mesh["world_matrix"] = list(header["world_matrix"])
    return True


# ------------------------------------------------------------------- encoding

def encode_mesh(*, mesh_id, geometry_id, name, positions, sizes, corners,
                texcoords=None, corner_uv=None, point_attribs=None, face_group=None,
                face_group_names=(), world_matrix=None, ngon=True, smooth_shading=True,
                request_id="", live_sync=False):
    """Build a mesh_full (header, binary) from flat arrays.

    `positions` is (n, 3) float, `sizes`/`corners` describe the faces, and
    `point_attribs` is a dict with any of color (n, 3), alpha, rough, metallic,
    mask, density (n,).
    """
    point_attribs = point_attribs or {}
    positions = numpy.ascontiguousarray(positions, "<f4")
    sizes = numpy.asarray(sizes, numpy.int32)
    corners = numpy.asarray(corners, numpy.int32)
    if corner_uv is None and texcoords is not None:
        corner_uv = numpy.arange(len(corners), dtype=numpy.int32)

    binary = bytearray()
    header = {
        "type": "mesh_full",
        "mesh_id": mesh_id,
        "geometry_id": geometry_id,
        "name": name,
        "vertex_count": int(len(positions)),
        "face_count": int(len(sizes)),
        "coordinate_system": "nomad_y_up",
        "world_matrix": list(world_matrix or IDENTITY),
        "smooth_shading": bool(smooth_shading),
        "live_sync": bool(live_sync),
        "replace_topology": True,
        "position_offset": 0,
        "position_format": "float32x3",
    }
    if request_id:
        header["request_id"] = request_id
    binary.extend(positions.tobytes())

    if ngon:
        header["face_format"] = "corners"
        header["corner_count"] = int(len(corners))
        header["face_size_offset"] = len(binary)
        binary.extend(sizes.astype("<i4").tobytes())
        header["corner_vertex_offset"] = len(binary)
        binary.extend(corners.astype("<i4").tobytes())
        if corner_uv is not None:
            header["corner_texcoord_offset"] = len(binary)
            binary.extend(numpy.asarray(corner_uv, "<i4").tobytes())
        groups = face_group
    else:
        inputs = [corners] if corner_uv is None else [corners, corner_uv]
        packed, mapping = corners_to_quads(sizes, *inputs)
        header["face_count"] = int(len(packed[0]))
        header["face_format"] = "int32x4"
        header["face_offset"] = len(binary)
        binary.extend(packed[0].astype("<i4").tobytes())
        if corner_uv is not None:
            header["face_uv_offset"] = len(binary)
            binary.extend(packed[1].astype("<i4").tobytes())
        groups = None if face_group is None else numpy.asarray(face_group)[mapping]

    if texcoords is not None:
        texcoords = numpy.asarray(texcoords, numpy.float32).reshape(-1, 2)
        header["texcoord_count"] = int(len(texcoords))
        header["texcoord_offset"] = len(binary)
        header["texcoord_format"] = "float32x2"
        binary.extend(numpy.ascontiguousarray(texcoords, "<f4").tobytes())

    color = point_attribs.get("color")
    if color is not None:
        header["color_offset"] = len(binary)
        header["color_format"] = "rgbm8"
        binary.extend(encode_rgbm(color).tobytes())
    alpha = point_attribs.get("alpha")
    if alpha is not None:
        header["opacity_offset"] = len(binary)
        header["opacity_format"] = "uint8_norm"
        binary.extend(_pack_unit(alpha, 255.0, "u1").tobytes())
    for houdini_name, channel, fmt, scale in SCALAR_CHANNELS:
        values = point_attribs.get(houdini_name)
        if values is None:
            continue
        header[channel + "_offset"] = len(binary)
        header[channel + "_format"] = fmt
        binary.extend(_pack_unit(values, scale, "u2" if scale > 255.0 else "u1").tobytes())

    if groups is not None:
        header["face_group_offset"] = len(binary)
        header["face_group_format"] = "uint16"
        binary.extend(numpy.clip(groups, 0, 65535).astype("<u2").tobytes())
        header["face_groups"] = [{"name": str(n)} for n in face_group_names]

    header["binary_size"] = len(binary)
    return header, bytes(binary)


def _pack_unit(values, scale, dtype):
    unit = numpy.clip(numpy.asarray(values, numpy.float32), 0.0, 1.0)
    return numpy.round(unit * scale).astype(dtype)


def transform_points(positions, matrix, inverse=False):
    """Apply a Nomad column-major world_matrix (or its inverse) to (n, 3) points."""
    m = numpy.array(matrix, numpy.float64).reshape(4, 4, order="F")
    if inverse:
        m = numpy.linalg.inv(m)
    return (numpy.asarray(positions, numpy.float64) @ m[:3, :3].T + m[:3, 3]).astype(numpy.float32)
