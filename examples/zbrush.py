# SPDX-License-Identifier: MIT
"""Bridge Nomad Sculpt and desktop ZBrush through GoZ.

Usage: python3 zbrush.py [--nomad host[:port]] [options]

Runs on the computer where ZBrush is installed (GoZ is a desktop feature;
ZBrush for iPad has none). Connects to Nomad as a Link client (see
PROTOCOL.md) and talks to ZBrush through GoZ's shared folders
(/Users/Shared/Pixologic on macOS, C:/Users/Public/Pixologic on Windows) —
ZBrush needs GoZ installed once from Preferences > GoZ. Requires numpy.

Operations, exposed as terminal commands and through Nomad's Link menu:
  pull — Nomad's selection becomes ZBrush subtools (also triggered by Send
         in Nomad): .GoZ files are written and GoZBrushFromApp makes ZBrush
         import them (matching tool names are updated in place)
  get  — presses ZBrush's GoZ button remotely (a ZScript is handed to the
         running instance, which brings ZBrush to the front) so the export
         is fresh, then forwards it to Nomad; triggered by Get in Nomad.
         Falls back to resending the last export when ZBrush is not found
         (pass --zbrush to pin the application).
  send — the last GoZ export from ZBrush lands in Nomad as linked meshes.
         Pressing GoZ in ZBrush triggers this automatically: the bridge
         watches GoZ_ObjectList.txt, so any GoZ target application works
         ("Nomad" is registered as one).

The bridge keeps running when Nomad quits or the network drops, and
reconnects (or re-discovers) on its own.

Carried both ways: positions, quads/triangles, UVs, polypaint (Nomad vertex
colors), mask, polygroups (Nomad face groups). Object names link the two
sides: a mesh pulled from Nomad and sent back from ZBrush updates the
original Nomad object instead of creating a new one.
"""
import argparse
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import uuid
import zlib
from pathlib import Path

try:
    import numpy
except ImportError:
    sys.exit("numpy is required: pip install numpy")

import transport

PROTOCOL = 1
COLLECT_SILENCE = 0.8  # selection transfers arrive as one packet per mesh; export after this quiet gap
WATCH_POLL = 0.5  # GoZ_ObjectList.txt mtime check cadence
GET_FRESH_TIMEOUT = 8.0  # wait this long for the scripted GoZ press before resending the last export
RECONNECT_RETRY = 3.0  # pause between reconnection attempts when Nomad is gone
IDENTITY = [1.0 if i % 5 == 0 else 0.0 for i in range(16)]
# honest hello: no scene_edits/object_state/material — this bridge only does explicit transfers
CAPABILITIES = ["selection_transfer", "scene_transfer", "session_config"]
# Nomad is y-up with +z toward the viewer, GoZ is y-down with +z away: negate
# both (a 180° rotation about x, so winding and UV/texture orientation survive)
AXIS = numpy.array([1.0, -1.0, -1.0], "f8")
UNGROUPED = 65504  # ZBrush's polygroup id for faces outside any group

# GoZ chunk tags (uint32 little-endian); every chunk is tag + uint32 size
# (payload + 16) + 8 more header bytes + payload, ending with 16 zero bytes.
# The 8 bytes are usually a uint64 count, but polypaint carries uint32 count +
# a nonzero float — parse array lengths from the size field, never the count.
TAG_NAME_END = 0x1389
TAG_VERTICES = 0x2711
TAG_FACES = 0x4E21
TAG_UVS = 0x61A9
TAG_POLYPAINT = 0x88B9
TAG_MASK = 0x7532
TAG_POLYGROUPS = 0x9C41

SRGB_TO_LINEAR = numpy.arange(256, dtype="f8") / 255.0
SRGB_TO_LINEAR = numpy.where(
    SRGB_TO_LINEAR <= 0.04045, SRGB_TO_LINEAR / 12.92, ((SRGB_TO_LINEAR + 0.055) / 1.055) ** 2.4
)


def linear_to_srgb(linear):
    linear = numpy.clip(linear, 0.0, 1.0)
    return numpy.where(linear <= 0.0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - 0.055)


def find_zbrush(explicit=""):
    """Locate the ZBrush application (highest version wins; --zbrush overrides)."""
    if explicit:
        path = Path(explicit)
        return path if path.exists() else None
    candidates = []
    try:
        if sys.platform == "darwin":
            for folder in Path("/Applications").iterdir():
                if "zbrush" not in folder.name.lower():
                    continue
                if folder.suffix == ".app":
                    candidates.append(folder)
                elif folder.is_dir():
                    candidates += [app for app in folder.glob("*.app") if "zbrush" in app.name.lower()]
        elif sys.platform == "win32":
            programs = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            for pattern in ("ZBrush*", "Pixologic/ZBrush*", "Maxon*/ZBrush*"):
                for folder in programs.glob(pattern):
                    candidates += list(folder.glob("ZBrush.exe"))
    except OSError:
        pass
    return max(candidates, default=None)


def group_color(group_id):
    """Deterministic display color for a ZBrush polygroup id (the file has none)."""
    hue = (group_id * 0.618033988749895) % 1.0
    slot = int(hue * 6.0)
    f = hue * 6.0 - slot
    v, p, q, t = 0.9, 0.9 * 0.45, 0.9 * (1 - 0.55 * f), 0.9 * (1 - 0.55 * (1 - f))
    return [(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)][slot % 6]


class GoZ:
    """ZBrush's GoZ exchange: .GoZ files in a shared folder plus trigger utilities."""

    def __init__(self, root):
        self.root = Path(root)
        self.project = self.root / "GoZProjects" / "Default"
        self.object_list = self.root / "GoZBrush" / "GoZ_ObjectList.txt"
        self.written = {}  # GoZ path -> mtime of files this bridge wrote (skip echoes)
        try:
            self.list_seen = self.object_list.stat().st_mtime
        except OSError:
            self.list_seen = 0.0
        self.list_pending = None  # (mtime, deadline) of a change waiting to settle

    @staticmethod
    def default_root():
        if sys.platform == "darwin":
            return Path("/Users/Shared/Pixologic")
        if sys.platform == "win32":
            return Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Pixologic"
        return None

    def register(self):
        """List 'Nomad' among ZBrush's GoZ target applications and select it."""
        apps = self.root / "GoZApps" / "Nomad"
        apps.mkdir(parents=True, exist_ok=True)
        (apps / "GoZ_Info.txt").write_text(
            'NAME\t\t=\t"Nomad"\nGOZ_VERSION\t=\t1\nEXTENSION\t=\t".GoZ"\n'
            'TAMPLATE\t=\t"GoZ Complete Binary.GoZ"\n\n'
            "EXPORT_FLIP_Y\t=\tFALSE\nEXPORT_FLIP_Z\t=\tFALSE\n"
            "IMPORT_FLIP_Y\t=\tFALSE\nIMPORT_FLIP_Z\t=\tFALSE\n"
        )
        # the GoZ button launches this path; the bridge watches the files instead,
        # so a silent no-op keeps ZBrush happy
        trigger = "/usr/bin/true" if sys.platform == "darwin" else (
            os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "rundll32.exe")
        )
        (apps / "GoZ_Config.txt").write_text(f'PATH = "{trigger}"')
        (self.root / "GoZBrush").mkdir(parents=True, exist_ok=True)
        (self.root / "GoZBrush" / "GoZ_Application.txt").write_text("Nomad")
        (self.root / "GoZBrush" / "GoZ_ProjectPath.txt").write_text(f"{self.project}{os.sep}")

    def poke_zbrush(self):
        """Make ZBrush import GoZ_ObjectList.txt (starts ZBrush when needed)."""
        app = self.root / "GoZBrush" / (
            "GoZBrushFromApp.app" if sys.platform == "darwin" else "GoZBrushFromApp.exe"
        )
        try:
            if not app.exists():
                raise OSError("GoZ is not installed (ZBrush Preferences > GoZ)")
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-a", str(app)])
            else:
                subprocess.Popen([str(app)])
            return True
        except OSError as exc:
            print(f"could not run GoZBrushFromApp ({exc}); "
                  f"load the .GoZ files from {self.project} in ZBrush instead")
            return False

    def trigger_export(self, zbrush):
        """Press ZBrush's GoZ button from outside: a running ZBrush executes a
        ZScript passed as an open-file argument (same launch trick as GoB), so a
        fresh export lands in the shared folder for the watcher to forward."""
        if zbrush is None:
            return False
        script = self.root / "GoZApps" / "Nomad" / "nomad_get.txt"
        try:
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text("[IPress, Tool:GoZ]\n")
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-a", str(zbrush), str(script)])
            else:
                subprocess.Popen([str(zbrush), str(script)])
            return True
        except OSError as exc:
            print(f"could not run ZBrush ({exc})")
            return False

    def import_script(self, names):
        """ZScript landing every pushed mesh as a subtool of one tool (ZBrush's own
        multi-file GoZ import scatters a push across tools). Subtool titles are read
        once into subs: SubToolSelect repaints, a per-mesh rescan is quadratic."""
        prefix = "!:" if sys.platform == "darwin" else ""  # zscript mac path prefix
        script = [
            '[VarDef,sub,""]', '[VarDef,subs(2048),""]', "[VarDef,found,0]",
            "[VarDef,i,0]", "[VarDef,n,0]", "[VarDef,drawn,0]", "[VarDef,last,1]",
            "[VarSet,n,[SubToolGetCount]]",
            "[If,n>2048,", "[VarSet,n,2048]", "]",
            "[If,[ToolGetSubToolID]==0,", "[VarSet,n,0]", "]",  # no polymesh active
            "[VarSet,i,0]", "[Loop,n,", "[SubToolSelect,[Val,i]]",
            '[VarSet,sub,[IGetTitle,"Tool:ItemInfo"]]',
            "[VarSet,sub,[StrExtract,sub,0,[StrLength,sub]-2]]",  # trailing dot
            "[VarSet,subs(i),sub]", "[VarInc,i]", "]",
        ]
        for name in names:
            imp = [f'[FileNameSetNext,"{prefix}{self.project / name}.GoZ"]',
                   "[IPress,Tool:Import]"]
            script += [
                # found = the index of the subtool named like the mesh, or -1
                "[VarSet,found,-1]", "[VarSet,i,0]", "[Loop,n,",
                f'[If,([StrFind,subs(i),"{name}"]==0)&&([StrLength,subs(i)]=={len(name)}),',
                "[VarSet,found,[Val,i]]", "[LoopExit]", "]", "[VarInc,i]", "]",
                # known name: update the subtool in place
                "[If,found>-1,", "[SubToolSelect,[Val,found]]", *imp, "[VarSet,last,0]",
                # no polymesh active (fresh ZBrush): the import becomes the tool
                ",", "[If,[ToolGetSubToolID]==0,", *imp,
                "[CanvasClick,10,10,10,20]", "[IPress,Transform: Edit]", "[VarSet,drawn,1]",
                # append at the end so the scanned indices stay valid
                ",", "[If,last==0,", "[VarSet,i,[SubToolGetCount]-1]",
                "[SubToolSelect,[Val,i]]", "]",
                '[VarSet,sub,[IGetTitle,"Tool:ItemInfo"]]',
                '[If,[StrFind,"PolyMesh3D",sub]!=-1,',
                "[IPress,Tool:SubTool:Insert]", "[IPress,PopUp:Cube3D]",
                ",", "[IPress,Tool:SubTool:Insert]", "[IPress,PopUp:PolyMesh3D]", "]",
                *imp, "[VarSet,last,1]", "]", "]",
            ]
        script += ["[If,drawn==1,", "[IPress,Transform: Fit]", "]"]  # frame a fresh tool
        return "\n".join(script) + "\n"

    def trigger_import(self, zbrush, names):
        """Drive the import with a zscript so the push stays one tool; ZBrush's own
        importer (poke_zbrush) groups multi-mesh pushes unpredictably."""
        if zbrush is None:
            return False
        script = self.root / "GoZApps" / "Nomad" / "nomad_send.txt"
        try:
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text(self.import_script(names))
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-a", str(zbrush), str(script)])
            else:
                subprocess.Popen([str(zbrush), str(script)])
            return True
        except OSError as exc:
            print(f"could not run ZBrush ({exc})")
            return False

    def paths_from_list(self):
        try:
            lines = self.object_list.read_text().splitlines()
        except OSError:
            return []
        return [Path(line.strip() + ".GoZ") for line in lines if line.strip()]

    def push(self, meshes):
        """meshes: (name, header bytes already encoded by write_goz). Returns tool paths."""
        self.project.mkdir(parents=True, exist_ok=True)
        tools = []
        for name, blob in meshes:
            path = self.project / f"{name}.GoZ"
            path.write_bytes(blob)
            (self.project / f"{name}.ztn").write_text(str(self.project / name))
            self.written[str(path)] = path.stat().st_mtime
            tools.append(str(self.project / name))
        self.object_list.parent.mkdir(parents=True, exist_ok=True)
        self.object_list.write_text("".join(f"{tool}\n" for tool in tools))
        self.list_seen = self.object_list.stat().st_mtime
        return tools

    def poll_export(self, now):
        """Paths freshly exported by ZBrush's GoZ button, or None. Waits one extra
        poll after the object list changes so ZBrush finishes writing."""
        try:
            mtime = self.object_list.stat().st_mtime
        except OSError:
            return None
        if mtime != self.list_seen and self.list_pending is None:
            self.list_pending = (mtime, now + WATCH_POLL)
        if self.list_pending is None:
            return None
        pending, deadline = self.list_pending
        if mtime != pending:  # still being rewritten, restart the settle window
            self.list_pending = (mtime, now + WATCH_POLL)
            return None
        if now < deadline:
            return None
        self.list_pending = None
        self.list_seen = mtime
        fresh = []
        for path in self.paths_from_list():
            try:
                stat = path.stat()
            except OSError:
                continue
            if self.written.get(str(path)) == stat.st_mtime:
                continue  # our own pull, not a ZBrush export
            fresh.append(path)
        return fresh or None


def write_goz(name, positions, faces, corner_uv=None, polypaint=None, mask=None, groups=None):
    """Encode one mesh as a .GoZ blob. positions float32 (n,3) in GoZ space,
    faces int32 (f,4) with -1 marking triangles (0xFFFFFFFF on disk, same bits),
    corner_uv float32 (f,4,2), polypaint uint8 (n,4) B,G,R,0, mask uint16 (n),
    groups uint16 (f)."""
    def chunk(tag, count, payload):
        return struct.pack("<IIQ", tag, len(payload) + 16, count) + payload

    goz_name = b"GoZMesh_" + name.encode("ascii")
    blob = bytearray(b"GoZb 1.0 ZBrush GoZ Binary" + b"." * 6 + struct.pack("<I", 1))
    blob += struct.pack("<IQ", len(goz_name) + 16, 1) + goz_name
    blob += struct.pack("<IIQI", TAG_NAME_END, 20, 1, 0)
    blob += chunk(TAG_VERTICES, len(positions), positions.astype("<f4").tobytes())
    blob += chunk(TAG_FACES, len(faces), faces.astype("<i4").tobytes())
    if corner_uv is not None:
        blob += chunk(TAG_UVS, len(faces), corner_uv.astype("<f4").tobytes())
    if polypaint is not None:  # ZBrush's own layout: uint32 count + float32, not uint64
        payload = polypaint.astype("u1").tobytes()
        blob += struct.pack("<IIIf", TAG_POLYPAINT, len(payload) + 16, len(positions), 0.0) + payload
    if mask is not None:
        blob += chunk(TAG_MASK, len(positions), mask.astype("<u2").tobytes())
    if groups is not None:
        blob += chunk(TAG_POLYGROUPS, len(faces), groups.astype("<u2").tobytes())
    blob += bytes(16)
    return bytes(blob)


def read_goz(path):
    """Decode a .GoZ file into the write_goz arrays (missing sections are None)."""
    data = path.read_bytes()
    if not data.startswith(b"GoZb") or len(data) < 64:
        raise ValueError(f"not a GoZ file: {path}")
    name_size = struct.unpack_from("<I", data, 36)[0] - 16  # 8-byte GoZMesh_ prefix + name
    if name_size < 8 or 48 + name_size > len(data):
        raise ValueError(f"corrupt GoZ header: {path}")
    name = data[56 : 48 + name_size].decode("ascii", "replace")
    name = "".join(c for c in name if c.isprintable()).strip() or "ZBrushMesh"  # ZBrush pads with NULs
    mesh = {"name": name, "uv": None, "polypaint": None, "mask": None, "groups": None}
    offset = 48 + name_size
    while offset + 16 <= len(data):
        tag, size = struct.unpack_from("<II", data, offset)
        if tag == 0 or size < 16:
            break
        payload = data[offset + 16 : offset + size]  # size includes the 16 header bytes
        offset += size
        if tag == TAG_VERTICES:
            mesh["positions"] = numpy.frombuffer(payload, "<f4", len(payload) // 12 * 3).reshape(-1, 3)
        elif tag == TAG_FACES:
            mesh["faces"] = numpy.frombuffer(payload, "<i4", len(payload) // 16 * 4).reshape(-1, 4)
        elif tag == TAG_UVS:
            mesh["uv"] = numpy.frombuffer(payload, "<f4", len(payload) // 32 * 8).reshape(-1, 4, 2)
        elif tag == TAG_POLYPAINT:
            mesh["polypaint"] = numpy.frombuffer(payload, "u1", len(payload) // 4 * 4).reshape(-1, 4)
        elif tag == TAG_MASK:
            mesh["mask"] = numpy.frombuffer(payload, "<u2", len(payload) // 2)
        elif tag == TAG_POLYGROUPS:
            mesh["groups"] = numpy.frombuffer(payload, "<u2", len(payload) // 2)
    if "positions" not in mesh or "faces" not in mesh:
        raise ValueError(f"GoZ file without geometry: {path}")
    return mesh


def nomad_to_goz(header, binary, scale):
    """mesh_full packet -> write_goz arguments (world space, GoZ axes)."""
    vcount = int(header["vertex_count"])
    fcount = int(header["face_count"])
    positions = numpy.frombuffer(binary, "<f4", vcount * 3, int(header["position_offset"])).reshape(-1, 3)
    faces = numpy.frombuffer(binary, "<i4", fcount * 4, int(header["face_offset"])).reshape(-1, 4)
    matrix = numpy.array(header.get("world_matrix", IDENTITY), "f8").reshape(4, 4, order="F")
    positions = (positions @ matrix[:3, :3].T + matrix[:3, 3]) * AXIS * scale

    corner_uv = None
    if header.get("texcoord_count"):
        texcoords = numpy.frombuffer(
            binary, "<f4", int(header["texcoord_count"]) * 2, int(header["texcoord_offset"])
        ).reshape(-1, 2)
        face_uv = numpy.frombuffer(binary, "<i4", fcount * 4, int(header["face_uv_offset"])).reshape(-1, 4)
        corner_uv = numpy.zeros((fcount, 4, 2), "f4")
        corner_uv[:, :, 1] = 1.0  # ZBrush pads triangle corners with (0, 1)
        valid = face_uv >= 0
        gathered = texcoords[face_uv[valid]]
        gathered[:, 1] = 1.0 - gathered[:, 1]  # flip v
        corner_uv[valid] = gathered

    polypaint = None
    if "color_offset" in header:  # composited rgbm8 -> sRGB bytes as B,G,R,0
        rgbm = numpy.frombuffer(binary, "u1", vcount * 4, int(header["color_offset"])).reshape(-1, 4)
        linear = rgbm[:, :3].astype("f8") * (rgbm[:, 3:4].astype("f8") / 65025.0)
        srgb = numpy.rint(linear_to_srgb(linear) * 255.0).astype("u1")
        polypaint = numpy.zeros((vcount, 4), "u1")
        polypaint[:, :3] = srgb[:, ::-1]

    mask = None
    if "mask_offset" in header:  # both sides use uint16 with 65535 = unmasked
        mask = numpy.frombuffer(binary, "<u2", vcount, int(header["mask_offset"]))

    groups = None
    if "face_group_offset" in header:
        indices = numpy.frombuffer(binary, "<u2", fcount, int(header["face_group_offset"]))
        configs = header.get("face_groups", [])
        if len(numpy.unique(indices)) > 1:  # a single group everywhere = ungrouped
            ids, used = [], {UNGROUPED}
            for i in range(int(indices.max()) + 1):
                config = configs[i] if i < len(configs) else {}
                candidate = zlib.crc32(repr((config.get("name"), config.get("color"))).encode())
                while (candidate := 0x1111 + candidate % 0xEEBE) in used:
                    candidate += 977  # stable ids keep ZBrush's colors steady across pulls
                used.add(candidate)
                ids.append(candidate)
            groups = numpy.array(ids, "<u2")[indices]

    return positions.astype("<f4"), faces, corner_uv, polypaint, mask, groups


def goz_to_nomad(mesh, mesh_id, geometry_id, scale, request_id, matrix=None):
    """read_goz output -> (header, binary) mesh_full for Nomad. A matrix remembered
    from the pull keeps the object's transform: positions are un-baked through it."""
    world = numpy.array(matrix if matrix else IDENTITY, "f8").reshape(4, 4, order="F")
    try:
        inverse = numpy.linalg.inv(world)
    except numpy.linalg.LinAlgError:
        world = numpy.identity(4)
        inverse = world
    positions = mesh["positions"].astype("f8") * AXIS / scale
    positions = (positions @ inverse[:3, :3].T + inverse[:3, 3]).astype("<f4")
    faces = mesh["faces"]
    binary = bytearray(positions.tobytes())
    header = {
        "type": "mesh_full",
        "request_id": request_id,
        "mesh_id": mesh_id,
        "geometry_id": geometry_id,
        "name": mesh["name"],
        "vertex_count": len(positions),
        "face_count": len(faces),
        "position_offset": 0,
        "position_format": "float32x3",
        "face_offset": len(binary),
        "face_format": "int32x4",
        "coordinate_system": "nomad_y_up",
        "world_matrix": [float(v) for v in world.flatten(order="F")],
        # no smooth_shading: ZBrush has no such notion, the Nomad object keeps its setting
        "live_sync": False,
        "replace_topology": True,
    }
    binary += faces.astype("<i4").tobytes()

    if mesh["uv"] is not None and len(mesh["uv"]) == len(faces):
        corner_uv = mesh["uv"].astype("f4").copy()
        corner_uv[:, :, 1] = 1.0 - corner_uv[:, :, 1]  # flip v back
        valid = numpy.ones(faces.shape, bool)
        valid[:, 3] = faces[:, 3] >= 0  # ignore the padded 4th corner of triangles
        flat = numpy.ascontiguousarray(corner_uv.reshape(-1, 2)[valid.reshape(-1)])
        keys, first, inverse = numpy.unique(
            flat.view("<u8").reshape(-1), return_index=True, return_inverse=True
        )
        face_uv = numpy.full(faces.shape, -1, "<i4")
        face_uv[valid] = inverse
        header["texcoord_count"] = len(keys)
        header["texcoord_offset"] = len(binary)
        header["texcoord_format"] = "float32x2"
        binary += flat[first].astype("<f4").tobytes()
        header["face_uv_offset"] = len(binary)
        binary += face_uv.tobytes()

    if mesh["polypaint"] is not None and len(mesh["polypaint"]) == len(positions):
        linear = SRGB_TO_LINEAR[mesh["polypaint"][:, 2::-1]]  # B,G,R,0 -> linear rgb
        rgbm = numpy.full((len(positions), 4), 255, "u1")
        rgbm[:, :3] = numpy.rint(linear * 255.0).astype("u1")
        header["color_offset"] = len(binary)
        header["color_format"] = "rgbm8"
        binary += rgbm.tobytes()

    if mesh["mask"] is not None and len(mesh["mask"]) == len(positions):
        header["mask_offset"] = len(binary)
        header["mask_format"] = "uint16_norm"
        binary += mesh["mask"].astype("<u2").tobytes()

    if mesh["groups"] is not None and len(mesh["groups"]) == len(faces):
        ids, indices = numpy.unique(mesh["groups"], return_inverse=True)
        if len(ids) > 1 or int(ids[0]) != UNGROUPED:
            header["face_group_offset"] = len(binary)
            header["face_group_format"] = "uint16"
            binary += indices.astype("<u2").tobytes()
            header["face_groups"] = [
                {"name": "Ungrouped", "color": [0.7, 0.7, 0.7]} if group_id == UNGROUPED else
                {"name": f"Group {group_id}", "color": list(group_color(int(group_id)))}
                for group_id in ids
            ]

    header["binary_size"] = len(binary)
    return header, bytes(binary)


class Bridge:
    def __init__(self, nomad, goz, scale, zbrush=None):
        self.nomad = nomad
        self.goz = goz
        self.scale = scale
        self.zbrush = zbrush
        self.token = ""  # pairing grant, reused for silent reconnects
        self.links = {}  # ZBrush tool name -> {"mesh_id", "geometry_id"}
        self.pending = {}  # request_id -> tool name, to adopt Nomad's acked mesh_id
        self.collected = {}  # mesh_id -> (header, binary) awaiting the GoZ export
        self.collect_deadline = 0.0
        self.get_deadline = 0.0  # waiting for the scripted GoZ press to produce files
        self.requested_fulls = set()
        self.pairing_logged = False

    def link(self, name):
        return self.links.setdefault(
            name, {"mesh_id": uuid.uuid4().hex, "geometry_id": uuid.uuid4().hex}
        )

    def pull(self):
        print("requesting Nomad's selection...")
        self.nomad.send({"type": "request_selection", "request_id": uuid.uuid4().hex})

    def get(self):
        # Nomad's Get: press ZBrush's GoZ button remotely so the export is current,
        # not the last one; the watcher forwards the files once ZBrush wrote them
        if self.goz.trigger_export(self.zbrush):
            print("asking ZBrush for a fresh export...")
            self.get_deadline = time.monotonic() + GET_FRESH_TIMEOUT
        else:
            self.send()  # no ZBrush application: replay the last export

    def send(self, quiet=False):
        paths = self.goz.paths_from_list()
        if not paths:
            if not quiet:
                print("nothing to send: press GoZ in ZBrush first (or use pull)")
            return
        self.send_paths(paths, quiet)

    def send_paths(self, paths, quiet=False):
        sent = 0
        for path in paths:
            try:
                mesh = read_goz(path)
            except (OSError, ValueError, struct.error) as exc:
                print(f"skipping {path.name}: {exc}")
                continue
            link = self.link(mesh["name"])
            request = uuid.uuid4().hex
            self.pending[request] = mesh["name"]
            header, binary = goz_to_nomad(
                mesh, link["mesh_id"], link["geometry_id"], self.scale, request, link.get("matrix")
            )
            if not self.nomad.send(header, binary):
                self.pending.pop(request, None)
                print("nomad is offline; type send once it is back")
                return
            sent += 1
            if not quiet:
                print(f"sent {mesh['name']!r} to Nomad: "
                      f"{header['vertex_count']} vertices, {header['face_count']} faces")
        if not sent and not quiet:
            print("nothing to send: no readable GoZ files in the object list")

    def push_goz(self):
        meshes, used = [], set()
        for header, binary in self.collected.values():
            name = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_-]+", "_", header.get("name", "Mesh")))
            name = name.strip("_-") or "Mesh"
            while name in used:
                name += "_1"
            used.add(name)
            self.link(name).update(  # remember the pair so a GoZ round trip updates it
                {"mesh_id": header["mesh_id"], "geometry_id": header.get("geometry_id", uuid.uuid4().hex),
                 "matrix": list(header.get("world_matrix", IDENTITY))}
            )
            try:
                meshes.append((name, write_goz(name, *nomad_to_goz(header, binary, self.scale))))
            except (ValueError, KeyError) as exc:
                print(f"skipping {name!r}: {exc}")
        self.collected.clear()
        self.requested_fulls.clear()
        if not meshes:
            return
        self.goz.register()
        tools = self.goz.push(meshes)
        print(f"pushed to ZBrush: {', '.join(repr(Path(t).name) for t in tools)}")
        if not self.goz.trigger_import(self.zbrush, [name for name, _ in meshes]):
            self.goz.poke_zbrush()  # no ZBrush application: native import

    def handle_nomad(self, header, binary):
        kind = header.get("type")
        if kind == "hello":
            print(f"connected to Nomad {header.get('nomad_version', '?')}")
            granted = header.get("pair_token")
            if granted:
                self.token = granted  # reconnects skip the approval
                print(f"pair token (reuse with --token): {granted}")
        elif kind == "pairing_pending":
            if not self.pairing_logged:
                print("waiting for approval in Nomad's Link menu...")
                self.pairing_logged = True
        elif kind == "mesh_full" and not header.get("live_sync"):
            self.collected[header.get("mesh_id", uuid.uuid4().hex)] = (header, binary)
            self.collect_deadline = time.monotonic() + COLLECT_SILENCE
        elif kind == "mesh_instance" and not header.get("live_sync"):
            link = header.get("mesh_id", "")
            if link and link not in self.requested_fulls:  # ask for real geometry instead
                self.requested_fulls.add(link)
                self.nomad.send({"type": "request_mesh", "request_id": uuid.uuid4().hex, "link_id": link})
                self.collect_deadline = time.monotonic() + COLLECT_SILENCE
        elif kind == "mesh_ack":
            name = self.pending.pop(header.get("request_id", ""), None)
            if name and header.get("mesh_id"):
                self.links[name]["mesh_id"] = header["mesh_id"]
        elif kind == "request_mesh" and header.get("link_id"):
            names = [n for n, l in self.links.items() if l["mesh_id"] == header["link_id"]]
            paths = [p for n in names if (p := self.goz.project / f"{n}.GoZ").is_file()]
            self.send_paths(paths) if paths else self.send(quiet=True)
        elif kind in {"request_mesh", "request_selection", "request_scene"}:
            self.get()  # Nomad's Get button: fresh GoZ export when possible
        elif kind == "error":
            print(f"nomad error: {header.get('message', '')}")

    def idle(self):
        now = time.monotonic()
        if self.collected and now >= self.collect_deadline:
            self.push_goz()
        fresh = self.goz.poll_export(now)
        if fresh:
            self.get_deadline = 0.0
            print(f"ZBrush exported {len(fresh)} tool(s), forwarding to Nomad...")
            self.send_paths(fresh)
        elif self.get_deadline and now >= self.get_deadline:
            self.get_deadline = 0.0
            print("ZBrush did not export in time, resending the last known state")
            self.send(quiet=True)


def stdin_lines():
    """Terminal input as a queue; select() cannot watch stdin on Windows."""
    lines = queue.Queue()

    def reader():
        for line in sys.stdin:
            lines.put(line.strip().lower())
        lines.put("quit")  # EOF

    threading.Thread(target=reader, daemon=True).start()
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nomad", default="", help="Nomad host[:port] (default: discover)")
    parser.add_argument("--token", default="", help="Nomad pairing token")
    parser.add_argument("--port", type=int, default=48312, help="Nomad port for discovery")
    parser.add_argument("--goz", default="", help="GoZ folder (default: the Pixologic shared folder)")
    parser.add_argument("--zbrush", default="", help="ZBrush application, for fresh Get exports (default: search)")
    parser.add_argument("--scale", type=float, default=1.0, help="ZBrush units per Nomad unit")
    args = parser.parse_args()

    root = Path(args.goz) if args.goz else GoZ.default_root()
    if root is None:
        sys.exit("GoZ only exists on macOS and Windows; pass --goz to force a folder")
    if not root.is_dir():
        print(f"warning: {root} not found — install GoZ from ZBrush's Preferences > GoZ")
    goz = GoZ(root)

    zbrush = find_zbrush(args.zbrush)
    if zbrush:
        print(f"ZBrush for fresh Get exports: {zbrush}")
    else:
        print("ZBrush application not located: Get resends the last export (--zbrush to fix)")

    target = None
    if args.nomad:
        host, _, port = args.nomad.partition(":")
        target = (host, int(port) if port else args.port)
    else:
        print("searching for Nomad (broadcast + Bonjour)...")
        target = transport.discover(args.port, timeout=2.0)
        if target:
            print(f"found Nomad at {target[0]}:{target[1]}")
        else:
            print("no Nomad answered; retrying in the background (or pass --nomad)")

    connection = transport.Connection(client_name="ZBrush Bridge", capabilities=CAPABILITIES)
    bridge = Bridge(connection, goz, args.scale, zbrush)
    bridge.token = args.token
    if target:
        connection.connect(target[0], target[1], bridge.token, transport.VERSION, PROTOCOL)
    if root.is_dir():
        goz.register()

    print("commands: pull (Nomad selection -> ZBrush), get (fresh export -> Nomad), "
          "send (last export -> Nomad), quit\n"
          "the GoZ button in ZBrush sends to Nomad automatically")
    commands = stdin_lines()
    announced = target is not None  # suppress the first "waiting" line right after a failed discovery
    retry_at = 0.0
    try:
        while True:
            if connection.status in ("Error", "Disconnected"):
                now = time.monotonic()
                if announced:
                    announced = False
                    print(f"waiting for Nomad... ({connection.error or 'connection closed'})")
                if now >= retry_at:  # keep living: rediscover and redial until Nomad is back
                    retry_at = now + RECONNECT_RETRY
                    if not args.nomad:
                        found = transport.discover(args.port, timeout=1.0)
                        if found:
                            target = found
                    if target:
                        bridge.pairing_logged = False
                        connection.connect(target[0], target[1], bridge.token, transport.VERSION, PROTOCOL)
            elif not announced and connection.status == "Connected":
                announced = True  # the hello handler prints the reconnection
            for header, binary in connection.poll():
                bridge.handle_nomad(header, binary)
            bridge.idle()
            try:
                word = commands.get(timeout=0.05)
            except queue.Empty:
                continue
            if word in ("quit", "exit", "q"):
                return 0
            if word == "pull":
                bridge.pull()
            elif word == "get":
                bridge.get()
            elif word == "send":
                bridge.send()
            elif word:
                print("commands: pull, get, send, quit")
    except KeyboardInterrupt:
        return 0
    finally:
        connection.disconnect()


if __name__ == "__main__":
    sys.exit(main())
