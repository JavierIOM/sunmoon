#!/usr/bin/env python3
"""SunMoon: the window. Points the antenna at the sun or moon through hamlib's
rotctl so you can check the rotator calibration, then tells you the offset.

The sums and the rotctl driver live in sunmoon.py. This file is only the face,
plus a worker thread that owns the rotator so a slow controller never freezes it.
"""

import ctypes
import glob
import os
import queue
import re
import sys
import threading
import time
import webbrowser
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
# the same, from the names in /dev/serial/by-id
PORT_KINDS_LINUX = (("FTDI", "FTDI"), ("Silicon_Labs", "CP210x"), ("CP210", "CP210x"),
                    ("1a86", "CH340"), ("QinHeng", "CH340"), ("CH34", "CH340"),
                    ("Prolific", "Prolific"), ("Arduino", "Arduino"))
# what else might be holding the rotator's port
OTHER_PROGRAMS = ("PstRotator, HRD Rotator, OscarWatch, rotctld" if sm.WINDOWS
                  else "rotctld, MacDoppler, Gpredict and the like" if sm.MAC
                  else "rotctld, Gpredict and the like")
# macOS /dev/cu.* names, by driver: Apple's own, Silicon Labs', WCH's, USB CDC
PORT_KINDS_MAC = (("SLAB", "CP210x"), ("wchusbserial", "CH340"), ("usbserial", "USB serial"),
                  ("usbmodem", "USB modem"))


def resource(*parts):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def model_name(model):
    return next((name for num, name in MODELS if num == model), f"hamlib model {model}")


def short_port(dev):
    """ttyUSB0 for a /dev/serial/by-id link, rather than 60 characters of FTDI serial number."""
    if dev and dev.startswith("/dev/serial/by-id/") and os.path.exists(dev):
        return os.path.basename(os.path.realpath(dev))
    return dev


def describe(cfg):
    if cfg["model"] == 1:
        return "simulator, nothing moves"
    return f"{model_name(cfg['model'])} on {short_port(cfg.get('device')) or 'no port'}"


def serial_ports():
    """[(device, label), ...] for the Port list.

    Windows: COM ports from the registry, so no PowerShell round trip.
    Linux: /dev/serial/by-id, because ttyUSB0 and ttyUSB1 can swap at the next boot
    when the radio is on USB too, and the by-id names don't, and they say which chip.
    macOS: /dev/cu.*, the call-out side, which opens without waiting for carrier detect
    the way /dev/tty.* does. The names carry the adapter's serial number, so they keep."""
    if sm.MAC:
        ports = []
        for dev in glob.glob("/dev/cu.*"):
            name = dev[len("/dev/cu."):]
            if "Bluetooth" in name or "debug" in name.lower():
                continue
            kind = next((label for pat, label in PORT_KINDS_MAC if pat.lower() in name.lower()), "")
            ports.append((dev, f"{name}  ({kind})" if kind else name))
        return sorted(ports, key=lambda p: p[1])
    if not sm.WINDOWS:
        ports = []
        for link in glob.glob("/dev/serial/by-id/*"):
            name = os.path.basename(link)
            kind = next((label for pat, label in PORT_KINDS_LINUX if pat.lower() in name.lower()),
                        "USB serial")
            ports.append((link, f"{short_port(link)}  ({kind})"))
        return sorted(ports, key=lambda p: p[1])
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
        ports.append((port, f"{port}  ({kind})" if kind else port))
    return sorted(ports, key=lambda p: int(re.sub(r"\D", "", p[0]) or 0))


def explain(err, cfg):
    """rotctl's error, turned into what to actually do about it."""
    dev = short_port(cfg.get("device")) or "the port"
    if "already open" in err or "Access denied" in err or "resource busy" in err:
        return (f"{dev} is in use by another program. Close whatever else drives the rotator "
                f"({OTHER_PROGRAMS}), then Connect.")
    if "Permission denied" in err and sm.MAC:
        return f"macOS won't let SunMoon open {dev}. Unplug the rotator's USB lead, plug it back in, then Connect."
    if "Permission denied" in err:
        return (f"Linux won't let you open {dev}. Add yourself to the dialout group "
                "(sudo usermod -aG dialout $USER), log out and back in, then Connect.")
    if "does not exist" in err or "No such file" in err:
        return (f"There's no {dev} on this computer. Is the rotator's USB lead in? "
                "Pick the port in Settings.")
    if "network_open" in err or "failed to connect" in err:
        return f"Nothing is listening at {dev}. Start rotctld, or pick the serial port in Settings."
    if "rotctl not found" in err:
        if sm.FROZEN and sm.MAC:
            return "Can't find rotctl, it should be inside SunMoon.app. Download SunMoon again."
        if sm.FROZEN:
            return "Can't find rotctl.exe, it should be in SunMoon's hamlib-w64 folder. Unzip SunMoon again."
        if sm.WINDOWS:
            return "Can't find rotctl.exe. Install hamlib, then point Settings at its bin folder."
        return f"Can't find rotctl. Install hamlib ({sm.HAMLIB_INSTALL}), then Connect."
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
        self.first_run = not sm.config_exists()
        try:
            self.cfg = sm.load_config()
        except (OSError, ValueError) as e:
            self.cfg = dict(sm.DEFAULTS)
            self.notes.append(("bad", f"sunmoon.json couldn't be read ({e}), using defaults"))
        self.cfg["rotctl"] = sm.find_rotctl(self.cfg["rotctl"])
        try:
            self.station = sm.station_from_cfg(self.cfg)
        except ValueError:
            self.station = None                # first run, or a config with no location in it
        self.tracker = Tracker()
        self.mark = None
        self.log = []
        self.cache = {}
        self.t = THEMES.get(self.cfg.get("theme"), THEMES["dark"])
        self.f = self._fonts()
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96)
        self.icon = int(52 * self.scale)

        root.title(f"{APP} {sm.__version__} by 2D0PEY")
        try:
            if sm.WINDOWS:
                root.iconbitmap(default=resource("assets", "sunmoon.ico"))
            else:
                self.icon_img = tk.PhotoImage(file=resource("assets", "sunmoon.png"))
                root.iconphoto(True, self.icon_img)
        except tk.TclError:
            pass
        root.protocol("WM_DELETE_WINDOW", self.close)
        if sm.MAC:
            root.createcommand("::tk::mac::Quit", self.close)    # Cmd+Q, so rotctl gets stopped
        root.bind("<Left>", lambda e: self.nudge(-1, 0))
        root.bind("<Right>", lambda e: self.nudge(1, 0))
        root.bind("<Up>", lambda e: self.nudge(0, 1))
        root.bind("<Down>", lambda e: self.nudge(0, -1))
        root.bind("<space>", lambda e: self.do_mark())
        root.bind("<Escape>", lambda e: self.do_stop())

        self.build()
        for kind, text in self.notes:
            self.add_log(kind, text)
        if self.first_run or self.station is None:
            self.add_log("info", "Welcome. Tell SunMoon where you are and what drives your rotator.")
            root.after(300, lambda: Settings(self, first_run=True))
        else:
            self.connect()
        self.tick()

    # fonts and theme

    def _fonts(self):
        fams = set(tkfont.families(self.root))

        def pick(names, fallback):
            return next((n for n in names if n in fams), fallback)

        # Windows first, then what Ubuntu, GNOME, Raspberry Pi OS and the rest ship, then macOS
        mono = pick(("Cascadia Mono", "Consolas", "Ubuntu Mono", "DejaVu Sans Mono",
                     "Liberation Mono", "SF Mono", "Menlo", "Courier New"), "TkFixedFont")
        ui = pick(("Segoe UI", "Ubuntu", "Cantarell", "Noto Sans", "DejaVu Sans", "Helvetica Neue"),
                  "TkDefaultFont")

        def pt(size):
            # Tk on macOS draws a point as one pixel and ignores "tk scaling" for text,
            # where Windows and Linux use 96 to the inch, so 10 came out a size small
            # (the CI screenshots). A third bigger is the same size, and body text
            # lands on the 13 px the rest of macOS uses.
            return round(size * 4 / 3) if sm.MAC else size

        def semi(size):
            if "Segoe UI Semibold" in fams:
                return ("Segoe UI Semibold", pt(size))
            return (ui, pt(size), "bold")

        return {
            "body": (ui, pt(10)), "small": (ui, pt(9)), "button": semi(10), "head": semi(10),
            "label": semi(8), "title": semi(15), "big": (mono, pt(22)), "num": (mono, pt(12)),
            "mono": (mono, pt(9)), "arrow": (ui, pt(12)),
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
        """Windows 10/11 immersive dark title bar, so the frame matches the theme. On
        macOS, Tk 8.6.10 and later can set the window's appearance the same way."""
        dark = self.cfg.get("theme", "dark") == "dark"
        if sm.MAC:
            try:
                self.root.tk.call("::tk::unsupported::MacWindowStyle", "appearance", win,
                                  "darkaqua" if dark else "aqua")
            except tk.TclError:
                pass
            return
        try:
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
            val = ctypes.c_int(1 if dark else 0)
            for attr in (20, 19):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), 4)
        except (AttributeError, OSError):
            pass

    def toggle_theme(self):
        self.cfg["theme"] = "light" if self.cfg.get("theme", "dark") == "dark" else "dark"
        self.t = THEMES[self.cfg["theme"]]
        self.save()
        self.build()
        if sm.WINDOWS:
            self.dark_titlebar(self.root)
            # nudge the frame so Windows repaints the title bar now, not on next focus
            self.root.withdraw()
            self.root.deiconify()
        elif sm.MAC:
            self.dark_titlebar(self.root)

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
        if self.station is None:
            self.add_log("warn", "Set your location and rotator in Settings first.")
            return
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
                     "off_az": sm.tidy(sm.angdiff(raz, az)), "off_el": sm.tidy(rel - el),
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
        cfg = dict(self.cfg)
        # the rotctl found automatically stays automatic, so a newer hamlib gets picked up
        if sm.same_path(cfg["rotctl"], sm.find_rotctl()):
            cfg["rotctl"] = ""
        try:
            sm.save_config(cfg)
        except OSError as e:
            self.add_log("bad", f"Couldn't save settings: {e}")

    def apply_settings(self, new):
        rotator_keys = ("rotctl", "model", "device", "speed")
        reconnect = (any(new[k] != self.cfg.get(k) for k in rotator_keys)
                     or self.tracker.snapshot()["link"] != "on")
        self.cfg.update(new)
        self.station = sm.station_from_cfg(self.cfg)
        self.first_run = False
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
        if self.station:
            w["station"].config(text=f"{self.station.label}, {self.station.alt_m:g} m")
        else:
            w["station"].config(text="No location yet. Open Settings to set up.")
        s = self.tracker.snapshot()
        on = s["link"] == "on"

        for body in ("sun", "moon"):
            b = w[body]
            if self.station is None:
                up = False
                b["az"].config(text="---", fg=t["faint"])
                b["el"].config(text="---", fg=t["faint"])
                note = "Needs your location."
            else:
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
            elif self.station is None:
                b["btn"].set(text="Set your location in Settings", enabled=False, kind="primary")
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
            off = f"{sm.tidy(sm.angdiff(pos[0], baz)):+.1f}\u00b0"
            if not cfg["az_only"]:
                off += f" / {sm.tidy(pos[1] - bel):+.1f}\u00b0"
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
    """Settings, and on first run the setup: there is no station or port to assume."""

    def __init__(self, app, first_run=False):
        super().__init__(app.root)
        self.app = app
        t, cfg = app.t, app.cfg
        self.withdraw()                       # build hidden, theme the title bar, then show
        self.title("Set up SunMoon" if first_run else "Settings")
        self.configure(bg=t["bg"])
        self.resizable(False, False)
        self.transient(app.root)

        body = tk.Frame(self, bg=t["card"], highlightthickness=1, highlightbackground=t["border"],
                        padx=18, pady=16)
        body.pack(fill="both", expand=True, padx=14, pady=14)
        self.v = {}
        rows = {}

        # two columns so it fits a 1080p screen: station and pointing left, rotator right
        top = tk.Frame(body, bg=t["card"])
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        left = tk.Frame(body, bg=t["card"])
        left.grid(row=1, column=0, sticky="nw", padx=(0, 28))
        right = tk.Frame(body, bg=t["card"])
        right.grid(row=1, column=1, sticky="nw")
        for col in (left, right):
            col.columnconfigure(1, weight=1)

        def nxt(parent):
            rows[parent] = rows.get(parent, -1) + 1
            return rows[parent]

        def section(parent, text):
            r = nxt(parent)
            app.lab(parent, text, "label", "muted").grid(row=r, column=0, columnspan=2, sticky="w",
                                                         pady=(0, 6) if r == 0 else (16, 6))

        def field(parent, label, widget, hint=None):
            r = nxt(parent)
            app.lab(parent, label).grid(row=r, column=0, sticky="w", padx=(0, 14), pady=3)
            widget.grid(row=r, column=1, sticky="ew", pady=3)
            if hint:
                app.lab(parent, hint, "small", "faint", wraplength=330, justify="left").grid(
                    row=nxt(parent), column=1, sticky="w", pady=(0, 4))

        def entry(parent, key, value, width=26):
            var = tk.StringVar(value="" if value is None else str(value))
            self.v[key] = var
            return ttk.Entry(parent, textvariable=var, width=width, font=app.f["body"])

        if first_run:
            app.lab(top, "Welcome to SunMoon", "head").pack(anchor="w")
            app.lab(top, "Easy calibration of your rotator using the sun or moon. Tell it where you are "
                         "and what drives your rotator. You can change any of this later in Settings.",
                    "small", "muted", wraplength=760, justify="left").pack(anchor="w", pady=(2, 14))

        section(left, "STATION")
        field(left, "Grid locator", entry(left, "grid", cfg["grid"]),
              "Your Maidenhead locator, e.g. IO74RE.")
        field(left, "Latitude", entry(left, "lat", cfg.get("lat")))
        field(left, "Longitude", entry(left, "lon", cfg.get("lon")),
              "Optional, beats the grid square. Decimal degrees, west negative.")
        field(left, "Height (m)", entry(left, "alt_m", cfg.get("alt_m", 0)))

        section(left, "POINTING")
        field(left, "Lowest elevation", entry(left, "min_el", cfg["min_el"]),
              "Degrees. Set it above your trees and roofs.")
        field(left, "Deadband", entry(left, "deadband", cfg["deadband"]),
              "Degrees. Re-commands only once the target moves this far.")
        self.v["refraction"] = tk.BooleanVar(value=bool(cfg.get("refraction", True)))
        field(left, "", ttk.Checkbutton(left, text="Allow for atmospheric refraction",
                                        variable=self.v["refraction"]))

        section(right, "ROTATOR")
        models = [f"{num}  {name}" for num, name in MODELS]
        cur = next((m for m in models if m.split()[0] == str(cfg["model"])), str(cfg["model"]))
        self.v["model"] = tk.StringVar(value=cur)
        field(right, "Model", ttk.Combobox(right, textvariable=self.v["model"], values=models, width=30,
                                           font=app.f["body"]),
              "Not listed? Type its hamlib number (rotctl -l).")

        self.ports = {label: dev for dev, label in serial_ports()}
        dev = cfg.get("device") or ""
        cur = next((label for label, d in self.ports.items() if d == dev), dev)
        self.v["device"] = tk.StringVar(value=cur)
        field(right, "Port", ttk.Combobox(right, textvariable=self.v["device"], values=list(self.ports),
                                          width=30, font=app.f["body"]),
              "For rotctld use model 2 and host:port, e.g. 127.0.0.1:4533.")

        self.v["speed"] = tk.StringVar(value=str(cfg.get("speed") or 9600))
        field(right, "Baud", ttk.Combobox(right, textvariable=self.v["speed"], values=BAUDS,
                                          state="readonly", width=30, font=app.f["body"]))

        self.v["az_only"] = tk.BooleanVar(value=bool(cfg["az_only"]))
        field(right, "", ttk.Checkbutton(right, text="Azimuth-only rotator", variable=self.v["az_only"]))

        rf = tk.Frame(right, bg=t["card"])
        rf.columnconfigure(0, weight=1)
        entry(rf, "rotctl", cfg["rotctl"], 24).grid(row=0, column=0, sticky="ew")
        FlatButton(rf, app, "Browse", self.browse, padx=10, pady=3).grid(row=0, column=1, padx=(6, 0))
        field(right, sm.ROTCTL, rf)
        hl = tk.Frame(right, bg=t["card"])
        hl.grid(row=nxt(right), column=1, sticky="w", pady=(0, 4))
        self.hamlib_note = app.lab(hl, "", "small", "faint", wraplength=330, justify="left")
        self.hamlib_note.pack(anchor="w")
        self.hamlib_link = app.lab(hl, sm.HAMLIB_DOWNLOAD, "small", "accent", cursor="hand2")
        self.hamlib_link.bind("<Button-1>", lambda e: webbrowser.open(sm.HAMLIB_DOWNLOAD))
        self.v["rotctl"].trace_add("write", lambda *a: self.hamlib_status())
        self.hamlib_status()

        self.err = app.lab(body, "", "small", "bad", wraplength=760, justify="left")
        self.err.grid(row=2, column=0, columnspan=2, sticky="w", pady=(12, 0))

        btns = tk.Frame(self, bg=t["bg"])
        btns.pack(fill="x", padx=14, pady=(0, 14))
        FlatButton(btns, app, "Start" if first_run else "Save", self.save, "primary").pack(side="right")
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

    def hamlib_status(self):
        path = self.v["rotctl"].get().strip()
        if path and os.path.isfile(path):
            text = ("Using the hamlib that came with SunMoon." if sm.is_bundled(path)
                    else "Found. SunMoon talks to your rotator through hamlib's rotctl.")
            self.hamlib_note.config(text=text, fg=self.app.t["faint"])
            self.hamlib_link.pack_forget()
        elif not sm.WINDOWS:
            # Linux gets hamlib from the distro, the Mac app carries its own: no download link
            text = ("Can't find rotctl. It comes inside SunMoon.app, so download SunMoon again."
                    if sm.FROZEN else f"hamlib isn't installed: {sm.HAMLIB_INSTALL}.")
            self.hamlib_note.config(text=text, fg=self.app.t["warn"])
            self.hamlib_link.pack_forget()
        else:
            text = ("Can't find rotctl.exe. hamlib comes in SunMoon's hamlib-w64 folder, so unzip "
                    "SunMoon again, or install hamlib-w64 from the link below and Browse to rotctl.exe "
                    "in its bin folder." if sm.FROZEN else
                    "hamlib isn't installed, or isn't where SunMoon looks. Get the Windows installer "
                    "(hamlib-w64) from the link below, then Browse to rotctl.exe in its bin folder.")
            self.hamlib_note.config(text=text, fg=self.app.t["warn"])
            self.hamlib_link.pack(anchor="w", pady=(2, 0))

    def browse(self):
        cur = self.v["rotctl"].get().strip()
        default = ("C:/Program Files" if sm.WINDOWS else
                   "/opt/homebrew/bin" if os.path.isdir("/opt/homebrew/bin") else
                   "/usr/local/bin" if sm.MAC else "/usr/bin")
        start = os.path.dirname(cur) if cur and os.path.isdir(os.path.dirname(cur)) else default
        types = [("rotctl", "rotctl.exe"), ("Programs", "*.exe")] if sm.WINDOWS else [("rotctl", "rotctl*")]
        path = filedialog.askopenfilename(parent=self, title=f"Find {sm.ROTCTL}", filetypes=types,
                                          initialdir=start)
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

        # same order as the form: where you are, then the rotator
        grid = v["grid"].strip().upper()
        lat_s, lon_s = v["lat"].strip(), v["lon"].strip()
        if bool(lat_s) != bool(lon_s):
            raise ValueError("Give both latitude and longitude, or leave both blank.")
        lat = num("lat", "Latitude", -90, 90) if lat_s else None
        lon = num("lon", "Longitude", -180, 180) if lon_s else None
        if lat is None and not grid:
            raise ValueError("Where are you? Give your grid locator, e.g. IO74RE.")
        if lat is None and not sm.GRID_RE.fullmatch(grid):
            raise ValueError("That grid locator doesn't look right. Four or six characters, e.g. IO74RE.")
        m = re.match(r"\s*(\d+)", v["model"])
        if not m:
            raise ValueError("Model needs a hamlib model number, e.g. 603.")
        model = int(m[1])
        port = v["device"].strip()
        device = self.ports.get(port) or (port.split()[0] if port else "")
        if model != 1 and not device:
            raise ValueError("Pick the rotator's port.")
        rotctl = v["rotctl"].strip().replace("\\", "/")
        if not os.path.isfile(rotctl):
            if sm.WINDOWS:
                raise ValueError("Can't find rotctl.exe there. Install hamlib (link above), then Browse to it.")
            raise ValueError(f"Can't find rotctl there. Install hamlib ({sm.HAMLIB_INSTALL}).")
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
    root = tk.Tk(className="sunmoon")       # WM_CLASS Sunmoon, matching sunmoon.desktop on Linux
    root.withdraw()
    app = App(root)
    root.update_idletasks()
    app.dark_titlebar(root)
    root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    main()
