#!/bin/sh
# Builds sunmoon_<version>_all.deb for Debian, Ubuntu, Mint and Raspberry Pi OS.
# Needs dpkg-deb, so run it on any Debian-family box (the GitHub workflow does).
# It's pure Python, so one package covers x86 and ARM, and apt pulls in hamlib
# (libhamlib-utils) and Tk (python3-tk) as dependencies.
#
#   sh packaging/linux/build-deb.sh [outdir]     default outdir: dist
set -eu
umask 022

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
out=${1:-$repo/dist}
version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$repo/sunmoon.py")
[ -n "$version" ] || { echo "no __version__ in sunmoon.py" >&2; exit 1; }

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
chmod 755 "$stage"

install -d "$stage/DEBIAN" "$stage/usr/bin" "$stage/usr/share/sunmoon/assets" \
    "$stage/usr/share/applications" "$stage/usr/share/icons/hicolor/256x256/apps" \
    "$stage/usr/share/doc/sunmoon"
install -m 644 "$repo/sunmoon.py" "$repo/sunmoon_gui.py" "$stage/usr/share/sunmoon/"
install -m 644 "$repo/assets/sunmoon.png" "$stage/usr/share/sunmoon/assets/"
install -m 755 "$here/sunmoon" "$stage/usr/bin/sunmoon"
install -m 644 "$here/sunmoon.desktop" "$stage/usr/share/applications/"
install -m 644 "$repo/assets/sunmoon.png" "$stage/usr/share/icons/hicolor/256x256/apps/"
install -m 644 "$repo/LICENSE" "$stage/usr/share/doc/sunmoon/copyright"
gzip -9n -c "$repo/CHANGELOG.md" > "$stage/usr/share/doc/sunmoon/changelog.gz"
chmod 644 "$stage/usr/share/doc/sunmoon/changelog.gz"

size=$(du -sk --exclude=DEBIAN "$stage" | cut -f1)
sed -e "s/@VERSION@/$version/" -e "s/@SIZE@/$size/" "$here/control" > "$stage/DEBIAN/control"

mkdir -p "$out"
dpkg-deb --root-owner-group -Zxz --build "$stage" "$out/sunmoon_${version}_all.deb"
