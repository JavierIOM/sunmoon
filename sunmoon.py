#!/usr/bin/env python3
"""sunmoon: point the antenna at the sun or moon through hamlib's rotctl.

This is the engine: ephemeris, the rotctl driver, settings, and a command-line
front end. SunMoon (sunmoon_gui.py) is the window built on top of it. Runs on
Windows, Linux and macOS.

The sun and moon are the only targets whose position you know to a fraction of
a degree at any moment, which makes them the obvious check on rotator
calibration. Point at one, confirm by eye (the boom's shadow) or by ear (sun
noise peaking on the receiver), and nudge with the arrow keys until it peaks.
Whatever the rotator reads minus where the body really is, is your offset.

    python sunmoon.py where          sun and moon az/el right now, no rotator
    python sunmoon.py sun            point at the sun and track it
    python sunmoon.py moon           same for the moon
    python sunmoon.py sun --once     point, wait for it to settle, report, exit
    python sunmoon.py sun --dummy    rehearse against hamlib's simulated rotator

Rotator and station settings live in sunmoon.json: on Windows next to the
program, or in %APPDATA%/SunMoon when the program's folder is read-only; on
Linux in ~/.config/sunmoon; on macOS in ~/Library/Application Support/SunMoon.
Standard library only, nothing to pip install.
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from math import atan2, cos, degrees, radians, sin, sqrt, tan

try:
    import msvcrt  # Windows console keys, for nudging
except ImportError:
    msvcrt = None
try:
    import select
    import termios  # the same on Linux, a terminal in cbreak mode
    import tty
except ImportError:
    termios = None

__version__ = "1.4.0"

WINDOWS = os.name == "nt"
MAC = sys.platform == "darwin"
ROTCTL = "rotctl.exe" if WINDOWS else "rotctl"
# Built as SunMoon.exe the program sits in its own folder, not PyInstaller's temp dir.
# Built as SunMoon.app it's in Contents/MacOS, and what came with it is under Contents.
FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
BUNDLE = os.path.dirname(HERE) if FROZEN and MAC else HERE


def _config_path():
    """Windows: sunmoon.json next to the program, unless that folder is read-only
    (unzipped into Program Files, say), in which case %APPDATA%/SunMoon.
    Linux: ~/.config/sunmoon, as the .deb installs the program read-only.
    macOS: ~/Library/Application Support/SunMoon, where Mac apps keep theirs."""
    if MAC:
        folder = os.path.expanduser("~/Library/Application Support/SunMoon")
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, "sunmoon.json")
    if not WINDOWS:
        folder = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                              "sunmoon")
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, "sunmoon.json")
    local = os.path.join(HERE, "sunmoon.json")
    if os.path.isfile(local):
        return local
    probe = os.path.join(HERE, ".sunmoon-write-test")
    try:
        with open(probe, "w"):
            pass
        os.remove(probe)
        return local
    except OSError:
        folder = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "SunMoon")
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, "sunmoon.json")


CONFIG_PATH = _config_path()

# No station or port baked in: the first run asks. These only prefill the form.
DEFAULTS = {
    "rotctl": "",          # found automatically when blank, see find_rotctl
    "model": 603,          # hamlib rotator model, rotctl -l lists them. 603 is the Yaesu GS-232B
    "device": "",          # serial port, or host:port with model 2 (rotctld)
    "speed": 9600,         # serial baud, ignored for network models
    "az_only": False,      # true for an azimuth-only rotator
    "grid": "",            # Maidenhead locator, used when lat/lon are null
    "lat": None,
    "lon": None,
    "alt_m": 0,
    "min_el": 0.0,         # stop tracking below this (trees, houses)
    "step": 1.0,           # degrees per arrow-key nudge
    "deadband": 1.0,       # only re-command once the target moves this far
    "poll_s": 2.0,         # how often to read the rotator back
    "refraction": True,    # lift elevation for the atmosphere, half a degree at the horizon
    "theme": "dark",       # the window's theme, dark or light
}

POLL_TIMEOUT_S = 240       # --once gives up waiting for a settle after this


# --- astronomy --------------------------------------------------------------
#
# Sun and moon from Paul Schlyter's "How to compute planetary positions", with
# his main lunar perturbation terms. Elements are referred to the equinox of
# date, so no separate precession step. The moon is corrected for parallax
# (up to a degree), which matters far more than any of the terms left out.

AU_IN_EARTH_RADII = 149597870.7 / 6378.137
DELTA_T_DAYS = 69.0 / 86400   # TT minus UT, near enough for 2020s


def rev(x):
    return x % 360.0


def angdiff(a, b):
    """a - b, wrapped into -180..180."""
    return (a - b + 180.0) % 360.0 - 180.0


def tidy(x):
    """Rounded to 0.1, and never -0.0: a reading 0.04 low is +0.0, not -0.0."""
    return round(x, 1) + 0.0


def julian_day(t):
    return t.timestamp() / 86400.0 + 2440587.5


def grid_to_latlon(grid):
    """Centre of a 4 or 6 character Maidenhead locator."""
    g = grid.strip().upper()
    lon = (ord(g[0]) - 65) * 20 - 180 + int(g[2]) * 2
    lat = (ord(g[1]) - 65) * 10 - 90 + int(g[3])
    if len(g) >= 6:
        lon += (ord(g[4]) - 65) * 5 / 60 + 2.5 / 60
        lat += (ord(g[5]) - 65) * 2.5 / 60 + 1.25 / 60
    else:
        lon += 1.0
        lat += 0.5
    return lat, lon


def _sun_elements(d):
    w = 282.9404 + 4.70935e-5 * d
    e = 0.016709 - 1.151e-9 * d
    m = rev(356.0470 + 0.9856002585 * d)
    return w, e, m


def sun_ecliptic(d):
    """Geocentric ecliptic lon, lat (deg) and distance (Earth radii)."""
    w, e, m = _sun_elements(d)
    ea = m + degrees(e * sin(radians(m)) * (1 + e * cos(radians(m))))
    xv = cos(radians(ea)) - e
    yv = sqrt(1 - e * e) * sin(radians(ea))
    lon = rev(degrees(atan2(yv, xv)) + w)
    return lon, 0.0, sqrt(xv * xv + yv * yv) * AU_IN_EARTH_RADII


def moon_ecliptic(d):
    """Geocentric ecliptic lon, lat (deg) and distance (Earth radii)."""
    n = 125.1228 - 0.0529538083 * d
    inc = 5.1454
    w = 318.0634 + 0.1643573223 * d
    a = 60.2666
    e = 0.054900
    m = rev(115.3654 + 13.0649929509 * d)

    ea = m + degrees(e * sin(radians(m)) * (1 + e * cos(radians(m))))
    for _ in range(5):
        ea -= (ea - degrees(e * sin(radians(ea))) - m) / (1 - e * cos(radians(ea)))
    xv = a * (cos(radians(ea)) - e)
    yv = a * sqrt(1 - e * e) * sin(radians(ea))
    r = sqrt(xv * xv + yv * yv)

    vw, nr, ir = radians(degrees(atan2(yv, xv)) + w), radians(n), radians(inc)
    xh = r * (cos(nr) * cos(vw) - sin(nr) * sin(vw) * cos(ir))
    yh = r * (sin(nr) * cos(vw) + cos(nr) * sin(vw) * cos(ir))
    zh = r * sin(vw) * sin(ir)
    lon = degrees(atan2(yh, xh))
    lat = degrees(atan2(zh, sqrt(xh * xh + yh * yh)))

    ws, _, ms = _sun_elements(d)
    mm = m
    dd = (m + w + n) - (ms + ws)   # mean elongation
    f = m + w                      # argument of latitude

    def s(x):
        return sin(radians(x))

    def c(x):
        return cos(radians(x))

    lon += (-1.274 * s(mm - 2 * dd)        # evection
            + 0.658 * s(2 * dd)            # variation
            - 0.186 * s(ms)                # yearly equation
            - 0.059 * s(2 * mm - 2 * dd)
            - 0.057 * s(mm - 2 * dd + ms)
            + 0.053 * s(mm + 2 * dd)
            + 0.046 * s(2 * dd - ms)
            + 0.041 * s(mm - ms)
            - 0.035 * s(dd)                # parallactic equation
            - 0.031 * s(mm + ms)
            - 0.015 * s(2 * f - 2 * dd)
            + 0.011 * s(mm - 4 * dd))
    lat += (-0.173 * s(f - 2 * dd)
            - 0.055 * s(mm - f - 2 * dd)
            - 0.046 * s(mm + f - 2 * dd)
            + 0.033 * s(f + 2 * dd)
            + 0.017 * s(2 * mm + f))
    r += -0.58 * c(mm - 2 * dd) - 0.46 * c(2 * dd)
    return rev(lon), lat, r


def gmst_deg(jd):
    t = (jd - 2451545.0) / 36525
    return rev(280.46061837 + 360.98564736629 * (jd - 2451545.0)
               + 0.000387933 * t * t - t ** 3 / 38710000)


class Station:
    def __init__(self, lat, lon, alt_m, label):
        self.lat, self.lon, self.alt_m, self.label = lat, lon, alt_m, label
        f = 1 / 298.257223563                      # WGS84
        phi, lam = radians(lat), radians(lon)
        cc = 1 / sqrt(cos(phi) ** 2 + (1 - f) ** 2 * sin(phi) ** 2)
        ss = (1 - f) ** 2 * cc
        h = alt_m / 6378137.0
        self.ecef = ((cc + h) * cos(phi) * cos(lam),
                     (cc + h) * cos(phi) * sin(lam),
                     (ss + h) * sin(phi))


def refraction_deg(el):
    """Saemundsson's formula, true altitude to apparent. Half a degree at the horizon."""
    el = max(el, -1.0)
    return 1.02 / tan(radians(el + 10.3 / (el + 5.11))) / 60


def az_el(body, t, station, refraction=True):
    """Topocentric azimuth (from true north) and elevation, degrees."""
    jd = julian_day(t)
    d = jd - 2451543.5 + DELTA_T_DAYS
    lon, lat, r = (sun_ecliptic if body == "sun" else moon_ecliptic)(d)

    ecl = radians(23.4393 - 3.563e-7 * d)
    lr, br = radians(lon), radians(lat)
    x = r * cos(br) * cos(lr)
    y0 = r * cos(br) * sin(lr)
    z0 = r * sin(br)
    y = y0 * cos(ecl) - z0 * sin(ecl)
    z = y0 * sin(ecl) + z0 * cos(ecl)

    g = radians(gmst_deg(jd))                      # into the Earth-fixed frame
    dx = x * cos(g) + y * sin(g) - station.ecef[0]
    dy = -x * sin(g) + y * cos(g) - station.ecef[1]
    dz = z - station.ecef[2]

    phi, lam = radians(station.lat), radians(station.lon)
    east = -sin(lam) * dx + cos(lam) * dy
    north = -sin(phi) * cos(lam) * dx - sin(phi) * sin(lam) * dy + cos(phi) * dz
    up = cos(phi) * cos(lam) * dx + cos(phi) * sin(lam) * dy + sin(phi) * dz

    az = rev(degrees(atan2(east, north)))
    el = degrees(atan2(up, sqrt(east * east + north * north)))
    if refraction:
        el += refraction_deg(el)
    return az, el


def moon_phase(t):
    """Fraction of the disc lit (0..1) and whether it is waxing."""
    d = julian_day(t) - 2451543.5 + DELTA_T_DAYS
    elong = rev(moon_ecliptic(d)[0] - sun_ecliptic(d)[0])   # east of the sun = waxing
    return (1 - cos(radians(elong))) / 2, elong < 180


def phase_name(lit, waxing):
    if lit < 0.03:
        return "new moon"
    if lit > 0.97:
        return "full moon"
    if 0.45 <= lit <= 0.55:
        return "first quarter" if waxing else "last quarter"
    shape = "crescent" if lit < 0.5 else "gibbous"
    return f"{'waxing' if waxing else 'waning'} {shape}"


def next_crossing(body, station, start, min_el, rising, refraction, hours=48):
    """When the body next rises above (or sets below) min_el, or None."""
    step = timedelta(minutes=5)

    def up(t):
        return az_el(body, t, station, refraction)[1] >= min_el

    t0, u0 = start, up(start)
    end = start + timedelta(hours=hours)
    while t0 < end:
        t1 = t0 + step
        u1 = up(t1)
        if u1 != u0 and u1 == rising:
            lo, hi = t0, t1
            for _ in range(12):
                mid = lo + (hi - lo) / 2
                if up(mid) == rising:
                    hi = mid
                else:
                    lo = mid
            return hi
        t0, u0 = t1, u1
    return None


# --- rotator ----------------------------------------------------------------

class RotatorError(Exception):
    def __init__(self, msg, reply=""):
        super().__init__(msg)
        self.reply = reply                         # everything rotctl said, not just the last line


def _last_line(text):
    lines = [ln.strip() for ln in text.replace("Rotator command:", "\n").splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return "no output"
    # hamlib ends on a bare "IO error" and says what actually went wrong just above it
    if len(lines) > 1 and len(lines[-1]) < 30:
        return f"{lines[-2]} ({lines[-1]})"
    return lines[-1]


class Rotator:
    """One long-lived rotctl.exe in interactive mode, driven over a pipe.

    rotctl's "-" stdin mode is broken on the Windows build (it reads stray NUL
    commands), but plain interactive mode behaves over a pipe: every reply ends
    with a fresh "Rotator command:" prompt, which is what we wait for. One
    process means the serial port is opened once, not once per move.
    """

    PROMPT = "Rotator command:"
    POS = re.compile(r"Azimuth:\s*(-?\d+(?:\.\d+)?)\s+Elevation:\s*(-?\d+(?:\.\d+)?)")

    def __init__(self, exe, model, device, speed):
        if not os.path.isfile(exe):
            raise RotatorError(f"rotctl not found at {exe}, fix 'rotctl' in sunmoon.json")
        # hamlib 4.3 (Ubuntu 22.04) only says "serial_open return(-6)" for both of
        # these, so check the port ourselves and say which it is
        if device and str(device).startswith("/dev/"):
            if not os.path.exists(device):
                raise RotatorError(f"could not open the rotator: {device}: No such file or directory")
            if not os.access(device, os.R_OK | os.W_OK):
                raise RotatorError(f"could not open the rotator: {device}: Permission denied")
        self.az_turn = 0.0                         # see set_pos
        args = [exe, "-m", str(model)]
        if device:
            args += ["-r", str(device)]
        if speed:
            args += ["-s", str(speed)]
        # no console window flashing up when started from SunMoon.exe
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, bufsize=0, creationflags=flags)
        self.buf = ""
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()
        self._wait(15, "open the rotator")

    def _read(self):
        while True:
            data = self.proc.stdout.read(4096)
            if not data:
                return
            with self.lock:
                self.buf += data.decode("ascii", "replace")

    def _wait(self, timeout, what):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                text = self.buf
            if self.PROMPT in text:
                reply = text.split(self.PROMPT)[0]
                if "error =" in reply:
                    raise RotatorError(f"could not {what}: {_last_line(reply)}", reply)
                return reply
            if self.proc.poll() is not None:
                time.sleep(0.2)                    # let the reader drain
                with self.lock:
                    text = self.buf
                raise RotatorError(f"could not {what}: {_last_line(text)}")
            time.sleep(0.02)
        raise RotatorError(f"could not {what}: no answer from rotctl in {timeout}s")

    def _send(self, line, what, timeout=10):
        with self.lock:
            self.buf = ""
        try:
            self.proc.stdin.write((line + "\n").encode("ascii"))
            self.proc.stdin.flush()
        except OSError:
            return self._wait(1, what)             # raises with rotctl's last words
        return self._wait(timeout, what)

    def get_pos(self, tries=4):
        # The GS-232B answers a move with a CR that can land after hamlib's input
        # flush, so the next C2 reads that one byte and hamlib calls it a protocol
        # error (seen on COM8, 1 in 4 straight after a move). Asking again is clean.
        err = None
        for _ in range(tries):
            try:
                reply = self._send("p", "read the position")
                m = self.POS.search(reply)
                if m:
                    return float(m[1]), float(m[2])
                err = RotatorError(f"could not read the position from {reply.strip()!r}")
            except RotatorError as e:
                if self.proc.poll() is not None:
                    raise
                err = e
            time.sleep(0.3)
        raise err

    def set_pos(self, az, el):
        # Azimuth comes in as 0 to 360. Some rotators take -180 to 180 instead (hamlib
        # 4.3's simulator, anything set up with min_az=-180,max_az=180), so when hamlib
        # says "range problem", send the same direction a turn the other way, and keep
        # using whichever turn worked last.
        turns = [self.az_turn] + [t for t in (0.0, -360.0, 360.0) if t != self.az_turn]
        for turn in turns:
            try:
                self._send(f"P {az + turn:.1f} {el:.1f}", f"move to az {az:.1f} el {el:.1f}")
                self.az_turn = turn
                return
            except RotatorError as e:
                said = e.reply + str(e)            # the debug line, or just the error code
                if self.proc.poll() is not None or not ("range problem" in said
                                                        or "Invalid parameter" in said):
                    raise
                err = e
        raise err

    def stop(self):
        self._send("S", "stop")

    def close(self):
        try:
            self.proc.stdin.write(b"q\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()


# --- the bit you look at ----------------------------------------------------

ARROWS = {"H": "up", "P": "down", "K": "left", "M": "right"}
# what a terminal sends after ESC, in normal and application cursor mode
ESC_ARROWS = {"[A": "up", "[B": "down", "[C": "right", "[D": "left",
              "OA": "up", "OB": "down", "OC": "right", "OD": "left"}


def have_keys():
    return bool(msvcrt) or bool(termios and sys.stdin.isatty())


@contextmanager
def key_mode():
    """Keypresses without Enter on Linux, put back as it was afterwards. Ctrl+C
    still works. Windows' msvcrt needs nothing."""
    if msvcrt or not have_keys():
        yield
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    try:
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def read_keys():
    keys = []
    if msvcrt:
        try:
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    keys.append(ARROWS.get(msvcrt.getwch(), ""))
                else:
                    keys.append(ch.lower())
        except OSError:                            # no console attached
            pass
    elif have_keys():
        fd = sys.stdin.fileno()
        while select.select([fd], [], [], 0)[0]:
            ch = os.read(fd, 1).decode("latin-1")
            if ch == "\x1b" and select.select([fd], [], [], 0.05)[0]:
                keys.append(ESC_ARROWS.get(os.read(fd, 2).decode("latin-1"), ""))
            else:
                keys.append(ch.lower())
    return keys


def utcnow():
    return datetime.now(timezone.utc)


def local_hm(t):
    loc = t.astimezone()
    today = datetime.now().astimezone().date()
    if loc.date() == today:
        return loc.strftime("%H:%M")
    if loc.date() == today + timedelta(days=1):
        return loc.strftime("%H:%M tomorrow")
    return loc.strftime("%H:%M %a %d %b")


def up_down_note(body, station, cfg, refraction):
    now = utcnow()
    el = az_el(body, now, station, refraction)[1]
    if el >= cfg["min_el"]:
        t = next_crossing(body, station, now, cfg["min_el"], False, refraction)
        return "up, " + (f"sets {local_hm(t)}" if t else "stays up")
    t = next_crossing(body, station, now, cfg["min_el"], True, refraction)
    return "down, " + (f"rises {local_hm(t)}" if t else "not up in the next 48 hours")


def cmd_where(station, cfg, refraction):
    now = utcnow()
    print(f"{station.label}   {now:%Y-%m-%d %H:%M:%S} UTC")
    print()
    print(f"{'':6}{'az':>7}{'el':>7}")
    for body in ("sun", "moon"):
        az, el = az_el(body, now, station, refraction)
        print(f"{body.title():6}{az:7.1f}{el:7.1f}   {up_down_note(body, station, cfg, refraction)}")
    if cfg["min_el"]:
        print(f"\nRise and set are against your min_el of {cfg['min_el']:.1f} deg.")


def cmd_track(body, station, cfg, rot, refraction, once, rot_label):
    min_el, step, deadband, poll_s = cfg["min_el"], cfg["step"], cfg["deadband"], cfg["poll_s"]
    az_only = cfg["az_only"]

    az, el = az_el(body, utcnow(), station, refraction)
    if el < min_el:
        print(f"The {body} is at el {el:.1f}, below your {min_el:.1f} deg limit. "
              f"{up_down_note(body, station, cfg, refraction).capitalize()}.")
        return 1

    print(f"Pointing at the {body} from {station.label}, rotator {rot_label}.")
    if once:
        print("Waiting for the rotator to settle. Ctrl+C to give up.")
    elif have_keys():
        print(f"Arrows nudge {step:g} deg, space marks a reading, 0 clears the nudge, q quits.")
    else:
        print("Ctrl+C quits.")
    print()

    nudge_az = nudge_el = 0.0
    sent = None
    sent_at = 0.0
    pos = prev = None
    next_poll = 0.0
    last_mark = None
    settled = False
    started = time.monotonic()
    status = ""

    def show(line, keep=False):
        nonlocal status
        pad = max(len(status) - len(line), 0)
        print("\r" + line + " " * pad, end="\n" if keep else "", flush=True)
        status = "" if keep else line

    try:
        while True:
            now = utcnow()
            az, el = az_el(body, now, station, refraction)
            if el < min_el:
                show(f"The {body} has gone below {min_el:.1f} deg, leaving the antenna here.", keep=True)
                break

            force = False
            for k in read_keys():
                if k in ("q", "\x1b"):
                    raise KeyboardInterrupt
                if k in ("left", "right"):
                    nudge_az += step if k == "right" else -step
                    force, settled = True, False
                elif k in ("up", "down") and not az_only:
                    nudge_el += step if k == "up" else -step
                    force, settled = True, False
                elif k in ("0", "c"):
                    nudge_az = nudge_el = 0.0
                    force, settled = True, False
                elif k in (" ", "m", "\r", "\n") and pos and not settled:
                    show("still moving, mark that again once it says settled", keep=True)
                elif k in (" ", "m", "\r", "\n") and pos:
                    last_mark = (now, az, el, pos)
                    show(f"mark {now:%H:%M:%S}  {body} {az:5.1f}/{el:4.1f}  "
                         f"rotator {pos[0]:5.1f}/{pos[1]:4.1f}  "
                         f"offset {tidy(angdiff(pos[0], az)):+.1f}"
                         + ("" if az_only else f"/{tidy(pos[1] - el):+.1f}"), keep=True)

            cmd_az = rev(az + nudge_az)
            cmd_el = 0.0 if az_only else min(max(el + nudge_el, 0.0), 90.0)
            if (sent is None or force
                    or abs(angdiff(cmd_az, sent[0])) >= deadband
                    or abs(cmd_el - sent[1]) >= deadband):
                rot.set_pos(cmd_az, cmd_el)
                sent, sent_at = (cmd_az, cmd_el), time.monotonic()
                next_poll = sent_at + 0.5          # let the controller's reply to the move land first

            if time.monotonic() >= next_poll:
                prev, pos = pos, rot.get_pos()
                next_poll = time.monotonic() + poll_s

            settled = bool(pos and prev and time.monotonic() - sent_at > poll_s * 1.5
                           and abs(angdiff(pos[0], prev[0])) < 0.15 and abs(pos[1] - prev[1]) < 0.15)

            if pos:
                line = (f"{now:%H:%M:%S} UTC  {body} {az:5.1f}/{el:4.1f}  "
                        f"rotator {pos[0]:5.1f}/{pos[1]:4.1f}  "
                        f"offset {tidy(angdiff(pos[0], az)):+5.1f}"
                        + ("" if az_only else f"/{tidy(pos[1] - el):+4.1f}"))
                if nudge_az or nudge_el:
                    line += f"  nudge {nudge_az:+.1f}" + ("" if az_only else f"/{nudge_el:+.1f}")
                line += "  settled" if settled else "  moving "
                show(line)

            if once and settled:
                show(status, keep=True)
                last_mark = (now, az, el, pos)
                break
            if once and time.monotonic() - started > POLL_TIMEOUT_S:
                show(f"Still moving after {POLL_TIMEOUT_S}s, giving up.", keep=True)
                return 1
            time.sleep(0.1)
    except KeyboardInterrupt:
        if status:
            show(status, keep=True)
        try:
            rot.stop()
        except RotatorError:
            pass

    if last_mark:
        _, az, el, pos = last_mark
        off_az = tidy(angdiff(pos[0], az))
        off_el = tidy(pos[1] - el)
        print()
        print(f"Rotator read {pos[0]:.1f}" + ("" if az_only else f"/{pos[1]:.1f}")
              + f" with the {body} at {az:.1f}" + ("" if az_only else f"/{el:.1f}") + ".")
        if once:
            print("That only tells you the controller went where it was told. The real test is")
            print(f"whether the antenna is actually on the {body}: check the shadow or the noise,")
            print("then run without --once and nudge until it peaks.")
        else:
            flags = f"-o {off_az:+.1f}" + ("" if az_only else f" -O {off_el:+.1f}")
            print(f"If the antenna was dead on the {body} at that mark, the offset is "
                  f"{off_az:+.1f} az" + ("" if az_only else f", {off_el:+.1f} el") + ".")
            print(f"To correct it, give rotctl/rotctld {flags}")
    elif not once:
        print("\nNo mark taken, so no offset to report. Space marks a reading once it's settled.")
    return 0


HAMLIB_DOWNLOAD = "https://github.com/Hamlib/Hamlib/releases"
# how to get hamlib where it doesn't come with SunMoon: Linux, or macOS from source
HAMLIB_INSTALL = ("brew install hamlib, or sudo port install hamlib with MacPorts" if MAC else
                  "sudo apt install libhamlib-utils on Debian, Ubuntu or Raspberry Pi OS, "
                  "or your distro's hamlib package")


def no_rotctl_hint():
    if WINDOWS:
        return f"Install hamlib from {HAMLIB_DOWNLOAD}, or set 'rotctl' in sunmoon.json."
    return f"Install hamlib: {HAMLIB_INSTALL}."


def _version_key(path):
    return [int(n) for n in re.findall(r"\d+", os.path.basename(os.path.dirname(os.path.dirname(path))))]


def find_rotctl(configured=""):
    """The configured rotctl.exe if it exists, else the newest hamlib in the usual
    places, else whatever is on PATH. Empty string when there is none.

    The release zip ships hamlib in a hamlib-w64-<version> folder next to SunMoon.exe,
    so the HERE search finds it. HERE goes first because max() keeps the first of
    equals: ours wins a tie, a newer hamlib installed elsewhere wins outright.

    On Linux hamlib comes from the distro (libhamlib-utils), so it's on PATH.

    SunMoon.app carries its own rotctl, built from the Hamlib source. Without it,
    Homebrew (Apple Silicon, then Intel) and MacPorts, by full path, because an app
    started from Finder doesn't get the shell's PATH."""
    if configured and os.path.isfile(configured):
        return configured
    if MAC:
        for path in (os.path.join(getattr(sys, "_MEIPASS", HERE), "hamlib", "rotctl"),
                     "/opt/homebrew/bin/rotctl", "/usr/local/bin/rotctl", "/opt/local/bin/rotctl"):
            if os.path.isfile(path):
                return path
    found = []
    roots = (HERE, "C:/Program Files", "C:/Program Files (x86)", "C:/Radio", "C:/") if WINDOWS else ()
    for root in roots:
        found += glob.glob(os.path.join(root, "hamlib*", "bin", "rotctl.exe"))
    if found:
        return max(found, key=_version_key).replace(os.sep, "/")
    on_path = shutil.which("rotctl")
    return on_path.replace(os.sep, "/") if on_path else ""


def same_path(a, b):
    return bool(a and b) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def is_bundled(path):
    """True for the hamlib that came in SunMoon's own folder, or inside SunMoon.app."""
    return bool(path) and os.path.normcase(os.path.abspath(path)).startswith(
        os.path.normcase(os.path.abspath(BUNDLE)) + os.sep)


def config_exists():
    return os.path.isfile(CONFIG_PATH)


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8-sig") as fh:
            cfg.update(json.load(fh))
    return cfg


def save_config(cfg):
    keep = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(keep, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, CONFIG_PATH)


GRID_RE = re.compile(r"[A-R]{2}\d{2}([A-X]{2})?", re.I)


def station_from_cfg(cfg):
    """The station, or ValueError when no location has been set yet."""
    if cfg.get("lat") is not None and cfg.get("lon") is not None:
        lat, lon = float(cfg["lat"]), float(cfg["lon"])
        label = f"{lat:.4f}, {lon:.4f}"
    else:
        if not GRID_RE.fullmatch((cfg.get("grid") or "").strip()):
            raise ValueError("no location set: give your grid locator, or lat and lon")
        lat, lon = grid_to_latlon(cfg["grid"])
        label = f"{cfg['grid'].upper()} ({lat:.4f}, {lon:.4f})"
    return Station(lat, lon, float(cfg.get("alt_m") or 0), label)


def main():
    ap = argparse.ArgumentParser(description="Point the antenna at the sun or moon via rotctl.")
    ap.add_argument("target", choices=["sun", "moon", "where"])
    ap.add_argument("--once", action="store_true", help="point, wait to settle, report, exit")
    ap.add_argument("--dummy", action="store_true", help="use hamlib's simulated rotator")
    ap.add_argument("--model", type=int, help="hamlib rotator model, overrides sunmoon.json")
    ap.add_argument("--device", help="serial port or host:port, overrides sunmoon.json")
    ap.add_argument("--step", type=float, help="degrees per arrow-key nudge")
    ap.add_argument("--no-refraction", action="store_true",
                    help="geometric elevation, no atmospheric lift near the horizon")
    args = ap.parse_args()

    try:
        cfg = load_config()
    except (OSError, ValueError) as e:
        print(f"sunmoon.json is broken: {e}")
        return 2
    if args.model is not None:
        cfg["model"] = args.model
    if args.device is not None:
        cfg["device"] = args.device
    if args.step is not None:
        cfg["step"] = args.step
    if args.dummy:
        cfg.update(model=1, device=None, speed=None)

    try:
        station = station_from_cfg(cfg)
    except ValueError as e:
        print(f"sunmoon.json: {e}. Run SunMoon once to set up, or edit {CONFIG_PATH}.")
        return 2
    refraction = cfg.get("refraction", True) and not args.no_refraction
    cfg["rotctl"] = find_rotctl(cfg["rotctl"])
    if not cfg["rotctl"]:
        print(f"Can't find {ROTCTL}. {no_rotctl_hint()}")
        return 2

    if args.target == "where":
        cmd_where(station, cfg, refraction)
        return 0

    rot_label = "dummy (simulated)" if args.dummy else f"model {cfg['model']} on {cfg['device']}"
    try:
        rot = Rotator(cfg["rotctl"], cfg["model"], cfg.get("device"), cfg.get("speed"))
    except RotatorError as e:
        print(f"Rotator {rot_label}: {e}")
        if any(s in str(e) for s in ("already open", "Access denied", "resource busy")):
            print("Something else has the port. Close whatever else drives the rotator "
                  "(PstRotator, HRD Rotator, OscarWatch, rotctld, Gpredict) first.")
        elif "Permission denied" in str(e):
            print("Add yourself to the dialout group: sudo usermod -aG dialout $USER, "
                  "then log out and back in.")
        return 2
    try:
        with key_mode():
            return cmd_track(args.target, station, cfg, rot, refraction, args.once, rot_label)
    except RotatorError as e:
        print(f"\nRotator {rot_label}: {e}")
        return 2
    finally:
        rot.close()


if __name__ == "__main__":
    sys.exit(main())
