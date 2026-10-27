#!/bin/sh
# ===================================================================
# Builds the Linux app (beta): release/BBVideoCutter-Linux-x86_64.tar.gz,
# a BBVideoCutter/ folder to unpack anywhere and run
# BBVideoCutter/BBVideoCutter from.
#
# RUN THIS ON LINUX (or in WSL). PyInstaller cannot cross-compile, and
# the build only runs on Linux systems with this one's glibc or newer:
# build on the oldest distro it should run on (Ubuntu 24.04 so far).
#
# Requirements before running (Debian/Ubuntu):
#   sudo apt install python3-tk python3-venv
# and, to run the app: ffmpeg libmpv2 (the build uses the system's
# ffmpeg and libmpv, as the source version does).
#
# What it does:
#   1) Makes a venv for the build (once, in ~/.cache/bbvc-build-venv)
#      and installs/updates pyinstaller, pillow, tkinterdnd2, python-mpv.
#   2) Runs PyInstaller --onedir into a temporary folder:
#      --collect-data=tkinterdnd2 bundles its drag-and-drop library (as
#      on Windows); bbvc_linux.py, which the app only imports on Linux,
#      is named so it goes in, as is PIL._tkinter_finder (Pillow's
#      ImageTk imports it only when it runs, on Linux: the waveforms);
#      the system's libmpv is kept out.
#   3) Packs BBVideoCutter/ into the .tar.gz (executable bits kept, owned
#      by root:root) and deletes the temporary folder.
#
# Bundled when they are in this folder (release/build/), like the
# Windows build's .exe files; all are Linux x86_64 programs:
#   ffmpeg + ffprobe   (otherwise the system's are used)
#   deep-filter        (Medium / Extreme noise reduction: DeepFilterNet's
#                       deep-filter-*-x86_64-unknown-linux-musl, renamed;
#                       otherwise it is looked for on PATH)
# The RNNoise model (Light noise reduction) is always bundled.
# ===================================================================
set -e
cd "$(dirname "$0")"
HERE="$(pwd)"
ROOT="$(cd ../.. && pwd)"
OUT="$ROOT/release/BBVideoCutter-Linux-x86_64.tar.gz"
VENV="${XDG_CACHE_HOME:-$HOME/.cache}/bbvc-build-venv"

echo "[1/3] Build dependencies ($VENV)..."
if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install -q --upgrade pyinstaller pillow tkinterdnd2 python-mpv certifi

set -- --add-data "$ROOT/assets/rnnoise/cb.rnnn:."
if [ -f ffmpeg ] && [ -f ffprobe ]; then
    echo "      including ffmpeg/ffprobe from here"
    set -- "$@" --add-binary "$HERE/ffmpeg:." --add-binary "$HERE/ffprobe:."
else
    echo "      ffmpeg/ffprobe not here: the app will use the system's"
fi
if [ -f deep-filter ]; then
    echo "      including deep-filter from here: Medium / Extreme noise reduction"
    set -- "$@" --add-binary "$HERE/deep-filter:."
else
    echo "      deep-filter not here: the app will look for it on PATH"
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
# PyInstaller sees python-mpv's ctypes.util.find_library("mpv") and would
# bundle the system's libmpv with everything it links (ffmpeg's libraries,
# codecs, ...: ~200 MB). The app loads the system's libmpv, so in this
# build process only, that lookup finds nothing.
NO_LIBMPV='import ctypes.util as u, sys
real = u.find_library
u.find_library = lambda name: None if name == "mpv" else real(name)
from PyInstaller.__main__ import run
run(sys.argv[1:])'
echo "[2/3] Building the app with PyInstaller..."
"$VENV/bin/python" -c "$NO_LIBMPV" \
    --onedir \
    --name BBVideoCutter \
    --collect-data=tkinterdnd2 \
    --hidden-import mpv \
    --hidden-import bbvc_linux \
    --hidden-import PIL._tkinter_finder \
    --exclude-module numpy \
    --exclude-module yaml \
    --distpath "$WORK/dist" \
    --workpath "$WORK/build" \
    --specpath "$WORK" \
    --noconfirm \
    --log-level WARN \
    "$@" \
    "$ROOT/BBVideoCutter.py"

echo "[3/3] Packing $OUT..."
tar -C "$WORK/dist" --owner=0 --group=0 --numeric-owner -czf "$OUT" BBVideoCutter
echo "Done: $OUT ($(du -h "$OUT" | cut -f1))"
