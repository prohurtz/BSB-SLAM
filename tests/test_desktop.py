"""Standalone setup progression and safe driver installation regressions."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from app import floor_status_text, setup_step
import control_backend as backend


class DesktopTests(unittest.TestCase):
    def test_floor_prompt_explains_why_calibration_is_unavailable(self):
        state = {}
        self.assertIn("start steamvr", floor_status_text(state).lower())
        state["driver_online"] = True
        self.assertIn("settle", floor_status_text(state))
        state.update(slam_ready=True, settled=True, floor_scan=1)
        self.assertIn("Keep still", floor_status_text(state))
        state["floor_scan"] = 2
        self.assertIn("calibrated", floor_status_text(state))

    def test_setup_progresses_only_when_prerequisites_are_ready(self):
        state = {}
        self.assertIsNone(setup_step(state)[1])
        state.update(sdk_ready=True, steamvr_available=True, steam_settings_ready=True, driver_source_ready=True)
        self.assertEqual(setup_step(state)[1], "setup_tracking")
        state["steamvr_running"] = True
        self.assertIsNone(setup_step(state)[1])
        state.update(installed=True, steamvr_running=False)
        self.assertEqual(setup_step(state)[1], "setup_tracking")
        state["override_enabled"] = True
        self.assertEqual(setup_step(state)[1], "launch_steamvr")
        state.update(steamvr_running=True, driver_online=True, slam_ready=True, settled=True)
        self.assertEqual(setup_step(state).action, "start_floor_scan")
        self.assertEqual(setup_step(state).title, "Set the floor height")
        self.assertIn("floor", setup_step(state)[0])
        state["floor_scan"] = 2
        self.assertEqual(setup_step(state).title, "Ready to track")

    def test_installed_bundle_registers_without_copying_files_onto_themselves(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            driver = root / "driver"
            binary = driver / "bin/win64"
            binary.mkdir(parents=True)
            for name in ("driver_BSB-SLAM.dll", "xvsdk.dll", "dependency.dll"):
                (binary / name).write_bytes(b"test")
            (driver / "driver.vrdrivermanifest").write_text('{"name": "BSB-SLAM"}')
            tool = root / "bin/win64/vrpathreg.exe"
            tool.parent.mkdir(parents=True)
            tool.write_bytes(b"test")
            registry = root / "openvrpaths.vrpath"
            def register(*args, **kwargs):
                registry.write_text(json.dumps({"external_drivers": [str(driver)]}))
            with mock.patch.object(backend.sdk_setup, "sdk_bin", return_value=binary), \
                 mock.patch.object(backend, "openvr_registry", return_value=registry), \
                 mock.patch.object(backend, "ROOT", root), \
                 mock.patch.object(backend, "steamvr_running", return_value=False), \
                 mock.patch.object(backend, "steam_root", return_value=root), \
                 mock.patch.object(backend, "steamvr_root", return_value=root), \
                 mock.patch.object(backend, "driver_path", return_value=driver), \
                 mock.patch.object(backend.subprocess, "run", side_effect=register) as run:
                backend.install_driver()
            self.assertEqual(run.call_args.args[0], [str(tool), "adddriver", str(driver)])
            self.assertEqual((binary / "xvsdk.dll").read_bytes(), b"test")

    def test_uninstall_removes_only_this_tracking_override(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config").mkdir()
            settings = root / "config/steamvr.vrsettings"
            settings.write_text(json.dumps({"TrackingOverrides": {backend.OVERRIDE_KEY: "/user/head", "other": "keep"}}))
            with mock.patch.object(backend, "steamvr_running", return_value=False), \
                 mock.patch.object(backend, "steam_root", return_value=root), \
                 mock.patch.object(backend, "steamvr_root", return_value=None):
                backend.unregister_driver()
            self.assertEqual(json.loads(settings.read_text())["TrackingOverrides"], {"other": "keep"})

    def test_uninstall_refuses_changes_while_steamvr_runs(self):
        with mock.patch.object(backend, "steamvr_running", return_value=True), \
             mock.patch.object(backend, "set_override") as override:
            with self.assertRaises(RuntimeError):
                backend.unregister_driver()
            override.assert_not_called()
