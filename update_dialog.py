"""Modal prompt that offers an update, shows download progress, and reports problems."""
import tkinter as tk
from tkinter import ttk

from display import AdaptiveButton, FlowRow, dark_title_bar, ui_scale, wrap_labels


class UpdateDialog(tk.Toplevel):
    def __init__(self, parent, update, current, on_install, on_later, on_skip, on_cancel, can_install=True):
        super().__init__(parent)
        self.withdraw()
        self.on_install, self.on_later, self.on_skip, self.on_cancel = on_install, on_later, on_skip, on_cancel
        self.stage = "prompt"
        self.title("Beyond-SLAM update")
        self.transient(parent)
        self.configure(background="#1c1c1c")
        scale = ui_scale(self)
        px = lambda value: round(value * scale)
        body = ttk.Frame(self, padding=px(24))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"Beyond-SLAM {update.version} is available", font="AppHeadingFont").pack(fill="x", pady=(0, px(8)))
        self.detail = tk.StringVar(value=(
            f"You have version {current}. Beyond-SLAM will close, install the update, and reopen by itself. "
            "Your SDK, mount, and SteamVR settings are kept." if can_install else
            f"You have version {current}. This copy runs from source, so update it with git or install the "
            "new version from the release page."))
        ttk.Label(body, textvariable=self.detail, style="Secondary.TLabel").pack(fill="x", pady=(0, px(16)))
        self.message = tk.StringVar()
        self.message_label = ttk.Label(body, textvariable=self.message, style="Error.TLabel")
        self.progress = ttk.Progressbar(body, mode="determinate", maximum=1000)
        self.feedback = ttk.Frame(body)
        self.feedback.pack(fill="x")
        buttons = FlowRow(body)
        buttons.gap, buttons.bottom = px(12), px(4)
        buttons.pack(fill="x", side="bottom", pady=(px(16), 0))
        self.install = buttons.add(AdaptiveButton(buttons, text="Update and restart" if can_install else "Open release page",
                                                  style="Accent.TButton", command=self.install_clicked))
        self.later = buttons.add(AdaptiveButton(buttons, text="Not now", command=self.close_clicked))
        self.skip = buttons.add(AdaptiveButton(buttons, text="Skip this version", command=self.skip_clicked))
        self.buttons = buttons
        if update.notes:
            ttk.Label(body, text="What's new", font="AppBodyFont").pack(fill="x", pady=(0, px(8)))
            notes = ttk.Frame(body)
            notes.pack(fill="both", expand=True)
            text = tk.Text(notes, wrap="word", height=8, relief="flat", borderwidth=0, highlightthickness=0,
                           background="#2b2b2b", foreground="#fafafa", insertbackground="#fafafa",
                           font="AppBodyFont", padx=px(12), pady=px(10))
            scroll = ttk.Scrollbar(notes, orient="vertical", command=text.yview)
            text.configure(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
            text.pack(side="left", fill="both", expand=True)
            text.insert("1.0", update.notes)
            text.configure(state="disabled")
        wrap_labels(body, lambda: ui_scale(self), limit=640)
        self.protocol("WM_DELETE_WINDOW", self.close_clicked)
        self.bind("<Escape>", lambda event: self.close_clicked())
        width, height = px(600), px(520 if update.notes else 300)
        parent.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - height) // 3
        width, height = min(width, self.winfo_screenwidth()), min(height, self.winfo_screenheight() - px(80))
        x = max(0, min(x, self.winfo_screenwidth() - width))
        y = max(0, min(y, self.winfo_screenheight() - height - px(40)))
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(min(width, px(360)), min(height, px(240)))
        self.deiconify()
        dark_title_bar(self)
        self.install.focus_set()
        try:
            self.grab_set()
        except tk.TclError:
            pass  # Another grab is active; the dialog still works without one.

    def install_clicked(self):
        if self.stage in ("prompt", "error"):
            self.on_install()

    def skip_clicked(self):
        if self.stage in ("prompt", "error"):
            self.on_skip()

    def close_clicked(self):
        if self.stage == "downloading":
            self.on_cancel()
        elif self.stage != "installing":
            self.on_later()

    def show_feedback(self, widget, visible, scale_padding=12):
        if visible and not widget.winfo_manager():
            widget.pack(in_=self.feedback, fill="x", pady=(0, round(scale_padding * ui_scale(self))))
        elif not visible and widget.winfo_manager():
            widget.pack_forget()

    def set_buttons(self, items):
        if self.buttons.items != items:
            for item in (self.install, self.later, self.skip):
                item.place_forget()
            self.buttons.items = items
            self.buttons.reflow()

    def show_downloading(self, done=0, total=0):
        self.stage = "downloading"
        self.message.set("")
        self.show_feedback(self.message_label, False)
        self.show_feedback(self.progress, True)
        self.progress.configure(value=round(1000 * done / total) if total else 0)
        megabytes = f"{done / 1048576:.1f} of {total / 1048576:.1f} MB" if total else ""
        self.detail.set(f"Downloading the update… {megabytes}".strip())
        self.later.configure(text="Cancel download")
        self.set_buttons([self.later])

    def show_installing(self):
        self.stage = "installing"
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.detail.set("Starting the installer. Beyond-SLAM will close and reopen when the update finishes.")
        self.set_buttons([])

    def show_error(self, message):
        self.stage = "error"
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.show_feedback(self.progress, False)
        self.message.set(message)
        self.show_feedback(self.message_label, True)
        self.install.configure(text="Try again")
        self.later.configure(text="Not now")
        self.set_buttons([self.install, self.later, self.skip])

    def close(self):
        self.progress.stop()
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
