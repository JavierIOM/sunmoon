# Changelog

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
