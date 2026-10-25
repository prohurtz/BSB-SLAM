"""Verify setup failure recovery using an isolated Steam and driver installation."""
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import control_backend as backend


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.bundle = self.root / "app/payload/driver"
        self.target = self.root / "local/driver"
        self.steam = self.root / "steam"
        self.registry = self.root / "openvrpaths.vrpath"
        self.settings = self.steam / "config/steamvr.vrsettings"
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps({"steamvr": {"ipd": .064}, "BSB-SLAM": {"offset_x": .2},
                                             "TrackingOverrides": {"other": "keep"}}))
        self.settings_before = self.settings.read_bytes()
        self.registry.write_text(json.dumps({"external_drivers": ["other-driver"]}))
        self.registry_before = self.registry.read_bytes()
        for folder, content in ((self.bundle, b"new"), (self.target, b"previous")):
            binary = folder / "bin/win64"
            binary.mkdir(parents=True)
            for name in ("driver_BSB-SLAM.dll", "xvsdk.dll", "dependency.dll"):
                (binary / name).write_bytes(content)
            (folder / "driver.vrdrivermanifest").write_text('{"name": "BSB-SLAM"}')
        tool = self.steam / "bin/win64/vrpathreg.exe"
        tool.parent.mkdir(parents=True)
        tool.write_bytes(b"tool")
        self.stack.enter_context(mock.patch.multiple(backend, ROOT=self.root / "app",
            steam_root=mock.Mock(return_value=self.steam), steamvr_root=mock.Mock(return_value=self.steam),
            driver_path=mock.Mock(return_value=self.target), openvr_registry=mock.Mock(return_value=self.registry),
            steamvr_running=mock.Mock(return_value=False)))
        self.stack.enter_context(mock.patch.object(backend.sdk_setup, "sdk_bin", return_value=self.bundle / "bin/win64"))
        self.run = self.stack.enter_context(mock.patch.object(backend.subprocess, "run", side_effect=self.register))

    def register(self, *args, **kwargs):
        self.registry.write_text(json.dumps({"external_drivers": ["other-driver", str(self.target)]}))

    def assert_previous_intact(self):
        self.assertEqual((self.target / "bin/win64/driver_BSB-SLAM.dll").read_bytes(), b"previous")
        self.assertEqual(self.registry.read_bytes(), self.registry_before)
        self.assertEqual(self.settings.read_bytes(), self.settings_before)

    def test_one_step_setup_preserves_other_settings_and_retries_safely(self):
        backend.install_driver(enable_tracking=True)
        settings = json.loads(self.settings.read_text())
        self.assertEqual(settings["TrackingOverrides"], {"other": "keep", backend.OVERRIDE_KEY: "/user/head"})
        self.assertEqual(settings["steamvr"], {"ipd": .064})
        self.assertEqual(settings["BSB-SLAM"], {"offset_x": .2})
        self.assertEqual((self.target / "bin/win64/driver_BSB-SLAM.dll").read_bytes(), b"new")
        backup = Path(str(self.settings) + ".before-beyond-slam.bak")
        self.assertEqual(backup.read_bytes(), self.settings_before)
        backend.install_driver(enable_tracking=True)
        self.assertEqual(backup.read_bytes(), self.settings_before)
        self.assertEqual(list(self.target.parent.glob("driver-setup-*")), [])

    def test_failed_registration_restores_files_registry_settings_and_legacy_driver(self):
        legacy = self.steam / "drivers/BSB-SLAM"
        legacy.mkdir(parents=True)
        (legacy / "marker").write_text("legacy")
        def fail(*args, **kwargs):
            self.register()
            raise subprocess.CalledProcessError(1, "vrpathreg")
        self.run.side_effect = fail
        with self.assertRaisesRegex(RuntimeError, "previous configuration restored"):
            backend.install_driver(enable_tracking=True)
        self.assert_previous_intact()
        self.assertEqual((legacy / "marker").read_text(), "legacy")

    def test_registration_success_code_without_registry_entry_is_rejected(self):
        self.run.side_effect = None
        with self.assertRaisesRegex(RuntimeError, "did not register"):
            backend.install_driver(enable_tracking=True)
        self.assert_previous_intact()

    def test_settings_write_failure_restores_the_driver_and_registration(self):
        original = backend.atomic_write
        def fail(path, data):
            if path == self.settings:
                raise PermissionError("settings are locked")
            original(path, data)
        with mock.patch.object(backend, "atomic_write", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "previous configuration restored"):
                backend.install_driver(enable_tracking=True)
        self.assert_previous_intact()

    def test_missing_runtime_does_not_touch_the_existing_install(self):
        (self.bundle / "bin/win64/xvsdk.dll").unlink()
        with self.assertRaisesRegex(RuntimeError, "missing"):
            backend.install_driver(enable_tracking=True)
        self.assert_previous_intact()
        self.run.assert_not_called()

    def test_copy_failure_does_not_modify_live_driver(self):
        with mock.patch.object(backend.shutil, "copy2", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(RuntimeError, "previous configuration restored"):
                backend.install_driver(enable_tracking=True)
        self.assert_previous_intact()
        self.run.assert_not_called()

    def test_failed_directory_swap_retains_recovery_when_restore_is_locked(self):
        rename = Path.rename
        def fail_swap(path, destination):
            if path.name in ("new", "previous"):
                raise PermissionError("folder is locked")
            return rename(path, destination)
        with mock.patch.object(Path, "rename", fail_swap):
            with self.assertRaisesRegex(RuntimeError, "Recovery files were preserved"):
                backend.install_driver(enable_tracking=True)
        recovery = list(self.target.parent.glob("driver-setup-*/previous"))
        self.assertEqual(len(recovery), 1)
        self.assertEqual((recovery[0] / "bin/win64/driver_BSB-SLAM.dll").read_bytes(), b"previous")
        self.run.assert_not_called()

    def test_steamvr_starting_during_staging_aborts_without_changes(self):
        backend.steamvr_running.side_effect = [False, True]
        with self.assertRaisesRegex(RuntimeError, "SteamVR started"):
            backend.install_driver(enable_tracking=True)
        self.assert_previous_intact()
        self.run.assert_not_called()

    def test_invalid_settings_are_not_replaced(self):
        self.settings.write_text('{"TrackingOverrides": []}')
        with self.assertRaisesRegex(RuntimeError, "settings are invalid"):
            backend.install_driver(enable_tracking=True)
        self.assertEqual(self.settings.read_text(), '{"TrackingOverrides": []}')
        self.assertEqual(self.registry.read_bytes(), self.registry_before)
        self.run.assert_not_called()
