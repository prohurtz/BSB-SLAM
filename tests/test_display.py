"""Home flow, live settings, confirmation, and DPI/compact layout regressions."""
import os
from pathlib import Path
import time
import unittest
from unittest import mock

from display import fit_window


class WindowPlacementTests(unittest.TestCase):
    def test_small_workarea_caps_size_and_reserves_taskbar(self):
        self.assertEqual(fit_window(1640, 1440, 100, 100, (0, 0, 640, 440)), (640, 440, 0, 0))

    def test_monitor_left_and_above_primary_uses_signed_coordinates(self):
        self.assertEqual(fit_window(820, 720, 300, 300, (-1920, -1080, 0, 0)), (820, 720, -820, -720))


@unittest.skipUnless(os.name == "nt", "Windows desktop layout")
class DisplayLayoutTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        import sv_ttk
        import app
        from display import enable_dpi_awareness
        self.app = app
        enable_dpi_awareness()
        self.root = tk.Tk()
        self.root.attributes("-alpha", 0)
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        sv_ttk.set_theme("dark", self.root)
        self.patch = mock.patch.multiple(app.ControlApp, refresh=mock.DEFAULT, schedule_update_check=mock.DEFAULT)
        self.patch.start()
        self.prefs = mock.patch.multiple(app.preferences, load=mock.Mock(return_value=dict(app.preferences.DEFAULTS)),
                                         save=mock.Mock(return_value=True))
        self.prefs.start()
        self.results = mock.patch.object(app.updater, "consume_update_result", return_value=None)
        self.results.start()
        self.ui = app.ControlApp(self.root)
        self.root.update()
        self.ui.display.close()
        self.root.minsize(1, 1)
        self.state = dict(sdk_ready=True, sdk_path="C:/Program Files/xvsdk/bin", steamvr_available=True,
                          steam_settings_ready=True, driver_source_ready=True, installed=True,
                          override_enabled=True, steamvr_running=True, driver_online=True,
                          config_available=True, slam_ready=True, settled=True, floor_scan=0,
                          world_active=False, camera_stream=False, can_save_mount_offline=False,
                          settings=dict(zip(app.backend.MOUNT_KEYS,
                              app.backend.MOUNT_PROFILE["position"] + app.backend.MOUNT_PROFILE["rotation"])))
        self.state["settings"].update(prediction_s=".015", time_offset="-.010", use_cslam="1", use_angular_velocity="0")
        self.ui.state = self.state
        self.ui.update_views()

    def tearDown(self):
        self.ui.operation = None
        self.ui.busy = False
        # Each test owns a separate Tk interpreter; discard mocked callbacks too.
        for timer in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.tk.call("after", "cancel", timer)
        self.ui.close()
        self.patch.stop()
        self.prefs.stop()
        self.results.stop()
        self.assertEqual(self.errors, [])

    def settle(self):
        self.root.after(40, self.root.quit)
        self.root.mainloop()
        self.root.update_idletasks()

    def test_live_settings_preserve_drafts_validate_inline_and_apply_profile_once(self):
        editor = self.ui.settings["set_prediction"]
        self.assertEqual(editor.value.get(), "15")
        self.assertTrue(editor.apply.instate(["disabled"]))
        editor.value.set("banana")
        self.assertIn("Enter a number", editor.hint.get())
        self.assertTrue(editor.apply.instate(["disabled"]))
        with mock.patch.object(editor, "submit") as submit:
            editor.save()
            submit.assert_not_called()
        editor.value.set("22")
        self.state["settings"]["prediction_s"] = ".017"
        self.ui.update_views()
        self.assertEqual(editor.value.get(), "22")
        self.assertFalse(editor.apply.instate(["disabled"]))
        switch = self.ui.settings["set_angular_velocity"]
        self.assertEqual(switch.status.get(), "Off")
        with mock.patch.object(switch, "submit") as submit:
            switch.switch.invoke()
            submit.assert_called_once_with("set_angular_velocity", {"enabled": True})
        self.state["settings"]["use_angular_velocity"] = "1"
        self.ui.update_views()
        self.assertEqual(switch.status.get(), "On")
        self.assertTrue(switch.value.get())
        self.state["settings"]["offset_x"] = .2
        self.ui.update_views()
        with mock.patch.object(self.ui, "run_action") as submit:
            self.ui.profile_button.invoke()
            submit.assert_called_once_with("apply_mount_offsets", self.ui.profile_payload(), source="profile")
        self.ui.offsets["offset_x"].set("bad")
        self.ui.update_views()
        self.assertIn("X:", self.ui.mount_error.get())
        self.assertTrue(self.ui.mount_save.instate(["disabled"]))
        self.state["settings"]["offset_x"] = .3
        self.ui.update_views()
        self.assertEqual(self.ui.offsets["offset_x"].get(), "bad")
        self.state["driver_online"] = False
        self.ui.update_views()
        self.assertTrue(editor.entry.instate(["disabled"]))
        self.assertEqual(switch.status.get(), "Unavailable")
        with mock.patch.object(self.app.sdk_setup, "sdk_initial_folder", return_value="C:/Program Files/xvsdk"), \
             mock.patch.object(self.app.filedialog, "askdirectory", return_value="") as picker:
            self.ui.choose_sdk_folder()
            self.assertEqual(picker.call_args.kwargs["initialdir"], "C:/Program Files/xvsdk")
            self.assertTrue(picker.call_args.kwargs["mustexist"])

    def wait_for(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while not condition():
            self.assertLess(time.monotonic(), deadline, "Timed out waiting for the interface")
            self.settle()

    def test_colored_labels_keep_their_style_after_a_theme_refresh(self):
        from tkinter import ttk
        self.ui.show_feedback("Something failed", error=True)
        ttk.Style(self.root).configure("TButton", padding=(14, 8, 14, 8))
        self.root.event_generate("<<ThemeChanged>>")
        self.settle()
        self.settle()
        pending = [self.root]
        while pending:
            widget = pending.pop()
            if isinstance(widget, ttk.Label):
                self.assertEqual(str(widget.cget("foreground")), "", str(widget))
            pending.extend(widget.winfo_children())
        self.assertEqual(self.ui.notice_label.cget("style"), "Error.TLabel")

    def test_a_failed_redraw_never_stops_polling(self):
        self.ui.events.put((self.state, None, None, None))
        with mock.patch.object(self.ui, "update_views", side_effect=RuntimeError("draw")):
            with self.assertRaises(RuntimeError):
                self.ui.drain()
        self.assertIsNotNone(self.ui.drain_id)
        self.assertIsNotNone(self.ui.poll_id)

    def test_checklist_and_hints_follow_the_live_state(self):
        marks = [mark.cget("text") for mark, _ in self.ui.checklist_rows]
        self.assertEqual(marks, ["\u2713"] * 7 + ["\u2192"])
        self.assertEqual(self.ui.step_count.get(), "Step 8 of 8")
        self.assertEqual(self.ui.status.get(), "\u25cf Tracking connected")
        self.assertFalse(self.ui.tools_hint_label.winfo_manager())
        self.assertTrue(self.ui.recovery_hint_label.winfo_manager())
        self.state.update(driver_online=False, steamvr_running=False)
        self.ui.update_views()
        self.assertTrue(self.ui.tools_hint_label.winfo_manager())
        self.assertIn("Start SteamVR", self.ui.tools_hint.get())
        self.assertEqual(self.ui.status.get(), "\u25cf SteamVR closed")
        self.state["floor_scan"] = 2
        self.state.update(driver_online=True, steamvr_running=True)
        self.ui.update_views()
        self.assertEqual(self.ui.step_count.get(), "All 8 steps complete")
        self.assertEqual(self.ui.status.get(), "\u25cf Tracking ready")

    def test_keyboard_shortcuts_switch_pages(self):
        self.ui.select_page("home")
        self.ui.cycle_page(1)
        self.assertEqual(self.ui.page_var.get(), "mount")
        self.ui.cycle_page(-2)
        self.assertEqual(self.ui.page_var.get(), "settings")
        self.assertTrue(self.ui.pages["settings"].winfo_manager())
        self.assertFalse(self.ui.pages["home"].winfo_manager())

    def test_update_prompt_waits_for_steamvr_then_hands_off_and_closes(self):
        updater = self.app.updater
        info = updater.Update("9.0.0", "Notes", "https://github.com/o/r/releases/tag/v9.0.0", "Beyond-SLAM-Setup.exe",
                              "https://github.com/o/r/releases/download/v9.0.0/Beyond-SLAM-Setup.exe", "", 10, "a" * 64)
        with mock.patch.object(updater, "check_for_update", return_value=info):
            self.ui.start_update_check()
            self.wait_for(lambda: self.ui.update_dialog is not None)
        dialog = self.ui.update_dialog
        self.assertFalse(self.ui.update_banner.winfo_manager())
        self.assertIn("9.0.0 is available", self.ui.update_status.get())
        with mock.patch.object(updater, "can_install", return_value=True), \
             mock.patch.object(self.app.backend, "steamvr_running", return_value=True), \
             mock.patch.object(updater, "download_update") as download:
            dialog.install.invoke()
            download.assert_not_called()
        self.assertIn("Close SteamVR", dialog.message.get())
        self.assertIsNone(self.ui.update_stage)
        with mock.patch.object(updater, "can_install", return_value=True), \
             mock.patch.object(self.app.backend, "steamvr_running", return_value=False), \
             mock.patch.object(updater, "download_update", return_value=Path("setup.exe")), \
             mock.patch.object(updater, "launch_installer") as launch, \
             mock.patch.object(self.ui, "close") as close:
            dialog.install.invoke()
            self.assertEqual(self.ui.update_stage, "downloading")
            self.assertTrue(self.ui.primary.instate(["disabled"]))
            self.wait_for(lambda: launch.called)
            launch.assert_called_once_with(Path("setup.exe"), "9.0.0")
            self.assertEqual(self.ui.update_stage, "installing")
            self.wait_for(lambda: close.called)
            close.assert_called_once_with(force=True)
        self.ui.update_stage = None

    def test_skipped_versions_are_not_prompted_again_automatically(self):
        updater = self.app.updater
        info = updater.Update("9.0.0", "", "", "Beyond-SLAM-Setup.exe", "", "", 10, "a" * 64)
        self.ui.update_info = info
        self.ui.prompt_update()
        self.ui.update_dialog.skip.invoke()
        self.assertEqual(self.ui.prefs["skipped_version"], "9.0.0")
        self.assertIsNone(self.ui.update_dialog)
        self.assertFalse(self.ui.update_banner.winfo_manager())
        self.ui.update_prompted.clear()
        self.ui.update_checked(info, None)
        self.assertIsNone(self.ui.update_dialog)
        self.ui.update_manual = True
        self.ui.update_checked(info, None)
        self.assertIsNotNone(self.ui.update_dialog)
        self.ui.update_dialog.later.invoke()
        self.assertIsNone(self.ui.update_dialog)

    def test_pending_changes_require_readback_and_stale_floor_success_is_ignored(self):
        self.ui.busy = True
        self.ui.run_action("set_prediction", {"value": 22})
        self.assertEqual(self.ui.pending_action, ("set_prediction", {"value": 22}))
        self.assertTrue(self.ui.action_busy)
        self.ui.events.put((self.state, None, None, None))
        with mock.patch.object(self.ui, "start_worker") as worker:
            self.ui.drain()
            worker.assert_called_once_with("set_prediction", {"value": 22})
        self.ui.events.put((self.state, "set_prediction", None, None))
        self.ui.drain()
        self.assertTrue(self.ui.action_busy)
        self.assertNotIn("saved", self.ui.notice.get())
        self.state["settings"]["prediction_s"] = ".022"
        self.ui.events.put((self.state, None, None, None))
        self.ui.drain()
        self.assertFalse(self.ui.action_busy)
        self.assertIn("saved", self.ui.notice.get())
        self.state["floor_scan"] = 2
        with mock.patch.object(self.ui, "start_worker"):
            self.ui.run_action("start_floor_scan")
        self.ui.events.put((self.state, "start_floor_scan", None, None))
        self.ui.drain()
        self.assertTrue(self.ui.action_busy)
        self.state["floor_scan"] = 1
        self.ui.events.put((self.state, None, None, None))
        self.ui.drain()
        self.assertTrue(self.ui.progress_running)
        self.assertIn("still", self.ui.next_step.get())
        self.state["floor_scan"] = 2
        self.ui.events.put((self.state, None, None, None))
        self.ui.drain()
        self.assertFalse(self.ui.action_busy)
        self.assertEqual(self.ui.step_title.get(), "Ready to track")
        self.assertIn("Floor calibrated", self.ui.notice.get())
        # A dropped setting command times out instead of claiming success.
        with mock.patch.object(self.ui, "start_worker"):
            self.ui.run_action("set_prediction", {"value": 31})
        self.ui.operation.update(sent=True, observed_at=time.monotonic() - 20)
        self.ui.events.put((self.state, None, None, None))
        self.ui.drain()
        self.assertFalse(self.ui.action_busy)
        self.assertIn("could not be confirmed", self.ui.notice.get())

    def test_dark_theme_scales_fonts_and_keeps_all_controls_reachable(self):
        import ctypes
        from ctypes import wintypes
        from tkinter import font
        import sv_ttk
        ui, root = self.ui, self.root
        self.assertEqual([ui.nav_buttons[key].cget("text") for key, _ in self.app.PAGES], ["Home", "Mount", "Tools", "Settings"])
        self.assertTrue(hasattr(ui, "app_icon"))
        ui.display.apply_scale(96)
        root.geometry("860x640")
        self.settle()
        self.assertFalse(ui.pages["home"].scroll.winfo_manager())
        with mock.patch.object(ui.primary, "state", wraps=ui.primary.state) as states, \
             mock.patch.object(ui.primary, "configure", wraps=ui.primary.configure) as configure:
            ui.busy = True
            ui.update_views()
            ui.update_views()
            states.assert_not_called()
            configure.assert_not_called()
            ui.busy = False
        with mock.patch.object(ui, "run_action") as submit:
            ui.next_action()
            submit.assert_called_once_with("start_floor_scan")
        disclosures = (ui.expert, ui.diagnostic_details, ui.custom_mount)
        pages = {"home": ui.home, "mount": ui.mount, "tools": ui.tools, "settings": ui.settings_page}
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        for dpi in (96, 120, 144, 192, 288, 96):
            ui.display.apply_scale(dpi)
            for size in ("640x480", "1280x720", "3840x2160"):
                root.geometry(size)
                self.settle()
                self.assertEqual(ui.body_font.cget("size"), -round(14 * dpi / 96))
                self.assertEqual(font.nametofont("SunValleyBodyFont", root).cget("size"), -round(14 * dpi / 96))
                self.assertEqual(sv_ttk.get_theme(root), "dark")
                self.assertLessEqual(ui.column.content.winfo_width(), round(980 * dpi / 96) + 1)
                for nav in ui.nav.items:
                    self.assertLessEqual(nav.winfo_x() + nav.winfo_width(), ui.nav.winfo_width(), (dpi, size))
                for key, page in pages.items():
                    ui.select_page(key)
                    for disclosure in disclosures:
                        if not disclosure.expanded:
                            disclosure.toggle.invoke()
                    self.settle()
                    for row in descendants(page):
                        if isinstance(row, self.app.FlowRow) and row.winfo_ismapped():
                            for child in row.items:
                                self.assertLessEqual(child.winfo_x() + child.winfo_width(), row.winfo_width(), (dpi, size, str(child)))
                    canvas = page.master
                    canvas.yview_moveto(1)
                    self.settle()
                    self.assertGreaterEqual(canvas.canvasy(canvas.winfo_height()) + 1, canvas.bbox("all")[3])
                    canvas.yview_moveto(0)
                    for disclosure in disclosures:
                        if disclosure.expanded:
                            disclosure.toggle.invoke()
                    self.settle()
                self.assertEqual(ui.home_wide, ui.home.winfo_width() >= round(760 * dpi / 96))
        if __import__("sys").getwindowsversion().build >= 22000:
            dwm = ctypes.WinDLL("dwmapi")
            dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
            color = wintypes.DWORD()
            hwnd = ui.display.user32.GetAncestor(root.winfo_id(), 2)
            self.assertEqual(dwm.DwmGetWindowAttribute(hwnd, 20, ctypes.byref(color), ctypes.sizeof(color)), 0)
            self.assertEqual(color.value, 1)
