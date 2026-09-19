# SPDX-License-Identifier: MIT
"""Author the Nomad scene onto a USD stage.

Nomad's model is glTF-shaped -- Y-up, column-major matrices, cameras down -Z,
a PBR material block -- so it lands on USD with very little translation:

    meshes      UsdGeom.Mesh, face groups as UsdGeomSubset
    materials   UsdPreviewSurface + UsdUVTexture (+ UsdTransform2d for uv xforms)
    lights      UsdLux Sphere / Distant / Rect / Dome, spots via ShapingAPI
    cameras     UsdGeom.Camera

Note there is no winding flip here, unlike the SOP path: USD's default
`rightHanded` orientation is glTF's, so Nomad's faces are already correct.

Nothing here touches hou -- it takes a stage and the client's cache.
"""
import math
import os

import numpy
from pxr import Gf, Sdf, Tf, UsdGeom, UsdLux, UsdShade, Vt

from . import convert, openpbr

IDENTITY = convert.IDENTITY
ROOT = "/nomad"
MATERIALS = ROOT + "/Materials"

# Nomad texture channel -> (UsdPreviewSurface input, value type, is colour)
TEXTURE_INPUTS = {
    "color": ("diffuseColor", Sdf.ValueTypeNames.Color3f, True),
    "roughness": ("roughness", Sdf.ValueTypeNames.Float, False),
    "metalness": ("metallic", Sdf.ValueTypeNames.Float, False),
    "normal": ("normal", Sdf.ValueTypeNames.Normal3f, False),
    "emissive": ("emissiveColor", Sdf.ValueTypeNames.Color3f, True),
    "occlusion": ("occlusion", Sdf.ValueTypeNames.Float, False),
    "opacity": ("opacity", Sdf.ValueTypeNames.Float, False),
    "displacement": ("displacement", Sdf.ValueTypeNames.Float, False),
}
CHANNEL_SUFFIX = {"color": "rgb", "emissive": "rgb", "normal": "rgb"}
WRAP = {"repeat": "repeat", "clamp": "clamp", "mirror": "mirror"}

# Nomad paints per vertex, so a channel the user has painted must be read from a
# primvar rather than taken from the material's single value:
#   nomad channel -> (mesh key, primvar, reader id, value type, shader input)
PAINT_CHANNELS = {
    "color": ("color", "displayColor", "UsdPrimvarReader_float3",
              Sdf.ValueTypeNames.Color3f, "diffuseColor"),
    "opacity": ("alpha", "displayOpacity", "UsdPrimvarReader_float",
                Sdf.ValueTypeNames.Float, "opacity"),
    "roughness": ("rough", "rough", "UsdPrimvarReader_float",
                  Sdf.ValueTypeNames.Float, "roughness"),
    "metalness": ("metallic", "metallic", "UsdPrimvarReader_float",
                  Sdf.ValueTypeNames.Float, "metallic"),
}


def _array(values, vt_type, dtype):
    flat = numpy.ascontiguousarray(values, dtype)
    try:
        return vt_type.FromNumpy(flat)
    except (AttributeError, TypeError):
        return vt_type(flat.tolist())


def matrix(values):
    """Nomad's column-major 16 floats -> Gf.Matrix4d.

    USD stores row-major and multiplies row vectors, which makes its layout the
    transpose of glTF's -- so the flat list transfers straight across.
    """
    return Gf.Matrix4d(*[float(v) for v in values])


def unique_child(parent_path, name, taken):
    """A valid, unused prim name for a user-facing Nomad object name."""
    base = Tf.MakeValidIdentifier(name or "unnamed")
    candidate, index = base, 1
    while candidate in taken:
        index += 1
        candidate = "%s_%d" % (base, index)
    taken.add(candidate)
    return parent_path + "/" + candidate


def author_scene(stage, cache, *, scale=1.0, import_materials=True, import_lights=True,
                 import_cameras=True, import_environment=True, light_scale=1.0,
                 material_style="openpbr", environment_path=""):
    """Write everything the client has cached onto `stage`. Returns prim paths."""
    UsdGeom.Xform.Define(stage, ROOT)
    try:
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    except Tf.ErrorException:
        pass  # a LOP edits a sublayer and cannot set stage metadata; Houdini is Y-up already
    written = []
    taken = set()

    materials = {}
    keys = {}
    if import_materials:
        keys = material_keys(cache)
        wanted = []
        for key in keys.values():
            if key not in wanted:
                wanted.append(key)
        if wanted:
            UsdGeom.Scope.Define(stage, MATERIALS)
        material_names = set()
        for key in wanted:
            mesh = cache.meshes.get(key)
            block = cache.materials.get(key, {})
            name = mesh["name"] if mesh else key
            path = unique_child(MATERIALS, name, material_names)
            builder = openpbr.author if material_style == "openpbr" else author_material
            materials[key] = builder(stage, path, block, cache.textures, mesh)
            written.append(path)

    # objects nest under their parent when Nomad names one, else under /nomad
    entries = {}
    for mesh_id in cache.order:
        if mesh_id in cache.meshes:
            entries[mesh_id] = ("mesh", cache.meshes[mesh_id])
    if import_lights:
        for link_id, light in cache.lights.items():
            entries[link_id] = ("light", light)
    if import_cameras:
        for link_id, camera in cache.cameras.items():
            entries[link_id] = ("camera", camera)
    # transform-only nodes (0.11.37): Nomad groups, and the synthetic /skew wrappers
    # sent to peers that cannot hold a skewed matrix -- we advertise `skew`, so we
    # should not see those, but a group is a plain Xform either way
    for link_id, group in getattr(cache, "groups", {}).items():
        entries.setdefault(link_id, ("group", group))

    if import_environment:
        dome = author_environment(stage, getattr(cache, "display", {}), cache.textures,
                                  light_scale=light_scale, search_path=environment_path)
        if dome is not None:
            written.append(dome.GetPath().pathString)

    children = {}
    for link_id, (_kind, entry) in entries.items():
        parent = entry.get("parent_id") or ""
        # an unknown parent is not an error: keep the node at the root and let a
        # later cook re-parent it once the parent arrives (PROTOCOL.md section 9)
        children.setdefault(parent if parent in entries else "", []).append(link_id)
    # child_index is advisory sibling order; ties keep arrival order
    for siblings in children.values():
        order = {link_id: index for index, link_id in enumerate(siblings)}
        siblings.sort(key=lambda link_id: (
            entries[link_id][1].get("child_index") if
            entries[link_id][1].get("child_index") is not None else order[link_id],
            order[link_id]))

    names = {"": taken}
    problems = []

    def author_branch(parent_id, parent_path):
        for link_id in children.get(parent_id, ()):
            try:
                path = author_one(parent_id, parent_path, link_id)
            except Exception as exc:  # one bad object must not truncate the scene
                problems.append("%s: %s" % (entries[link_id][1].get("name", link_id), exc))
                continue
            written.append(path)
            author_branch(link_id, path)

    def author_one(parent_id, parent_path, link_id):
        """Author one node and return its prim path."""
        kind, entry = entries[link_id]
        label = entry.get("name") or kind
        path = unique_child(parent_path, label, names.setdefault(parent_id, set()))
        world = entry.get("world_matrix", IDENTITY)
        local = world
        if parent_id:
            # local_matrix is relative to world_matrix_parent, which is NOT always
            # the parent's world matrix: a skewed transform splits again and the
            # extra frame belongs between the parent and the node (PROTOCOL.md
            # section 3). Using the pair under our parent prim then loses whatever
            # that frame held -- typically a non-uniform scale. So take the pair
            # only when the frame it is relative to is the parent we authored, and
            # otherwise derive the local transform from the two world matrices.
            # USD holds a skewed matrix directly, which is why we advertise `skew`.
            parent_entry = entries[parent_id][1]
            parent_world = parent_entry.get("world_matrix", IDENTITY)
            pair = entry.get("local_matrix")
            frame = entry.get("world_matrix_parent")
            if pair is not None and frame is not None and convert.matrices_close(frame, parent_world):
                local = pair
            else:
                local = convert.compose_local(world, parent_world)
        if kind == "mesh":
            prim = author_mesh(stage, path, entry, scale=scale, matrix_values=local)
            material = materials.get(keys.get(link_id, link_id))
            if material is not None:
                UsdShade.MaterialBindingAPI.Apply(prim.GetPrim())
                UsdShade.MaterialBindingAPI(prim.GetPrim()).Bind(material)
        elif kind == "light":
            author_light(stage, path, entry, scale=scale, light_scale=light_scale,
                         matrix_values=local)
        elif kind == "camera":
            author_camera(stage, path, entry, scale=scale, matrix_values=local)
        else:
            author_group(stage, path, entry, scale=scale, matrix_values=local)
        return path

    author_branch("", ROOT)
    if problems:
        report_problems(problems)
    return written


# ----------------------------------------------------------------------- mesh

def report_problems(problems):
    """Anything odd hit while authoring. Overridden by the node side so it surfaces."""
    print("nomad_link: %d note(s) while authoring: %s"
          % (len(problems), "; ".join(problems[:5])))


def material_keys(cache):
    """mesh_id -> the mesh_id whose material block it should use.

    Link keys a material by mesh_id, one block per mesh, with no notion of
    sharing. A copy therefore has no material of its own: mesh_instance says
    which mesh it came from, and anything else that reuses a geometry_id (array
    repeaters, say) is matched on that. A mesh with only vertex paint still gets
    a material of its own so the paint has something to render through.
    """
    # a painted mesh needs a material even when Nomad sent no material block
    painted = {mesh_id for mesh_id, mesh in cache.meshes.items()
               if any(spec[0] in mesh for spec in PAINT_CHANNELS.values())}
    by_geometry = {}
    for mesh_id in cache.order:
        mesh = cache.meshes.get(mesh_id)
        if mesh is not None and mesh_id in cache.materials:
            by_geometry.setdefault(mesh.get("geometry_id") or mesh_id, mesh_id)

    keys = {}
    for mesh_id in cache.order:
        mesh = cache.meshes.get(mesh_id)
        if mesh is None:
            continue
        if mesh_id in cache.materials:
            keys[mesh_id] = mesh_id
            continue
        source = mesh.get("material_source")
        if source not in cache.materials:
            source = by_geometry.get(mesh.get("geometry_id"))
        if source in cache.materials:
            keys[mesh_id] = source
        elif mesh_id in painted:
            keys[mesh_id] = mesh_id
    return keys


def author_mesh(stage, path, mesh, scale=1.0, matrix_values=None):
    geom = UsdGeom.Mesh.Define(stage, path)
    prim = geom.GetPrim()

    points = numpy.asarray(mesh["positions"], numpy.float32) * scale
    geom.CreatePointsAttr(_array(points, Vt.Vec3fArray, "f4"))
    geom.CreateFaceVertexCountsAttr(_array(mesh["sizes"], Vt.IntArray, "i4"))
    geom.CreateFaceVertexIndicesAttr(_array(mesh["corners"], Vt.IntArray, "i4"))
    geom.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    if len(points):
        geom.CreateExtentAttr(_array([points.min(axis=0), points.max(axis=0)],
                                     Vt.Vec3fArray, "f4"))
    if not mesh.get("smooth_shading", True):
        normals = flat_normals(points, mesh["sizes"], mesh["corners"])
        geom.CreateNormalsAttr(_array(normals, Vt.Vec3fArray, "f4"))
        geom.SetNormalsInterpolation(UsdGeom.Tokens.faceVarying)

    values = mesh["world_matrix"] if matrix_values is None else matrix_values
    UsdGeom.Xformable(prim).AddTransformOp().Set(matrix(scaled_matrix(values, scale)))
    if not mesh.get("visible", True):
        UsdGeom.Imageable(prim).CreateVisibilityAttr(UsdGeom.Tokens.invisible)

    api = UsdGeom.PrimvarsAPI(prim)
    if "texcoords" in mesh:
        uvs = mesh["texcoords"][numpy.asarray(mesh["corner_uv"], numpy.int64)]
        uvs = numpy.column_stack((uvs[:, 0], 1.0 - uvs[:, 1]))  # USD's v origin is bottom-left
        primvar = api.CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray,
                                    UsdGeom.Tokens.faceVarying)
        primvar.Set(_array(uvs, Vt.Vec2fArray, "f4"))
    if "color" in mesh:
        primvar = api.CreatePrimvar("displayColor", Sdf.ValueTypeNames.Color3fArray,
                                    UsdGeom.Tokens.vertex)
        primvar.Set(_array(mesh["color"], Vt.Vec3fArray, "f4"))
    if "alpha" in mesh:
        primvar = api.CreatePrimvar("displayOpacity", Sdf.ValueTypeNames.FloatArray,
                                    UsdGeom.Tokens.vertex)
        primvar.Set(_array(mesh["alpha"], Vt.FloatArray, "f4"))
    for key in ("rough", "metallic", "mask", "density"):
        if key in mesh:
            primvar = api.CreatePrimvar(key, Sdf.ValueTypeNames.FloatArray,
                                        UsdGeom.Tokens.vertex)
            primvar.Set(_array(mesh[key], Vt.FloatArray, "f4"))

    if "face_group" in mesh:
        author_face_groups(geom, mesh)

    prim.SetCustomDataByKey("nomad:mesh_id", mesh.get("mesh_id", ""))
    if mesh.get("locked"):
        prim.SetCustomDataByKey("nomad:locked", True)
    prim.SetCustomDataByKey("nomad:geometry_id", mesh.get("geometry_id", ""))
    return geom


def author_face_groups(geom, mesh):
    """Nomad face groups become UsdGeomSubsets, one per group that has faces."""
    groups = numpy.asarray(mesh["face_group"], numpy.int32)
    names = list(mesh.get("face_group_names", ()))
    for index in numpy.unique(groups):
        faces = numpy.flatnonzero(groups == index)
        label = names[index] if index < len(names) else "group%d" % index
        subset = UsdGeom.Subset.CreateGeomSubset(
            geom, Tf.MakeValidIdentifier(label), UsdGeom.Tokens.face,
            _array(faces, Vt.IntArray, "i4"), "nomadFaceGroup",
        )
        subset.GetPrim().SetCustomDataByKey("nomad:face_group", int(index))


def flat_normals(points, sizes, corners):
    """One normal per corner from the face plane, for flat-shaded meshes."""
    sizes = numpy.asarray(sizes, numpy.int64)
    corners = numpy.asarray(corners, numpy.int64)
    starts = numpy.concatenate(([0], numpy.cumsum(sizes)[:-1]))
    first, second, third = (points[corners[starts]], points[corners[starts + 1]],
                            points[corners[starts + 2]])
    face = numpy.cross(second - first, third - first)
    lengths = numpy.linalg.norm(face, axis=1)
    lengths[lengths == 0.0] = 1.0
    return numpy.repeat(face / lengths[:, None], sizes, axis=0)


def scaled_matrix(values, scale):
    """Scale the translation to match scaled points, leaving rotation alone."""
    if scale == 1.0:
        return values
    out = list(values)
    out[12], out[13], out[14] = out[12] * scale, out[13] * scale, out[14] * scale
    return out


# ------------------------------------------------------------------- material

def author_material(stage, path, block, textures, mesh=None):
    material = UsdShade.Material.Define(stage, path)
    shader = UsdShade.Shader.Define(stage, path + "/Preview")
    shader.CreateIdAttr("UsdPreviewSurface")
    material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")

    channels = block.get("textures") or {}
    # a bound texture replaces the scalar for these channels (PROTOCOL.md section 10),
    # so do not author a value the texture connection would only override
    textured = {name for name, channel in channels.items()
                if textures.get(channel.get("texture_id"))}
    # likewise for vertex paint, which is what most Nomad sculpts actually carry
    painted = {name for name, spec in PAINT_CHANNELS.items()
               if name not in textured and mesh is not None and spec[0] in mesh}
    for name in painted:
        author_paint_reader(stage, path, shader, name)

    colour = block.get("color")
    if colour is not None and not {"color"} & (textured | painted):
        shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*colour[:3]))
    for key, name in (("roughness", "roughness"), ("metalness", "metallic"),
                      ("opacity", "opacity")):
        if key in block and key not in textured and key not in painted:
            shader.CreateInput(name, Sdf.ValueTypeNames.Float).Set(float(block[key]))
    if "refraction_ior" in block:
        shader.CreateInput("ior", Sdf.ValueTypeNames.Float).Set(float(block["refraction_ior"]))
    if block.get("material_type") == "blending" or float(block.get("opacity", 1.0)) < 1.0:
        shader.CreateInput("opacityThreshold", Sdf.ValueTypeNames.Float).Set(0.0)
    if block.get("two_sided_value"):
        pass  # doubleSided lives on the mesh, not the shader

    if any(channel.get("texture_id") for channel in channels.values()):
        reader = UsdShade.Shader.Define(stage, path + "/stReader")
        reader.CreateIdAttr("UsdPrimvarReader_float2")
        reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")
        for name, channel in channels.items():
            author_texture(stage, path, shader, reader, name, channel, textures)

    # Nomad extras UsdPreviewSurface cannot express, kept so nothing is silently lost
    extras = {key: value for key, value in block.items()
              if key.startswith(("subsurface", "absorption", "translucency", "refraction"))
              or key in ("material_type", "reflectance", "shadow_color")}
    if extras:
        material.GetPrim().SetCustomDataByKey("nomad:material", extras)
    return material


def author_paint_reader(stage, path, shader, name):
    """Read a painted channel off the mesh's primvar instead of a flat value."""
    _key, primvar, reader_id, value_type, shader_input = PAINT_CHANNELS[name]
    node = UsdShade.Shader.Define(stage, "%s/%s_paint" % (path, Tf.MakeValidIdentifier(name)))
    node.CreateIdAttr(reader_id)
    node.CreateInput("varname", Sdf.ValueTypeNames.Token).Set(primvar)
    node.CreateOutput("result", value_type)
    shader.CreateInput(shader_input, value_type).ConnectToSource(node.ConnectableAPI(), "result")
    return node


def author_texture(stage, path, shader, reader, name, channel, textures):
    target = TEXTURE_INPUTS.get(name)
    texture_id = channel.get("texture_id")
    if target is None or not texture_id:
        return
    blob = textures.get(texture_id)
    if blob is None:
        return  # not arrived yet; the client has asked for it and we recook on arrival

    input_name, value_type, is_colour = target
    node = UsdShade.Shader.Define(stage, "%s/%s_texture" % (path, Tf.MakeValidIdentifier(name)))
    node.CreateIdAttr("UsdUVTexture")
    node.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(blob["path"])
    node.CreateInput("wrapS", Sdf.ValueTypeNames.Token).Set(WRAP.get(channel.get("wrap_s"), "repeat"))
    node.CreateInput("wrapT", Sdf.ValueTypeNames.Token).Set(WRAP.get(channel.get("wrap_t"), "repeat"))
    node.CreateInput("sourceColorSpace", Sdf.ValueTypeNames.Token).Set(
        "sRGB" if is_colour else "raw")

    factor = channel.get("factor")
    if factor is not None:
        values = [float(f) for f in factor] if isinstance(factor, (list, tuple)) else [float(factor)] * 3
        node.CreateInput("scale", Sdf.ValueTypeNames.Float4).Set(
            Gf.Vec4f(values[0], values[1 % len(values)], values[2 % len(values)], 1.0))

    source = reader
    transform = uv_transform(stage, path, name, channel, reader)
    if transform is not None:
        source = transform
    node.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(
        source.ConnectableAPI(), "result")

    suffix = CHANNEL_SUFFIX.get(name, "r")
    node.CreateOutput(suffix, value_type)
    shader.CreateInput(input_name, value_type).ConnectToSource(node.ConnectableAPI(), suffix)


def uv_transform(stage, path, name, channel, reader):
    """Nomad's per-channel uv offset/scale/rotation as a UsdTransform2d."""
    offset = channel.get("offset", [0.0, 0.0])
    scale = channel.get("scale", [1.0, 1.0])
    rotation = float(channel.get("rotation", 0.0))
    if list(offset) == [0.0, 0.0] and list(scale) == [1.0, 1.0] and rotation == 0.0:
        return None
    node = UsdShade.Shader.Define(stage, "%s/%s_uv" % (path, Tf.MakeValidIdentifier(name)))
    node.CreateIdAttr("UsdTransform2d")
    node.CreateInput("in", Sdf.ValueTypeNames.Float2).ConnectToSource(
        reader.ConnectableAPI(), "result")
    # our st is v-flipped relative to Nomad, which turns T + Rz(-r).S into
    # T' + Rz(r).S' with T' = (Tx, 1-Ty) and S' = (Sx, -Sy)
    node.CreateInput("translation", Sdf.ValueTypeNames.Float2).Set(
        Gf.Vec2f(float(offset[0]), 1.0 - float(offset[1])))
    node.CreateInput("scale", Sdf.ValueTypeNames.Float2).Set(
        Gf.Vec2f(float(scale[0]), -float(scale[1])))
    node.CreateInput("rotation", Sdf.ValueTypeNames.Float).Set(math.degrees(rotation))
    node.CreateOutput("result", Sdf.ValueTypeNames.Float2)
    return node


def author_group(stage, path, group, scale=1.0, matrix_values=None):
    """A transform-only node: Nomad group, Blender empty, USD Xform."""
    xform = UsdGeom.Xform.Define(stage, path)
    prim = xform.GetPrim()
    values = group.get("world_matrix", IDENTITY) if matrix_values is None else matrix_values
    UsdGeom.Xformable(prim).AddTransformOp().Set(matrix(scaled_matrix(values, scale)))
    if not group.get("visible", True):
        UsdGeom.Imageable(prim).CreateVisibilityAttr(UsdGeom.Tokens.invisible)
    prim.SetCustomDataByKey("nomad:link_id", group.get("link_id", ""))
    if group.get("locked"):
        prim.SetCustomDataByKey("nomad:locked", True)
    return xform


# ---------------------------------------------------------------- environment

# Nomad's environment lives in shading_config (PROTOCOL.md 10.1). Keep the old
# candidates as read compatibility for scenes cached by pre-0.11.38 bridges.
ENVIRONMENT_KEYS = {
    "intensity": ("env_intensity", "env_factor", "env_power"),
    "exposure": ("environment_exposure", "env_exposure"),
    "rotation": ("environment_rotation", "env_rotation", "env_rotate",
                 "env_orientation", "env_angle"),
    # Legacy display_config could point at the texture cache. Current custom
    # environments use asset blobs, which this bridge does not advertise yet.
    "texture": ("env_texture_id", "env_texture", "env_image", "env_map"),
    "name": ("environment_name", "env_name", "env_preset", "env_id"),
    "enable": ("environment_enable", "env_enable", "env_visible", "show_env"),
    "blur": ("background_blur",),
}


def environment_value(display, kind):
    for key in ENVIRONMENT_KEYS[kind]:
        if key in display:
            return key, display[key]
    return None, None


def find_hdri(name, search_path):
    """Nomad names a built-in HDRI rather than sending pixels; look for the file."""
    if not name or not search_path:
        return ""
    for folder in str(search_path).split(os.pathsep):
        candidate = os.path.join(os.path.expanduser(folder.strip()), os.path.basename(name))
        if folder.strip() and os.path.isfile(candidate):
            return candidate
    return ""


def author_environment(stage, display, textures, light_scale=1.0, search_path=""):
    """Nomad's environment as a UsdLux.DomeLight."""
    if not display:
        return None
    if not any(key.startswith(("env_", "environment_")) or key == "background_blur"
               for key in display):
        return None

    dome = UsdLux.DomeLight.Define(stage, ROOT + "/Environment")
    prim = dome.GetPrim()

    _key, intensity = environment_value(display, "intensity")
    dome.CreateIntensityAttr(float(intensity if intensity is not None else 1.0) * light_scale)
    _key, exposure = environment_value(display, "exposure")
    if exposure is not None:
        # Nomad's exposure is a linear multiplier; USD's exposure is in stops.
        dome.CreateExposureAttr(math.log(max(float(exposure), 1e-12), 2.0))

    _key, texture_id = environment_value(display, "texture")
    blob = textures.get(texture_id) if texture_id else None
    _key, name = environment_value(display, "name")
    if blob is not None:
        dome.CreateTextureFileAttr(blob["path"])
    else:
        found = find_hdri(name, search_path)
        if found:
            dome.CreateTextureFileAttr(found)
    dome.CreateTextureFormatAttr(UsdLux.Tokens.latlong)

    _key, rotation = environment_value(display, "rotation")
    if rotation:
        # Nomad rotates the environment about up (+Y); USD wants an xform op
        UsdGeom.Xformable(prim).AddRotateYOp().Set(math.degrees(float(rotation))
                                                   if abs(float(rotation)) <= 6.284 else float(rotation))

    _key, enabled = environment_value(display, "enable")
    if enabled is not None and not enabled:
        UsdGeom.Imageable(prim).CreateVisibilityAttr(UsdGeom.Tokens.invisible)

    # keep the whole environment block: the key names are Nomad's, not the spec's,
    # so anything unmapped is still inspectable on the prim
    kept = {key: value for key, value in display.items()
            if key.startswith(("env_", "environment_"))
            or key in ("background_blur", "lights_enable")}
    if kept:
        prim.SetCustomDataByKey("nomad:environment", kept)
    return dome


# ---------------------------------------------------------------------- light

def author_light(stage, path, light, scale=1.0, light_scale=1.0, matrix_values=None):
    kind = str(light.get("light_type", "point")).lower()
    if kind in ("directional", "sun"):
        prim = UsdLux.DistantLight.Define(stage, path)
        prim.CreateAngleAttr(math.degrees(float(light.get("angle", 0.0))))
        intensity = float(light.get("intensity", 1.0))
    elif kind == "area":  # legacy: area was removed from the protocol in 0.11.38
        prim = UsdLux.RectLight.Define(stage, path)
        prim.CreateWidthAttr(float(light.get("size", 1.0)) * scale or 1.0)
        prim.CreateHeightAttr(float(light.get("size", 1.0)) * scale or 1.0)
        intensity = float(light.get("power", 1.0))
    elif kind == "environment":
        prim = UsdLux.DomeLight.Define(stage, path)
        intensity = float(light.get("factor", 1.0))
    else:  # POINT and SPOT are both sphere lights; SPOT adds a cone
        prim = UsdLux.SphereLight.Define(stage, path)
        prim.CreateRadiusAttr(float(light.get("size", 0.0)) * scale)
        prim.CreateTreatAsPointAttr(float(light.get("size", 0.0)) <= 0.0)
        intensity = float(light.get("power", 1.0))
        if kind == "spot":
            shaping = UsdLux.ShapingAPI.Apply(prim.GetPrim())
            # Nomad sends the full outer cone; USD wants the half angle
            shaping.CreateShapingConeAngleAttr(math.degrees(float(light.get("spot_angle", 0.785))) / 2.0)
            softness = float(light.get("spot_softness", 0.5))
            shaping.CreateShapingConeSoftnessAttr(softness)

    prim.CreateIntensityAttr(intensity * light_scale)
    colour = light.get("color")
    if colour is not None:
        prim.CreateColorAttr(Gf.Vec3f(*colour[:3]))
    if light.get("use_kelvin"):
        prim.CreateEnableColorTemperatureAttr(True)
        prim.CreateColorTemperatureAttr(float(light.get("kelvin", 6500)))
    if "shadow_cast" in light:
        UsdLux.ShadowAPI.Apply(prim.GetPrim()).CreateShadowEnableAttr(bool(light["shadow_cast"]))

    values = light.get("world_matrix", IDENTITY) if matrix_values is None else matrix_values
    UsdGeom.Xformable(prim.GetPrim()).AddTransformOp().Set(matrix(scaled_matrix(values, scale)))
    if not light.get("visible", True):
        UsdGeom.Imageable(prim.GetPrim()).CreateVisibilityAttr(UsdGeom.Tokens.invisible)
    prim.GetPrim().SetCustomDataByKey("nomad:link_id", light.get("link_id", ""))
    return prim


# --------------------------------------------------------------------- camera

def author_camera(stage, path, camera, scale=1.0, aperture=24.0, matrix_values=None):
    """Nomad and USD cameras both look down -Z with +Y up, so the matrix transfers."""
    geom = UsdGeom.Camera.Define(stage, path)
    if camera.get("orthographic"):
        geom.CreateProjectionAttr(UsdGeom.Tokens.orthographic)
        # USD ortho apertures are in tenths of a scene unit
        size = float(camera.get("ortho_scale", 1.0)) * scale * 10.0
        geom.CreateHorizontalApertureAttr(size)
        geom.CreateVerticalApertureAttr(size)
    else:
        geom.CreateProjectionAttr(UsdGeom.Tokens.perspective)
        fov = math.radians(float(camera.get("fov_y", 50.0)))
        geom.CreateVerticalApertureAttr(aperture)
        geom.CreateHorizontalApertureAttr(aperture * 16.0 / 9.0)
        geom.CreateFocalLengthAttr((aperture / 2.0) / math.tan(fov / 2.0))

    geom.CreateClippingRangeAttr(Gf.Vec2f(0.01 * scale, 1000000.0 * scale))
    values = camera.get("world_matrix", IDENTITY) if matrix_values is None else matrix_values
    UsdGeom.Xformable(geom.GetPrim()).AddTransformOp().Set(matrix(scaled_matrix(values, scale)))
    prim = geom.GetPrim()
    prim.SetCustomDataByKey("nomad:link_id", camera.get("link_id", ""))
    if "pivot" in camera:
        prim.SetCustomDataByKey("nomad:pivot", Gf.Vec3d(*[float(v) for v in camera["pivot"][:3]]))
    return geom
