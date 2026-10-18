# Changelog

## [1.4.0] - 2026-10-18

macOS.

### Added
- SunMoon.app for macOS 11 and later, one app for Apple Silicon and Intel, in a `.dmg`. rotctl
  is built from the Hamlib source and sits inside the app, so there's nothing else to install,
  Homebrew included.
- Mac touches: settings in `~/Library/Application Support/SunMoon`, `/dev/cu.*` ports, the
  title bar follows the theme, Cmd+Q stops the rotator before quitting, text the size the rest
  of macOS uses.
- "by 2D0PEY" in the title bar.
- README screenshot retaken on 1.4.0, and a macOS section added.
- README: macOS section, and the screenshot retaken on 1.4.0.
- A GitHub workflow that builds the app and tests it on macOS 14 and 15, Apple Silicon and
  Intel, with screenshots. One smoke test, `tests/smoke.py`, now covers Linux and the Mac.

## [1.3.0] - 2026-10-18

Linux.

### Added
- A `.deb` for Debian, Ubuntu, Mint and Raspberry Pi OS, x86 and ARM alike. apt installs hamlib
  and Tk along with it. `sunmoon` opens the window; `sunmoon where`, `sunmoon sun` and the rest
  are the command line.
- Linux ports are listed by their `/dev/serial/by-id` names, which stay put across reboots where
  `ttyUSB0` and `ttyUSB1` can swap, with the chip behind each.
- Plain-English Linux errors, including the dialout group one.
- Arrow keys in the command-line version on Linux too.
- A GitHub workflow that builds the `.deb` and tests it installed on Ubuntu 22.04 and 24.04, x86
  and ARM, with screenshots of the window.

### Changed
- On Linux, settings live in `~/.config/sunmoon`. Windows is unchanged.
- README screenshot retaken on 1.3.0, and a Linux section added.

### Fixed
- Rotators that take azimuth as -180 to 180 (hamlib 4.3's simulator, or any rotator set up
  that way) refused anything past 180 with "range problem". SunMoon now sends the same
  direction a turn the other way, so 182 goes as -178.
- An offset a hair below zero showed as `-0.0`, flags included. Now `+0.0`.
- hamlib 4.3 says only `serial_open return(-6)` for a missing port and a permission problem
  alike, so SunMoon checks `/dev` ports itself first and says which.

## [1.2.0] - 2026-10-18

### Added
- hamlib comes in the zip: rotctl.exe 4.7.0 and its DLLs, unmodified from the official
  hamlib-w64 build, in a `hamlib-w64-4.7.0` folder. Nothing else to install. A newer hamlib
  you've installed yourself still wins.
- The Hamlib source and licence files go out with every release that ships it, as the GPL asks.

### Changed
- Settings says when it's using the hamlib that came with SunMoon.
- The rotctl path is only saved to `sunmoon.json` when you've picked one yourself. The one
  found automatically stays automatic, so moving the folder or installing a newer hamlib just
  works.
- `build.ps1` downloads the official hamlib zip and source once and checks their SHA-256.
- README screenshot retaken on 1.2.0.

## [1.1.0] - 2026-10-17

First public release.

### Added
- First-run setup that asks for your location and rotator, so nothing about one shack is assumed.
- hamlib detection across Program Files, `C:\Radio`, `C:\` and PATH, newest version first, with
  a download link in Settings when it isn't installed.
- Release zip from `build.ps1`, MIT licence, README rewritten for anyone downloading it.

### Changed
- No station or port baked in any more (was IO74RE and COM8). Existing `sunmoon.json` files are
  untouched.
- Settings window is two columns so it fits a 1080p screen, and checks your location before the
  rotator, in the same order as the form.
- Settings save to `%APPDATA%\SunMoon` when the program's folder is read-only.
- "Port in use" names the usual suspects (PstRotator, HRD Rotator, OscarWatch, rotctld).

## [1.0.0] - 2026-10-17

### Added
- SunMoon.exe: a window with live sun and moon positions, rise and set times, and the moon drawn
  in its real phase.
- Point at the sun or moon with one button, track it, nudge with the arrow keys or buttons in
  0.5, 1, 2 or 5 degree steps, and mark a settled reading with Space.
- Result panel with the offset and the matching rotctl `-o`/`-O` flags, plus Copy flags.
- Rotator panel: live az/el readback, MOVING/SETTLED, Connect and Disconnect so the port can be
  handed back to OscarWatch without closing.
- Plain-English errors for a busy port, a missing port, no rotctld, no answer and no rotctl.exe.
- Settings window for model, port (with the chip behind each port), baud, azimuth-only, rotctl
  path, station, lowest elevation, deadband and refraction.
- Dark theme by default, light theme toggle, matching dark title bars.
- Icon, build script with Desktop and Start menu shortcuts.

### Fixed
- GS-232B move-then-read race: a position read straight after a move could catch the
  controller's stray CR and fail with `Protocol error`. Now waits half a second after a move and
  retries a failed read. This crashed the first real run of the script version.
