"""Texture factor and intensity: the node graph follows Nomad's shader and sends them back.

Runs inside Blender: blender --background --factory-startup --python tests/test_intensity.py
"""
import os, struct, sys, zlib
import bpy

ADDON_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ADDON_DIR)
sys.path.insert(0, os.path.join(ADDON_DIR, "nomad_blender_link"))
import nomad_blender_link as addon
addon.register()

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


def source(socket):
    return socket.links[0].from_node if socket.is_linked else None


def sock(node, identifier):
    return addon.node_socket(node.inputs, identifier)


def near(a, b):
    return all(abs(x - y) < 1e-5 for x, y in zip(a, b)) if isinstance(a, (list, tuple)) else abs(a - b) < 1e-5


def store(image, data, name):
    return {"texture_id": image["nomad_texture_id"], "name": name}


bpy.ops.wm.read_factory_settings(use_empty=True)
for texture_id in ("tex-color", "tex-gray"):
    image = addon.image_from_bytes(texture_id + ".png", png(bytes([200, 100, 50, 128] * 4), 2, 2))
    image["nomad_texture_id"] = texture_id

mesh = bpy.data.meshes.new("Quad")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
mesh.uv_layers.new()
obj = bpy.data.objects.new("Quad", mesh)
bpy.context.scene.collection.objects.link(obj)

settings = {
    "color": [0.8, 0.8, 0.8], "roughness": 0.4, "metalness": 0.1, "opacity": 1.0,
    "use_color_opacity_value": True,
    "textures": {
        "color": {"texture_id": "tex-color", "factor": [0.5, 1.0, 1.0], "intensity": 0.3},
        "roughness": {"texture_id": "tex-gray", "factor": 0.5, "intensity": 0.25},
        "metalness": {"texture_id": "tex-gray", "intensity": 1.0},
        "opacity": {"texture_id": "tex-gray", "intensity": 0.6},
    },
}
addon.make_material(mesh, {"material": settings}, True, True, False)
tree = mesh.materials[0].node_tree
shader = next(n for n in tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")

# --- color: vertex paint × mix(1, sample × factor, intensity)
mix = source(shader.inputs["Base Color"])
check("Base Color from a MULTIPLY mix", mix is not None and mix.type == "MIX" and mix.blend_type == "MULTIPLY")
if mix is not None and mix.type == "MIX":
    check("color intensity", near(sock(mix, "Factor_Float").default_value, 0.3))
    paint = source(sock(mix, "A_Color"))
    check("vertex color multiplies", paint is not None and paint.attribute_name == addon.COLOR_ATTRIBUTE)
    factor = source(sock(mix, "B_Color"))
    check("color factor node", factor is not None and near(sock(factor, "B_Color").default_value[:3], (0.5, 1, 1)))
    if factor is not None:
        check("factor scales the image", (source(sock(factor, "A_Color")) or factor).type == "TEX_IMAGE")

# --- roughness: mix(vertex roughness, sample × factor, intensity)
mix = source(shader.inputs["Roughness"])
check("Roughness from a FLOAT mix", mix is not None and mix.type == "MIX" and mix.data_type == "FLOAT")
if mix is not None and mix.type == "MIX":
    check("roughness intensity", near(sock(mix, "Factor_Float").default_value, 0.25))
    paint = source(sock(mix, "A_Float"))
    check("vertex roughness mixes", paint is not None and paint.attribute_name == addon.ROUGHNESS_ATTRIBUTE)
    factor = source(sock(mix, "B_Float"))
    check("roughness factor", factor is not None and factor.type == "MATH" and near(factor.inputs[1].default_value, 0.5))

# --- metalness at full intensity, no vertex paint: the image straight in
check("metalness image straight in", (source(shader.inputs["Metallic"]) or shader).type == "TEX_IMAGE")

# --- alpha: vertex alpha × mix(1, color alpha, 0.3) × mix(1, opacity, 0.6)
mixes = {n.get("nomad_intensity"): n for n in tree.nodes if n.get("nomad_intensity")}
check("color alpha intensity",
      "color_alpha" in mixes and near(sock(mixes["color_alpha"], "Factor_Float").default_value, 0.3))
check("opacity intensity", "opacity" in mixes and near(sock(mixes["opacity"], "Factor_Float").default_value, 0.6))
check("alpha is a product", (source(shader.inputs["Alpha"]) or shader).type == "MATH")

# --- send back what the graph shows
obj.active_material = mesh.materials[0]
textures = addon.material_settings(obj, True, True, False, store=store).get("textures", {})
check("sent color factor", near(textures.get("color", {}).get("factor", [0] * 3), (0.5, 1, 1)))
check("sent color intensity", near(textures.get("color", {}).get("intensity", 0), 0.3))
check("sent roughness factor", near(textures.get("roughness", {}).get("factor", 0), 0.5))
check("sent roughness intensity", near(textures.get("roughness", {}).get("intensity", 0), 0.25))
check("sent metalness intensity", near(textures.get("metalness", {}).get("intensity", 0), 1.0))
check("sent opacity intensity", near(textures.get("opacity", {}).get("intensity", 0), 0.6))

# --- without vertex paint the untextured value sits in the mix and travels back
addon.make_material(mesh, {"material": settings}, False, False, False)
shader = next(n for n in tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
mix = source(shader.inputs["Base Color"])
check("material color in the mix", mix is not None and near(sock(mix, "A_Color").default_value[:3], (0.8, 0.8, 0.8)))
sent = addon.material_settings(obj, False, False, False, store=store)
check("sent material color", near(sent["color"], (0.8, 0.8, 0.8)), sent["color"])
check("sent untextured roughness", near(sent["roughness"], 0.4), sent["roughness"])

print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
