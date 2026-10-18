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
rotctl, and the window, and saves screenshots into the folder given.
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
import time

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

print()
print(f"{len(failures)} failed" if failures else "all passed")
sys.exit(1 if failures else 0)
