import math
import unittest

import ui_state
from ui_state import (action_outcome, checklist, mount_matches, mount_trim, nudge_mount, number, parse_mount,
                      progress_text, recovery_hint, setting_value, setup_step, tools_hint)

PROFILE_ROTATION = [0.0, 0.0, 0.0871557427, 0.9961946981]  # upside down, pitched 10 degrees


def rotate(q, v):
    w, x, y, z = q
    vx, vy, vz = v
    # q * v * conj(q), expanded.
    tx, ty, tz = 2 * (y*vz - z*vy), 2 * (z*vx - x*vz), 2 * (x*vy - y*vx)
    return [vx + w*tx + y*tz - z*ty, vy + w*ty + z*tx - x*tz, vz + w*tz + x*ty - y*tx]


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


class UIStateTests(unittest.TestCase):
    def test_home_covers_first_use_and_full_session_without_navigation(self):
        self.assertEqual(setup_step({"sdk_ready": False}).action, "install_sdk")
        state = {"sdk_ready": True}
        self.assertEqual(setup_step(state).action, "open_steamvr_store")
        state["steamvr_available"] = True
        self.assertEqual(setup_step(state).action, "launch_steamvr")
        state.update(steam_settings_ready=True, driver_source_ready=True)
        self.assertEqual(setup_step(state).action, "setup_tracking")
        state.update(installed=True, override_enabled=True)
        self.assertEqual(setup_step(state).action, "launch_steamvr")
        state.update(steamvr_running=True, driver_online=True, slam_ready=True, settled=True)
        self.assertEqual(setup_step(state).action, "start_floor_scan")
        state["floor_scan"] = 1
        self.assertIsNone(setup_step(state).action)
        self.assertIn("still", setup_step(state).description)
        state["floor_scan"] = 2
        self.assertIsNone(setup_step(state).action)
        self.assertEqual(setup_step(state).title, "Ready to track")

    def test_settings_use_live_config_and_never_invent_zero_or_false(self):
        state = {"driver_online": True, "config_available": True,
                 "settings": {"prediction_s": ".021", "time_offset": "-.012", "use_cslam": "1", "use_angular_velocity": "0",
                              "angular_velocity_local": "1"}}
        self.assertEqual(setting_value(state, "set_prediction"), 21)
        self.assertEqual(setting_value(state, "set_time_offset"), -12)
        self.assertTrue(setting_value(state, "set_cslam"))
        self.assertFalse(setting_value(state, "set_angular_velocity"))
        self.assertTrue(setting_value(state, "set_angular_local"))
        self.assertTrue(action_outcome("set_angular_local", {"enabled": True}, state).complete)
        self.assertIsNone(setting_value({"driver_online": True, "config_available": True, "settings": {}}, "set_angular_local"))
        state["config_available"] = False
        self.assertIsNone(setting_value(state, "set_prediction"))
        self.assertIsNone(setting_value(state, "set_cslam"))
        state.update(config_available=True, driver_online=False)
        self.assertIsNone(setting_value(state, "set_prediction"))

    def test_numeric_errors_are_friendly_and_nonfinite_values_are_rejected(self):
        for text in ("", "banana", "nan", "inf", "51", "-1"):
            with self.assertRaisesRegex(ValueError, "Enter a number from 0 to 50"):
                number(text, 0, 50)
        self.assertEqual(number("-10.5", -50, 100), -10.5)

    def test_mount_validation_and_equivalent_quaternion_signs(self):
        payload = parse_mount([".01", "0", "0", "1", "0", "0", "0"])
        keys = ("offset_x", "offset_y", "offset_z", "offset_qw", "offset_qx", "offset_qy", "offset_qz")
        state = {"settings": dict(zip(keys, [.01, 0, 0, -1, 0, 0, 0]))}
        self.assertTrue(mount_matches(payload, state))
        with self.assertRaisesRegex(ValueError, "unit quaternion"):
            parse_mount(["0"] * 7)
        with self.assertRaisesRegex(ValueError, "X:"):
            parse_mount(["bad", "0", "0", "1", "0", "0", "0"])

    def test_setting_success_waits_for_driver_readback(self):
        state = {"driver_online": True, "config_available": True, "settings": {"prediction_s": ".010"}}
        self.assertFalse(action_outcome("set_prediction", {"value": 25}, state).complete)
        state["settings"]["prediction_s"] = ".025"
        self.assertTrue(action_outcome("set_prediction", {"value": 25}, state).complete)
        self.assertFalse(action_outcome("start_floor_scan", {}, {"floor_scan": 1}).complete)
        self.assertTrue(action_outcome("start_floor_scan", {}, {"floor_scan": 2}).complete)
        failure = action_outcome("start_floor_scan", {}, {"driver_online": True, "slam_ready": True, "settled": True, "floor_scan": 6})
        self.assertTrue(failure.error)
        self.assertIn("moved", failure.message)

    def test_level_nudges_turn_the_view_the_way_their_labels_say(self):
        device = [math.cos(.4), .1, math.sin(.4), .05]  # an arbitrary head orientation
        norm = math.sqrt(sum(x * x for x in device))
        device = [x / norm for x in device]
        head = lambda mount: ui_state._multiply(device, mount)
        before = head(PROFILE_ROTATION)
        up, right, forward = rotate(before, [0, 1, 0]), rotate(before, [1, 0, 0]), rotate(before, [0, 0, -1])
        # "Horizon too high" pitches the view up: forward gains some of the old "up".
        self.assertGreater(dot(rotate(head(nudge_mount(PROFILE_ROTATION, pitch_degrees=.5)), [0, 0, -1]), up), .008)
        self.assertLess(dot(rotate(head(nudge_mount(PROFILE_ROTATION, pitch_degrees=-.5)), [0, 0, -1]), up), -.008)
        # Positive roll raises the view's right side, which lowers the floor on the left.
        self.assertGreater(dot(rotate(head(nudge_mount(PROFILE_ROTATION, roll_degrees=.5)), [1, 0, 0]), up), .008)
        # Pure pitch leaves the right axis where it was; pure roll leaves forward.
        self.assertAlmostEqual(dot(rotate(head(nudge_mount(PROFILE_ROTATION, pitch_degrees=.5)), [1, 0, 0]), right), 1, places=9)
        self.assertAlmostEqual(dot(rotate(head(nudge_mount(PROFILE_ROTATION, roll_degrees=.5)), [0, 0, -1]), forward), 1, places=9)

    def test_level_trim_reports_the_adjustment_from_the_profile(self):
        self.assertEqual(mount_trim(PROFILE_ROTATION, PROFILE_ROTATION), (0.0, 0.0))
        nudged = nudge_mount(nudge_mount(PROFILE_ROTATION, pitch_degrees=1.0), roll_degrees=-.5)
        self.assertAlmostEqual(sum(x * x for x in nudged), 1.0, places=12)
        pitch, roll = mount_trim(PROFILE_ROTATION, nudged)
        self.assertAlmostEqual(pitch, 1.0, places=2)
        self.assertAlmostEqual(roll, -.5, places=2)
        # A sign-flipped quaternion is the same rotation and the same trim.
        flipped = mount_trim(PROFILE_ROTATION, [-x for x in nudged])
        self.assertAlmostEqual(flipped[0], pitch, places=9)
        self.assertAlmostEqual(flipped[1], roll, places=9)

    def test_browser_sdk_download_is_guidance_not_an_error(self):
        outcome = action_outcome("install_sdk", {}, {"sdk_ready": False}, "browser")
        self.assertTrue(outcome.complete)
        self.assertFalse(outcome.error)
        self.assertIn("browser", outcome.message)
        self.assertFalse(action_outcome("install_sdk", {}, {"sdk_ready": False}).complete)

    def test_unobservable_map_save_does_not_claim_saved(self):
        self.assertIn("requested", action_outcome("save_map", {}, {}).message)

    def test_checklist_marks_one_current_step_and_never_hides_finished_ones(self):
        self.assertEqual({status for _, status in checklist({})}, {"unknown"})
        self.assertEqual(progress_text({}), "")
        state = {"sdk_ready": True, "steamvr_available": True, "steam_settings_ready": True, "steamvr_running": True}
        statuses = [status for _, status in checklist(state)]
        self.assertEqual(statuses, ["done", "done", "done", "current", "done", "todo", "todo", "todo"])
        self.assertEqual(progress_text(state), "Step 4 of 8")
        self.assertIn("Close SteamVR", recovery_hint(state))
        self.assertIn("Start SteamVR", tools_hint(state))
        state.update(installed=True, override_enabled=True, driver_online=True, slam_ready=True, settled=True, floor_scan=2)
        self.assertEqual(progress_text(state), "All 8 steps complete")
        self.assertEqual(tools_hint(state), "")
