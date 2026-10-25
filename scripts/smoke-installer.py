"""Exercise a SmokeTest installer; never run against production Steam paths."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    installer, fixture = map(lambda x: Path(x).resolve(), sys.argv[1:])
    expected = (ROOT / ".tools/installer-smoke").resolve()
    if fixture.parent != expected or installer.parent != expected:
        raise RuntimeError("Installer smoke test paths must be inside .tools/installer-smoke")
    local = fixture / "local"
    steam = fixture / "steam"
    installed = local / "BSB-SLAM"
    retired_files = ("calibrate.py", "calibration_preflight.py", "pose_mapping.py", "CALIBRATION.md")
    settings = steam / "config/steamvr.vrsettings"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"TrackingOverrides": {"other": "keep"}, "BSB-SLAM": {"offset_x": .123}}))
    registry = local / "openvr/openvrpaths.vrpath"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({"external_drivers": [str(fixture / "other-driver")]}))
    # A separately installed SDK fixture; never copy vendor files into the test installer.
    sdk = fixture / "separate-sdk"
    (sdk / "bin").mkdir(parents=True)
    data = bytearray(512)
    import struct
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 64)
    data[64:70] = b"PE\0\0\x64\x86"
    (sdk / "bin/xvsdk.dll").write_bytes(data)
    env = dict(os.environ, LOCALAPPDATA=str(local), STEAM_PATH=str(steam), STEAMVR_PATH=str(steam),
               XVISIO_SDK_PATH=str(sdk))
    def run(command, log):
        result = subprocess.run(command, env=env, timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError(f"Installer command failed ({result.returncode}); see {log}")
    def uninstall(name):
        log = fixture / f"{name}.log"
        uninstallers = list(installed.glob("unins*.exe"))
        assert len(uninstallers) == 1, uninstallers
        run([str(uninstallers[0]), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={log}"], log)
        # Inno's original process may exit before its temporary uninstaller does.
        # Wait for completed cleanup before starting another install in this folder.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            content = log.read_text(encoding="utf-8-sig", errors="replace") if log.exists() else ""
            if "Uninstallation process succeeded." in content and "Log closed." in content and not list(installed.glob("unins*.exe")):
                return
            time.sleep(.1)
        raise RuntimeError(f"Uninstaller cleanup did not finish; see {log}")
    def install(name):
        log = fixture / f"{name}.log"
        run([str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOICONS",
             f"/DIR={installed}", f"/LOG={log}"], log)
        document = json.loads(settings.read_text(encoding="utf-8-sig"))
        assert document["TrackingOverrides"]["/devices/BSB-SLAM/SLAM_TRACKER_001"] == "/user/head"
        assert document["TrackingOverrides"]["other"] == "keep"
        assert document["BSB-SLAM"]["offset_x"] == .123
        assert (installed / "driver/bin/win64/driver_BSB-SLAM.dll").is_file()
        assert (installed / "payload/driver/bin/win64/xvsdk.dll").exists() is False
        assert {path.name for path in (installed / "payload/driver/bin/win64").glob("*.dll")} == {"driver_BSB-SLAM.dll"}
        assert str(installed / "driver") in json.loads(registry.read_text())["external_drivers"]
        assert not any((installed / name).exists() for name in retired_files)
        print(name + " passed", flush=True)
    env["XVISIO_SDK_PATH"] = str(fixture / "missing-sdk")
    missing_log = fixture / "missing-sdk.log"
    run([str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOICONS",
         f"/DIR={installed}", f"/LOG={missing_log}"], missing_log)
    assert "Xvisio Windows x64 SDK is required" in (installed / "setup-result.txt").read_text(encoding="utf-8")
    assert not (installed / "driver").exists()
    assert json.loads(settings.read_text())["TrackingOverrides"] == {"other": "keep"}
    assert json.loads(registry.read_text())["external_drivers"] == [str(fixture / "other-driver")]
    uninstall("missing-sdk-uninstall")
    print("missing-sdk install passed; no tracking changes", flush=True)
    env["XVISIO_SDK_PATH"] = str(sdk)
    install("fresh-install")
    (installed / "payload/driver/bin/win64/xvsdk.dll").write_bytes(b"old bundled runtime")
    for name in retired_files:
        (installed / name).write_text("old deprecated capture helper")
    install("upgrade")
    (installed / "driver/bin/win64/driver_BSB-SLAM.dll").unlink()
    install("repair")
    uninstall("uninstall")
    document = json.loads(settings.read_text(encoding="utf-8-sig"))
    assert document["TrackingOverrides"] == {"other": "keep"}
    assert document["BSB-SLAM"]["offset_x"] == .123
    assert json.loads(registry.read_text())["external_drivers"] == [str(fixture / "other-driver")]
    assert not (installed / "driver").exists()
    assert not (installed / "BeyondSLAMControl.exe").exists()
    assert (sdk / "bin/xvsdk.dll").is_file()
    print("uninstall passed; unrelated settings and drivers preserved", flush=True)
    (fixture / "result.json").write_text(json.dumps({"passed": ["missing-sdk", "fresh-install", "upgrade", "repair", "uninstall"],
        "scope": "Isolated paths on the development machine; fake vrpathreg; no XR50 hardware or clean VM"}, indent=2))


if __name__ == "__main__":
    main()
