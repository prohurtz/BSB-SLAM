"""Home progression, validated inputs, and observable action results."""
import math
from pathlib import Path
from typing import NamedTuple

from control_backend import MOUNT_KEYS


class SetupStep(NamedTuple):
    description: str
    action: str | None
    title: str


def setup_step(state):
    if not state:
        return SetupStep("This only takes a moment.", None, "Checking your setup…")
    if not state.get("sdk_ready"):
        if state.get("steamvr_running"):
            return SetupStep("Close SteamVR to continue with SDK setup.", None, "Close SteamVR")
        return SetupStep("Download the installer and follow its setup window. If the SDK is installed already, choose its folder.", "install_sdk", "SDK setup")
    if not state.get("steamvr_available"):
        return SetupStep("Install SteamVR from Steam, then return here.", "open_steamvr_store", "Install SteamVR")
    if not state.get("steam_settings_ready"):
        return SetupStep("Finish SteamVR's first-time setup, then close it and return here.", None if state.get("steamvr_running") else "launch_steamvr", "Open SteamVR once")
    if not state.get("installed") or not state.get("override_enabled"):
        if state.get("steamvr_running"):
            return SetupStep("Close SteamVR before changing your headset's tracking source.", None, "Close SteamVR")
        if not state.get("driver_source_ready"):
            return SetupStep("Run the Beyond-SLAM installer again to restore its driver files.", None, "Repair Beyond-SLAM")
        return SetupStep("Select XR50 head tracking for your Beyond. SteamVR must stay closed during setup.", "setup_tracking", "Set up your headset")
    if not state.get("steamvr_running"):
        return SetupStep("Connect your Beyond and XR50, then start SteamVR.", "launch_steamvr", "Start your tracking session")
    if not state.get("driver_online"):
        return SetupStep("Check the XR50 connection. If the driver stays offline, restart SteamVR.", None, "Waiting for the driver")
    if not state.get("slam_ready") or not state.get("settled"):
        return SetupStep("Move the headset gently while the XR50 finds its position.", None, "Let tracking settle")
    scan = state.get("floor_scan", 0)
    if scan == 1:
        return SetupStep("Keep the headset still on the floor for two seconds.", None, "Setting floor height…")
    if scan == 2:
        return SetupStep("Floor height is set for this SteamVR session.", None, "Ready to track")
    detail = (floor_status_text(state) + ". " if scan >= 3 else "")
    return SetupStep(detail + "Place the headset on the floor, select Set floor height, and keep it still for two seconds.", "start_floor_scan", "Set the floor height")


CHECKLIST = (
    ("Xvisio SDK installed", lambda state: state.get("sdk_ready")),
    ("SteamVR installed", lambda state: state.get("steamvr_available")),
    ("SteamVR first-time setup finished", lambda state: state.get("steam_settings_ready")),
    ("Headset tracking set up", lambda state: state.get("installed") and state.get("override_enabled")),
    ("SteamVR running", lambda state: state.get("steamvr_running")),
    ("Tracking driver connected", lambda state: state.get("driver_online")),
    ("Tracking settled", lambda state: state.get("slam_ready") and state.get("settled")),
    ("Floor height set", lambda state: state.get("floor_scan") == 2),
)


def checklist(state):
    """Each setup requirement as (label, "done" | "current" | "todo" | "unknown")."""
    if not state:
        return [(label, "unknown") for label, _ in CHECKLIST]
    items, current = [], False
    for label, test in CHECKLIST:
        if test(state):
            items.append((label, "done"))
        elif not current:
            items.append((label, "current"))
            current = True
        else:
            items.append((label, "todo"))
    return items


def progress_text(state):
    items = checklist(state)
    if not state:
        return ""
    current = next((index for index, (_, status) in enumerate(items) if status == "current"), None)
    return f"All {len(items)} steps complete" if current is None else f"Step {current + 1} of {len(items)}"


def tools_hint(state):
    if not state.get("driver_online"):
        return "Start SteamVR with headset tracking set up to use these tools."
    if not state.get("slam_ready") or not state.get("settled"):
        return "Move the headset gently until tracking settles, then these tools become available."
    return ""


def recovery_hint(state):
    if state.get("steamvr_running"):
        return "Close SteamVR to repair setup or switch back to base stations."
    return "Restart and clear actions need SteamVR running." if state else ""


def floor_status_text(state):
    if not state.get("driver_online"):
        return "Start SteamVR before setting the floor"
    if not state.get("slam_ready") or not state.get("settled"):
        return "Wait for XR50 tracking to settle"
    return ("Ready to set floor", "Keep still for two seconds…", "Floor calibrated for this session",
            "Tracking stopped; try again", "Tracking is not ready", "Floor setup stopped", "Headset moved; try again")[max(0, min(6, state.get("floor_scan", 0)))]


def number(text, low, high):
    try:
        value = float(text)
    except (ValueError, TypeError):
        raise ValueError(f"Enter a number from {low:g} to {high:g}.") from None
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"Enter a number from {low:g} to {high:g}.")
    return value


def setting_value(state, action):
    if not state.get("driver_online") or not state.get("config_available"):
        return None
    key, scale = {"set_prediction": ("prediction_s", 1000), "set_time_offset": ("time_offset", 1000),
                  "set_cslam": ("use_cslam", None), "set_angular_velocity": ("use_angular_velocity", None),
                  "set_angular_local": ("angular_velocity_local", None)}[action]
    raw = state.get("settings", {}).get(key)
    if scale is None:
        return True if str(raw).lower() in ("1", "true") else False if str(raw).lower() in ("0", "false") else None
    try:
        value = float(raw) * scale
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def mount_values(state):
    if state.get("driver_online") and not state.get("config_available"):
        return None
    try:
        result = [float(state.get("settings", {})[key]) for key in MOUNT_KEYS]
        return result if all(math.isfinite(x) for x in result) else None
    except (ValueError, TypeError, KeyError):
        return None


def parse_mount(texts):
    values = []
    for text, label, limit in zip(texts, ("X", "Y", "Z", "W", "X", "Y", "Z"), (2, 2, 2, 1, 1, 1, 1)):
        try:
            values.append(number(text, -limit, limit))
        except ValueError as exc:
            raise ValueError(f"{label}: {exc}") from None
    if not .95 <= sum(x * x for x in values[3:]) <= 1.05:
        raise ValueError("Rotation must be a unit quaternion. Check W, X, Y, and Z.")
    return {"position": values[:3], "rotation": values[3:]}


def _multiply(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return [aw*bw - ax*bx - ay*by - az*bz, aw*bx + ax*bw + ay*bz - az*by,
            aw*by - ax*bz + ay*bw + az*bx, aw*bz + ax*by - ay*bx + az*bw]


def nudge_mount(rotation, pitch_degrees=0.0, roll_degrees=0.0):
    """Rotate a mount quaternion (w, x, y, z) in the head frame, where the view looks along -Z.

    The driver renders head = device * mount, so right-multiplying turns the view itself:
    positive pitch tilts it up (lowering the horizon); positive roll raises its right side
    (lowering the floor on the left).
    """
    p, r = math.radians(pitch_degrees) / 2, math.radians(roll_degrees) / 2
    turn = _multiply([math.cos(p), math.sin(p), 0.0, 0.0], [math.cos(r), 0.0, 0.0, math.sin(r)])
    result = _multiply(list(rotation), turn)
    norm = math.sqrt(sum(x * x for x in result))
    return [x / norm for x in result]


def mount_trim(profile_rotation, rotation):
    """Approximate (pitch, roll) in degrees that `rotation` adds to `profile_rotation`."""
    pw, px, py, pz = profile_rotation
    delta = _multiply([pw, -px, -py, -pz], list(rotation))
    if delta[0] < 0:
        delta = [-x for x in delta]
    return math.degrees(2 * delta[1]), math.degrees(2 * delta[3])


def mount_matches(payload, state):
    actual = mount_values(state)
    if actual is None:
        return False
    position, rotation = payload["position"], payload["rotation"]
    return (all(abs(a - b) < 1e-5 for a, b in zip(position, actual[:3])) and
            (all(abs(a - b) < 1e-5 for a, b in zip(rotation, actual[3:])) or
             all(abs(a + b) < 1e-5 for a, b in zip(rotation, actual[3:]))))


class Outcome(NamedTuple):
    complete: bool
    message: str = ""
    error: bool = False


def action_outcome(action, payload, state, result=None):
    if action in ("set_prediction", "set_time_offset", "set_cslam", "set_angular_velocity", "set_angular_local"):
        actual = setting_value(state, action)
        desired = payload.get("value", payload.get("enabled"))
        if actual is not None and abs(float(actual) - float(desired)) < 1e-3:
            return Outcome(True, "Setting saved." + (" Map tracking takes effect next time SteamVR starts." if action == "set_cslam" else ""))
    elif action == "apply_mount_offsets":
        if mount_matches(payload, state):
            return Outcome(True, "Mount offsets saved. Verify alignment with gentle movement.")
    elif action == "start_floor_scan":
        scan = state.get("floor_scan", 0)
        if scan == 2:
            return Outcome(True, "Floor calibrated for this session.")
        if scan in (3, 4, 5, 6):
            return Outcome(True, floor_status_text(state) + ".", True)
    elif action == "setup_tracking":
        if state.get("installed") and state.get("override_enabled"):
            return Outcome(True, "Tracking setup complete.")
    elif action == "use_sdk_folder":
        chosen = Path(payload["folder"]).resolve()
        actual = Path(state.get("sdk_path", "")).resolve()
        if state.get("sdk_ready") and actual in (chosen, chosen / "bin"):
            return Outcome(True, "SDK folder saved.")
    elif action == "install_sdk":
        if state.get("sdk_ready"):
            return Outcome(True, "SDK detected. Continue with the next step.")
        if result == "browser":
            return Outcome(True, "Xvisio's download opened in your browser. Run the installer, and setup will continue "
                                 "once the SDK is detected.")
    elif action == "launch_steamvr":
        if state.get("steamvr_running"):
            return Outcome(True, "SteamVR started.")
    elif action == "use_base_stations":
        if not state.get("override_enabled"):
            return Outcome(True, "Base stations selected. Start SteamVR to apply the change.")
    elif action == "clear_world":
        if not state.get("world_active"):
            return Outcome(True, "Room alignment cleared.")
    elif action in ("start_camera_preview", "stop_camera_preview"):
        if bool(state.get("camera_stream")) == (action == "start_camera_preview"):
            return Outcome(True, "Camera preview started." if action == "start_camera_preview" else "Camera preview stopped.")
    elif action == "export_camera_calibration":
        return Outcome(True, f"Camera calibration saved to {result}.")
    elif action == "save_map":
        return Outcome(True, "Map save requested. Completion is recorded in the driver log.")
    elif action == "reset_slam":
        return Outcome(True, "Tracking restart requested. Let the XR50 settle again.")
    return Outcome(False)
