#!/usr/bin/env python3
"""Smoke test for a built SunMoon, run by the Linux and macOS workflows.

    xvfb-run -a -s "-screen 0 1280x1000x24" /usr/bin/python3 tests/smoke.py shots
    python3 tests/smoke.py --mac /path/to/SunMoon.app shots

Linux tests the installed .deb: /usr/share/sunmoon, the sunmoon command, the
distro's rotctl. macOS tests the built app and its own rotctl, and runs the
command line and the window from the source under python.org's Python, which
is what the app is built from.

Checks the rotctl driver against hamlib's simulator, what the errors say, the
command line with and without a terminal, that the program finds the right
rotctl, the window, and the GPS reader against a pretend GPS on a pty, and saves
screenshots into the folder given.
"""

import argparse
import glob
import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import threading
import time
import tty
from datetime import timedelta

ap = argparse.ArgumentParser()
ap.add_argument("--mac", metavar="APP", help="test this SunMoon.app rather than the Linux install")
ap.add_argument("out", nargs="?", default="shots")
args = ap.parse_args()
OUT = os.path.abspath(args.out)
os.makedirs(OUT, exist_ok=True)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# settings somewhere empty; sunmoon reads where on import, so this goes first
home = tempfile.mkdtemp()
if args.mac:
    APP = os.path.abspath(args.mac)
    os.environ["HOME"] = home                  # ~/Library/Application Support/SunMoon
    SRC = REPO
    ROTCTL = [p for p in glob.glob(os.path.join(APP, "Contents", "**", "rotctl"), recursive=True)
              if os.path.isfile(p) and not os.path.islink(p)][0]
    CLI = [sys.executable, os.path.join(REPO, "sunmoon.py")]
    GUI = [os.path.join(APP, "Contents", "MacOS", "SunMoon")]
    SHOT = ["screencapture", "-x"]
else:
    os.environ["XDG_CONFIG_HOME"] = home
    SRC = "/usr/share/sunmoon"
    ROTCTL = "/usr/bin/rotctl"
    CLI = ["sunmoon"]
    GUI = ["sunmoon"]
    SHOT = ["import", "-window", "root"]
sys.path.insert(0, SRC)
import sunmoon as sm          # noqa: E402
import sunmoon_gui as g       # noqa: E402
import tkinter as tk          # noqa: E402

failures = []


def check(ok, what):
    print(("ok    " if ok else "FAIL  ") + what, flush=True)
    if not ok:
        failures.append(what)


def write_cfg(**kw):
    cfg = {"grid": "IO74RE", "model": 1, "theme": "dark"}
    if args.mac:
        cfg["rotctl"] = ROTCTL                 # run from source, it can't see the app's own
    cfg.update(kw)
    with open(sm.CONFIG_PATH, "w") as fh:
        json.dump(cfg, fh)


def shot(name):
    subprocess.run(SHOT + [os.path.join(OUT, name + ".png")], check=False)


def rotctl_processes():
    r = subprocess.run(["ps", "-axo", "command"], capture_output=True, text=True)
    return [ln for ln in r.stdout.splitlines() if "rotctl" in ln and " -m " in ln]


# 1. where things are
if not args.mac:
    check(sm.find_rotctl("") == ROTCTL, f"rotctl found at {sm.find_rotctl('')!r}")
ver = subprocess.run([ROTCTL, "--version"], capture_output=True, text=True).stdout
print("     ", ver.splitlines()[0] if ver else "(no version)", "at", ROTCTL)
check(sm.CONFIG_PATH.startswith(home), f"settings at {sm.CONFIG_PATH}")

# whichever is up, so the pointing tests point at something; if neither, allow below
now = sm.utcnow()
els = {b: sm.az_el(b, now, sm.station_from_cfg({"grid": "IO74RE"}), True)[1] for b in ("sun", "moon")}
body = max(els, key=els.get)
min_el = 0.0 if els[body] > 1 else -90.0
print(f"      pointing at the {body}, el {els[body]:.1f}, min_el {min_el}")

# 2. the driver: interactive rotctl over a pipe, against the simulator
rot = sm.Rotator(ROTCTL, 1, None, None)
rot.set_pos(123.0, 45.0)
pos, deadline = None, time.time() + 120
while time.time() < deadline:
    pos = rot.get_pos()
    if abs(pos[0] - 123) < 0.5 and abs(pos[1] - 45) < 0.5:
        break
    time.sleep(1)
rot.stop()
rot.close()
check(pos is not None and abs(pos[0] - 123) < 0.5 and abs(pos[1] - 45) < 0.5,
      f"simulator moved to 123/45, read {pos}")

# 3. the errors, raw and as the window puts them
for dev in ["/dev/ttyNOPE"] + ([] if args.mac else ["/dev/ttyS0"]):
    try:
        r = sm.Rotator(ROTCTL, 603, dev, 9600)
        r.close()
        print(f"      {dev}: opened without complaint")
    except sm.RotatorError as e:
        shown = g.explain(str(e), {"device": dev})
        print(f"      {dev}: rotctl says  {e}")
        print(f"      {dev}: SunMoon says {shown}")
        if dev == "/dev/ttyNOPE":
            check("There's no" in shown, "missing port explained")
        elif not os.access(dev, os.R_OK | os.W_OK):
            check("dialout" in shown, "no permission explained")

# 4. the command line, no terminal
write_cfg(min_el=min_el)
r = subprocess.run(CLI + ["where"], capture_output=True, text=True, timeout=60)
print(r.stdout)
check(r.returncode == 0 and "Sun" in r.stdout and "Moon" in r.stdout, "where")
r = subprocess.run(CLI + [body, "--dummy", "--once"], capture_output=True, text=True, timeout=300)
print(r.stdout[-800:], r.stderr[-800:])
check(r.returncode == 0 and "Rotator read" in r.stdout, f"{body} --dummy --once")

# 5. the command line in a real terminal: right arrow nudges, q quits
pid, fd = pty.fork()
if pid == 0:
    os.execvp(CLI[0], CLI + [body, "--dummy"])
out = b""


def read_for(sec):
    global out
    end = time.time() + sec
    while time.time() < end:
        if select.select([fd], [], [], 0.2)[0]:
            try:
                out += os.read(fd, 4096)
            except OSError:
                return


read_for(5)
os.write(fd, b"\x1b[C")
read_for(4)
os.write(fd, b"q")
read_for(5)
_, status = os.waitpid(pid, 0)
text = out.decode(errors="replace")
print(text[-700:])
check("Arrows nudge" in text, "terminal offers the keys")
check("nudge +1.0" in text, "right arrow nudged azimuth by 1")
check(os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, "q quit cleanly")

# 6. the program as built: it finds its own rotctl and connects to the simulator
write_cfg(min_el=min_el, rotctl="")
p = subprocess.Popen(GUI, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
time.sleep(8)
procs = rotctl_processes()
print("      rotctl running:", procs)
want = os.path.realpath(ROTCTL) if not args.mac else APP
check(any(want in os.path.realpath(ln.split(" -m ")[0].strip()) or want in ln for ln in procs),
      "the program started its own rotctl")
shot("0_connected")
p.terminate()
print(p.communicate(timeout=15)[0].decode(errors="replace")[-1500:])

# 7. first run: no settings yet, so the setup dialog, and nothing saved before Start
os.remove(sm.CONFIG_PATH)
p = subprocess.Popen(GUI, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
time.sleep(8)
shot("1_first_run")
alive = p.poll() is None
p.terminate()
print(p.communicate(timeout=15)[0].decode(errors="replace")[-1500:])
check(alive, "window opened and stayed up")
check(not os.path.exists(sm.CONFIG_PATH), "nothing saved before setup")

# 8. the window, pointing, nudged, marked, then the light theme
write_cfg(min_el=min_el, alt_m=63)
root = tk.Tk(className="sunmoon")
app = g.App(root)
root.geometry("+0+0")
t0 = time.time()
result = {}


def step():
    s = app.tracker.snapshot()
    if "pointed" not in result and s["link"] == "on":
        result["pointed"] = True
        app.point(body)
        root.after(2000, lambda: (app.set_step(2.0), app.nudge(1, 0)))
    elif "pointed" in result and s["settled"] and "marked" not in result and time.time() - t0 > 8:
        result["marked"] = True
        app.do_mark()
        root.after(600, lambda: shot("2_marked"))
        root.after(900, app.toggle_theme)
        root.after(2000, lambda: (shot("3_light"), result.update(done=True), app.close()))
        return
    if time.time() - t0 > 240:
        app.close()
        return
    root.after(500, step)


root.after(1000, step)
root.mainloop()
check(result.get("marked") and app.mark is not None, "window pointed, settled and marked")
if app.mark:
    print("      flags:", app.flags())
    check("-0.0" not in app.flags(), "no -0.0 in the flags")
check(result.get("done"), "light theme drawn")
for line in app.log:
    print("     ", *line)

# 9. the GPS, a pretend one on a pty. It sits at the Royal Observatory, Greenwich.
GREENWICH = "5128.6740,N,00000.0900,W"


def nmea(body):
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"${body}*{cs:02X}\r\n".encode()


def gga(quality=1):
    pos = GREENWICH if quality else ",,,"
    return nmea(f"GNGGA,{sm.utcnow():%H%M%S}.000,{pos},{quality},9,1.0,46.0,M,47.0,M,,")


def rmc(status="A", late_s=0):
    t = sm.utcnow() - timedelta(seconds=late_s)
    pos = GREENWICH if status == "A" else ",,,"
    return nmea(f"GNRMC,{t:%H%M%S}.000,{status},{pos},0.10,0.00,{t:%d%m%y},,,A")


class FakeGps:
    """A pty that says whatever talk() returns, five times a second."""

    def __init__(self, talk):
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        os.set_blocking(self.master, False)
        self.path = os.ttyname(self.slave)
        self.talk, self.on = talk, True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while self.on:
            try:
                os.write(self.master, self.talk())
            except OSError:
                pass                           # nobody reading and the buffer's full
            time.sleep(0.2)

    def close(self):
        self.on = False
        time.sleep(0.3)
        os.close(self.master)
        os.close(self.slave)


def fix_from(talk, **kw):
    """(the fix, or None, and what GpsError said, or "")"""
    gps = FakeGps(talk)
    try:
        return sm.take_fix(gps.path, **kw), ""
    except sm.GpsError as e:
        return None, str(e)
    finally:
        gps.close()


check(sm.latlon_to_grid(51.4779, -0.0015) == "IO91XL", "grid from a position")
check(sm.latlon_to_grid(-33.8688, 151.2093) == "QF56OD", "grid from a position, south and east")

fix, err = fix_from(lambda: b"# a status line\r\n" + gga() + rmc())
print("      fix:", fix, err)
check(bool(fix) and abs(fix["lat"] - 51.4779) < 1e-4 and abs(fix["lon"] + 0.0015) < 1e-4,
      "position, west of Greenwich is negative")
check(bool(fix) and fix["grid"] == "IO91XL" and fix["alt_m"] == 46 and fix["sats"] == 9,
      "grid, height and satellites")
check(bool(fix) and abs(fix["clock_s"]) < 5 and sm.clock_warning(fix["clock_s"]) == "", "clock agrees, no warning")

fix, err = fix_from(lambda: rmc() + gga())
check(bool(fix), "RMC before GGA is a fix too")

fix, err = fix_from(lambda: gga() + rmc("A", late_s=120))
check(bool(fix) and 110 < fix["clock_s"] < 130, "clock 120 s out is spotted")
check("120 s fast" in sm.clock_warning(120) and "0.50 deg" in sm.clock_warning(120), "and warned about")

bad = b"$GNGGA,120000.000,0000.0000,N,00000.0000,E,1,9,1.0,0.0,M,0.0,M,,*00\r\n"
fix, err = fix_from(lambda: bad + gga() + rmc())
check(bool(fix) and fix["lat"] > 51, "a sentence with a bad checksum is ignored")

for what, talk in (("GGA with no fix, RMC void", lambda: gga(0) + rmc("V")),
                   ("GGA fix but RMC void", lambda: gga() + rmc("V")),
                   ("RMC active but GGA no fix", lambda: gga(0) + rmc()),
                   ("GGA fix and no RMC at all", lambda: gga())):
    fix, err = fix_from(talk, no_fix_s=1.5)
    check(fix is None and "no fix" in err, f"{what}: not trusted: {err}")

fix, err = fix_from(lambda: b"", silent_s=1.5)
check(fix is None and "Nothing from" in err, f"silence: {err}")
fix, err = fix_from(lambda: bytes(range(128, 200)), silent_s=1.5)
check(fix is None and "not in NMEA" in err, f"garbage: {err}")
gps = FakeGps(lambda: b"")
stop = threading.Event()
threading.Timer(1.0, stop.set).start()
t0 = time.time()
check(sm.take_fix(gps.path, stop) is None and time.time() - t0 < 3, "calling it off lets go promptly")
gps.close()
try:
    sm.take_fix("/dev/ttyNOPE")
    check(False, "a missing port raises")
except sm.GpsError as e:
    check("There's no" in str(e), f"missing port: {e}")

# the command line finds us and saves it, and leaves the rotator's port alone
gps = FakeGps(lambda: gga() + rmc())
write_cfg(device="/dev/rotator")
r = subprocess.run(CLI + ["gps", "--device", gps.path], capture_output=True, text=True, timeout=60)
print(r.stdout, r.stderr)
with open(sm.CONFIG_PATH) as fh:
    saved = json.load(fh)
check(r.returncode == 0 and abs(saved.get("lat", 0) - 51.4779) < 1e-4, "gps on the command line saves the fix")
check(saved.get("gps_port") == gps.path and saved.get("device") == "/dev/rotator", "and the right ports")

# the program as built asks at startup, and the rotator's connected meanwhile
write_cfg(min_el=min_el, rotctl="", gps_port=gps.path)
p = subprocess.Popen(GUI, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
deadline, lat = time.time() + 30, None
while time.time() < deadline and lat is None:
    time.sleep(1)
    with open(sm.CONFIG_PATH) as fh:
        lat = json.load(fh).get("lat")
shot("4_gps_at_startup")
p.terminate()
print(p.communicate(timeout=15)[0].decode(errors="replace")[-1500:])
check(lat is not None and abs(lat - 51.4779) < 1e-4, "the program took a fix at startup and saved it")

# the window: Settings fills itself in, and the startup fix moves the station
write_cfg(min_el=min_el, gps_port=gps.path)
root = tk.Tk(className="sunmoon")
app = g.App(root)
root.update()


def wait_for(until, seconds):
    end = time.time() + seconds
    while time.time() < end and not until():
        root.update()
        time.sleep(0.05)
    return until()


check(wait_for(lambda: app.gps is None, 15) and abs(app.station.lat - 51.4779) < 1e-4,
      f"window moved to the fix: {app.station.label}")
check(any("location updated" in ln[2] for ln in app.log), "and said so")
check(not any("5128" in ln[2] or "51.47" in ln[2] for ln in app.log), "without putting the position in the log")
d = g.Settings(app)
root.update()
for key in ("grid", "lat", "lon"):
    d.v[key].set("")
d.v["gps_port"].set(gps.path)
d.get_position()
check(wait_for(lambda: d.job is None, 15) and d.v["grid"].get() == "IO91XL" and d.v["lat"].get().startswith("51.477"),
      f"Settings filled in: {d.v['grid'].get()} {d.v['lat'].get()} {d.v['lon'].get()}")
shot("5_gps_settings")
d.destroy()
app.close()
gps.close()

print()
print(f"{len(failures)} failed" if failures else "all passed")
sys.exit(1 if failures else 0)
