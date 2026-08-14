# SPDX-License-Identifier: MIT
"""scene.py against the fake mset: what actually lands in the Toolbag scene."""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "NomadLink"))

import fake_mset  # noqa: E402

sys.modules["mset"] = fake_mset

import convert  # noqa: E402
import scene as scene_module  # noqa: E402
from test_convert import cube  # noqa: E402

scene_module.mset = fake_mset


class SceneTest(unittest.TestCase):
    def setUp(self):
        fake_mset.reset()
        self.logged = []
        self.scene = scene_module.Scene(log=self.logged.append)

    def _cube(self, **kwargs):
        mesh = convert.decode_mesh(*cube(**kwargs))
        return mesh, convert.build(mesh)

    # ------------------------------------------------------------------ meshes

    def test_mesh_lands_in_the_scene_with_every_channel(self):
        mesh, built = self._cube()
        obj = self.scene.apply_mesh("cube", mesh, built)
        self.assertEqual(obj.name, "Cube")
        self.assertEqual(len(obj.mesh.vertices), 24)
        self.assertEqual(len(obj.mesh.triangles), 36)
        self.assertEqual(len(obj.mesh.normals), 24)
        self.assertEqual(len(obj.mesh.colors), 32)
        self.assertEqual(obj.mesh.polygons, [])   # held back, see convert.SEND_POLYGONS
        self.assertEqual(fake_mset.getAllObjectsOfType("MeshObject"), [obj])

    def test_the_polygon_table_still_reaches_toolbag_when_enabled(self):
        saved = convert.SEND_POLYGONS
        convert.SEND_POLYGONS = True
        self.addCleanup(setattr, convert, "SEND_POLYGONS", saved)
        mesh, built = self._cube()
        obj = self.scene.apply_mesh("cube", mesh, built)
        self.assertEqual(obj.mesh.polygons, [0, 2, 2, 2, 4, 2, 6, 2, 8, 2, 10, 2])

    def test_uvs_reach_the_mesh(self):
        mesh, built = self._cube(with_uvs=True)
        obj = self.scene.apply_mesh("cube", mesh, built)
        self.assertEqual(len(obj.mesh.uvs), 48)

    def test_resending_a_mesh_reuses_the_same_object(self):
        mesh, built = self._cube()
        first = self.scene.apply_mesh("cube", mesh, built)
        second = self.scene.apply_mesh("cube", mesh, built)
        self.assertIs(first, second)
        self.assertEqual(len(fake_mset.getAllObjectsOfType("MeshObject")), 1)

    def substitute_mesh_class(self, subclass):
        """Swap the class Toolbag constructs, since that is what scene.py calls."""
        saved = fake_mset.Mesh
        fake_mset.Mesh = subclass
        self.addCleanup(setattr, fake_mset, "Mesh", saved)

    def test_a_rejected_channel_does_not_lose_the_mesh(self):
        base = fake_mset.Mesh

        class Picky(base):
            def __init__(self, *args, **kwargs):
                base.__init__(self, *args, **kwargs)
                self.ready = True

            def __setattr__(self, name, value):
                if name == "colors" and getattr(self, "ready", False):
                    raise ValueError("this build has no vertex colors")
                base.__setattr__(self, name, value)

        self.substitute_mesh_class(Picky)
        mesh, built = self._cube()
        obj = self.scene.apply_mesh("cube", mesh, built)
        self.assertEqual(len(obj.mesh.vertices), 24)   # the mesh still arrived
        self.assertTrue(any("colors" in line for line in self.logged))

    def test_it_never_reads_a_written_mesh_back_out_of_the_object(self):
        # the wrapper Toolbag hands back owns its own mesh: writing it to the
        # object it came from is a self-assignment inside Toolbag's C++
        mesh, built = self._cube()
        obj = self.scene.apply_mesh("cube", mesh, built)
        self.assertEqual(obj.mesh_reads, 0)
        self.scene.update_geometry("cube", built)
        self.assertEqual(obj.mesh_reads, 0)

    def test_the_mesh_given_to_toolbag_stays_referenced(self):
        mesh, built = self._cube()
        obj = self.scene.apply_mesh("cube", mesh, built)
        self.assertIs(self.scene.geometry["cube"], obj.mesh)
        self.scene.delete("cube")
        self.assertNotIn("cube", self.scene.geometry)

    def test_every_mesh_gets_the_submesh_it_needs_to_render(self):
        mesh, built = self._cube()
        obj = self.scene.apply_mesh("cube", mesh, built)
        subs = obj.getChildren()
        self.assertEqual(len(subs), 1)
        self.assertEqual((subs[0].startIndex, subs[0].indexCount), (0, -1))
        self.scene.update_geometry("cube", built)   # a stroke must not stack more
        self.assertEqual(len(obj.getChildren()), 1)

    def test_paint_disables_srgb_and_alpha_follows_the_opacity_channel(self):
        mesh, built = self._cube()
        self.scene.apply_mesh("cube", mesh, built)
        material = self.scene.apply_material("cube", {}, has_paint=True, has_alpha=True)
        self.assertEqual(material.albedo.name, "Vertex Color")
        self.assertIs(material.albedo.getField("sRGB Color"), False)
        self.assertIs(material.albedo.getField("Vertex Alpha"), True)
        self.scene.apply_material("cube", {}, has_paint=True, has_alpha=False)
        self.assertIs(material.albedo.getField("Vertex Alpha"), False)

    def test_the_material_lands_on_the_submesh(self):
        mesh, built = self._cube()
        self.scene.apply_mesh("cube", mesh, built)
        material = self.scene.apply_material("cube", {"roughness": 0.4})
        self.assertIs(self.scene.submeshes["cube"].material, material)

    def test_the_first_mesh_frames_the_camera_and_later_ones_do_not(self):
        mesh, built = self._cube()
        obj = self.scene.apply_mesh("cube", mesh, built)
        self.assertEqual(fake_mset.FRAMED, [obj])
        self.scene.apply_mesh("cube2", mesh, built)
        self.assertEqual(fake_mset.FRAMED, [obj])   # still just the first

    def test_a_centimeter_scene_gets_the_mesh_100x_bigger(self):
        # without this a Nomad sculpt is 1/100th of the floor grid: "nothing shows"
        saved = fake_mset.getSceneUnitScale
        fake_mset.getSceneUnitScale = lambda: 0.01
        self.addCleanup(setattr, fake_mset, "getSceneUnitScale", saved)
        self.assertAlmostEqual(self.scene.unit_scale(), 100.0)
        mesh, _ = self._cube()
        built = convert.build(mesh, True, self.scene.unit_scale())
        obj = self.scene.apply_mesh("cube", mesh, built)
        self.assertAlmostEqual(max(obj.mesh.vertices), 100.0, places=3)

    def test_replace_all_clears_user_objects_but_keeps_sky_and_working_camera(self):
        mesh, built = self._cube()
        self.scene.apply_mesh("cube", mesh, built)
        working = fake_mset.CameraObject("Main Camera")   # getCamera returns this
        user_mesh = fake_mset.MeshObject("user mesh")
        user_light = fake_mset.LightObject("user light")
        user_camera = fake_mset.CameraObject("user cam")
        sky = fake_mset.SkyBoxObject()
        self.scene.clear_scene()
        self.assertTrue(user_mesh.destroyed)
        self.assertTrue(user_light.destroyed)
        self.assertTrue(user_camera.destroyed)
        self.assertFalse(working.destroyed)
        self.assertFalse(sky.destroyed)
        self.assertEqual(self.scene.objects, {})

    def test_a_nomad_light_becomes_a_toolbag_light(self):
        matrix = list(convert.IDENTITY)
        matrix[12:15] = [1.0, 2.0, 3.0]
        light = self.scene.apply_light("sun", {
            "light_type": "SUN", "color": [1.0, 0.5, 0.2], "intensity": 2.0,
            "power": 6.0, "factor": 1.0,   # the block carries every strength
            "world_matrix": matrix, "name": "Key", "shadow_cast": False})
        self.assertEqual(light.lightType, "directional")
        self.assertEqual(light.brightness, 2.0)   # intensity, not power
        self.assertEqual(light.position, [1.0, 2.0, 3.0])
        self.assertFalse(light.castShadows)
        spot = self.scene.apply_light("spot", {"light_type": "SPOT",
                                               "spot_angle": 3.14159265 / 2,
                                               "spot_softness": 0.25, "power": 5.0})
        self.assertEqual(spot.lightType, "spot")
        self.assertAlmostEqual(spot.spotAngle, 90.0, places=3)
        self.assertAlmostEqual(spot.spotSharpness, 0.75)
        self.assertIsNone(self.scene.apply_light("env", {"light_type": "ENVIRONMENT"}))
        self.assertTrue(self.scene.delete("sun"))

    def test_strength_goes_through_untouched_and_only_the_radius_scales(self):
        # Toolbag's brightness is log-scaled, so scaling it by the scene unit
        # moved the light four decades: the number travels as Nomad sends it
        saved = fake_mset.getSceneUnitScale
        fake_mset.getSceneUnitScale = lambda: 0.01
        self.addCleanup(setattr, fake_mset, "getSceneUnitScale", saved)
        light = self.scene.apply_light("bulb", {"light_type": "POINT", "power": 3.0,
                                                "intensity": 1.0, "size": 0.5})
        self.assertAlmostEqual(light.brightness, 3.0)
        self.assertAlmostEqual(light.width, 50.0)     # a radius is a distance
        self.assertFalse(light.physicalUnits)
        sun = self.scene.apply_light("sun", {"light_type": "SUN", "intensity": 2.0,
                                             "power": 3.0})
        self.assertAlmostEqual(sun.brightness, 2.0)   # directional has no distance

    def test_kelvin_forces_the_color_white_and_off_restores_it(self):
        # Toolbag multiplies temperature with color; Nomad's kelvin replaces it
        light = self.scene.apply_light("key", {
            "light_type": "POINT", "color": [1.0, 0.2, 0.1],
            "use_kelvin": True, "kelvin": 3200})
        self.assertEqual(light.color, [1.0, 1.0, 1.0])
        self.assertTrue(light.useTemperature)
        self.assertEqual(light.temperature, 3200)
        self.scene.apply_light("key", {"light_type": "POINT",
                                       "color": [1.0, 0.2, 0.1], "use_kelvin": False})
        self.assertEqual(light.color, [1.0, 0.2, 0.1])
        self.assertFalse(light.useTemperature)

    def test_a_nomad_camera_becomes_a_scene_camera(self):
        camera = self.scene.apply_camera_object("cam", {
            "name": "Shot", "orthographic": True, "fov_y": 40.0,
            "world_matrix": convert.IDENTITY})
        self.assertEqual(camera.mode, "orthographic")
        self.assertEqual(camera.fov, 40.0)
        again = self.scene.apply_camera_object("cam", {"orthographic": False})
        self.assertIs(again, camera)
        self.assertEqual(camera.mode, "perspective")

    def test_an_orthographic_view_follows_as_orthographic(self):
        self.scene.follow_view = True
        camera = fake_mset.CameraObject("Main Camera")
        header = {"world_from_view": convert.IDENTITY, "fov_y": 30.0,
                  "orthographic": True, "ortho_scale": 2.5}
        self.assertTrue(self.scene.apply_camera(header))
        self.assertEqual(camera.mode, "orthographic")
        self.assertEqual(camera.orthoScale, 2.5)

    # Toolbag 5.032 disagrees with its own documentation on these two signatures

    def test_a_material_is_assigned_without_the_includechildren_argument(self):
        tried = []
        base = fake_mset.Material

        class Strict(base):
            def assign(self, obj, includeChildren=None):
                tried.append(includeChildren)
                if includeChildren is not None:
                    raise ValueError("The attribute value must be an Object")
                base.assign(self, obj)

        fake_mset.Material = Strict
        self.addCleanup(setattr, fake_mset, "Material", base)
        mesh, built = self._cube()
        obj = self.scene.apply_mesh("cube", mesh, built)
        material = self.scene.apply_material("cube", {"roughness": 0.4})
        self.assertEqual(tried, [None])
        self.assertEqual(material.assigned, [obj])

    def test_a_type_lookup_falls_back_when_a_string_is_refused(self):
        saved = fake_mset.getAllObjectsOfType

        def only_strings(class_name):
            if isinstance(class_name, type):
                raise ValueError("Must pass an object of type")
            return saved(class_name)

        fake_mset.getAllObjectsOfType = only_strings
        self.addCleanup(setattr, fake_mset, "getAllObjectsOfType", saved)
        sky = fake_mset.SkyBoxObject()
        self.assertEqual(scene_module.objects_of_type("SkyBoxObject"), [sky])

    def test_it_sends_normals_when_toolbag_makes_none(self):
        mesh, built = self._cube()
        obj = self.scene.apply_mesh("cube", mesh, built)
        self.assertTrue(self.scene.needs_normals)
        self.assertEqual(len(obj.mesh.normals), 24)

    def test_it_stops_computing_normals_once_toolbag_does_it(self):
        base = fake_mset.Mesh

        class Generous(base):
            def __init__(self, *args, **kwargs):   # this build makes its own normals
                base.__init__(self, *args, **kwargs)
                base.__setattr__(self, "normals", [0.0, 1.0, 0.0] * 8)

        self.substitute_mesh_class(Generous)
        mesh, built = self._cube()
        self.scene.apply_mesh("cube", mesh, built)
        self.assertFalse(self.scene.needs_normals)
        # the client asks before building, and now skips the expensive step
        self.assertNotIn("normals", convert.build(mesh, False))

    def test_a_failed_write_leaves_no_meshless_object_behind(self):
        mesh, built = self._cube()
        broken = dict(built)
        del broken["vertices"]          # whatever the cause, the write throws
        with self.assertRaises(Exception):
            self.scene.apply_mesh("cube", mesh, broken)
        self.assertEqual(fake_mset.getAllObjectsOfType("MeshObject"), [])
        self.assertNotIn("cube", self.scene.objects)

    def test_a_failed_rewrite_keeps_the_object_that_already_worked(self):
        mesh, built = self._cube()
        obj = self.scene.apply_mesh("cube", mesh, built)
        broken = dict(built)
        del broken["vertices"]
        with self.assertRaises(Exception):
            self.scene.apply_mesh("cube", mesh, broken)
        self.assertFalse(obj.destroyed)   # it still has its old geometry

    def test_object_state_renames_and_hides(self):
        mesh, built = self._cube()
        obj = self.scene.apply_mesh("cube", mesh, built)
        self.scene.apply_object_state("cube", {"name": "Head", "visible": False})
        self.assertEqual(obj.name, "Head")
        self.assertFalse(obj.visible)

    def test_delete_destroys_and_forgets(self):
        mesh, built = self._cube()
        obj = self.scene.apply_mesh("cube", mesh, built)
        self.assertTrue(self.scene.delete("cube"))
        self.assertTrue(obj.destroyed)
        self.assertEqual(fake_mset.getAllObjectsOfType("MeshObject"), [])
        self.assertFalse(self.scene.delete("cube"))

    def test_a_destroyed_object_is_rebuilt_not_reused(self):
        mesh, built = self._cube()
        obj = self.scene.apply_mesh("cube", mesh, built)
        obj.destroy()  # deleted in Toolbag behind our back: the handle goes dead
        again = self.scene.apply_mesh("cube", mesh, built)
        self.assertIsNot(again, obj)

    # --------------------------------------------------------------- materials

    def test_material_maps_roughness_and_metalness(self):
        mesh, built = self._cube()
        self.scene.apply_mesh("cube", mesh, built)
        material = self.scene.apply_material("cube", {"roughness": 0.25, "metalness": 1.0})
        self.assertAlmostEqual(material.microsurface.getField("Roughness"), 0.25, places=6)
        self.assertAlmostEqual(material.reflectivity.getField("Metalness"), 1.0, places=6)

    def test_roughness_is_inverted_for_a_gloss_shader(self):
        base = fake_mset.MaterialSubroutine
        mesh, built = self._cube()
        self.scene.apply_mesh("cube", mesh, built)
        material = self.scene.apply_material("cube", {})
        material.microsurface = base("Gloss", {"Gloss Map": None, "Gloss": 0.5})
        self.scene.apply_material("cube", {"roughness": 0.25})
        self.assertAlmostEqual(material.microsurface.getField("Gloss"), 0.75, places=6)

    def test_a_slot_no_shader_can_fill_is_reported_not_crashed_into(self):
        # a build that refuses every transparency shader must not take the plugin down
        material = fake_mset.Material("picky")
        material.setSubroutine = lambda slot, shader: (_ for _ in ()).throw(ValueError(shader))
        self.assertFalse(self.scene._set(material, "transparency", [("alpha",)], 0.4))
        self.assertIsNone(material.transparency)
        self.assertTrue([line for line in self.logged if "transparency" in line])

    def test_a_missing_field_is_logged_not_raised(self):
        mesh, built = self._cube()
        self.scene.apply_mesh("cube", mesh, built)
        self.scene._set(self.scene.materials.get("cube") or fake_mset.Material("x"),
                        "albedo", [("nosuchfield",)], 1.0)
        self.assertTrue(any("albedo" in line for line in self.logged))

    def test_painted_mesh_switches_albedo_to_vertex_color(self):
        mesh, built = self._cube()
        self.scene.apply_mesh("cube", mesh, built)
        material = self.scene.apply_material("cube", {"color": [1.0, 0.0, 0.0]}, has_paint=True)
        self.assertEqual(material.albedo.name, "Vertex Color")

    def test_unpainted_mesh_keeps_the_material_color(self):
        mesh, built = self._cube()
        self.scene.apply_mesh("cube", mesh, built)
        material = self.scene.apply_material("cube", {"color": [1.0, 0.0, 0.0]}, has_paint=False)
        self.assertEqual(material.albedo.getField("Color"), [1.0, 0.0, 0.0])

    def test_material_is_assigned_to_its_object(self):
        mesh, built = self._cube()
        obj = self.scene.apply_mesh("cube", mesh, built)
        material = self.scene.apply_material("cube", {"roughness": 0.5})
        self.assertIn(obj, material.assigned)

    def test_texture_blob_is_written_and_assigned(self):
        mesh, built = self._cube()
        self.scene.apply_mesh("cube", mesh, built)
        path = self.scene.store_blob("abc123", "Sphere color.png", b"\x89PNG fake")
        self.assertTrue(os.path.isfile(path))
        material = self.scene.apply_material("cube", {
            "textures": {"color": {"texture_id": "abc123", "name": "Sphere color.png"}}})
        self.assertEqual(material.albedo.getField("Albedo Map").path, path)

    def test_color_map_stays_srgb_while_paint_and_data_maps_do_not(self):
        mesh, built = self._cube()
        self.scene.apply_mesh("cube", mesh, built)
        color = self.scene.store_blob("c", "color.png", b"\x89PNG fake")
        rough = self.scene.store_blob("r", "rough.png", b"\x89PNG fake")
        material = self.scene.apply_material("cube", {"textures": {
            "color": {"texture_id": "c"}, "roughness": {"texture_id": "r"}}},
            has_paint=True)
        self.assertIs(material.albedo.getField("sRGB Color"), False)
        self.assertIs(fake_mset.findTexture(color).sRGB, True)
        self.assertIs(fake_mset.findTexture(rough).sRGB, False)

    def test_empty_slots_are_switched_on_for_the_channels_that_need_them(self):
        mesh, built = self._cube()
        self.scene.apply_mesh("cube", mesh, built)
        glow = self.scene.store_blob("e", "glow.png", b"\x89PNG fake")
        material = self.scene.apply_material("cube", {"opacity": 0.5, "textures": {
            "emissive": {"texture_id": "e", "factor": [1.0, 0.5, 0.0], "strength": 3.0}}})
        self.assertEqual(material.emission.getField("Emissive Map").path, glow)
        self.assertIs(fake_mset.findTexture(glow).sRGB, True)
        self.assertEqual(material.emission.getField("Color"), [1.0, 0.5, 0.0])
        self.assertAlmostEqual(material.emission.getField("Intensity"), 3.0, places=6)
        self.assertAlmostEqual(material.transparency.getField("Alpha"), 0.5, places=6)

    def test_an_emptied_channel_removes_the_map_it_had(self):
        mesh, built = self._cube()
        self.scene.apply_mesh("cube", mesh, built)
        self.scene.store_blob("e", "glow.png", b"\x89PNG fake")
        material = self.scene.apply_material("cube", {"textures": {
            "emissive": {"texture_id": "e", "strength": 2.0}}})
        self.scene.apply_material("cube", {"textures": {"emissive": {}}})
        self.assertIsNone(material.emission.getField("Emissive Map"))
        self.assertAlmostEqual(material.emission.getField("Intensity"), 0.0, places=6)

    def test_a_channel_still_waiting_for_its_pixels_keeps_the_current_map(self):
        mesh, built = self._cube()
        self.scene.apply_mesh("cube", mesh, built)
        path = self.scene.store_blob("here", "color.png", b"\x89PNG fake")
        material = self.scene.apply_material("cube", {"textures": {
            "color": {"texture_id": "here"}}})
        self.scene.apply_material("cube", {"textures": {"color": {"texture_id": "later"}}})
        self.assertEqual(material.albedo.getField("Albedo Map").path, path)

    def test_the_factors_multiply_the_maps_they_belong_to(self):
        mesh, built = self._cube()
        self.scene.apply_mesh("cube", mesh, built)
        for blob in ("c", "r", "m", "o", "a"):
            self.scene.store_blob(blob, blob + ".png", b"\x89PNG fake")
        material = self.scene.apply_material("cube", {
            "color": [1.0, 0.5, 0.5], "roughness": 0.9, "metalness": 0.9, "opacity": 0.5,
            "textures": {
                "color": {"texture_id": "c", "factor": [0.5, 1.0, 1.0]},
                "roughness": {"texture_id": "r", "factor": 0.25},
                "metalness": {"texture_id": "m", "factor": 0.75},
                "occlusion": {"texture_id": "o", "factor": 0.6},
                "opacity": {"texture_id": "a", "factor": 0.4}}})
        # the map replaces the slider, except for opacity where both multiply
        self.assertEqual(material.albedo.getField("Color"), [0.5, 0.5, 0.5])
        self.assertAlmostEqual(material.microsurface.getField("Roughness"), 0.25, places=6)
        self.assertAlmostEqual(material.reflectivity.getField("Metalness"), 0.75, places=6)
        self.assertAlmostEqual(material.occlusion.getField("Occlusion"), 0.6, places=6)
        self.assertAlmostEqual(material.transparency.getField("Alpha"), 0.2, places=6)
        self.assertEqual(material.transparency.getField("Channel"), 0)   # red

    def test_a_flipped_normal_map_and_a_displacement_map_reach_their_slots(self):
        mesh, built = self._cube()
        self.scene.apply_mesh("cube", mesh, built)
        for blob in ("n", "d"):
            self.scene.store_blob(blob, blob + ".png", b"\x89PNG fake")
        material = self.scene.apply_material("cube", {"textures": {
            "normal": {"texture_id": "n", "neg_y": True},
            "displacement": {"texture_id": "d"}}})
        self.assertIs(material.surface.getField("Flip Y"), True)
        self.assertIsNotNone(material.displacement.getField("Height Map"))

    def test_the_material_type_picks_the_slot_that_carries_it(self):
        mesh, built = self._cube()
        self.scene.apply_mesh("cube", mesh, built)
        material = self.scene.apply_material("cube", {
            "material_type": "refraction", "refraction_ior": 1.33})
        self.assertEqual(material.transparency.name, "Refraction")
        self.assertAlmostEqual(material.transparency.getField("IOR"), 1.33, places=6)
        self.scene.apply_material("cube", {"material_type": "additive"})
        self.assertEqual(material.transparency.name, "Add")

    def test_subsurface_lands_in_the_diffusion_slot_and_is_undone(self):
        mesh, built = self._cube()
        self.scene.apply_mesh("cube", mesh, built)
        material = self.scene.apply_material("cube", {
            "material_type": "subsurface", "subsurface_color": [1.0, 0.2, 0.1],
            "subsurface_depth": 2.5})
        self.assertEqual(material.diffusion.name, "Subsurface Scatter")
        self.assertEqual(material.diffusion.getField("Subdermis Color"), [1.0, 0.2, 0.1])
        self.assertAlmostEqual(material.diffusion.getField("Depth"), 2.5, places=6)
        self.scene.apply_material("cube", {"material_type": "opaque"})
        self.assertEqual(material.diffusion.name, "Lambertian")

    def test_an_auto_scatter_depth_keeps_toolbags_own(self):
        mesh, built = self._cube()
        self.scene.apply_mesh("cube", mesh, built)
        material = self.scene.apply_material("cube", {
            "material_type": "subsurface", "subsurface_depth": -1})
        self.assertAlmostEqual(material.diffusion.getField("Depth"), 1.0, places=6)

    def test_removing_a_map_takes_its_factor_with_it(self):
        for paint in (False, True):
            fake_mset.reset()
            self.scene = scene_module.Scene(log=self.logged.append)
            mesh, built = self._cube()
            self.scene.apply_mesh("cube", mesh, built)
            for blob in ("c", "o", "a", "e"):
                self.scene.store_blob(blob, blob + ".png", b"\x89PNG fake")
            tinted = {"color": [1.0, 1.0, 1.0], "opacity": 1.0, "textures": {
                "color": {"texture_id": "c", "factor": [1.0, 0.0, 0.0]},
                "occlusion": {"texture_id": "o", "factor": 0.3},
                "opacity": {"texture_id": "a", "factor": 0.5},
                "emissive": {"texture_id": "e", "strength": 4.0}}}
            material = self.scene.apply_material("cube", tinted, has_paint=paint)
            self.assertEqual(material.albedo.getField("Color"), [1.0, 0.0, 0.0])
            cleared = {"color": [1.0, 1.0, 1.0], "opacity": 1.0, "textures": {
                "color": {}, "occlusion": {}, "opacity": {}, "emissive": {}}}
            self.scene.apply_material("cube", cleared, has_paint=paint)
            self.assertEqual(material.albedo.getField("Color"), [1.0, 1.0, 1.0])
            self.assertAlmostEqual(material.occlusion.getField("Occlusion"), 1.0, places=6)
            self.assertAlmostEqual(material.transparency.getField("Alpha"), 1.0, places=6)
            self.assertAlmostEqual(material.emission.getField("Intensity"), 0.0, places=6)

    def test_blob_names_cannot_escape_the_cache_folder(self):
        path = self.scene.store_blob("evil", "../../etc/passwd", b"x")
        self.assertEqual(os.path.dirname(path), self.scene._folder)
        self.assertNotIn("..", os.path.basename(path))

    def test_material_names_stay_unique(self):
        mesh, built = self._cube()
        self.scene.apply_mesh("a", mesh, built)
        self.scene.apply_mesh("b", mesh, built)
        first = self.scene.apply_material("a", {"roughness": 0.1})
        second = self.scene.apply_material("b", {"roughness": 0.2})
        self.assertNotEqual(first.name, second.name)

    # ----------------------------------------------------------------- shading

    def test_environment_reaches_the_skybox(self):
        sky = fake_mset.SkyBoxObject()
        path = self.scene.store_blob("env1", "studio.hdr", b"fake hdr")
        self.assertTrue(self.scene.apply_shading({
            "environment_id": "env1", "environment_rotation": 90.0, "background_blur": 0.4}))
        self.assertEqual(sky.image, path)
        self.assertAlmostEqual(sky.rotation, 90.0)
        self.assertAlmostEqual(sky.blur, 0.4)

    # ------------------------------------------------------------------ camera

    def test_camera_is_left_alone_until_the_user_opts_in(self):
        fake_mset.CameraObject("Main Camera")
        self.assertFalse(self.scene.apply_camera({"world_from_view": convert.IDENTITY}))

    def test_camera_follows_when_enabled(self):
        camera = fake_mset.CameraObject("Main Camera")
        self.scene.follow_view = True
        matrix = list(convert.IDENTITY)
        matrix[12], matrix[13], matrix[14] = 1.0, 2.0, 3.0
        self.assertTrue(self.scene.apply_camera({"world_from_view": matrix, "fov_y": 35.0}))
        self.assertEqual(camera.position, [1.0, 2.0, 3.0])
        self.assertAlmostEqual(camera.fov, 35.0)

    def test_euler_recovers_a_known_rotation(self):
        angle = math.radians(30.0)
        matrix = list(convert.IDENTITY)  # rotate about y, column-major
        matrix[0], matrix[2] = math.cos(angle), -math.sin(angle)
        matrix[8], matrix[10] = math.sin(angle), math.cos(angle)
        x, y, z = scene_module.euler_from_matrix(matrix)
        self.assertAlmostEqual(x, 0.0, places=4)
        self.assertAlmostEqual(y, 30.0, places=4)
        self.assertAlmostEqual(z, 0.0, places=4)

    def test_euler_round_trips_the_order_probe_py_measures(self):
        """R = Ry*Rx*Rz, i.e. z then x then y: what Toolbag 5.032 really does."""
        angles = [30.0, 40.0, 50.0]

        def spin(point):
            x, y, z = (math.radians(a) for a in angles)
            for sin, cos, axis in ((math.sin(z), math.cos(z), "z"),
                                   (math.sin(x), math.cos(x), "x"),
                                   (math.sin(y), math.cos(y), "y")):
                a, b, c = point
                if axis == "x":
                    point = (a, cos * b - sin * c, sin * b + cos * c)
                elif axis == "y":
                    point = (cos * a + sin * c, b, -sin * a + cos * c)
                else:
                    point = (cos * a - sin * b, sin * a + cos * b, c)
            return point

        matrix = []
        for basis in ((1, 0, 0), (0, 1, 0), (0, 0, 1)):
            matrix.extend(list(spin(basis)) + [0.0])
        matrix.extend([0.0, 0.0, 0.0, 1.0])
        for got, want in zip(scene_module.euler_from_matrix(matrix), angles):
            self.assertAlmostEqual(got, want, places=4)

    def test_euler_ignores_scale(self):
        matrix = list(convert.IDENTITY)
        matrix[0] = matrix[5] = matrix[10] = 4.0
        self.assertEqual([round(a, 6) for a in scene_module.euler_from_matrix(matrix)],
                         [0.0, 0.0, 0.0])


if __name__ == "__main__":
    unittest.main()
