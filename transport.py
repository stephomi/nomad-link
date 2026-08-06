# SPDX-License-Identifier: MIT

import base64
import hashlib
import json
import queue
import select
import socket
import struct
import threading
import time


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DISCOVERY = b"NOMAD_LINK_DISCOVER 1"
MAX_JSON = 1 << 20
MAX_BINARY = 1 << 30
MDNS_GROUP = ("224.0.0.251", 5353)
MDNS_SERVICE = b"_nomadlink._tcp.local"


def mdns_query():
    labels = b"".join(bytes((len(part),)) + part for part in MDNS_SERVICE.split(b"."))
    # PTR question with the QU bit set, so Nomad replies unicast to this socket
    return struct.pack("!6H", 0, 0, 1, 0, 0, 0) + labels + b"\x00" + struct.pack("!HH", 12, 0x8001)


def mdns_skip_name(data, offset):
    while offset < len(data):
        length = data[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0:
            return offset + 2
        offset += 1 + length
    raise ValueError("Truncated mDNS name")


def mdns_port(data):
    """SRV port carried by an mDNS reply, or None."""
    try:
        questions, answers = struct.unpack("!HH", data[4:8])
        extra = sum(struct.unpack("!HH", data[8:12]))
        offset = 12
        for _ in range(questions):
            offset = mdns_skip_name(data, offset) + 4
        for _ in range(answers + extra):
            offset = mdns_skip_name(data, offset)
            record_type, _class, _ttl, size = struct.unpack_from("!HHIH", data, offset)
            offset += 10
            if record_type == 33 and size >= 6:  # SRV
                return struct.unpack_from("!H", data, offset + 4)[0]
            offset += size
    except (ValueError, IndexError, struct.error):
        pass
    return None


def discover(port, timeout=1.0):
    """Find Nomad through UDP broadcast and Bonjour/mDNS; first reply wins."""
    broadcast = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    mdns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        broadcast.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        broadcast.bind(("", 0))
        mdns.bind(("", 0))
        for sock, payload, target in (
            (broadcast, DISCOVERY, ("255.255.255.255", port)),
            (mdns, mdns_query(), MDNS_GROUP),
        ):
            try:
                sock.sendto(payload, target)
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            readable, _, _ = select.select([broadcast, mdns], [], [], remaining)
            for sock in readable:
                data, address = sock.recvfrom(4096)
                if sock is mdns:
                    found = mdns_port(data)
                    if found:
                        return address[0], found
                    continue
                try:
                    reply = json.loads(data.decode("utf-8"))
                except ValueError:
                    continue
                if reply.get("type") == "nomad_link":
                    return address[0], int(reply.get("port", port))
    finally:
        broadcast.close()
        mdns.close()


DEFAULT_CAPABILITIES = [
    "selection_transfer",
    "scene_transfer",
    "scene_edits",
    "object_state",
    "material",
    "light",
    "camera_object",
    "camera",
    "session_config",
]


class Connection:
    def __init__(self, client_name, capabilities=None):
        self.client_name = client_name
        self.capabilities = list(DEFAULT_CAPABILITIES if capabilities is None else capabilities)
        self.incoming = queue.Queue()
        self.outgoing = queue.Queue()
        self.status = "Disconnected"
        self.error = ""
        self.peer = ""
        self._socket = None
        self._thread = None
        self._stop = threading.Event()

    def connect(self, host, port, pair_token, version, protocol):
        self.disconnect()
        self.status = "Connecting"
        self.error = ""
        self.peer = f"{host}:{port}"
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(host, port, pair_token, version, protocol),
            name="Nomad Link",
            daemon=True,
        )
        self._thread.start()

    def listen(self, port, pair_token, version, protocol):
        # Nomad Web cannot dial raw TCP: accept its WebSocket instead. The protocol and the
        # client role stay identical, only the transport differs (one WS message per frame).
        self.disconnect()
        self.status = "Listening"
        self.error = ""
        self.peer = ""
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_listen,
            args=(port, pair_token, version, protocol),
            name="Nomad Link WS",
            daemon=True,
        )
        self._thread.start()

    def disconnect(self):
        self._stop.set()
        sock = self._socket
        self._socket = None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._thread = None
        for pending in (self.incoming, self.outgoing):
            while True:
                try:
                    pending.get_nowait()
                except queue.Empty:
                    break
        self.status = "Disconnected"

    def send(self, header, binary=b""):
        if self.status != "Connected":
            return False
        self.outgoing.put((header, binary))
        return True

    def poll(self):
        packets = []
        while True:
            try:
                packets.append(self.incoming.get_nowait())
            except queue.Empty:
                return packets

    @staticmethod
    def _frame(header, binary):
        encoded = json.dumps(header, separators=(",", ":")).encode("utf-8")
        return struct.pack("!II", len(encoded), len(binary)) + encoded + binary

    @staticmethod
    def _pop_frames(buffer):
        packets = []
        while len(buffer) >= 8:
            json_size, binary_size = struct.unpack_from("!II", buffer)
            if json_size > MAX_JSON or binary_size > MAX_BINARY:
                raise ValueError("Nomad packet is too large")
            total = 8 + json_size + binary_size
            if len(buffer) < total:
                break
            header = json.loads(bytes(buffer[8 : 8 + json_size]).decode("utf-8"))
            binary = bytes(buffer[8 + json_size : total])
            del buffer[:total]
            packets.append((header, binary))
        return packets

    def _run(self, host, port, pair_token, version, protocol):
        buffer = bytearray()
        pending = bytearray()
        try:
            sock = socket.create_connection((host, port), timeout=5.0)
            sock.setblocking(False)
            self._socket = sock
            self.status = "Connected"
            self.outgoing.put(
                (
                    {
                        "type": "hello",
                        "protocol": protocol,
                        "pair_token": pair_token,
                        "bridge_version": version,
                        "client_name": self.client_name,
                        "capabilities": self.capabilities,
                    },
                    b"",
                )
            )

            while not self._stop.is_set():
                reads = [sock]
                writes = [sock] if pending or not self.outgoing.empty() else []
                readable, writable, _ = select.select(reads, writes, [], 0.1)
                if readable:
                    data = sock.recv(1 << 18)
                    if not data:
                        raise ConnectionError("Nomad closed the connection")
                    buffer.extend(data)
                    for packet in self._pop_frames(buffer):
                        self.incoming.put(packet)
                if writable:
                    if not pending:
                        header, binary = self.outgoing.get_nowait()
                        pending.extend(self._frame(header, binary))
                    sent = sock.send(pending)
                    del pending[:sent]
        except Exception as exc:
            if not self._stop.is_set():
                self.error = str(exc)
                self.status = "Error"
        finally:
            sock = self._socket
            self._socket = None
            if sock:
                sock.close()
            if self.status != "Error":
                self.status = "Disconnected"

    # ---- WebSocket listener (Nomad Web) ----

    @staticmethod
    def _ws_frame(payload, opcode=2):
        length = len(payload)
        head = bytearray([0x80 | opcode])
        if length < 126:
            head.append(length)
        elif length < (1 << 16):
            head.append(126)
            head += length.to_bytes(2, "big")
        else:
            head.append(127)
            head += length.to_bytes(8, "big")
        return bytes(head) + payload

    @staticmethod
    def _pop_ws_frames(ws, message):
        # yields (opcode, payload) for complete messages; data frames assemble in `message`
        packets = []
        while True:
            if len(ws) < 2:
                return packets
            b0, b1 = ws[0], ws[1]
            length = b1 & 0x7F
            offset = 2
            if length == 126:
                if len(ws) < 4:
                    return packets
                length = int.from_bytes(ws[2:4], "big")
                offset = 4
            elif length == 127:
                if len(ws) < 10:
                    return packets
                length = int.from_bytes(ws[2:10], "big")
                offset = 10
            if length > MAX_BINARY:
                raise ValueError("WebSocket message is too large")
            mask = b""
            if b1 & 0x80:
                if len(ws) < offset + 4:
                    return packets
                mask = bytes(ws[offset : offset + 4])
                offset += 4
            if len(ws) < offset + length:
                return packets
            payload = bytes(ws[offset : offset + length])
            del ws[: offset + length]
            if mask and length:
                key = (mask * (length // 4 + 1))[:length]
                payload = (
                    int.from_bytes(payload, "little") ^ int.from_bytes(key, "little")
                ).to_bytes(length, "little")
            opcode = b0 & 0x0F
            if opcode in (0, 1, 2):
                message.extend(payload)
                if b0 & 0x80:
                    packets.append((2, bytes(message)))
                    message.clear()
            else:
                packets.append((opcode, payload))

    def _run_listen(self, port, pair_token, version, protocol):
        server = None
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("", port))
            server.listen(1)
            server.settimeout(0.2)
            while not self._stop.is_set():
                self.status = "Listening"
                self.peer = ""
                try:
                    sock, address = server.accept()
                except socket.timeout:
                    continue
                try:
                    self._serve_websocket(sock, address, pair_token, version, protocol)
                except Exception as exc:
                    if not self._stop.is_set():
                        self.error = str(exc)  # shown, but keep listening for the next page
                finally:
                    self._socket = None
                    sock.close()
                    for pending in (self.incoming, self.outgoing):
                        while True:
                            try:
                                pending.get_nowait()
                            except queue.Empty:
                                break
        except Exception as exc:
            if not self._stop.is_set():
                self.error = str(exc)
                self.status = "Error"
        finally:
            if server:
                server.close()
            if self.status != "Error":
                self.status = "Disconnected"

    def _serve_websocket(self, sock, address, pair_token, version, protocol):
        sock.settimeout(5.0)
        request = b""
        while b"\r\n\r\n" not in request:
            data = sock.recv(4096)
            if not data:
                raise ConnectionError("Browser closed during the handshake")
            request += data
            if len(request) > 16384:
                raise ValueError("Invalid WebSocket handshake")
        head, _, extra = request.partition(b"\r\n\r\n")
        key = ""
        for line in head.decode("latin-1").split("\r\n")[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-key":
                key = value.strip()
        if not key:
            raise ValueError("Not a WebSocket connection")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        sock.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode()
        )

        self._socket = sock
        self.peer = f"{address[0]}:{address[1]}"
        self.status = "Connected"
        sock.setblocking(False)
        self.outgoing.put(
            (
                {
                    "type": "hello",
                    "protocol": protocol,
                    "pair_token": pair_token,
                    "bridge_version": version,
                    "client_name": self.client_name,
                    "capabilities": self.capabilities,
                },
                b"",
            )
        )

        ws = bytearray(extra)
        message = bytearray()
        buffer = bytearray()
        pending = bytearray()
        while not self._stop.is_set():
            reads = [sock]
            writes = [sock] if pending or not self.outgoing.empty() else []
            readable, writable, _ = select.select(reads, writes, [], 0.1)
            if readable:
                data = sock.recv(1 << 18)
                if not data:
                    return  # page closed: back to listening
                ws.extend(data)
                for opcode, payload in self._pop_ws_frames(ws, message):
                    if opcode == 8:
                        return  # clean close: back to listening
                    if opcode == 9:
                        pending.extend(self._ws_frame(payload, opcode=10))
                        continue
                    if opcode != 2:
                        continue
                    buffer.extend(payload)
                    for packet in self._pop_frames(buffer):
                        self.incoming.put(packet)
            if writable:
                if not pending:
                    header, binary = self.outgoing.get_nowait()
                    pending.extend(self._ws_frame(self._frame(header, binary)))
                sent = sock.send(pending)
                del pending[:sent]
