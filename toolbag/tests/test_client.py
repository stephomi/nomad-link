# SPDX-License-Identifier: MIT
"""The whole bridge over a real socket: handshake, scene traffic, recovery."""
import os
import socket
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "NomadLink"))

import fake_mset  # noqa: E402

sys.modules["mset"] = fake_mset

import convert  # noqa: E402
import scene as scene_module  # noqa: E402

scene_module.mset = fake_mset

import client as client_module  # noqa: E402
from mock_nomad import MockNomad, wait  # noqa: E402
from test_convert import cube, encode_rgbm  # noqa: E402


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class ClientTest(unittest.TestCase):
    def setUp(self):
        fake_mset.reset()
        self.tokens = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tokens.close()
        client_module._settings_path = lambda: self.tokens.name

        self.port = free_port()
        self.nomad = MockNomad(self.port, capabilities=["scene_edits", "texture", "asset"])
        self.nomad.start()
        self.link = client_module.Client()
        self.link.connect("127.0.0.1", self.port)
        self.assertTrue(wait(self.link, lambda: self.link.connected and self.link.nomad_version))

    def tearDown(self):
        self.link.disconnect()
        self.nomad.stop()
        os.unlink(self.tokens.name)

    def objects(self):
        return fake_mset.getAllObjectsOfType("MeshObject")

    def send_cube(self, **kwargs):
        header, binary = cube(**kwargs)
        self.nomad.send(header, binary)
        self.assertTrue(wait(self.link, lambda: "cube" in self.link.meshes))
        return header, binary

    # -------------------------------------------------------------- handshake

    def test_hello_is_honest_about_what_this_bridge_does(self):
        advertised = set(self.nomad.hello["capabilities"])
        self.assertEqual(self.nomad.hello["client_name"], "Marmoset Toolbag")
        self.assertEqual(self.nomad.hello["protocol"], 1)
        self.assertIn("scene_edits", advertised)     # or Nomad streams nothing live
        self.assertIn("texture", advertised)
        # nothing is ever sent back, so these must not be claimed
        self.assertNotIn("selection_transfer", advertised)
        self.assertNotIn("scene_transfer", advertised)

    def test_pair_token_is_stored_for_a_silent_reconnect(self):
        import json
        with open(self.tokens.name) as handle:
            self.assertEqual(json.load(handle)["tokens"]["127.0.0.1"], "token123")

    def test_the_address_is_remembered_for_the_next_session(self):
        self.assertEqual(client_module.saved_address(), ("127.0.0.1", self.port))

    def test_autoconnect_redials_the_saved_address(self):
        self.link.disconnect()
        again = client_module.Client()
        try:
            self.assertTrue(again.autoconnect())
            self.assertEqual(again.host, "127.0.0.1")
        finally:
            again.disconnect()

    # ------------------------------------------------------------------ meshes

    def test_a_sent_mesh_becomes_a_toolbag_object(self):
        self.send_cube()
        objects = self.objects()
        self.assertEqual(len(objects), 1)
        self.assertEqual(objects[0].name, "Cube")
        self.assertEqual(len(objects[0].mesh.vertices), 24)
        self.assertEqual(self.link.counts["meshes"], 1)

    def test_mesh_ack_is_returned_when_asked(self):
        header, binary = cube()
        header["request_id"] = "req-1"
        self.nomad.send(header, binary)
        self.assertTrue(wait(self.link, lambda: self.nomad.first("mesh_ack")[0] is not None))
        self.assertEqual(self.nomad.first("mesh_ack")[0]["mesh_id"], "cube")

    def test_a_stroke_moves_the_vertices_in_place(self):
        self.send_cube()
        obj = self.objects()[0]
        binary = bytearray(struct.pack("<I", 0))
        header = {"type": "mesh_delta", "mesh_id": "cube", "count": 1, "vertex_count": 8,
                  "index_offset": 0, "index_format": "uint32", "position_offset": 4,
                  "position_format": "float32x3", "live_sync": True}
        binary += struct.pack("<3f", 5.0, 5.0, 5.0)
        self.nomad.send(header, bytes(binary))
        self.assertTrue(wait(self.link, lambda: obj.mesh.vertices[0] == 5.0))
        self.assertEqual(self.link.counts["updates"], 1)
        self.assertEqual(len(self.objects()), 1)  # patched, not duplicated

    def _delta(self, index, position):
        header = {"type": "mesh_delta", "mesh_id": "cube", "count": 1, "vertex_count": 8,
                  "index_offset": 0, "index_format": "uint32", "position_offset": 4,
                  "position_format": "float32x3", "live_sync": True}
        return header, struct.pack("<I3f", index, *position)

    def test_a_burst_of_deltas_costs_one_write(self):
        # Toolbag rebuilds the mesh adjacency on every write, and only the state
        # the deltas add up to is ever seen
        self.send_cube()
        obj = self.objects()[0]
        writes = []
        original = self.link.scene.update_geometry
        self.link.scene.update_geometry = lambda link_id, built: (
            writes.append(link_id), original(link_id, built))[-1]
        batch = [self._delta(0, (5.0, 5.0, 5.0)),
                 self._delta(1, (6.0, 6.0, 6.0)),
                 self._delta(0, (7.0, 7.0, 7.0))]
        self.link.connection.poll = lambda batches=[batch]: batches.pop() if batches else []
        self.link.pump()
        self.assertEqual(writes, ["cube"])
        self.assertEqual(self.link.counts["updates"], 3)
        self.assertEqual(obj.mesh.vertices[0], 7.0)     # the last word on vertex 0
        self.assertEqual(obj.mesh.vertices[3], 6.0)     # and vertex 1 is not lost

    def test_a_full_mesh_cancels_the_delta_it_arrived_with(self):
        self.send_cube()
        writes = []
        original = self.link.scene.update_geometry
        self.link.scene.update_geometry = lambda link_id, built: (
            writes.append(link_id), original(link_id, built))[-1]
        header, binary = cube()
        batch = [self._delta(0, (5.0, 5.0, 5.0)), (header, binary)]
        self.link.connection.poll = lambda batches=[batch]: batches.pop() if batches else []
        self.link.pump()
        self.assertEqual(writes, [])        # apply_mesh wrote the whole thing instead
        self.assertFalse(self.link._dirty)

    def test_the_pump_measures_itself(self):
        self.link._window = [time.time() - 1.5, 3, 6, 0.3]   # a window ready to close
        self.link.pump()
        self.assertGreater(self.link.stats["rate"], 0.0)
        self.assertGreater(self.link.stats["busy"], 0.0)
        self.assertEqual(self.link.stats["packets"], 6)

    def test_a_delta_for_stale_topology_asks_for_a_full_mesh(self):
        self.send_cube()
        header = {"type": "mesh_delta", "mesh_id": "cube", "count": 1, "vertex_count": 999,
                  "index_offset": 0, "position_offset": 4}
        self.nomad.send(header, struct.pack("<I3f", 0, 0.0, 0.0, 0.0))
        self.assertTrue(wait(self.link, lambda: self.nomad.first("request_mesh")[0] is not None))
        self.assertEqual(self.nomad.first("request_mesh")[0]["link_id"], "cube")

    def test_paint_refresh_recolors_without_rebuilding(self):
        self.send_cube()
        obj = self.objects()[0]
        binary = encode_rgbm([(0.0, 1.0, 0.0)] * 8)
        self.nomad.send({"type": "mesh_attributes", "mesh_id": "cube",
                         "vertex_count": 8, "color_offset": 0}, binary)
        self.assertTrue(wait(self.link, lambda: obj.mesh.colors[1] > 0.9))
        self.assertLess(obj.mesh.colors[0], 0.1)

    def test_instances_share_the_geometry_at_their_own_place(self):
        self.send_cube()
        matrix = list(convert.IDENTITY)
        matrix[12] = 10.0
        self.nomad.send({"type": "mesh_instance", "mesh_id": "copy", "geometry_id": "geo",
                         "name": "Cube copy", "world_matrix": matrix})
        self.assertTrue(wait(self.link, lambda: len(self.objects()) == 2))
        copy = next(o for o in self.objects() if o.name == "Cube copy")
        self.assertAlmostEqual(min(copy.mesh.vertices[0::3]), 9.0, places=5)

    def test_a_stroke_redraws_every_instance_of_that_geometry(self):
        self.send_cube()
        matrix = list(convert.IDENTITY)
        matrix[12] = 10.0
        self.nomad.send({"type": "mesh_instance", "mesh_id": "copy", "geometry_id": "geo",
                         "name": "Cube copy", "world_matrix": matrix})
        self.assertTrue(wait(self.link, lambda: len(self.objects()) == 2))
        copy = next(o for o in self.objects() if o.name == "Cube copy")

        binary = bytearray(struct.pack("<I", 0))
        header = {"type": "mesh_delta", "mesh_id": "cube", "count": 1, "vertex_count": 8,
                  "index_offset": 0, "position_offset": 4, "live_sync": True}
        binary += struct.pack("<3f", 0.0, 7.0, 0.0)
        self.nomad.send(header, bytes(binary))
        # the copy is placed 10 along x, so the moved vertex lands at y = 7 there too
        self.assertTrue(wait(self.link, lambda: max(copy.mesh.vertices[1::3]) == 7.0))

    def test_an_unknown_instance_asks_for_its_geometry(self):
        self.nomad.send({"type": "mesh_instance", "mesh_id": "copy", "geometry_id": "nope"})
        self.assertTrue(wait(self.link, lambda: self.nomad.first("request_mesh")[0] is not None))

    def test_moving_an_object_rebakes_its_vertices(self):
        self.send_cube()
        obj = self.objects()[0]
        matrix = list(convert.IDENTITY)
        matrix[13] = 4.0
        self.nomad.send({"type": "object_state", "link_id": "cube", "name": "Moved",
                         "world_matrix": matrix, "live_sync": True})
        self.assertTrue(wait(self.link, lambda: obj.name == "Moved"))
        self.assertAlmostEqual(min(obj.mesh.vertices[1::3]), 3.0, places=5)

    def test_delete_removes_it_from_the_scene(self):
        self.send_cube()
        self.nomad.send({"type": "object_delete", "link_id": "cube", "live_sync": True})
        self.assertTrue(wait(self.link, lambda: not self.objects()))
        self.assertEqual(self.link.counts["meshes"], 0)

    # ------------------------------------------------------------------- looks

    def test_material_and_its_texture_land_on_the_object(self):
        self.send_cube()
        self.nomad.send({"type": "texture", "texture_id": "tex1", "name": "skin.png"},
                        b"\x89PNG fake bytes")
        self.nomad.send({"type": "material", "mesh_id": "cube", "material": {
            "roughness": 0.2,
            "textures": {"color": {"texture_id": "tex1", "name": "skin.png"}}}})
        self.assertTrue(wait(self.link, lambda: self.link.scene.materials.get("cube")))
        material = self.link.scene.materials["cube"]
        self.assertAlmostEqual(material.microsurface.getField("Roughness"), 0.2, places=6)
        self.assertTrue(str(self.albedo_map()).endswith("skin.png"))

    def albedo_map(self):
        """The assigned color map, or None while the slot has no such field."""
        material = self.link.scene.materials.get("cube")
        slot = material.albedo if material else None
        if slot is None or "Albedo Map" not in slot.getFieldNames():
            return None
        texture = slot.getField("Albedo Map")
        return getattr(texture, "path", texture)

    def test_a_texture_arriving_late_is_applied_when_it_lands(self):
        self.send_cube()
        self.nomad.send({"type": "material", "mesh_id": "cube", "material": {
            "textures": {"color": {"texture_id": "late", "name": "late.png"}}}})
        self.assertTrue(wait(self.link, lambda: self.nomad.first("request_texture")[0] is not None))
        self.assertIsNone(self.albedo_map())   # paint stands in until the blob arrives
        self.nomad.send({"type": "texture", "texture_id": "late", "name": "late.png"}, b"bytes")
        self.assertTrue(wait(self.link, self.albedo_map))

    def test_environment_asset_is_requested_then_applied(self):
        sky = fake_mset.SkyBoxObject()
        self.nomad.send({"type": "shading_config", "shading": {
            "environment_id": "env9", "environment_rotation": 45.0}})
        self.assertTrue(wait(self.link, lambda: self.nomad.first("request_asset")[0] is not None))
        self.assertEqual(self.nomad.first("request_asset")[0]["collection"], "environments")
        self.nomad.send({"type": "asset", "collection": "environments", "asset_id": "env9",
                         "name": "studio.hdr"}, b"fake hdr bytes")
        self.assertTrue(wait(self.link, lambda: sky.image))
        self.assertAlmostEqual(sky.rotation, 45.0)

    def test_lights_and_cameras_arrive_and_partial_edits_merge(self):
        self.nomad.send({"type": "light", "link_id": "sun", "light_type": "SUN",
                         "intensity": 3.0, "name": "Key"})
        self.assertTrue(wait(self.link, lambda: "sun" in self.link.lights))
        light = self.link.scene.objects["sun"]
        self.assertEqual((light.lightType, light.brightness), ("directional", 3.0))
        self.nomad.send({"type": "light", "link_id": "sun", "intensity": 0.5})
        self.assertTrue(wait(self.link, lambda: light.brightness == 0.5))
        self.assertEqual(light.lightType, "directional")   # merged, not replaced

        self.nomad.send({"type": "camera_object", "link_id": "cam",
                         "name": "Shot", "fov_y": 35.0})
        self.assertTrue(wait(self.link, lambda: "cam" in self.link.cameras))
        self.assertEqual(self.link.scene.objects["cam"].fov, 35.0)

        self.nomad.send({"type": "object_delete", "link_id": "sun"})
        self.assertTrue(wait(self.link, lambda: "sun" not in self.link.scene.objects))
        self.assertNotIn("sun", self.link.lights)

    def test_get_asks_nomad_for_the_scene(self):
        self.link.request("scene")
        self.assertTrue(wait(self.link, lambda: self.nomad.first("request_scene")[0]))
        self.link.request("selection")
        self.assertTrue(wait(self.link, lambda: self.nomad.first("request_selection")[0]))

    def test_replace_all_drops_what_the_bridge_put_there_first(self):
        self.send_cube()
        obj = self.objects()[0]
        self.link.request("scene", replace=True)
        self.assertTrue(obj.destroyed)
        self.assertEqual(self.objects(), [])
        self.assertEqual(self.link.meshes, {})
        self.assertTrue(wait(self.link, lambda: self.nomad.first("request_scene")[0]))

    def test_every_message_is_named_on_disk_before_it_is_handled(self):
        # a crash inside Toolbag leaves no traceback; the file has to say what
        # was in flight, and 'queue drained' means the crash was Toolbag's own
        trace = tempfile.NamedTemporaryFile(suffix=".txt", delete=False)
        trace.close()
        self.addCleanup(os.unlink, trace.name)
        self.link.scene.trace_path = trace.name
        self.send_cube()
        with open(trace.name) as handle:
            steps = handle.read().split()
        self.assertIn("mesh_full", steps)
        self.assertEqual(steps[-2:], ["queue", "drained"])

    def test_camera_messages_are_ignored_until_the_user_opts_in(self):
        camera = fake_mset.CameraObject("Main Camera")
        self.nomad.send({"type": "camera", "world_from_view": convert.IDENTITY, "fov_y": 20.0})
        self.assertTrue(wait(self.link, lambda: True, seconds=0.3))
        self.assertAlmostEqual(camera.fov, 50.0)
        self.link.scene.follow_view = True
        self.nomad.send({"type": "camera", "world_from_view": convert.IDENTITY, "fov_y": 20.0})
        self.assertTrue(wait(self.link, lambda: camera.fov == 20.0))

    # ---------------------------------------------------------------- plumbing

    def test_session_config_is_replicated_for_the_panel(self):
        self.nomad.send({"type": "session_config", "revision": 3, "live_sync": True,
                         "source_name": "Nomad iPad"})
        self.assertTrue(wait(self.link, lambda: self.link.session_config.get("revision") == 3))
        self.assertEqual(self.link.session_config["source_name"], "Nomad iPad")

    def test_the_shared_view_flag_drives_camera_following(self):
        self.nomad.send({"type": "session_config", "revision": 3, "sync_mode": "auto",
                         "sync_view": True})
        self.assertTrue(wait(self.link, lambda: self.link.scene.follow_view))
        self.nomad.send({"type": "session_config", "revision": 4, "sync_mode": "auto",
                         "sync_view": False})
        self.assertTrue(wait(self.link, lambda: not self.link.scene.follow_view))

    def test_a_stale_config_echo_does_not_undo_the_current_one(self):
        self.nomad.send({"type": "session_config", "revision": 7, "sync_mode": "auto",
                         "sync_view": True})
        self.assertTrue(wait(self.link, lambda: self.link.scene.follow_view))
        self.nomad.send({"type": "session_config", "revision": 2, "sync_mode": "auto",
                         "sync_view": False})
        self.assertTrue(wait(self.link, lambda: True, seconds=0.3))
        self.assertTrue(self.link.scene.follow_view)
        self.assertEqual(self.link.config_revision, 7)

    def test_following_asks_nomad_instead_of_deciding_alone(self):
        self.nomad.send({"type": "session_config", "revision": 5, "sync_mode": "nomad",
                         "live_sync": True, "sync_view": False, "sync_objects": True,
                         "sync_lights": False, "source_name": "Nomad iPad"})
        self.assertTrue(wait(self.link, lambda: self.link.config_revision == 5))
        self.link.set_sync_view(True)
        self.assertTrue(wait(self.link,
                             lambda: self.nomad.first("set_session_config")[0] is not None))
        asked = self.nomad.first("set_session_config")[0]
        self.assertEqual(asked["base_revision"], 5)
        self.assertTrue(asked["sync_view"])
        self.assertEqual(asked["sync_mode"], "nomad")   # or Nomad refuses the message
        self.assertTrue(asked["sync_objects"])          # the other flags travel untouched
        self.assertFalse(asked["sync_lights"])
        self.assertTrue(asked["live_sync"])
        self.assertNotIn("source_name", asked)          # informational, not a setting

    def test_the_checkbox_still_works_before_any_config_arrives(self):
        self.link.set_sync_view(True)
        self.assertTrue(self.link.scene.follow_view)
        self.assertIsNone(self.nomad.first("set_session_config")[0])

    def test_a_ping_is_answered(self):
        self.nomad.send({"type": "ping"})
        self.assertTrue(wait(self.link, lambda: self.nomad.first("pong")[0] is not None))

    def test_an_unknown_message_type_is_ignored(self):
        self.nomad.send({"type": "something_from_a_newer_nomad", "whatever": 1})
        self.send_cube()  # the connection still works afterwards
        self.assertEqual(len(self.objects()), 1)

    def test_a_broken_packet_does_not_kill_the_pump(self):
        self.nomad.send({"type": "mesh_full", "mesh_id": "bad", "vertex_count": 99,
                         "face_count": 1, "position_offset": 0}, b"too short")
        self.assertTrue(wait(self.link, lambda: self.link.log))
        self.send_cube()
        self.assertEqual(len(self.objects()), 1)

    def test_an_error_clears_the_request_caches(self):
        self.send_cube()
        self.link._requested.add("cube")
        self.nomad.send({"type": "error", "message": "nope"})
        self.assertTrue(wait(self.link, lambda: not self.link._requested))


if __name__ == "__main__":
    unittest.main()
