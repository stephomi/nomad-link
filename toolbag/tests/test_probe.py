# SPDX-License-Identifier: MIT
"""The probe runs to the end and reads the fake Toolbag correctly."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "NomadLink"))

import fake_mset  # noqa: E402

sys.modules["mset"] = fake_mset

import probe as probe_module  # noqa: E402

probe_module.mset = fake_mset


class ProbeTest(unittest.TestCase):
    def setUp(self):
        fake_mset.reset()
        self.folder = tempfile.mkdtemp()
        self.trace = os.path.join(self.folder, "nomad_probe_last.txt")
        self.report = probe_module.Probe(self.folder).run()

    def test_it_reaches_the_end_without_raising(self):
        self.assertIn("Nomad Link probe", self.report)
        self.assertIn("look at the 'Nomad probe quad'", self.report)

    def test_it_writes_its_report_and_test_texture(self):
        self.assertTrue(os.path.isfile(os.path.join(self.folder, "nomad_probe_report.txt")))
        png = os.path.join(self.folder, "nomad_probe_uv.png")
        self.assertTrue(os.path.isfile(png))
        with open(png, "rb") as handle:
            self.assertEqual(handle.read(8), b"\x89PNG\r\n\x1a\n")

    def test_it_identifies_the_rotation_order_the_app_really_uses(self):
        # fake_mset rotates z then x then y in degrees, as Toolbag 5.032 does
        self.assertIn("applies ZXY, in degrees", self.report)
        self.assertIn("scene.euler_from_matrix", self.report)
        self.assertNotIn("NEEDS UPDATING", self.report)

    def test_it_reports_the_real_material_field_names(self):
        self.assertIn("Albedo Map", self.report)
        self.assertIn("Roughness", self.report)
        self.assertIn("Vertex Color", self.report)

    def test_it_notices_toolbag_computing_no_normals(self):
        self.assertIn("the bridge must send its own", self.report)

    def test_it_leaves_only_the_quad_behind(self):
        left = [o.name for o in fake_mset.getAllObjectsOfType("MeshObject")]
        self.assertEqual(left, ["Nomad probe quad"])

    def test_running_it_twice_leaves_one_quad_and_one_material(self):
        probe_module.Probe(self.folder).run()
        self.assertEqual([o.name for o in fake_mset.getAllObjectsOfType("MeshObject")],
                         ["Nomad probe quad"])
        self.assertEqual([m.name for m in fake_mset.getAllMaterials()],
                         ["Nomad probe material"])

    # Toolbag dies inside its own C++ with no traceback, so the report has to be
    # on disk before the call that might kill it, and the next run has to move on.

    def test_the_report_reaches_the_disk_line_by_line(self):
        probe = probe_module.Probe(self.folder)
        probe.say("half a report")
        with open(probe.report_path) as handle:
            self.assertIn("half a report", handle.read())

    def test_a_run_that_finishes_leaves_nothing_to_skip(self):
        with open(self.trace) as handle:
            self.assertEqual(handle.read().strip(), "")

    def test_the_call_in_flight_is_named_on_disk(self):
        probe = probe_module.Probe(self.folder)
        probe.begin("section transform")
        probe.begin("rotation")
        with open(self.trace) as handle:
            self.assertEqual(handle.read().split("\n")[-1], "rotation")

    def test_the_next_run_skips_what_crashed_and_finishes_the_rest(self):
        with open(self.trace, "w") as handle:
            handle.write("section transform")     # as a crash would leave it
        report = probe_module.Probe(self.folder).run()
        self.assertIn("SKIPPED", report)
        self.assertNotIn("applies ZXY", report)   # the transform section is skipped
        self.assertIn("=== material slots ===", report)

    def test_it_refuses_to_run_outside_toolbag(self):
        saved, probe_module.mset = probe_module.mset, None
        try:
            with self.assertRaises(RuntimeError):
                probe_module.run()
        finally:
            probe_module.mset = saved


if __name__ == "__main__":
    unittest.main()
