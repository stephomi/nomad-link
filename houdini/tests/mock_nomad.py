# SPDX-License-Identifier: MIT
"""A stand-in for Nomad: accepts one client, answers hello, records the rest."""
import json
import socket
import struct
import threading
import time


class MockNomad(threading.Thread):
    def __init__(self, port, capabilities=("ngon", "scene_transfer", "selection_transfer")):
        threading.Thread.__init__(self, daemon=True)
        self.port = port
        self.capabilities = list(capabilities)
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", port))
        self.server.listen(1)
        self.received = []
        self.hello = None
        self.socket = None
        self.ready = threading.Event()

    def run(self):
        self.socket, _ = self.server.accept()
        buffer = bytearray()
        while True:
            try:
                data = self.socket.recv(1 << 16)
            except OSError:
                return
            if not data:
                return
            buffer.extend(data)
            while len(buffer) >= 8:
                json_size, binary_size = struct.unpack_from("!II", buffer)
                total = 8 + json_size + binary_size
                if len(buffer) < total:
                    break
                header = json.loads(bytes(buffer[8:8 + json_size]))
                binary = bytes(buffer[8 + json_size:total])
                del buffer[:total]
                if header.get("type") == "hello":
                    self.hello = header
                    self.send({"type": "hello", "protocol": 1, "nomad_version": "2.0",
                               "capabilities": self.capabilities, "pair_token": "token123"})
                    self.ready.set()
                elif header.get("type") != "ping":
                    self.received.append((header, binary))

    def send(self, header, binary=b""):
        payload = json.dumps(header).encode()
        self.socket.sendall(struct.pack("!II", len(payload), len(binary)) + payload + binary)

    def first(self, kind):
        return next(((h, b) for h, b in self.received if h.get("type") == kind), (None, None))


def wait(link, predicate, seconds=5.0):
    """Pump the link until the predicate holds (no Houdini event loop in tests)."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        link.pump()
        if predicate():
            return True
        time.sleep(0.02)
    return False
