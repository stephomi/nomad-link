# SPDX-License-Identifier: MIT
"""One long-lived Nomad Link connection per Houdini session.

The socket lives in transport.Connection's own thread; everything else runs on
Houdini's main thread from an event loop callback, so SOP cooks only ever touch
the decoded mesh cache -- never the network.
"""
import json
import os
import time
import uuid

from . import convert, transport

PROTOCOL = 1
DEFAULT_PORT = 48312
CLIENT_NAME = "Houdini"
PING_INTERVAL = 10.0

# honest hello: we receive geometry and object state and we send mesh_full.
# No material/light/camera handling, so those are not advertised.
CAPABILITIES = [
    "selection_transfer",
    "scene_transfer",
    "scene_edits",
    "object_state",
    "session_config",
    "mesh_delta_receive",
    "mesh_instance",
    "ngon",
]

_client = None


def client():
    global _client
    if _client is None:
        _client = Client()
    return _client


def _token_path():
    try:
        import hou
        base = hou.homeHoudiniDirectory()
    except Exception:
        base = os.path.expanduser("~")
    return os.path.join(base, "nomad_link_tokens.json")


def _load_tokens():
    try:
        with open(_token_path()) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _save_token(host, token):
    tokens = _load_tokens()
    tokens[host] = token
    try:
        with open(_token_path(), "w") as handle:
            json.dump(tokens, handle, indent=1)
    except OSError:
        pass


class Client:
    def __init__(self):
        self.connection = transport.Connection(CLIENT_NAME, CAPABILITIES)
        self.host = ""
        self.port = DEFAULT_PORT
        self.message = "Disconnected"
        self.nomad_version = ""
        self.peer_capabilities = set()
        self.session_config = {}
        self.meshes = {}          # mesh_id -> decoded mesh dict (convert.decode_mesh)
        self.order = []           # arrival order, for the node menus
        self.revision = 0         # bumped whenever the cache changes
        self.log = []
        self._pending_acks = {}   # request_id -> node path waiting for its mesh_id
        self._requested = set()   # mesh_ids we already asked a mesh_full for
        self._callback = None
        self._last_ping = 0.0

    # ------------------------------------------------------------- lifecycle

    @property
    def status(self):
        return self.connection.status

    @property
    def connected(self):
        return self.connection.status == "Connected"

    def peer_has(self, capability):
        return capability in self.peer_capabilities

    def connect(self, host="", port=DEFAULT_PORT):
        self.disconnect()
        if not host:
            self.message = "Searching for Nomad..."
            found = transport.discover(port, timeout=2.0)
            if not found:
                self.message = "No Nomad answered the discovery broadcast"
                return False
            host, port = found
        self.host, self.port = host, int(port)
        self.message = "Connecting to %s:%d..." % (self.host, self.port)
        self.connection.connect(
            self.host, self.port, _load_tokens().get(self.host, ""),
            transport.VERSION, PROTOCOL,
        )
        self._install_pump()
        return True

    def disconnect(self):
        self.connection.disconnect()
        self._remove_pump()
        self.peer_capabilities = set()
        self.nomad_version = ""
        self.message = "Disconnected"

    def send(self, header, binary=b""):
        return self.connection.send(header, binary)

    def request(self, kind, link_id=""):
        header = {"type": kind, "request_id": uuid.uuid4().hex}
        if link_id:
            header["link_id"] = link_id
        return self.send(header)

    # ------------------------------------------------------------- main loop

    def _install_pump(self):
        try:
            import hou
            if not hou.isUIAvailable() or self._callback is not None:
                return
        except ImportError:
            return
        self._callback = self.pump
        hou.ui.addEventLoopCallback(self._callback)

    def _remove_pump(self):
        if self._callback is None:
            return
        try:
            import hou
            hou.ui.removeEventLoopCallback(self._callback)
        except Exception:
            pass
        self._callback = None

    def pump(self):
        """Drain the socket queue. Main thread only (event loop or hython loop)."""
        before = self.revision
        for header, binary in self.connection.poll():
            try:
                self._handle(header, binary)
            except Exception as exc:  # never let one bad packet kill the callback
                self.note("error handling %s: %s" % (header.get("type"), exc))
        if self.connection.status == "Error" and self.message != self.connection.error:
            self.message = self.connection.error or "Connection lost"
        if self.connected and time.time() - self._last_ping > PING_INTERVAL:
            self._last_ping = time.time()
            self.send({"type": "ping"})
        if self.revision != before:
            self._dirty_nodes()

    def note(self, text):
        self.log.append(text)
        del self.log[:-200]

    def _touch(self):
        self.revision += 1

    def _dirty_nodes(self):
        nodes = self._nodes()
        if nodes is not None:
            nodes.refresh_inputs(self.revision)

    # -------------------------------------------------------------- messages

    def _handle(self, header, binary):
        kind = header.get("type")
        if kind == "hello":
            self.nomad_version = header.get("nomad_version", "?")
            self.peer_capabilities = set(header.get("capabilities", []))
            self.message = "Connected to Nomad %s" % self.nomad_version
            token = header.get("pair_token")
            if token:
                _save_token(self.host, token)
        elif kind == "pairing_pending":
            self.message = "Waiting for approval in Nomad's Link menu"
        elif kind == "error":
            self.message = "Nomad: %s" % header.get("message", "error")
            self.note(self.message)
            self._requested.clear()
        elif kind == "mesh_full":
            mesh = convert.decode_mesh(header, binary)
            self._store(mesh)
        elif kind == "mesh_instance":
            self._instance(header)
        elif kind == "mesh_delta":
            mesh = self.meshes.get(header.get("mesh_id"))
            if mesh is None or not convert.apply_delta(mesh, header, binary):
                self._recover(header.get("mesh_id", ""))
            else:
                self._touch()
        elif kind == "mesh_attributes":
            self._recover(header.get("mesh_id", ""))  # cheaper to just refetch
        elif kind == "object_state":
            mesh = self.meshes.get(header.get("link_id"))
            if mesh is not None:
                mesh["name"] = header.get("name", mesh["name"])
                mesh["visible"] = bool(header.get("visible", mesh.get("visible", True)))
                if "world_matrix" in header:
                    mesh["world_matrix"] = list(header["world_matrix"])
                self._touch()
        elif kind == "object_delete":
            if self.meshes.pop(header.get("link_id"), None) is not None:
                self.order = [i for i in self.order if i in self.meshes]
                self._touch()
        elif kind == "mesh_ack":
            path = self._pending_acks.pop(header.get("request_id", ""), None)
            if path and self._nodes():
                self._nodes().store_mesh_id(path, header.get("mesh_id", ""))
        elif kind == "session_config":
            self.session_config = header
        elif kind in ("request_mesh", "request_selection", "request_scene"):
            if self._nodes():
                self._nodes().answer_request(header)

    @staticmethod
    def _nodes():
        """The Houdini-facing module, or None when running headless."""
        try:
            from . import nodes
        except ImportError:
            return None
        return nodes

    def _store(self, mesh):
        mesh_id = mesh["mesh_id"]
        previous = self.meshes.get(mesh_id)
        if previous is None:
            self.order.append(mesh_id)
        if "visible" not in mesh:  # absent = leave as-is, a new object is visible
            mesh["visible"] = previous.get("visible", True) if previous else True
        self.meshes[mesh_id] = mesh
        self._requested.discard(mesh_id)
        self._touch()

    def _instance(self, header):
        """Share an already-known geometry under a second mesh_id."""
        source = next(
            (m for m in self.meshes.values() if m["geometry_id"] == header.get("geometry_id")),
            None,
        )
        if source is None:
            self._recover(header.get("mesh_id", ""))
            return
        mesh = dict(source)
        mesh.pop("visible", None)  # the shared geometry's flag is not this instance's
        mesh["mesh_id"] = header.get("mesh_id", "")
        mesh["name"] = header.get("name", source["name"])
        if "visible" in header:
            mesh["visible"] = bool(header["visible"])
        mesh["world_matrix"] = list(header.get("world_matrix", convert.IDENTITY))
        self._store(mesh)

    def _recover(self, mesh_id):
        if mesh_id and mesh_id not in self._requested:
            self._requested.add(mesh_id)
            self.request("request_mesh", mesh_id)

    # ----------------------------------------------------------- outgoing geo

    def send_mesh(self, header, binary, node_path=""):
        if node_path:
            self._pending_acks[header.get("request_id", "")] = node_path
        return self.send(header, binary)
