# SPDX-License-Identifier: MIT
"""Nomad materials as MaterialX OpenPBR: hython tests/test_openpbr.py"""
import os
import sys

import numpy
from pxr import Usd, UsdShade

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "python"))

from nomad_link import openpbr, usd  # noqa: E402
from fixtures import Cache, quad_and_tri  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("ok  " + message)


def nodedefs_exist():
    """Every shader id we author must be a real MaterialX nodedef."""
    import MaterialX
    doc = MaterialX.createDocument()
    MaterialX.loadLibraries(["libraries"], MaterialX.getDefaultDataSearchPath(), doc)
    return doc


doc = nodedefs_exist()
for node_id in (openpbr.SURFACE, openpbr.MULTIPLY_COLOR, openpbr.GEOMPROP_COLOR,
                openpbr.GEOMPROP_FLOAT, openpbr.IMAGE_COLOR, openpbr.IMAGE_FLOAT,
                openpbr.GEOMPROP_UV, "ND_constant_color3"):
    check(doc.getNodeDef(node_id) is not None, "%s is a real nodedef" % node_id)

surface_def = doc.getNodeDef(openpbr.SURFACE)
inputs = {i.getName() for i in surface_def.getInputs()}
for _key, target in openpbr.SCALARS:
    check(target in inputs, "OpenPBR has an input called %s" % target)
for target in ("subsurface_weight", "subsurface_color", "subsurface_radius",
               "transmission_weight", "transmission_color", "transmission_depth",
               "specular_weight", "emission_luminance", "base_color"):
    check(target in inputs, "OpenPBR has an input called %s" % target)

# a painted, tinted, subsurface material: the case UsdPreviewSurface cannot express
cache = Cache()
mesh = quad_and_tri(mesh_id="m1", name="Skin")
cache.add_mesh(mesh)
cache.materials["m1"] = {
    "color": [0.9, 0.6, 0.5], "roughness": 0.42, "metalness": 0.0,
    "material_type": "subsurface", "subsurface_color": [1.0, 0.2, 0.1],
    "subsurface_depth": 0.15, "translucency": True, "translucency_factor": 0.8,
    "reflectance": 0.5, "refraction_ior": 1.4,
}
stage = Usd.Stage.CreateInMemory()
usd.author_scene(stage, cache, material_style="openpbr")
material = UsdShade.Material(stage.GetPrimAtPath("/nomad/Materials/Skin"))
shader = UsdShade.Shader(stage.GetPrimAtPath("/nomad/Materials/Skin/OpenPBR"))
check(bool(material) and bool(shader), "an OpenPBR material was authored")
check(shader.GetIdAttr().Get() == openpbr.SURFACE, "the shader is the OpenPBR surface")
check(bool(material.GetSurfaceOutput("mtlx").GetConnectedSource()),
      "it is connected on the mtlx render context, which is what Karma reads")

check(abs(shader.GetInput("subsurface_weight").Get() - 0.8 * openpbr.SUBSURFACE_WEIGHT) < 1e-6,
      "translucency_factor scales the calibrated subsurface weight")
check(abs(shader.GetInput("subsurface_radius").Get() - 0.15) < 1e-6,
      "subsurface_depth became subsurface_radius")
check(abs(shader.GetInput("specular_ior").Get() - 1.4) < 1e-6, "ior transferred")
check(abs(shader.GetInput("specular_weight").Get() - 1.0) < 1e-6,
      "reflectance 0.5 became specular_weight 1.0")
check(abs(shader.GetInput("specular_roughness").Get() - 0.42) < 1e-6, "roughness transferred")

# the compositing UsdPreviewSurface could not do: tint x vertex paint
source, _name, _kind = shader.GetInput("base_color").GetConnectedSource()
mix = UsdShade.Shader(source.GetPrim())
check(mix.GetIdAttr().Get() == openpbr.MULTIPLY_COLOR,
      "base_color is a multiply, not one source overriding the other")
feeds = [UsdShade.Shader(mix.GetInput(name).GetConnectedSource()[0].GetPrim()).GetIdAttr().Get()
         for name in ("in1", "in2")]
check("ND_constant_color3" in feeds and openpbr.GEOMPROP_COLOR in feeds,
      "the tint and the displayColor primvar are both multiplied in: %s" % feeds)
paint = UsdShade.Shader(stage.GetPrimAtPath("/nomad/Materials/Skin/paint"))
check(paint.GetInput("geomprop").Get() == "displayColor", "the primvar name is right")

# translucency defaults to true on every Nomad material: it must not scatter
plain = Cache()
plain.add_mesh(quad_and_tri(mesh_id="p1", name="Plain"))
plain.materials["p1"] = {"color": [0.8, 0.8, 0.8], "roughness": 0.5,
                         "translucency": True, "translucency_factor": 1.0,
                         "subsurface_depth": -0.00624, "material_type": "opaque"}
plain_stage = Usd.Stage.CreateInMemory()
usd.author_scene(plain_stage, plain, material_style="openpbr")
ps = UsdShade.Shader(plain_stage.GetPrimAtPath("/nomad/Materials/Plain/OpenPBR"))
check(not ps.GetInput("subsurface_weight"),
      "an opaque material does not scatter just because translucency defaults to true")

# a real subsurface material, with Nomad's negative "auto" depth
skin = Cache()
skin.add_mesh(quad_and_tri(mesh_id="s1", name="Head"))
skin.materials["s1"] = {"material_type": "subsurface", "subsurface_color": [1.0, 0.3, 0.2],
                        "subsurface_depth": 0.00624, "translucency": True,
                        "translucency_factor": 1.0}
skin_stage = Usd.Stage.CreateInMemory()
usd.author_scene(skin_stage, skin, material_style="openpbr")
ss = UsdShade.Shader(skin_stage.GetPrimAtPath("/nomad/Materials/Head/OpenPBR"))
check(abs(ss.GetInput("subsurface_weight").Get() - openpbr.SUBSURFACE_WEIGHT) < 1e-6,
      "a subsurface material scatters, at the calibrated weight (%.2f)" % openpbr.SUBSURFACE_WEIGHT)
check(openpbr.SUBSURFACE_WEIGHT == 0.5, "the calibration constant is the tuned 0.5")
scale = ss.GetInput("subsurface_radius_scale").Get()
check(abs(scale[0] - 1.0) < 1e-6 and abs(scale[1] - 0.3) < 1e-6 and abs(scale[2] - 0.2) < 1e-6,
      "the subsurface tint becomes per-channel scatter distance, not albedo: %s" % (scale,))
check(ss.GetInput("subsurface_color").HasConnectedSource(),
      "the scattering albedo follows base_color rather than the tint")
check(abs(ss.GetInput("subsurface_radius").Get() - 0.00624) < 1e-6,
      "the sculpt's depth reaches subsurface_radius, not OpenPBR's 1 metre: %r"
      % ss.GetInput("subsurface_radius").Get())

# an auto (negative) depth uses the magnitude, and an absent one Nomad's default
for value, expected, label in ((-0.008, 0.008, "auto (negative) depth uses its magnitude"),
                               (None, 0.15, "an absent depth falls back to Nomad's 0.15")):
    block = {"material_type": "subsurface"}
    if value is not None:
        block["subsurface_depth"] = value
    case = Cache()
    case.add_mesh(quad_and_tri(mesh_id="d1", name="Depth"))
    case.materials["d1"] = block
    case_stage = Usd.Stage.CreateInMemory()
    usd.author_scene(case_stage, case, material_style="openpbr")
    radius = UsdShade.Shader(case_stage.GetPrimAtPath(
        "/nomad/Materials/Depth/OpenPBR")).GetInput("subsurface_radius").Get()
    check(abs(radius - expected) < 1e-6, "%s (%r)" % (label, radius))

# refraction with absorption
glass = Cache()
glass.add_mesh(quad_and_tri(mesh_id="g1", name="Glass"))
glass.materials["g1"] = {
    "material_type": "refraction", "refraction_ior": 1.52,
    "refraction_surface_roughness": 0.05, "absorption_enable": True,
    "absorption_color": [0.2, 0.9, 0.6], "absorption_factor": 4.0,
}
glass_stage = Usd.Stage.CreateInMemory()
usd.author_scene(glass_stage, glass, material_style="openpbr")
gs = UsdShade.Shader(glass_stage.GetPrimAtPath("/nomad/Materials/Glass/OpenPBR"))
check(abs(gs.GetInput("transmission_weight").Get() - 1.0) < 1e-6, "refraction transmits")
check(abs(gs.GetInput("transmission_depth").Get() - 0.25) < 1e-6,
      "absorption_factor 4 became transmission_depth 0.25")
check(abs(gs.GetInput("specular_roughness").Get() - 0.05) < 1e-6,
      "surface roughness drives specular_roughness")

# the preview surface path must still work
preview_stage = Usd.Stage.CreateInMemory()
usd.author_scene(preview_stage, cache, material_style="preview")
check(bool(UsdShade.Shader(preview_stage.GetPrimAtPath("/nomad/Materials/Skin/Preview"))),
      "UsdPreviewSurface is still available as a style")

print("\nall good")

# ---- additive: unlit emission, opacity from the image's luminance
additive = Cache()
additive.add_mesh(quad_and_tri(mesh_id="a1", name="Glow"))
additive.materials["a1"] = {"material_type": "additive", "color": [1.0, 1.0, 1.0],
                            "textures": {"color": {"texture_id": "glowtex", "name": "flare.png"}}}
additive.textures["glowtex"] = {"name": "flare.png", "path": "/tmp/nomad_tex/flare.png"}
add_stage = Usd.Stage.CreateInMemory()
usd.author_scene(add_stage, additive, material_style="openpbr")
glow = UsdShade.Shader(add_stage.GetPrimAtPath("/nomad/Materials/Glow/OpenPBR"))
check(glow.GetInput("base_weight").Get() == 0.0, "additive is unlit: no diffuse")
check(glow.GetInput("specular_weight").Get() == 0.0, "and no specular highlight")
check(glow.GetInput("emission_color").HasConnectedSource(), "the image drives emission")
check(abs(glow.GetInput("emission_luminance").Get() - openpbr.ADDITIVE_EMISSION) < 1e-6,
      "emission_luminance is set")
opacity_source = UsdShade.Shader(glow.GetInput("geometry_opacity").GetConnectedSource()[0].GetPrim())
check(opacity_source.GetIdAttr().Get() == openpbr.EXTRACT,
      "opacity comes through a float extract, since geometry_opacity is not a colour")
lum = UsdShade.Shader(opacity_source.GetInput("in").GetConnectedSource()[0].GetPrim())
check(lum.GetIdAttr().Get() == openpbr.LUMINANCE,
      "and the extract reads a luminance node, so black is transparent")

# the material's opacity scales the additive effect
faded = Cache()
faded.add_mesh(quad_and_tri(mesh_id="a3", name="HalfGlow"))
faded.materials["a3"] = {"material_type": "additive", "opacity": 0.5, "color": [1.0, 1.0, 1.0],
                         "textures": {"color": {"texture_id": "glowtex", "name": "flare.png"}}}
faded.textures["glowtex"] = {"name": "flare.png", "path": "/tmp/nomad_tex/flare.png"}
faded_stage = Usd.Stage.CreateInMemory()
usd.author_scene(faded_stage, faded, material_style="openpbr")
half = UsdShade.Shader(faded_stage.GetPrimAtPath("/nomad/Materials/HalfGlow/OpenPBR"))
scale_node = UsdShade.Shader(half.GetInput("geometry_opacity").GetConnectedSource()[0].GetPrim())
check(scale_node.GetIdAttr().Get() == openpbr.MULTIPLY_FLOAT,
      "the material opacity multiplies the luminance-driven opacity")
check(abs(scale_node.GetInput("in2").Get() - 0.5) < 1e-6, "by the value Nomad sent")

flat = Cache()
flat.add_mesh(quad_and_tri(mesh_id="a2", name="FlatGlow"))
flat.materials["a2"] = {"material_type": "additive", "color": [1.0, 1.0, 1.0], "opacity": 0.25}
flat_stage = Usd.Stage.CreateInMemory()
usd.author_scene(flat_stage, flat, material_style="openpbr")
flat_shader = UsdShade.Shader(flat_stage.GetPrimAtPath("/nomad/Materials/FlatGlow/OpenPBR"))
check(abs(flat_shader.GetInput("geometry_opacity").Get() - 0.25) < 1e-6,
      "with no texture, opacity is the flat colour's luminance times the material opacity: %r"
      % flat_shader.GetInput("geometry_opacity").Get())

print("\nadditive ok")

# ---- texture repeats: Nomad's scale/offset/rotation and wrap modes
tiled = Cache()
tiled.add_mesh(quad_and_tri(mesh_id="t1", name="Tiled"))
tiled.materials["t1"] = {"textures": {"color": {
    "texture_id": "tiletex", "name": "tile.png", "scale": [4.0, 2.0],
    "offset": [0.25, 0.0], "rotation": 1.5708, "wrap_s": "clamp", "wrap_t": "mirror"}}}
tiled.textures["tiletex"] = {"name": "tile.png", "path": "/tmp/nomad_tex/tile.png"}
tiled_stage = Usd.Stage.CreateInMemory()
usd.author_scene(tiled_stage, tiled, material_style="openpbr")
image = UsdShade.Shader(tiled_stage.GetPrimAtPath("/nomad/Materials/Tiled/color_texture"))
check(bool(image), "the texture node exists")
check(image.GetInput("uaddressmode").Get() == "clamp"
      and image.GetInput("vaddressmode").Get() == "mirror",
      "wrap modes reach the image node as MaterialX address modes")
placed = UsdShade.Shader(image.GetInput("texcoord").GetConnectedSource()[0].GetPrim())
check(placed.GetIdAttr().Get() == openpbr.ADD_UV, "the uv chain ends with the offset")
offset = placed.GetInput("in2").Get()
check(abs(offset[0] - 0.25) < 1e-6 and abs(offset[1] - 1.0) < 1e-6,
      "the offset is v-flipped: (Tx, 1-Ty) = %s" % (offset,))
rotate = UsdShade.Shader(placed.GetInput("in1").GetConnectedSource()[0].GetPrim())
check(rotate.GetIdAttr().Get() == openpbr.ROTATE_UV
      and abs(rotate.GetInput("amount").Get() - 90.0) < 0.01,
      "rotation in degrees")
scale_node = UsdShade.Shader(rotate.GetInput("in").GetConnectedSource()[0].GetPrim())
repeat = scale_node.GetInput("in2").Get()
check(abs(repeat[0] - 4.0) < 1e-6 and abs(repeat[1] + 2.0) < 1e-6,
      "the repeat reaches the chain, with v negated: %s" % (repeat,))

print("\nuv transform ok")
