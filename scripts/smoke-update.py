"""Exercise a real in-place update with isolated SmokeTest installers; never touches real Steam paths.

Builds the current source twice (as VERSION and the next patch version), installs the older build
into .tools/update-e2e, publishes the newer one on a loopback mock of the GitHub Releases API, then
accepts the app's update prompt and checks that it installs, keeps the tracking source, refreshes
the driver, relaunches the new version, and does not prompt again. Run with the build venv:

    .venv/Scripts/python.exe scripts/smoke-update.py

It shows the app and presses Space in its update prompt, so leave the desktop alone while it runs.
"""
import ctypes
from ctypes import wintypes
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from release_version import VERSION  # noqa: E402

AREA = ROOT / ".tools/update-e2e"
PYTHON = Path(sys.executable)
ISCC = next((path for path in (ROOT / ".tools/InnoSetup/ISCC.exe",
                               Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6/ISCC.exe",
                               Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6/ISCC.exe") if path.is_file()), None)
SOURCES = ["app.py", "control_backend.py", "display.py", "preferences.py", "release_version.py", "sdk_setup.py",
           "ui_controls.py", "ui_state.py", "update_dialog.py", "updater.py", "window_activation.py"]
OLD = VERSION
NEW = ".".join(VERSION.split(".")[:2] + [str(int(VERSION.split(".")[2]) + 1)])
TRACKING = "/devices/BSB-SLAM/SLAM_TRACKER_001"
user32 = ctypes.WinDLL("user32", use_last_error=True)


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def build(version):
    stage, out = AREA / f"stage-{version}", AREA / f"out-{version}"
    shutil.rmtree(stage, ignore_errors=True)
    shutil.rmtree(out, ignore_errors=True)
    stage.mkdir(parents=True)
    for name in SOURCES:
        shutil.copy2(ROOT / name, stage / name)
    shutil.copytree(ROOT / "profiles", stage / "profiles")
    shutil.copytree(ROOT / "assets", stage / "assets")
    text = (stage / "release_version.py").read_text(encoding="utf-8")
    (stage / "release_version.py").write_text(text.replace(f'VERSION = "{VERSION}"', f'VERSION = "{version}"'), encoding="utf-8")
    log(f"Packaging {version}")
    subprocess.run([str(PYTHON), "-m", "PyInstaller", "--noconfirm", "--windowed", "--onedir", "--contents-directory", ".",
                    "--name", "BeyondSLAMControl", "--manifest", str(ROOT / "installer/app.manifest"),
                    "--icon", str(ROOT / "assets/beyond-slam.ico"), "--collect-all", "sv_ttk",
                    "--add-data", f"{stage / 'profiles'};profiles", "--add-data", f"{stage / 'assets'};assets",
                    "--distpath", str(out / "dist"), "--workpath", str(out / "work"), "--specpath", str(out),
                    "app.py"], cwd=stage, check=True, capture_output=True)
    result = subprocess.run([str(ISCC), "/DSmokeTest", f"/DAppVersion={version}", f"/DPackageDir={out / 'dist/BeyondSLAMControl'}",
                             f"/O{out / 'installer'}", f"/Fsetup-{version}", str(ROOT / "installer/Beyond-SLAM.iss")],
                            capture_output=True, text=True)
    if result.returncode:
        print(result.stdout[-3000:], result.stderr[-3000:])
        raise RuntimeError("Installer compilation failed")
    return out / "installer" / f"setup-{version}.exe"


def windows(title_prefix, pids):
    found = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        if title.value.startswith(title_prefix) and user32.IsWindowVisible(hwnd):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value in pids:
                found.append((hwnd, title.value, owner.value))
        return True
    user32.EnumWindows(callback_type(visit), 0)
    return found


def wait(condition, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(.25)
    raise RuntimeError(f"Timed out waiting for {what}")


def app_pids(folder):
    output = subprocess.run(["powershell", "-NoProfile", "-Command",
                             "Get-Process BeyondSLAMControl -ErrorAction SilentlyContinue | ForEach-Object { \"$($_.Id)|$($_.Path)\" }"],
                            capture_output=True, text=True).stdout
    pids = set()
    for line in output.splitlines():
        pid, _, path = line.partition("|")
        if path and Path(path).resolve().parent == folder.resolve():
            pids.add(int(pid))
    return pids


class Releases(http.server.BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):
        status, body = self.routes.get(self.path, (404, b"{}"))
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def make_fixture(fixture):
    local, steam = fixture / "local", fixture / "steam"
    tool = steam / "bin/win64/vrpathreg.exe"
    tool.parent.mkdir(parents=True)
    compiler = fixture / "compile.ps1"
    compiler.write_text("param($Source, $Destination)\n$ErrorActionPreference = 'Stop'\n"
                        "Add-Type -Path $Source -ReferencedAssemblies System.Web.Extensions -OutputAssembly $Destination -OutputType ConsoleApplication\n",
                        encoding="utf-8")
    subprocess.run([os.environ["SystemRoot"] + r"\System32\WindowsPowerShell\v1.0\powershell.exe", "-NoProfile", "-File", str(compiler),
                    str(ROOT / "scripts/installer-registry-fixture.cs"), str(tool)], check=True)
    settings = steam / "config/steamvr.vrsettings"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"TrackingOverrides": {"other": "keep"}, "BSB-SLAM": {"offset_x": .123}}))
    registry = local / "openvr/openvrpaths.vrpath"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"external_drivers": [str(fixture / "other-driver")]}))
    sdk = fixture / "separate-sdk"
    (sdk / "bin").mkdir(parents=True)
    data = bytearray(512)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 64)
    data[64:70] = b"PE\0\0\x64\x86"
    (sdk / "bin/xvsdk.dll").write_bytes(data)
    return local, steam, settings, sdk


def main():
    if os.name != "nt":
        raise RuntimeError("The update smoke test needs Windows")
    if ISCC is None:
        raise RuntimeError("Install Inno Setup 6, or place it in .tools/InnoSetup")
    AREA.mkdir(parents=True, exist_ok=True)
    old_installer, new_installer = build(OLD), build(NEW)
    fixture = AREA / uuid.uuid4().hex[:10]
    local, steam, settings, sdk = make_fixture(fixture)
    installed = local / "BSB-SLAM"
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Releases)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    env = dict(os.environ, LOCALAPPDATA=str(local), STEAM_PATH=str(steam), STEAMVR_PATH=str(steam), XVISIO_SDK_PATH=str(sdk),
               BEYOND_SLAM_UPDATE_API=base, BEYOND_SLAM_UPDATE_REPO="fovea/beyond-slam-test")
    for name in ("BEYOND_SLAM_UPDATE_TOKEN", "BEYOND_SLAM_VERSION_OVERRIDE"):
        env.pop(name, None)
    log(f"Installing {OLD}")
    subprocess.run([str(old_installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOICONS", f"/DIR={installed}",
                    f"/LOG={fixture / 'install-old.log'}"], env=env, check=True, timeout=180)
    document = json.loads(settings.read_text(encoding="utf-8-sig"))
    assert document["TrackingOverrides"][TRACKING] == "/user/head"
    # The user switches back to base stations; an update must keep that choice.
    document["TrackingOverrides"].pop(TRACKING)
    settings.write_text(json.dumps(document))
    driver = installed / "driver/bin/win64/driver_BSB-SLAM.dll"
    driver.write_bytes(b"stale driver")
    payload = new_installer.read_bytes()
    Releases.routes = {
        "/repos/fovea/beyond-slam-test/releases/latest": (200, json.dumps({
            "tag_name": f"v{NEW}", "draft": False, "prerelease": False, "html_url": base + "/release",
            "body": f"Beyond-SLAM {NEW}\n\n- Test update delivered by scripts/smoke-update.py.",
            "assets": [{"name": "Beyond-SLAM-Setup.exe", "size": len(payload), "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                        "browser_download_url": base + "/download/Beyond-SLAM-Setup.exe", "url": base + "/asset"}]}).encode()),
        "/download/Beyond-SLAM-Setup.exe": (200, payload)}
    log(f"Launching installed {OLD}")
    first = subprocess.Popen([str(installed / "BeyondSLAMControl.exe")], env=env, cwd=installed)
    try:
        dialog = wait(lambda: windows("Beyond-SLAM update", {first.pid}), 40, "the update prompt")[0][0]
        time.sleep(1)
        user32.SetForegroundWindow(dialog)
        time.sleep(.3)
        if user32.GetForegroundWindow() != dialog:
            raise RuntimeError("Could not focus the update prompt; leave the desktop alone and retry")
        user32.keybd_event(0x20, 0, 0, 0)
        user32.keybd_event(0x20, 0, 2, 0)
        log("Accepted the update")
        wait(lambda: first.poll() is not None, 60, "the old app to exit")
    finally:
        if first.poll() is None:
            first.kill()
    relaunched = wait(lambda: windows(f"Beyond-SLAM {NEW}", app_pids(installed)), 180, f"the relaunched {NEW} app")[0]
    log(f"Relaunched as {relaunched[1]}")
    install_log = (installed / "update-install.log").read_text(encoding="utf-8-sig", errors="replace")
    assert "Installation process succeeded" in install_log, install_log[-2000:]
    time.sleep(3)
    document = json.loads(settings.read_text(encoding="utf-8-sig"))
    assert TRACKING not in document["TrackingOverrides"], document
    assert document["TrackingOverrides"]["other"] == "keep" and document["BSB-SLAM"]["offset_x"] == .123
    assert driver.read_bytes() != b"stale driver", "driver files were not refreshed"
    assert not (installed / "update-pending.json").exists(), "relaunched app did not report the update result"
    assert not list((installed / "updates").glob("*.exe")), "downloaded installer was not cleaned up"
    time.sleep(4)
    assert not windows("Beyond-SLAM update", app_pids(installed)), "updated app prompted for the same version again"
    user32.PostMessageW(relaunched[0], 0x0010, 0, 0)
    wait(lambda: not app_pids(installed), 20, "the relaunched app to close")
    uninstaller = next(installed.glob("unins*.exe"))
    subprocess.run([str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], env=env, timeout=120)
    server.shutdown()
    (fixture / "result.json").write_text(json.dumps({"passed": f"in-place update {OLD} -> {NEW}",
        "scope": "Isolated SmokeTest installers and paths on this machine; mock GitHub API on loopback"}, indent=2))
    log(f"Update smoke test passed; evidence in {fixture}")


if __name__ == "__main__":
    main()
