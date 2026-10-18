# SunMoon

**Easy calibration of your rotator using the sun or moon.**

SunMoon points your antenna at the sun or moon through hamlib, keeps it tracking, and lets you
nudge it with the arrow keys until it's dead on. Mark the reading and it tells you how far out
your rotator is, plus the `rotctl` flags that correct it.

Why the sun and moon? They're the only things in the sky whose position you can know to a
fraction of a degree, from anywhere, on any day, with no other kit. If your rotator says 180
and the sun is at 183, your rotator is 3 degrees out.

![SunMoon after marking a reading](docs/screenshot.png)

## Download

Everything is on the [latest release](https://github.com/JavierIOM/sunmoon/releases/latest).
You'll need a rotator and controller that hamlib supports: Yaesu GS-232A/B, SPID, Easycomm and
[plenty more](https://github.com/Hamlib/Hamlib/wiki/Supported-Rotators), or a rotctld already
running.

### Windows

1. Grab `SunMoon-<version>-win64.zip`.
2. Unzip it anywhere and run `SunMoon.exe`. Nothing to install. Windows 10 or 11, 64-bit.

hamlib comes in the zip. SunMoon drives your rotator through hamlib's `rotctl.exe`, and a copy of
it (version 4.7.0) sits in the `hamlib-w64-4.7.0` folder next to `SunMoon.exe`. If you've
installed a newer hamlib yourself, SunMoon finds that and uses it instead.

**Windows will probably warn you the first time** ("Windows protected your PC"), because the exe
isn't code-signed. Click **More info**, then **Run anyway**. That's normal for hobby software
off GitHub.

### Linux

For Debian, Ubuntu, Mint and Raspberry Pi OS, on a PC or a Pi alike. Grab
`sunmoon_<version>_all.deb`, then:

```
sudo apt install ./sunmoon_<version>_all.deb
```

apt installs hamlib (`libhamlib-utils`) and Tk along with it. SunMoon is then in your
applications menu, or run `sunmoon`.

Linux only lets members of the `dialout` group open serial ports. If SunMoon says so, run
`sudo usermod -aG dialout $USER`, then log out and back in. Raspberry Pi OS's first user is
already in it.

Other distros: install hamlib and Tk from your package manager (Fedora:
`sudo dnf install hamlib python3-tkinter`, Arch: `sudo pacman -S hamlib tk`), clone this repo
and run `python3 sunmoon_gui.py`.

The Linux version is tested on Ubuntu 22.04 and 24.04, on Intel and ARM, against hamlib's
simulator. Nobody has driven a real rotator with it yet, so tell us how you get on.

## First run

SunMoon asks for:

- **Your location:** your grid locator, or latitude and longitude if you want it exact.
- **Your rotator:** its hamlib model, the serial port (the list shows the chip behind each port,
  FTDI or CP210x, so you don't pick your radio by mistake) and the baud rate. On Linux the
  port is saved by its `/dev/serial/by-id` name, which stays put when `ttyUSB0` and `ttyUSB1`
  swap round at the next boot.

Close anything else that drives the rotator first (PstRotator, HRD Rotator, OscarWatch, Gpredict,
a rotctld). On Windows only one program can hold a serial port, and SunMoon will tell you if
something else has it. Linux lets two programs open it at once, and they garble each other's
commands, so close the other one. **Disconnect** hands the port back without closing SunMoon.

Want to try it without moving anything? Pick model **1 Simulator (hamlib dummy)**.

## Calibrating

1. Wait for a clear day with the sun well above the trees, and press **Point at the sun**. It
   slews there and keeps tracking.
2. Check it's really on the sun:
   - **By eye:** the boom's shadow shrinks to a dot, or the elements' shadows line up on the boom.
   - **By ear:** sun noise peaks on the receiver. Faint on 2 m, much easier on 70 cm and up.
3. Nudge with the arrow keys until it's dead on. If small nudges don't shift it, pick a bigger
   step: some controllers ignore moves of a degree or two.
4. Wait for **SETTLED**, then press **Space** (or **Mark reading**).
5. The result panel shows the offset and the flags, e.g. `-o +1.9 -O -1.9`. **Copy flags** puts
   them on the clipboard.

The offset is the rotator's reading minus where the sun really is. hamlib adds `-o`/`-O` to every
position it sends and subtracts them from every reading (see `src/rotator.c`), so the flags are
the right way round for `rotctl` and `rotctld`. Other programs' offset settings may work the
other way round, so do one test move before trusting them.

The moon works the same way. It's too faint to hear on most VHF setups, so it's a by-eye check,
or strap a camera to the boom.

| Key | Does |
| --- | --- |
| Arrow keys | Nudge azimuth and elevation |
| Space | Mark the reading, once it says SETTLED |
| Esc | Stop the rotator and stop tracking |

## Settings

All in the **Settings** window.

| Setting | What it's for |
| --- | --- |
| Grid locator, or latitude and longitude | Where you are. Lat/lon beats the middle of the grid square |
| Lowest elevation | It won't point below this. Set it above your trees and roofs |
| Deadband | Only re-commands once the target has moved this far, to spare the motors |
| Refraction | Lifts elevation for the atmosphere: half a degree at the horizon, nothing above 10 degrees |
| Model | hamlib rotator model. Any number `rotctl -l` lists. For a running rotctld use model 2 and port `127.0.0.1:4533` |
| Azimuth-only | Elevation is sent as 0 and the up and down nudges go away |
| rotctl | Found automatically. Windows: the one in the zip, or a newer hamlib you've installed. Linux: the distro's. Browse to pick a different one |

On Windows they're saved to `sunmoon.json` next to `SunMoon.exe`, or to `%APPDATA%\SunMoon` if
that folder is read-only (say you unzipped it into Program Files). On Linux they're in
`~/.config/sunmoon/sunmoon.json`.

## Accuracy

Sun and moon positions use Paul Schlyter's method with the main lunar perturbations, plus the
moon's parallax (up to a degree, the term that matters). Checked against PyEphem at 4,000 random
times across 2025 to 2027:

| | Mean error | Worst |
| --- | --- | --- |
| Sun | 0.008 deg | 0.014 deg |
| Moon | 0.03 deg | 0.10 deg |

Rise and set times agree with PyEphem to within 2 seconds, and the moon's lit fraction to within
0.3%. Any rotator reads back far coarser than this.

## hamlib quirks it works around

- **`rotctl -` is broken in the Windows 4.7.0 build.** It reads stray NUL commands and ignores
  the `+` prefix. SunMoon runs rotctl in plain interactive mode over a pipe instead, and waits
  for the `Rotator command:` prompt that ends every reply.
- **The Yaesu GS-232B's move-then-read race.** hamlib sends `W183 009` plus a bare CR, and the
  controller answers with a CR that sometimes lands after hamlib's input flush. A position read
  straight after a move then gets one byte and fails with `Protocol error`. Measured: 9 reads in
  20 fail straight after a move, none with a half-second wait. SunMoon waits half a second after
  each move and retries a failed read.
- **Azimuth as -180 to 180.** Most rotators take 0 to 360 or more, but some are set up for -180
  to 180, and so is hamlib 4.3's simulator. Ask one for 182 and hamlib refuses with
  `range problem`. SunMoon sends the same direction a turn the other way, -178.
- **hamlib 4.3's serial errors** (Ubuntu 22.04) only say `serial_open return(-6)`, whether the
  port is missing or you're not in `dialout`. SunMoon checks the port itself first and tells you
  which.

## Building from source

Windows:

```
pwsh -File build.ps1              build, install to C:\Radio\sunmoon, make shortcuts, zip a release
pwsh -File build.ps1 -NoInstall   build and zip only
pwsh -File build.ps1 -InstallDir D:\Apps\SunMoon
```

Linux, on any Debian-family box:

```
sh packaging/linux/build-deb.sh   builds dist/sunmoon_<version>_all.deb
```

The [Linux workflow](.github/workflows/linux.yml) builds the `.deb` on every push and tests it
installed on Ubuntu 22.04, 24.04 and 24.04 ARM with `tests/smoke.py`: the rotctl driver
against hamlib's simulator, the error messages, the command line with and without a terminal,
and the window under Xvfb, with screenshots saved as artifacts.

On Windows the first build makes a venv with PyInstaller and Pillow in `%LOCALAPPDATA%\sunmoon-build`, and
downloads the official `hamlib-w64-4.7.0.zip` and `hamlib-4.7.0.tar.gz` there, checking both
against the SHA-256 in `build.ps1`. The program itself is standard-library Python 3: `sunmoon.py`
is the engine (ephemeris, the rotctl driver, settings), `sunmoon_gui.py` is the window, and
`assets/make_icon.py` draws the icon.

Run from source, SunMoon uses whatever hamlib you've installed, since the bundled copy only
goes into the built program. `sunmoon.py` also works on its own from a terminal, using the same
`sunmoon.json` (installed from the `.deb` on Linux, it's `sunmoon where`, `sunmoon sun` and so on):

```
python sunmoon.py where          sun and moon az/el and rise/set, no rotator
python sunmoon.py sun            track the sun, arrow keys nudge, Space marks
python sunmoon.py moon --once    point, wait for it to settle, report, exit
python sunmoon.py sun --dummy    against hamlib's simulator
```

## Licence

SunMoon is MIT, see [LICENSE](LICENSE).

The release zip also contains `rotctl.exe` and its DLLs from [Hamlib](https://github.com/Hamlib/Hamlib)
4.7.0, unmodified. Hamlib is a separate project and isn't covered by SunMoon's licence: the
library is LGPL and the programs are GPL, and their licence files are in the `hamlib-w64-4.7.0`
folder. SunMoon only runs `rotctl.exe` as a separate program and talks to it over a pipe. The
matching Hamlib source, `hamlib-4.7.0.tar.gz`, is attached to every release that ships it.
