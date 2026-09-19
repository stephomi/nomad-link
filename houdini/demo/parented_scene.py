# SPDX-License-Identifier: MIT
"""A stand-in Nomad that sends a parented, partly hidden scene.

Protocol 1 has no way to say who an object's parent is, and `mesh_full` has no
`visible` field, so a real Nomad scene arrives in Houdini as a flat list of
visible objects. This serves the same scene *with* the two proposed fields
(see ../PROPOSAL.md), so the result can be seen and screenshotted.

    hython demo/parented_scene.py

Then in Houdini: Nomad Link Import -> Host `127.0.0.1` -> Connect -> Get Scene.
The Scene Graph Tree should show

    /nomad/Body
        /nomad/Body/Head
        /nomad/Body/Arm_L/Hand_L
        /nomad/Body/Arm_R/Hand_R/Key      <- a light parented to a hand
        /nomad/Body/Backpack              <- hidden in Nomad, invisible here

Ctrl-C to stop. Nothing here talks to a real Nomad or changes the bridge: it
only exercises the two fields the proposal asks for.
"""
import os
import sys
import time

import numpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tests"))
sys.path.insert(0, os.path.join(HERE, "..", "python"))

from mock_nomad import MockNomad  # noqa: E402

from nomad_link import convert  # noqa: E402

PORT = 48312  # Nomad's usual port, so the node's defaults work


def box(size=0.5):
    """A unit cube: eight corners, six quads, wound Nomad's way."""
    points = numpy.array([
        [-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1],
        [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1],
    ], "f4") * size
    corners = numpy.array([
        0, 3, 2, 1, 4, 5, 6, 7, 0, 1, 5, 4,
        1, 2, 6, 5, 2, 3, 7, 6, 3, 0, 4, 7,
    ], "i4")
    return points, numpy.full(6, 4, "i4"), corners


def translation(x=0.0, y=0.0, z=0.0):
    values = list(convert.IDENTITY)
    values[12], values[13], values[14] = x, y, z
    return values


# name, parent, world position, size, visible
OBJECTS = [
    ("Body", "", (0.0, 0.0, 0.0), 0.9, True),
    ("Head", "Body", (0.0, 1.6, 0.0), 0.5, True),
    ("Arm_L", "Body", (-1.2, 0.6, 0.0), 0.4, True),
    ("Hand_L", "Arm_L", (-2.1, 0.6, 0.0), 0.25, True),
    ("Arm_R", "Body", (1.2, 0.6, 0.0), 0.4, True),
    ("Hand_R", "Arm_R", (2.1, 0.6, 0.0), 0.25, True),
    ("Backpack", "Body", (0.0, 0.5, -0.9), 0.6, False),
]


def scene_packets():
    """Every object as a mesh_full, plus a light parented to the right hand."""
    packets = []
    for name, parent, position, size, visible in OBJECTS:
        points, sizes, corners = box(size)
        header, binary = convert.encode_mesh(
            mesh_id=name, geometry_id=name + "-geo", name=name,
            positions=points, sizes=sizes, corners=corners,
            point_attribs={"color": numpy.tile([0.55, 0.6, 0.7], (len(points), 1))},
            world_matrix=translation(*position), ngon=True,
        )
        header["parent_id"] = parent      # proposed: who this belongs to
        header["visible"] = visible       # proposed: hidden objects stay hidden
        packets.append((header, binary))

    packets.append(({
        "type": "light", "link_id": "Key", "name": "Key", "parent_id": "Hand_R",
        "light_type": "point", "color": [1.0, 0.85, 0.6], "power": 12.0, "size": 0.1,
        "visible": True, "world_matrix": translation(2.1, 1.0, 0.6), "live_sync": False,
    }, b""))
    return packets


def main():
    # the version shows up in the node's Status, so there is no doubt which
    # end Houdini is actually talking to
    nomad = MockNomad(PORT, version="DEMO-parented-scene")
    nomad.start()
    print(__doc__.split("Then in Houdini")[0].strip())
    print("\nlistening on 127.0.0.1:%d -- connect Houdini with Host 127.0.0.1" % PORT,
          flush=True)

    packets = scene_packets()
    served = 0
    answered = 0
    try:
        while True:
            time.sleep(0.2)
            if nomad.connections > served:  # a fresh Connect gets the scene again
                served = nomad.connections
                answered = 0
                for header, binary in packets:
                    nomad.send(header, binary)
                print("sent %d objects (%d meshes, 1 light, 1 hidden) to connection %d"
                      % (len(packets), len(OBJECTS), served), flush=True)
            requests = [h for h, _ in nomad.received
                        if h.get("type") in ("request_scene", "request_selection")]
            if len(requests) > answered:
                answered = len(requests)
                for header, binary in packets:
                    nomad.send(header, binary)
                print("re-sent the scene for a Get Scene", flush=True)
    except KeyboardInterrupt:
        print("\nbye")
        return 0


if __name__ == "__main__":
    sys.exit(main())
