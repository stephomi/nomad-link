# SPDX-License-Identifier: MIT
"""Codec round trips. Runs outside Houdini: python3 tests/test_convert.py"""
import os
import sys

import numpy

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

from nomad_link import convert  # noqa: E402

CUBE_POINTS = numpy.array([
    [-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1],
    [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1],
], "f4")
CUBE_SIZES = numpy.full(6, 4, "i4")
CUBE_CORNERS = numpy.array([
    0, 1, 2, 3, 4, 7, 6, 5, 0, 4, 5, 1, 1, 5, 6, 2, 2, 6, 7, 3, 3, 7, 4, 0,
], "i4")


def mixed_mesh():
    """Two triangles and one pentagon, to exercise sizes/corners bookkeeping."""
    sizes = numpy.array([3, 5, 3], "i4")
    corners = numpy.array([0, 1, 2, 2, 3, 4, 5, 6, 6, 0, 1], "i4")
    points = numpy.arange(21, dtype="f4").reshape(7, 3)
    return points, sizes, corners


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("ok  " + message)


def test_ngon_round_trip():
    points, sizes, corners = mixed_mesh()
    texcoords = numpy.arange(len(corners) * 2, dtype="f4").reshape(-1, 2) / 32.0
    header, binary = convert.encode_mesh(
        mesh_id="m", geometry_id="g", name="mixed",
        positions=points, sizes=sizes, corners=corners, texcoords=texcoords,
        point_attribs={"color": numpy.linspace(0, 1, 21).reshape(7, 3),
                       "rough": numpy.linspace(0, 1, 7)},
        face_group=numpy.array([0, 1, 1], "i4"), face_group_names=("a", "b"),
        ngon=True,
    )
    check(header["binary_size"] == len(binary), "binary_size matches the payload")
    mesh = convert.decode_mesh(header, binary)
    check(numpy.allclose(mesh["positions"], points), "ngon positions survive")
    check(numpy.array_equal(mesh["sizes"], sizes), "ngon face sizes survive")
    check(numpy.array_equal(mesh["corners"], corners), "ngon corners survive")
    check(numpy.allclose(mesh["texcoords"][mesh["corner_uv"]], texcoords, atol=1e-6),
          "ngon uvs survive")
    check(numpy.allclose(mesh["color"], numpy.linspace(0, 1, 21).reshape(7, 3), atol=0.01),
          "rgbm8 colour round trips within a byte")
    check(numpy.allclose(mesh["rough"], numpy.linspace(0, 1, 7), atol=0.01), "roughness round trips")
    check(numpy.array_equal(mesh["face_group"], [0, 1, 1]), "face groups survive")


def test_quad_split():
    points, sizes, corners = mixed_mesh()
    header, binary = convert.encode_mesh(
        mesh_id="m", geometry_id="g", name="mixed",
        positions=points, sizes=sizes, corners=corners,
        face_group=numpy.array([7, 8, 9], "i4"), face_group_names=("a", "b", "c"),
        ngon=False,
    )
    mesh = convert.decode_mesh(header, binary)
    # pentagon fans into 3 triangles, the two triangles stay: 5 faces
    check(header["face_count"] == 5, "n-gon fans into triangles (%d faces)" % header["face_count"])
    check(set(mesh["sizes"].tolist()) == {3}, "split output is all triangles")
    # groups 7 and 9 are the two triangles; group 8's pentagon becomes three faces
    check(sorted(mesh["face_group"].tolist()) == [7, 8, 8, 8, 9],
          "face groups follow the split: %s" % mesh["face_group"].tolist())
    areas = {tuple(sorted(part.tolist())) for part in
             numpy.split(mesh["corners"], numpy.cumsum(mesh["sizes"])[:-1])}
    check((0, 1, 2) in areas, "original triangle survives the split")


def test_int32x4_uv_pairing():
    """Quads and triangles mixed: uv corners must stay aligned with vertex corners."""
    sizes = numpy.array([4, 3], "i4")
    corners = numpy.array([0, 1, 2, 3, 1, 2, 4], "i4")
    texcoords = numpy.array([[0, 0], [1, 0], [1, 1], [0, 1], [0.5, 0.5], [0.25, 0], [0, 0.75]], "f4")
    header, binary = convert.encode_mesh(
        mesh_id="m", geometry_id="g", name="q",
        positions=numpy.zeros((5, 3), "f4"), sizes=sizes, corners=corners,
        texcoords=texcoords, corner_uv=numpy.arange(7, dtype="i4"), ngon=False,
    )
    mesh = convert.decode_mesh(header, binary)
    # the split groups faces by size, so face order changes; the pairing must not
    check(sorted(mesh["sizes"].tolist()) == [3, 4], "one triangle and one quad come back")
    check(numpy.array_equal(corners[mesh["corner_uv"]], mesh["corners"]),
          "every corner still points at its own texcoord")


def test_reverse_permutation():
    sizes = numpy.array([3, 4], "i4")
    corners = numpy.array([0, 1, 2, 10, 11, 12, 13], "i4")
    flipped = corners[convert.reverse_permutation(sizes)]
    check(flipped.tolist() == [2, 1, 0, 13, 12, 11, 10], "winding flips per face")
    twice = flipped[convert.reverse_permutation(sizes)]
    check(numpy.array_equal(twice, corners), "flipping twice is the identity")


def test_delta():
    header, binary = convert.encode_mesh(
        mesh_id="m", geometry_id="g", name="cube",
        positions=CUBE_POINTS, sizes=CUBE_SIZES, corners=CUBE_CORNERS, ngon=True,
    )
    mesh = convert.decode_mesh(header, binary)
    moved = numpy.array([[9.0, 9.0, 9.0], [8.0, 8.0, 8.0]], "<f4")
    payload = numpy.array([2, 5], "<u4").tobytes() + moved.tobytes()
    applied = convert.apply_delta(mesh, {
        "count": 2, "vertex_count": 8, "index_offset": 0, "position_offset": 8,
    }, payload)
    check(applied, "delta applies to matching topology")
    check(numpy.allclose(mesh["positions"][2], [9, 9, 9]), "delta moved the right vertex")
    check(numpy.allclose(mesh["positions"][0], CUBE_POINTS[0]), "untouched vertices are untouched")
    check(not convert.apply_delta(mesh, {"count": 1, "vertex_count": 99}, b""),
          "delta on mismatched topology is refused")


def test_transform():
    matrix = list(convert.IDENTITY)
    matrix[12], matrix[13], matrix[14] = 5.0, 0.0, 0.0  # column-major translation
    moved = convert.transform_points(CUBE_POINTS, matrix)
    check(numpy.allclose(moved - CUBE_POINTS, [5, 0, 0]), "world_matrix translation applies")
    back = convert.transform_points(moved, matrix, inverse=True)
    check(numpy.allclose(back, CUBE_POINTS), "inverse transform returns the original")


if __name__ == "__main__":
    for name, function in sorted(globals().items()):
        if name.startswith("test_"):
            print("--- %s" % name)
            function()
    print("\nall good")
