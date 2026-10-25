import io
import ctypes
import json
import os
from pathlib import Path
import ssl
import struct
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.request

import sdk_setup
from app import setup_step


def fake_dll(path, machine=0x8664):
    data = bytearray(512)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 64)
    data[64:70] = b"PE\0\0" + struct.pack("<H", machine)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class SDKSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # Resolve so 8.3 short temp paths (e.g. RUNNER~1 on CI) compare equal to the SDK's resolved paths.
        self.root = Path(self.temporary.name).resolve()
        patch = mock.patch.object(sdk_setup, "data_folder", return_value=self.root / "user-data")
        patch.start()
        self.addCleanup(patch.stop)

    def test_select_and_find_a_custom_sdk(self):
        sdk = self.root / "custom-sdk"
        fake_dll(sdk / "bin/xvsdk.dll")
        with mock.patch.dict(os.environ, {"XVISIO_SDK_PATH": ""}):
            sdk_setup.select_sdk(sdk)
            self.assertEqual(sdk_setup.sdk_bin(), sdk / "bin")
            self.assertEqual(sdk_setup.sdk_initial_folder(), sdk)

    def test_standard_sdk_is_detected_without_selecting_a_folder(self):
        programs = self.root / "Program Files"
        sdk = programs / "xvsdk"
        fake_dll(sdk / "bin/xvsdk.dll")
        with mock.patch.dict(os.environ, {"ProgramW6432": str(programs), "XVISIO_SDK_PATH": ""}):
            self.assertEqual(sdk_setup.sdk_bin(), sdk / "bin")
            self.assertEqual(sdk_setup.sdk_initial_folder(), sdk)
        self.assertFalse((self.root / "user-data/sdk-location.json").exists())

    def test_folder_picker_starts_in_program_files_before_the_sdk_is_installed(self):
        programs = self.root / "Program Files"
        programs.mkdir()
        with mock.patch.dict(os.environ, {"ProgramW6432": str(programs), "XVISIO_SDK_PATH": ""}):
            self.assertIsNone(sdk_setup.sdk_bin())
            self.assertEqual(sdk_setup.sdk_initial_folder(), programs)

    def test_wrong_architecture_is_rejected(self):
        sdk = self.root / "wrong-sdk"
        fake_dll(sdk / "bin/xvsdk.dll", 0x14c)
        with self.assertRaisesRegex(ValueError, "x64"):
            sdk_setup.select_sdk(sdk)

    def test_explicit_missing_sdk_does_not_fall_back_to_the_real_install(self):
        with mock.patch.dict(os.environ, {"XVISIO_SDK_PATH": str(self.root / "absent")}):
            self.assertIsNone(sdk_setup.sdk_bin())

    def test_missing_sdk_is_an_actionable_setup_step(self):
        self.assertEqual(setup_step({"sdk_ready": False})[1], "install_sdk")
        self.assertIsNone(setup_step({"sdk_ready": False, "steamvr_running": True})[1])

    def response(self, data, declared=None):
        response = io.BytesIO(data)
        response.headers = {"Content-Length": str(len(data) if declared is None else declared)}
        return response

    def test_secure_download_records_origin_and_digest(self):
        payload = b"MZ" + b"test" * 1024
        opener = mock.Mock()
        opener.open.return_value = self.response(payload)
        with mock.patch.object(sdk_setup.urllib.request, "build_opener", return_value=opener):
            output = sdk_setup.download_sdk()
        self.assertEqual(output.read_bytes(), payload)
        self.assertEqual(json.loads(output.with_suffix(".json").read_text())["source"], sdk_setup.SDK_DOWNLOAD_URL)
        self.assertEqual(list(output.parent.glob("*.part")), [])

    def test_certificate_failure_hands_the_download_to_the_browser_without_running_anything(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError(ssl.SSLCertVerificationError("expired"))
        with mock.patch.object(sdk_setup.urllib.request, "build_opener", return_value=opener), \
             mock.patch.object(sdk_setup.webbrowser, "open", return_value=True) as browser, \
             mock.patch.object(sdk_setup, "launch_sdk_installer") as launch:
            self.assertEqual(sdk_setup.install_sdk(), "browser")
        browser.assert_called_once_with(sdk_setup.SDK_DOWNLOAD_URL)
        launch.assert_not_called()
        self.assertEqual(list((self.root / "user-data/downloads").iterdir()), [])

    def test_certificate_failure_is_reported_when_no_browser_opens(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError(ssl.SSLCertVerificationError("expired"))
        with mock.patch.object(sdk_setup.urllib.request, "build_opener", return_value=opener), \
             mock.patch.object(sdk_setup.webbrowser, "open", return_value=False), \
             mock.patch.object(sdk_setup, "launch_sdk_installer") as launch:
            with self.assertRaisesRegex(sdk_setup.CertificateError, "certificate"):
                sdk_setup.install_sdk()
        launch.assert_not_called()

    def test_non_installer_and_incomplete_downloads_are_rejected(self):
        for data, declared in ((b"<html>" * 1024, None), (b"MZ" + b"x" * 2048, 4096)):
            opener = mock.Mock()
            opener.open.return_value = self.response(data, declared)
            with mock.patch.object(sdk_setup.urllib.request, "build_opener", return_value=opener):
                with self.assertRaises(RuntimeError):
                    sdk_setup.download_sdk()
        self.assertEqual(list((self.root / "user-data/downloads").iterdir()), [])

    def test_vendor_redirects_cannot_downgrade_tls_or_change_hosts(self):
        handler = sdk_setup.VendorRedirects()
        request = urllib.request.Request(sdk_setup.SDK_DOWNLOAD_URL)
        for destination in ("http://download.xvisiotech.com/sdk.exe", "https://example.com/sdk.exe"):
            with self.assertRaisesRegex(RuntimeError, "unexpected server"):
                handler.redirect_request(request, None, 302, "Found", {}, destination)

    @unittest.skipUnless(os.name == "nt", "Windows vendor installer handoff")
    def test_installer_handoff_keeps_vendor_wizard_visible_and_has_no_silent_flags(self):
        installer = self.root / "Xvisio.exe"
        installer.write_bytes(b"MZ" + b"x" * 1024)
        shell = mock.Mock()
        def execute(pointer):
            info = pointer._obj
            self.assertEqual(info.lpFile, str(installer.resolve()))
            self.assertIsNone(info.lpParameters)
            self.assertEqual(info.nShow, 1)
            self.assertEqual(ctypes.sizeof(info), 112)
            return True
        shell.ShellExecuteExW.side_effect = execute
        ole = mock.Mock()
        ole.CoInitializeEx.return_value = 0
        with mock.patch.object(sdk_setup.ctypes, "WinDLL", side_effect=lambda name, **kwargs: ole if name == "ole32" else shell), \
             mock.patch.object(sdk_setup, "sdk_bin", return_value=self.root):
            sdk_setup.launch_sdk_installer(installer)
