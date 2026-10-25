"""Beyond-SLAM desktop control. Run with Python or the packaged Windows app."""
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, font, messagebox, ttk
import webbrowser

import control_backend as backend
import preferences
from release_version import VERSION
import sdk_setup
import updater
from display import (AdaptiveButton, CenteredColumn, Disclosure, DisplayManager, FlowRow, ScrollPage,
                     dark_title_bar, enable_dpi_awareness, wrap_labels)
from ui_controls import BooleanSetting, NumberSetting, enabled
from ui_state import (action_outcome, checklist, floor_status_text, mount_matches, mount_trim, mount_values,
                      nudge_mount, parse_mount, progress_text, recovery_hint, setup_step, tools_hint)
from update_dialog import UpdateDialog
from window_activation import restore_existing_instance

PAGES = (("home", "Home"), ("mount", "Mount"), ("tools", "Tools"), ("settings", "Settings"))
CHECK_MARKS = {"done": ("✓", "Success.TLabel"), "current": ("→", "Accent.TLabel"),
               "todo": ("○", "Secondary.TLabel"), "unknown": ("…", "Secondary.TLabel")}
FIRST_UPDATE_CHECK_MS = 3000
UPDATE_CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000
# Closing during these could leave SteamVR settings half-written.
UNINTERRUPTIBLE = ("setup_tracking", "apply_mount_offsets", "use_base_stations", "use_sdk_folder")


class ControlApp:
    def __init__(self, root):
        self.root, self.display = root, DisplayManager(root)
        self.title_font = font.Font(root, name="AppTitleFont", family="Segoe UI", size=-21, weight="bold")
        self.heading_font = font.Font(root, name="AppHeadingFont", family="Segoe UI", size=-18, weight="bold")
        self.body_font = font.Font(root, name="AppBodyFont", family="Segoe UI", size=-14)
        self.strong_font = font.Font(root, name="AppStrongFont", family="Segoe UI", size=-14, weight="bold")
        for name in ("SunValleyBodyFont", "SunValleyBodyStrongFont"):
            if name in font.names(root):
                font.nametofont(name, root).configure(size=-14)
        # Sun Valley reapplies tk_setPalette after every style change, which stamps a fixed
        # foreground onto existing labels and hides their colored styles; undo that each time.
        self.recolor_id = None
        root.bind("<<ThemeChanged>>", self.theme_changed, add="+")
        style = ttk.Style(root)
        for name in ("TLabel", "TButton", "TEntry", "TNotebook.Tab", "TCheckbutton", "TRadiobutton", "Toggle.TButton"):
            style.configure(name, font=self.body_font)
        for name, color in (("Secondary", "#bcbcbc"), ("Error", "#ff99a4"), ("Success", "#6ccb5f"),
                            ("Accent", "#57c8ff"), ("Warning", "#f7c948")):
            style.configure(name + ".TLabel", foreground=color, font=self.body_font)
        style.configure("Title.TLabel", font=self.title_font)
        style.configure("Heading.TLabel", font=self.heading_font)
        style.configure("Strong.TLabel", font=self.strong_font)
        self.state, self.events, self.guarded = {}, queue.Queue(), []
        self.busy = self.action_busy = self.closed = self.close_requested = False
        self.pending_action = self.operation = None
        self.poll_id = self.drain_id = self.notice_timer = self.update_timer = None
        self.notice = tk.StringVar()
        self.progress_running = False
        self.mount_syncing = self.mount_dirty = False
        self.mount_entries = []
        self.dynamic = {}
        self.prefs = preferences.load()
        self.update_events = queue.Queue()
        self.update_info = self.update_dialog = self.update_stage = self.update_cancel = None
        self.update_checking = self.update_manual = False
        self.update_error = ""
        self.update_prompted = set()
        root.title(f"Beyond-SLAM {VERSION}")
        root.geometry("900x760")
        root.minsize(460, 360)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.column = CenteredColumn(root)
        self.column.pack(fill="both", expand=True)
        self.build_chrome(self.column.content)
        self.home = self.page("home")
        self.mount = self.page("mount")
        self.tools = self.page("tools")
        self.settings_page = self.page("settings")
        self.build_home()
        self.build_mount()
        self.build_tools()
        self.build_settings()
        self.select_page("home")
        wrap_labels(self.column, lambda: self.display.dpi / 96)
        self.display.listeners.append(self.rescale_dynamic)
        root.bind("<MouseWheel>", self.scroll_page, add="+")
        root.bind("<Map>", self.style_window, add="+")
        root.bind("<Control-Tab>", lambda event: self.cycle_page(1))
        root.bind("<Control-Shift-Tab>", lambda event: self.cycle_page(-1))
        root.bind("<Control-ISO_Left_Tab>", lambda event: self.cycle_page(-1))
        for number, (key, _) in enumerate(PAGES, 1):
            root.bind(f"<Control-Key-{number}>", lambda event, key=key: self.select_page(key))
        root.after_idle(self.display.start)
        root.after_idle(self.style_window)
        self.drain_id = root.after(50, self.drain)
        self.update_views()
        self.update_update_views()
        result = updater.consume_update_result()
        if result:
            self.show_feedback(result[1], error=not result[0])
            # The installer relaunches the app from the background; raise it so the result is seen.
            root.after(300, self.bring_to_front)
        self.schedule_update_check(FIRST_UPDATE_CHECK_MS)
        self.refresh()

    # Layout helpers -----------------------------------------------------------------------

    def theme_changed(self, event):
        if event.widget is self.root and self.recolor_id is None:
            self.recolor_id = self.root.after_idle(self.restore_label_colors)

    def restore_label_colors(self):
        self.recolor_id = None
        pending = [self.root]
        while pending:
            widget = pending.pop()
            if isinstance(widget, ttk.Label) and str(widget.cget("foreground")):
                widget.configure(foreground="")
            pending.extend(widget.winfo_children())

    def build_chrome(self, parent):
        header = FlowRow(parent)
        header.pack(fill="x", pady=(0, 4))
        title = header.add(ttk.Label(header, text="Beyond-SLAM", style="Heading.TLabel"))
        title.fixed_width = True
        self.status = tk.StringVar(value="Checking your setup…")
        self.status_label = header.add(ttk.Label(header, textvariable=self.status, style="Secondary.TLabel"))
        self.status_label.fixed_width = True
        self.header = header
        self.update_banner = ttk.Frame(parent)
        self.update_banner_text = tk.StringVar()
        row = FlowRow(self.update_banner)
        row.pack(fill="x")
        row.add(ttk.Label(row, textvariable=self.update_banner_text, style="Accent.TLabel"))
        row.add(AdaptiveButton(row, text="See update", command=self.prompt_update))
        self.nav = FlowRow(parent)
        self.nav.pack(fill="x", pady=(4, 0))
        self.page_var = tk.StringVar(value="home")
        self.nav_buttons = {}
        for key, caption in PAGES:
            button = self.nav.add(ttk.Radiobutton(self.nav, text=caption, value=key, variable=self.page_var,
                                                  style="Toggle.TButton", command=lambda key=key: self.select_page(key)))
            self.nav_buttons[key] = button
        self.notice_frame = ttk.Frame(parent)
        self.notice_icon = ttk.Label(self.notice_frame, text="", style="Success.TLabel")
        self.notice_icon.fixed_width = True
        self.notice_icon.pack(side="left", anchor="n", padx=(0, 8))
        self.notice_dismiss = AdaptiveButton(self.notice_frame, text="Dismiss", command=lambda: self.show_feedback())
        self.notice_dismiss.pack(side="right", anchor="n", padx=(12, 0))
        self.notice_label = ttk.Label(self.notice_frame, textvariable=self.notice, style="Success.TLabel")
        self.notice_label.pack(side="left", fill="x", expand=True)
        ttk.Separator(parent).pack(fill="x", pady=(0, 0))
        self.page_area = ttk.Frame(parent)
        self.page_area.pack(fill="both", expand=True)
        self.pages = {}

    def page(self, key):
        page = ScrollPage(self.page_area, padding=(0, 20, 20, 20))
        self.pages[key] = page
        return page.content

    def select_page(self, key):
        if isinstance(key, int):
            key = PAGES[key % len(PAGES)][0]
        for name, page in self.pages.items():
            if name != key and page.winfo_manager():
                page.pack_forget()
        if not self.pages[key].winfo_manager():
            self.pages[key].pack(fill="both", expand=True)
        if self.page_var.get() != key:
            self.page_var.set(key)
        self.current_page = key
        return "break"

    def cycle_page(self, step):
        keys = [key for key, _ in PAGES]
        return self.select_page(keys[(keys.index(self.current_page) + step) % len(keys)])

    def scaled(self, options):
        scale = self.display.dpi / 96
        def size(value):
            return tuple(round(x * scale) for x in value) if isinstance(value, tuple) else round(value * scale)
        return {key: size(value) if key in ("padx", "pady") else value for key, value in options.items()}

    def show(self, widget, visible, **options):
        """Pack or hide a widget, keeping its spacing scaled for the current display."""
        self.dynamic[widget] = options
        if visible and not widget.winfo_manager():
            widget.pack(**self.scaled(options))
        elif not visible and widget.winfo_manager():
            widget.pack_forget()

    def rescale_dynamic(self, scale):
        self.layout_home(force=True)
        for widget, options in self.dynamic.items():
            if widget.winfo_manager() == "pack":
                widget.pack_configure(**{key: value for key, value in self.scaled(options).items() if key in ("padx", "pady")})

    def set_row(self, row, visible):
        if row.items != visible:
            for item in row.winfo_children():
                item.place_forget()
            row.items = visible
            row.reflow()

    def text(self, parent, text, style=None, pady=(0, 16)):
        label = ttk.Label(parent, text=text, style=style or "TLabel")
        label.pack(fill="x", pady=pady)
        return label

    def section(self, parent, title, description=None, first=False):
        if not first:
            ttk.Separator(parent).pack(fill="x", pady=(16, 24))
        self.text(parent, title, "Heading.TLabel", (0, 8))
        if description:
            self.text(parent, description, "Secondary.TLabel")

    def row(self, parent):
        row = FlowRow(parent)
        row.pack(fill="x")
        return row

    def button(self, row, caption, action, payload=None, predicate=None):
        def invoke():
            try:
                data = payload() if callable(payload) else payload
                self.run_action(action, data)
            except ValueError as exc:
                self.show_feedback(str(exc), error=True)
        button = row.add(AdaptiveButton(row, text=caption, command=invoke))
        self.guarded.append((button, predicate or (lambda state: state.get("driver_online"))))
        return button

    def disclosure(self, parent, title):
        item = Disclosure(parent, title)
        item.pack(fill="x", pady=(0, 16))
        return item

    # Pages --------------------------------------------------------------------------------

    def build_home(self):
        hero, checks = self.home_hero, self.home_checks = ttk.Frame(self.home), ttk.Frame(self.home)
        self.home_dividers = ttk.Separator(self.home, orient="horizontal"), ttk.Separator(self.home, orient="vertical")
        self.home_wide = None
        self.step_count, self.step_title, self.next_step = tk.StringVar(), tk.StringVar(), tk.StringVar()
        ttk.Label(hero, textvariable=self.step_count, style="Secondary.TLabel").pack(fill="x", pady=(0, 4))
        ttk.Label(hero, textvariable=self.step_title, style="Title.TLabel").pack(fill="x", pady=(0, 12))
        ttk.Label(hero, textvariable=self.next_step).pack(fill="x", pady=(0, 20))
        self.progress = ttk.Progressbar(hero, mode="indeterminate", length=220)
        self.primary_row = self.row(hero)
        self.primary = self.primary_row.add(AdaptiveButton(self.primary_row, text="Checking…", style="Accent.TButton", command=self.next_action))
        self.home_folder = self.primary_row.add(AdaptiveButton(self.primary_row, text="Choose SDK folder", command=self.choose_sdk_folder))
        self.recalibrate = self.primary_row.add(AdaptiveButton(self.primary_row, text="Set floor again", command=lambda: self.run_action("start_floor_scan")))
        self.session_note = tk.StringVar()
        self.session_note_label = ttk.Label(hero, textvariable=self.session_note, style="Secondary.TLabel")
        self.text(checks, "Setup checklist", "Heading.TLabel", (0, 12))
        self.checklist_rows = []
        for label, _ in checklist({}):
            row = ttk.Frame(checks)
            row.pack(fill="x", pady=(0, 6))
            mark = ttk.Label(row, text="…", style="Secondary.TLabel", width=2, anchor="center")
            mark.fixed_width = True
            mark.pack(side="left", anchor="n", padx=(0, 8))
            caption = ttk.Label(row, text=label, style="Secondary.TLabel")
            caption.pack(side="left", fill="x", expand=True)
            self.checklist_rows.append((mark, caption))
        self.home.bind("<Configure>", lambda event: self.layout_home(), add="+")
        self.layout_home()

    def layout_home(self, force=False):
        """Show the checklist beside the next step on wide windows and below it on narrow ones."""
        scale = self.display.dpi / 96
        wide = self.home.winfo_width() >= round(760 * scale)
        if wide == self.home_wide and not force:
            return
        self.home_wide = wide
        horizontal, vertical = self.home_dividers
        for widget in (self.home_hero, self.home_checks, horizontal, vertical):
            widget.grid_forget()
        self.home.columnconfigure(0, weight=1)
        self.home.columnconfigure(2, weight=0, minsize=round(260 * scale) if wide else 0)
        self.home_hero.grid(row=0, column=0, sticky="new")
        if wide:
            vertical.grid(row=0, column=1, sticky="ns", padx=round(28 * scale))
            self.home_checks.grid(row=0, column=2, sticky="new")
        else:
            horizontal.grid(row=1, column=0, sticky="ew", pady=(round(24 * scale), round(20 * scale)))
            self.home_checks.grid(row=2, column=0, sticky="new")

    def build_mount(self):
        self.section(self.mount, "Headset mount", "Tells tracking where the XR50 sits on your Beyond. "
                     "A wrong mount makes the view swim when you turn your head.", first=True)
        self.text(self.mount, "Bundled profile: Beyond Cover · upside-down XR50 · 10° downward pitch. "
                  "It is a CAD estimate; use it only with this exact mount.")
        self.mount_status = tk.StringVar()
        ttk.Label(self.mount, textvariable=self.mount_status, style="Secondary.TLabel").pack(fill="x", pady=(0, 16))
        self.profile_button = AdaptiveButton(self.mount, text="Apply mount profile", style="Accent.TButton", command=self.apply_profile)
        self.profile_button.pack(anchor="w", pady=(0, 24))
        self.custom_mount = self.disclosure(self.mount, "Custom offsets")
        self.text(self.custom_mount.content, "For a different mount, enter offsets you measured. "
                  "Position is in meters; rotation is a unit quaternion.", "Secondary.TLabel")
        self.offsets = {}
        for title, axes, keys in (("Position (meters)", "XYZ", backend.MOUNT_KEYS[:3]), ("Rotation (unit quaternion)", "WXYZ", backend.MOUNT_KEYS[3:])):
            self.text(self.custom_mount.content, title, "Strong.TLabel", (0, 8))
            row = self.row(self.custom_mount.content)
            for axis, key in zip(axes, keys):
                value = tk.StringVar()
                group = row.add(ttk.Frame(row))
                ttk.Label(group, text=axis).pack(side="left", padx=(0, 8))
                entry = ttk.Entry(group, textvariable=value, width=10)
                entry.pack(side="left", fill="x", expand=True)
                entry.bind("<Return>", lambda event: self.save_custom_mount())
                self.mount_entries.append(entry)
                self.offsets[key] = value
                value.trace_add("write", self.mount_edited)
        self.mount_error = tk.StringVar()
        ttk.Label(self.custom_mount.content, textvariable=self.mount_error, style="Error.TLabel").pack(fill="x", pady=(0, 12))
        row = self.row(self.custom_mount.content)
        self.mount_save = row.add(AdaptiveButton(row, text="Save custom offsets", command=self.save_custom_mount))
        self.mount_revert = row.add(AdaptiveButton(row, text="Discard changes", command=lambda: self.update_mount(reset=True)))
        self.section(self.mount, "Level adjustment", "If the floor looks tilted in the headset, nudge the view "
                     "0.5° at a time while wearing it. Changes apply immediately and are saved with your mount. "
                     "Apply mount profile undoes them.")
        self.level_status = tk.StringVar()
        ttk.Label(self.mount, textvariable=self.level_status, style="Secondary.TLabel").pack(fill="x", pady=(0, 12))
        row = self.row(self.mount)
        self.level_buttons = [row.add(AdaptiveButton(row, text=caption, command=lambda p=pitch, r=roll: self.nudge_level(p, r)))
                              for caption, pitch, roll in (("Horizon too high", .5, 0), ("Horizon too low", -.5, 0),
                                                           ("Floor lower on left", 0, .5), ("Floor lower on right", 0, -.5))]

    def build_tools(self):
        ready = lambda state: state.get("driver_online") and state.get("slam_ready") and state.get("settled")
        self.section(self.tools, "Maps and cameras", first=True)
        self.tracking_status = tk.StringVar()
        ttk.Label(self.tools, textvariable=self.tracking_status, style="Secondary.TLabel").pack(fill="x", pady=(0, 12))
        self.tools_hint = tk.StringVar()
        self.tools_hint_label = ttk.Label(self.tools, textvariable=self.tools_hint, style="Warning.TLabel")
        self.map_row = self.row(self.tools)
        self.button(self.map_row, "Save map", "save_map", predicate=ready)
        self.text(self.tools, "Camera preview", "Strong.TLabel", (8, 4))
        self.text(self.tools, "Shows the XR50 cameras in your web browser. Stop the preview when you're done.", "Secondary.TLabel", (0, 12))
        row = self.row(self.tools)
        self.button(row, "Start preview", "start_camera_preview", predicate=lambda state: state.get("driver_online") and state.get("slam_ready") and not state.get("camera_stream"))
        self.button(row, "Stop preview", "stop_camera_preview", predicate=lambda state: state.get("camera_stream"))
        preview = row.add(AdaptiveButton(row, text="Open preview", command=lambda: webbrowser.open("http://127.0.0.1:8422/stereo.mjpg")))
        self.guarded.append((preview, lambda state: state.get("camera_stream")))
        self.button(self.row(self.tools), "Export camera calibration", "export_camera_calibration")
        self.section(self.tools, "Troubleshooting")
        self.recovery_hint = tk.StringVar()
        self.recovery_hint_label = ttk.Label(self.tools, textvariable=self.recovery_hint, style="Warning.TLabel")
        self.recovery_row = self.row(self.tools)
        self.button(self.recovery_row, "Repair tracking setup", "setup_tracking", predicate=lambda state: state.get("sdk_ready") and state.get("steam_settings_ready") and state.get("driver_source_ready") and not state.get("steamvr_running"))
        self.button(self.recovery_row, "Use base stations", "use_base_stations", predicate=lambda state: state.get("override_enabled") and state.get("steam_settings_ready") and not state.get("steamvr_running"))
        row = self.row(self.tools)
        self.button(row, "Clear room alignment", "clear_world", predicate=lambda state: state.get("driver_online") and state.get("world_active"))
        self.button(row, "Restart tracking", "reset_slam", predicate=lambda state: state.get("driver_online") and state.get("slam_ready"))
        self.section(self.tools, "Xvisio SDK")
        self.sdk_location = tk.StringVar()
        ttk.Label(self.tools, textvariable=self.sdk_location, style="Secondary.TLabel").pack(fill="x", pady=(0, 16))
        row = self.row(self.tools)
        self.button(row, "Download installer", "install_sdk", predicate=lambda state: not state.get("steamvr_running"))
        chooser = row.add(AdaptiveButton(row, text="Choose SDK folder", command=self.choose_sdk_folder))
        self.guarded.append((chooser, lambda state: not state.get("steamvr_running")))

    def build_settings(self):
        page = self.settings_page
        self.section(page, "Updates", first=True)
        self.text(page, f"Installed version: {VERSION}", "Secondary.TLabel", (0, 12))
        self.auto_update = tk.BooleanVar(value=self.prefs["check_for_updates"])
        ttk.Checkbutton(page, text="Check for updates automatically", style="Switch.TCheckbutton",
                        variable=self.auto_update, command=self.toggle_auto_update).pack(anchor="w", pady=(0, 12))
        self.update_status = tk.StringVar()
        ttk.Label(page, textvariable=self.update_status, style="Secondary.TLabel").pack(fill="x", pady=(0, 16))
        self.update_row = self.row(page)
        self.check_now = self.update_row.add(AdaptiveButton(self.update_row, text="Check now", command=lambda: self.start_update_check(manual=True)))
        self.install_now = self.update_row.add(AdaptiveButton(self.update_row, text="Install update…", style="Accent.TButton", command=self.prompt_update))
        self.release_notes = self.update_row.add(AdaptiveButton(self.update_row, text="Release notes", command=self.open_release_page))
        self.section(page, "Expert tracking settings", "Change these only when troubleshooting latency or drift. "
                     "Values come from the running driver and are confirmed after each change.")
        self.expert = self.disclosure(page, "Show expert settings")
        self.settings = {}
        for title, action, low, high in (("SDK prediction", "set_prediction", 0, 50), ("Pose time offset", "set_time_offset", -50, 100)):
            editor = NumberSetting(self.expert.content, title, action, low, high, self.run_action)
            editor.pack(fill="x", pady=(0, 24))
            self.settings[action] = editor
        for title, action in (("Map tracking", "set_cslam"), ("Angular velocity", "set_angular_velocity"),
                              ("Angular velocity in headset space", "set_angular_local")):
            editor = BooleanSetting(self.expert.content, title, action, self.run_action)
            editor.pack(fill="x", pady=(0, 12))
            self.settings[action] = editor
        self.section(page, "Diagnostics", "If something isn't working, copy diagnostics and include them when you ask for help.")
        row = self.row(page)
        row.add(AdaptiveButton(row, text="Copy diagnostics", command=self.copy_diagnostics))
        row.add(AdaptiveButton(row, text="Open log folder", command=self.open_log_folder))
        self.diagnostic_details = self.disclosure(page, "Show details")
        self.diagnostics = tk.StringVar()
        ttk.Label(self.diagnostic_details.content, textvariable=self.diagnostics, style="Secondary.TLabel").pack(fill="x")
        self.section(page, "About")
        self.text(page, f"Beyond-SLAM {VERSION} tracks a Bigscreen Beyond with an Xvisio XR50 in SteamVR. "
                  "© Fovea Labs LLC.", "Secondary.TLabel")

    # Mount offsets ------------------------------------------------------------------------

    def mount_edited(self, *_):
        if not self.mount_syncing:
            self.mount_dirty = True
            self.update_mount()

    def mount_payload(self):
        return parse_mount([self.offsets[key].get() for key in backend.MOUNT_KEYS])

    def profile_payload(self):
        return {"position": backend.MOUNT_PROFILE["position"][:], "rotation": backend.MOUNT_PROFILE["rotation"][:]}

    def apply_profile(self):
        if not self.profile_button.instate(["disabled"]):
            self.run_action("apply_mount_offsets", self.profile_payload(), source="profile")

    def nudge_level(self, pitch, roll):
        actual = mount_values(self.state)
        if actual is not None and not self.level_buttons[0].instate(["disabled"]):
            self.run_action("apply_mount_offsets", {"position": actual[:3],
                                                    "rotation": nudge_mount(actual[3:], pitch, roll)})

    def save_custom_mount(self):
        self.update_mount()
        if not self.mount_save.instate(["disabled"]):
            self.run_action("apply_mount_offsets", self.mount_payload())

    def update_mount(self, reset=False):
        actual = mount_values(self.state)
        if reset:
            self.mount_dirty = False
        if not self.mount_dirty:
            self.mount_syncing = True
            for key, value in zip(backend.MOUNT_KEYS, actual if actual is not None else [None] * 7):
                text = "" if value is None else f"{value:.9g}"
                if self.offsets[key].get() != text:
                    self.offsets[key].set(text)
            self.mount_syncing = False
        valid, same, error = False, False, ""
        try:
            payload = self.mount_payload()
            same, valid = mount_matches(payload, self.state), True
            if same:
                self.mount_dirty = False
        except ValueError as exc:
            error = str(exc) if self.mount_dirty else ""
        if self.mount_error.get() != error:
            self.mount_error.set(error)
        can_save = bool(self.state.get("driver_online") and self.state.get("config_available") or self.state.get("can_save_mount_offline") and self.state.get("steam_settings_ready"))
        applied = mount_matches(self.profile_payload(), self.state)
        status = ("✓ The Beyond Cover profile is applied." if applied else "Custom offsets are saved." if actual is not None else "Mount settings unavailable.")
        if self.mount_dirty:
            status = "Custom offsets have unsaved changes."
        if not can_save:
            status += " Start SteamVR, or close it to save offsets offline."
        if self.mount_status.get() != status:
            self.mount_status.set(status)
        busy = self.action_busy or bool(self.update_stage)
        enabled(self.profile_button, can_save and not applied and not busy)
        enabled(self.mount_save, can_save and valid and not same and not busy)
        enabled(self.mount_revert, self.mount_dirty and not busy)
        for entry in self.mount_entries:
            enabled(entry, can_save and not busy)
        # Nudging only makes sense while you can see the result in the headset.
        live = bool(self.state.get("driver_online") and self.state.get("config_available") and actual is not None)
        for button in self.level_buttons:
            enabled(button, live and not busy)
        if actual is None:
            level = ""
        else:
            pitch, roll = mount_trim(backend.MOUNT_PROFILE["rotation"], actual[3:])
            pitch, roll = round(pitch, 1) + 0.0, round(roll, 1) + 0.0  # + 0.0 turns -0.0 into 0.0
            level = ("No level adjustment from the bundled profile." if abs(pitch) < .05 and abs(roll) < .05
                     else f"Adjusted from the bundled profile: pitch {pitch:+.1f}°, roll {roll:+.1f}°.")
            if not live:
                level += " Start SteamVR to adjust."
        if self.level_status.get() != level:
            self.level_status.set(level)

    # Actions ------------------------------------------------------------------------------

    def choose_sdk_folder(self):
        folder = filedialog.askdirectory(parent=self.root, title="Choose SDK folder", initialdir=str(sdk_setup.sdk_initial_folder()), mustexist=True)
        if folder:
            self.run_action("use_sdk_folder", {"folder": folder})

    def next_action(self):
        action = setup_step(self.state).action
        if action == "open_steamvr_store":
            webbrowser.open("https://store.steampowered.com/app/250820/SteamVR/")
        elif action:
            self.run_action(action)

    def show_feedback(self, message="", error=False):
        if self.notice_timer is not None:
            self.root.after_cancel(self.notice_timer)
            self.notice_timer = None
        self.notice.set(message)
        style = "Error.TLabel" if error else "Success.TLabel"
        self.notice_label.configure(style=style)
        self.notice_icon.configure(style=style, text="⚠" if error else "✓")
        self.show(self.notice_frame, bool(message), fill="x", pady=(8, 0), before=self.page_area)
        if message and not error:
            self.notice_timer = self.root.after(8000, lambda: self.show_feedback())

    def copy_diagnostics(self):
        details = {"version": VERSION, "packaged": bool(getattr(sys, "frozen", False)),
                   "windows": platform.platform(), "python": platform.python_version(),
                   "display_dpi": self.display.dpi, "update_check": self.update_status.get()}
        report = f"Beyond-SLAM {VERSION}\n" + json.dumps(details, indent=2) + "\n\nState:\n" + json.dumps(self.state, indent=2, default=str)
        log = sdk_setup.data_folder() / "control.log"
        try:
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 8000))
                report += "\n\nRecent log:\n" + stream.read().decode("utf-8", errors="replace")
        except OSError:
            pass
        self.root.clipboard_clear()
        self.root.clipboard_append(report)
        self.show_feedback("Diagnostics copied. Paste them into your support message.")

    def open_log_folder(self):
        folder = sdk_setup.data_folder()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(folder)
        except (OSError, AttributeError):
            self.show_feedback(f"Couldn't open the log folder. It is at {folder}", error=True)

    def run_action(self, action, payload=None, source=None):
        if self.action_busy or self.update_stage or self.closed:
            return
        prompts = {"reset_slam": ("Restart tracking?", "Tracking will restart and floor height will need to be set again."),
                   "clear_world": ("Clear room alignment?", "The saved room alignment will be removed."),
                   "use_base_stations": ("Switch to base stations?", "SteamVR will use Lighthouse tracking for your headset "
                                         "the next time it starts. You can set up XR50 tracking again from Home.")}
        if action in prompts and not messagebox.askokcancel(*prompts[action], default="cancel", parent=self.root):
            return
        self.show_feedback()
        self.operation = {"action": action, "payload": payload or {}, "sent": False, "source": source,
                          "floor_started": self.state.get("floor_scan") != 2}
        self.action_busy = True
        if self.busy:
            self.pending_action = (action, payload or {})
            self.update_views()
        else:
            self.start_worker(action, payload or {})

    def start_worker(self, action=None, payload=None):
        if self.poll_id is not None:
            self.root.after_cancel(self.poll_id)
            self.poll_id = None
        self.busy = True
        self.update_views()
        def work():
            result = None
            try:
                if action:
                    result = backend.dispatch(action, payload or {})
                self.events.put((backend.inspect(), action, result, None))
            except Exception as exc:
                logging.exception("Driver operation failed")
                self.events.put((None, action, result, exc))
        threading.Thread(target=work, daemon=True).start()

    def refresh(self):
        self.poll_id = None
        if not self.busy and not self.closed:
            self.start_worker()

    def finish_operation(self, message, error=False):
        reset_mount = not error and self.operation and self.operation.get("source") == "profile"
        self.operation, self.action_busy = None, False
        if reset_mount:
            self.update_mount(reset=True)
        self.show_feedback(message, error)

    def drain(self):
        if self.closed:
            return
        if self.drain_id is not None:
            self.root.after_cancel(self.drain_id)
            self.drain_id = None
        try:
            self.drain_updates()
            self.drain_backend()
        finally:
            # One failed redraw must never stop the app from polling again.
            if not self.closed:
                self.drain_id = self.root.after(80, self.drain)

    def drain_backend(self):
        try:
            state, action, result, error = self.events.get_nowait()
        except queue.Empty:
            return
        self.busy = False
        try:
            if state is not None:
                self.state = state
            if error is not None:
                message = str(error) if isinstance(error, (ValueError, RuntimeError)) else "Could not complete this step. Check Diagnostics and try again."
                if action:
                    self.finish_operation(message, True)
                elif self.operation is None:
                    self.state = {}
                    self.show_feedback(message, True)
            if self.pending_action:
                name, payload = self.pending_action
                self.pending_action = None
                self.start_worker(name, payload)
                return
            if self.operation and action and error is None:
                self.operation.update(sent=True, result=result, observed_at=time.monotonic())
            if self.operation and self.operation["sent"]:
                if self.operation["action"] == "start_floor_scan" and self.state.get("floor_scan") == 1:
                    self.operation["floor_started"] = True
                outcome = action_outcome(self.operation["action"], self.operation["payload"], self.state, self.operation.get("result"))
                stale_floor_result = self.operation["action"] == "start_floor_scan" and not self.operation["floor_started"] and not outcome.error
                if outcome.complete and not stale_floor_result:
                    self.finish_operation(outcome.message, outcome.error)
                elif time.monotonic() - self.operation["observed_at"] > (12 if self.operation["action"] == "start_floor_scan" else 8):
                    self.finish_operation("The change could not be confirmed. Check the connection and try again.", True)
            self.update_views()
            if self.close_requested and not self.action_busy:
                self.close()
        finally:
            if not self.closed and not self.busy and self.poll_id is None:
                self.poll_id = self.root.after(300 if self.operation else 1500, self.refresh)

    # Views --------------------------------------------------------------------------------

    def update_views(self):
        step = setup_step(self.state)
        action = step.action
        title, description = step.title, step.description
        if self.operation:
            title, description = {"install_sdk": ("Installing the SDK…", "Follow the Xvisio setup window when it opens."),
                "setup_tracking": ("Setting up tracking…", "Keep SteamVR closed until setup finishes."),
                "launch_steamvr": ("Starting SteamVR…", "Your tracking session will be ready shortly."),
                "start_floor_scan": ("Setting floor height…", "Keep the headset still on the floor for two seconds.")}.get(self.operation["action"], ("Saving your change…", "Waiting for the driver to confirm the result."))
        for variable, text in ((self.step_title, title), (self.next_step, description), (self.step_count, progress_text(self.state))):
            if variable.get() != text:
                variable.set(text)
        ready = self.state.get("driver_online") and self.state.get("floor_scan") == 2 and self.state.get("settled")
        summary, style = (("Working…", "Accent.TLabel") if self.operation else ("● Tracking ready", "Success.TLabel") if ready else
                          ("● Tracking connected", "Accent.TLabel") if self.state.get("driver_online") else
                          ("● Checking…", "Secondary.TLabel") if not self.state else
                          ("● Setup needed", "Warning.TLabel") if not self.state.get("installed") or not self.state.get("sdk_ready") else
                          ("● SteamVR closed", "Secondary.TLabel") if not self.state.get("steamvr_running") else ("● Connecting…", "Secondary.TLabel"))
        if self.status.get() != summary:
            self.status.set(summary)
            self.status_label.configure(style=style)
            self.root.after_idle(self.header.reflow)
        busy = self.action_busy or bool(self.update_stage)
        caption = {"install_sdk": "Download SDK installer", "setup_tracking": "Set up tracking", "launch_steamvr": "Start SteamVR", "open_steamvr_store": "Open SteamVR in Steam", "start_floor_scan": "Set floor height"}.get(action, "Working…")
        if busy:
            caption = "Working…"
        if self.primary.caption != caption:
            self.primary.configure(text=caption)
        visible = ([self.primary] if action else []) + ([self.home_folder] if not self.state.get("sdk_ready") and self.state and not self.state.get("steamvr_running") else []) + ([self.recalibrate] if self.state.get("driver_online") and self.state.get("settled") and self.state.get("floor_scan") == 2 else [])
        self.set_row(self.primary_row, visible)
        enabled(self.primary, bool(action) and not busy)
        enabled(self.home_folder, not busy)
        enabled(self.recalibrate, not busy and bool(self.state.get("slam_ready")))
        working = bool(self.operation or self.state.get("floor_scan") == 1)
        if working != self.progress_running:
            if working:
                self.progress.start(12)
            else:
                self.progress.stop()
            self.progress_running = working
        self.show(self.progress, working, anchor="w", pady=(0, 24), before=self.primary_row)
        note = "Set floor height again after restarting SteamVR." if self.state.get("driver_online") and self.state.get("settled") else ""
        if self.session_note.get() != note:
            self.session_note.set(note)
        self.show(self.session_note_label, bool(note), fill="x", pady=(4, 0))
        for (mark, caption), (_, status) in zip(self.checklist_rows, checklist(self.state)):
            symbol, mark_style = CHECK_MARKS[status]
            if mark.cget("text") != symbol:
                mark.configure(text=symbol, style=mark_style)
                caption.configure(style="Strong.TLabel" if status == "current" else "TLabel" if status == "done" else "Secondary.TLabel")
        location = self.state.get("sdk_path")
        if location:
            location = Path(location)
            if location.name.lower() == "bin":
                location = location.parent
        sdk_status = f"✓ SDK found at {location}" if self.state.get("sdk_ready") and location else "✓ SDK detected automatically." if self.state.get("sdk_ready") else "SDK not found. Download and run the installer, or choose the folder where it is installed."
        if self.sdk_location.get() != sdk_status:
            self.sdk_location.set(sdk_status)
        for variable, label, text, anchor in ((self.tools_hint, self.tools_hint_label, tools_hint(self.state) if self.state else "", self.map_row),
                                             (self.recovery_hint, self.recovery_hint_label, recovery_hint(self.state), self.recovery_row)):
            if variable.get() != text:
                variable.set(text)
            self.show(label, bool(text), fill="x", pady=(0, 12), before=anchor)
        for button, predicate in self.guarded:
            enabled(button, not busy and bool(predicate(self.state)))
        for editor in self.settings.values():
            editor.refresh(self.state, busy, self.operation["action"] if self.operation else None)
        self.update_mount()
        alignment = "Room alignment: " + ("active" if self.state.get("world_active") else "not active")
        if self.tracking_status.get() != alignment:
            self.tracking_status.set(alignment)
        diagnostics = f"SteamVR: {self.state.get('steamvr_path', '') or 'not found'}\nDriver: {self.state.get('driver_path', '')}\nLogs: {sdk_setup.data_folder()}\n\n{self.state.get('status', '')}"
        if self.diagnostics.get() != diagnostics:
            self.diagnostics.set(diagnostics)

    def bring_to_front(self):
        if self.closed:
            return
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(250, lambda: self.closed or self.root.attributes("-topmost", False))
        self.root.focus_force()

    def style_window(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        dark_title_bar(self.root)
        if not hasattr(self, "app_icon"):
            icon = backend.ROOT / "assets/beyond-slam-icon.png"
            if icon.is_file():
                try:
                    self.app_icon = tk.PhotoImage(master=self.root, file=str(icon))
                    self.root.iconphoto(True, self.app_icon)
                except tk.TclError:
                    logging.warning("Could not load the window icon", exc_info=True)

    def scroll_page(self, event):
        widget = event.widget
        if isinstance(widget, str):
            return
        while widget is not None:
            if isinstance(widget, tk.Text):
                return
            if isinstance(widget, ScrollPage):
                bounds = widget.canvas.bbox("all")
                if bounds and bounds[3] > widget.canvas.winfo_height():
                    widget.canvas.yview_scroll(-int(event.delta / 120) or (-1 if event.delta > 0 else 1), "units")
                return "break"
            widget = getattr(widget, "master", None)

    # Updates ------------------------------------------------------------------------------

    def schedule_update_check(self, delay):
        if self.update_timer is not None:
            self.root.after_cancel(self.update_timer)
        self.update_timer = self.root.after(delay, self.auto_update_check)

    def auto_update_check(self):
        self.update_timer = None
        if self.closed:
            return
        if self.prefs["check_for_updates"]:
            self.start_update_check(manual=False)
        self.schedule_update_check(UPDATE_CHECK_INTERVAL_MS)

    def start_update_check(self, manual=False):
        if self.update_checking or self.update_stage or self.closed:
            return
        self.update_checking, self.update_manual = True, manual
        self.update_update_views()
        def work():
            try:
                self.update_events.put(("checked", updater.check_for_update(), None))
            except updater.UpdateError as exc:
                logging.info("Update check failed: %s", exc)
                self.update_events.put(("checked", None, str(exc)))
            except Exception:
                logging.exception("Update check failed")
                self.update_events.put(("checked", None, "Couldn't check for updates. Try again later."))
        threading.Thread(target=work, daemon=True).start()

    def drain_updates(self):
        while True:
            try:
                kind, value, error = self.update_events.get_nowait()
            except queue.Empty:
                return
            getattr(self, "update_" + kind)(value, error)
            self.update_update_views()

    def update_checked(self, info, error):
        self.update_checking = False
        self.prefs["last_update_check"] = time.time()
        preferences.save(self.prefs)
        if error:
            self.update_error = error
            if self.update_manual:
                self.show_feedback(error, error=True)
            return
        self.update_error, self.update_info = "", info
        if info is None:
            if self.update_manual:
                self.show_feedback("Beyond-SLAM is up to date.")
        elif self.update_manual or (info.version != self.prefs["skipped_version"] and info.version not in self.update_prompted):
            self.prompt_update()

    def prompt_update(self):
        info = self.update_info
        if info is None or self.closed:
            return
        self.update_prompted.add(info.version)
        if self.update_dialog is not None and self.update_dialog.winfo_exists():
            self.update_dialog.lift()
            return
        self.update_dialog = UpdateDialog(self.root, info, updater.current_version(), self.install_update, self.dismiss_update,
                                          self.skip_update, self.cancel_update, can_install=updater.can_install())
        self.update_update_views()

    def update_blocker(self):
        try:
            running = backend.steamvr_running()
        except OSError:
            running = True
        if running:
            return "Close SteamVR first. The update replaces the tracking driver, which SteamVR keeps in use."
        if self.action_busy:
            return "Wait for the current step to finish, then try again."
        return ""

    def install_update(self):
        info, dialog = self.update_info, self.update_dialog
        if info is None or self.update_stage:
            return
        if not updater.can_install():
            if info.page_url:
                webbrowser.open(info.page_url)
            self.dismiss_update()
            return
        blocker = self.update_blocker()
        if blocker:
            if dialog is not None:
                dialog.show_error(blocker)
            else:
                self.show_feedback(blocker, error=True)
            return
        self.update_stage, self.update_cancel = "downloading", threading.Event()
        if dialog is not None:
            dialog.show_downloading()
        cancel = self.update_cancel
        def progress(done, total):
            self.update_events.put(("progress", (done, total), None))
        def work():
            try:
                self.update_events.put(("downloaded", updater.download_update(info, progress, cancel), None))
            except updater.UpdateCancelled:
                self.update_events.put(("download_failed", None, ""))
            except updater.UpdateError as exc:
                logging.warning("Update download failed: %s", exc)
                self.update_events.put(("download_failed", None, str(exc)))
            except Exception:
                logging.exception("Update download failed")
                self.update_events.put(("download_failed", None, "The update couldn't be downloaded. Try again later."))
        threading.Thread(target=work, daemon=True).start()
        self.update_views()

    def update_progress(self, value, error):
        if self.update_stage == "downloading" and self.update_dialog is not None and self.update_dialog.winfo_exists():
            self.update_dialog.show_downloading(*value)

    def update_downloaded(self, path, error):
        if self.closed or self.update_stage != "downloading":
            return
        dialog = self.update_dialog
        blocker = self.update_blocker()
        if blocker:
            self.update_stage = None
            if dialog is not None:
                dialog.show_error(blocker)
            self.update_views()
            return
        self.update_stage = "installing"
        if dialog is not None:
            dialog.show_installing()
        try:
            updater.launch_installer(path, self.update_info.version)
        except updater.UpdateError as exc:
            self.update_stage = None
            if dialog is not None:
                dialog.show_error(str(exc))
            else:
                self.show_feedback(str(exc), error=True)
            self.update_views()
            return
        self.root.after(600, lambda: self.close(force=True))

    def update_download_failed(self, value, error):
        self.update_stage = self.update_cancel = None
        dialog = self.update_dialog
        if error and dialog is not None and dialog.winfo_exists():
            dialog.show_error(error)
        elif error:
            self.show_feedback(error, error=True)
        else:
            self.dismiss_update()
            self.show_feedback("Update cancelled. Install it any time from Settings → Updates.")
        self.update_views()

    def cancel_update(self):
        if self.update_cancel is not None:
            self.update_cancel.set()

    def dismiss_update(self):
        if self.update_dialog is not None:
            if self.update_dialog.winfo_exists():
                self.update_dialog.close()
            self.update_dialog = None
        self.update_update_views()

    def skip_update(self):
        if self.update_info is not None:
            self.prefs["skipped_version"] = self.update_info.version
            preferences.save(self.prefs)
            self.show_feedback(f"Version {self.update_info.version} skipped. You can still install it from Settings → Updates.")
        self.dismiss_update()

    def open_release_page(self):
        if self.update_info is not None and self.update_info.page_url:
            webbrowser.open(self.update_info.page_url)

    def toggle_auto_update(self):
        self.prefs["check_for_updates"] = bool(self.auto_update.get())
        if not preferences.save(self.prefs):
            self.show_feedback("Couldn't save this preference. It applies until you close the app.", error=True)
        if self.prefs["check_for_updates"] and time.time() - self.prefs["last_update_check"] > 3600:
            self.start_update_check(manual=False)
        self.update_update_views()

    def update_update_views(self):
        info, skipped = self.update_info, self.prefs["skipped_version"]
        if self.update_stage == "downloading":
            status = f"Downloading version {info.version}…"
        elif self.update_stage == "installing":
            status = "Starting the installer…"
        elif self.update_checking:
            status = "Checking for updates…"
        elif info is not None:
            status = f"Version {info.version} is available." + (" You chose to skip it." if info.version == skipped else "")
        elif self.update_error:
            status = f"Last check didn't finish: {self.update_error}"
        elif self.prefs["last_update_check"]:
            checked = time.strftime("%b %d at %H:%M", time.localtime(self.prefs["last_update_check"]))
            status = f"Beyond-SLAM is up to date. Last checked {checked}."
        else:
            status = "Not checked yet."
        if not self.prefs["check_for_updates"] and not info and not self.update_checking:
            status += " Automatic checks are off."
        if self.update_status.get() != status:
            self.update_status.set(status)
        idle = not self.update_checking and not self.update_stage
        self.set_row(self.update_row, [self.check_now] + ([self.install_now] if info else []) +
                     ([self.release_notes] if info and info.page_url else []))
        enabled(self.check_now, idle)
        enabled(self.install_now, idle)
        dialog_open = self.update_dialog is not None and self.update_dialog.winfo_exists()
        banner = bool(info and info.version != skipped and not dialog_open and not self.update_stage)
        text = f"Beyond-SLAM {info.version} is available." if info else ""
        if self.update_banner_text.get() != text:
            self.update_banner_text.set(text)
        self.show(self.update_banner, banner, fill="x", pady=(4, 4), before=self.nav)

    # Shutdown -----------------------------------------------------------------------------

    def close(self, force=False):
        if self.closed:
            return
        if not force and self.busy and self.operation and self.operation["action"] in UNINTERRUPTIBLE:
            self.close_requested = True
            self.show_feedback("Finishing the current change before closing.")
            return
        if not force and self.update_stage == "installing":
            return
        self.closed = True
        if self.update_cancel is not None:
            self.update_cancel.set()
        self.display.close()
        self.progress.stop()
        for timer in (self.poll_id, self.drain_id, self.notice_timer, self.update_timer, self.recolor_id):
            if timer is not None:
                self.root.after_cancel(timer)
        if self.update_dialog is not None and self.update_dialog.winfo_exists():
            self.update_dialog.close()
        self.root.destroy()


def configure_logging(location):
    try:
        location.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(location / "control.log", maxBytes=1048576, backupCount=3, encoding="utf-8")
    except OSError:
        handler = logging.StreamHandler()
    logging.basicConfig(handlers=[handler], level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def write_result(location, text):
    try:
        (location / "setup-result.txt").write_text(text, encoding="utf-8")
    except OSError:
        logging.warning("Could not write the setup result", exc_info=True)


def main():
    if os.name != "nt":
        print("Beyond-SLAM requires Windows x64 and SteamVR.", file=sys.stderr)
        return 2
    location = sdk_setup.data_folder()
    configure_logging(location)
    logging.info("Beyond-SLAM %s starting (%s)", VERSION, " ".join(sys.argv[1:]) or "interactive")
    if "--setup" in sys.argv:
        try:
            backend.install_driver(enable_tracking=True)
            write_result(location, "Tracking setup completed.")
            return 0
        except Exception as exc:
            logging.exception("Tracking setup failed")
            write_result(location, str(exc))
            return 1
    if "--refresh-driver" in sys.argv:
        # Updates refresh installed driver files but keep the user's tracking source.
        try:
            target = backend.driver_path()
            if target and (target / "driver.vrdrivermanifest").is_file():
                backend.install_driver()
            write_result(location, "")
            return 0
        except Exception as exc:
            logging.exception("Driver refresh failed")
            write_result(location, str(exc))
            return 1
    if "--unregister" in sys.argv:
        try:
            backend.unregister_driver()
            return 0
        except Exception:
            logging.exception("Could not unregister driver")
            return 1
    enable_dpi_awareness()
    # Let the installer detect an open app before replacing its runtime files.
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    mutex = kernel.CreateMutexW(None, False, "BeyondSLAMControl")
    if not mutex:
        raise OSError(ctypes.get_last_error(), "Could not create application mutex")
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(mutex)
        restore_existing_instance()
        return 0
    root = None
    try:
        root = tk.Tk()
        try:
            import sv_ttk
            sv_ttk.set_theme("dark")
        except ImportError:
            messagebox.showerror("Missing theme", "Install the app requirements first:\npython -m pip install -r requirements.txt", parent=root)
            return 2
        ui = None
        def callback_error(exc_type, value, traceback):
            logging.error("Interface error", exc_info=(exc_type, value, traceback))
            if ui is not None and not ui.closed:
                ui.show_feedback("Something went wrong. Copy diagnostics from Settings and try again.", error=True)
        root.report_callback_exception = callback_error
        ui = ControlApp(root)
        root.mainloop()
        return 0
    except Exception:
        logging.exception("Beyond-SLAM stopped unexpectedly")
        try:
            messagebox.showerror("Beyond-SLAM", "Beyond-SLAM couldn't start. Reinstalling usually fixes this.\n\n"
                                 f"Details were saved to {location / 'control.log'}", parent=root)
        except tk.TclError:
            pass
        return 1
    finally:
        try:
            if root is not None:
                root.destroy()
        except tk.TclError:
            pass
        kernel.CloseHandle(mutex)


if __name__ == "__main__":
    sys.exit(main())
