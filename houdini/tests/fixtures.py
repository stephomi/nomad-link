# SPDX-License-Identifier: MIT
"""Shared test scene: a stand-in cache and a small mesh with paint and groups."""
import os
import sys

import numpy

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

from nomad_link import convert  # noqa: E402


class Cache:
    """Stands in for the client: just the fields author_scene reads."""

    def __init__(self):
        self.meshes = {}
        self.order = []
        self.materials = {}
        self.lights = {}
        self.cameras = {}
        self.textures = {}

    def add_mesh(self, mesh):
        self.meshes[mesh["mesh_id"]] = mesh
        self.order.append(mesh["mesh_id"])


def quad_and_tri(mesh_id="m1", name="Sculpt", translate_y=10.0):
    """A quad and a triangle, with uvs, vertex paint, mask and two face groups."""
    points = numpy.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [2, 0, 0]], "f4")
    texcoords = numpy.array([[0, 0], [1, 0], [1, 1], [0, 1], [0, 0], [1, 0], [1, 1]], "f4")
    world = list(convert.IDENTITY)
    world[13] = translate_y
    header, binary = convert.encode_mesh(
        mesh_id=mesh_id, geometry_id="g1", name=name,
        positions=points, sizes=numpy.array([4, 3], "i4"),
        corners=numpy.array([0, 1, 2, 3, 1, 4, 2], "i4"), texcoords=texcoords,
        point_attribs={"color": numpy.tile([0.2, 0.4, 0.6], (5, 1)),
                       "mask": numpy.linspace(0, 1, 5)},
        face_group=numpy.array([0, 1], "i4"), face_group_names=("Head", "Body"),
        world_matrix=world, ngon=True,
    )
    return convert.decode_mesh(header, binary)
