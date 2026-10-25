"""Prepare version resources/notices, export release artifacts, or publish an update release.

publish uploads dist/Beyond-SLAM-Setup.exe and dist/SHA256SUMS to GitHub release v<VERSION> in
UPDATE_REPOSITORY with the GitHub CLI. Installed apps offer that release as an in-app update.
Extra arguments are passed to `gh release create`, for example --draft.
"""
import importlib.metadata
import json
from pathlib import Path
import shutil
import sys
import hashlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from release_version import UPDATE_REPOSITORY, VERSION


def prepare():
    from PyInstaller.utils.win32.versioninfo import VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable, StringStruct, VarFileInfo, VarStruct
    numbers = tuple(map(int, VERSION.split("."))) + (0,)
    info = VSVersionInfo(ffi=FixedFileInfo(filevers=numbers, prodvers=numbers, fileType=1), kids=[
        StringFileInfo([StringTable("040904B0", [StringStruct(key, value) for key, value in {
            "CompanyName": "Fovea Labs LLC", "FileDescription": "Beyond-SLAM Control",
            "FileVersion": VERSION, "ProductVersion": VERSION, "ProductName": "Beyond-SLAM",
            "OriginalFilename": "BeyondSLAMControl.exe"}.items()])]),
        VarFileInfo([VarStruct("Translation", [1033, 1200])])])
    (ROOT / "dist").mkdir(exist_ok=True)
    (ROOT / "dist/version-info.txt").write_text(str(info), encoding="utf-8")
    notices = ROOT / "dist/notices"
    notices.mkdir(parents=True, exist_ok=True)
    for name, path in {
        "Python-LICENSE.txt": Path(sys.base_prefix) / "LICENSE.txt",
        # Python's complete LICENSE.txt also carries its bundled Tcl/Tk notices.
        "Tk-license.terms": Path(sys.base_prefix) / "tcl/tk8.6/license.terms",
        "OpenVR-LICENSE.txt": ROOT / "Driver/openvr-master/LICENSE",
    }.items():
        if not path.is_file():
            raise RuntimeError(f"Required third-party notice missing: {path}")
        shutil.copy2(path, notices / name)
    for package in ("sv-ttk", "pyinstaller"):
        distribution = importlib.metadata.distribution(package)
        copied = False
        for file in distribution.files or []:
            if file.name in ("LICENSE", "COPYING.txt"):
                shutil.copy2(distribution.locate_file(file), notices / (package + "-" + file.name))
                copied = True
        if not copied:
            raise RuntimeError(f"Required notice missing for {package}")


def checksum():
    installer = ROOT / "dist/Beyond-SLAM-Setup.exe"
    with installer.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    (ROOT / "dist/SHA256SUMS").write_text(f"{digest}  {installer.name}\n", encoding="ascii")


def export():
    destination = ROOT / "dist/release-candidate" / VERSION
    destination.mkdir(parents=True, exist_ok=True)
    name = f"Beyond-SLAM-{VERSION}-windows-x64-unsigned.exe"
    shutil.copy2(ROOT / "dist/Beyond-SLAM-Setup.exe", destination / name)
    with (destination / name).open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    (destination / "SHA256SUMS").write_text(f"{digest}  {name}\n", encoding="ascii")
    report = {"version": VERSION, "artifact": name, "sha256": digest,
              "public_launch_ready": False,
              "sdk": {"bundled": False, "installation": "separate vendor installer from the app",
                      "download_url": "https://download.xvisiotech.com/sdk-download/history_version/V3.2.0-20230907/Select_Platform_Download/For%20windows/XVSDK-3.2.0-msvc2017-x64.exe"},
              "blockers": ["Revalidate the official SDK download after its expired-certificate failure on 2026-10-18",
                           "Clean Windows machine and XR50 hardware acceptance tests pending"],
              "signing": "unsigned; no signing certificate supplied",
              "source_commit": None, "working_tree_changes_included": True}
    import subprocess
    report["source_commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    report["working_tree_changes_included"] = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
    verification = ROOT / ".tools/launch-verification.json"
    if verification.is_file():
        evidence = json.loads(verification.read_text(encoding="utf-8"))
        report["verification"] = evidence if evidence.get("installer_sha256") == digest else {"status": "stale; revalidate this installer"}
    (destination / "release-manifest.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Unsigned release candidate: {destination}")


def publish():
    import subprocess
    installer, sums = ROOT / "dist/Beyond-SLAM-Setup.exe", ROOT / "dist/SHA256SUMS"
    with installer.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if sums.read_text(encoding="ascii").split() != [digest, installer.name]:
        raise RuntimeError("dist/SHA256SUMS does not match the installer; rebuild with build_bundle.bat")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip():
        raise RuntimeError("Commit the source for this release before publishing it")
    subprocess.run(["gh", "release", "create", f"v{VERSION}", str(installer), str(sums),
                    "--repo", UPDATE_REPOSITORY, "--title", f"Beyond-SLAM {VERSION}",
                    "--generate-notes", *sys.argv[2:]], cwd=ROOT, check=True)
    print(f"Published v{VERSION} to {UPDATE_REPOSITORY}; installed apps will offer it as an update.")


if __name__ == "__main__":
    {"prepare": prepare, "checksum": checksum, "export": export, "publish": publish}[sys.argv[1]]()
