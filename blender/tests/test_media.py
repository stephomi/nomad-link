"""Textures, environment, lights, cameras from a synthetic .nom.

Runs inside Blender: blender --background --factory-startup --python tests/test_media.py
"""
import json, math, os, struct, sys, zlib
import numpy
import bpy

ADDON_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
NOMAD_ASSETS = os.path.abspath(os.path.join(ADDON_DIR, *[".."] * 3, "assets"))
sys.path.insert(0, ADDON_DIR)
sys.path.insert(0, os.path.join(ADDON_DIR, "nomad_blender_link"))
import nomad_blender_link as addon
import nom_file
addon.register()

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media.nom")
fails = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'}  {label} {detail}")
    if not condition:
        fails.append(label)


def png(rgba, w, h):
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    raw = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


positions = numpy.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], "<f4")
faces = numpy.array([[0, 1, 2, 3]], "<i4")
uvs = numpy.array([[0, 0], [1, 0], [1, 1], [0, 1]], "<f4")
faces_uv = numpy.array([[0, 1, 2, 3]], "<i4")
tex_png = png(bytes([255, 0, 0, 255, 0, 255, 0, 255, 0, 0, 255, 255, 255, 255, 0, 255]), 2, 2)
env_png = png(bytes([10, 20, 30, 255] * 4), 2, 2)

blobs = [positions.tobytes(), faces.tobytes(), uvs.tobytes(), faces_uv.tobytes(), tex_png, env_png]
offsets, binary, cursor = [], b"", 0
for piece in blobs:
    offsets.append(cursor)
    binary += piece
    cursor += len(piece)

mesh_entry = {"count_vertex": 4, "count_face": 1, "count_uv": 4, "material": 0,
              "vertices": {"count": 4, "type": "f32vec3", "offset": offsets[0], "length": positions.nbytes},
              "faces": {"count": 1, "type": "i32vec4", "offset": offsets[1], "length": faces.nbytes},
              "uvs": {"count": 4, "type": "f32vec2", "offset": offsets[2], "length": uvs.nbytes},
              "faces_uv": {"count": 1, "type": "i32vec4", "offset": offsets[3], "length": faces_uv.nbytes}}
material = {"color": [1, 1, 1], "roughness": 0.5, "metalness": 0.0, "factorColor": [1, 1, 1],
            "textures": {"color": {"index": 0, "name": "tex.png", "projection": "uv",
                                   "wrapS": "clamp", "magFilter": "linear"}}}
light = {"type": "spot", "color": [1.0, 0.5, 0.25], "power": 42.0,
         "spot_angle": 0.8, "spot_softness": 0.25, "shadow_cast": True}
camera = {"orthographic": False, "fovy": 40.0}
lift = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 5, 1]  # nomad translation (0,0,5)

scene = [{"name": "Quad", "mesh": 0},
         {"name": "Spot", "light": 0, "matrix": lift},
         {"name": "Cam", "camera": 0}]
js = {"scene": scene, "meshes": [mesh_entry], "materials": [material],
      "images": [{"type": "image/png", "offset": offsets[4], "length": len(tex_png)}],
      "assets": [{"collection": "environments", "name": "env.png",
                  "offset": offsets[5], "length": len(env_png)}],
      "settings": {"environment_rotation": 90.0, "environment_exposure": 2.0,
                   "environment_enable": True},
      "lights": [light], "cameras": [camera]}

payload = json.dumps(js).encode()
head = struct.pack("<12sI", nom_file.MAGIC, 6)
header = struct.pack("<5Q", 72 + len(payload) + len(binary), 72, len(payload), 72 + len(payload), len(binary))
open(FIX, "wb").write(head + header + struct.pack("<2Q", 0, 0) + payload + binary)

bpy.ops.wm.read_factory_settings(use_empty=True)
result = bpy.ops.nomad.import_nom(filepath=FIX)
check("import finished", result == {"FINISHED"})
bpy.context.view_layer.update()

# --- texture into the material
quad = bpy.data.objects["Quad"]
tree = quad.data.materials[0].node_tree
image_nodes = [n for n in tree.nodes if n.bl_idname == "ShaderNodeTexImage"]
check("one image texture node", len(image_nodes) == 1, [n.bl_idname for n in tree.nodes])
if image_nodes:
    image = image_nodes[0].image
    check("image tagged by content hash", str(image.get("nomad_texture_id", "")).startswith("nom-"))
    check("image is packed", image.packed_file is not None)
    check("clamp wrap became EXTEND", image_nodes[0].extension == "EXTEND", image_nodes[0].extension)
    shader = next(n for n in tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
    feeds = [l for l in tree.links if l.to_node == shader and l.to_socket.name == "Base Color"]
    mix = feeds[0].from_node if feeds else None
    sample = addon.node_socket(mix.inputs, "B_Color").links if mix is not None and mix.type == "MIX" else []
    check("texture feeds Base Color through the intensity mix", bool(sample) and sample[0].from_node == image_nodes[0])

# --- light
spot = bpy.data.objects["Spot"]
check("light object", spot.type == "LIGHT" and spot.data.type == "SPOT")
check("light power", abs(spot.data.energy - 42.0) < 1e-5, spot.data.energy)
check("spot angle", abs(spot.data.spot_size - 0.8) < 1e-5, spot.data.spot_size)
check("light color", all(abs(a - b) < 1e-5 for a, b in zip(spot.data.color, (1.0, 0.5, 0.25))))
# nomad (0,0,5) -> blender (0,-5,0); lights map as world geometry, no mesh conjugation
check("light placement unconjugated",
      all(abs(a - b) < 1e-5 for a, b in zip(spot.matrix_world.translation, (0, -5, 0))),
      list(spot.matrix_world.translation))

# --- camera
cam = bpy.data.objects["Cam"]
check("camera object", cam.type == "CAMERA" and cam.data.type == "PERSP")
want_lens = cam.data.sensor_height / (2.0 * math.tan(math.radians(40.0) * 0.5))
check("camera lens from fovy", abs(cam.data.lens - want_lens) < 1e-3, cam.data.lens)

# --- environment
world = bpy.context.scene.world
env_nodes = [n for n in world.node_tree.nodes if n.bl_idname == "ShaderNodeTexEnvironment"] if world else []
check("world environment texture", bool(env_nodes))
if env_nodes:
    check("environment image tagged", str(env_nodes[0].image.get(addon.ASSET_ID, "")).startswith("nom-"))
    background = next(n for n in world.node_tree.nodes if n.bl_idname == "ShaderNodeBackground")
    check("exposure on strength", abs(background.inputs["Strength"].default_value - 2.0) < 1e-5)

# --- second import reuses the packed images (content dedup)
bpy.ops.nomad.import_nom(filepath=FIX)
tagged = [i for i in bpy.data.images if str(i.get("nomad_texture_id", "")).startswith("nom-")]
check("re-import dedups the texture", len(tagged) == 1, len(tagged))

# --- environment off leaves the world alone
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.nomad.import_nom(filepath=FIX, import_environment=False)
world = bpy.context.scene.world
env_nodes = [n for n in world.node_tree.nodes if n.bl_idname == "ShaderNodeTexEnvironment"] if world and world.node_tree else []
check("environment toggle off", not env_nodes)

os.remove(FIX)
print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
