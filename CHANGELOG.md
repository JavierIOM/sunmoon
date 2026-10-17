# Changelog

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
