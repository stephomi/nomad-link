# SPDX-License-Identifier: MIT
"""The panel builds, ticks and shuts down without a live Toolbag."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "NomadLink"))

import fake_mset  # noqa: E402

sys.modules["mset"] = fake_mset

import scene as scene_module  # noqa: E402

scene_module.mset = fake_mset

import client as client_module  # noqa: E402
import panel as panel_module  # noqa: E402

panel_module.mset = fake_mset


class PanelTest(unittest.TestCase):
    def setUp(self):
        fake_mset.reset()
        import tempfile
        self.settings = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.settings.close()
        client_module._settings_path = lambda: self.settings.name
        self.link = client_module.Client()
        self.panel = panel_module.Panel(self.link)

    def tearDown(self):
        os.unlink(self.settings.name)

    def test_the_address_field_is_prefilled_from_the_last_session(self):
        client_module._save_address("192.168.1.9", 40000)
        panel = panel_module.Panel(client_module.Client())
        self.assertEqual(panel.host.value, "192.168.1.9")
        self.assertEqual(panel.port.value, 40000)

    def test_a_first_run_prefills_nothing_and_offers_find(self):
        self.assertEqual(self.panel.host.value, "")
        self.assertEqual(self.panel.port.value, 48312)
        self.assertIsNotNone(self.panel.find_button)

    def test_find_takes_over_from_whatever_was_typed(self):
        # the field is never rewritten (Toolbag reformats it through a float);
        # a successful Find makes its address the one Connect dials
        self.link.find = lambda port=48312: (setattr(self.link, "host", "10.0.0.4"),
                                             setattr(self.link, "port", port), True)[-1]
        self.panel.host.value = "192.2.junk"
        self.panel.on_host_edited()
        self.panel.on_find()
        calls = []
        self.link.connect = lambda host, port: calls.append(host)
        self.panel.on_connect()
        self.assertEqual(calls, ["10.0.0.4"])

    def test_a_junk_port_falls_back_to_the_default(self):
        self.panel.port.value = "not a number"
        self.assertEqual(self.panel._port(), 48312)

    def test_the_window_gets_its_controls(self):
        self.assertIsNotNone(self.panel.window)
        self.assertGreaterEqual(len(self.panel.window.getElements()), 6)

    def test_ticking_while_disconnected_is_harmless(self):
        self.panel.update()
        self.assertEqual(self.panel.status.text, "Disconnected")

    def test_follow_view_checkbox_reaches_the_scene(self):
        self.panel.follow.value = True
        self.panel.on_follow()
        self.assertTrue(self.link.scene.follow_view)

    def test_connect_button_uses_the_typed_address(self):
        calls = []
        self.link.connect = lambda host, port: calls.append((host, port))
        self.panel.host.value = " 192.168.1.5 "
        self.panel.on_host_edited()   # Toolbag fires onChange when the user types
        self.panel.host.value = "192.2"   # then the field mangles its own text
        self.panel.port.value = 40000
        self.panel.on_connect()
        self.assertEqual(calls, [("192.168.1.5", 40000)])

    def test_a_widget_mangled_address_is_not_dialed(self):
        # Toolbag's text field commits "192.168.1.16" back as "192.2"; without an
        # edit from the user, the client's own string wins over the readback
        calls = []
        self.link.connect = lambda host, port: calls.append(host)
        self.link.host = "192.168.1.16"
        self.panel.refresh_host()
        self.panel.host.value = "192.2"   # the widget reformatting, no onChange
        self.panel.on_connect()
        self.assertEqual(calls, ["192.168.1.16"])

    def test_one_button_offers_disconnect_once_the_user_is_connected(self):
        self.assertEqual(self.panel.connect_button.text, "Connect")
        self.link._wanted = True
        self.panel.update()
        self.assertEqual(self.panel.connect_button.text, "Disconnect")

    def test_that_button_disconnects_while_it_says_disconnect(self):
        calls = []
        self.link.disconnect = lambda: calls.append("off")
        self.link.connect = lambda host, port: calls.append("on")
        self.panel.on_toggle()
        self.link._wanted = True
        self.panel.on_toggle()
        self.assertEqual(calls, ["on", "off"])

    def test_detail_line_reports_the_session(self):
        self.link.connection.status = "Connected"
        self.link.counts["meshes"] = 3
        self.link.session_config = {"live_sync": True, "source_name": "Nomad iPad"}
        self.assertIn("3 meshes", self.panel._detail())
        self.assertIn("Nomad iPad", self.panel._detail())

    def test_shutdown_releases_the_callbacks(self):
        fake_mset.callbacks.onPeriodicUpdate = self.panel.update
        self.panel.shutdown()
        self.assertIsNone(fake_mset.callbacks.onPeriodicUpdate)


if __name__ == "__main__":
    unittest.main()
