# SPDX-License-Identifier: MIT
"""Decoded Nomad messages -> the Toolbag scene.

This is the only module that touches mset. Everything it does is wrapped: a
Toolbag build that names a material field differently should cost that one
setting, never the connection.

Toolbag's material shaders are picked by name at runtime rather than assumed,
because a slot's fields depend on which shader is in it (a 'Gloss' microsurface
wants the inverse of a 'Roughness' one).
"""
import math
import os
import re
import tempfile

import convert

try:
    import mset
except ImportError:  # unit tests inject a stand-in
    mset = None

# nomad texture channel -> (material slot, words to look for in the field name)
TEXTURE_SLOTS = {
    "color": ("albedo", ("albedo", "map")),
    "roughness": ("microsurface", ("map",)),
    "metalness": ("reflectivity", ("map",)),
    "normal": ("surface", ("normal", "map")),
    "emissive": ("emission", ("emissive", "map")),
    "occlusion": ("occlusion", ("occlusion", "map")),
    "opacity": ("transparency", ("map",)),
    "displacement": ("displacement", ("map",)),
}

# the color channels are authored in sRGB, every other map is raw data
SRGB_TEXTURES = ("color", "emissive")

# a default Toolbag material leaves these slots off; the first shader that the
# build accepts turns one on, so Nomad has somewhere to put the channel
SLOT_SHADERS = {
    "emission": ("Emissive",),
    "occlusion": ("Occlusion",),
    "transparency": ("Dither", "Alpha Blend", "Cutout", "Add"),
    "displacement": ("Height", "Displacement"),   # only bites with subdivision on
}

# nomad material type -> the Toolbag transparency shader that stands for the same
# look; subsurface is a diffusion shader instead, opaque and shadow_catcher neither
MATERIAL_TYPES = {
    "opaque": None,
    "subsurface": None,
    "shadow_catcher": None,
    "blending": "Dither",
    "dithering": "Dither",
    "additive": "Add",
    "refraction": "Refraction",
}


def _safe_name(name):
    """Blob names are untrusted display data: keep a basename, drop the rest."""
    name = os.path.basename(str(name or "texture"))
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "texture"


# Toolbag can die inside a native call, taking the traceback with it. Each step
# is written to nomad_link_last.txt first, so the file names whatever killed it.
TRACE = True

# Toolbag computes normals when they are omitted, and that pass builds the mesh
# adjacency map itself. Off sends ours instead, which is the way to find out
# whether a crash lives in that pass.
LEARN_NORMALS = True


class Scene:
    def __init__(self, log=None):
        self.objects = {}        # link_id -> mset object
        self.geometry = {}       # link_id -> the mset.Mesh handed to that object
        self.submeshes = {}      # link_id -> its SubMeshObject (needed to render)
        self.materials = {}      # link_id -> mset.Material
        self.blobs = {}          # texture_id / asset_id -> path on disk
        self.follow_view = False
        self.needs_normals = None   # unknown until the first mesh is written
        self._unit = None           # scene units per meter, asked once
        self.trace_path = ""
        self._steps = []
        self._folder = None
        self._log = log or (lambda text: None)

    # ------------------------------------------------------------------ meshes

    def unit_scale(self):
        """Scene units per Nomad meter: 100 in a centimeter scene, 1 in meters.

        Without this a sculpt lands 1/100th the size of the floor grid, which
        reads as an empty viewport. Asked every time rather than cached, so
        changing the scene's unit takes effect on the next thing Nomad sends.
        """
        try:
            self._unit = 1.0 / float(mset.getSceneUnitScale() or 1.0)
        except Exception:
            self._unit = self._unit or 1.0
        return self._unit

    def apply_mesh(self, link_id, mesh, built):
        """Create or replace a Toolbag mesh object from a decoded mesh_full."""
        obj = self.objects.get(link_id)
        fresh = obj is None or not _alive(obj)
        first = fresh and not self.objects
        if fresh:
            obj = mset.MeshObject()
        obj.name = mesh.get("name") or "Nomad mesh"
        try:
            self._write_geometry(link_id, obj, built)
        except Exception:
            # a MeshObject with no mesh makes Toolbag complain every frame
            if fresh:
                obj.destroy()
            self.objects.pop(link_id, None)
            self.geometry.pop(link_id, None)
            raise
        self.objects[link_id] = obj
        if first:   # the user is waiting on this one: bring the camera to it
            try:
                mset.frameObject(obj)
            except Exception:
                pass
        obj.visible = bool(mesh.get("visible", True))
        if mesh.get("material"):
            self.apply_material(link_id, mesh["material"], has_paint="colors" in built,
                                has_alpha="alpha" in mesh)
        return obj

    def _write_geometry(self, link_id, obj, built):
        problems = convert.validate(built)
        for field in ("vertices", "triangles"):
            if field in problems:   # the mesh itself is unusable: do not risk Toolbag
                raise ValueError("bad %s: %s" % (field, problems[field]))
        for field, reason in problems.items():
            self._log("dropped %s (%s)" % (field, reason))
            built.pop(field, None)

        if self.needs_normals is None:
            self._learn_normals()
        self.trace("Mesh(%d verts, %d tris)"
                   % (len(built["vertices"]) // 3, len(built["triangles"]) // 3))
        target = self._new_mesh(link_id, built)
        if self.needs_normals and "normals" in built:
            target.normals = built["normals"]
        # optional channels: a Toolbag that rejects one should not lose the mesh
        for field in ("polygons", "uvs", "colors"):
            if field in built:
                self.trace("mesh.%s" % field)
                try:
                    setattr(target, field, built[field])
                except Exception as exc:
                    self._log("mesh %s not applied: %s" % (field, exc))
        self.trace("obj.mesh =")
        obj.mesh = target
        # Toolbag never says which side owns a written mesh, so the one it was
        # given is kept here rather than left for the garbage collector, and
        # obj.mesh is never read back: writing a read-back mesh onto the object
        # it came from is a self-assignment inside Toolbag's own C++.
        self.geometry[link_id] = target
        self._ensure_submesh(link_id, obj)
        self.trace("done")

    def _ensure_submesh(self, link_id, obj):
        """A Python-built MeshObject renders NOTHING until it has a submesh.

        Imported models always carry one; the probe quad only appeared once
        addSubmesh ran. indexCount=-1 covers the whole mesh, so it survives
        topology changes.
        """
        sub = self.submeshes.get(link_id)
        if sub is not None and _alive(sub):
            return sub
        try:
            existing = obj.getChildren()
            sub = existing[0] if existing else None
        except Exception:
            sub = None
        if sub is None:
            self.trace("addSubmesh")
            try:
                sub = obj.addSubmesh(obj.name, self.materials.get(link_id), 0, -1)
            except Exception:
                try:
                    sub = obj.addSubmesh(obj.name)
                except Exception as exc:
                    self._log("submesh not created (mesh stays invisible): %s" % exc)
                    return None
        self.submeshes[link_id] = sub
        return sub

    def _new_mesh(self, link_id, built):
        """Toolbag's Mesh takes its geometry at construction, and a fresh
        MeshObject has none, so there is nothing to fill in place. Argument form
        varies by build, hence the ladder."""
        triangles, vertices = built["triangles"], built["vertices"]
        forms = (
            lambda: mset.Mesh(triangles=triangles, vertices=vertices),
            lambda: mset.Mesh(triangles, vertices),
            lambda: _fill(self.geometry.get(link_id), triangles, vertices),
        )
        failure = None
        for form in forms:
            try:
                mesh = form()
            except Exception as exc:
                failure = exc
                continue
            if mesh is not None:
                return mesh
        raise failure or RuntimeError("could not build an mset.Mesh")

    def trace(self, what):
        """Breadcrumb for a crash: the file holds the last calls attempted."""
        if not TRACE or not self.trace_path:
            return
        self._steps.append(what)
        del self._steps[:-12]
        try:
            with open(self.trace_path, "w") as handle:
                handle.write("\n".join(self._steps) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            pass

    def _learn_normals(self):
        """Does this Toolbag build its own normals? Ask it once, on a throwaway.

        If it does, the bridge stops computing them, which is the most expensive
        step there is. The question is asked on a throwaway object, because
        reading a mesh back out of a live object is what this code must not do.
        """
        self.needs_normals = True
        probe = None
        if not LEARN_NORMALS:
            return self._log("normals: sending our own (LEARN_NORMALS is off)")
        try:
            self.trace("normals probe")
            probe = mset.MeshObject()
            probe.name = "Nomad normals probe"
            probe.visible = False
            probe.mesh = mset.Mesh(triangles=[0, 1, 2],
                                   vertices=[0, 0, 0, 1, 0, 0, 0, 1, 0])
            written = getattr(probe.mesh, "normals", None)
            self.needs_normals = not (written is not None and len(written))
        except Exception as exc:
            self._log("normals probe failed: %s" % exc)
        if probe is not None and _alive(probe):
            probe.destroy()
        self._log("normals: %s" % ("sending our own" if self.needs_normals
                                   else "Toolbag computes them"))

    def update_geometry(self, link_id, built):
        """Rewrite vertex data after a delta, keeping the same object."""
        obj = self.objects.get(link_id)
        if obj is None or not _alive(obj):
            return False
        self._write_geometry(link_id, obj, built)
        return True

    def apply_object_state(self, link_id, header):
        obj = self.objects.get(link_id)
        if obj is None or not _alive(obj):
            return False
        if "name" in header:
            obj.name = header["name"] or obj.name
        if "visible" in header:
            obj.visible = bool(header["visible"])
        return True

    def delete(self, link_id):
        obj = self.objects.pop(link_id, None)
        self.geometry.pop(link_id, None)
        self.submeshes.pop(link_id, None)   # destroyed with its parent
        self.materials.pop(link_id, None)
        if obj is not None and _alive(obj):
            self.trace("destroy %s" % link_id[:8])
            obj.destroy()
            return True
        return False

    # ----------------------------------------------------- lights and cameras

    def apply_light(self, link_id, block):
        """Nomad light -> a Toolbag LightObject; ENVIRONMENT stays on the sky."""
        if block.get("light_type") == "ENVIRONMENT":
            return None
        obj = self.objects.get(link_id)
        if obj is None or not _alive(obj):
            self.trace("LightObject()")
            try:
                obj = mset.LightObject()
            except Exception as exc:
                self._log("light not created: %s" % exc)
                return None
            self.objects[link_id] = obj
        self._state(obj, block)
        kind = LIGHT_TYPES.get(block.get("light_type"))
        if kind:
            _try_set(obj, "lightType", kind)
        # Toolbag multiplies temperature with color; Nomad's kelvin replaces it,
        # so the color is forced white whenever temperature drives the light
        kelvin = bool(block.get("use_kelvin"))
        if kelvin:
            _try_set(obj, "color", [1.0, 1.0, 1.0])
        elif "color" in block:
            _try_set(obj, "color", list(block["color"]))
        if "use_kelvin" in block:
            _try_set(obj, "useTemperature", kelvin)
        if "kelvin" in block:
            _try_set(obj, "temperature", float(block["kelvin"]))
        # the block carries every strength; only one drives this light's type
        key = {"SUN": "intensity", "POINT": "power", "SPOT": "power"}.get(
            block.get("light_type"))
        if key and key in block:
            # Nomad's strengths are unitless, so Toolbag's lux/lumens mode
            # would read 1.0 as one lux; plain brightness matches
            _try_set(obj, "physicalUnits", False)
            # straight through: Toolbag's brightness field is log-scaled and
            # already relative to the scene unit, so a unit_scale correction here
            # reads as four decades of light
            _try_set(obj, "brightness", float(block[key]))
        if "spot_angle" in block:
            _try_set(obj, "spotAngle", math.degrees(float(block["spot_angle"])))
        if "spot_softness" in block:
            _try_set(obj, "spotSharpness", 1.0 - float(block["spot_softness"]))
        if "size" in block:
            _try_set(obj, "width", float(block["size"]) * self.unit_scale())
        if "shadow_cast" in block:
            _try_set(obj, "castShadows", bool(block["shadow_cast"]))
        return obj

    def apply_camera_object(self, link_id, block):
        """Nomad camera -> a scene CameraObject to render through, not the view."""
        obj = self.objects.get(link_id)
        if obj is None or not _alive(obj):
            self.trace("CameraObject()")
            try:
                obj = mset.CameraObject()
            except Exception as exc:
                self._log("camera not created: %s" % exc)
                return None
            self.objects[link_id] = obj
        self._state(obj, block)
        if "orthographic" in block:
            _try_set(obj, "mode",
                     "orthographic" if block["orthographic"] else "perspective")
        if "fov_y" in block:
            _try_set(obj, "fov", float(block["fov_y"]))
        return obj

    def _state(self, obj, block):
        """The object_state part every non-mesh shares: name, visibility, place."""
        if "name" in block:
            obj.name = block["name"] or obj.name
        if "visible" in block:
            obj.visible = bool(block["visible"])
        matrix = block.get("world_matrix")
        if matrix:
            unit = self.unit_scale()
            try:
                obj.position = [matrix[12] * unit, matrix[13] * unit, matrix[14] * unit]
                obj.rotation = euler_from_matrix(matrix)
            except Exception as exc:
                self._log("transform not applied: %s" % exc)

    # --------------------------------------------------------------- materials

    def apply_material(self, link_id, block, has_paint=False, has_alpha=False):
        """Nomad's PBR block -> a Toolbag material assigned to this object."""
        obj = self.objects.get(link_id)
        if obj is None or not _alive(obj):
            return None
        material = self.materials.get(link_id)
        if material is None or not _alive(material):
            self.trace("Material()")
            material = mset.Material(_unique_material_name(obj.name))
            self.materials[link_id] = material

        # Toolbag's Vertex Color albedo shader keeps its Albedo Map slot, so paint
        # and a color texture coexist there, the way Nomad multiplies them.
        textures = block.get("textures") or {}
        if has_paint:
            self._use_vertex_color(material, has_alpha)
        else:
            self._use_albedo_map(material)
        self._apply_type(material, block)

        # Nomad's slider and the map's factor both multiply the map, so each slot
        # takes the factor once the map is there and the plain slider otherwise.
        # Every one of them is written every time: a factor left behind by a map
        # that has since been removed would tint the material for good.
        tint = self._factor(textures, "color", [1.0, 1.0, 1.0])
        color = (list(block.get("color") or []) if not has_paint else []) or [1.0, 1.0, 1.0]
        if tint:
            color = [c * t for c, t in zip(color, list(tint))]
        self._set(material, "albedo", [("color",)], color)

        rough = self._factor(textures, "roughness")
        rough = block.get("roughness") if rough is None else rough
        if rough is not None:
            self._set_microsurface(material, float(rough))

        metal = self._factor(textures, "metalness")
        metal = block.get("metalness") if metal is None else metal
        if metal is not None:
            self._set(material, "reflectivity", [("metal",)], float(metal))

        # the opacity map multiplies the slider rather than replacing it
        opacity = self._factor(textures, "opacity")
        alpha = float(block.get("opacity", 1.0)) * (1.0 if opacity is None else float(opacity))
        if alpha < 1.0 or _subroutine(material, "transparency") is not None:
            self._set(material, "transparency", [("alpha",), ("opacity",)], alpha)

        # the slots below only exist because a map put them there, so they are
        # written back to neutral rather than switched on for nothing
        occlusion = self._factor(textures, "occlusion")
        if occlusion is not None or _subroutine(material, "occlusion") is not None:
            self._set(material, "occlusion", [("occlusion",)],
                      1.0 if occlusion is None else float(occlusion))

        glow = self._factor(textures, "emissive", [1.0, 1.0, 1.0])
        if glow is not None:
            self._set(material, "emission", [("color",)], list(glow))
            strength = (textures.get("emissive") or {}).get("strength", 1.0)
            self._set(material, "emission", [("intensity",), ("strength",)], float(strength))
        elif _subroutine(material, "emission") is not None:
            self._set(material, "emission", [("intensity",), ("strength",)], 0.0)

        self.trace("material textures")
        for channel, texture in textures.items():
            self._apply_texture(material, channel, texture)

        self.trace("material.assign")
        sub = self.submeshes.get(link_id)
        if sub is not None and _alive(sub):
            try:
                sub.material = material   # the submesh is what actually renders
            except Exception as exc:
                self._log("submesh material not set: %s" % exc)
        try:
            assign_material(material, obj)
        except Exception as exc:
            self._log("material not assigned: %s" % exc)
        return material

    def _use_vertex_color(self, material, has_alpha=False):
        if not _find_field(_subroutine(material, "albedo"), ("vertex",), texture=False):
            for shader in ("Vertex Color", "Vertex Colors", "Albedo Vertex Color"):
                try:
                    material.setSubroutine("albedo", shader)
                    break
                except Exception:
                    continue
            else:
                self._log("no vertex-color albedo shader found; paint may not show")
                return False
        # Nomad's paint arrives linear, so the sRGB decode would double-darken it;
        # alpha only means something when the opacity channel actually travelled
        self._set(material, "albedo", [("srgb",)], False)
        self._set(material, "albedo", [("vertex", "alpha")], bool(has_alpha))
        return True

    def _apply_type(self, material, block):
        """Nomad's material type -> the Toolbag slot that carries the same look."""
        kind = block.get("material_type")
        if kind is None:
            return
        if kind not in MATERIAL_TYPES:
            self._log("unknown material type %s" % kind)
            return
        shader = MATERIAL_TYPES[kind]
        if shader:
            self._use_shader(material, "transparency", shader)
        if kind == "refraction" and "refraction_ior" in block:
            self._set(material, "transparency", [("ior",), ("index",)],
                      float(block["refraction_ior"]))
        if kind == "subsurface":
            self._subsurface(material, block)
        elif _scatters(material):
            self._use_shader(material, "diffusion", "Lambertian")

    def _subsurface(self, material, block):
        """Toolbag scatters in the diffusion slot, so the type lands there rather
        than on transparency. Nomad's depth is -1 when it wants its own default."""
        self._use_shader(material, "diffusion", "Subsurface Scatter")
        if not _scatters(material):
            return
        color = block.get("subsurface_color")
        if color:
            self._set(material, "diffusion",
                      [("subdermis",), ("scatter", "color"), ("color",)], list(color))
        depth = block.get("subsurface_depth")
        if depth is not None and depth >= 0:
            self._set(material, "diffusion", [("depth",)], float(depth))

    def _use_shader(self, material, slot_name, shader):
        """Switch a slot to a named shader, unless it is the one already running."""
        slot = _subroutine(material, slot_name)
        if _shader_name(slot) == shader.lower():
            return slot
        try:
            material.setSubroutine(slot_name, shader)
        except Exception:
            self._log("no %s shader for %s" % (shader, slot_name))
            return slot
        return _subroutine(material, slot_name)

    def _use_albedo_map(self, material):
        """Undo a previous vertex-color swap so a color texture has a slot again."""
        if _find_field(_subroutine(material, "albedo"), ("map",), texture=True):
            return True
        for shader in ("Albedo", "Albedo Map"):
            try:
                material.setSubroutine("albedo", shader)
                return True
            except Exception:
                continue
        return False

    def _set_microsurface(self, material, roughness):
        """Toolbag ships gloss and roughness shaders; they are inverses."""
        slot = _subroutine(material, "microsurface")
        if slot is None:
            return False
        gloss = "gloss" in str(getattr(slot, "name", "")).lower()
        return self._set(material, "microsurface", [("gloss",)] if gloss else [("rough",)],
                         1.0 - roughness if gloss else roughness)

    def _slot(self, material, slot_name):
        """The subroutine, switched on first if the material still has it empty."""
        slot = _subroutine(material, slot_name)
        if slot is not None:
            return slot
        for shader in SLOT_SHADERS.get(slot_name, ()):
            try:
                material.setSubroutine(slot_name, shader)
            except Exception:
                continue
            slot = _subroutine(material, slot_name)
            if slot is not None:
                return slot
        self._log("no %s slot on this material" % slot_name)
        return None

    def _set(self, material, slot_name, candidates, value):
        """Set the first field matching any candidate; builds name them differently."""
        slot = self._slot(material, slot_name)
        field = next((f for f in (_find_field(slot, words, texture=False)
                                  for words in candidates) if f), None)
        if field is None:
            self._log("no %s field for %s"
                      % (slot_name, "/".join(w[0] for w in candidates)))
            return False
        try:
            slot.setField(field, value)
            return True
        except Exception as exc:
            self._log("%s.%s not set: %s" % (slot_name, field, exc))
            return False

    def _factor(self, textures, channel, default=1.0):
        """The channel's factor, or None while no map of that channel is loaded."""
        texture = textures.get(channel) or {}
        if not self.blobs.get(texture.get("texture_id")):
            return None
        value = texture.get("factor")
        return default if value is None else value

    def _apply_texture(self, material, channel, texture):
        slot_name, words = TEXTURE_SLOTS.get(channel, (None, None))
        if slot_name is None:
            self._log("no slot for the %s channel" % channel)
            return False
        texture = texture or {}
        if not texture.get("texture_id"):
            return self._clear_texture(material, channel, slot_name, words)
        path = self.blobs.get(texture["texture_id"])
        if not path:
            return False   # the pixels have not landed yet, keep what is there
        slot = self._slot(material, slot_name)
        field = _find_field(slot, words, texture=True)
        if field is None:
            self._log("no %s texture field" % slot_name)
            return False
        try:
            slot.setField(field, path)
        except Exception as exc:
            self._log("%s texture not set: %s" % (slot_name, exc))
            return False
        # the color space lives on the texture, not on the slot, so the vertex
        # color toggle below never speaks for the map sharing that slot
        self._set_texture_srgb(slot, field, path, channel in SRGB_TEXTURES)
        if channel == "opacity":
            self._set(material, slot_name, [("channel",)], 0)   # Nomad reads the red one
        if channel == "normal" and "neg_y" in texture:
            self._set(material, slot_name, [("flip", "y")], bool(texture["neg_y"]))
        return True

    def _clear_texture(self, material, channel, slot_name, words):
        """An empty channel means the map was removed, not that it is unchanged."""
        slot = _subroutine(material, slot_name)   # never switch a slot on to empty it
        field = _find_field(slot, words, texture=True)
        if field is None:
            return False
        try:
            slot.setField(field, None)
        except Exception as exc:
            self._log("%s texture not cleared: %s" % (slot_name, exc))
            return False
        # an Emissive slot with no map still glows its own color, so mute it
        if channel == "emissive":
            self._set(material, slot_name, [("intensity",), ("strength",)], 0.0)
        return True

    def _set_texture_srgb(self, slot, field, path, srgb):
        texture = None
        try:
            texture = slot.getField(field)
        except Exception:
            pass
        if not hasattr(texture, "sRGB"):
            try:
                texture = mset.findTexture(path)
            except Exception:
                texture = None
        if not hasattr(texture, "sRGB"):
            self._log("no texture object for %s; srgb left as loaded" % field)
            return False
        try:
            texture.sRGB = bool(srgb)
            return True
        except Exception as exc:
            self._log("srgb not set on %s: %s" % (field, exc))
            return False

    # ------------------------------------------------------------------- blobs

    def store_blob(self, blob_id, name, data):
        """Keep a texture/asset payload on disk; Toolbag loads images by path."""
        if not blob_id or blob_id in self.blobs:
            return self.blobs.get(blob_id)
        if self._folder is None:
            self._folder = tempfile.mkdtemp(prefix="nomad_link_")
        path = os.path.join(self._folder, "%s_%s" % (blob_id[:8], _safe_name(name)))
        try:
            with open(path, "wb") as handle:
                handle.write(data)
        except OSError as exc:
            self._log("could not cache %s: %s" % (name, exc))
            return None
        self.blobs[blob_id] = path
        return path

    # ------------------------------------------------------------------ shading

    def apply_shading(self, shading):
        """Nomad's environment -> the Toolbag sky, which is the lookdev part."""
        sky = _first_of_type("SkyBoxObject")
        if sky is None:
            return False
        path = self.blobs.get(shading.get("environment_id"))
        if path:
            try:
                self.trace("sky.importImage")
                sky.importImage(path)
            except Exception as exc:
                self._log("environment not loaded: %s" % exc)
        for key, attribute in (("environment_rotation", "rotation"),
                               ("environment_exposure", "brightness"),
                               ("background_blur", "blur")):
            if key in shading:
                try:
                    setattr(sky, attribute, float(shading[key]))
                except Exception:
                    pass
        return True

    # ------------------------------------------------------------------ camera

    def apply_camera(self, header):
        """Point Toolbag's viewport camera the way Nomad's is pointing."""
        if not self.follow_view:
            return False
        camera = None
        try:
            camera = mset.getCamera()
        except Exception:
            pass
        if camera is None:
            return False
        matrix = header.get("world_from_view")
        try:
            if matrix:
                self.trace("camera move")
                unit = self.unit_scale()   # same meters -> scene units as the meshes
                camera.position = [matrix[12] * unit, matrix[13] * unit, matrix[14] * unit]
                camera.rotation = euler_from_matrix(matrix)
            if "orthographic" in header:
                _try_set(camera, "mode", "orthographic" if header["orthographic"]
                         else "perspective")
                if header["orthographic"] and "ortho_scale" in header:
                    _try_set(camera, "orthoScale",
                             float(header["ortho_scale"]) * self.unit_scale())
            if "fov_y" in header:
                camera.fov = float(header["fov_y"])
        except Exception as exc:
            self._log("camera not applied: %s" % exc)
            return False
        return True

    def clear_scene(self):
        """Replace all, the way Blender's button means it: every mesh, light and
        camera goes, the user's included. The sky, the render setup and the
        working camera stay -- Toolbag needs a view to draw through."""
        keep = None
        try:
            keep = mset.getCamera()
        except Exception:
            pass
        for class_name in ("MeshObject", "LightObject", "CameraObject"):
            for obj in objects_of_type(class_name):
                if obj is keep or not _alive(obj):
                    continue
                self.trace("destroy %s" % obj.name)
                try:
                    obj.destroy()
                except Exception:
                    pass
        for material in self.materials.values():
            if _alive(material):
                material.destroy()
        self.clear()

    def clear(self):
        self.objects.clear()
        self.geometry.clear()
        self.submeshes.clear()
        self.materials.clear()


LIGHT_TYPES = {"SUN": "directional", "POINT": "omni", "SPOT": "spot"}


# ------------------------------------------------------------------- utilities

def _try_set(obj, attribute, value):
    """A light/camera setting a build lacks costs that setting, nothing else."""
    try:
        setattr(obj, attribute, value)
        return True
    except Exception:
        return False


def euler_from_matrix(matrix, degrees=True):
    """Column-major rotation -> Toolbag's Euler angles.

    Toolbag applies z, then x, then y (R = Ry * Rx * Rz), measured by probe.py
    against getBounds(). This is the only place that encodes it.
    """
    import math
    m = list(matrix)
    # basis vectors, normalised so a scaled matrix still yields a pure rotation
    columns = []
    for c in range(3):
        x, y, z = m[c * 4], m[c * 4 + 1], m[c * 4 + 2]
        length = math.sqrt(x * x + y * y + z * z) or 1.0
        columns.append((x / length, y / length, z / length))
    (m00, m10, m20), (m01, m11, m21), (m02, m12, m22) = columns

    sx = -m12
    if abs(sx) < 0.999999:
        x = math.asin(max(-1.0, min(1.0, sx)))
        y = math.atan2(m02, m22)
        z = math.atan2(m10, m11)
    else:  # gimbal lock: x is straight up or down, fold y into z
        x = math.pi / 2 if sx > 0 else -math.pi / 2
        y = 0.0
        z = math.atan2(-m01, m00)
    angles = [x, y, z]
    return [math.degrees(a) for a in angles] if degrees else angles


def _fill(mesh, triangles, vertices):
    """Last resort: an existing Mesh, written in place."""
    if mesh is None:
        return None
    mesh.triangles = triangles
    mesh.vertices = vertices
    return mesh


def _alive(obj):
    """Toolbag objects survive their scene as dead handles; poke one to find out."""
    try:
        _ = obj.name
        return True
    except Exception:
        return False


def _subroutine(material, slot):
    try:
        return getattr(material, _SLOT_ATTRIBUTES.get(slot, slot))
    except Exception:
        return None


def _shader_name(slot):
    return str(getattr(slot, "name", "")).lower()


def _scatters(material):
    return "subsurface" in _shader_name(_subroutine(material, "diffusion"))


def _find_field(slot, words, texture):
    """First field whose name carries every word; 'map' marks the texture ones."""
    if slot is None:
        return None
    try:
        names = slot.getFieldNames()
    except Exception:
        return None
    for name in names:
        lowered = name.lower()
        if texture != ("map" in lowered or "texture" in lowered):
            continue
        if all(word in lowered for word in words):
            return name
    return None


def objects_of_type(class_name):
    """Toolbag 5.032 answers "Must pass an object of type" to the documented
    string, so the class object is tried first and the string kept as fallback."""
    for wanted in (getattr(mset, class_name, None), class_name):
        if wanted is None:
            continue
        try:
            found = list(mset.getAllObjectsOfType(wanted))
        except Exception:
            continue
        if found:   # empty is also what the form it dislikes returns
            return found
    return []


def assign_material(material, obj):
    """5.032 rejects the documented includeChildren argument ("The attribute value
    must be an Object"), so the one-argument form goes first."""
    failure = None
    for args in ((obj,), (obj, True)):
        try:
            material.assign(*args)
            return True
        except Exception as exc:
            failure = exc
    raise failure


def _first_of_type(class_name):
    found = objects_of_type(class_name)
    return found[0] if found else None


def _unique_material_name(base):
    """Toolbag requires unique material names."""
    name = "%s (Nomad)" % (base or "Nomad")
    try:
        existing = {m.name for m in mset.getAllMaterials()}
    except Exception:
        return name
    if name not in existing:
        return name
    for index in range(2, 1000):
        candidate = "%s %d" % (name, index)
        if candidate not in existing:
            return candidate
    return name


_SLOT_ATTRIBUTES = {
    "albedo": "albedo",
    "microsurface": "microsurface",
    "reflectivity": "reflectivity",
    "surface": "surface",
    "emission": "emission",
    "occlusion": "occlusion",
    "transparency": "transparency",
    "displacement": "displacement",
    "diffusion": "diffusion",
}
