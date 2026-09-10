# SPDX-License-Identifier: MIT
"""Dump the transforms Nomad sends for a parented scene, and check them.

Answers three questions from real data rather than reasoning:
  - is world_matrix = world_matrix_parent x local_matrix, as section 3 says?
  - does world_matrix_parent match the parent object's own world_matrix?
  - what would we author, and does the child end up where Nomad says?

    hython demo/matrices.py 10.0.0.2
"""
import importlib
import os
import sys
import time

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "python"))

from nomad_link import convert, transport  # noqa: E402

tokens = importlib.import_module("nomad_link.client")
CAPABILITIES = ["scene_transfer", "object_state", "session_config", "mesh_instance",
                "hierarchy", "scene_batch", "skew", "ngon", "material"]


def m(values):
    return numpy.array(values, numpy.float64).reshape(4, 4, order="F")


def scale_of(values):
    matrix = m(values)
    return [round(float(numpy.linalg.norm(matrix[:3, i])), 4) for i in range(3)]


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else ""
    port = 48312
    if not host:
        found = transport.discover(port, timeout=2.0)
        if not found:
            return "no Nomad answered"
        host, port = found

    link = transport.Connection("Matrices", CAPABILITIES)
    link.connect(host, port, tokens._load_tokens().get(host, ""), transport.VERSION, 1)

    objects = {}
    order = []
    started = time.monotonic()
    asked = False
    last = started
    while True:
        time.sleep(0.01)
        now = time.monotonic()
        if link.status == "Error":
            return "connection failed: %s" % link.error
        for header, _binary in link.poll():
            kind = header.get("type")
            if kind == "hello" and header.get("pair_token"):
                tokens._save_token(host, header["pair_token"])
            if kind in ("mesh_full", "mesh_instance", "group", "light", "camera_object"):
                link_id = header.get("mesh_id") or header.get("link_id")
                objects[link_id] = header
                order.append(link_id)
                last = now
        if link.status == "Connected" and not asked and now - started > 1.0:
            asked = True
            link.send({"type": "request_scene", "request_id": "matrices"})
        if asked and now - last > 12.0:
            break
        if now - started > 180.0:
            break
    link.disconnect()

    print("\n%d objects in %.1fs\n" % (len(objects), time.monotonic() - started))
    parented = [o for o in objects.values() if o.get("parent_id")]
    print("%d of them have a parent_id" % len(parented))

    checked = 0
    for header in parented:
        name = header.get("name", "?")
        world = header.get("world_matrix")
        local = header.get("local_matrix")
        parent_world = header.get("world_matrix_parent")
        parent = objects.get(header["parent_id"])
        if checked >= 4:
            break
        checked += 1
        print("\n--- %s (parent %s)" % (name, (parent or {}).get("name", header["parent_id"])))
        print("   world scale        : %s" % (scale_of(world) if world else None))
        if parent and parent.get("world_matrix"):
            print("   parent world scale : %s" % scale_of(parent["world_matrix"]))
        if local:
            print("   local scale        : %s" % scale_of(local))
        if world and local and parent_world:
            product = m(parent_world) @ m(local)
            print("   parent x local == world ? %s"
                  % numpy.allclose(product, m(world), atol=1e-4))
        if parent and parent.get("world_matrix") and parent_world:
            print("   world_matrix_parent == parent's own world ? %s"
                  % numpy.allclose(m(parent_world), m(parent["world_matrix"]), atol=1e-4))
        if world and parent and parent.get("world_matrix"):
            derived = convert.compose_local(world, parent["world_matrix"])
            print("   derived local scale: %s" % scale_of(derived))
    return 0


if __name__ == "__main__":
    sys.exit(main())
