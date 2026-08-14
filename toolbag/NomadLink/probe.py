# SPDX-License-Identifier: MIT
"""Answers the Toolbag API questions the docs leave open.

Press "Run probe" in the Nomad Link window. It must run inside Toolbag -- mset
only exists there -- and reports to the console (Cmd+Shift+C / Ctrl+~), to a
`nomad_probe_report.txt` beside this file, and to the panel.

Toolbag dies inside its own C++ on data it does not like, with no traceback, so
every call is named in `nomad_probe_last.txt` before it is made and the report is
flushed line by line. Whatever is left in that file after a crash is the call
that did it, and the next run skips that one and finishes the rest.

It only adds objects; nothing in the open scene is modified. One 'Nomad probe
quad' is left behind for the two checks a script cannot make: UV orientation and
face winding.
"""
import math
import os
import struct
import sys
import zlib

import scene as scene_module

try:
    import mset
except ImportError:  # unit tests inject a stand-in
    mset = None

QUAD_VERTICES = [0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0]
QUAD_TRIANGLES = [0, 1, 2, 0, 2, 3]


def png(path, texels, width, height):
    """Smallest possible RGB png, so the UV check needs no external file."""
    raw = b"".join(b"\x00" + bytes(texels[y * width * 3:(y + 1) * width * 3])
                   for y in range(height))

    def chunk(tag, data):
        body = tag + data
        return struct.pack("!I", len(data)) + body + struct.pack("!I", zlib.crc32(body))

    header = struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0)
    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return path


def rotate(point, angles, order, degrees=True):
    x, y, z = (math.radians(a) for a in angles) if degrees else angles
    sx, cx, sy, cy, sz, cz = (math.sin(x), math.cos(x), math.sin(y),
                              math.cos(y), math.sin(z), math.cos(z))
    matrices = {
        "x": ((1, 0, 0), (0, cx, -sx), (0, sx, cx)),
        "y": ((cy, 0, sy), (0, 1, 0), (-sy, 0, cy)),
        "z": ((cz, -sz, 0), (sz, cz, 0), (0, 0, 1)),
    }
    for axis in order:
        m = matrices[axis]
        point = tuple(sum(m[r][c] * point[c] for c in range(3)) for r in range(3))
    return point


def _write(path, text):
    """Straight to the platter: a crash must not take the file with it."""
    try:
        with open(path, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass


def _named(items, name):
    """Whatever a previous run left behind under this name, if it is still there."""
    try:
        return next((item for item in items if item.name == name), None)
    except Exception:
        return None


class Probe:
    def __init__(self, folder=""):
        self.report = []
        self.folder = folder or os.path.expanduser("~")
        self.report_path = os.path.join(self.folder, "nomad_probe_report.txt")
        self.trace_path = os.path.join(self.folder, "nomad_probe_last.txt")
        self.keep = []      # nothing handed to Toolbag is left for the GC to free
        self.mesh_form = ""
        self.skip = self._last_crash()
        self._stack = []

    # -------------------------------------------------------------- bookkeeping

    def _last_crash(self):
        """The call that was in flight when Toolbag last died, if it did."""
        try:
            with open(self.trace_path) as handle:
                names = [line.strip() for line in handle if line.strip()]
        except OSError:
            return ""
        return names[-1] if names else ""

    def say(self, text):
        self.report.append(text)
        print(text)
        _write(self.report_path, "\n".join(self.report) + "\n")

    def begin(self, label):
        """Name the call before making it; False when it killed the last run."""
        if label == self.skip:
            self.say("%-28s SKIPPED: this crashed Toolbag on the last run" % label)
            return False
        self._stack.append(label)
        _write(self.trace_path, "\n".join(self._stack))
        return True

    def done(self):
        del self._stack[-1:]
        _write(self.trace_path, "\n".join(self._stack))

    def attempt(self, label, function):
        if not self.begin(label):
            return
        try:
            self.say("%-28s %s" % (label, function()))
        except Exception as exc:
            self.say("%-28s FAILED: %s" % (label, exc))
        self.done()

    def run_section(self, name, section, *args):
        """One failing section must not cost the rest of the report."""
        if not self.begin("section %s" % name):
            return None
        try:
            result = section(*args)
        except Exception as exc:
            self.say("")
            self.say("!! %s stopped: %s" % (name, exc))
            result = None
        self.done()
        return result

    # -------------------------------------------------------------------- mesh

    def new_mesh(self, triangles, vertices, **channels):
        """A Mesh carrying whatever channels this build accepts.

        Nothing is attached to an object here: which side owns a mesh after
        `obj.mesh = ` is undocumented, so the probe holds every one it builds.
        """
        mesh = self._construct(triangles, vertices)
        self.keep.append(mesh)
        for name, value in channels.items():
            if value is not None:
                setattr(mesh, name, value)
        return mesh

    def _construct(self, triangles, vertices):
        """Whichever constructor form this build accepts; remembers the winner."""
        forms = (
            ("Mesh(triangles=, vertices=)",
             lambda: mset.Mesh(triangles=triangles, vertices=vertices)),
            ("Mesh(triangles, vertices)", lambda: mset.Mesh(triangles, vertices)),
        )
        failure = None
        for name, form in forms:
            try:
                mesh = form()
            except Exception as exc:
                failure = "%s -> %s" % (name, exc)
                continue
            if self.mesh_form != name:
                self.mesh_form = name
                self.say("%-28s %s" % ("mesh constructor", name))
            return mesh
        raise RuntimeError(failure or "no way to build an mset.Mesh")

    def object_named(self, name):
        """Reuse the object the last run left, so probing twice leaves one quad."""
        obj = _named(scene_module.objects_of_type("MeshObject"), name)
        if obj is None:
            obj = mset.MeshObject()
        obj.name = name
        self.keep.append(obj)
        return obj

    # ------------------------------------------------------------------ probes

    def environment(self):
        self.say("=== environment ===")
        self.attempt("toolbag version", lambda: mset.getToolbagVersion())
        self.attempt("plugin path", lambda: mset.getPluginPath())
        self.attempt("scene unit scale", lambda: mset.getSceneUnitScale())
        self.say("%-28s %s" % ("python", sys.version.split()[0]))

    def mesh_api(self):
        self.say("")
        self.say("=== mesh construction ===")
        self.say("%-28s %s" % ("mset.Mesh signature",
                               (getattr(mset.Mesh, "__doc__", "") or "?").strip()
                               .replace("\n", " | ")[:200]))
        self.say("%-28s %s" % ("  __init__",
                               (getattr(getattr(mset.Mesh, "__init__", None),
                                        "__doc__", "") or "?").strip()
                               .replace("\n", " | ")[:200]))
        obj = self.object_named("Nomad probe quad")
        self.say("%-28s %s" % ("its .mesh attribute",
                               type(getattr(obj, "mesh", None)).__name__))

        self.attempt("vertices + triangles only",
                     lambda: "%d floats survive" % len(
                         self.new_mesh(QUAD_TRIANGLES, QUAD_VERTICES).vertices))
        self.attempt("polygons [start, count]",
                     lambda: "accepted %s" % (self.new_mesh(
                         QUAD_TRIANGLES, QUAD_VERTICES, polygons=[0, 2]).polygons,))
        self.attempt("uvs per vertex",
                     lambda: "accepted %d floats" % len(self.new_mesh(
                         QUAD_TRIANGLES, QUAD_VERTICES,
                         uvs=[0, 0, 1, 0, 1, 1, 0, 1]).uvs))
        self.attempt("colors rgba per vertex",
                     lambda: "accepted %d floats" % len(self.new_mesh(
                         QUAD_TRIANGLES, QUAD_VERTICES, colors=[1, 0, 0, 1] * 4).colors))
        self.attempt("normals when omitted", self.normals_check)
        return obj

    def normals_check(self):
        """Does this build fill normals in? Ask it on an object nobody keeps."""
        bare = mset.MeshObject()
        bare.name = "Nomad probe normals"
        self.keep.append(bare)
        bare.mesh = self.new_mesh(QUAD_TRIANGLES, QUAD_VERTICES)
        got = list(getattr(bare.mesh, "normals", []) or [])
        bare.destroy()
        return ("computed by toolbag (%d)" % len(got) if got
                else "EMPTY - the bridge must send its own")

    def rotation_order(self):
        """Set a known rotation, then read the bounds back to see what Toolbag did."""
        self.say("")
        self.say("=== transform ===")
        box = [(x, y, z) for x in (0.0, 2.0) for y in (0.0, 1.0) for z in (0.0, 0.5)]
        obj = mset.MeshObject()
        obj.name = "Nomad probe rotation"
        self.keep.append(obj)
        obj.mesh = self.new_mesh([0, 1, 2, 1, 3, 2, 4, 5, 6, 5, 7, 6],
                                 [c for p in box for c in p])

        angles = [30.0, 40.0, 50.0]
        try:
            obj.rotation = angles
            bounds = obj.getBounds()
        except Exception as exc:
            obj.destroy()
            return self.say("rotation probe failed: %s" % exc)
        if not bounds:
            obj.destroy()
            return self.say("rotation probe failed: no bounds")

        best, error = None, None
        for degrees in (True, False):
            for order in ("xyz", "xzy", "yxz", "yzx", "zxy", "zyx"):
                spun = [rotate(p, angles, order, degrees) for p in box]
                low = [min(p[i] for p in spun) for i in range(3)]
                high = [max(p[i] for p in spun) for i in range(3)]
                delta = sum(abs(low[i] - bounds[0][i]) + abs(high[i] - bounds[1][i])
                            for i in range(3))
                if error is None or delta < error:
                    best, error = (order, degrees), delta
        order, degrees = best
        self.say("%-28s applies %s, in %s (residual %.4f)"
                 % ("rotation [30, 40, 50]", order.upper(),
                    "degrees" if degrees else "radians", error))
        # the bridge decomposes R = Ry*Rx*Rz, which is z-then-x-then-y here
        self.say("%-28s %s" % ("scene.euler_from_matrix",
                               "matches" if best == ("zxy", True)
                               else "NEEDS UPDATING for %s" % order.upper()))
        self.say("  (a large residual means the bounds are object-local and this"
                 " probe cannot tell)")
        obj.destroy()

    def materials(self):
        self.say("")
        self.say("=== material slots ===")
        name = "Nomad probe material"
        material = _named(mset.getAllMaterials(), name) or mset.Material(name)
        self.keep.append(material)
        for slot in ("albedo", "microsurface", "reflectivity", "surface",
                     "emission", "occlusion", "transparency"):
            try:
                sub = getattr(material, slot)
                self.say("%-16s %-18s %s" % (slot, sub.name, sub.getFieldNames()))
            except Exception as exc:
                self.say("%-16s unavailable: %s" % (slot, exc))

        self.say("")
        self.say("albedo shaders this build accepts:")
        for shader in ("Albedo", "Vertex Color", "Vertex Colors", "Albedo Vertex Color"):
            try:
                material.setSubroutine("albedo", shader)
                self.say("  %-24s yes -> fields %s"
                         % (shader, material.albedo.getFieldNames()))
            except Exception as exc:
                self.say("  %-24s no (%s)" % (shader, exc))
        try:
            material.setSubroutine("albedo", "Albedo")
        except Exception:
            pass
        return material

    def visual_check(self, obj, material):
        """A quad with known corners, for the conventions scripts cannot read."""
        self.say("")
        self.say("=== look at the 'Nomad probe quad' in the viewport ===")
        texels = [255, 0, 0, 0, 255, 0,      # top row:    red,  green
                  0, 0, 255, 255, 255, 255]  # bottom row: blue, white
        try:
            path = png(os.path.join(self.folder, "nomad_probe_uv.png"), texels, 2, 2)
        except OSError as exc:
            return self.say("could not write the test texture: %s" % exc)

        # a quad in the XY plane, front face toward +Z under counter-clockwise winding
        vertices = [-1, -1, 0, 1, -1, 0, 1, 1, 0, -1, 1, 0]
        uvs = [0, 0, 1, 0, 1, 1, 0, 1]          # v=0 at the bottom, as the bridge sends
        mesh = self.new_mesh([0, 1, 2, 0, 2, 3], vertices, uvs=uvs)
        self.attempt("quad geometry", lambda: _attach(obj, mesh))
        # a mesh that is in the scene but draws nothing: does Toolbag see its
        # extent, and does it want a submesh to render a range at all?
        self.attempt("quad bounds", lambda: obj.getBounds())
        self.attempt("quad children", lambda: [type(c).__name__ for c in obj.getChildren()]
                     or "none")
        self.attempt("quad texture", lambda: _texture(material, path))
        self.attempt("quad material assign",
                     lambda: "ok" if scene_module.assign_material(material, obj) else "no")
        self.attempt("quad submesh", lambda: _submesh(obj, material))
        self.attempt("frame the quad", lambda: mset.frameObject(obj) or "ok")

        self.say("0. Is the quad there at all? If the viewport is empty, say which of")
        self.say("           the lines above answered, and whether the camera moved.")
        self.say("1. UVs   - the quad's TOP-LEFT corner should be RED.")
        self.say("           If it is BLUE, set FLIP_V = False in convert.py.")
        self.say("2. Faces - the quad should be lit and solid from the front.")
        self.say("           If it is culled, the winding needs reversing.")

    def run(self):
        self.say("Nomad Link probe")
        if self.skip:
            self.say("(last run died in '%s'; skipping it this time)" % self.skip)
        self.run_section("environment", self.environment)
        quad = self.run_section("mesh construction", self.mesh_api)
        self.run_section("transform", self.rotation_order)
        material = self.run_section("material slots", self.materials)
        if quad is not None and material is not None:
            self.run_section("visual check", self.visual_check, quad, material)

        self.say("")
        self.say("report written to %s" % self.report_path)
        _write(self.trace_path, "")   # got to the end: nothing to skip next time
        return "\n".join(self.report)


def _attach(obj, mesh):
    obj.mesh = mesh
    return "%d floats attached" % len(mesh.vertices)


def _submesh(obj, material):
    """One is enough; a second run must not stack them."""
    existing = obj.getChildren()
    if existing:
        return "already has %d" % len(existing)
    return type(obj.addSubmesh("Nomad probe submesh", material)).__name__


def _texture(material, path):
    field = next(f for f in material.albedo.getFieldNames() if "map" in f.lower())
    material.albedo.setField(field, path)
    return field


def run(folder=""):
    if mset is None:
        raise RuntimeError("the probe only runs inside Marmoset Toolbag")
    return Probe(folder).run()
