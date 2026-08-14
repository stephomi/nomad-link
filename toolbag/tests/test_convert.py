# SPDX-License-Identifier: MIT
"""convert.py against hand-built mesh_full payloads, on both math paths."""
import array
import math
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "NomadLink"))

import convert  # noqa: E402


def encode_rgbm(colors):
    """linear rgb -> Nomad's rgbm8, the inverse of convert.decode_rgbm."""
    out = bytearray()
    for r, g, b in colors:
        m = max(1, min(255, math.ceil(max(r, g, b) * 255.0)))
        scale = 65025.0 / m
        out += bytes((min(255, int(r * scale + 0.5)), min(255, int(g * scale + 0.5)),
                      min(255, int(b * scale + 0.5)), m))
    return bytes(out)


def cube(with_uvs=False, smooth=True, matrix=None):
    """A unit cube as Nomad would send it: 8 vertices, 6 quads."""
    positions = [(x, y, z) for x in (-1.0, 1.0) for y in (-1.0, 1.0) for z in (-1.0, 1.0)]
    faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1),
             (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    binary = bytearray()
    header = {
        "type": "mesh_full", "mesh_id": "cube", "geometry_id": "geo", "name": "Cube",
        "vertex_count": len(positions), "face_count": len(faces),
        "coordinate_system": "nomad_y_up", "world_matrix": matrix or convert.IDENTITY,
        "smooth_shading": smooth, "live_sync": False,
        "position_offset": 0, "position_format": "float32x3",
    }
    for p in positions:
        binary += struct.pack("<3f", *p)
    header["face_offset"] = len(binary)
    header["face_format"] = "int32x4"
    for f in faces:
        binary += struct.pack("<4i", *f)

    if with_uvs:
        # one UV per corner: every quad gets its own square, so all 24 corners split
        header["texcoord_count"] = 24
        header["texcoord_offset"] = len(binary)
        header["texcoord_format"] = "float32x2"
        for i in range(24):
            binary += struct.pack("<2f", (i % 4) / 4.0, 0.25)
        header["face_uv_offset"] = len(binary)
        for i in range(len(faces)):
            binary += struct.pack("<4i", i * 4, i * 4 + 1, i * 4 + 2, i * 4 + 3)

    header["color_offset"] = len(binary)
    header["color_format"] = "rgbm8"
    binary += encode_rgbm([(0.5, 0.25, 1.0)] * len(positions))
    header["opacity_offset"] = len(binary)
    header["opacity_format"] = "uint8_norm"
    binary += bytes([128] * len(positions))
    header["binary_size"] = len(binary)
    return header, bytes(binary)


class ConvertTest(unittest.TestCase):
    """The mesh conversion, end to end, on the standard library alone."""

    def test_cube_without_uvs_keeps_nomad_indexing(self):
        mesh = convert.decode_mesh(*cube())
        built = convert.build(mesh)
        self.assertEqual(len(built["vertices"]), 8 * 3)
        self.assertEqual(len(built["triangles"]), 6 * 2 * 3)
        self.assertEqual(built["source"], list(range(8)))
        self.assertNotIn("uvs", built)

    def test_quads_stay_quads_in_the_polygon_table(self):
        shape = convert.topology(convert.decode_mesh(*cube()))
        # six polygons, each two triangles, starts running 0,2,4,...
        self.assertEqual(shape["polygons"], [0, 2, 2, 2, 4, 2, 6, 2, 8, 2, 10, 2])

    def test_the_polygon_table_is_held_back_until_its_units_are_known(self):
        built = convert.build(convert.decode_mesh(*cube()))
        self.assertEqual(convert.SEND_POLYGONS, False)
        self.assertNotIn("polygons", built)

    def test_triangles_index_real_vertices(self):
        built = convert.build(convert.decode_mesh(*cube()))
        self.assertTrue(all(0 <= i < 8 for i in built["triangles"]))

    def test_uv_seams_split_vertices(self):
        mesh = convert.decode_mesh(*cube(with_uvs=True))
        built = convert.build(mesh)
        self.assertEqual(len(built["source"]), 24)          # every corner is its own vertex
        self.assertEqual(len(built["uvs"]), 24 * 2)
        self.assertEqual(len(built["vertices"]), 24 * 3)
        self.assertEqual(sorted(set(built["source"])), list(range(8)))

    def test_uv_v_is_flipped_for_toolbag(self):
        built = convert.build(convert.decode_mesh(*cube(with_uvs=True)))
        self.assertAlmostEqual(built["uvs"][1], 1.0 - 0.25, places=6)

    def test_flat_shading_splits_every_corner(self):
        built = convert.build(convert.decode_mesh(*cube(smooth=False)))
        self.assertEqual(len(built["source"]), 24)
        self.assertTrue(built["flat"])

    def test_normals_are_unit_and_point_outward(self):
        built = convert.build(convert.decode_mesh(*cube()))
        normals = built["normals"]
        vertices = built["vertices"]
        for i in range(0, len(normals), 3):
            n = normals[i:i + 3]
            self.assertAlmostEqual(math.sqrt(sum(c * c for c in n)), 1.0, places=5)
            # a cube centred on the origin: the normal agrees with the position
            self.assertGreater(sum(a * b for a, b in zip(n, vertices[i:i + 3])), 0.0)

    def test_world_matrix_is_baked_into_positions(self):
        matrix = list(convert.IDENTITY)
        matrix[12], matrix[13], matrix[14] = 10.0, 0.0, 0.0   # translate +10 on x
        built = convert.build(convert.decode_mesh(*cube(matrix=matrix)))
        self.assertAlmostEqual(min(built["vertices"][0::3]), 9.0, places=5)
        self.assertAlmostEqual(max(built["vertices"][0::3]), 11.0, places=5)

    def test_vertex_colors_carry_paint_opacity_as_alpha(self):
        built = convert.build(convert.decode_mesh(*cube()))
        self.assertEqual(len(built["colors"]), 8 * 4)
        r, g, b, a = built["colors"][:4]
        self.assertAlmostEqual(r, 0.5, places=2)
        self.assertAlmostEqual(g, 0.25, places=2)
        self.assertAlmostEqual(b, 1.0, places=2)
        self.assertAlmostEqual(a, 128 / 255.0, places=3)

    def test_mixed_triangles_and_quads(self):
        header, binary = cube()
        patched = bytearray(binary)
        offset = header["face_offset"]
        struct.pack_into("<4i", patched, offset, 0, 1, 3, -1)   # first quad -> triangle
        mesh = convert.decode_mesh(header, bytes(patched))
        shape = convert.topology(mesh)
        self.assertEqual(shape["polygons"][:4], [0, 1, 1, 2])
        self.assertEqual(len(shape["triangles"]), (1 + 5 * 2) * 3)

    def test_corner_face_format_fans_ngons(self):
        # one pentagon, sent the way an ngon-capable peer would
        binary = bytearray()
        for p in [(0, 0, 0), (1, 0, 0), (2, 1, 0), (1, 2, 0), (0, 1, 0)]:
            binary += struct.pack("<3f", *p)
        header = {
            "vertex_count": 5, "face_count": 1, "position_offset": 0,
            "world_matrix": convert.IDENTITY, "smooth_shading": True,
            "face_format": "corners", "corner_count": 5,
        }
        header["face_size_offset"] = len(binary)
        binary += struct.pack("<i", 5)
        header["corner_vertex_offset"] = len(binary)
        binary += struct.pack("<5i", 0, 1, 2, 3, 4)
        built = convert.build(convert.decode_mesh(header, bytes(binary)))
        self.assertEqual(len(built["triangles"]), 3 * 3)   # 5-gon -> 3 triangles

    def test_delta_patches_positions_and_colors(self):
        mesh = convert.decode_mesh(*cube())
        binary = bytearray()
        binary += struct.pack("<2I", 0, 5)                    # touch vertices 0 and 5
        header = {"count": 2, "vertex_count": 8, "index_offset": 0, "index_format": "uint32"}
        header["position_offset"] = len(binary)
        binary += struct.pack("<6f", 9.0, 9.0, 9.0, 8.0, 8.0, 8.0)
        header["color_offset"] = len(binary)
        binary += encode_rgbm([(1.0, 0.0, 0.0), (0.0, 1.0, 0.0)])
        self.assertTrue(convert.apply_delta(mesh, header, bytes(binary)))
        self.assertEqual(list(mesh["positions"][0:3]), [9.0, 9.0, 9.0])
        self.assertEqual(list(mesh["positions"][15:18]), [8.0, 8.0, 8.0])
        self.assertAlmostEqual(mesh["color"][0], 1.0, places=2)
        self.assertAlmostEqual(mesh["color"][16], 1.0, places=2)   # vertex 5, green
        self.assertEqual(list(mesh["positions"][3:6]), [-1.0, -1.0, 1.0])  # untouched

    def test_delta_is_rejected_when_topology_changed(self):
        mesh = convert.decode_mesh(*cube())
        header = {"count": 1, "vertex_count": 99, "index_offset": 0}
        self.assertFalse(convert.apply_delta(mesh, header, struct.pack("<I", 0)))

    def test_attributes_replace_paint_arrays(self):
        mesh = convert.decode_mesh(*cube())
        binary = encode_rgbm([(0.0, 0.0, 1.0)] * 8)
        header = {"vertex_count": 8, "color_offset": 0}
        self.assertTrue(convert.apply_attributes(mesh, header, binary))
        self.assertAlmostEqual(mesh["color"][2], 1.0, places=2)
        self.assertAlmostEqual(mesh["color"][0], 0.0, places=2)

    def test_rgbm_decode_matches_the_reference_formula(self):
        binary = bytes((128, 64, 255, 200))
        decoded = convert.decode_rgbm(binary, 0, 1)
        for i, raw in enumerate((128, 64, 255)):
            self.assertAlmostEqual(decoded[i], raw * (200 / 65025.0), places=6)

    def test_an_empty_mesh_builds_to_nothing(self):
        mesh = convert.decode_mesh({
            "vertex_count": 0, "face_count": 0, "position_offset": 0, "face_offset": 0,
            "world_matrix": convert.IDENTITY, "smooth_shading": True}, b"")
        built = convert.build(mesh)
        self.assertEqual(built["vertices"], [])
        self.assertEqual(built["triangles"], [])
        self.assertEqual(built["normals"], [])

    def test_paint_channels_decode_to_unit_floats(self):
        mesh = convert.decode_mesh(*cube())
        self.assertEqual(len(mesh["alpha"]), 8)
        self.assertAlmostEqual(mesh["alpha"][0], 128 / 255.0, places=6)

    def test_identity_matrix_leaves_positions_untouched(self):
        positions = array.array("f", [1.0, 2.0, 3.0])
        self.assertIs(convert.transform_points(positions, convert.IDENTITY), positions)


if __name__ == "__main__":
    unittest.main()
