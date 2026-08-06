# SPDX-License-Identifier: MIT
"""Bridge Nomad Sculpt and CozyBlanket for a retopology round trip.

Usage: python3 cozyblanket.py [--nomad host[:port]] [--cb host] [options]

Connects to Nomad as a Link client (see PROTOCOL.md) and to
CozyBlanket through its remote protocol (sparseal.com/cozyblanket/network,
needs a CozyBlanket Standard license and an open document). Both apps are
discovered automatically when no host is given; CozyBlanket announces itself
by UDP broadcast, which iPads cannot receive, so run this on a computer or
pass --cb explicitly. Requires numpy.

Two operations, exposed as buttons inside CozyBlanket (remote actions), as
terminal commands, and through Nomad's own Blender Link menu:
  pull — Nomad's selection becomes CozyBlanket's retopology target
         (also triggered by Send in Nomad)
  send — CozyBlanket's edit mesh lands in Nomad as a linked mesh
         (also triggered by Get in Nomad)

Live extras (CozyBlanket's protocol is one-way here, so both follow CB):
  camera — toggle Nomad's viewport following CozyBlanket's camera (default on;
           needs "Sync view" enabled in Nomad)
  live   — toggle automatic `send` whenever the edit mesh changes (default on,
           --no-live starts it off; each update is one undo step in Nomad)
Type `pull`, `send`, `camera`, `live` or `quit` in the terminal, or tap the
buttons in CozyBlanket's interface.

The bridge keeps running when Nomad quits or the network drops, and
reconnects (or re-discovers) on its own; both apps may also be launched
after the script.
"""
import argparse
import queue
import select
import socket
import struct
import sys
import threading
import time
import uuid

try:
    import numpy
except ImportError:
    sys.exit("numpy is required: pip install numpy")

import transport

PROTOCOL = 1
TARGET_NAME = "Nomad"
COLLECT_SILENCE = 0.8  # selection transfers arrive as one packet per mesh; push after this quiet gap
CAMERA_RENEW = 5.0  # CozyBlanket streams its camera for a bounded window; renew it this often
LIVE_POLL = 1.0  # EDITMESH_CHANGES_CHECK cadence when live mode is on
RECONNECT_RETRY = 3.0  # pause between reconnection attempts when Nomad is gone
PIVOT_FALLBACK = 2.0  # orbit distance before any target or Nomad camera tells us better
PIVOT_MIN_DEPTH = 0.01  # keep the pivot in front of the eye: Nomad's ortho scale is its forward depth
IDENTITY = [1.0 if i % 5 == 0 else 0.0 for i in range(16)]
# honest hello: no scene_edits/object_state/material/light — this bridge drops live scene traffic;
# "camera" because the follow mode sends CozyBlanket's view to Nomad
CAPABILITIES = ["selection_transfer", "scene_transfer", "session_config", "camera"]


def pack_str(text):
    return struct.pack("<i", len(text)) + text.encode()


def read_exactly(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("CozyBlanket closed the connection")
        data.extend(chunk)
    return bytes(data)


class CozyBlanket:
    """Minimal client for CozyBlanket's remote protocol (one TCP command per call)."""

    def __init__(self, host, base_port=8606):
        self.host = host
        self.broadcast_port = base_port
        self.command_port = base_port + 1
        self.stream_port = base_port + 2  # UDP camera transforms
        self.report_port = base_port + 3  # remote-action registration

    @staticmethod
    def discover(base_port=8606, timeout=5.0):
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("", base_port))
            listener.settimeout(timeout)
            _, address = listener.recvfrom(4096)
            return address[0]
        except OSError:
            return None
        finally:
            listener.close()

    def command(self, name, data=b"", timeout=30.0):
        """Returns (ok, payload) with the trailing OK token stripped."""
        command = name.encode()
        frame = struct.pack("<ii", 4 + len(command) + len(data), len(command)) + command + data
        sock = socket.create_connection((self.host, self.command_port), timeout=5.0)
        try:
            sock.settimeout(timeout)
            sock.sendall(frame)
            size = struct.unpack("<i", read_exactly(sock, 4))[0]
            reply = read_exactly(sock, size)
        finally:
            sock.close()
        if reply[-2:] != b"OK":
            return False, reply
        return True, reply[:-2]

    def ping(self):
        try:
            return self.command("PING", timeout=2.0)[0]
        except OSError:
            return False

    def target_push(self, coords, indices, name=TARGET_NAME):
        """coords: flat float32 xyz; indices: flat int32 triangle list (CozyBlanket winding)."""
        data = pack_str(name) + struct.pack("<ii", len(coords) // 3, len(indices))
        data += coords.astype("<f4").tobytes() + indices.astype("<i4").tobytes()
        if not self.command("TARGET_PUSH", data)[0]:
            return False
        return self.command("TARGET_LOAD", pack_str(name))[0]

    def editmesh_pull(self):
        """Returns (positions Nx3 float32, polygon stream int32) or (None, None)."""
        ok, payload = self.command("EDITMESH_PULL")
        if not ok or len(payload) < 8:
            return None, None
        vcount = struct.unpack_from("<ii", payload)[0]
        positions = numpy.frombuffer(payload, "<f4", vcount * 3, 8).reshape(-1, 3)
        stream = numpy.frombuffer(payload, "<i4", (len(payload) - 8 - vcount * 12) // 4, 8 + vcount * 12)
        return positions, stream

    def notify(self, title, content):
        try:
            self.command("NOTIFICATION_SEND", pack_str(title) + pack_str(content), timeout=2.0)
        except OSError:
            pass

    def editmesh_changed(self):
        try:
            ok, payload = self.command("EDITMESH_CHANGES_CHECK", timeout=2.0)
        except OSError:
            return False
        return ok and len(payload) > 0 and payload[0] != 0

    def camera_stream_renew(self, seconds=10):
        try:
            return self.command("CAMERA_TRANSFORM_BROADCAST", struct.pack("<i", seconds), timeout=2.0)[0]
        except OSError:
            return False

    def open_camera_stream(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.stream_port))
        sock.setblocking(False)
        return sock


class RemoteActions:
    """Buttons inside CozyBlanket's interface: registered by name, fired into a queue."""

    def __init__(self, cb, client_name, actions):
        self.cb = cb
        self.client_name = client_name
        self.actions = actions
        self.fired = queue.Queue()
        self.stop = threading.Event()
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.port = cb.report_port + 1
        while True:
            try:
                self.server.bind(("", self.port))
                break
            except OSError:
                self.port += 1
        self.server.settimeout(0.2)
        self.server.listen()
        threading.Thread(target=self._serve, daemon=True).start()
        threading.Thread(target=self._report, daemon=True).start()

    def _serve(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                size = struct.unpack("<i", read_exactly(connection, 4))[0]
                data = read_exactly(connection, size)
                if data[-2:] == b"OK":
                    name_size = struct.unpack_from("<i", data)[0]
                    self.fired.put(data[4 : 4 + name_size].decode())
            except (OSError, struct.error):
                pass
            finally:
                connection.close()

    def _report(self):
        # CozyBlanket shows the actions while a client re-registers them regularly
        while not self.stop.is_set():
            try:
                sock = socket.create_connection((self.cb.host, self.cb.report_port), timeout=0.5)
                data = pack_str(self.client_name) + struct.pack("<ii", self.port, len(self.actions))
                for action in self.actions:
                    data += pack_str(action)
                sock.sendall(data)
                sock.close()
                self.stop.wait(3.0)
            except OSError:
                self.stop.wait(0.5)


def decode_nomad_mesh(header, binary):
    """mesh_full payload -> world-space positions and int32x4 faces."""
    vcount = int(header["vertex_count"])
    fcount = int(header["face_count"])
    positions = numpy.frombuffer(binary, "<f4", vcount * 3, int(header["position_offset"])).reshape(-1, 3)
    faces = numpy.frombuffer(binary, "<i4", fcount * 4, int(header["face_offset"])).reshape(-1, 4)
    matrix = numpy.array(header.get("world_matrix", IDENTITY), "f8").reshape(4, 4, order="F")
    positions = positions @ matrix[:3, :3].T + matrix[:3, 3]
    return positions.astype("<f4"), faces


def target_arrays(meshes):
    """Merge (positions, faces) pairs into CozyBlanket target arrays.

    CozyBlanket targets are flat triangle lists wound opposite to Nomad faces,
    matching the reference clients shipped by Sparseal.
    """
    coords, triangles, base = [], [], 0
    for positions, faces in meshes:
        quads = faces[:, 3] >= 0
        flipped = faces[:, (0, 2, 1)].copy()
        second = faces[quads][:, (0, 3, 2)]
        triangles.append(numpy.concatenate((flipped, second)) + base)
        coords.append(positions.reshape(-1))
        base += len(positions)
    return numpy.concatenate(coords), numpy.concatenate(triangles).reshape(-1).astype("<i4")


def stream_to_faces(stream):
    """CozyBlanket polygon stream -> Nomad int32x4 faces (fans past quads)."""
    faces = []
    it = 0
    while it < len(stream):
        count = int(stream[it])
        polygon = stream[it + 1 : it + 1 + count]
        it += 1 + count
        if count < 3 or len(polygon) < count:
            continue
        if count == 3:
            faces.append((polygon[0], polygon[1], polygon[2], -1))
        elif count == 4:
            faces.append(tuple(polygon))
        else:
            for corner in range(1, count - 1):
                faces.append((polygon[0], polygon[corner], polygon[corner + 1], -1))
    return numpy.array(faces, "<i4").reshape(-1, 4)


def encode_nomad_mesh(mesh_id, geometry_id, positions, faces, request_id):
    binary = positions.astype("<f4").tobytes() + faces.astype("<i4").tobytes()
    header = {
        "type": "mesh_full",
        "request_id": request_id,
        "mesh_id": mesh_id,
        "geometry_id": geometry_id,
        "name": "CozyBlanket",
        "vertex_count": len(positions),
        "face_count": len(faces),
        "position_offset": 0,
        "position_format": "float32x3",
        "face_offset": len(positions) * 12,
        "face_format": "int32x4",
        "binary_size": len(binary),
        "coordinate_system": "nomad_y_up",
        "world_matrix": IDENTITY,
        "smooth_shading": False,
        "live_sync": False,
        "replace_topology": True,
    }
    return header, binary


def camera_world_from_view(position, forward, up):
    """CozyBlanket (position, forward, up) -> Nomad column-major world_from_view.

    Both apps are y-up; Nomad cameras look along local -Z with +Y up.
    Returns None for degenerate transforms.
    """
    z = -forward / numpy.linalg.norm(forward)
    x = numpy.cross(up, z)
    length = numpy.linalg.norm(x)
    if length < 1e-6:
        return None
    x /= length
    y = numpy.cross(z, x)
    matrix = [0.0] * 16
    matrix[0:3] = x
    matrix[4:7] = y
    matrix[8:11] = z
    matrix[12:15] = position
    matrix[15] = 1.0
    return matrix


class Bridge:
    def __init__(self, nomad, cb):
        self.nomad = nomad
        self.cb = cb
        self.mesh_id = uuid.uuid4().hex  # replaced by Nomad's mesh_ack
        self.geometry_id = uuid.uuid4().hex
        self.token = ""  # pairing grant, reused for silent reconnects
        self.push_request = ""
        self.collected = {}  # mesh_id -> (header, binary) awaiting the CozyBlanket push
        self.collect_deadline = 0.0
        self.requested_fulls = set()
        self.pairing_logged = False
        try:
            self.camera_stream = cb.open_camera_stream()
            self.camera_follow = True
        except OSError as exc:
            self.camera_stream = None
            self.camera_follow = False
            print(f"camera follow unavailable (stream port busy?): {exc}")
        self.camera_renew_at = 0.0
        self.last_camera = None  # last CozyBlanket packet, to claim only on actual movement
        self.claimed = False
        self.pivot = None  # world-space orbit centre: the target's middle, or Nomad's own pivot
        self.live = True
        self.live_poll_at = 0.0
        self.resend_at = 0.0  # retry a push that Nomad bounced while mid-gesture

    def pull(self):
        print("requesting Nomad's selection...")
        self.nomad.send({"type": "request_selection", "request_id": uuid.uuid4().hex})

    def send(self, quiet=False):
        try:
            positions, stream = self.cb.editmesh_pull()
        except OSError as exc:
            print(f"CozyBlanket unreachable: {exc}")
            return
        if positions is None or not len(positions):
            if not quiet:
                print("nothing to send: no edit mesh in CozyBlanket (license/document?)")
            return
        faces = stream_to_faces(stream)
        if not len(faces):
            if not quiet:
                print("nothing to send: the edit mesh has no faces")
            return
        self.push_request = uuid.uuid4().hex
        header, binary = encode_nomad_mesh(
            self.mesh_id, self.geometry_id, positions, faces, self.push_request
        )
        self.nomad.send(header, binary)
        if not quiet:
            print(f"sent retopo to Nomad: {len(positions)} vertices, {len(faces)} faces")
            self.cb.notify("Nomad", f"Sent {len(positions)} vertices")

    def push_target(self):
        meshes = [decode_nomad_mesh(header, binary) for header, binary in self.collected.values()]
        names = ", ".join(repr(h.get("name", "?")) for h, _ in self.collected.values())
        self.collected.clear()
        self.requested_fulls.clear()
        coords, indices = target_arrays(meshes)
        points = coords.reshape(-1, 3).astype("f8")
        self.pivot = (points.min(0) + points.max(0)) * 0.5  # orbit what we are retopologising
        print(f"pushing target to CozyBlanket: {names} ({len(coords) // 3} vertices)")
        try:
            pushed = self.cb.target_push(coords, indices)
        except OSError as exc:
            print(f"CozyBlanket unreachable: {exc}")
            return
        if pushed:
            self.cb.notify("Nomad", f"Target updated: {len(coords) // 3} vertices")
        else:
            print("CozyBlanket refused the target (license/document?)")

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
            if header.get("request_id") == self.push_request:
                self.mesh_id = header.get("mesh_id", self.mesh_id)
        elif kind in {"request_mesh", "request_selection", "request_scene"}:
            self.send()  # Nomad's Get button asks for our scene: deliver the edit mesh
        elif kind == "session_config":
            if header.get("active_source") == "nomad":
                self.claimed = False  # Nomad's user took over; reclaim on the next CB movement
        elif kind == "camera":
            pivot = header.get("pivot")  # adopt Nomad's own orbit centre while its user drives
            if pivot:
                self.pivot = numpy.array(pivot, "f8")
        elif kind == "error":
            message = header.get("message", "")
            transient = "action" in message or "busy" in message
            if transient and header.get("request_id") == self.push_request:
                self.resend_at = time.monotonic() + 0.6  # Nomad was mid-gesture; try again
            else:
                print(f"nomad error: {message}")

    def follow_camera(self, now):
        if not self.camera_follow or self.camera_stream is None:
            return
        if now >= self.camera_renew_at:
            self.camera_renew_at = now + CAMERA_RENEW
            self.cb.camera_stream_renew()
        newest = None
        while True:
            try:
                packet, _ = self.camera_stream.recvfrom(64)
            except (BlockingIOError, OSError):
                break
            if len(packet) >= 36:
                newest = packet
        if newest is None:
            return
        values = numpy.frombuffer(newest, "<f4", 9)
        if self.last_camera is not None and numpy.allclose(values, self.last_camera, atol=1e-6):
            return
        self.last_camera = values.copy()
        position, forward, up = values[0:3], values[3:6], values[6:9]
        matrix = camera_world_from_view(position.astype("f8"), forward.astype("f8"), up.astype("f8"))
        if matrix is None:
            return
        if not self.claimed:  # CozyBlanket's user is navigating: claim authorship
            self.claimed = self.nomad.send({"type": "claim_sync", "source": "client"})
        # a fixed world pivot is what makes dollying read as zoom: Nomad derives its orbit
        # distance (and its orthographic scale) from eye-to-pivot, so it must not follow the eye
        eye = position.astype("f8")
        length = numpy.linalg.norm(forward)
        if length < 1e-6:
            return
        axis = forward.astype("f8") / length
        pivot = self.pivot if self.pivot is not None else eye + axis * PIVOT_FALLBACK
        depth = float(numpy.dot(pivot - eye, axis))
        if depth < PIVOT_MIN_DEPTH:  # dollied past the pivot: slide it forward, keep the offset
            pivot = pivot + axis * (PIVOT_MIN_DEPTH - depth)
        self.nomad.send({
            "type": "camera",
            "world_from_view": matrix,
            "pivot": list(pivot),
            "coordinate_system": "nomad_y_up",
            "live_sync": True,
        })

    def poll_live(self, now):
        if not self.live or now < self.live_poll_at:
            return
        self.live_poll_at = now + LIVE_POLL
        if self.cb.editmesh_changed():
            self.send(quiet=True)

    def idle(self):
        now = time.monotonic()
        if self.collected and now >= self.collect_deadline:
            self.push_target()
        if self.resend_at and now >= self.resend_at:
            self.resend_at = 0.0
            self.send(quiet=True)
        self.follow_camera(now)
        self.poll_live(now)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nomad", default="", help="Nomad host[:port] (default: discover)")
    parser.add_argument("--cb", default="", help="CozyBlanket host (default: discover)")
    parser.add_argument("--token", default="", help="Nomad pairing token")
    parser.add_argument("--port", type=int, default=48312, help="Nomad port for discovery")
    parser.add_argument("--cb-ports", type=int, default=8606, help="CozyBlanket base port")
    parser.add_argument("--no-live", action="store_true", help="start with auto-send off")
    args = parser.parse_args()

    nomad_target = None
    if args.nomad:
        host, _, port = args.nomad.partition(":")
        nomad_target = (host, int(port) if port else args.port)
    else:
        print("searching for Nomad (broadcast + Bonjour)...")
        nomad_target = transport.discover(args.port, timeout=2.0)
        if nomad_target:
            print(f"found Nomad at {nomad_target[0]}:{nomad_target[1]}")
        else:
            print("no Nomad answered; retrying in the background (or pass --nomad)")

    cb_host = args.cb
    if not cb_host:
        print("waiting for CozyBlanket's broadcast (open it with network features on, or pass --cb)...")
        try:
            while not cb_host:
                cb_host = CozyBlanket.discover(args.cb_ports)
        except KeyboardInterrupt:
            return 0
        print(f"found CozyBlanket at {cb_host}")
    cb = CozyBlanket(cb_host, args.cb_ports)
    if not cb.ping():
        print("warning: CozyBlanket did not answer PING; continuing anyway")

    connection = transport.Connection(client_name="CozyBlanket Bridge", capabilities=CAPABILITIES)
    bridge = Bridge(connection, cb)
    bridge.token = args.token
    bridge.live = not args.no_live
    if nomad_target:
        connection.connect(nomad_target[0], nomad_target[1], bridge.token, "1.0.0", PROTOCOL)
    actions = RemoteActions(cb, "Nomad", ["Pull sculpt", "Send retopo"])

    print(f"auto-send (live) is {'on' if bridge.live else 'off'}")
    print("commands: pull (selection -> target), send (edit mesh -> Nomad), "
          "camera (follow toggle), live (auto-send toggle), quit")
    announced = nomad_target is not None  # suppress the first "waiting" line right after a failed discovery
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
                            nomad_target = found
                    if nomad_target:
                        bridge.pairing_logged = False
                        bridge.claimed = False  # the fresh session needs a new claim
                        connection.connect(nomad_target[0], nomad_target[1], bridge.token, "1.0.0", PROTOCOL)
            elif not announced and connection.status == "Connected":
                announced = True  # the hello handler prints the reconnection
            for header, binary in connection.poll():
                bridge.handle_nomad(header, binary)
            while True:
                try:
                    fired = actions.fired.get_nowait()
                except queue.Empty:
                    break
                print(f"CozyBlanket button: {fired}")
                bridge.pull() if fired == "Pull sculpt" else bridge.send()
            bridge.idle()
            readable, _, _ = select.select([sys.stdin], [], [], 0.05)
            if readable:
                line = sys.stdin.readline()
                word = line.strip().lower()
                if not line or word in ("quit", "exit", "q"):
                    return 0
                if word == "pull":
                    bridge.pull()
                elif word == "send":
                    bridge.send()
                elif word == "camera":
                    bridge.camera_follow = not bridge.camera_follow and bridge.camera_stream is not None
                    print(f"camera follow {'on' if bridge.camera_follow else 'off'}")
                elif word == "live":
                    bridge.live = not bridge.live
                    print(f"live auto-send {'on' if bridge.live else 'off'}")
                elif word:
                    print("commands: pull, send, camera, live, quit")
    except KeyboardInterrupt:
        return 0
    finally:
        actions.stop.set()
        connection.disconnect()


if __name__ == "__main__":
    sys.exit(main())
