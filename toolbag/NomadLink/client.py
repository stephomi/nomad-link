# SPDX-License-Identifier: MIT
"""One long-lived Nomad Link connection per Toolbag session.

The socket lives in transport.Connection's own thread; `pump` drains it from
Toolbag's onPeriodicUpdate callback, so every mset call happens on the main
thread. Geometry flows one way -- Toolbag renders, it does not sculpt -- so the
hello advertises only what this bridge really does.
"""
import json
import os
import time
import uuid

import convert
import scene as scene_module
import transport

PROTOCOL = 1
DEFAULT_PORT = 48312
CLIENT_NAME = "Marmoset Toolbag"
PING_INTERVAL = 10.0
RECONNECT_DELAY = 3.0

# honest hello (PROTOCOL.md §4): this bridge receives a scene and renders it.
# No selection/scene_transfer -- it never answers request_*, having nothing to
# send back.
CAPABILITIES = [
    "scene_edits",
    "light",
    "camera_object",
    "object_state",
    "material",
    "session_config",
    "mesh_delta_receive",
    "mesh_attributes_receive",
    "mesh_instance",
    "skew",              # world matrices are baked into positions, so skew is exact
    "shading_config",
    "texture",
    "asset",
]

_client = None


def client():
    global _client
    if _client is None:
        _client = Client()
    return _client


def _settings_path():
    """Address and pair tokens, remembered between Toolbag sessions."""
    return os.path.join(os.path.expanduser("~"), "nomad_link_toolbag.json")


def _load():
    try:
        with open(_settings_path()) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _store(data):
    try:
        with open(_settings_path(), "w") as handle:
            json.dump(data, handle, indent=1)
    except OSError:
        pass


def saved_address():
    """The last address used, for prefilling the panel."""
    data = _load()
    try:
        port = int(data.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    return str(data.get("host") or ""), port


def _save_address(host, port):
    data = _load()
    data["host"], data["port"] = host, int(port)
    _store(data)


def _tokens():
    return _load().get("tokens") or {}


def _save_token(host, token):
    data = _load()
    tokens = data.get("tokens") or {}
    tokens[host] = token
    data["tokens"] = tokens
    _store(data)


class Client:
    def __init__(self):
        self.connection = transport.Connection(CLIENT_NAME, CAPABILITIES)
        self.scene = scene_module.Scene(log=self.note)
        self.host = ""
        self.port = DEFAULT_PORT
        self.message = "Disconnected"
        self.nomad_version = ""
        self.peer_capabilities = set()
        self.session_config = {}
        self.config_revision = -1   # -1 = Nomad has not sent its settings yet
        self.meshes = {}          # link_id -> decoded mesh (convert.decode_mesh)
        self.lights = {}          # link_id -> merged light block (edits are partial)
        self.cameras = {}         # link_id -> merged camera_object block
        self.log = []
        self.counts = {"meshes": 0, "updates": 0}
        # where the time goes, measured rather than assumed (the Toolbag docs only
        # promise onPeriodicUpdate runs "a few times per second")
        self.stats = {"rate": 0.0, "busy": 0.0, "packets": 0,
                      "convert": 0.0, "write": 0.0}
        self._dirty = {}          # link_id -> pending geometry write, one per tick
        self._window = [0.0, 0, 0, 0.0]   # since, ticks, packets, busy
        self._materials = {}      # link_id -> last material block, for texture arrivals
        self._shading = None      # last shading block, for asset arrivals
        self._requested = set()
        self._wanted = False      # the user asked to be connected: keep retrying
        self._last_ping = 0.0
        self._last_try = 0.0

    # -------------------------------------------------------------- lifecycle

    @property
    def status(self):
        return self.connection.status

    @property
    def connected(self):
        return self.connection.status == "Connected"

    @property
    def wanted(self):
        """The user asked to be connected; still true while a retry is pending."""
        return self._wanted

    def find(self, port=DEFAULT_PORT):
        """Broadcast for Nomad and keep the address it answers from."""
        self.message = "Searching for Nomad..."
        found = transport.discover(port, timeout=2.0)
        if not found:
            self.message = "No Nomad answered; type an address"
            return False
        self.host, self.port = found[0], int(found[1])
        _save_address(self.host, self.port)
        self.message = "Found Nomad at %s:%d" % (self.host, self.port)
        return True

    def connect(self, host="", port=DEFAULT_PORT):
        self.disconnect()
        if not host and not self.find(port):
            return False
        self._wanted = True
        if host:
            self.host, self.port = host, int(port)
        _save_address(self.host, self.port)
        self._dial()
        return True

    def autoconnect(self):
        """Redial the address last used, without a blocking search."""
        host, port = saved_address()
        if not host:
            return False
        self.host, self.port = host, port
        self._wanted = True
        self._dial()
        return True

    def _dial(self):
        self._last_try = time.time()
        self.message = "Connecting to %s:%d..." % (self.host, self.port)
        self.connection.connect(self.host, self.port, _tokens().get(self.host, ""),
                                transport.VERSION, PROTOCOL)

    def disconnect(self):
        self._wanted = False
        self.connection.disconnect()
        self.peer_capabilities = set()
        self.session_config = {}
        self.config_revision = -1
        self.nomad_version = ""
        self.message = "Disconnected"

    def send(self, header, binary=b""):
        return self.connection.send(header, binary)

    def request(self, what="scene", replace=False):
        """Blender's Get / Replace all: ask Nomad to send it over, after dropping
        what it sent before when the whole scene is being replaced."""
        if replace:
            self.scene.clear_scene()
            self.meshes.clear()
            self.lights.clear()
            self.cameras.clear()
            self._materials.clear()
            self.counts["meshes"] = 0
        self._requested.clear()
        return self.send({"type": "request_%s" % what, "request_id": uuid.uuid4().hex})

    def set_sync_view(self, enabled):
        """The panel's follow checkbox is Nomad's shared `sync_view` (Blender calls
        it Working View), so it asks and the echoed config confirms. `sync_mode` has
        to travel back or Nomad refuses the whole message; the flags left out keep
        the value they already have."""
        enabled = bool(enabled)
        self.scene.follow_view = enabled
        if not self.connected or self.config_revision < 0:
            return False
        # PROTOCOL.md §5 wants the whole flag set back; the replica cannot be stale
        # behind it because base_revision would then be refused
        flags = {key: value for key, value in self.session_config.items()
                 if key == "live_sync" or key.startswith("sync_")}
        flags["sync_mode"] = self.session_config.get("sync_mode", "auto")
        flags["sync_view"] = enabled
        return self.send(dict(flags, type="set_session_config",
                              base_revision=self.config_revision))

    def note(self, text):
        self.log.append(text)
        del self.log[:-200]

    # --------------------------------------------------------------- main loop

    def pump(self):
        """Drain the socket queue. Main thread only."""
        started = time.time()
        packets = 0
        for header, binary in self.connection.poll():
            packets += 1
            self.scene.trace("message %s" % header.get("type"))
            try:
                self._handle(header, binary)
            except Exception as exc:  # one bad packet must not kill the callback
                self.note("error handling %s: %s" % (header.get("type"), exc))
        if self._dirty:
            self._flush_geometry()
        if packets:
            # a crash after this breadcrumb is Toolbag's own work on what it was
            # just given, not a call this bridge made
            self.scene.trace("queue drained")
        self._measure(started, packets)
        now = time.time()
        if self.connection.status == "Error":
            if self.message != self.connection.error:
                self.message = self.connection.error or "Connection lost"
            if self._wanted and now - self._last_try > RECONNECT_DELAY:
                self._dial()
        elif self.connected and now - self._last_ping > PING_INTERVAL:
            self._last_ping = now
            self.send({"type": "ping"})

    def _flush_geometry(self):
        """One write per mesh per tick. A stroke queues several deltas and only the
        state they add up to is ever seen; each write also makes Toolbag rebuild the
        mesh adjacency itself, so the saving is mostly its time, not ours."""
        convert_ms = write_ms = 0.0
        for link_id in self._dirty:
            mesh = self.meshes.get(link_id)
            if mesh is None:
                continue        # deleted before the tick ended
            start = time.time()
            built = self._build(mesh)
            middle = time.time()
            self.scene.update_geometry(link_id, built)
            self._refresh_group(link_id)
            convert_ms += (middle - start) * 1000.0
            write_ms += (time.time() - middle) * 1000.0
        self._dirty.clear()
        self.stats["convert"], self.stats["write"] = convert_ms, write_ms

    def _measure(self, started, packets):
        """How often Toolbag really calls back, and how much of that is us."""
        now = time.time()
        window = self._window
        if not window[0]:
            window[0] = started
        window[1] += 1
        window[2] += packets
        window[3] += now - started
        span = now - window[0]
        if span >= 1.0:
            self.stats.update(rate=window[1] / span, busy=window[3] / span,
                              packets=window[2])
            self._window = [now, 0, 0, 0.0]

    # ---------------------------------------------------------------- messages

    def _handle(self, header, binary):
        kind = header.get("type")
        handler = getattr(self, "_on_" + kind, None) if isinstance(kind, str) else None
        if handler is not None:
            handler(header, binary)

    def _on_hello(self, header, _binary):
        self.nomad_version = header.get("nomad_version", "?")
        self.peer_capabilities = set(header.get("capabilities", []))
        self.message = "Connected to Nomad %s" % self.nomad_version
        token = header.get("pair_token")
        if token:
            _save_token(self.host, token)

    def _on_pairing_pending(self, _header, _binary):
        self.message = "Waiting for approval in Nomad's Link menu"

    def _on_ping(self, _header, _binary):
        self.send({"type": "pong"})

    def _on_error(self, header, _binary):
        self.message = "Nomad: %s" % header.get("message", "error")
        self.note(self.message)
        self._requested.clear()

    def _on_session_config(self, header, _binary):
        revision = int(header.get("revision", -1))
        if revision < self.config_revision:
            return                  # an older echo overtaking the settings we hold
        self.config_revision = revision
        self.session_config = header
        # Nomad owns the view flag; the panel checkbox only shows what it says
        self.scene.follow_view = bool(header.get("sync_view", self.scene.follow_view))

    # -- geometry

    def _on_mesh_full(self, header, binary):
        mesh = convert.decode_mesh(header, binary)
        link_id = mesh["mesh_id"]
        self.meshes[link_id] = mesh
        self._requested.discard(link_id)
        if mesh.get("material"):
            self._materials[link_id] = mesh["material"]
        self.scene.apply_mesh(link_id, mesh, self._build(mesh))
        self._refresh_group(link_id)
        self._dirty.pop(link_id, None)      # a full states everything a delta asked for
        self.counts["meshes"] = len(self.meshes)
        if header.get("request_id"):
            self.send({"type": "mesh_ack", "mesh_id": link_id,
                       "request_id": header["request_id"]})

    def _on_mesh_instance(self, header, _binary):
        source = next((m for m in self.meshes.values()
                       if m["geometry_id"] == header.get("geometry_id")), None)
        if source is None:
            return self._recover(header.get("mesh_id", ""))
        link_id = header.get("mesh_id", "")
        mesh = dict(source)
        mesh["mesh_id"] = link_id
        mesh["name"] = header.get("name", source["name"])
        mesh["visible"] = bool(header.get("visible", True))
        mesh["world_matrix"] = list(header.get("world_matrix", convert.IDENTITY))
        self.meshes[link_id] = mesh
        self.scene.apply_mesh(link_id, mesh, self._build(mesh))
        self._dirty.pop(link_id, None)
        self.counts["meshes"] = len(self.meshes)

    def _on_mesh_delta(self, header, binary):
        """Patching the cached mesh is cheap; the write it earns waits for the end
        of the batch, so a stroke's worth of deltas costs Toolbag one rebuild."""
        link_id = header.get("mesh_id", "")
        mesh = self.meshes.get(link_id)
        if mesh is None or not convert.apply_delta(mesh, header, binary):
            return self._recover(link_id)
        self._dirty[link_id] = True
        self.counts["updates"] += 1

    def _on_mesh_attributes(self, header, binary):
        link_id = header.get("mesh_id", "")
        mesh = self.meshes.get(link_id)
        if mesh is None or not convert.apply_attributes(mesh, header, binary):
            return self._recover(link_id)
        self._dirty[link_id] = True

    def _on_light(self, header, _binary):
        link_id = header.get("link_id", "")
        state = self.lights.setdefault(link_id, {})
        state.update(header)
        self.scene.apply_light(link_id, state)

    def _on_camera_object(self, header, _binary):
        link_id = header.get("link_id", "")
        state = self.cameras.setdefault(link_id, {})
        state.update(header)
        self.scene.apply_camera_object(link_id, state)

    def _on_object_state(self, header, _binary):
        link_id = header.get("link_id", "")
        mesh = self.meshes.get(link_id)
        if mesh is None:
            for store, apply in ((self.lights, self.scene.apply_light),
                                 (self.cameras, self.scene.apply_camera_object)):
                state = store.get(link_id)
                if state is not None:
                    state.update(header)
                    apply(link_id, state)
            return
        if "name" in header:
            mesh["name"] = header["name"]
        if "visible" in header:
            mesh["visible"] = bool(header["visible"])
        self.scene.apply_object_state(link_id, header)
        if "world_matrix" in header:  # baked into the vertices, so rebuild them
            mesh["world_matrix"] = list(header["world_matrix"])
            self.scene.update_geometry(link_id, self._build(mesh))

    def _on_object_delete(self, header, _binary):
        link_id = header.get("link_id", "")
        self.meshes.pop(link_id, None)
        self.lights.pop(link_id, None)
        self.cameras.pop(link_id, None)
        self._materials.pop(link_id, None)
        self.scene.delete(link_id)
        self.counts["meshes"] = len(self.meshes)

    # -- looks

    def _on_material(self, header, _binary):
        link_id = header.get("mesh_id", "")
        block = header.get("material")
        if not block:
            return
        self._materials[link_id] = block
        mesh = self.meshes.get(link_id)
        self._request_textures(block)
        self.scene.apply_material(link_id, block, has_paint=bool(mesh and "color" in mesh),
                                  has_alpha=bool(mesh and "alpha" in mesh))

    def _on_texture(self, header, binary):
        blob_id = header.get("texture_id", "")
        if not self.scene.store_blob(blob_id, header.get("name"), binary):
            return
        for link_id, block in list(self._materials.items()):
            if any((t or {}).get("texture_id") == blob_id
                   for t in (block.get("textures") or {}).values()):
                mesh = self.meshes.get(link_id)
                self.scene.apply_material(link_id, block,
                                          has_paint=bool(mesh and "color" in mesh),
                                          has_alpha=bool(mesh and "alpha" in mesh))

    def _on_asset(self, header, binary):
        blob_id = header.get("asset_id", "")
        if self.scene.store_blob(blob_id, header.get("name"), binary) and self._shading:
            self.scene.apply_shading(self._shading)

    def _on_shading_config(self, header, _binary):
        shading = header.get("shading")
        if not shading:
            return
        self._shading = shading
        asset_id = shading.get("environment_id")
        if asset_id and asset_id not in self.scene.blobs:
            self.send({"type": "request_asset", "collection": "environments",
                       "asset_id": asset_id})
        self.scene.apply_shading(shading)

    def _on_camera(self, header, _binary):
        self.scene.apply_camera(header)

    # -- recovery

    def _request_textures(self, block):
        for texture in (block.get("textures") or {}).values():
            blob_id = (texture or {}).get("texture_id")
            if blob_id and blob_id not in self.scene.blobs and blob_id not in self._requested:
                self._requested.add(blob_id)
                self.send({"type": "request_texture", "texture_id": blob_id})

    def _wants_normals(self):
        """False once Toolbag has shown it computes its own (see Scene)."""
        return self.scene.needs_normals is not False

    def _build(self, mesh):
        return convert.build(mesh, self._wants_normals(), self.scene.unit_scale())

    def _refresh_group(self, link_id):
        """Instances share one geometry, so an edit to it redraws every copy."""
        source = self.meshes.get(link_id)
        if source is None:
            return
        for other_id, mesh in self.meshes.items():
            if other_id == link_id or mesh.get("geometry_id") != source.get("geometry_id"):
                continue
            own = {key: mesh[key] for key in ("mesh_id", "name", "visible", "world_matrix")}
            mesh.clear()
            mesh.update(source)
            mesh.update(own)
            self.scene.update_geometry(other_id, self._build(mesh))

    def _recover(self, link_id):
        if link_id and link_id not in self._requested:
            self._requested.add(link_id)
            self.send({"type": "request_mesh", "link_id": link_id,
                       "request_id": uuid.uuid4().hex})
