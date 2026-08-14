# SPDX-License-Identifier: MIT
"""Nomad Link mesh payloads -> Toolbag render meshes.

Nomad and Toolbag are both right-handed Y-up with counter-clockwise front faces,
so positions and winding travel unchanged. Two conversions do matter:

- Nomad indexes UVs per face corner, Toolbag stores one UV per vertex, so
  vertices are split along UV seams. Meshes without UVs -- most sculpts -- skip
  the split and keep Nomad's own indexing.
- Nomad places a node with `world_matrix`; Toolbag's transform is Euler and its
  order is undocumented, so the matrix is baked into the positions and the object
  stays at the origin. Skewed nodes and instances then come out exact.

Cached arrays are flat `array.array` throughout. There is no numpy path: Toolbag
runs plugins in a sub-interpreter, which numpy does not support and can crash in,
so the standard library is all there is.

Nothing here imports mset, so it can all be tested outside Toolbag.
"""
import array
import math

IDENTITY = [1.0 if i % 5 == 0 else 0.0 for i in range(16)]

# Nomad's v origin is top-left (glTF); Toolbag reads UVs bottom-up like OBJ/FBX.
FLIP_V = True

# Toolbag's polygon table is (start, count) pairs, but the units are undocumented:
# triangles, or indices into them? If it is indices, Toolbag reads three times past
# the end of the array and takes the app down. Off until probe.py confirms it; the
# cost is a triangle wireframe and no quad-aware subdivision, nothing visual.
SEND_POLYGONS = False

# per-vertex paint: nomad channel -> (cache key, byte width, scale)
SCALAR_CHANNELS = (
    ("opacity", "alpha", 1, 255.0),
    ("roughness", "roughness", 1, 255.0),
    ("metalness", "metalness", 1, 255.0),
    ("mask", "mask", 2, 65535.0),
)


# ------------------------------------------------------------------ primitives

def _floats(binary, offset, count):
    values = array.array("f")
    values.frombytes(bytes(binary[offset:offset + count * 4]))
    return values


def _ints(binary, offset, count, code="i"):
    values = array.array(code)
    values.frombytes(bytes(binary[offset:offset + count * values.itemsize]))
    return values


def decode_rgbm(binary, offset, count):
    """Nomad's rgbm8 vertex colors: linear rgb = rgb * (m / 65025)."""
    raw = bytes(binary[offset:offset + count * 4])
    out = array.array("f", bytes(count * 12))
    for i in range(count):
        m = raw[i * 4 + 3] / 65025.0
        out[i * 3] = raw[i * 4] * m
        out[i * 3 + 1] = raw[i * 4 + 1] * m
        out[i * 3 + 2] = raw[i * 4 + 2] * m
    return out


def decode_unit(binary, offset, count, width, scale):
    """uint8_norm / uint16_norm channel -> floats in [0, 1]."""
    raw = bytes(binary[offset:offset + count * width])
    values = array.array("B" if width == 1 else "H")
    values.frombytes(raw)
    return array.array("f", [v / scale for v in values])


def transform_points(positions, matrix):
    """Apply a Nomad column-major world_matrix to flat xyz positions."""
    m = list(matrix)
    if m == IDENTITY:
        return positions
    out = array.array("f", positions)
    for i in range(0, len(out), 3):
        x, y, z = out[i], out[i + 1], out[i + 2]
        out[i] = m[0] * x + m[4] * y + m[8] * z + m[12]
        out[i + 1] = m[1] * x + m[5] * y + m[9] * z + m[13]
        out[i + 2] = m[2] * x + m[6] * y + m[10] * z + m[14]
    return out


# -------------------------------------------------------------------- decoding

def decode_mesh(header, binary):
    """mesh_full -> a cache dict in Nomad's own indexing, which deltas patch."""
    count = int(header["vertex_count"])
    faces = int(header["face_count"])
    mesh = {
        "mesh_id": header.get("mesh_id", ""),
        "geometry_id": header.get("geometry_id", ""),
        "name": header.get("name") or "Nomad mesh",
        "world_matrix": list(header.get("world_matrix", IDENTITY)),
        "visible": bool(header.get("visible", True)),
        "smooth_shading": bool(header.get("smooth_shading", True)),
        "material": header.get("material"),
        "vertex_count": count,
        "face_count": faces,
        "positions": _floats(binary, header["position_offset"], count * 3),
        "faces": decode_faces(header, binary, faces),
    }

    if "texcoord_count" in header and "face_uv_offset" in header:
        mesh["texcoords"] = _floats(binary, header["texcoord_offset"], int(header["texcoord_count"]) * 2)
        mesh["face_uv"] = _ints(binary, header["face_uv_offset"], faces * 4)

    if "color_offset" in header:
        mesh["color"] = decode_rgbm(binary, header["color_offset"], count)
    for channel, key, width, scale in SCALAR_CHANNELS:
        offset = header.get(channel + "_offset")
        if offset is not None:
            mesh[key] = decode_unit(binary, offset, count, width, scale)
    return mesh


def decode_faces(header, binary, faces):
    """Both face formats -> int32x4 rows, a triangle keeping -1 in the 4th slot."""
    if header.get("face_format") != "corners":
        return _ints(binary, header["face_offset"], faces * 4)
    sizes = _ints(binary, header["face_size_offset"], faces)
    corners = _ints(binary, header["corner_vertex_offset"], int(header["corner_count"]))
    out = array.array("i")
    at = 0
    for size in sizes:  # fan into quad slots; Nomad splits n-gons on arrival anyway
        for i in range(1, size - 1):
            out.extend((corners[at], corners[at + i], corners[at + i + 1], -1))
        at += size
    return out


def apply_delta(mesh, header, binary):
    """mesh_delta -> patch the cached mesh. False when topology has moved on."""
    if int(header.get("vertex_count", mesh["vertex_count"])) != mesh["vertex_count"]:
        return False
    count = int(header["count"])
    indices = _ints(binary, header["index_offset"], count, "I")
    if "position_offset" in header:
        _patch(mesh["positions"], indices, _floats(binary, header["position_offset"], count * 3), 3)
    if "color_offset" in header and "color" in mesh:
        _patch(mesh["color"], indices, decode_rgbm(binary, header["color_offset"], count), 3)
    for channel, key, width, scale in SCALAR_CHANNELS:
        offset = header.get(channel + "_offset")
        if offset is not None and key in mesh:
            _patch(mesh[key], indices, decode_unit(binary, offset, count, width, scale), 1)
    if "world_matrix" in header:
        mesh["world_matrix"] = list(header["world_matrix"])
    return True


def apply_attributes(mesh, header, binary):
    """mesh_attributes -> replace whole paint arrays, topology untouched."""
    if int(header.get("vertex_count", mesh["vertex_count"])) != mesh["vertex_count"]:
        return False
    count = mesh["vertex_count"]
    if "color_offset" in header:
        mesh["color"] = decode_rgbm(binary, header["color_offset"], count)
    for channel, key, width, scale in SCALAR_CHANNELS:
        offset = header.get(channel + "_offset")
        if offset is not None:
            mesh[key] = decode_unit(binary, offset, count, width, scale)
    return True


def _patch(target, indices, values, width):
    for slot, vertex in enumerate(indices):
        target[vertex * width:vertex * width + width] = values[slot * width:slot * width + width]


# --------------------------------------------------------------- render meshes

def split(mesh):
    """Corner indexing Toolbag can use: one UV, color and normal per vertex.

    Returns (source, corners, uv_of_vertex). `source` maps each Toolbag vertex
    back to its Nomad vertex; `corners` maps each face corner to a Toolbag vertex,
    or None when Nomad's own indexing already works.
    """
    faces = mesh["faces"]
    face_uv = mesh.get("face_uv")
    flat = not mesh.get("smooth_shading", True)
    if face_uv is None and not flat:
        return list(range(mesh["vertex_count"])), None, None

    source = []
    uv_of_vertex = [] if face_uv is not None else None
    corners = array.array("i")
    seen = {}
    for corner in range(len(faces)):
        vertex = faces[corner]
        if vertex < 0:  # unused quad slot on a triangle
            corners.append(-1)
            continue
        uv = face_uv[corner] if face_uv is not None else -1
        key = corner if flat else (vertex, uv)
        index = seen.get(key)
        if index is None:
            index = len(source)
            seen[key] = index
            source.append(vertex)
            if uv_of_vertex is not None:
                uv_of_vertex.append(uv)
        corners.append(index)
    return source, corners, uv_of_vertex


def triangulate(faces, corners):
    """int32x4 faces -> triangle indices + Toolbag (start, count) polygon pairs.

    Counts are in triangles: a quad is one polygon covering two of them, which is
    what keeps Toolbag's wireframe and Catmull-Clark subdivision on the quads.
    """
    triangles = array.array("i")
    polygons = array.array("i")
    at = 0
    for face in range(0, len(faces), 4):
        slots = [face, face + 1, face + 2]
        if faces[face + 3] >= 0:
            slots.append(face + 3)
        indices = [corners[s] if corners is not None else faces[s] for s in slots]
        for i in range(1, len(indices) - 1):
            triangles.extend((indices[0], indices[i], indices[i + 1]))
        polygons.extend((at, len(indices) - 2))
        at += len(indices) - 2
    return triangles, polygons


def topology(mesh):
    """Split, triangulation and UVs, computed once and kept on the mesh.

    A stroke only moves vertices, so this survives every delta and only a fresh
    mesh_full pays for it.
    """
    cached = mesh.get("_topology")
    if cached is not None:
        return cached
    source, corners, uv_of_vertex = split(mesh)
    triangles, polygons = triangulate(mesh["faces"], corners)
    cached = {
        "source": source,
        "identity": corners is None,   # Nomad's indexing works as-is
        "faces": triangles,            # native, for the normal kernel
        "triangles": triangles.tolist(),
        "polygons": polygons.tolist(),
    }
    if uv_of_vertex is not None and "texcoords" in mesh:
        cached["uvs"] = gather_uvs(mesh["texcoords"], uv_of_vertex)
    mesh["_topology"] = cached
    return cached


def build(mesh, with_normals=True, scale=1.0):
    """Cached mesh -> the flat lists mset.Mesh takes, plus the vertex map.

    Everything stays in native arrays until the end, so the conversion to Python
    lists that mset needs happens exactly once per channel. Computing normals is
    the most expensive step, so it is skipped once Toolbag is known to do it.

    `scale` maps Nomad's meters onto the scene unit (100 in a centimeter scene);
    it folds into the baked matrix, so it costs nothing extra.
    """
    shape = topology(mesh)
    matrix = list(mesh["world_matrix"])
    if scale != 1.0:
        for i in range(15):          # uniform scale after M: every row but the 4th
            if i % 4 != 3:
                matrix[i] *= scale
    positions = transform_points(mesh["positions"], matrix)
    vertices = gather(positions, shape["source"], 3, shape["identity"])
    built = {
        "vertices": vertices.tolist(),
        "triangles": shape["triangles"],
        "source": shape["source"],
        "flat": not mesh.get("smooth_shading", True),
    }
    if SEND_POLYGONS:
        built["polygons"] = shape["polygons"]
    if with_normals:
        built["normals"] = normals(vertices, shape["faces"]).tolist()
    if "uvs" in shape:
        built["uvs"] = shape["uvs"]
    if "color" in mesh:
        built["colors"] = gather_colors(mesh, shape["source"], shape["identity"])
    return built


def validate(built):
    """Problems per channel, before any of it reaches Toolbag's C++.

    Toolbag does not appear to bounds-check what Python hands it, and a bad index
    or a short array takes the whole app down with no traceback, so nothing goes
    across until it is consistent.
    """
    problems = {}
    vertices = built.get("vertices") or []
    triangles = built.get("triangles") or []
    count = len(vertices) // 3

    if len(vertices) % 3:
        problems["vertices"] = "%d floats is not a whole number of points" % len(vertices)
    if len(triangles) % 3:
        problems["triangles"] = "%d indices is not a whole number of triangles" % len(triangles)
    elif triangles and (min(triangles) < 0 or max(triangles) >= count):
        problems["triangles"] = ("index out of range: %d..%d for %d vertices"
                                 % (min(triangles), max(triangles), count))

    for field, per_vertex in (("normals", 3), ("uvs", 2), ("colors", 4)):
        values = built.get(field)
        if values is not None and len(values) != count * per_vertex:
            problems[field] = "%d floats, expected %d" % (len(values), count * per_vertex)

    polygons = built.get("polygons")
    if polygons is not None:
        total = len(triangles) // 3
        if len(polygons) % 2:
            problems["polygons"] = "%d values is not whole (start, count) pairs" % len(polygons)
        elif polygons:
            starts, counts = polygons[0::2], polygons[1::2]
            if min(counts) < 1 or sum(counts) != total or max(starts) + counts[-1] > total:
                problems["polygons"] = ("covers %d of %d triangles"
                                        % (sum(counts), total))
    return problems


def gather(values, source, width, identity=False):
    """Pick `width` floats per entry of `source`, staying in a native array."""
    if identity:
        return values
    out = array.array("f")
    for vertex in source:
        out.extend(values[vertex * width:vertex * width + width])
    return out


def gather_uvs(texcoords, uv_of_vertex):
    out = array.array("f")
    for uv in uv_of_vertex:
        if uv < 0:
            out.extend((0.0, 0.0))
            continue
        u, v = texcoords[uv * 2], texcoords[uv * 2 + 1]
        out.extend((u, 1.0 - v if FLIP_V else v))
    return list(out)


def gather_colors(mesh, source, identity=False):
    """rgb + paint opacity -> Toolbag's rgba vertex colors."""
    color = mesh["color"]
    alpha = mesh.get("alpha")
    out = array.array("f")
    for vertex in (range(len(color) // 3) if identity else source):
        out.extend(color[vertex * 3:vertex * 3 + 3])
        out.append(alpha[vertex] if alpha is not None else 1.0)
    return out.tolist()


def normals(vertices, triangles):
    """Area-weighted vertex normals; corners are already split when flat-shaded."""
    if not len(triangles):
        return array.array("f", bytes(len(vertices) * 4))
    out = array.array("f", bytes(len(vertices) * 4))
    for i in range(0, len(triangles), 3):
        ia, ib, ic = triangles[i] * 3, triangles[i + 1] * 3, triangles[i + 2] * 3
        ax, ay, az = vertices[ia], vertices[ia + 1], vertices[ia + 2]
        ux, uy, uz = vertices[ib] - ax, vertices[ib + 1] - ay, vertices[ib + 2] - az
        vx, vy, vz = vertices[ic] - ax, vertices[ic + 1] - ay, vertices[ic + 2] - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        for base in (ia, ib, ic):
            out[base] += nx
            out[base + 1] += ny
            out[base + 2] += nz
    for i in range(0, len(out), 3):
        length = math.sqrt(out[i] ** 2 + out[i + 1] ** 2 + out[i + 2] ** 2)
        if length > 0.0:
            out[i] /= length
            out[i + 1] /= length
            out[i + 2] /= length
    return out
