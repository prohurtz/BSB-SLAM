"""GitHub release checks, verified downloads, installer handoff, and preference storage."""
import hashlib
import http.server
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import preferences
import updater

INSTALLER = b"MZ" + b"installer" * 2000
DIGEST = hashlib.sha256(INSTALLER).hexdigest()


def release(tag="v0.5.0", digest=True, base="https://github.com/o/r/releases/download/v0.5.0", **extra):
    asset = {"name": "Beyond-SLAM-Setup.exe", "size": len(INSTALLER), "browser_download_url": f"{base}/Beyond-SLAM-Setup.exe",
             "url": "https://api.github.com/repos/o/r/releases/assets/1"}
    if digest:
        asset["digest"] = "sha256:" + DIGEST
    document = {"tag_name": tag, "draft": False, "prerelease": False, "body": "Fixes", "html_url": "https://github.com/o/r/releases/tag/" + tag,
                "assets": [asset]}
    document.update(extra)
    return document


class VersionTests(unittest.TestCase):
    def test_versions_compare_numerically_and_prereleases_sort_first(self):
        self.assertTrue(updater.is_newer("0.4.10", "0.4.9"))
        self.assertTrue(updater.is_newer("v1.0", "0.9.9"))
        self.assertFalse(updater.is_newer("1.0.0", "v1.0"))
        self.assertTrue(updater.is_newer("1.0.0", "1.0.0-beta.2"))
        self.assertFalse(updater.is_newer("1.0.0-rc1", "1.0.0"))
        self.assertFalse(updater.is_newer("garbage", "0.1.0"))
        self.assertIsNone(updater.parse_version("1.2.3.4.5"))

    def test_version_override_is_for_testing_updates(self):
        with mock.patch.dict(os.environ, BEYOND_SLAM_VERSION_OVERRIDE="0.0.1"):
            self.assertEqual(updater.current_version(), "0.0.1")


class ReleaseTests(unittest.TestCase):
    def test_newer_release_with_github_digest_is_offered(self):
        update, checksums = updater.release_update(release(), "0.4.0")
        self.assertEqual((update.version, update.sha256, update.size), ("0.5.0", DIGEST, len(INSTALLER)))
        self.assertIsNone(checksums)
        self.assertEqual(update.page_url, "https://github.com/o/r/releases/tag/v0.5.0")

    def test_same_draft_and_prerelease_versions_are_ignored(self):
        self.assertIsNone(updater.release_update(release("v0.4.0"), "0.4.0"))
        self.assertIsNone(updater.release_update(release(draft=True), "0.4.0"))
        self.assertIsNone(updater.release_update(release(prerelease=True), "0.4.0"))
        self.assertIsNone(updater.release_update([], "0.4.0"))

    def test_unusable_newer_releases_explain_why(self):
        with self.assertRaisesRegex(updater.UpdateError, "Windows installer"):
            updater.release_update(release(assets=[{"name": "notes.txt", "size": 4}]), "0.4.0")
        with self.assertRaisesRegex(updater.UpdateError, "version number"):
            updater.release_update(release("latest"), "0.4.0")
        broken = release()
        broken["assets"][0]["size"] = 0
        with self.assertRaisesRegex(updater.UpdateError, "invalid size"):
            updater.release_update(broken, "0.4.0")

    def test_the_canonical_installer_name_wins(self):
        document = release()
        document["assets"].insert(0, dict(document["assets"][0], name="Beyond-SLAM-0.5.0-debug.exe"))
        self.assertEqual(updater.release_update(document, "0.4.0")[0].asset_name, "Beyond-SLAM-Setup.exe")

    def test_checksum_file_lines_are_matched_by_name(self):
        text = f"{'0' * 64}  other.exe\n{DIGEST} *Beyond-SLAM-Setup.exe\n"
        self.assertEqual(updater.checksum_for(text, "Beyond-SLAM-Setup.exe"), DIGEST)
        self.assertEqual(updater.checksum_for("bad line", "Beyond-SLAM-Setup.exe"), "")

    def test_repository_and_api_overrides_are_validated(self):
        with mock.patch.dict(os.environ, BEYOND_SLAM_UPDATE_REPO="not a repo"):
            with self.assertRaises(updater.UpdateError):
                updater.repository()
        with mock.patch.dict(os.environ, BEYOND_SLAM_UPDATE_API="http://example.com"):
            with self.assertRaisesRegex(updater.UpdateError, "HTTPS"):
                updater.api_base()
        self.assertTrue(updater.trusted("https://objects.githubusercontent.com/x"))
        self.assertFalse(updater.trusted("https://evil.example/x"))
        self.assertFalse(updater.trusted("http://github.com/x"))


class ReleaseServer(http.server.BaseHTTPRequestHandler):
    routes, seen = {}, []

    def do_GET(self):
        self.seen.append((self.path, self.headers.get("Authorization")))
        status, headers, body = self.routes.get(self.path, (404, {}, b"{}"))
        if callable(body):
            body = body()
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ReleaseServer)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.folder = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(os.environ, BEYOND_SLAM_UPDATE_API=self.base, BEYOND_SLAM_UPDATE_REPO="o/r",
                                           LOCALAPPDATA=self.folder.name)
        self.environment.start()
        os.environ.pop("BEYOND_SLAM_UPDATE_TOKEN", None)
        ReleaseServer.seen.clear()
        ReleaseServer.routes = {"/dl/Beyond-SLAM-Setup.exe": (200, {}, INSTALLER),
                                "/dl/SHA256SUMS": (200, {}, f"{DIGEST}  Beyond-SLAM-Setup.exe\n".encode())}

    def tearDown(self):
        self.environment.stop()
        self.server.shutdown()
        self.server.server_close()
        self.folder.cleanup()

    def publish(self, document):
        ReleaseServer.routes["/repos/o/r/releases/latest"] = (200, {"Content-Type": "application/json"}, json.dumps(document).encode())

    def test_check_download_and_verify_from_checksum_file(self):
        document = release(digest=False, base=self.base + "/dl")
        document["assets"].append({"name": "SHA256SUMS", "size": 90, "browser_download_url": self.base + "/dl/SHA256SUMS"})
        self.publish(document)
        update = updater.check_for_update("0.4.0")
        self.assertEqual(update.sha256, DIGEST)
        progress = []
        path = updater.download_update(update, lambda done, total: progress.append((done, total)))
        self.assertEqual(path.read_bytes(), INSTALLER)
        self.assertEqual(progress[-1], (len(INSTALLER), len(INSTALLER)))
        self.assertEqual(list(path.parent.glob("*.part")), [])
        # A verified earlier download is reused instead of fetched again.
        ReleaseServer.seen.clear()
        self.assertEqual(updater.download_update(update), path)
        self.assertEqual(ReleaseServer.seen, [])

    def test_up_to_date_and_missing_checksum(self):
        self.publish(release("v0.4.0", base=self.base + "/dl"))
        self.assertIsNone(updater.check_for_update("0.4.0"))
        self.publish(release(digest=False, base=self.base + "/dl"))
        with self.assertRaisesRegex(updater.UpdateError, "checksum"):
            updater.check_for_update("0.4.0")

    def test_private_or_missing_repository_is_a_friendly_error(self):
        with self.assertRaisesRegex(updater.UpdateError, "private or unavailable"):
            updater.check_for_update("0.4.0")

    def test_damaged_download_is_discarded(self):
        self.publish(release(base=self.base + "/dl"))
        update = updater.check_for_update("0.4.0")
        ReleaseServer.routes["/dl/Beyond-SLAM-Setup.exe"] = (200, {}, b"MZ" + b"x" * (len(INSTALLER) - 2))
        with self.assertRaisesRegex(updater.UpdateError, "integrity"):
            updater.download_update(update)
        self.assertEqual(list(updater.updates_folder().iterdir()), [])
        ReleaseServer.routes["/dl/Beyond-SLAM-Setup.exe"] = (200, {}, INSTALLER[:100])
        with self.assertRaisesRegex(updater.UpdateError, "wrong size"):
            updater.download_update(update)

    def test_untrusted_redirects_and_cancellation_stop_the_download(self):
        self.publish(release(base=self.base + "/dl"))
        update = updater.check_for_update("0.4.0")
        ReleaseServer.routes["/dl/Beyond-SLAM-Setup.exe"] = (302, {"Location": "https://evil.example/x.exe"}, b"")
        with self.assertRaisesRegex(updater.UpdateError, "unexpected server"):
            updater.download_update(update)
        ReleaseServer.routes["/dl/Beyond-SLAM-Setup.exe"] = (200, {}, INSTALLER)
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(updater.UpdateCancelled):
            updater.download_update(update, cancelled=cancelled)
        self.assertEqual(list(updater.updates_folder().iterdir()), [])

    def test_token_is_sent_to_the_api_but_never_forwarded_on_redirect(self):
        document = release(base=self.base + "/dl")
        document["assets"][0]["url"] = self.base + "/api/asset"
        self.publish(document)
        ReleaseServer.routes["/api/asset"] = (302, {"Location": self.base + "/dl/Beyond-SLAM-Setup.exe"}, b"")
        with mock.patch.dict(os.environ, BEYOND_SLAM_UPDATE_TOKEN="secret"):
            updater.download_update(updater.check_for_update("0.4.0"))
        self.assertEqual(ReleaseServer.seen, [("/repos/o/r/releases/latest", "Bearer secret"), ("/api/asset", "Bearer secret"),
                                              ("/dl/Beyond-SLAM-Setup.exe", None)])


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(os.environ, LOCALAPPDATA=self.folder.name)
        self.environment.start()
        os.environ.pop("BEYOND_SLAM_VERSION_OVERRIDE", None)
        self.marker, self.result = updater.marker_paths()

    def tearDown(self):
        self.environment.stop()
        self.folder.cleanup()

    def test_installer_runs_silently_into_the_current_folder_and_records_a_marker(self):
        popen = mock.Mock()
        app = Path(self.folder.name) / "BSB-SLAM"
        self.result.parent.mkdir(parents=True)
        self.result.write_text("failed")
        updater.launch_installer(Path(self.folder.name) / "setup.exe", "0.5.0", app, popen=popen)
        command = popen.call_args.args[0]
        self.assertEqual(command[1:6], ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOICONS", "/UPDATE"])
        self.assertIn(f"/DIR={app}", command)
        self.assertIn(f"/RESULT={self.result}", command)
        self.assertIn(f"/RELAUNCH={app / 'BeyondSLAMControl.exe'}", command)
        self.assertFalse(self.result.exists())
        self.assertEqual(json.loads(self.marker.read_text())["version"], "0.5.0")

    def test_installer_start_failure_leaves_no_marker(self):
        with self.assertRaisesRegex(updater.UpdateError, "couldn't start"):
            updater.launch_installer("setup.exe", "0.5.0", self.folder.name, popen=mock.Mock(side_effect=OSError))
        self.assertFalse(self.marker.exists())

    def write(self, outcome=None, started=None, version="0.5.0"):
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text(json.dumps({"version": version, "started": time.time() if started is None else started}))
        if outcome is not None:
            self.result.write_text(outcome)

    def test_update_results_are_reported_once(self):
        self.assertIsNone(updater.consume_update_result("0.5.0"))
        self.write("success")
        self.assertEqual(updater.consume_update_result("0.5.0"), (True, "Updated to Beyond-SLAM 0.5.0."))
        self.assertIsNone(updater.consume_update_result("0.5.0"))
        self.write("success: The Xvisio SDK is required")
        succeeded, message = updater.consume_update_result("0.5.0")
        self.assertTrue(succeeded)
        self.assertIn("still needs attention: The Xvisio SDK is required", message)
        self.write("failed")
        succeeded, message = updater.consume_update_result("0.4.0")
        self.assertFalse(succeeded)
        self.assertIn("current version was kept", message)
        self.assertFalse(self.marker.exists() or self.result.exists())

    def test_running_installer_is_not_reported_until_it_finishes(self):
        self.write()
        self.assertIsNone(updater.consume_update_result("0.4.0"))
        self.assertTrue(self.marker.exists())
        self.write(started=time.time() - 3600)
        self.assertFalse(updater.consume_update_result("0.4.0")[0])
        self.write()
        self.assertTrue(updater.consume_update_result("0.5.0")[0])
        self.marker.write_text("{broken")
        self.assertIsNone(updater.consume_update_result("0.4.0"))
        self.assertFalse(self.marker.exists())


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(os.environ, LOCALAPPDATA=self.folder.name)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.folder.cleanup()

    def test_defaults_survive_missing_damaged_and_mistyped_files(self):
        self.assertEqual(preferences.load(), preferences.DEFAULTS)
        preferences.location().parent.mkdir(parents=True)
        preferences.location().write_text("{nope")
        self.assertEqual(preferences.load(), preferences.DEFAULTS)
        preferences.location().write_text(json.dumps({"check_for_updates": "no", "last_update_check": True, "skipped_version": 3}))
        self.assertEqual(preferences.load(), preferences.DEFAULTS)

    def test_round_trip_and_disk_errors(self):
        values = dict(preferences.DEFAULTS, check_for_updates=False, skipped_version="0.5.0", last_update_check=12)
        self.assertTrue(preferences.save(values))
        self.assertEqual(preferences.load(), dict(values, last_update_check=12.0))
        with mock.patch.object(preferences.tempfile, "mkstemp", side_effect=PermissionError), self.assertLogs(level="WARNING"):
            self.assertFalse(preferences.save(values))
