"""DPI-aware sizing and responsive Tk containers for the desktop UI."""
import ctypes
from ctypes import wintypes
import os
import tkinter as tk
from tkinter import font, ttk


def enable_dpi_awareness():
    """Source runs need the same awareness as the packaged executable manifest."""
    if os.name != "nt":
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        # Python's own manifest may have already selected process awareness.
        user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass


def ui_scale(widget):
    """Current interface scale, where 1.0 is 96 DPI."""
    try:
        return float(widget.tk.call("tk", "scaling")) * 72 / 96
    except (tk.TclError, ValueError):
        return 1.0


def wrap_labels(widget, scale_of, limit=720):
    """Wrap every label to its own width, capped to a comfortable reading length."""
    if isinstance(widget, ttk.Label) and not getattr(widget, "fixed_width", False):
        widget.configure(justify="left", anchor="w")
        widget.bind("<Configure>", lambda event: event.widget.configure(
            wraplength=max(1, min(event.width, round(limit * scale_of())))), add="+")
    for child in widget.winfo_children():
        wrap_labels(child, scale_of, limit)


def fit_window(width, height, x, y, workarea):
    """Keep the outer window within a signed Windows monitor work area."""
    left, top, right, bottom = workarea
    width, height = min(width, right - left), min(height, bottom - top)
    return width, height, max(left, min(x, right - width)), max(top, min(y, bottom - height))


class AdaptiveButton(ttk.Button):
    """Keep the full action label visible even on a small, highly scaled screen."""
    def __init__(self, parent, **kwargs):
        self.caption = kwargs.get("text", "")
        super().__init__(parent, **kwargs)
        parent.bind("<Configure>", lambda event: self.fit(event.width), add="+")

    def configure(self, cnf=None, **kwargs):
        if "text" in kwargs:
            self.caption = kwargs["text"]
            self.after_idle(lambda: self.fit(self.master.winfo_width()))
        return super().configure(cnf, **kwargs)

    def fit(self, width):
        style = ttk.Style(self)
        face = font.Font(root=self, font=style.lookup(self.cget("style") or "TButton", "font") or "TkDefaultFont")
        scale = float(self.tk.call("tk", "scaling")) * 72 / 96
        available = max(1, width - round(40 * scale))
        lines = [""]
        for word in self.caption.split():
            if face.measure(word) > available:
                if lines[-1]:
                    lines.append("")
                for character in word:
                    if lines[-1] and face.measure(lines[-1] + character) > available:
                        lines.append("")
                    lines[-1] += character
                lines.append("")
                continue
            candidate = (lines[-1] + " " + word).strip()
            if lines[-1] and face.measure(candidate) > available:
                lines.append(word)
            else:
                lines[-1] = candidate
        text = "\n".join(lines).rstrip()
        if self.cget("text") != text:
            super().configure(text=text)


class FlowRow(ttk.Frame):
    """Wrap actions and fields without reducing their font or target size."""
    def __init__(self, parent):
        super().__init__(parent)
        self.items = []
        self.gap = 12
        self.bottom = 16
        self.bind("<Configure>", lambda event: self.reflow())

    def add(self, widget):
        self.items.append(widget)
        widget.place(x=0, y=0)
        return widget

    def reflow(self):
        available = max(1, self.winfo_width())
        used = y = line_height = 0
        line = []
        def place_line():
            for widget, x, width, height in line:
                widget.place(x=x, y=y + (line_height - height) // 2, width=width)
        for widget in self.items:
            if isinstance(widget, AdaptiveButton):
                widget.fit(available - self.gap)
            if isinstance(widget, ttk.Label):
                widget.configure(wraplength=max(1, available - self.gap))
            width = min(widget.winfo_reqwidth(), available)
            if used and used + width > available:
                place_line()
                y += line_height + self.bottom
                used = line_height = 0
                line = []
            line.append((widget, used, width, widget.winfo_reqheight()))
            used += width + self.gap
            line_height = max(line_height, widget.winfo_reqheight())
        place_line()
        height = y + line_height + self.bottom
        if int(self.cget("height")) != height:
            self.configure(height=height)


class ScrollPage(ttk.Frame):
    def __init__(self, parent, padding=24):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, background="#1c1c1c")
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=self.update_scrollbar)
        self.content = ttk.Frame(self.canvas, padding=padding)
        self.item = self.canvas.create_window(0, 0, anchor="nw", window=self.content)
        self.canvas.bind("<Configure>", self.resize)
        self.content.bind("<Configure>", lambda event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

    def resize(self, event):
        self.canvas.itemconfigure(self.item, width=event.width)

    def scroll_to_top(self):
        self.canvas.yview_moveto(0)

    def update_scrollbar(self, first, last):
        needed = float(first) > .001 or float(last) < .999
        if needed and not self.scroll.winfo_manager():
            self.scroll.pack(side="right", fill="y", before=self.canvas)
        elif not needed and self.scroll.winfo_manager():
            self.scroll.pack_forget()
        self.scroll.set(first, last)


class CenteredColumn(ttk.Frame):
    """Center content with a readable maximum width on large or high-resolution windows."""
    def __init__(self, parent, max_width=980, padding=20):
        super().__init__(parent)
        self.max_width, self.padding = max_width, padding
        self.content = ttk.Frame(self)
        self.bind("<Configure>", lambda event: self.relayout())

    def relayout(self):
        scale = ui_scale(self)
        margin = round(self.padding * scale)
        available = max(1, self.winfo_width() - 2 * margin)
        width = min(available, round(self.max_width * scale))
        self.content.place(x=(self.winfo_width() - width) // 2, y=margin, width=width,
                           height=max(1, self.winfo_height() - 2 * margin))


class Disclosure(ttk.Frame):
    """Keep optional controls reachable by mouse and keyboard without crowding a page."""
    def __init__(self, parent, title):
        super().__init__(parent)
        self.title = title
        self.expanded = False
        self.toggle = AdaptiveButton(self, text="▸ " + title, command=self.toggle_content)
        self.toggle.pack(anchor="w")
        self.content = ttk.Frame(self, padding=(0, 20, 0, 0))

    def toggle_content(self):
        self.expanded = not self.expanded
        self.toggle.configure(text=("▾ " if self.expanded else "▸ ") + self.title)
        if self.expanded:
            self.content.pack(fill="x")
        else:
            self.content.pack_forget()


class DisplayManager:
    def __init__(self, root):
        self.root = root
        self.dpi = 96
        self.monitor = None
        self.timer = None
        self.logical_size = (900, 760)
        self.fonts = {}
        self.spacing = []
        self.listeners = []
        self.user32 = None
        if os.name == "nt":
            self.user32 = ctypes.WinDLL("user32", use_last_error=True)
            self.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            self.user32.GetAncestor.restype = wintypes.HWND
            self.user32.GetDpiForWindow.argtypes = [wintypes.HWND]
            self.user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            self.user32.MonitorFromWindow.restype = wintypes.HANDLE
            self.user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
            self.user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            self.user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                               ctypes.c_int, ctypes.c_int, wintypes.UINT]

    def start(self):
        for name in font.names(self.root):
            self.fonts[name] = font.nametofont(name, self.root).cget("size")
        def numbers(value):
            if isinstance(value, (int, float)):
                return (float(value),)
            return tuple(float(x) for x in self.root.tk.splitlist(value))
        def collect(widget):
            options = {}
            if "padding" in widget.keys():
                value = widget.cget("padding")
                if value:
                    options["padding"] = numbers(value)
            packed = widget.pack_info() if widget.winfo_manager() == "pack" else {}
            packing = {key: numbers(packed[key])
                       for key in ("padx", "pady") if key in packed}
            self.spacing.append((widget, options, packing))
            for child in widget.winfo_children():
                collect(child)
        collect(self.root)
        self.poll(initial=True)

    def apply_scale(self, dpi):
        self.dpi = dpi
        scale = dpi / 96
        self.root.tk.call("tk", "scaling", dpi / 72)
        # Sun Valley's named fonts have negative (pixel) sizes; scale those too.
        for name, size in self.fonts.items():
            pixels = abs(size) * scale if size < 0 else size * dpi / 72
            font.nametofont(name, self.root).configure(size=-max(1, round(pixels)))
        for widget, options, packing in self.spacing:
            for key, values in options.items():
                widget.configure(**{key: tuple(round(x * scale) for x in values)})
            if packing:
                widget.pack_configure(**{key: tuple(round(x * scale) for x in values) for key, values in packing.items()})
            if isinstance(widget, FlowRow):
                widget.gap, widget.bottom = round(12 * scale), round(16 * scale)
                self.root.after_idle(widget.reflow)
            elif isinstance(widget, CenteredColumn):
                self.root.after_idle(widget.relayout)
            elif isinstance(widget, AdaptiveButton) and not isinstance(widget.master, FlowRow):
                self.root.after_idle(lambda button=widget: button.fit(button.master.winfo_width()))
        style = ttk.Style(self.root)
        for name in ("TButton", "Accent.TButton"):
            style.configure(name, padding=tuple(round(x * scale) for x in (14, 8, 14, 8)))
        style.configure("TEntry", padding=tuple(round(x * scale) for x in (8, 6, 6, 6)))
        style.configure("TNotebook.Tab", padding=(round(16 * scale), round(10 * scale)))
        style.configure("Switch.TCheckbutton", padding=(round(8 * scale), round(8 * scale)))
        style.configure("Toggle.TButton", padding=tuple(round(x * scale) for x in (16, 6, 16, 6)))
        for listener in self.listeners:
            listener(scale)

    def poll(self, initial=False):
        if not self.root.winfo_exists():
            return
        if self.root.state() == "iconic":
            self.timer = self.root.after(250, self.poll)
            return
        if self.user32:
            hwnd = self.user32.GetAncestor(self.root.winfo_id(), 2)
            dpi = self.user32.GetDpiForWindow(hwnd) or 96
            class MonitorInfo(ctypes.Structure):
                _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                            ("work", wintypes.RECT), ("flags", wintypes.DWORD)]
            info = MonitorInfo()
            info.size = ctypes.sizeof(info)
            monitor = self.user32.MonitorFromWindow(hwnd, 2)
            if self.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                area = (info.work.left, info.work.top, info.work.right, info.work.bottom)
                if initial or dpi != self.dpi or area != self.monitor:
                    rect = wintypes.RECT()
                    self.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    width = round(self.logical_size[0] * dpi / 96)
                    height = round(self.logical_size[1] * dpi / 96)
                    self.apply_scale(dpi)
                    width, height, x, y = fit_window(width, height, rect.left, rect.top, area)
                    self.root.minsize(min(round(460 * dpi / 96), width - 20), min(round(360 * dpi / 96), height - 50))
                    if self.root.state() != "zoomed":
                        self.user32.SetWindowPos(hwnd, None, x, y, width, height, 0x0014)
                    self.monitor = area
                else:
                    rect = wintypes.RECT()
                    self.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    if self.root.state() == "normal":
                        self.logical_size = ((rect.right - rect.left) * 96 / dpi,
                                             (rect.bottom - rect.top) * 96 / dpi)
        elif initial:
            self.apply_scale(round(self.root.winfo_fpixels("1i")))
        self.timer = self.root.after(250, self.poll)

    def close(self):
        if self.timer:
            self.root.after_cancel(self.timer)
            self.timer = None


def dark_title_bar(root):
    if os.name == "nt":
        try:
            user32 = ctypes.WinDLL("user32")
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetAncestor.restype = wintypes.HWND
            hwnd = user32.GetAncestor(root.winfo_id(), 2)
            dwm = ctypes.WinDLL("dwmapi")
            dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
            enabled = ctypes.c_int(1)
            for attribute in (20, 19):
                if dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)) == 0:
                    break
            # Explicit colors also keep the caption dark when Windows uses light mode.
            for attribute, color in ((35, 0x001C1C1C), (36, 0x00FAFAFA)):
                value = wintypes.DWORD(color)
                dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
        except (OSError, AttributeError):
            pass
