# SPDX-License-Identifier: MIT
"""The entry point must survive how Toolbag actually runs it.

Toolbag compiles a plugin as "<string>", so __file__ is not defined and the
folder has to come from mset.getPluginPath().
"""
import os
import sys
import tempfile
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

PLUGIN = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "NomadLink"))


class MainTest(unittest.TestCase):
    def setUp(self):
        fake_mset.reset()
        client_module._client = None
        self.tokens = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tokens.close()
        client_module._settings_path = lambda: self.tokens.name  # never dial a real Nomad
        self.saved = list(sys.path)

    def tearDown(self):
        link = client_module._client
        if link is not None:
            link.disconnect()
        client_module._client = None
        fake_mset.callbacks.onPeriodicUpdate = None
        fake_mset.PLUGIN_PATH = ""
        sys.path[:] = self.saved
        os.unlink(self.tokens.name)

    def run_main(self, plugin_path):
        fake_mset.PLUGIN_PATH = plugin_path
        with open(os.path.join(PLUGIN, "__main__.py")) as handle:
            source = handle.read()
        namespace = {"__name__": "__main__"}   # exactly what Toolbag provides
        exec(compile(source, "<string>", "exec"), namespace)
        return namespace

    def test_it_starts_when_toolbag_gives_the_entry_file(self):
        self.run_main(os.path.join(PLUGIN, "__main__.py"))
        self.assertIsNotNone(fake_mset.callbacks.onPeriodicUpdate)
        self.assertIsNotNone(client_module._client)

    def test_it_starts_when_toolbag_gives_the_folder(self):
        self.run_main(PLUGIN)
        self.assertIsNotNone(fake_mset.callbacks.onPeriodicUpdate)

    def test_the_periodic_callback_ticks_without_a_connection(self):
        self.run_main(PLUGIN)
        fake_mset.callbacks.onPeriodicUpdate()
        fake_mset.callbacks.onPeriodicUpdate()

    def test_shutdown_releases_the_callback(self):
        self.run_main(PLUGIN)
        fake_mset.callbacks.onShutdownPlugin()
        self.assertIsNone(fake_mset.callbacks.onPeriodicUpdate)

    def test_no_requirements_file_ships(self):
        # Toolbag tries to pip install it at launch and fails loudly; the plugin
        # has no dependencies to declare
        self.assertFalse(os.path.exists(os.path.join(PLUGIN, "requirements.txt")))


if __name__ == "__main__":
    unittest.main()
