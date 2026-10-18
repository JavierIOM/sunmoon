#!/usr/bin/python3
"""Linux smoke test for the installed .deb, run by .github/workflows/linux.yml
under xvfb-run. Checks the rotctl driver against hamlib's simulator, what the
errors say, the command line with and without a terminal, and the window, and
saves screenshots into the folder given (default: shots).

    xvfb-run -a -s "-screen 0 1280x1000x24" /usr/bin/python3 tests/linux_smoke.py shots
"""

import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import time

OUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "shots")
os.makedirs(OUT, exist_ok=True)
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()     # sunmoon reads it on import
sys.path.insert(0, "/usr/share/sunmoon")
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
    cfg.update(kw)
    with open(sm.CONFIG_PATH, "w") as fh:
        json.dump(cfg, fh)


def shot(name):
    subprocess.run(["import", "-window", "root", os.path.join(OUT, name + ".png")], check=False)


# 1. where things are
rotctl = sm.find_rotctl("")
check(rotctl == "/usr/bin/rotctl", f"rotctl found at {rotctl!r}")
ver = subprocess.run([rotctl, "--version"], capture_output=True, text=True).stdout
print("     ", ver.splitlines()[0] if ver else "(no version)")
check(sm.CONFIG_PATH.startswith(os.environ["XDG_CONFIG_HOME"]), f"settings at {sm.CONFIG_PATH}")

# whichever is up, so the pointing tests point at something; if neither, allow below
now = sm.utcnow()
els = {b: sm.az_el(b, now, sm.station_from_cfg({"grid": "IO74RE"}), True)[1] for b in ("sun", "moon")}
body = max(els, key=els.get)
min_el = 0.0 if els[body] > 1 else -90.0
print(f"      pointing at the {body}, el {els[body]:.1f}, min_el {min_el}")

# 2. the driver: interactive rotctl over a pipe, against the simulator
rot = sm.Rotator(rotctl, 1, None, None)
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
for dev in ("/dev/ttyNOPE", "/dev/ttyS0"):
    try:
        r = sm.Rotator(rotctl, 603, dev, 9600)
        r.close()
        print(f"      {dev}: opened without complaint")
    except sm.RotatorError as e:
        print(f"      {dev}: rotctl says  {e}")
        print(f"      {dev}: SunMoon says {g.explain(str(e), {'device': dev})}")
        if dev == "/dev/ttyNOPE":
            check("There's no" in g.explain(str(e), {"device": dev}), "missing port explained")
        elif not os.access(dev, os.R_OK | os.W_OK):
            check("dialout" in g.explain(str(e), {"device": dev}), "no permission explained")

# 4. the command line, no terminal
write_cfg(min_el=min_el)
r = subprocess.run(["sunmoon", "where"], capture_output=True, text=True, timeout=60)
print(r.stdout)
check(r.returncode == 0 and "Sun" in r.stdout and "Moon" in r.stdout, "sunmoon where")
r = subprocess.run(["sunmoon", body, "--dummy", "--once"], capture_output=True, text=True, timeout=300)
print(r.stdout[-800:], r.stderr[-800:])
check(r.returncode == 0 and "Rotator read" in r.stdout, f"sunmoon {body} --dummy --once")

# 5. the command line in a real terminal: right arrow nudges, q quits
pid, fd = pty.fork()
if pid == 0:
    os.execvp("sunmoon", ["sunmoon", body, "--dummy"])
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

# 6. the window as installed, first run: no settings yet, setup dialog up
os.remove(sm.CONFIG_PATH)
p = subprocess.Popen(["sunmoon"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
time.sleep(6)
shot("1_first_run")
alive = p.poll() is None
p.terminate()
print(p.communicate(timeout=10)[0].decode(errors="replace"))
check(alive, "window opened and stayed up")
check(not os.path.exists(sm.CONFIG_PATH), "nothing saved before setup")

# 7. the window, pointing, nudged, marked, then the light theme
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
check(result.get("done"), "light theme drawn")
for line in app.log:
    print("     ", *line)

print()
print(f"{len(failures)} failed" if failures else "all passed")
sys.exit(1 if failures else 0)
