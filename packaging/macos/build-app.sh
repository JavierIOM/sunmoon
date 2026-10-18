#!/bin/sh
# Builds SunMoon.app, universal (Apple Silicon and Intel), with rotctl built from the
# Hamlib source inside it, and wraps it in SunMoon-<version>-macos.dmg.
#
#   sh packaging/macos/build-app.sh [outdir]       default outdir: dist
#
# Needs macOS with the Xcode command line tools, and python.org's Python 3.13
# (universal2, comes with Tk; set PYTHON to use another). The GitHub workflow
# installs both. Work files go in ~/Library/Caches/sunmoon-build.
#
# Why build rotctl: Homebrew stopped supporting Intel Macs in September 2026, and an
# app shouldn't need Homebrew anyway. Built static and without the optional extras
# (libusb, readline, libxml2), it needs nothing but macOS itself.
set -eu
umask 022

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
out=${1:-$repo/dist}
case "$out" in /*) ;; *) out="$repo/$out" ;; esac
work=${SUNMOON_WORK:-$HOME/Library/Caches/sunmoon-build}
python=${PYTHON:-/Library/Frameworks/Python.framework/Versions/3.13/bin/python3}

hamlib_version=4.7.0
hamlib_sha=24542b09cb2432458ba239b2ba8f5b7fb67cde64df6553f150e6eb8475a87a23
pyinstaller_version=6.22.3
arch_flags="-arch arm64 -arch x86_64 -mmacosx-version-min=11.0"

version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$repo/sunmoon.py")
[ -n "$version" ] || { echo "no __version__ in sunmoon.py" >&2; exit 1; }
mkdir -p "$work" "$out"

# 1. rotctl, both architectures in one binary, kept between builds
tarball="$work/hamlib-$hamlib_version.tar.gz"
bin="$work/hamlib-$hamlib_version-bin"
if [ ! -x "$bin/rotctl" ]; then
    [ -f "$tarball" ] || curl -fsSL -o "$tarball" \
        "https://github.com/Hamlib/Hamlib/releases/download/$hamlib_version/hamlib-$hamlib_version.tar.gz"
    echo "$hamlib_sha  $tarball" | shasum -a 256 -c -
    src="$work/hamlib-$hamlib_version"
    rm -rf "$src"
    tar -xzf "$tarball" -C "$work"
    echo "Building rotctl from Hamlib $hamlib_version, a few minutes"
    (
        cd "$src"
        ./configure --disable-shared --enable-static --disable-dependency-tracking \
            --without-libusb --without-readline --without-xml-support --without-indi \
            --without-cxx-binding --disable-html-matrix \
            CFLAGS="-O2 $arch_flags" LDFLAGS="$arch_flags" > "$work/hamlib-configure.log" 2>&1 \
            || { tail -40 "$work/hamlib-configure.log"; exit 1; }
        make -j"$(sysctl -n hw.ncpu)" > "$work/hamlib-make.log" 2>&1 \
            || { tail -60 "$work/hamlib-make.log"; exit 1; }
    )
    mkdir -p "$bin"
    cp "$src/tests/rotctl" "$bin/rotctl"
fi
lipo -info "$bin/rotctl"
otool -L "$bin/rotctl"

# Hamlib's licence files and where its source is, to go in the app with rotctl (GPL)
licence="$work/hamlib-licence"
rm -rf "$licence"
mkdir -p "$licence"
for f in COPYING COPYING.LIB LICENSE AUTHORS; do
    tar -xzf "$tarball" -C "$licence" --strip-components=1 "hamlib-$hamlib_version/$f"
done
cat > "$licence/SOURCE.txt" <<EOF
SunMoon.app's rotctl is built, unmodified, from the Hamlib $hamlib_version source
(hamlib-$hamlib_version.tar.gz, SHA-256 $hamlib_sha).

Hamlib is free software, not part of SunMoon and not covered by SunMoon's MIT
licence. The library is under the LGPL and the programs under the GPL: see
LICENSE, COPYING and COPYING.LIB here.

Its complete source code, hamlib-$hamlib_version.tar.gz, is attached to every
SunMoon release that includes it:
    https://github.com/JavierIOM/sunmoon/releases
and is also at
    https://github.com/Hamlib/Hamlib/releases/tag/$hamlib_version
EOF

# 2. the app
venv="$work/venv"
[ -x "$venv/bin/python" ] || "$python" -m venv "$venv"
"$venv/bin/python" -m pip install --disable-pip-version-check -q "pyinstaller==$pyinstaller_version"
dist="$work/dist"
"$venv/bin/python" -m PyInstaller --noconfirm --clean --windowed --name SunMoon \
    --target-arch universal2 --osx-bundle-identifier io.github.javieriom.sunmoon \
    --icon "$repo/assets/sunmoon.icns" \
    --add-data "$repo/assets/sunmoon.png:assets" \
    --add-binary "$bin/rotctl:hamlib" \
    --add-data "$licence:hamlib-licence" \
    --distpath "$dist" --workpath "$work/build" --specpath "$work" \
    "$repo/sunmoon_gui.py"

app="$dist/SunMoon.app"
plist="$app/Contents/Info.plist"
plutil -replace CFBundleShortVersionString -string "$version" "$plist"
plutil -replace CFBundleVersion -string "$version" "$plist"
plutil -replace LSMinimumSystemVersion -string "11.0" "$plist"
plutil -replace NSHumanReadableCopyright -string "SunMoon: MIT licence. rotctl: Hamlib, GPL and LGPL." "$plist"
# changing Info.plist breaks the signature, so sign again: ad hoc, there's no Apple
# Developer ID, which is why Gatekeeper asks the first time
codesign --force --deep --sign - "$app"
codesign --verify --deep --strict "$app"
echo "Built $app"

# 3. the disk image: the app, and a link to Applications to drag it onto
stage="$work/dmg"
rm -rf "$stage"
mkdir -p "$stage"
ditto "$app" "$stage/SunMoon.app"
ln -s /Applications "$stage/Applications"
dmg="$out/SunMoon-$version-macos.dmg"
rm -f "$dmg"
hdiutil create -volname "SunMoon $version" -srcfolder "$stage" -ov -format UDZO "$dmg"
echo "Built $dmg"
