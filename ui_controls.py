"""Small setting editors that preserve drafts and display confirmed device values."""
import tkinter as tk
from tkinter import ttk

from display import AdaptiveButton, FlowRow
from ui_state import number, setting_value


def enabled(widget, value):
    if widget.instate(["disabled"]) == bool(value):
        widget.state(["!disabled" if value else "disabled"])


class NumberSetting(ttk.Frame):
    def __init__(self, parent, title, action, low, high, submit):
        super().__init__(parent)
        self.action, self.low, self.high, self.submit = action, low, high, submit
        self.current = None
        self.dirty = self.syncing = self.busy = False
        self.state = {}
        ttk.Label(self, text=title).pack(fill="x", pady=(0, 8))
        row = FlowRow(self)
        row.pack(fill="x")
        self.value = tk.StringVar()
        self.entry = row.add(ttk.Entry(row, textvariable=self.value, width=10))
        row.add(ttk.Label(row, text="ms"))
        self.apply = row.add(AdaptiveButton(row, text="Apply", command=self.save))
        self.hint = tk.StringVar()
        self.hint_label = ttk.Label(self, textvariable=self.hint, style="Secondary.TLabel")
        self.hint_label.pack(fill="x")
        self.value.trace_add("write", self.edited)
        self.entry.bind("<Return>", lambda event: self.save())
        self.refresh({}, False)

    def edited(self, *_):
        if not self.syncing:
            self.dirty = True
            self.refresh(self.state, self.busy)

    def refresh(self, state, busy, active_action=None):
        self.state, self.busy = state, busy
        self.current = setting_value(state, self.action)
        if not self.dirty:
            text = "" if self.current is None else f"{self.current:g}"
            if self.value.get() != text:
                self.syncing = True
                self.value.set(text)
                self.syncing = False
        error = ""
        try:
            value = number(self.value.get(), self.low, self.high)
            if self.current is not None and abs(value - self.current) < 1e-3:
                self.dirty = False
        except ValueError as exc:
            error = str(exc) if self.dirty else ""
        enabled(self.entry, self.current is not None and not busy)
        enabled(self.apply, self.current is not None and not busy and self.dirty and not error)
        invalid = bool(error)
        if self.entry.instate(["invalid"]) != invalid:
            self.entry.state(["invalid" if invalid else "!invalid"])
        hint = (error or ("Saving…" if active_action == self.action else
                "Start SteamVR to view this setting." if self.current is None else
                f"{self.low:g} to {self.high:g} ms" + (" · Unsaved changes" if self.dirty else "")))
        if self.hint.get() != hint:
            self.hint.set(hint)
            self.hint_label.configure(style="Error.TLabel" if error else "Secondary.TLabel")

    def save(self):
        self.refresh(self.state, self.busy)
        if self.apply.instate(["disabled"]):
            return
        self.submit(self.action, {"value": number(self.value.get(), self.low, self.high)})


class BooleanSetting(ttk.Frame):
    def __init__(self, parent, title, action, submit):
        super().__init__(parent)
        self.action, self.submit = action, submit
        self.current = None
        self.value = tk.BooleanVar()
        self.status = tk.StringVar(value="Unavailable")
        row = FlowRow(self)
        row.pack(fill="x")
        row.add(ttk.Label(row, text=title))
        self.switch = row.add(ttk.Checkbutton(row, style="Switch.TCheckbutton", variable=self.value, command=self.save))
        row.add(ttk.Label(row, textvariable=self.status, style="Secondary.TLabel"))
        enabled(self.switch, False)

    def refresh(self, state, busy, active_action=None):
        self.current = setting_value(state, self.action)
        if self.value.get() != bool(self.current):
            self.value.set(bool(self.current))
        text = "Saving…" if active_action == self.action else "Unavailable" if self.current is None else "On" if self.current else "Off"
        if self.status.get() != text:
            self.status.set(text)
        enabled(self.switch, self.current is not None and not busy)

    def save(self):
        desired = self.value.get()
        self.value.set(bool(self.current))
        if self.current is not None and not self.switch.instate(["disabled"]):
            self.submit(self.action, {"enabled": desired})
