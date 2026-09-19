# SPDX-License-Identifier: MIT
"""Time a scene transfer with nothing but the socket in the way.

No Houdini, no USD, no caching -- it connects, asks for the scene, and prints
when each message arrives and how fast bytes are moving. If this is slow too,
the bridge is not the problem and the evidence is a twenty-line client.

    hython demo/probe.py 10.0.0.2          (or python3, if you have numpy-free deps)
    hython demo/probe.py 10.0.0.2 --minimal   advertise almost nothing
    hython demo/probe.py 10.0.0.2 --ping 10   send a keepalive every 10s

Ctrl-C to stop.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "python"))

import importlib  # noqa: E402

from nomad_link import transport  # noqa: E402

# the same token file the bridge uses, so this pairs silently like it does
tokens = importlib.import_module("nomad_link.client")

FULL = ["selection_transfer", "scene_transfer", "scene_edits", "object_state",
        "session_config", "mesh_delta_receive", "mesh_instance", "hierarchy",
        "scene_batch", "skew", "ngon", "material", "light", "camera_object",
        "texture", "shading_config"]
MINIMAL = ["scene_transfer", "object_state"]


def main():
    args = sys.argv[1:]
    host = args[0] if args and not args[0].startswith("-") else ""
    minimal = "--minimal" in args
    ping_every = float(args[args.index("--ping") + 1]) if "--ping" in args else 0.0
    # re-ask when the sender goes quiet: does a stalled transfer resume?
    retry_after = float(args[args.index("--retry") + 1]) if "--retry" in args else 0.0
    idle_stop = float(args[args.index("--idle") + 1]) if "--idle" in args else 20.0

    port = 48312
    if not host:
        print("searching for Nomad...")
        found = transport.discover(port, timeout=2.0)
        if not found:
            return "no Nomad answered; pass its address"
        host, port = found
    capabilities = MINIMAL if minimal else FULL
    print("connecting to %s:%d\ncapabilities: %s\nkeepalive: %s\n"
          % (host, port, ", ".join(capabilities),
             ("every %.1fs" % ping_every) if ping_every else "off"))

    token = tokens._load_tokens().get(host, "")
    link = transport.Connection("Probe", capabilities)
    link.connect(host, port, token, transport.VERSION, 1)

    started = time.monotonic()
    last = started
    last_ping = started
    counts = {}
    total = 0
    asked = False
    paired = False
    try:
        while True:
            time.sleep(0.01)
            now = time.monotonic()
            if link.status == "Error":
                return "connection failed: %s" % link.error
            for header, binary in link.poll():
                kind = header.get("type", "?")
                counts[kind] = counts.get(kind, 0) + 1
                total += len(binary)
                if kind == "hello":
                    paired = True
                    print("paired with Nomad %s" % header.get("nomad_version"))
                    if header.get("pair_token"):
                        tokens._save_token(host, header["pair_token"])
                elif kind == "pairing_pending":
                    print("waiting for approval in Nomad's Link menu...")
                name = header.get("name") or header.get("mesh_id", "")
                size = " %6.2f MB" % (len(binary) / 1048576.0) if binary else ""
                print("%7.2fs  +%6.3f  %-15s %-24s%s"
                      % (now - started, now - last, kind, str(name)[:24], size))
                last = now
            # only ask once Nomad has actually paired us: asking during
            # pairing_pending just earns an error
            if paired and not asked:
                asked = True
                print("\n--- asking for the scene ---")
                link.send({"type": "request_scene", "request_id": "probe"})
            if ping_every and now - last_ping > ping_every:
                last_ping = now
                link.send({"type": "ping"})
            if retry_after and asked and now - last > retry_after:
                last = now
                counts["retries"] = counts.get("retries", 0) + 1
                print("\n--- %.0fs quiet, asking again (%d) ---"
                      % (retry_after, counts["retries"]))
                link.send({"type": "request_scene", "request_id": "probe%d" % counts["retries"]})
                continue
            if asked and now - last > idle_stop:
                break
    except KeyboardInterrupt:
        pass
    finally:
        elapsed = time.monotonic() - started
        print("\n%.1f MB in %.1fs (%.2f MB/s)"
              % (total / 1048576.0, elapsed, total / 1048576.0 / max(elapsed, 0.01)))
        print("messages: %s" % ", ".join("%s x%d" % (k, v) for k, v in sorted(counts.items())))
        link.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
