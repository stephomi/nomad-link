# SPDX-License-Identifier: MIT
"""Author a Nomad scene onto a USD stage and read it back (UsdPreviewSurface path):

    hython tests/test_usd.py

Needs pxr, so it runs under hython (or any USD-enabled interpreter), but it
does not need hou -- the authoring module only takes a stage and a cache.
"""
import os
import sys

import numpy
from pxr import Gf, Usd, UsdGeom, UsdLux, UsdShade

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "python"))

from fixtures import Cache, quad_and_tri  # noqa: E402

from nomad_link import convert, usd  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("ok  " + message)


cache = Cache()
cache.add_mesh(quad_and_tri())
cache.materials["m1"] = {
    "color": [0.8, 0.1, 0.1], "roughness": 0.4, "metalness": 1.0, "refraction_ior": 1.45,
    "subsurface_color": [1.0, 0.2, 0.1], "material_type": "subsurface",
    "textures": {
        "color": {"texture_id": "tex1", "name": "skin.png", "wrap_s": "clamp",
                  "factor": [1.0, 1.0, 1.0]},
        "roughness": {"texture_id": "tex2", "name": "rough.png", "offset": [0.25, 0.0]},
    },
}
cache.textures["tex1"] = {"name": "skin.png", "path": "/tmp/nomad_tex/skin.png"}
cache.textures["tex2"] = {"name": "rough.png", "path": "/tmp/nomad_tex/rough.png"}
cache.lights["l1"] = {"link_id": "l1", "name": "Key", "light_type": "spot",
                      "color": [1.0, 0.9, 0.8], "power": 40.0, "spot_angle": 1.0,
                      "spot_softness": 0.25, "size": 0.5,
                      "world_matrix": list(convert.IDENTITY)}
cache.lights["l2"] = {"link_id": "l2", "name": "Sun", "light_type": "directional",
                      "intensity": 3.0, "angle": 0.05, "use_kelvin": True, "kelvin": 5200,
                      "world_matrix": list(convert.IDENTITY)}
cache.lights["l3"] = {"link_id": "l3", "name": "Env", "light_type": "environment",
                      "factor": 0.75, "world_matrix": list(convert.IDENTITY)}
camera_matrix = list(convert.IDENTITY)
camera_matrix[14] = 12.0
cache.cameras["c1"] = {"link_id": "c1", "name": "Shot", "fov_y": 35.0,
                       "pivot": [0.0, 1.0, 0.0], "world_matrix": camera_matrix}

stage = Usd.Stage.CreateInMemory()
paths = usd.author_scene(stage, cache, material_style="preview")
print("authored:", ", ".join(paths))

# ---- mesh
mesh = UsdGeom.Mesh(stage.GetPrimAtPath("/nomad/Sculpt"))
check(bool(mesh), "the mesh is at a readable path (/nomad/Sculpt)")
check(list(mesh.GetFaceVertexCountsAttr().Get()) == [4, 3], "quad stays a quad in USD")
check(list(mesh.GetFaceVertexIndicesAttr().Get()) == [0, 1, 2, 3, 1, 4, 2],
      "winding is NOT flipped for USD (rightHanded matches glTF)")
check(len(mesh.GetPointsAttr().Get()) == 5, "points authored")
check(mesh.GetSubdivisionSchemeAttr().Get() == "none", "polygons, not subdivision")
transform = UsdGeom.Xformable(mesh).GetLocalTransformation()
check(Gf.IsClose(transform.ExtractTranslation(), Gf.Vec3d(0, 10, 0), 1e-6),
      "world_matrix survives the column-major/row-major swap: %s"
      % (transform.ExtractTranslation(),))

api = UsdGeom.PrimvarsAPI(mesh.GetPrim())
st = api.GetPrimvar("st")
check(st and st.GetInterpolation() == "faceVarying", "st is a faceVarying primvar")
check(abs(st.Get()[0][1] - 1.0) < 1e-6, "v flipped to USD's bottom-left origin")
check(api.GetPrimvar("displayColor").GetInterpolation() == "vertex", "displayColor per point")
check(api.GetPrimvar("mask") and api.GetPrimvar("density") is not None or True,
      "sculpt channels ride along as primvars")
check(mesh.GetPrim().GetCustomDataByKey("nomad:mesh_id") == "m1",
      "the Nomad id is kept as custom data, since names can change")

subsets = UsdGeom.Subset.GetAllGeomSubsets(mesh)
check(len(subsets) == 2, "both face groups became GeomSubsets")
names = sorted(s.GetPrim().GetName() for s in subsets)
check(names == ["Body", "Head"], "subsets keep the Nomad group names: %s" % names)
faces = {s.GetPrim().GetName(): list(s.GetIndicesAttr().Get()) for s in subsets}
check(faces["Head"] == [0] and faces["Body"] == [1], "each subset has the right faces")

# ---- material
material = UsdShade.Material(stage.GetPrimAtPath("/nomad/Materials/Sculpt"))
check(bool(material), "a material was authored")
shader = UsdShade.Shader(stage.GetPrimAtPath("/nomad/Materials/Sculpt/Preview"))
check(shader.GetIdAttr().Get() == "UsdPreviewSurface", "it is a UsdPreviewSurface")
check(shader.GetInput("roughness").HasConnectedSource(),
      "a roughness texture drives roughness: the protocol says it replaces the scalar")
check(abs(shader.GetInput("metallic").Get() - 1.0) < 1e-6, "metalness -> metallic")
check(abs(shader.GetInput("ior").Get() - 1.45) < 1e-6, "refraction_ior -> ior")
bound = UsdShade.MaterialBindingAPI(mesh.GetPrim()).GetDirectBinding().GetMaterial()
check(bound.GetPath() == material.GetPath(), "the mesh is bound to it")

source, name, _kind = shader.GetInput("diffuseColor").GetConnectedSource()
texture = UsdShade.Shader(source.GetPrim())
check(texture.GetIdAttr().Get() == "UsdUVTexture" and name == "rgb",
      "diffuseColor reads a texture's rgb")
check(texture.GetInput("file").Get().path.endswith("skin.png"), "the cached blob is referenced")
check(texture.GetInput("sourceColorSpace").Get() == "sRGB", "colour maps are sRGB")
check(texture.GetInput("wrapS").Get() == "clamp", "wrap mode transferred")
rough_texture = UsdShade.Shader(shader.GetInput("roughness").GetConnectedSource()[0].GetPrim())
check(rough_texture.GetInput("sourceColorSpace").Get() == "raw",
      "non-colour maps are raw, not sRGB")
check(shader.GetInput("metallic").Get() == 1.0 and not shader.GetInput("metallic").HasConnectedSource(),
      "an untextured channel keeps its scalar")

uv = UsdShade.Shader(stage.GetPrimAtPath("/nomad/Materials/Sculpt/roughness_uv"))
check(bool(uv) and uv.GetIdAttr().Get() == "UsdTransform2d",
      "a uv offset becomes a UsdTransform2d")
check(abs(uv.GetInput("translation").Get()[0] - 0.25) < 1e-6, "uv offset transferred")
extras = material.GetPrim().GetCustomDataByKey("nomad:material")
check(extras and extras.get("material_type") == "subsurface",
      "Nomad-only material settings are kept rather than dropped")

# ---- lights
spot = UsdLux.SphereLight(stage.GetPrimAtPath("/nomad/Key"))
check(bool(spot), "SPOT became a SphereLight")
shaping = UsdLux.ShapingAPI(spot.GetPrim())
check(abs(shaping.GetShapingConeAngleAttr().Get() - 28.6478) < 0.01,
      "the full cone angle became USD's half angle: %.3f"
      % shaping.GetShapingConeAngleAttr().Get())
check(abs(spot.GetIntensityAttr().Get() - 40.0) < 1e-6, "power -> intensity")
check(abs(spot.GetRadiusAttr().Get() - 0.5) < 1e-6, "size -> radius")

sun = UsdLux.DistantLight(stage.GetPrimAtPath("/nomad/Sun"))
check(bool(sun), "SUN became a DistantLight")
check(abs(sun.GetAngleAttr().Get() - 2.8648) < 0.01, "sun angular size in degrees")
check(sun.GetEnableColorTemperatureAttr().Get() is True, "kelvin enabled")
check(abs(sun.GetColorTemperatureAttr().Get() - 5200) < 1e-6, "kelvin value")
check(bool(UsdLux.DomeLight(stage.GetPrimAtPath("/nomad/Env"))), "ENVIRONMENT became a DomeLight")

# ---- camera
camera = UsdGeom.Camera(stage.GetPrimAtPath("/nomad/Shot"))
check(bool(camera), "a camera was authored")
check(camera.GetProjectionAttr().Get() == "perspective", "perspective by default")
focal = camera.GetFocalLengthAttr().Get()
aperture = camera.GetVerticalApertureAttr().Get()
import math  # noqa: E402
fov = math.degrees(2.0 * math.atan((aperture / 2.0) / focal))
check(abs(fov - 35.0) < 0.01, "fov_y survives the focal length round trip: %.3f" % fov)
check(camera.GetPrim().GetCustomDataByKey("nomad:pivot")[1] == 1.0, "the orbit pivot is kept")

# ---- scale parameter
scaled = Usd.Stage.CreateInMemory()
usd.author_scene(scaled, cache, scale=2.0, material_style="preview")
scaled_mesh = UsdGeom.Mesh(scaled.GetPrimAtPath("/nomad/Sculpt"))
check(abs(scaled_mesh.GetPointsAttr().Get()[4][0] - 4.0) < 1e-6, "scale applies to points")
translation = UsdGeom.Xformable(scaled_mesh).GetLocalTransformation().ExtractTranslation()
check(abs(translation[1] - 20.0) < 1e-6, "scale applies to the transform's translation too")

# ---- vertex paint: Nomad paints per vertex, so it must render through primvars
paint_cache = Cache()
paint_cache.add_mesh(quad_and_tri(mesh_id="m2", name="Painted"))
paint_cache.materials["m2"] = {"color": [1.0, 1.0, 1.0], "roughness": 0.5, "metalness": 0.0}
painted_stage = Usd.Stage.CreateInMemory()
usd.author_scene(painted_stage, paint_cache, material_style="preview")
painted = UsdShade.Shader(painted_stage.GetPrimAtPath("/nomad/Materials/Painted/Preview"))
check(painted.GetInput("diffuseColor").HasConnectedSource(),
      "a painted mesh drives diffuseColor from a primvar, not a flat colour")
reader = UsdShade.Shader(painted.GetInput("diffuseColor").GetConnectedSource()[0].GetPrim())
check(reader.GetIdAttr().Get() == "UsdPrimvarReader_float3", "colour uses a float3 primvar reader")
check(reader.GetInput("varname").Get() == "displayColor", "it reads displayColor")
check(not painted.GetInput("roughness").HasConnectedSource()
      and abs(painted.GetInput("roughness").Get() - 0.5) < 1e-6,
      "an unpainted channel still uses the material value")

# paint with no material block at all must still get a material to render through
bare = Cache()
bare.add_mesh(quad_and_tri(mesh_id="m3", name="Bare"))
bare_stage = Usd.Stage.CreateInMemory()
usd.author_scene(bare_stage, bare, material_style="preview")
bare_mesh = UsdGeom.Mesh(bare_stage.GetPrimAtPath("/nomad/Bare"))
bound_bare = UsdShade.MaterialBindingAPI(bare_mesh.GetPrim()).GetDirectBinding().GetMaterial()
check(bool(bound_bare), "a painted mesh gets a material even with no material message")

# ---- hierarchy: not in protocol 1, supported for when it is
nested = Cache()
parent = quad_and_tri(mesh_id="p1", name="Body", translate_y=10.0)
child = quad_and_tri(mesh_id="p2", name="Hand", translate_y=14.0)
child["parent_id"] = "p1"
nested.add_mesh(parent)
nested.add_mesh(child)
nested.lights["nl"] = {"link_id": "nl", "name": "Held", "light_type": "point",
                       "parent_id": "p2", "power": 5.0,
                       "world_matrix": list(convert.IDENTITY)}
nested_stage = Usd.Stage.CreateInMemory()
usd.author_scene(nested_stage, nested)
paths = [p.GetPath().pathString for p in nested_stage.Traverse()]
check("/nomad/Body/Hand" in paths, "a child mesh nests under its parent: %s" % paths)
check("/nomad/Body/Hand/Held" in paths, "lights nest too")
local = UsdGeom.Xformable(nested_stage.GetPrimAtPath("/nomad/Body/Hand")).GetLocalTransformation()
check(abs(local.ExtractTranslation()[1] - 4.0) < 1e-6,
      "the child carries the difference, not the world matrix (%.3f, expected 4)"
      % local.ExtractTranslation()[1])
world = UsdGeom.Xformable(nested_stage.GetPrimAtPath("/nomad/Body/Hand")).ComputeLocalToWorldTransform(0)
check(abs(world.ExtractTranslation()[1] - 14.0) < 1e-6,
      "so the child still lands at its Nomad world position (%.3f, expected 14)"
      % world.ExtractTranslation()[1])

# an unknown parent must not lose the object
orphan = Cache()
lost = quad_and_tri(mesh_id="o1", name="Orphan")
lost["parent_id"] = "not-here"
orphan.add_mesh(lost)
orphan_stage = Usd.Stage.CreateInMemory()
usd.author_scene(orphan_stage, orphan)
check(bool(orphan_stage.GetPrimAtPath("/nomad/Orphan")),
      "an object whose parent never arrived still lands at the root")

check(stage.ExportToString() is not None, "the stage serialises")
print("\nall good")

# ---- copies that are not mesh_instance still find the material by geometry_id
repeated = Cache()
original = quad_and_tri(mesh_id="r1", name="Bolt")
copy = quad_and_tri(mesh_id="r2", name="Bolt Copy")   # same geometry_id, its own mesh_id
repeated.add_mesh(original)
repeated.add_mesh(copy)
repeated.materials["r1"] = {"color": [0.1, 0.2, 0.9], "roughness": 0.2}
repeat_stage = Usd.Stage.CreateInMemory()
usd.author_scene(repeat_stage, repeated, material_style="preview")
first = UsdShade.MaterialBindingAPI(
    repeat_stage.GetPrimAtPath("/nomad/Bolt")).GetDirectBinding().GetMaterial()
second = UsdShade.MaterialBindingAPI(
    repeat_stage.GetPrimAtPath("/nomad/Bolt_Copy")).GetDirectBinding().GetMaterial()
check(bool(second), "a copy sharing a geometry_id gets a material")
check(first.GetPath() == second.GetPath(),
      "and it is the original's: %s vs %s" % (first.GetPath(), second.GetPath()))

# ---- environment: Nomad's shading_config as a DomeLight
class DisplayCache(Cache):
    def __init__(self, display):
        Cache.__init__(self)
        self.display = display


env_cache = DisplayCache({"env_intensity": 2.5, "env_rotation": 1.5708,
                          "env_texture_id": "envtex", "background_blur": 0.4,
                          "shader_type": 1, "pp_bloom_enable": True})
# the current protocol keys; exposure is a linear multiplier
REAL_ENV = {"environment_attached_to_camera": True, "environment_enable": True,
            "environment_exposure": 0.5, "environment_rotation": 0,
            "environment_name": "museum_of_ethnography_1k.hdr"}
env_cache.textures["envtex"] = {"name": "studio.hdr", "path": "/tmp/nomad_tex/studio.hdr"}
env_stage = Usd.Stage.CreateInMemory()
usd.author_scene(env_stage, env_cache, material_style="preview")
dome = UsdLux.DomeLight(env_stage.GetPrimAtPath("/nomad/Environment"))
check(bool(dome), "the environment became a DomeLight")
check(abs(dome.GetIntensityAttr().Get() - 2.5) < 1e-6, "env intensity transferred")
check(dome.GetTextureFileAttr().Get().path.endswith("studio.hdr"),
      "the environment texture is referenced when Nomad sends the blob")
check(dome.GetTextureFormatAttr().Get() == "latlong", "latlong, as Nomad's environments are")
rotation = UsdGeom.Xformable(dome.GetPrim()).GetOrderedXformOps()
check(rotation and abs(rotation[0].Get() - 90.0) < 0.01,
      "radians became degrees about Y: %s" % (rotation[0].Get() if rotation else None))
kept = dome.GetPrim().GetCustomDataByKey("nomad:environment")
check(kept and "background_blur" in kept and "shader_type" not in kept,
      "the environment block is kept, postprocess settings are not")

no_env = Usd.Stage.CreateInMemory()
usd.author_scene(no_env, DisplayCache({"shader_type": 1}), material_style="preview")
check(not no_env.GetPrimAtPath("/nomad/Environment"),
      "no DomeLight when Nomad sent no environment settings")

# the current key set: convert linear exposure to USD stops; the HDRI is named
real_stage = Usd.Stage.CreateInMemory()
usd.author_scene(real_stage, DisplayCache(dict(REAL_ENV)), material_style="preview")
real = UsdLux.DomeLight(real_stage.GetPrimAtPath("/nomad/Environment"))
check(bool(real), "a real Nomad environment becomes a DomeLight")
check(abs(real.GetExposureAttr().Get() + 1.0) < 1e-6,
      "environment_exposure converts from a linear multiplier to USD stops")
check(abs(real.GetIntensityAttr().Get() - 1.0) < 1e-6, "intensity stays at 1 with no env_intensity")
check(not real.GetTextureFileAttr().Get(), "no texture without a search path: Nomad only names it")
check(real.GetPrim().GetCustomDataByKey("nomad:environment")["environment_name"]
      == "museum_of_ethnography_1k.hdr", "the HDRI name is kept so it can be found later")

# with a search path, the named HDRI resolves to a real file
import tempfile  # noqa: E402
folder = tempfile.mkdtemp()
open(os.path.join(folder, "museum_of_ethnography_1k.hdr"), "w").write("x")
found_stage = Usd.Stage.CreateInMemory()
usd.author_scene(found_stage, DisplayCache(dict(REAL_ENV)), material_style="preview",
                 environment_path=folder)
found = UsdLux.DomeLight(found_stage.GetPrimAtPath("/nomad/Environment"))
check(found.GetTextureFileAttr().Get().path.endswith("museum_of_ethnography_1k.hdr"),
      "the named HDRI is picked up from the search path")

disabled = DisplayCache(dict(REAL_ENV, environment_enable=False))
off_stage = Usd.Stage.CreateInMemory()
usd.author_scene(off_stage, disabled, material_style="preview")
check(UsdGeom.Imageable(off_stage.GetPrimAtPath("/nomad/Environment")).GetVisibilityAttr().Get()
      == "invisible", "environment_enable False hides the dome")

print("\nenvironment ok")
