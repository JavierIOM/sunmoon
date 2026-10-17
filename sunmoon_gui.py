#!/usr/bin/env python3
"""SunMoon: the window. Points the antenna at the sun or moon through rotctl.exe
so you can check the rotator calibration, then tells you the offset.

The sums and the rotctl driver live in sunmoon.py. This file is only the face,
plus a worker thread that owns the rotator so a slow controller never freezes it.
"""

import ctypes
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from math import acos, cos, pi, sin
from tkinter import filedialog, ttk

import sunmoon as sm

APP = "SunMoon"

THEMES = {
    "dark": {
        "bg": "#121417", "card": "#1a1d21", "raised": "#23272c", "hover": "#2d3238",
        "border": "#2b3036", "text": "#e9e7e4", "muted": "#9aa1a9", "faint": "#636a72",
        "accent": "#F56400", "accent_hover": "#ff7b22", "accent_text": "#16181b",
        "good": "#5cbf76", "warn": "#e6a53c", "bad": "#ef6a63",
        "sun": "#ffb03a", "moon_lit": "#ece6d6", "moon_dark": "#30353c", "moon_rim": "#3d434b",
    },
    "light": {
        "bg": "#FAF7F4", "card": "#FFFFFF", "raised": "#F3EFEA", "hover": "#E8E2DB",
        "border": "#D9D9D9", "text": "#222222", "muted": "#595959", "faint": "#8c8c8c",
        "accent": "#F56400", "accent_hover": "#d95700", "accent_text": "#ffffff",
        "good": "#2e7d32", "warn": "#9a5b00", "bad": "#c62828",
        "sun": "#f29a00", "moon_lit": "#f4efe3", "moon_dark": "#4a4f57", "moon_rim": "#bdb6aa",
    },
}

MODELS = [
    (603, "Yaesu GS-232B"),
    (601, "Yaesu GS-232A"),
    (611, "Yaesu GS-232B, azimuth only"),
    (902, "SPID Rot1Prog"),
    (901, "SPID Rot2Prog"),
    (903, "SPID MD-01/02"),
    (202, "Easycomm II"),
    (2, "rotctld over the network"),
    (1, "Simulator (hamlib dummy)"),
]
BAUDS = ["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200"]
STEPS = (0.5, 1.0, 2.0, 5.0)
PORT_KINDS = (("VCP", "FTDI"), ("Silabser", "CP210x"), ("CH34", "CH340"),
              ("Prolific", "Prolific"), ("USBSER", "USB serial"), ("Serial", "built-in"))


def resource(*parts):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def model_name(model):
    return next((name for num, name in MODELS if num == model), f"hamlib model {model}")


def describe(cfg):
    if cfg["model"] == 1:
        return "simulator, nothing moves"
    return f"{model_name(cfg['model'])} on {cfg.get('device') or 'no port'}"


def serial_ports():
    """[(COM8, FTDI), ...] from the registry, so no PowerShell round trip."""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
    except OSError:
        return []
    ports, i = [], 0
    while True:
        try:
            dev, port, _ = winreg.EnumValue(key, i)
        except OSError:
            break
        i += 1
        kind = next((label for pat, label in PORT_KINDS if pat.lower() in dev.lower()), "")
        ports.append((port, kind))
    return sorted(ports, key=lambda p: int(re.sub(r"\D", "", p[0]) or 0))


def explain(err, cfg):
    """rotctl's error, turned into what to actually do about it."""
    dev = cfg.get("device") or "the port"
    if "already open" in err or "Access denied" in err:
        return f"{dev} is in use by another program. Close OscarWatch, HRD Rotator or rotctld, then Connect."
    if "does not exist" in err:
        return f"There's no {dev} on this PC. Is the rotator's USB lead in? Pick the port in Settings."
    if "network_open" in err or "failed to connect" in err:
        return f"Nothing is listening at {dev}. Start rotctld, or pick the serial port in Settings."
    if "rotctl not found" in err:
        return "Can't find rotctl.exe. Point Settings at your hamlib bin folder."
    if "no answer" in err or "timed out" in err.lower():
        return f"The rotator didn't answer on {dev}. Is the controller on? Check model and baud in Settings."
    return err


# --- the rotator, on its own thread ----------------------------------------

class Tracker:
    """Owns the rotctl process. The window sends it commands and reads a snapshot."""

    def __init__(self):
        self.cmds = queue.Queue()
        self.events = queue.Queue()
        self.lock = threading.Lock()
        self.state = {"link": "off", "detail": "", "pos": None, "settled": False,
                      "target": None, "nudge_az": 0.0, "nudge_el": 0.0}
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def send(self, *cmd):
        self.cmds.put(cmd)

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def _say(self, text, kind="info"):
        self.events.put((kind, text))

    @staticmethod
    def _release(rot):
        try:
            rot.stop()
        except sm.RotatorError:
            pass
        rot.close()

    def _run(self):
        rot = cfg = station = None
        sent = prev = None
        sent_at = next_poll = 0.0
        force = False
        while True:
            try:
                cmd = self.cmds.get(timeout=0.1)
            except queue.Empty:
                cmd = None
            try:
                if cmd:
                    name, args = cmd[0], cmd[1:]
                    if name in ("connect", "disconnect", "quit"):
                        if rot:
                            self._release(rot)
                            rot = None
                            self._set(link="off", pos=None, settled=False, target=None)
                            if name == "disconnect":
                                self._say("Disconnected, port released")
                        if name == "quit":
                            return
                        if name == "connect":
                            cfg = dict(args[0])
                            station = sm.station_from_cfg(cfg)
                            self._set(link="connecting", detail="")
                            rot = sm.Rotator(sm.find_rotctl(cfg["rotctl"]), cfg["model"],
                                             cfg.get("device"), cfg.get("speed"))
                            self._set(link="on")
                            self._say(f"Connected: {describe(cfg)}", "good")
                            sent = prev = None
                            next_poll = 0.0
                    elif rot is None:
                        pass
                    elif name == "track":
                        self._set(target=args[0], nudge_az=0.0, nudge_el=0.0, settled=False)
                        sent = None
                        self._say(f"Pointing at the {args[0]}")
                    elif name == "stop":
                        was = self.snapshot()["target"]
                        self._set(target=None, settled=False)
                        rot.stop()
                        sent_at = time.monotonic()
                        next_poll = sent_at + 0.5
                        self._say(f"Stopped tracking the {was}" if was else "Stopped")
                    elif name == "nudge":
                        s = self.snapshot()
                        self._set(nudge_az=s["nudge_az"] + args[0],
                                  nudge_el=s["nudge_el"] + args[1], settled=False)
                        force = True
                    elif name == "clear":
                        self._set(nudge_az=0.0, nudge_el=0.0, settled=False)
                        force = True

                if rot is None:
                    continue

                s = self.snapshot()
                if s["target"]:
                    az, el = sm.az_el(s["target"], sm.utcnow(), station, cfg["refraction"])
                    if el < cfg["min_el"]:
                        self._set(target=None)
                        self._say(f"The {s['target']} has gone below {cfg['min_el']:g} deg, "
                                  "stopped tracking", "warn")
                    else:
                        cmd_az = sm.rev(az + s["nudge_az"])
                        cmd_el = 0.0 if cfg["az_only"] else min(max(el + s["nudge_el"], 0.0), 90.0)
                        if (sent is None or force
                                or abs(sm.angdiff(cmd_az, sent[0])) >= cfg["deadband"]
                                or abs(cmd_el - sent[1]) >= cfg["deadband"]):
                            force = False
                            rot.set_pos(cmd_az, cmd_el)
                            sent, sent_at = (cmd_az, cmd_el), time.monotonic()
                            next_poll = sent_at + 0.5   # let the controller's reply to the move land
                            self._set(settled=False)

                now = time.monotonic()
                if now >= next_poll:
                    pos = rot.get_pos()
                    next_poll = time.monotonic() + cfg["poll_s"]
                    settled = bool(prev and now - sent_at > cfg["poll_s"] * 1.5
                                   and abs(sm.angdiff(pos[0], prev[0])) < 0.15
                                   and abs(pos[1] - prev[1]) < 0.15)
                    prev = pos
                    self._set(pos=pos, settled=settled)
            except sm.RotatorError as e:
                if rot is None or rot.proc.poll() is not None:
                    rot = None
                    self._set(link="error", detail=str(e), pos=None, settled=False, target=None)
                self._say(explain(str(e), cfg or {}), "bad")
                next_poll = time.monotonic() + 2.0


# --- widgets ------------------------------------------------------------------

class FlatButton(tk.Label):
    """A label that behaves like a button, so it looks the same in both themes."""

    def __init__(self, parent, app, text, command, kind="normal", font=None, padx=14, pady=6):
        super().__init__(parent, text=text, font=font or app.f["button"], padx=padx, pady=pady,
                         highlightthickness=1, bd=0)
        self.app, self.command, self.kind = app, command, kind
        self.enabled, self.hover, self.selected = True, False, False
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<ButtonRelease-1>", self._click)
        self.paint()

    def _hover(self, on):
        self.hover = on
        self.paint()

    def _click(self, event):
        inside = 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        if self.enabled and inside and self.command:
            self.command()

    def set(self, text=None, enabled=None, kind=None, selected=None):
        changed = False
        for attr, val in (("enabled", enabled), ("kind", kind), ("selected", selected)):
            if val is not None and getattr(self, attr) != val:
                setattr(self, attr, val)
                changed = True
        if text is not None and self.cget("text") != text:
            self.config(text=text)
        if changed:
            self.paint()

    def paint(self):
        t = self.app.t
        if not self.enabled:
            bg, fg, edge = t["raised"], t["faint"], t["raised"]
        elif self.kind == "primary":
            bg = t["accent_hover"] if self.hover else t["accent"]
            fg, edge = t["accent_text"], bg
        elif self.selected:
            bg, fg, edge = t["raised"], t["accent"], t["accent"]
        else:
            bg = t["hover"] if self.hover else t["raised"]
            fg, edge = t["text"], t["border"]
        self.config(bg=bg, fg=fg, highlightbackground=edge, highlightcolor=edge,
                    cursor="hand2" if self.enabled else "arrow")


def draw_sun(cv, size, t, dim):
    cv.delete("all")
    c, r = size / 2, size * 0.22
    col = t["faint"] if dim else t["sun"]
    for k in range(8):
        a = k * pi / 4
        cv.create_line(c + cos(a) * r * 1.45, c + sin(a) * r * 1.45,
                       c + cos(a) * r * 2.0, c + sin(a) * r * 2.0,
                       fill=col, width=max(2, size // 22), capstyle="round")
    cv.create_oval(c - r, c - r, c + r, c + r, fill=col, outline=col)


def draw_moon(cv, size, lit, waxing, t, dim):
    """The real phase: lit limb plus an elliptical terminator, lit side on the
    right when waxing as seen from the northern hemisphere."""
    cv.delete("all")
    c, r = size / 2, size * 0.40
    cv.create_oval(c - r, c - r, c + r, c + r, fill=t["moon_dark"], outline=t["moon_rim"])
    if lit < 0.01:
        return
    cos_i = 2 * lit - 1
    side = 1 if waxing else -1
    n = 36
    pts = []
    for k in range(n + 1):                                   # limb, top to bottom
        th = pi * k / n
        pts += [c + side * r * sin(th), c - r * cos(th)]
    for k in range(n, -1, -1):                               # terminator, back up
        th = pi * k / n
        pts += [c - side * r * sin(th) * cos_i, c - r * cos(th)]
    fill = t["faint"] if dim else t["moon_lit"]
    cv.create_polygon(pts, fill=fill, outline=fill)


# --- the window ---------------------------------------------------------------

class App:
    def __init__(self, root):
        self.root = root
        self.notes = []
        try:
            self.cfg = sm.load_config()
        except (OSError, ValueError) as e:
            self.cfg = dict(sm.DEFAULTS)
            self.notes.append(("bad", f"sunmoon.json couldn't be read ({e}), using defaults"))
        self.cfg["rotctl"] = sm.find_rotctl(self.cfg["rotctl"])
        self.station = sm.station_from_cfg(self.cfg)
        self.tracker = Tracker()
        self.mark = None
        self.log = []
        self.cache = {}
        self.t = THEMES.get(self.cfg.get("theme"), THEMES["dark"])
        self.f = self._fonts()
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96)
        self.icon = int(52 * self.scale)

        root.title(f"{APP} {sm.__version__}")
        ico = resource("assets", "sunmoon.ico")
        if os.path.isfile(ico):
            try:
                root.iconbitmap(default=ico)
            except tk.TclError:
                pass
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Left>", lambda e: self.nudge(-1, 0))
        root.bind("<Right>", lambda e: self.nudge(1, 0))
        root.bind("<Up>", lambda e: self.nudge(0, 1))
        root.bind("<Down>", lambda e: self.nudge(0, -1))
        root.bind("<space>", lambda e: self.do_mark())
        root.bind("<Escape>", lambda e: self.do_stop())

        self.build()
        for kind, text in self.notes:
            self.add_log(kind, text)
        self.connect()
        self.tick()

    # fonts and theme

    def _fonts(self):
        fams = set(tkfont.families(self.root))
        mono = next((m for m in ("Cascadia Mono", "Consolas", "Courier New") if m in fams), "TkFixedFont")
        ui = "Segoe UI" if "Segoe UI" in fams else "TkDefaultFont"
        semi = "Segoe UI Semibold" if "Segoe UI Semibold" in fams else ui
        return {
            "body": (ui, 10), "small": (ui, 9), "button": (semi, 10), "head": (semi, 10),
            "label": (semi, 8), "title": (semi, 15), "big": (mono, 22), "num": (mono, 12),
            "mono": (mono, 9), "arrow": (ui, 12),
        }

    def style_ttk(self):
        t = self.t
        st = ttk.Style(self.root)
        st.theme_use("clam")
        common = dict(fieldbackground=t["raised"], foreground=t["text"], background=t["raised"],
                      bordercolor=t["border"], lightcolor=t["raised"], darkcolor=t["raised"],
                      insertcolor=t["text"], arrowcolor=t["muted"], selectbackground=t["accent"],
                      selectforeground=t["accent_text"], padding=4)
        st.configure("TEntry", **common)
        st.configure("TCombobox", **common)
        st.map("TEntry", bordercolor=[("focus", t["accent"])])
        st.map("TCombobox", bordercolor=[("focus", t["accent"])],
               fieldbackground=[("readonly", t["raised"])], foreground=[("readonly", t["text"])],
               background=[("active", t["hover"])])
        st.configure("TCheckbutton", background=t["card"], foreground=t["text"],
                     indicatorbackground=t["raised"], indicatorforeground=t["accent"],
                     upperbordercolor=t["border"], lowerbordercolor=t["border"], font=self.f["body"])
        st.map("TCheckbutton", background=[("active", t["card"])],
               indicatorbackground=[("pressed", t["hover"]), ("active", t["hover"])])
        self.root.option_add("*TCombobox*Listbox.background", t["card"])
        self.root.option_add("*TCombobox*Listbox.foreground", t["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", t["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", t["accent_text"])
        self.root.option_add("*TCombobox*Listbox.font", self.f["body"])

    def dark_titlebar(self, win):
        """Windows 10/11 immersive dark title bar, so the frame matches the theme."""
        try:
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
            val = ctypes.c_int(1 if self.cfg.get("theme", "dark") == "dark" else 0)
            for attr in (20, 19):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), 4)
        except (AttributeError, OSError):
            pass

    def toggle_theme(self):
        self.cfg["theme"] = "light" if self.cfg.get("theme", "dark") == "dark" else "dark"
        self.t = THEMES[self.cfg["theme"]]
        self.save()
        self.build()
        self.dark_titlebar(self.root)
        # nudge the frame so Windows repaints the title bar now, not on next focus
        self.root.withdraw()
        self.root.deiconify()

    # layout

    def card(self, parent, **grid):
        f = tk.Frame(parent, bg=self.t["card"], highlightthickness=1,
                     highlightbackground=self.t["border"], padx=16, pady=14)
        f.grid(**grid)
        return f

    def lab(self, parent, text="", font="body", fg="text", bg="card", **kw):
        return tk.Label(parent, text=text, font=self.f[font], fg=self.t[fg], bg=self.t[bg], **kw)

    def build(self):
        t = self.t
        for child in self.root.winfo_children():
            if not isinstance(child, tk.Toplevel):
                child.destroy()
        self.style_ttk()
        self.root.configure(bg=t["bg"])
        self.w = w = {}
        self.cache.pop("moon_icon", None)
        self.cache.pop("sun_icon", None)

        outer = tk.Frame(self.root, bg=t["bg"], padx=18, pady=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1, uniform="col")
        outer.columnconfigure(1, weight=1, uniform="col")

        # header
        head = tk.Frame(outer, bg=t["bg"])
        head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        head.columnconfigure(1, weight=1)
        title = tk.Frame(head, bg=t["bg"])
        title.grid(row=0, column=0, sticky="w")
        self.lab(title, APP, "title", bg="bg").pack(side="left")
        self.lab(title, f"v{sm.__version__}", "small", "faint", "bg").pack(side="left", padx=(8, 0), pady=(6, 0))
        w["station"] = self.lab(head, "", "small", "muted", "bg")
        w["station"].grid(row=1, column=0, sticky="w")
        right = tk.Frame(head, bg=t["bg"])
        right.grid(row=0, column=2, rowspan=2, sticky="e")
        w["clock"] = self.lab(right, "", "num", "text", "bg")
        w["clock"].pack(side="left", padx=(0, 14))
        FlatButton(right, self, "Settings", self.open_settings).pack(side="left")
        FlatButton(right, self, "Light" if self.cfg.get("theme", "dark") == "dark" else "Dark",
                   self.toggle_theme).pack(side="left", padx=(8, 0))

        # sun and moon
        for col, body in enumerate(("sun", "moon")):
            c = self.card(outer, row=1, column=col, sticky="nsew", padx=(0, 6) if col == 0 else (6, 0))
            c.columnconfigure(1, weight=1)
            cv = tk.Canvas(c, width=self.icon, height=self.icon, bg=t["card"], highlightthickness=0)
            cv.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 12))
            self.lab(c, body.upper(), "label", "muted").grid(row=0, column=1, sticky="w")
            nums = tk.Frame(c, bg=t["card"])
            nums.grid(row=1, column=1, sticky="w")
            self.lab(nums, "AZ", "label", "faint").pack(side="left", pady=(8, 0))
            az = self.lab(nums, "", "big", width=7, anchor="w")
            az.pack(side="left", padx=(4, 8))
            self.lab(nums, "EL", "label", "faint").pack(side="left", pady=(8, 0))
            el = self.lab(nums, "", "big", width=6, anchor="w")
            el.pack(side="left", padx=(4, 0))
            note = self.lab(c, "", "small", "muted", anchor="w", justify="left")
            note.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
            btn = FlatButton(c, self, f"Point at the {body}", lambda b=body: self.point(b), "primary")
            btn.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(12, 0))
            w[body] = {"icon": cv, "az": az, "el": el, "note": note, "btn": btn}

        # rotator
        rc = self.card(outer, row=2, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        rc.columnconfigure(1, weight=1)
        self.lab(rc, "ROTATOR", "label", "muted").grid(row=0, column=0, sticky="w")
        link = tk.Frame(rc, bg=t["card"])
        link.grid(row=0, column=1, sticky="w", padx=(14, 0))
        d = int(10 * self.scale)
        w["dot"] = tk.Canvas(link, width=d, height=d, bg=t["card"], highlightthickness=0)
        w["dot"].pack(side="left", padx=(0, 6))
        w["link"] = self.lab(link, "", "small", "muted", anchor="w", justify="left", wraplength=520)
        w["link"].pack(side="left")
        w["connect"] = FlatButton(rc, self, "Connect", self.toggle_connect)
        w["connect"].grid(row=0, column=2, sticky="e")

        read = tk.Frame(rc, bg=t["card"])
        read.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        read.columnconfigure(4, weight=1)
        self.lab(read, "AZ", "label", "faint").grid(row=0, column=0, sticky="sw", pady=(0, 6))
        w["raz"] = self.lab(read, "", "big", width=7, anchor="w")
        w["raz"].grid(row=0, column=1, sticky="w", padx=(4, 8))
        self.lab(read, "EL", "label", "faint").grid(row=0, column=2, sticky="sw", pady=(0, 6))
        w["rel"] = self.lab(read, "", "big", width=6, anchor="w")
        w["rel"].grid(row=0, column=3, sticky="w", padx=(4, 18))
        offf = tk.Frame(read, bg=t["card"])
        offf.grid(row=0, column=4, sticky="w")
        self.lab(offf, "OFFSET", "label", "faint").pack(anchor="w")
        w["off"] = self.lab(offf, "", "num")
        w["off"].pack(anchor="w")
        w["chip"] = self.lab(read, "", "label", "muted", padx=10, pady=4)
        w["chip"].grid(row=0, column=5, sticky="e")
        w["tracking"] = self.lab(rc, "", "small", "muted", anchor="w")
        w["tracking"].grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        ctl = tk.Frame(rc, bg=t["card"])
        ctl.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        ctl.columnconfigure(1, weight=1)
        pad = tk.Frame(ctl, bg=t["card"])
        pad.grid(row=0, column=0, sticky="w")
        arrow = dict(font=self.f["arrow"], padx=12, pady=2)
        w["up"] = FlatButton(pad, self, "\u2191", lambda: self.nudge(0, 1), **arrow)
        w["left"] = FlatButton(pad, self, "\u2190", lambda: self.nudge(-1, 0), **arrow)
        w["stop"] = FlatButton(pad, self, "Stop", self.do_stop, padx=12, pady=5)
        w["right"] = FlatButton(pad, self, "\u2192", lambda: self.nudge(1, 0), **arrow)
        w["down"] = FlatButton(pad, self, "\u2193", lambda: self.nudge(0, -1), **arrow)
        w["up"].grid(row=0, column=1, pady=(0, 4), sticky="ew")
        w["left"].grid(row=1, column=0, padx=(0, 4), sticky="ns")
        w["stop"].grid(row=1, column=1, sticky="ew")
        w["right"].grid(row=1, column=2, padx=(4, 0), sticky="ns")
        w["down"].grid(row=2, column=1, pady=(4, 0), sticky="ew")

        side = tk.Frame(ctl, bg=t["card"])
        side.grid(row=0, column=1, sticky="nw", padx=(28, 0))
        self.lab(side, "NUDGE STEP", "label", "faint").grid(row=0, column=0, columnspan=4, sticky="w")
        w["steps"] = {}
        steps = tk.Frame(side, bg=t["card"])
        steps.grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 12))
        for s in STEPS:
            b = FlatButton(steps, self, f"{s:g}\u00b0", lambda s=s: self.set_step(s), padx=10, pady=4)
            b.pack(side="left", padx=(0, 6))
            w["steps"][s] = b
        acts = tk.Frame(side, bg=t["card"])
        acts.grid(row=2, column=0, columnspan=4, sticky="w")
        w["mark"] = FlatButton(acts, self, "Mark reading (Space)", self.do_mark, "primary")
        w["mark"].pack(side="left")
        w["clear"] = FlatButton(acts, self, "Clear nudge", lambda: self.tracker.send("clear"))
        w["clear"].pack(side="left", padx=(8, 0))
        self.lab(ctl, "Arrow keys nudge,\nEsc stops.", "small", "faint", justify="right").grid(
            row=0, column=2, sticky="se")

        # result
        res = self.card(outer, row=3, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        res.columnconfigure(0, weight=1)
        self.lab(res, "RESULT", "label", "muted").grid(row=0, column=0, sticky="w")
        w["res_main"] = self.lab(res, "", "num", anchor="w", justify="left")
        w["res_main"].grid(row=1, column=0, sticky="w", pady=(6, 0))
        w["res_sub"] = self.lab(res, "", "small", "muted", anchor="w", justify="left", wraplength=640)
        w["res_sub"].grid(row=2, column=0, sticky="w", pady=(4, 0))
        w["copy"] = FlatButton(res, self, "Copy flags", self.copy_flags)
        w["copy"].grid(row=1, column=1, rowspan=2, sticky="e")

        # log
        lg = tk.Frame(outer, bg=t["bg"])
        lg.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        outer.rowconfigure(4, weight=1)
        w["log"] = tk.Text(lg, height=5, bg=t["bg"], fg=t["muted"], font=self.f["mono"], bd=0,
                           highlightthickness=0, wrap="word", takefocus=0, cursor="arrow")
        w["log"].pack(fill="both", expand=True)
        for kind in ("good", "warn", "bad"):
            w["log"].tag_configure(kind, foreground=t[kind])
        w["log"].tag_configure("time", foreground=t["faint"])
        w["log"].configure(state="disabled")
        self.redraw_log()

        self.refresh()
        self.root.minsize(self.root.winfo_reqwidth(), self.root.winfo_reqheight())

    # actions

    def connect(self):
        self.tracker.send("connect", dict(self.cfg))

    def toggle_connect(self):
        if self.tracker.snapshot()["link"] in ("on", "connecting"):
            self.tracker.send("disconnect")
        else:
            self.connect()

    def point(self, body):
        s = self.tracker.snapshot()
        if s["target"] == body:
            self.do_stop()
        else:
            self.mark = None
            self.tracker.send("track", body)

    def do_stop(self):
        if self.tracker.snapshot()["link"] == "on":
            self.tracker.send("stop")

    def nudge(self, daz, del_):
        s = self.tracker.snapshot()
        if not s["target"]:
            return
        step = float(self.cfg["step"])
        self.tracker.send("nudge", daz * step, 0.0 if self.cfg["az_only"] else del_ * step)

    def set_step(self, s):
        self.cfg["step"] = s
        self.save()

    def do_mark(self):
        s = self.tracker.snapshot()
        if not s["target"]:
            self.add_log("warn", "Nothing to mark. Point at the sun or moon first.")
            return
        if not (s["settled"] and s["pos"]):
            self.add_log("warn", "Still moving. Mark once it says settled.")
            return
        body = s["target"]
        az, el = sm.az_el(body, sm.utcnow(), self.station, self.cfg["refraction"])
        raz, rel = s["pos"]
        self.mark = {"body": body, "az": az, "el": el, "raz": raz, "rel": rel,
                     "off_az": sm.angdiff(raz, az), "off_el": rel - el,
                     "at": datetime.now().strftime("%H:%M:%S")}
        self.add_log("good", f"Marked: rotator {raz:.1f}/{rel:.1f}, {body} {az:.1f}/{el:.1f}, "
                             f"offset {self.mark['off_az']:+.1f}/{self.mark['off_el']:+.1f}")

    def flags(self):
        m = self.mark
        if not m:
            return ""
        out = f"-o {m['off_az']:+.1f}"
        if not self.cfg["az_only"]:
            out += f" -O {m['off_el']:+.1f}"
        return out

    def copy_flags(self):
        if self.mark:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.flags())
            self.add_log("info", f"Copied {self.flags()}")

    def save(self):
        try:
            sm.save_config(self.cfg)
        except OSError as e:
            self.add_log("bad", f"Couldn't save settings: {e}")

    def apply_settings(self, new):
        rotator_keys = ("rotctl", "model", "device", "speed")
        reconnect = (any(new[k] != self.cfg.get(k) for k in rotator_keys)
                     or self.tracker.snapshot()["link"] != "on")
        self.cfg.update(new)
        self.station = sm.station_from_cfg(self.cfg)
        self.cache.clear()
        self.save()
        self.mark = None
        # the worker keeps its own copy of the settings, so hand it the new ones
        self.add_log("info", "Settings saved, reconnecting" if reconnect else "Settings saved")
        self.connect()

    def open_settings(self):
        Settings(self)

    def close(self):
        self.tracker.send("quit")
        self.tracker.thread.join(timeout=4)
        self.root.destroy()

    # log

    def add_log(self, kind, text):
        self.log.append((datetime.now().strftime("%H:%M:%S"), kind, text))
        del self.log[:-200]
        self.redraw_log()

    def redraw_log(self):
        lg = self.w.get("log") if hasattr(self, "w") else None
        if not lg:
            return
        lg.configure(state="normal")
        lg.delete("1.0", "end")
        for stamp, kind, text in self.log:
            lg.insert("end", stamp + "  ", "time")
            lg.insert("end", text + "\n", kind if kind != "info" else ())
        lg.configure(state="disabled")
        lg.see("end")

    # the 5 Hz refresh

    def tick(self):
        try:
            self.refresh()
        finally:
            self.root.after(200, self.tick)

    def cached(self, key, seconds, fn):
        now = time.monotonic()
        hit = self.cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        val = fn()
        self.cache[key] = (now + seconds, val)
        return val

    def refresh(self):
        t, w, cfg = self.t, self.w, self.cfg
        while True:
            try:
                kind, text = self.tracker.events.get_nowait()
            except queue.Empty:
                break
            self.add_log(kind, text)

        now = sm.utcnow()
        w["clock"].config(text=f"{now.astimezone():%H:%M:%S} local   {now:%H:%M} UTC")
        w["station"].config(text=f"{self.station.label}, {self.station.alt_m:g} m")
        s = self.tracker.snapshot()
        on = s["link"] == "on"

        for body in ("sun", "moon"):
            b = w[body]
            az, el = sm.az_el(body, now, self.station, cfg["refraction"])
            up = el >= cfg["min_el"]
            fg = t["text"] if up else t["faint"]
            b["az"].config(text=f"{az:.1f}\u00b0", fg=fg)
            b["el"].config(text=f"{el:.1f}\u00b0", fg=fg)
            note = self.cached(f"note_{body}", 30, lambda body=body: sm.up_down_note(
                body, self.station, cfg, cfg["refraction"]))
            note = note[0].upper() + note[1:]
            if body == "moon":
                lit, waxing = self.cached("phase", 60, lambda: sm.moon_phase(now))
                note += f"\n{lit * 100:.0f}% lit, {sm.phase_name(lit, waxing)}"
                key = (round(lit, 2), waxing, up, cfg.get("theme"))
                if self.cache.get("moon_icon") != key:
                    draw_moon(b["icon"], self.icon, lit, waxing, t, not up)
                    self.cache["moon_icon"] = key
            else:
                note += "\n "
                key = (up, cfg.get("theme"))
                if self.cache.get("sun_icon") != key:
                    draw_sun(b["icon"], self.icon, t, not up)
                    self.cache["sun_icon"] = key
            b["note"].config(text=note)
            if s["target"] == body:
                b["btn"].set(text="Stop tracking", enabled=True, kind="normal")
            elif not up:
                b["btn"].set(text="Below the horizon" if cfg["min_el"] == 0
                             else f"Below {cfg['min_el']:g}\u00b0", enabled=False, kind="primary")
            else:
                b["btn"].set(text=f"Point at the {body}", enabled=on, kind="primary")

        # link line
        dot = {"on": t["good"], "connecting": t["warn"], "error": t["bad"]}.get(s["link"], t["faint"])
        d = int(10 * self.scale)
        w["dot"].delete("all")
        w["dot"].create_oval(1, 1, d - 1, d - 1, fill=dot, outline=dot)
        if s["link"] == "on":
            text = f"Connected, {describe(cfg)}"
        elif s["link"] == "connecting":
            text = f"Connecting to {describe(cfg)}..."
        elif s["link"] == "error":
            text = explain(s["detail"], cfg)
        else:
            text = "Not connected. The port is free for other programs."
        w["link"].config(text=text, fg=t["bad"] if s["link"] == "error" else t["muted"])
        w["connect"].set(text="Disconnect" if s["link"] in ("on", "connecting") else "Connect")

        # readings
        pos = s["pos"] if on else None
        w["raz"].config(text=f"{pos[0]:.1f}\u00b0" if pos else "---", fg=t["text"] if pos else t["faint"])
        w["rel"].config(text=f"{pos[1]:.1f}\u00b0" if pos else "---", fg=t["text"] if pos else t["faint"])
        tracking = s["target"] if on else None
        if tracking and pos:
            baz, bel = sm.az_el(tracking, now, self.station, cfg["refraction"])
            off = f"{sm.angdiff(pos[0], baz):+.1f}\u00b0"
            if not cfg["az_only"]:
                off += f" / {pos[1] - bel:+.1f}\u00b0"
            # mid-slew the offset is only "how far it has to go", so keep it quiet until settled
            w["off"].config(text=off, fg=t["text"] if s["settled"] else t["faint"])
        else:
            w["off"].config(text="---", fg=t["faint"])
        if not on:
            chip, chip_fg = "", t["muted"]
        elif tracking:
            chip, chip_fg = ("SETTLED", t["good"]) if s["settled"] else ("MOVING", t["warn"])
        else:
            chip, chip_fg = "IDLE", t["muted"]
        w["chip"].config(text=chip, fg=chip_fg, bg=t["raised"] if chip else t["card"])

        if tracking:
            line = f"Tracking the {tracking}."
            if s["nudge_az"] or s["nudge_el"]:
                line += f"  Nudged {s['nudge_az']:+.1f}\u00b0"
                if not cfg["az_only"]:
                    line += f" / {s['nudge_el']:+.1f}\u00b0"
                line += "."
            line += "  Offset is rotator reading minus where it really is."
        elif on:
            line = "Not tracking. Pick the sun or moon above."
        else:
            line = ""
        w["tracking"].config(text=line)

        for k in ("left", "right"):
            w[k].set(enabled=bool(tracking))
        for k in ("up", "down"):
            w[k].set(enabled=bool(tracking) and not cfg["az_only"])
        w["stop"].set(enabled=on)
        w["clear"].set(enabled=bool(tracking) and bool(s["nudge_az"] or s["nudge_el"]))
        w["mark"].set(enabled=bool(tracking) and s["settled"])
        for st, b in w["steps"].items():
            b.set(selected=abs(st - float(cfg["step"])) < 1e-6)

        # result
        m = self.mark
        if m:
            main = f"Offset  az {m['off_az']:+.1f}\u00b0"
            if not cfg["az_only"]:
                main += f"   el {m['off_el']:+.1f}\u00b0"
            w["res_main"].config(text=main, fg=t["accent"])
            w["res_sub"].config(text=(
                f"Marked at {m['at']}: rotator read {m['raz']:.1f}/{m['rel']:.1f} with the {m['body']} at "
                f"{m['az']:.1f}/{m['el']:.1f}. If the antenna was dead on it, give rotctl or rotctld "
                f"{self.flags()} to correct it."))
            w["copy"].set(enabled=True)
        else:
            w["res_main"].config(text="No reading marked yet", fg=t["faint"])
            w["res_sub"].config(text=(
                "Point at the sun, then check it's really on it: the boom's shadow shrinks to a dot, "
                "or sun noise peaks on the receiver. Nudge until it is, wait for SETTLED, then Mark."))
            w["copy"].set(enabled=False)


# --- settings -----------------------------------------------------------------

class Settings(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        t, cfg = app.t, app.cfg
        self.withdraw()                       # build hidden, theme the title bar, then show
        self.title("Settings")
        self.configure(bg=t["bg"])
        self.resizable(False, False)
        self.transient(app.root)

        body = tk.Frame(self, bg=t["card"], highlightthickness=1, highlightbackground=t["border"],
                        padx=18, pady=16)
        body.pack(fill="both", expand=True, padx=14, pady=14)
        body.columnconfigure(1, weight=1)
        self.row = 0
        self.v = {}

        def section(text):
            pad = (0, 6) if self.row == 0 else (16, 6)
            app.lab(body, text, "label", "muted").grid(row=self.row, column=0, columnspan=3, sticky="w", pady=pad)
            self.row += 1

        def field(label, widget, hint=None):
            app.lab(body, label).grid(row=self.row, column=0, sticky="w", padx=(0, 14), pady=3)
            widget.grid(row=self.row, column=1, sticky="ew", pady=3)
            self.row += 1
            if hint:
                app.lab(body, hint, "small", "faint", wraplength=380, justify="left").grid(
                    row=self.row, column=1, sticky="w", pady=(0, 4))
                self.row += 1

        def entry(key, value, width=30, parent=body):
            var = tk.StringVar(value="" if value is None else str(value))
            self.v[key] = var
            return ttk.Entry(parent, textvariable=var, width=width, font=app.f["body"])

        section("ROTATOR")
        models = [f"{num}  {name}" for num, name in MODELS]
        cur = next((m for m in models if m.split()[0] == str(cfg["model"])), str(cfg["model"]))
        self.v["model"] = tk.StringVar(value=cur)
        field("Model", ttk.Combobox(body, textvariable=self.v["model"], values=models, width=30,
                                    font=app.f["body"]),
              "Type any hamlib model number if yours isn't listed (rotctl -l).")

        ports = [f"{p}  ({k})" if k else p for p, k in serial_ports()]
        dev = cfg.get("device") or ""
        cur = next((p for p in ports if p.split()[0] == dev), dev)
        self.v["device"] = tk.StringVar(value=cur)
        field("Port", ttk.Combobox(body, textvariable=self.v["device"], values=ports, width=30,
                                   font=app.f["body"]),
              "For rotctld pick model 2 and type host:port, e.g. 127.0.0.1:4533.")

        self.v["speed"] = tk.StringVar(value=str(cfg.get("speed") or 9600))
        field("Baud", ttk.Combobox(body, textvariable=self.v["speed"], values=BAUDS, state="readonly",
                                   width=30, font=app.f["body"]))

        self.v["az_only"] = tk.BooleanVar(value=bool(cfg["az_only"]))
        field("", ttk.Checkbutton(body, text="Azimuth-only rotator", variable=self.v["az_only"]))

        rf = tk.Frame(body, bg=t["card"])
        rf.columnconfigure(0, weight=1)
        entry("rotctl", cfg["rotctl"], 40, rf).grid(row=0, column=0, sticky="ew")
        FlatButton(rf, app, "Browse", self.browse, padx=10, pady=3).grid(row=0, column=1, padx=(6, 0))
        field("rotctl.exe", rf)

        section("STATION")
        field("Grid locator", entry("grid", cfg["grid"]))
        field("Latitude", entry("lat", cfg.get("lat")))
        field("Longitude", entry("lon", cfg.get("lon")),
              "Decimal degrees, west negative. Leave both blank to use the middle of the grid square.")
        field("Height (m)", entry("alt_m", cfg.get("alt_m", 0)))

        section("POINTING")
        field("Lowest elevation", entry("min_el", cfg["min_el"]),
              "Degrees. It won't point below this, so set it above your trees and roofs.")
        field("Deadband", entry("deadband", cfg["deadband"]),
              "Degrees. Only re-commands once the target has moved this far, to spare the motors.")
        self.v["refraction"] = tk.BooleanVar(value=bool(cfg.get("refraction", True)))
        field("", ttk.Checkbutton(body, text="Allow for atmospheric refraction", variable=self.v["refraction"]))

        self.err = app.lab(body, "", "small", "bad", wraplength=400, justify="left")
        self.err.grid(row=self.row, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.row += 1

        btns = tk.Frame(self, bg=t["bg"])
        btns.pack(fill="x", padx=14, pady=(0, 14))
        FlatButton(btns, app, "Save", self.save, "primary").pack(side="right")
        FlatButton(btns, app, "Cancel", self.destroy).pack(side="right", padx=(0, 8))
        self.bind("<Return>", lambda e: self.save())
        self.bind("<Escape>", lambda e: self.destroy())

        self.update_idletasks()
        app.dark_titlebar(self)
        x = app.root.winfo_rootx() + (app.root.winfo_width() - self.winfo_reqwidth()) // 2
        y = app.root.winfo_rooty() + 30
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.deiconify()
        self.grab_set()
        self.focus_set()

    def browse(self):
        path = filedialog.askopenfilename(parent=self, title="Find rotctl.exe",
                                          filetypes=[("rotctl", "rotctl.exe"), ("Programs", "*.exe")],
                                          initialdir="C:/Radio")
        if path:
            self.v["rotctl"].set(path)

    def save(self):
        try:
            new = self.collect()
        except ValueError as e:
            self.err.config(text=str(e))
            return
        self.destroy()
        self.app.apply_settings(new)

    def collect(self):
        v = {k: var.get() for k, var in self.v.items()}

        def num(key, label, lo, hi):
            try:
                x = float(str(v[key]).strip())
            except ValueError:
                raise ValueError(f"{label} needs to be a number.")
            if not lo <= x <= hi:
                raise ValueError(f"{label} should be between {lo:g} and {hi:g}.")
            return x

        m = re.match(r"\s*(\d+)", v["model"])
        if not m:
            raise ValueError("Model needs a hamlib model number, e.g. 603.")
        model = int(m[1])
        device = v["device"].split()[0] if v["device"].strip() else ""
        if model != 1 and not device:
            raise ValueError("Pick the rotator's port.")
        grid = v["grid"].strip().upper()
        lat_s, lon_s = v["lat"].strip(), v["lon"].strip()
        if bool(lat_s) != bool(lon_s):
            raise ValueError("Give both latitude and longitude, or leave both blank.")
        lat = num("lat", "Latitude", -90, 90) if lat_s else None
        lon = num("lon", "Longitude", -180, 180) if lon_s else None
        if lat is None and not re.fullmatch(r"[A-R]{2}\d{2}([A-X]{2})?", grid):
            raise ValueError("That grid locator doesn't look right. Four or six characters, e.g. IO74RE.")
        rotctl = v["rotctl"].strip().replace("\\", "/")
        if not os.path.isfile(rotctl):
            raise ValueError("Can't find rotctl.exe at that path.")
        return {
            "model": model, "device": device or None, "speed": int(v["speed"]),
            "az_only": bool(v["az_only"]), "rotctl": rotctl, "grid": grid or self.app.cfg["grid"],
            "lat": lat, "lon": lon, "alt_m": num("alt_m", "Height", -500, 9000),
            "min_el": num("min_el", "Lowest elevation", -2, 60),
            "deadband": num("deadband", "Deadband", 0.1, 10), "refraction": bool(v["refraction"]),
        }


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # crisp text on scaled displays
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    root.withdraw()
    app = App(root)
    root.update_idletasks()
    app.dark_titlebar(root)
    root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    main()
