# SPDX-License-Identifier: MIT
"""Nomad materials as MaterialX OpenPBR, for Karma.

UsdPreviewSurface has nowhere to put most of Nomad's material block --
subsurface, refraction, absorption, reflectance -- and no multiply node, so it
cannot express "material colour times vertex paint times texture" and has to
pick one source per channel. OpenPBR covers the parameters, and MaterialX's
`multiply` and `geompropvalue` nodes cover the compositing.

Verified against MaterialX 1.39.5 as shipped with Houdini 22.

Approximate mappings, flagged because they are judgement calls rather than
translations -- see MAPPING_NOTES:

    reflectance      -> specular_weight     (Nomad's 0.5 = 4% F0 = weight 1.0)
    absorption       -> transmission_depth  (Beer-Lambert distance, inverted)
    interior roughness                      (no OpenPBR equivalent; kept as data)
"""
import math

from pxr import Gf, Sdf, UsdShade

SURFACE = "ND_open_pbr_surface_surfaceshader"
MULTIPLY_COLOR = "ND_multiply_color3"
GEOMPROP_COLOR = "ND_geompropvalue_color3"
GEOMPROP_FLOAT = "ND_geompropvalue_float"
IMAGE_COLOR = "ND_image_color3"
IMAGE_FLOAT = "ND_image_float"
GEOMPROP_UV = "ND_geompropvalue_vector2"
LUMINANCE = "ND_luminance_color3"
MULTIPLY_FLOAT = "ND_multiply_float"
MULTIPLY_UV = "ND_multiply_vector2"
ROTATE_UV = "ND_rotate2d_vector2"
ADD_UV = "ND_add_vector2"
EXTRACT = "ND_extract_color3"

# Nomad's subsurface reads about twice as strong as OpenPBR's at the same weight,
# from comparing a character against Nomad's own render. One constant, so it can be
# overridden (nomad_link.openpbr.SUBSURFACE_WEIGHT = ...) without editing this file.
SUBSURFACE_WEIGHT = 0.5

# An additive material has no PBR equivalent: it is approximated as unlit emission
# whose opacity follows the image's luminance, so black is transparent and bright
# areas add light. This is the emission level that approximation uses.
ADDITIVE_EMISSION = 1.0

MAPPING_NOTES = {
    "reflectance": "specular_weight = reflectance * 2, so Nomad's 0.5 default becomes 1.0",
    "absorption": "transmission_depth = 1 / absorption_factor; Nomad's absorption is a "
                  "density, OpenPBR's is a distance",
    "refraction_interior_roughness": "no OpenPBR input; kept in customData",
    "material_type": "additive is approximated as unlit emission with luminance-driven "
                     "opacity; dithering and shadow_catcher have no equivalent",
    "subsurface_depth": "absent means Nomad's 0.15 default, negative means auto and the "
                        "magnitude is used; OpenPBR's own default radius is 1.0, a metre",
    "translucency": "defaults to true on every material, so it does NOT drive subsurface; "
                    "only material_type == subsurface scatters",
    "subsurface_weight": "scaled by SUBSURFACE_WEIGHT (0.5), matched by eye against "
                         "Nomad's render rather than derived",
    "subsurface_color": "Nomad's is a bleed-through tint, OpenPBR's is the scattering "
                        "albedo: the tint goes to subsurface_radius_scale and the albedo "
                        "follows base_color",
}

# scalar Nomad value -> OpenPBR input. Vertex paint or a texture replaces these
# (PROTOCOL.md section 10), colour is the only channel that multiplies.
SCALARS = (
    ("roughness", "specular_roughness"),
    ("metalness", "base_metalness"),
    ("opacity", "geometry_opacity"),
    ("refraction_ior", "specular_ior"),
)
# nomad paint channel -> (mesh key, primvar, OpenPBR input)
PAINT = {
    "roughness": ("rough", "rough", "specular_roughness"),
    "metalness": ("metallic", "metallic", "base_metalness"),
    "opacity": ("alpha", "displayOpacity", "geometry_opacity"),
}
TEXTURE_INPUTS = {
    "roughness": "specular_roughness",
    "metalness": "base_metalness",
    "opacity": "geometry_opacity",
    "emissive": "emission_color",
}


def author(stage, path, block, textures, mesh=None):
    """Author an OpenPBR material at `path`. Returns the UsdShade.Material."""
    material = UsdShade.Material.Define(stage, path)
    shader = UsdShade.Shader.Define(stage, path + "/OpenPBR")
    shader.CreateIdAttr(SURFACE)
    # Karma reads the mtlx render context
    material.CreateSurfaceOutput("mtlx").ConnectToSource(shader.ConnectableAPI(), "out")

    channels = block.get("textures") or {}
    available = {name: textures[channel["texture_id"]]
                 for name, channel in channels.items()
                 if textures.get(channel.get("texture_id"))}

    base = _base_color(stage, path, shader, block, mesh, available)
    _scalars(stage, path, shader, block, mesh, available)
    _transmission(shader, block)
    _subsurface(shader, block, base)
    if block.get("material_type") == "additive":
        _additive(stage, path, shader, block, base)
    _emission(shader, block)

    kept = {key: value for key, value in block.items()
            if key in ("material_type", "refraction_interior_roughness", "shadow_color",
                       "always_unlit", "flip_culling", "translucency", "translucency_factor")}
    if kept:
        material.GetPrim().SetCustomDataByKey("nomad:material", kept)
    return material


def _shader(stage, path, name, node_id):
    node = UsdShade.Shader.Define(stage, "%s/%s" % (path, name))
    node.CreateIdAttr(node_id)
    return node


def _uv_reader(stage, path, cache):
    """One st reader shared by every texture on this material."""
    if "uv" not in cache:
        node = _shader(stage, path, "st", GEOMPROP_UV)
        node.CreateInput("geomprop", Sdf.ValueTypeNames.String).Set("st")
        node.CreateOutput("out", Sdf.ValueTypeNames.Float2)
        cache["uv"] = node
    return cache["uv"]


ADDRESS = {"repeat": "periodic", "clamp": "clamp", "mirror": "mirror"}


def _uv_transform(stage, path, name, channel, source):
    """Nomad's per-channel uv offset/scale/rotation, in USD's v-up space.

    Nomad applies T + Rz(-r).S.uv in its own v-down space. Flipping v (which we
    do when authoring st) turns that into T' + Rz(r).S'.uv with T' = (Tx, 1-Ty)
    and S' = (Sx, -Sy) -- the same algebra the Blender client uses.
    """
    offset = [float(v) for v in (channel.get("offset") or (0.0, 0.0))[:2]]
    scale = [float(v) for v in (channel.get("scale") or (1.0, 1.0))[:2]]
    rotation = float(channel.get("rotation", 0.0))
    if offset == [0.0, 0.0] and scale == [1.0, 1.0] and rotation == 0.0:
        return source

    # the v flip makes the scale (Sx, -Sy), so it is always authored
    node = _shader(stage, path, name + "_uv_scale", MULTIPLY_UV)
    node.CreateInput("in1", Sdf.ValueTypeNames.Float2).ConnectToSource(
        source.ConnectableAPI(), "out")
    node.CreateInput("in2", Sdf.ValueTypeNames.Float2).Set(Gf.Vec2f(scale[0], -scale[1]))
    node.CreateOutput("out", Sdf.ValueTypeNames.Float2)
    result = node
    if rotation:
        node = _shader(stage, path, name + "_uv_rotate", ROTATE_UV)
        node.CreateInput("in", Sdf.ValueTypeNames.Float2).ConnectToSource(
            result.ConnectableAPI(), "out")
        node.CreateInput("amount", Sdf.ValueTypeNames.Float).Set(math.degrees(rotation))
        node.CreateOutput("out", Sdf.ValueTypeNames.Float2)
        result = node
    node = _shader(stage, path, name + "_uv_offset", ADD_UV)
    node.CreateInput("in1", Sdf.ValueTypeNames.Float2).ConnectToSource(
        result.ConnectableAPI(), "out")
    node.CreateInput("in2", Sdf.ValueTypeNames.Float2).Set(
        Gf.Vec2f(offset[0], 1.0 - offset[1]))
    node.CreateOutput("out", Sdf.ValueTypeNames.Float2)
    return node


def _texture(stage, path, name, blob, colour, cache, channel=None):
    node = _shader(stage, path, name + "_texture", IMAGE_COLOR if colour else IMAGE_FLOAT)
    node.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(blob["path"])
    source = _uv_reader(stage, path, cache)
    if channel:
        source = _uv_transform(stage, path, name, channel, source)
        node.CreateInput("uaddressmode", Sdf.ValueTypeNames.String).Set(
            ADDRESS.get(channel.get("wrap_s"), "periodic"))
        node.CreateInput("vaddressmode", Sdf.ValueTypeNames.String).Set(
            ADDRESS.get(channel.get("wrap_t"), "periodic"))
    node.CreateInput("texcoord", Sdf.ValueTypeNames.Float2).ConnectToSource(
        source.ConnectableAPI(), "out")
    node.CreateOutput("out", Sdf.ValueTypeNames.Color3f if colour
                      else Sdf.ValueTypeNames.Float)
    return node


def _base_color(stage, path, shader, block, mesh, available):
    """colour = material tint x vertex paint x texture, the way Nomad composites."""
    cache = {}
    sources = []
    tint = block.get("color")
    painted = mesh is not None and "color" in mesh
    textured = "color" in available

    if tint is not None and (list(tint[:3]) != [1.0, 1.0, 1.0] or not (painted or textured)):
        constant = _shader(stage, path, "base_tint", "ND_constant_color3")
        constant.CreateInput("value", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*tint[:3]))
        constant.CreateOutput("out", Sdf.ValueTypeNames.Color3f)
        sources.append(constant)
    if painted:
        paint = _shader(stage, path, "paint", GEOMPROP_COLOR)
        paint.CreateInput("geomprop", Sdf.ValueTypeNames.String).Set("displayColor")
        paint.CreateOutput("out", Sdf.ValueTypeNames.Color3f)
        sources.append(paint)
    if textured:
        sources.append(_texture(stage, path, "color", available["color"], True, cache,
                                (block.get("textures") or {}).get("color")))

    if not sources:
        return None
    result = sources[0]
    for index, node in enumerate(sources[1:]):
        combine = _shader(stage, path, "base_mix%d" % index, MULTIPLY_COLOR)
        combine.CreateInput("in1", Sdf.ValueTypeNames.Color3f).ConnectToSource(
            result.ConnectableAPI(), "out")
        combine.CreateInput("in2", Sdf.ValueTypeNames.Color3f).ConnectToSource(
            node.ConnectableAPI(), "out")
        combine.CreateOutput("out", Sdf.ValueTypeNames.Color3f)
        result = combine
    shader.CreateInput("base_color", Sdf.ValueTypeNames.Color3f).ConnectToSource(
        result.ConnectableAPI(), "out")
    return result


def _scalars(stage, path, shader, block, mesh, available):
    """Texture beats paint beats the material value, per the protocol."""
    cache = {}
    for nomad_key, target in SCALARS:
        paint = PAINT.get(nomad_key)
        if nomad_key in available:
            node = _texture(stage, path, nomad_key, available[nomad_key], False, cache,
                            (block.get("textures") or {}).get(nomad_key))
            shader.CreateInput(target, Sdf.ValueTypeNames.Float).ConnectToSource(
                node.ConnectableAPI(), "out")
        elif paint and mesh is not None and paint[0] in mesh:
            node = _shader(stage, path, nomad_key + "_paint", GEOMPROP_FLOAT)
            node.CreateInput("geomprop", Sdf.ValueTypeNames.String).Set(paint[1])
            node.CreateOutput("out", Sdf.ValueTypeNames.Float)
            shader.CreateInput(target, Sdf.ValueTypeNames.Float).ConnectToSource(
                node.ConnectableAPI(), "out")
        elif nomad_key in block:
            shader.CreateInput(target, Sdf.ValueTypeNames.Float).Set(float(block[nomad_key]))

    if "reflectance" in block:
        # Nomad's 0.5 is the 4% F0 default, which is specular_weight 1.0
        weight = max(0.0, min(2.0, float(block["reflectance"]) * 2.0))
        shader.CreateInput("specular_weight", Sdf.ValueTypeNames.Float).Set(weight)


def _transmission(shader, block):
    if block.get("material_type") != "refraction":
        return
    shader.CreateInput("transmission_weight", Sdf.ValueTypeNames.Float).Set(1.0)
    if "refraction_surface_roughness" in block:
        shader.CreateInput("specular_roughness", Sdf.ValueTypeNames.Float).Set(
            float(block["refraction_surface_roughness"]))
    if block.get("absorption_enable"):
        colour = block.get("absorption_color", [1.0, 1.0, 1.0])
        shader.CreateInput("transmission_color", Sdf.ValueTypeNames.Color3f).Set(
            Gf.Vec3f(*colour[:3]))
        factor = float(block.get("absorption_factor", 1.0))
        # Nomad gives a density, OpenPBR wants the distance light travels
        shader.CreateInput("transmission_depth", Sdf.ValueTypeNames.Float).Set(
            1.0 / factor if factor > 1e-6 else 0.0)


def _subsurface(shader, block, base=None):
    """Only material_type "subsurface" scatters.

    `translucency` defaults to true on every Nomad material, so treating it as
    subsurface turns scattering on for the whole scene.

    Nomad's subsurface_color is the tint of the light that bleeds through, but
    OpenPBR's subsurface_color is the scattering *albedo* -- setting one from
    the other washes the whole surface with that colour (a red head, for skin).
    The tint belongs on subsurface_radius_scale, which is per-channel scatter
    distance and whose own default (1, 0.5, 0.25) is skin-shaped. The scattering
    albedo then follows the surface: base colour and vertex paint.
    """
    if block.get("material_type") != "subsurface":
        return
    weight = max(0.0, min(1.0, float(block.get("translucency_factor", 1.0))))
    shader.CreateInput("subsurface_weight", Sdf.ValueTypeNames.Float).Set(
        weight * SUBSURFACE_WEIGHT)

    colour = block.get("subsurface_color")
    if colour is not None:
        values = [max(0.0, float(channel)) for channel in colour[:3]]
        peak = max(values) or 1.0
        shader.CreateInput("subsurface_radius_scale", Sdf.ValueTypeNames.Color3f).Set(
            Gf.Vec3f(*[value / peak for value in values]))
    if base is not None:
        shader.CreateInput("subsurface_color", Sdf.ValueTypeNames.Color3f).ConnectToSource(
            base.ConnectableAPI(), "out")
    # Only edited fields travel, so an absent depth means Nomad's own default
    # (0.15). Negative means auto, where the magnitude is the best guess we have.
    # Anything is better than OpenPBR's default radius of 1.0, which is a metre.
    depth = abs(float(block.get("subsurface_depth", 0.15))) or 0.15
    shader.CreateInput("subsurface_radius", Sdf.ValueTypeNames.Float).Set(depth)


def _additive(stage, path, shader, block, base):
    """Approximate Nomad's additive blending: unlit, black transparent, bright adds.

    There is no additive mode in a PBR surface, so: no diffuse and no specular
    response, the image drives emission, and opacity follows the image's
    luminance so dark areas let the background through unchanged.
    """
    shader.CreateInput("base_weight", Sdf.ValueTypeNames.Float).Set(0.0)
    shader.CreateInput("specular_weight", Sdf.ValueTypeNames.Float).Set(0.0)
    shader.CreateInput("emission_luminance", Sdf.ValueTypeNames.Float).Set(ADDITIVE_EMISSION)

    # the material's own opacity scales the whole effect, as it does in Nomad
    opacity = float(block.get("opacity", 1.0))

    if base is None:  # a flat colour with no texture or paint behind it
        colour = block.get("color") or [1.0, 1.0, 1.0]
        shader.CreateInput("emission_color", Sdf.ValueTypeNames.Color3f).Set(
            Gf.Vec3f(*colour[:3]))
        luminance = 0.2126 * colour[0] + 0.7152 * colour[1] + 0.0722 * colour[2]
        shader.CreateInput("geometry_opacity", Sdf.ValueTypeNames.Float).Set(
            float(luminance) * opacity)
        return

    shader.CreateInput("emission_color", Sdf.ValueTypeNames.Color3f).ConnectToSource(
        base.ConnectableAPI(), "out")
    # geometry_opacity is a float, so the colour has to be reduced before it can drive it
    luminance = _shader(stage, path, "additive_luminance", LUMINANCE)
    luminance.CreateInput("in", Sdf.ValueTypeNames.Color3f).ConnectToSource(
        base.ConnectableAPI(), "out")
    luminance.CreateOutput("out", Sdf.ValueTypeNames.Color3f)
    channel = _shader(stage, path, "additive_opacity", EXTRACT)
    channel.CreateInput("in", Sdf.ValueTypeNames.Color3f).ConnectToSource(
        luminance.ConnectableAPI(), "out")
    channel.CreateInput("index", Sdf.ValueTypeNames.Int).Set(0)
    channel.CreateOutput("out", Sdf.ValueTypeNames.Float)
    source = channel
    if opacity != 1.0:
        scaled = _shader(stage, path, "additive_opacity_scale", MULTIPLY_FLOAT)
        scaled.CreateInput("in1", Sdf.ValueTypeNames.Float).ConnectToSource(
            channel.ConnectableAPI(), "out")
        scaled.CreateInput("in2", Sdf.ValueTypeNames.Float).Set(opacity)
        scaled.CreateOutput("out", Sdf.ValueTypeNames.Float)
        source = scaled
    shader.CreateInput("geometry_opacity", Sdf.ValueTypeNames.Float).ConnectToSource(
        source.ConnectableAPI(), "out")


def _emission(shader, block):
    channel = (block.get("textures") or {}).get("emissive") or {}
    strength = float(channel.get("strength", 0.0))
    if strength > 0.0:
        shader.CreateInput("emission_luminance", Sdf.ValueTypeNames.Float).Set(strength)
        factor = channel.get("factor")
        if isinstance(factor, (list, tuple)):
            shader.CreateInput("emission_color", Sdf.ValueTypeNames.Color3f).Set(
                Gf.Vec3f(*[float(f) for f in factor[:3]]))
