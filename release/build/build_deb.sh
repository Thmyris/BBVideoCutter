#!/bin/sh
# ===================================================================
# Packs the Linux app (beta) as a Debian/Ubuntu package:
# release/BBVideoCutter-Linux-amd64.deb, installed with
#   sudo apt install ./BBVideoCutter-Linux-amd64.deb
# which also installs what it needs (ffmpeg, libmpv2, ...).
#
# RUN build_linux.sh FIRST, on the same Linux (or WSL): this packs its
# release/BBVideoCutter-Linux-x86_64.tar.gz, and reads the libraries the
# build needs from this system's package database.
#
# Requirements: dpkg-deb (any Debian/Ubuntu), and build_linux.sh's
# build venv (for Pillow, which draws the menu icon from the .ico).
#
# What it installs:
#   /opt/bbvideocutter/                 the app (the .tar.gz's folder)
#   /usr/bin/bbvideocutter              a link to start it
#   /usr/share/applications/bbvideocutter.desktop   the menu entry
#   /usr/share/icons/hicolor/256x256/apps/bbvideocutter.png
# Its version is APP_VERSION from BBVideoCutter.py.
# ===================================================================
set -e
cd "$(dirname "$0")"
HERE="$(pwd)"
ROOT="$(cd ../.. && pwd)"
TARBALL="$ROOT/release/BBVideoCutter-Linux-x86_64.tar.gz"
OUT="$ROOT/release/BBVideoCutter-Linux-amd64.deb"
VENV="${XDG_CACHE_HOME:-$HOME/.cache}/bbvc-build-venv"

if [ ! -f "$TARBALL" ]; then
    echo "$TARBALL is missing: run build_linux.sh first." >&2
    exit 1
fi
if [ ! -x "$VENV/bin/python" ]; then
    echo "$VENV is missing: run build_linux.sh first." >&2
    exit 1
fi
VERSION="$(python3 "$HERE/app_version.py")"

PKG="$(mktemp -d)"
trap 'rm -rf "$PKG"' EXIT
echo "[1/3] Laying out version $VERSION..."
mkdir -p "$PKG/DEBIAN" "$PKG/opt" "$PKG/usr/bin" "$PKG/usr/share/applications" \
    "$PKG/usr/share/icons/hicolor/256x256/apps"
tar -C "$PKG/opt" -xzf "$TARBALL"
mv "$PKG/opt/BBVideoCutter" "$PKG/opt/bbvideocutter"
ln -s /opt/bbvideocutter/BBVideoCutter "$PKG/usr/bin/bbvideocutter"
"$VENV/bin/python" -c 'import sys
from PIL import Image
ico = Image.open(sys.argv[1])
ico.size = max(ico.info["sizes"])
ico.convert("RGBA").resize((256, 256), Image.LANCZOS).save(sys.argv[2])' \
    "$ROOT/assets/barkbark2.ico" "$PKG/usr/share/icons/hicolor/256x256/apps/bbvideocutter.png"
cat > "$PKG/usr/share/applications/bbvideocutter.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=BarkBark Lossless Video Cutter
GenericName=Video Cutter
Comment=Cut, crop, rotate and combine videos without re-encoding
Exec=bbvideocutter
Icon=bbvideocutter
Terminal=false
Categories=AudioVideo;Video;AudioVideoEditing;
StartupWMClass=bbvideocutter
EOF

echo "[2/3] Working out the dependencies..."
# The system libraries the app's programs and libraries load (those it
# doesn't bundle), as the packages that hold them; like dpkg-shlibdeps,
# which needs a full debian/ source tree.
APPDIR="$PKG/opt/bbvideocutter"
LIBPKGS="$(find "$APPDIR" -type f | while read -r f; do
        if head -c 4 "$f" | grep -q 'ELF'; then
            LD_LIBRARY_PATH="$APPDIR/_internal" ldd "$f" 2>/dev/null
        fi
    done | awk '$2 == "=>" && $3 ~ /^\// {print $3}' | grep -v "^$APPDIR/" | sort -u |
    xargs -r readlink -f | sort -u | xargs -r dpkg -S 2>/dev/null |
    cut -d: -f1 | sort -u | grep -vx 'libc6' | tr '\n' ' ')"
# libc6: the build needs this glibc or newer. ffmpeg and libmpv2: the app
# runs the system's ffmpeg/ffprobe and loads its libmpv (the player).
DEPENDS="libc6 (>= $(ldd --version | head -1 | awk '{print $NF}')), ffmpeg, libmpv2"
for p in $LIBPKGS; do
    DEPENDS="$DEPENDS, $p"
done
cat > "$PKG/DEBIAN/control" <<EOF
Package: bbvideocutter
Version: $VERSION
Section: video
Priority: optional
Architecture: amd64
Maintainer: Thmyris <thmyris@windowslive.com>
Installed-Size: $(du -sk "$PKG/opt" "$PKG/usr" | awk '{s += $1} END {print s}')
Depends: $DEPENDS
Homepage: https://github.com/Thmyris/BBVideoCutter
Description: Lossless video cutter (beta Linux build)
 BarkBark Lossless Video Cutter cuts, crops, rotates and combines videos
 without re-encoding them where it can, and mixes and cleans up their
 audio tracks.
EOF
echo "      Depends: $DEPENDS"

echo "[3/3] Packing $OUT..."
dpkg-deb --root-owner-group -Zxz --build "$PKG" "$OUT" >/dev/null
echo "Done: $OUT ($(du -h "$OUT" | cut -f1))"
