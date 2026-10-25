"""Windows driver control shared by the standalone app and legacy module."""

import json
import logging
import ctypes
import math
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
from functools import wraps
import sdk_setup



ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
OVERRIDE_KEY = "/devices/BSB-SLAM/SLAM_TRACKER_001"
MOUNT_PROFILE = json.loads((ROOT / "profiles/beyond-cover-xr50.json").read_text(encoding="utf-8"))
MOUNT_KEYS = ("offset_x", "offset_y", "offset_z", "offset_qw", "offset_qx", "offset_qy", "offset_qz")


def saved_tracking_settings(steam):
    """Show persistent mount calibration before the SteamVR driver starts."""
    defaults = dict(zip(MOUNT_KEYS, MOUNT_PROFILE["position"] + MOUNT_PROFILE["rotation"]))
    if steam:
        try:
            document = json.loads((steam / "config/steamvr.vrsettings").read_text(encoding="utf-8-sig"))
            saved = document.get("BSB-SLAM", {})
            if isinstance(saved, dict):
                defaults.update(saved)
        except (OSError, ValueError, AttributeError):
            pass
    return defaults


def save_mount_offline(position, rotation):
    if steamvr_running():
        raise RuntimeError("SteamVR is running but the driver is offline; close SteamVR before saving mount offsets")
    steam = steam_root()
    if not steam:
        raise RuntimeError("Steam installation not found")
    path = steam / "config/steamvr.vrsettings"
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    settings = document.setdefault("BSB-SLAM", {})
    if not isinstance(settings, dict):
        raise RuntimeError("SteamVR BSB-SLAM settings are not an object")
    settings.update(zip(MOUNT_KEYS, position + rotation))
    settings["calibration_version"] = 2
    shutil.copy2(path, str(path) + ".before-mount-offsets.bak")
    temporary = Path(str(path) + ".bsb-slam.tmp")
    try:
        temporary.write_text(json.dumps(document, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def steam_root():
    if os.environ.get("STEAM_PATH"):
        return Path(os.environ["STEAM_PATH"])
    try:
        import winreg
        for hive, key, name in (
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    return Path(winreg.QueryValueEx(handle, name)[0])
            except OSError:
                pass
    except ImportError:
        pass
    return None


def steamvr_root(steam):
    if os.environ.get("STEAMVR_PATH"):
        return Path(os.environ["STEAMVR_PATH"])
    if not steam:
        return None
    direct = steam / "steamapps/common/SteamVR"
    if (direct / "bin/win64/vrpathreg.exe").is_file():
        return direct
    libraries = steam / "steamapps/libraryfolders.vdf"
    if libraries.is_file():
        for raw in re.findall(r'"path"\s+"([^"]+)"', libraries.read_text(errors="replace")):
            candidate = Path(raw.replace("\\\\", "\\")) / "steamapps/common/SteamVR"
            if (candidate / "bin/win64/vrpathreg.exe").is_file():
                return candidate
    return direct


def driver_path():
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "BSB-SLAM/driver" if local else None


def udp(command, reply=False):
    if not command or len(command) > 512 or "\r" in command or "\n" in command:
        raise ValueError("Invalid driver command")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        connection.settimeout(2.5 if command in ("START_CAMERA_STREAM", "STOP_CAMERA_STREAM", "GET_CAMERA_CALIB") else 0.18)
        connection.sendto(command.encode("ascii"), ("127.0.0.1", 8421))
        if reply:
            return connection.recvfrom(32768)[0].decode("ascii", errors="replace")
    return ""


def fields(line):
    return dict(part.split("=", 1) for part in line.split() if "=" in part)


def inspect(feedback=""):
    steam = steam_root()
    steamvr = steamvr_root(steam)
    installed = driver_path()
    bundle = driver_bundle()
    bundled = all((bundle / path).is_file() for path in
                  ("bin/win64/driver_BSB-SLAM.dll", "driver.vrdrivermanifest"))
    source = (ROOT / "Driver/x64/Release/driver_BSB-SLAM.dll").is_file() and \
             (ROOT / "Driver/Install Example/BSB-SLAM/driver.vrdrivermanifest").is_file()
    sdk = sdk_setup.sdk_bin()
    try:
        vr_running = steamvr_running()
    except OSError:
        vr_running = True  # Do not offer driver changes if process status cannot be checked.
    state = {
        "driver_online": False, "config_available": False, "slam_ready": False, "settled": False,
        "world_active": False, "camera_stream": False, "floor_scan": 0,
        "floor_height": 0.0, "override_enabled": False, "installed": False,
        "legacy_installed": False, "status": "", "settings": saved_tracking_settings(steam),
        "mount_profile": MOUNT_PROFILE["name"],
        "can_save_mount_offline": not vr_running,
        "steamvr_path": str(steamvr or ""), "driver_path": str(installed or ""),
        "steamvr_available": bool(steamvr and (steamvr / "bin/win64/vrpathreg.exe").is_file()),
        "steam_settings_ready": bool(steam and (steam / "config/steamvr.vrsettings").is_file()),
        "driver_source_ready": bundled or source,
        "sdk_ready": sdk is not None, "sdk_path": str(sdk or ""),
        "can_build_driver": (ROOT / "build.bat").is_file(),
        "steamvr_running": vr_running,
        "feedback": feedback,
    }
    if steamvr:
        state["legacy_installed"] = (steamvr / "drivers/BSB-SLAM/driver.vrdrivermanifest").is_file()
    if steam and installed:
        settings_path = steam / "config/steamvr.vrsettings"
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
            state["override_enabled"] = settings.get("TrackingOverrides", {}).get(OVERRIDE_KEY) == "/user/head"
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        registry = Path(os.environ["LOCALAPPDATA"]) / "openvr/openvrpaths.vrpath"
        if (installed / "bin/win64/driver_BSB-SLAM.dll").is_file() and (installed / "driver.vrdrivermanifest").is_file():
            try:
                paths = json.loads(registry.read_text(encoding="utf-8-sig"))
                state["installed"] = any(Path(path).resolve() == installed.resolve()
                                         for path in paths.get("external_drivers", []))
            except (OSError, ValueError, TypeError):
                pass
    try:
        status = udp("GET_STATUS", reply=True)
        if status.startswith("STATUS "):
            state["driver_online"] = True
            state["status"] = status
            values = fields(status)
            for key in ("slam_ready", "settled", "world_active", "level_ready"):
                state[key] = values.get(key) == "1"
            state["camera_stream"] = values.get("camera_stream") == "1"
            for key in ("floor_scan", "angular_frame", "linear_frame"):
                try:
                    state[key] = int(values[key])
                except (KeyError, ValueError):
                    pass
            for key in ("floor_height", "sample_age_ms", "get_pose_ms", "pose_tick_ms",
                        "callback_age_ms", "callback_submit_ms", "sdk_prediction_ms", "pose_offset_ms",
                        "level_deg", "accel_norm", "raw_pose_age_ms"):
                try:
                    state[key] = float(values[key])
                except (KeyError, ValueError):
                    pass
            config = udp("GET_CONFIG", reply=True)
            if config.startswith("CONFIG "):
                state["settings"] = fields(config)
                state["config_available"] = True
    except (OSError, UnicodeError):
        pass
    return state


def steamvr_running():
    if os.name != "nt":
        return False
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "Could not inspect SteamVR processes")
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        found = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while found:
            if entry.szExeFile.lower() in ("vrserver.exe", "vrmonitor.exe", "vrstartup.exe"):
                return True
            found = kernel.Process32NextW(handle, ctypes.byref(entry))
        return False
    finally:
        kernel.CloseHandle(handle)


def set_override(enabled):
    if steamvr_running():
        raise RuntimeError("Close SteamVR before changing head tracking, then restart it")
    steam = steam_root()
    if not steam:
        raise RuntimeError("Steam installation not found")
    path = steam / "config/steamvr.vrsettings"
    settings = json.loads(path.read_text(encoding="utf-8-sig"))
    overrides = settings.setdefault("TrackingOverrides", {})
    if not isinstance(overrides, dict):
        raise RuntimeError("SteamVR TrackingOverrides is not an object")
    if enabled:
        overrides[OVERRIDE_KEY] = "/user/head"
    else:
        overrides.pop(OVERRIDE_KEY, None)
    shutil.copy2(path, str(path) + ".bsb-slam.bak")
    temporary = Path(str(path) + ".bsb-slam.tmp")
    try:
        temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write(path, data):
    """Replace a settings file only after all bytes have been flushed."""
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def openvr_registry():
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("Windows user data folder was not found")
    return Path(local) / "openvr/openvrpaths.vrpath"


def setup_operation(function):
    @wraps(function)
    def locked(*args, **kwargs):
        if os.name != "nt":
            return function(*args, **kwargs)
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateMutexW(None, False, "Local\\BeyondSLAMSetup")
        if not handle:
            raise OSError(ctypes.get_last_error(), "Could not lock driver setup")
        acquired = False
        try:
            acquired = kernel.WaitForSingleObject(handle, 0) in (0, 128)
            if not acquired:
                raise RuntimeError("Another setup operation is running. Wait for it to finish, then retry")
            return function(*args, **kwargs)
        finally:
            if acquired:
                kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)
    return locked


def driver_bundle():
    payload = ROOT / "payload/driver"
    return payload if payload.is_dir() else ROOT / "driver"


@setup_operation
def install_driver(enable_tracking=False):
    """Stage files, register and optionally select tracking as one recoverable operation."""
    import tempfile
    if steamvr_running():
        raise RuntimeError("Close SteamVR, then select Set up tracking again")
    steam = steam_root()
    steamvr = steamvr_root(steam)
    target = driver_path()
    if not steamvr or not target:
        raise RuntimeError("Install Steam and SteamVR, open SteamVR once, then close it and retry")
    tool = steamvr / "bin/win64/vrpathreg.exe"
    if not tool.is_file():
        raise RuntimeError("Install SteamVR in Steam, or verify its files, then retry")
    settings_path = steam / "config/steamvr.vrsettings" if steam else None
    original_settings = None
    if enable_tracking:
        if not settings_path or not settings_path.is_file():
            raise RuntimeError("Open SteamVR once to finish its setup, then close it and retry")
        original_settings = settings_path.read_bytes()
        try:
            settings = json.loads(original_settings.decode("utf-8-sig"))
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError("SteamVR settings are invalid. Repair them in SteamVR before retrying") from exc
        if not isinstance(settings, dict) or not isinstance(settings.get("TrackingOverrides", {}), dict):
            raise RuntimeError("SteamVR settings are invalid. Repair them in SteamVR before retrying")
        settings.setdefault("TrackingOverrides", {})[OVERRIDE_KEY] = "/user/head"
    bundle = driver_bundle()
    if (bundle / "bin/win64/driver_BSB-SLAM.dll").is_file():
        source = bundle / "bin/win64"
        manifest = bundle / "driver.vrdrivermanifest"
    else:
        source = ROOT / "Driver/x64/Release"
        manifest = ROOT / "Driver/Install Example/BSB-SLAM/driver.vrdrivermanifest"
    runtime = sdk_setup.sdk_bin()
    if runtime is None:
        raise RuntimeError("The Xvisio Windows x64 SDK is required. Open Beyond-SLAM on Home and select Download installer or Choose SDK folder")
    if not all(path.is_file() for path in (source / "driver_BSB-SLAM.dll", manifest, runtime / "xvsdk.dll")):
        raise RuntimeError("Driver files are missing. Run the Beyond-SLAM installer again to repair them")
    try:
        driver_manifest = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError("Driver manifest is invalid. Reinstall Beyond-SLAM") from exc
    if not isinstance(driver_manifest, dict) or driver_manifest.get("name") != "BSB-SLAM":
        raise RuntimeError("Driver manifest is invalid. Reinstall Beyond-SLAM")
    legacy = steamvr / "drivers/BSB-SLAM"
    legacy_backup = steamvr / "drivers/BSB-SLAM.legacy-backup"
    if legacy.exists() and legacy_backup.exists():
        raise RuntimeError("Two legacy driver folders exist. Move the old backup out of SteamVR before retrying")
    registry = openvr_registry()
    original_registry = registry.read_bytes() if registry.exists() else None
    # Check the registry before modifying any driver or settings files.
    if original_registry is not None:
        try:
            registered = json.loads(original_registry.decode("utf-8-sig"))
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError("SteamVR driver registry is invalid. Repair SteamVR before retrying") from exc
        if not isinstance(registered, dict) or not isinstance(registered.get("external_drivers", []), list):
            raise RuntimeError("SteamVR driver registry is invalid. Repair SteamVR before retrying")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="driver-setup-", dir=target.parent))
    previous = staging / "previous"
    changed = migrated = registration_attempted = settings_changed = previous_moved = False
    keep_recovery = False
    try:
        same_files = source.resolve() == (target / "bin/win64").resolve()
        if not same_files:
            new = staging / "new"
            new_bin = new / "bin/win64"
            new_bin.mkdir(parents=True)
            shutil.copy2(source / "driver_BSB-SLAM.dll", new_bin)
            shutil.copy2(manifest, new)
            for dll in runtime.glob("*.dll"):
                if dll.name.lower() != "driver_bsb-slam.dll":
                    shutil.copy2(dll, new_bin / dll.name)
        if steamvr_running():
            raise RuntimeError("SteamVR started during setup. Close it and retry")
        if enable_tracking and settings_path.read_bytes() != original_settings:
            raise RuntimeError("SteamVR settings changed during setup. Retry after SteamVR is closed")
        if not same_files:
            if target.exists():
                target.rename(previous)
                previous_moved = True
            new.rename(target)
            changed = True
        if legacy.exists():
            legacy.rename(legacy_backup)
            migrated = True
        registration_attempted = True
        subprocess.run([str(tool), "adddriver", str(target)], cwd=tool.parent, timeout=15,
                       capture_output=True, text=True, check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        registered = json.loads(registry.read_text(encoding="utf-8-sig"))
        if not any(Path(entry).resolve() == target.resolve() for entry in registered.get("external_drivers", [])):
            raise RuntimeError("SteamVR did not register the driver. Verify SteamVR files and retry")
        if enable_tracking:
            if steamvr_running() or settings_path.read_bytes() != original_settings:
                raise RuntimeError("SteamVR started or its settings changed during setup. Close it and retry")
            backup = Path(str(settings_path) + ".before-beyond-slam.bak")
            if not backup.exists():
                with backup.open("xb") as stream:
                    stream.write(original_settings)
            atomic_write(settings_path, json.dumps(settings, indent=2).encode("utf-8"))
            settings_changed = True
    except Exception as exc:
        try:
            if settings_changed:
                atomic_write(settings_path, original_settings)
            if registration_attempted:
                if original_registry is None:
                    registry.unlink(missing_ok=True)
                else:
                    atomic_write(registry, original_registry)
            if migrated:
                legacy_backup.rename(legacy)
            if changed:
                target.rename(staging / "failed")
            if previous_moved:
                previous.rename(target)
        except OSError as recovery:
            keep_recovery = True
            raise RuntimeError(f"Setup failed: {exc}. Recovery files were preserved at {staging}: {recovery}") from exc
        raise RuntimeError(f"Setup failed; previous configuration restored. {exc}") from exc
    finally:
        if not keep_recovery:
            try:
                shutil.rmtree(staging)
            except OSError:
                logging.warning("Could not remove setup staging folder %s", staging, exc_info=True)


@setup_operation
def unregister_driver():
    if steamvr_running():
        raise RuntimeError("Close SteamVR before uninstalling Beyond-SLAM")
    steam = steam_root()
    if steam and (steam / "config/steamvr.vrsettings").is_file():
        set_override(False)
    root = steamvr_root(steam)
    target = driver_path()
    if root and target and (root / "bin/win64/vrpathreg.exe").is_file():
        subprocess.run([str(root / "bin/win64/vrpathreg.exe"), "removedriver", str(target)],
                       capture_output=True, text=True, timeout=15, check=True)


def values(payload, key, count, limit, quaternion=False):
    raw = payload.get(key)
    if not isinstance(raw, list) or len(raw) != count or any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in raw):
        raise ValueError("Invalid transform")
    numbers = [float(x) for x in raw]
    if any(not math.isfinite(x) or abs(x) > limit for x in numbers):
        raise ValueError("Transform is outside the supported range")
    if quaternion and not 0.95 <= sum(x * x for x in numbers) <= 1.05:
        raise ValueError("Quaternion must have unit length")
    return numbers


def dispatch(name, payload):
    if name == "refresh":
        return
    if name in ("install_sdk", "install_sdk_file", "use_sdk_folder"):
        if steamvr_running():
            raise RuntimeError("Close SteamVR before changing the Xvisio SDK")
        if name == "use_sdk_folder":
            sdk_setup.select_sdk(payload.get("folder", ""))
        else:
            return sdk_setup.install_sdk(payload.get("installer") if name == "install_sdk_file" else None)
        return
    if name == "setup_tracking":
        return install_driver(enable_tracking=True)
    if name == "install_driver":
        return install_driver()
    if name == "build_driver":
        build = ROOT / "build.bat"
        if not build.is_file():
            raise RuntimeError("Build script is unavailable in this package")
        subprocess.Popen(["cmd.exe", "/k", str(build)], cwd=ROOT,
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        return
    if name in ("enable_headset", "use_base_stations"):
        return set_override(name == "enable_headset")
    if name == "launch_steamvr":
        root = steamvr_root(steam_root())
        startup = root / "bin/win64/vrstartup.exe" if root else None
        if not startup or not startup.is_file():
            raise RuntimeError("SteamVR startup executable not found")
        subprocess.Popen([str(startup)], cwd=root)
        return
    if name == "apply_mount_offsets":
        position = values(payload, "position", 3, 2)
        rotation = values(payload, "rotation", 4, 1, quaternion=True)
        if not inspect()["driver_online"]:
            return save_mount_offline(position, rotation)
        udp("SET_OFFSET " + " ".join(f"{x:.9g}" for x in position))
        udp("SET_ROT_OFFSET " + " ".join(f"{x:.9g}" for x in rotation))
        return
    if not inspect()["driver_online"]:
        raise RuntimeError("Driver is offline")
    commands = {"save_map": "SAVE_MAP", "start_floor_scan": "START_FLOOR_SCAN",
                "reset_slam": "RESET_SLAM", "clear_world": "CLEAR_WORLD_OFFSET"}
    if name in commands:
        udp(commands[name])
        return
    if name in ("start_camera_preview", "stop_camera_preview"):
        reply = udp("START_CAMERA_STREAM" if name == "start_camera_preview" else "STOP_CAMERA_STREAM", reply=True)
        if not reply.startswith("OK "):
            raise RuntimeError(reply or "Camera stream unavailable")
        return
    if name == "export_camera_calibration":
        reply = udp("GET_CAMERA_CALIB", reply=True)
        try:
            document = json.loads(reply)
            if not isinstance(document.get("cameras"), list) or len(document["cameras"]) < 2:
                raise ValueError("Camera calibration is incomplete")
        except (ValueError, AttributeError) as exc:
            raise RuntimeError(reply if reply.startswith("ERROR ") else "Camera calibration unavailable") from exc
        location = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "BSB-SLAM"
        location.mkdir(parents=True, exist_ok=True)
        output = location / "camera-calibration.json"
        output.write_text(json.dumps(document, indent=2), encoding="utf-8")
        return str(output)
    numeric = {"set_prediction": ("SET_PREDICTION", 0, 50, 0.001),
               "set_time_offset": ("SET_TIME_OFFSET", -50, 100, 0.001)}
    if name in numeric:
        verb, low, high, scale = numeric[name]
        value = payload.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError("Value is outside the supported range")
        udp(f"{verb} {value * scale:.9g}")
        return
    toggles = {"set_cslam": "SET_USE_CSLAM", "set_angular_velocity": "SET_ANGULAR_VELOCITY",
               "set_angular_local": "SET_ANGULAR_LOCAL"}
    if name in toggles:
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("Expected a boolean value")
        udp(f"{toggles[name]} {int(enabled)}")
        return
    if name == "set_lever":
        args = values(payload, "values", 3, 2)
        verb = "SET_OFFSET"
    elif name == "set_mount":
        args = values(payload, "values", 4, 1, quaternion=True)
        verb = "SET_ROT_OFFSET"
    elif name == "set_world":
        args = values(payload, "quaternion", 4, 1, quaternion=True) + values(payload, "translation", 3, 100)
        verb = "SET_WORLD_OFFSET"
    else:
        raise ValueError(f"Unknown command: {name}")
    udp(verb + " " + " ".join(f"{x:.9g}" for x in args))
