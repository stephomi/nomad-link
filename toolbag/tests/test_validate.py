# SPDX-License-Identifier: MIT
"""Nothing inconsistent may reach Toolbag: it crashes rather than raising."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "NomadLink"))

import fake_mset  # noqa: E402

sys.modules["mset"] = fake_mset

import convert  # noqa: E402
import scene as scene_module  # noqa: E402

scene_module.mset = fake_mset

from test_convert import cube  # noqa: E402


def good():
    return convert.build(convert.decode_mesh(*cube()))


class ValidateTest(unittest.TestCase):
    def test_a_real_mesh_is_clean(self):
        self.assertEqual(convert.validate(good()), {})

    def test_it_catches_an_out_of_range_triangle(self):
        built = good()
        built["triangles"] = list(built["triangles"])
        built["triangles"][0] = 9999
        self.assertIn("triangles", convert.validate(built))

    def test_it_catches_a_negative_index(self):
        built = good()
        built["triangles"] = [-1, 0, 1]
        self.assertIn("triangles", convert.validate(built))

    def test_it_catches_ragged_arrays(self):
        built = good()
        built["vertices"] = built["vertices"][:-1]
        self.assertIn("vertices", convert.validate(built))

    def test_it_catches_a_short_channel(self):
        for field in ("normals", "uvs", "colors"):
            built = good()
            built[field] = [0.0, 0.0, 0.0]
            self.assertIn(field, convert.validate(built), field)

    def test_it_catches_polygons_that_miss_triangles(self):
        built = good()
        built["polygons"] = [0, 2]          # claims 2 of the cube's 12 triangles
        self.assertIn("polygons", convert.validate(built))

    def test_it_catches_polygons_running_past_the_end(self):
        built = good()
        built["polygons"] = [10, 12]
        self.assertIn("polygons", convert.validate(built))


class GuardTest(unittest.TestCase):
    def setUp(self):
        fake_mset.reset()
        self.logged = []
        self.scene = scene_module.Scene(log=self.logged.append)
        self.mesh = convert.decode_mesh(*cube())

    def test_a_broken_optional_channel_is_dropped_not_sent(self):
        built = good()
        built["colors"] = [0.0]             # would be read past the end in C++
        obj = self.scene.apply_mesh("cube", self.mesh, built)
        self.assertEqual(obj.mesh.colors, [])
        self.assertTrue(any("dropped colors" in line for line in self.logged))
        self.assertEqual(len(obj.mesh.vertices), 24)   # the mesh still lands

    def test_broken_core_geometry_is_refused_outright(self):
        built = good()
        built["triangles"] = [0, 1, 9999]
        with self.assertRaises(ValueError):
            self.scene.apply_mesh("cube", self.mesh, built)
        self.assertEqual(fake_mset.getAllObjectsOfType("MeshObject"), [])

    def test_the_last_steps_are_recorded_for_a_crash(self):
        import tempfile
        self.scene.trace_path = os.path.join(tempfile.mkdtemp(), "last.txt")
        self.scene.apply_mesh("cube", self.mesh, good())
        with open(self.scene.trace_path) as handle:
            steps = handle.read().split()
        self.assertEqual(steps[-1], "done")          # the newest call is last
        self.assertTrue(any("Mesh(" in s for s in steps))

    def test_the_trace_covers_the_material_path_too(self):
        import tempfile
        self.scene.trace_path = os.path.join(tempfile.mkdtemp(), "last.txt")
        self.scene.apply_mesh("cube", self.mesh, good())
        self.scene.apply_material("cube", {"roughness": 0.5})
        with open(self.scene.trace_path) as handle:
            trace = handle.read()
        self.assertIn("Material()", trace)
        self.assertIn("material.assign", trace)


if __name__ == "__main__":
    unittest.main()
