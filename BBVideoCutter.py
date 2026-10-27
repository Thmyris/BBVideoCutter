"""
BarkBark Lossless Video Cutter
===============================
A desktop tool for cutting, combining, cropping, and audio-mixing video
while avoiding unnecessary re-encoding wherever ffmpeg stream copy
("-c copy") can do the job instead. Five tabs:

Cut tab
-------
Trims a clip out of a video. Three interchangeable cut modes (also
available on the Bulk Cut tab):
  - Lossless (classic) -- pure stream copy, no re-encoding at all. Can
    only start/stop on a keyframe, so the cut may land up to one keyframe
    interval early -- the fastest option, and the output is guaranteed
    byte-identical to the source.
  - Re-encode edges (smart-cut) -- stream-copies the bulk of the clip and
    re-encodes only the small slivers at the start/end that don't land on
    a keyframe, so the cut lands exactly on the requested millisecond.
    Uses a GPU encoder (NVENC/QSV/AMF) when available, matched to the
    source's own codec (an H.264 source re-encodes to H.264, HEVC to
    HEVC) so the re-encoded edges and the copied middle share one codec
    when joined back together.
  - Edit-list trim -- also zero re-encoding: cuts at the nearest keyframes
    like classic mode, then patches the MP4's own edit-list metadata to
    hide the small extra margin. Exact and lossless everywhere, but only
    as reliable as the player's own edit-list support (some, like VLC,
    honor it less consistently than others) -- experimental.
Start/end times can be typed with millisecond precision (HH:MM:SS.mmm);
dragging a slider handle or clicking a step button always rounds to a whole
second instead, so repeated attempts at "the same second" are
reproducible rather than landing on an invisible, slightly different
sub-second position each time.
The video plays right in the window (libmpv, GPU-decoded): Space or a
click on the video plays / pauses, , and . step one frame, Left / Right
jump 5 s (with Shift 1 s), and I / O set the start / end at the frame on
screen, so that frame is the first / last one in the cut. Clicking or
dragging the slider away from the handles moves the playhead (red);
"Play selection" loops the chosen range. Without libmpv the preview shows
still frames from ffmpeg instead.

Combine tab
-----------
Joins multiple video files together, also without re-encoding, as long as
they share the same codec/format (ffmpeg reports an error if they don't --
a hard technical limit of stream-copy joining, not a bug here). Goes
through an MPEG-TS intermediate step rather than feeding the files
straight into an MP4 concat, to avoid a well-known bug where playback
freezes on a frame while audio keeps playing after the first file ends.
The list stays editable while combining: files removed before their turn
are skipped, files added mid-run are included.

Bulk Cut tab
------------
Batch version of the Cut tab: drop in files whose names contain one or
more cut ranges (e.g. "clip [00.00.00-00.00.17].mp4") and cut them all in
one pass, using whichever of the three cut modes is selected. A bracket
like [-43.-51] means second 43 to 51 of the last minute that has them
(19:43-19:51 in a 20:10 video), and [-27] means from second 27 of the
last minute that has it to the end (19:27-20:10 in a 20:10 video), so
these are resolved from the video's length when the file is added.
Works as a
live queue: rows added mid-run are picked up, rows removed before their
turn are never processed. A file with several ranges produces
name.CUT01.(<range>).ext, name.CUT02.(<range>).ext, and so on.

Crop tab
--------
Visual crop editor with aspect-ratio presets -- widescreen (16:9, 16:10,
21:9, 4:3, 1:1) and their vertical/portrait mirrors (9:16, 10:16, 9:21,
3:4) for Shorts/Reels/TikTok-style crops. Dragging a corner keeps the
selected ratio (the source's own ratio by default); Freeform lets it
distort, and holding Shift/Ctrl/Alt in Freeform keeps the box's shape.
Arbitrary spatial cropping can't be done with stream copy, so this tab
always re-encodes the video (GPU-accelerated when available) while
copying the audio through untouched.

Audio tab
---------
Lets you enable/mute individual audio tracks in a file and mix the
enabled ones down together into one track. The video stream is always
stream-copied (untouched, lossless); only the audio is re-encoded, since
mixing multiple tracks into one requires decoding them first.

Every tab that saves a file has a "Save to current folder" checkbox that
skips the save dialog and writes the output right next to the source. If
that file already exists, the app asks whether to overwrite it or keep
both (turn this off under Options). Long jobs show a percentage, a
progress bar and a time-left estimate on the status bar. Dragging an empty drop area (a preview with no video loaded,
the blank part of the Combine or Bulk Cut list) moves the whole window.
Options > Dark mode switches the whole app (title bar included on Windows)
to a dark theme; the choice is remembered in
%APPDATA%\\BBVideoCutter\\settings.json.

Requirements
------------
  - Python 3.8+
  - ffmpeg + ffprobe on your system PATH
      Windows: https://www.gyan.dev/ffmpeg/builds/  (unzip, add /bin to PATH)
      macOS:   brew install ffmpeg
      Linux:   sudo apt install ffmpeg
  - pip install pillow tkinterdnd2 python-mpv

    pillow        -> shows a preview frame of the video
    tkinterdnd2   -> required for drag & drop of video files onto the window
    python-mpv    -> optional: the video player in the Cut tab, together
                     with libmpv-2.dll next to the app or in release\\build
                     (the "mpv-dev-x86_64" download from
                     https://github.com/shinchiro/mpv-winbuild-cmake/releases)

Run:
    python BBVideoCutter.py
"""

import base64
import ctypes
import io
import json
import math
import os
import re
import struct
import subprocess
import shutil
import sys
import tempfile
import threading
import time
import zipfile
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import ttk, filedialog, messagebox, font as tkfont

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
except ImportError as e:
    raise SystemExit(
        "This app requires the 'tkinterdnd2' package for drag & drop.\n"
        "Install it with:\n\n    pip install tkinterdnd2\n\n"
        f"(original error: {e})"
    )

_BaseTk = TkinterDnD.Tk

# The app's version. The installer gets it from here (via
# release\build\app_version.py), and a GitHub release of it is tagged "v" + this.
APP_VERSION = "1.1"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

TIME_RE = re.compile(r"^(\d{1,2}):([0-5]\d):([0-5]\d)$")
TIME_MS_RE = re.compile(r"^(\d{1,2}):([0-5]\d):([0-5]\d)(?:\.(\d{1,3}))?$")
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".wmv", ".webm", ".m4v", ".ts")


def seconds_to_hms(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def hms_to_seconds(hms: str) -> float:
    m = TIME_RE.match(hms.strip())
    if not m:
        raise ValueError(f"Time must look like HH:MM:SS, got: {hms!r}")
    h, mnt, s = (int(x) for x in m.groups())
    return h * 3600 + mnt * 60 + s


def hms_ms_to_seconds(hms: str) -> float:
    """Like hms_to_seconds, but also accepts an optional .mmm millisecond
    suffix (1-3 digits, right-padded -- ".5" means 500ms, not 5ms) -- for
    the Cut tab's start/end entries, which display and accept millisecond
    precision so a typed time is never silently rounded away."""
    m = TIME_MS_RE.match(hms.strip())
    if not m:
        raise ValueError(f"Time must look like HH:MM:SS or HH:MM:SS.mmm, got: {hms!r}")
    h, mnt, s, ms = m.groups()
    ms = (ms or "0").ljust(3, "0")
    return int(h) * 3600 + int(mnt) * 60 + int(s) + int(ms) / 1000.0


def seconds_to_ffmpeg_time(seconds: float) -> str:
    """HH:MM:SS.mmm, millisecond precision. Unlike seconds_to_hms (whole
    seconds only -- fine for the on-screen time fields), this is what gets
    fed to ffmpeg for actual cut boundaries, so a slider-dragged sub-second
    position isn't silently rounded away before frame-accurate cutting even
    gets a chance to work with it."""
    seconds = max(0.0, seconds)
    whole = int(seconds)
    ms = int(round((seconds - whole) * 1000))
    if ms >= 1000:
        whole += 1
        ms -= 1000
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def seconds_to_ffmpeg_time_keyframe_ss(seconds: float) -> str:
    """Like seconds_to_ffmpeg_time, but rounds the millisecond value UP
    instead of to the nearest ms. Use this -- never the round-to-nearest
    version -- for an -ss value that must land exactly ON a timestamp
    already known to be a real keyframe (from a keyframe probe), when the
    ffmpeg command has no re-encode to fall back on (-c copy).

    Rounding to nearest can round a keyframe's true timestamp DOWN by a
    fraction of a millisecond (e.g. probed pts 100.117456 -> "100.117").
    With -c copy there's no accurate-seek decode to correct for that --
    ffmpeg's demuxer does a plain "seek to the keyframe at or before this
    timestamp", and since 100.117 is technically before the real keyframe
    at 100.117456, it silently lands on the *previous* keyframe instead,
    pulling in an entire extra GOP (confirmed: ~0.5s) of unwanted footage
    before the intended start. Rounding up instead guarantees the target
    is never less than the true keyframe timestamp, so the seek lands on
    the intended keyframe -- the next real keyframe is always a whole GOP
    (hundreds of ms) later, so this can never overshoot onto it."""
    seconds = max(0.0, seconds)
    whole = int(seconds)
    ms = math.ceil(round((seconds - whole) * 1000, 6))
    if ms >= 1000:
        whole += 1
        ms -= 1000
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


# Cut times are whole milliseconds, frame times generally aren't: a keyframe
# at 2.333333 s typed as 2.333 is still taken to be that keyframe.
CUT_TIME_SLACK = 0.0005

# A re-encoded cut piece is decoded from this many seconds before the
# keyframe it needs (see _ffmpeg_reencode_segment).
CUT_SEEK_PREROLL = 1.0

# Audio copied into an MP4 cut starts this many seconds early, hidden by the
# track's edit list (see _join_cut_pieces).
CUT_AUDIO_LEAD = 0.05


def probe_interval(origin, lo_sec, hi_sec):
    """ffprobe -read_intervals value for lo_sec..hi_sec on ffmpeg -ss's
    clock (origin: the file's start time). An interval from the file's
    start reads from the beginning instead of seeking there: a seek to the
    start of an MPEG-TS file can land after its first keyframe."""
    if lo_sec <= 0:
        return f"%{hi_sec + origin:.6f}"
    return f"{lo_sec + origin:.6f}%{hi_sec + origin:.6f}"


def keep_av_streams_args(input_index):
    """Extra ffmpeg output (appended after the real one) that copies every
    video and audio stream of input input_index into nothing.

    For an MPEG-TS-like input, ffmpeg recomputes the start time from the
    streams in use and, if that differs from the file's start time (only
    the video is read, and the audio starts first), changes what -ss
    counts from. Builds before 2024 or so get that wrong (a misplaced
    parenthesis drops the file's start time), and the cut lands up to
    that offset away. With every video and audio stream in use, the start
    time doesn't change and -ss means the same on every build. The cut
    also never seeks to less than _probe_av_offset, which keeps current
    builds right should a subtitle or data stream start first."""
    return ["-map", f"{input_index}:v?", "-map", f"{input_index}:a?",
            "-c", "copy", "-f", "null", "-"]


# --------------------------------------------------------------------------
# Minimal MP4 box reader/patcher, used only by the "edit-list trim" cut mode.
#
# This never resizes or inserts a box -- only overwrites existing fixed-width
# integer fields (mvhd/tkhd duration, the video track's last edit-list entry)
# in place, at their exact original file offset. That's what keeps it safe:
# no box's size changes, so nothing downstream of it (chunk offset tables,
# sibling boxes, anything) needs to move. If the file doesn't already have
# the box shape this expects (e.g. the video track has no edit list at all,
# which would require inserting one -- a real "resize the file" operation
# this deliberately does not attempt), the caller is expected to fall back
# to the smart-cut re-encode path instead.
# --------------------------------------------------------------------------

def _read_box_header(f):
    pos = f.tell()
    header = f.read(8)
    if len(header) < 8:
        return None
    size, boxtype = struct.unpack(">I4s", header)
    boxtype = boxtype.decode("latin1")
    header_len = 8
    if size == 1:
        size = struct.unpack(">Q", f.read(8))[0]
        header_len = 16
    return pos, size, boxtype, header_len


def parse_mp4_edit_structure(path):
    """Read-only walk of an MP4's box tree. Returns:
      {
        "mvhd": (file_offset, version) or None,
        "tracks": [
          {
            "is_video": bool,
            "timescale": int,           # mdia/mdhd timescale for this track
            "tkhd": (file_offset, version),
            "elst_version": int or None,
            "elst_entries": [(seg_duration_offset, media_time_offset,
                               seg_duration, media_time), ...],
          }, ...
        ],
      }
    All offsets point at the start of the (version+flags-prefixed) duration
    field itself, ready for struct.pack to overwrite in place.
    """
    result = {"mvhd": None, "movie_timescale": None, "tracks": []}

    def read_full_box_version(f):
        version = f.read(1)[0]
        f.read(3)  # flags
        return version

    def walk(f, end, track_state=None):
        while f.tell() < end:
            box = _read_box_header(f)
            if box is None:
                break
            pos, size, boxtype, header_len = box
            body_start = pos + header_len
            body_end = pos + size if size > 0 else end

            if boxtype == "moov":
                walk(f, body_end)
            elif boxtype == "mvhd":
                f.seek(body_start)
                version = read_full_box_version(f)
                if version == 1:
                    f.read(16)  # creation_time(8) + modification_time(8)
                else:
                    f.read(8)   # creation_time(4) + modification_time(4)
                timescale = struct.unpack(">I", f.read(4))[0]
                result["mvhd"] = (f.tell(), version)
                result["movie_timescale"] = timescale
            elif boxtype == "trak":
                state = {
                    "is_video": False, "timescale": None, "tkhd": None,
                    "elst_version": None, "elst_entries": [],
                }
                walk(f, body_end, track_state=state)
                if state["tkhd"] is not None:
                    result["tracks"].append(state)
            elif boxtype in ("mdia", "minf", "stbl", "edts") and track_state is not None:
                walk(f, body_end, track_state=track_state)
            elif boxtype == "tkhd" and track_state is not None:
                f.seek(body_start)
                version = read_full_box_version(f)
                # duration field follows creation_time + modification_time +
                # track_ID + reserved: each of the first two is 4 bytes (8
                # for version==1), track_ID and reserved are always 4 bytes.
                skip = 24 if version == 1 else 16
                f.read(skip)
                track_state["tkhd"] = (f.tell(), version)
            elif boxtype == "mdhd" and track_state is not None:
                f.seek(body_start)
                version = read_full_box_version(f)
                f.read(16 if version == 1 else 8)
                track_state["timescale"] = struct.unpack(">I", f.read(4))[0]
            elif boxtype == "vmhd" and track_state is not None:
                track_state["is_video"] = True
            elif boxtype == "elst" and track_state is not None:
                f.seek(body_start)
                version = read_full_box_version(f)
                entry_count = struct.unpack(">I", f.read(4))[0]
                entries = []
                for _ in range(entry_count):
                    seg_off = f.tell()
                    if version == 1:
                        seg_dur, media_time = struct.unpack(">Qq", f.read(16))
                        media_off = seg_off + 8
                    else:
                        seg_dur, media_time = struct.unpack(">Ii", f.read(8))
                        media_off = seg_off + 4
                    f.read(4)  # rate
                    entries.append((seg_off, media_off, seg_dur, media_time))
                track_state["elst_version"] = version
                track_state["elst_entries"] = entries

            f.seek(body_end)

    with open(path, "rb") as f:
        f.seek(0, 2)
        file_end = f.tell()
        f.seek(0)
        walk(f, file_end)

    return result


def _write_duration_field(f, offset, version, value):
    f.seek(offset)
    f.write(struct.pack(">Q" if version == 1 else ">I", value))


def patch_video_edit_list(path, start_sec, end_sec, video_wide_start_sec, structure):
    """Rewrite (in place, same box sizes throughout) the video track's edit
    list plus every track's/movie's duration field so the whole file agrees
    the playable span is exactly [start_sec, end_sec) -- without touching a
    single video/audio sample byte. Raises ValueError if the file doesn't
    have the box shape this can safely patch (caller should fall back)."""
    mvhd = structure["mvhd"]
    if mvhd is None:
        raise ValueError("no mvhd box found")
    mvhd_offset, mvhd_version = mvhd
    # movie timescale itself isn't re-read here -- caller already has it
    # from a plain ffprobe call, passed in via `structure["movie_timescale"]"
    movie_timescale = structure["movie_timescale"]

    video_tracks = [t for t in structure["tracks"] if t["is_video"]]
    if len(video_tracks) != 1:
        raise ValueError(f"expected exactly 1 video track, found {len(video_tracks)}")
    video_track = video_tracks[0]
    if not video_track["elst_entries"]:
        raise ValueError("video track has no edit list to patch")

    new_total = round((end_sec - start_sec) * movie_timescale)
    if new_total <= 0:
        raise ValueError("computed non-positive total duration")

    entries = video_track["elst_entries"]
    earlier_total = sum(e[2] for e in entries[:-1])
    new_last_seg_dur = new_total - earlier_total
    if new_last_seg_dur <= 0:
        raise ValueError("earlier edit-list entries already exceed the new total duration")
    new_media_time = round((start_sec - video_wide_start_sec) * video_track["timescale"])
    if new_media_time < 0:
        raise ValueError("computed negative media_time")

    with open(path, "r+b") as f:
        _write_duration_field(f, mvhd_offset, mvhd_version, new_total)
        for track in structure["tracks"]:
            tkhd_offset, tkhd_version = track["tkhd"]
            _write_duration_field(f, tkhd_offset, tkhd_version, new_total)

        seg_off, media_off, _old_seg, _old_media = entries[-1]
        f.seek(seg_off)
        if video_track["elst_version"] == 1:
            f.write(struct.pack(">Qq", new_last_seg_dur, new_media_time))
        else:
            f.write(struct.pack(">Ii", new_last_seg_dur, new_media_time))


def format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}"
        size /= 1024


def format_duration(seconds: float) -> str:
    """A short, human time span for time-left estimates: "45s", "3m 05s",
    "1h 12m"."""
    seconds = max(1, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _app_dir() -> str:
    """Directory the running app lives in -- the exe's own folder when
    frozen (PyInstaller), or this script's folder otherwise. Used so a
    copy of ffmpeg.exe/ffprobe.exe placed right next to the app (or in an
    "ffmpeg"/"ffmpeg\\bin" subfolder beside it) works with no PATH setup
    at all, matching how build_exe.bat can optionally bundle them in."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def find_exe(name: str) -> str:
    exe_name = name + ".exe" if os.name == "nt" else name
    app_dir = _app_dir()
    search_dirs = [app_dir, os.path.join(app_dir, "ffmpeg"), os.path.join(app_dir, "ffmpeg", "bin")]
    # A --onefile PyInstaller exe extracts anything added with --add-binary
    # into a temp folder (sys._MEIPASS) at runtime, not next to the exe
    # itself -- check there too, so a build that bundles ffmpeg.exe/
    # ffprobe.exe straight into the exe (see build_exe.bat's optional
    # block) is found without needing them dropped alongside it as well.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        search_dirs.append(meipass)
    for folder in search_dirs:
        candidate = os.path.join(folder, exe_name)
        if os.path.isfile(candidate):
            return candidate

    path = shutil.which(name)
    if not path:
        raise FileNotFoundError(
            f"'{name}' was not found next to the app or on your system PATH. "
            f"Install ffmpeg (which includes ffprobe) and try again."
        )
    return path


# The Audio tab's noise reduction, per track: (key, name). Light is RNNoise
# (ffmpeg's arnndn filter with the model file below), applied as the video
# plays and in Finalize. Medium and Extreme are DeepFilterNet
# (deep-filter.exe), which can't run in a filter graph: each track is
# cleaned to a WAV in the background first, then played and saved from
# that.
DENOISE_LEVELS = (
    ("off", "Off"),
    ("light", "Light filtering"),
    ("medium", "Medium filtering"),
    ("extreme", "Extreme filtering"),
)
# Both engines silence the noise between words and let it through with the
# voice, so at full strength the background cuts in and out with every
# word. Light and Medium blend some of the original back in, which caps how
# far it can drop and so how much it jumps.
# deep-filter's -a, the most it may lower the noise by (dB): Medium keeps
# some of the background; Extreme takes it all (deep-filter's own default,
# 100 dB).
DFN_ATTEN_LIMIT_DB = {"medium": 9, "extreme": None}
RNNOISE_MODEL = "cb.rnnn"   # GregorR/rnnoise-models' general-purpose model
# arnndn's mix for Light: the share of the filtered audio, the rest the
# original (0.5: the noise drops by at most 6 dB).
RNNOISE_MIX = 0.5
# arnndn's output comes 10 ms late (480 samples at its 48 kHz). ffmpeg
# (Finalize, the ffplay mix) drops those first 10 ms and moves the rest back
# (RNNOISE_TRIM; a timestamp shift alone is ignored by amix and the
# encoders). In the player that trim does nothing (its filter graph's
# timestamps don't start at 0), so there the other tracks are delayed as
# much and the whole mix plays that much earlier (_audio_apply_mix).
RNNOISE_DELAY = 0.01
RNNOISE_TRIM = f"atrim=start={RNNOISE_DELAY},asetpts=PTS-{RNNOISE_DELAY}/TB"
DEEP_FILTER_EXE = "deep-filter.exe"
# A track's Mono: each ear gets the average of its channels, (L+R)/2 for
# stereo; a sound in the middle keeps its level, one on one side is 6 dB
# quieter. Float first: from an integer format the downmix is scaled to fit
# and comes out 3 dB quieter.
TRACK_MONO = "aformat=sample_fmts=fltp:channel_layouts=mono,aformat=channel_layouts=stereo"
# deep-filter's run time, as a fraction of the track's length: a base plus
# a part per channel (measured on a Ryzen 7 7800X3D; each run corrects
# the next estimate by how the last one went).
DFN_TIME_BASE, DFN_TIME_PER_CHANNEL = 0.015, 0.033


def find_denoise_file(name):
    """The noise reduction file name (RNNOISE_MODEL or DEEP_FILTER_EXE), or
    None: next to the app, inside a PyInstaller bundle, in assets\\rnnoise
    (the model, in a source checkout), in release\\build (deep-filter.exe,
    where the build scripts take it from), or on PATH."""
    app = _app_dir()
    dirs = [app]
    if getattr(sys, "_MEIPASS", None):
        dirs.append(sys._MEIPASS)
    dirs += [os.path.join(app, "assets", "rnnoise"), os.path.join(app, "release", "build")]
    for d in dirs:
        candidate = os.path.join(d, name)
        if os.path.isfile(candidate):
            return candidate
    return shutil.which(name)


def lavfi_value(value):
    """value (a file path, say) written as a filter option inside a filter
    graph, for ffmpeg's -filter_complex and mpv's lavfi-complex alike: its
    \\ ' : escaped for the option, then \\ ' [ ] , ; for the graph."""
    v = value.replace("\\", "/")
    v = re.sub(r"([\\':])", r"\\\1", v)
    return re.sub(r"([\\'\[\],;])", r"\\\1", v)


# On Windows, launching a console-subsystem program (ffmpeg/ffprobe) from a
# process that itself has no console (pythonw.exe, or a PyInstaller
# --windowed exe) makes Windows pop up a brand new console window for that
# child process -- it flashes on screen every single time. CREATE_NO_WINDOW
# tells it not to. This is a no-op dict on macOS/Linux. Every subprocess.run
# call in this file should be given **SUBPROCESS_KWARGS.
if os.name == "nt":
    SUBPROCESS_KWARGS = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    SUBPROCESS_KWARGS = {}


_PROGRESS_TIME_RE = re.compile(r"^out_time_(?:us|ms)=(\d+)$")


def run_ffmpeg(cmd, total_sec=None, progress=None):
    """Run an ffmpeg command, returning a CompletedProcess with text stdout/
    stderr like subprocess.run(capture_output=True, text=True).

    If `progress` is given along with the expected output duration
    (`total_sec`), ffmpeg's machine-readable -progress stream is parsed and
    progress(fraction) is called from this (worker) thread as it runs.
    Output is decoded as UTF-8 with replacement so a filename that isn't
    valid in the system codepage can't crash the read."""
    if progress is None or not total_sec or total_sec <= 0:
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            **SUBPROCESS_KWARGS,
        )

    full_cmd = [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]
    proc = subprocess.Popen(
        full_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", **SUBPROCESS_KWARGS,
    )
    # stderr must be drained concurrently, or ffmpeg blocks once the pipe
    # buffer fills while this thread is busy reading stdout.
    stderr_chunks = []
    drain = threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True)
    drain.start()
    for line in proc.stdout:
        m = _PROGRESS_TIME_RE.match(line.strip())
        if m:
            progress(min(1.0, int(m.group(1)) / 1_000_000 / total_sec))
    proc.wait()
    drain.join()
    return subprocess.CompletedProcess(full_cmd, proc.returncode, "", "".join(stderr_chunks))


def sub_progress(progress, lo, hi):
    """Map a stage's own 0..1 progress onto the lo..hi slice of an overall
    job's progress. Returns None if there's no overall callback."""
    if progress is None:
        return None
    return lambda frac: progress(lo + (hi - lo) * max(0.0, min(1.0, frac)))


def unique_output_path(path: str) -> str:
    """First "name (2).ext", "name (3).ext", ... that doesn't exist yet."""
    root, ext = os.path.splitext(path)
    n = 2
    while True:
        candidate = f"{root} ({n}){ext}"
        if not os.path.exists(candidate):
            return candidate
        n += 1


def probe_duration(ffprobe_exe: str, path: str):
    """Container duration in seconds, or None if it can't be read."""
    try:
        result = subprocess.run(
            [ffprobe_exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30, **SUBPROCESS_KWARGS,
        )
        return float(result.stdout.strip())
    except Exception:
        return None


def _parse_rate(text):
    """"30000/1001" -> 29.97..., or None for "0/0" and the like."""
    try:
        num, _, den = str(text).partition("/")
        value = float(num) / float(den or 1)
        return value if value > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def probe_join_format(ffprobe_exe: str, path: str):
    """What a lossless join needs to be the same in every file: the first
    video stream's codec, shown size (after rotation), pixel format, frame
    rate and rotation, and the first audio stream's codec, sample rate and
    channels. None if the file can't be read."""
    try:
        result = subprocess.run(
            [ffprobe_exe, "-v", "error", "-show_entries",
             "stream=index,codec_type,codec_name,width,height,pix_fmt,avg_frame_rate,"
             "r_frame_rate,sample_aspect_ratio,sample_rate,channels:"
             "stream_side_data=rotation:stream_tags=rotate:format=duration",
             "-of", "json", path],
            capture_output=True, text=True, timeout=30, **SUBPROCESS_KWARGS,
        )
        data = json.loads(result.stdout or "{}")
    except Exception:
        return None
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and s.get("width")), None)
    if video is None:
        return None
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    rotation = 0
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            rotation = int(round(float(side["rotation"])))
    if not rotation and (video.get("tags") or {}).get("rotate"):
        rotation = -int(video["tags"]["rotate"])
    rotation %= 360
    width, height = int(video["width"]), int(video["height"])
    if rotation in (90, 270):
        width, height = height, width
    try:
        duration = float((data.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    rate_text = video.get("avg_frame_rate")
    if not _parse_rate(rate_text):
        rate_text = video.get("r_frame_rate")
    return {
        "vcodec": video.get("codec_name"), "width": width, "height": height,
        "pix_fmt": video.get("pix_fmt"), "rotation": rotation,
        "fps": _parse_rate(rate_text), "fps_text": rate_text if _parse_rate(rate_text) else None,
        "acodec": audio.get("codec_name") if audio else None,
        "sample_rate": int(audio.get("sample_rate") or 0) if audio else None,
        "channels": int(audio.get("channels") or 0) if audio else None,
        # Every audio track's (codec, sample rate, channels), in file order.
        "audio_tracks": [(s.get("codec_name"), int(s.get("sample_rate") or 0),
                          int(s.get("channels") or 0))
                         for s in streams if s.get("codec_type") == "audio"],
        "duration": duration,
    }


def join_format_differences(formats):
    """What keeps these files from a lossless join (names for the user,
    e.g. ["resolution", "frame rate"]); [] if they all match the first one.
    A None entry is a file that couldn't be read."""
    if any(f is None for f in formats):
        return ["unreadable file"]
    first = formats[0]
    # Each audio track is joined to the same track of the next file, so the
    # tracks all files have must match one by one.
    common = min(len(f["audio_tracks"]) for f in formats)

    def tracks(field):
        return lambda f: tuple(t[field] for t in f["audio_tracks"][:common])

    checks = (
        ("video codec", lambda f: f["vcodec"]),
        ("resolution", lambda f: (f["width"], f["height"])),
        ("pixel format", lambda f: f["pix_fmt"]),
        ("rotation", lambda f: f["rotation"]),
        ("audio", lambda f: f["acodec"] is not None),
        ("number of audio tracks", lambda f: len(f["audio_tracks"])),
        ("audio codec", tracks(0)),
        ("audio sample rate", tracks(1)),
        ("audio channels", tracks(2)),
    )
    diffs = [name for name, key in checks if any(key(f) != key(first) for f in formats[1:])]
    if "audio" in diffs:   # some have none: their track count differs too, say it once
        diffs = [d for d in diffs if d == "audio" or "audio" not in d]
    fps = [f["fps"] for f in formats]
    if all(fps) and any(abs(r - fps[0]) > 0.01 * fps[0] for r in fps[1:]):
        diffs.insert(2, "frame rate")
    return diffs


_HW_ENCODER_CACHE = {}


def get_video_encoder_args(ffmpeg_exe: str, source_codec: str = "h264"):
    """Return (cli_args, label) for the best available encoder that
    produces `source_codec` ("h264" or "hevc" -- anything else falls back
    to h264).

    Matching the source's codec matters whenever the result may end up
    concatenated with a stream-copied segment of the original file (smart-
    cut's head/tail re-encodes, joined with a copied middle): mixing two
    different video codecs into one "-c copy" concat silently breaks
    playback of whichever segment doesn't match the codec the muxer/
    demuxer locks onto for the whole track (confirmed: an HEVC source with
    an H.264-forced re-encode produced a file whose entire copied HEVC
    middle became unseekable/undecodable, while the H.264 head/tail played
    fine) -- so a caller that concatenates its result with a stream-copied
    piece of the original must always pass that file's real video codec.

    Prefers a GPU encoder (NVENC, then Quick Sync, then AMF) when ffmpeg
    reports it as built in, falling back to software x264/x265 otherwise.
    Cached per codec family after that family's first `ffmpeg -encoders`
    probe (the probe result is process-lifetime constant).
    """
    global _HW_ENCODER_CACHE
    family = "hevc" if source_codec == "hevc" else "h264"
    if family in _HW_ENCODER_CACHE:
        return _HW_ENCODER_CACHE[family]

    try:
        result = subprocess.run(
            [ffmpeg_exe, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10, **SUBPROCESS_KWARGS,
        )
        available = result.stdout
    except Exception:
        available = ""

    if family == "hevc":
        candidates = [
            ("hevc_nvenc", ["-c:v", "hevc_nvenc", "-preset", "p5", "-rc", "vbr",
                            "-cq", "18", "-b:v", "0"], "NVIDIA NVENC (GPU)"),
            ("hevc_qsv", ["-c:v", "hevc_qsv", "-preset", "medium",
                          "-global_quality", "18"], "Intel Quick Sync (GPU)"),
            ("hevc_amf", ["-c:v", "hevc_amf", "-quality", "quality", "-rc", "cqp",
                          "-qp_i", "18", "-qp_p", "18"], "AMD AMF (GPU)"),
        ]
        software = (["-c:v", "libx265", "-preset", "veryfast", "-crf", "20"], "CPU (libx265)")
    else:
        candidates = [
            ("h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
                            "-cq", "18", "-b:v", "0"], "NVIDIA NVENC (GPU)"),
            ("h264_qsv", ["-c:v", "h264_qsv", "-preset", "medium",
                          "-global_quality", "18"], "Intel Quick Sync (GPU)"),
            ("h264_amf", ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp",
                          "-qp_i", "18", "-qp_p", "18"], "AMD AMF (GPU)"),
        ]
        software = (["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"], "CPU (libx264)")

    for name, args, label in candidates:
        if name in available:
            _HW_ENCODER_CACHE[family] = (args, label)
            return _HW_ENCODER_CACHE[family]

    _HW_ENCODER_CACHE[family] = software
    return _HW_ENCODER_CACHE[family]


def parse_dropped_paths(data: str):
    """tkinterdnd2 gives dropped paths as a single string; entries with
    spaces are wrapped in {curly braces}. Split that out properly."""
    paths = []
    buf = ""
    depth = 0
    for ch in data:
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            continue
        if ch == " " and depth == 0:
            if buf:
                paths.append(buf)
                buf = ""
            continue
        buf += ch
    if buf:
        paths.append(buf)
    return [p for p in paths if p]


def format_mmss(seconds: float) -> str:
    """MM'SS style used in the CUT filename suffix, e.g. 5:30 -> 05'30"""
    seconds = max(0, int(round(seconds)))
    m, s = divmod(seconds, 60)
    return f"{m:02d}'{s:02d}"


def format_clip_suffix(start_sec: float, end_sec: float) -> str:
    return f"{format_mmss(start_sec)}-{format_mmss(end_sec)}"


# --------------------------------------------------------------------------
# Bulk Cut: parsing "[start-end]" ranges out of a dropped file's name
# --------------------------------------------------------------------------

# Matches a bracket like [00.00.00-00.00.17] (HH.MM.SS) or [00.15-15.24] (MM.SS).
# Each side independently may have 2 dot-separated parts (MM.SS) or 3 (HH.MM.SS).
# Accept both normal ranges such as [00.00.17-00.01.23] / [55.39-01.25.27]
# and a small typo such as [55.39-01.25.272]. The parser below normalizes
# overlong seconds fields to the first two digits (272 -> 27).
BULK_BRACKET_RE = re.compile(
    r"\[(\d{1,}(?:\.\d{1,}){1,2})-(\d{1,}(?:\.\d{1,}){1,2})\]"
)


def parse_bracket_time(token: str) -> float:
    """Parse MM.SS or HH.MM.SS.

    A seconds component longer than two digits is treated as a typo and
    truncated to its first two digits, e.g. 01.25.272 -> 01:25:27.
    """
    raw_parts = token.split(".")
    if len(raw_parts) == 3:
        h_s, m_s, s_s = raw_parts
    elif len(raw_parts) == 2:
        h_s = "0"
        m_s, s_s = raw_parts
    else:
        raise ValueError(f"Unrecognized bracket time token: {token!r}")

    if len(m_s) > 2:
        m_s = m_s[:2]
    if len(s_s) > 2:
        s_s = s_s[:2]

    h, mnt, sec = int(h_s), int(m_s), int(s_s)
    if mnt > 59 or sec > 59:
        raise ValueError(f"Invalid minute/second field in {token!r}")
    return h * 3600 + mnt * 60 + sec


# "Last minute" form: [-43.-51] means second 43 to second 51 of the last
# minute in the video that has them, and [-27] means from second 27 of the
# last minute that has it to the end of the video. Which minute that is
# depends on the video's length, so it can only be resolved once the
# duration is known.
BULK_LAST_MINUTE_RE = re.compile(r"\[-(\d{1,2})(?:\.-(\d{1,2}))?\]")


def resolve_last_minute_range(start_s: int, end_s, duration: float):
    """Seconds start_s..end_s of the last minute that has them, as absolute
    (start_sec, end_sec), or None if the video is too short. end_s=None
    means "to the end of the video".

    The end is the latest MM:end_s that is still inside the video; the start
    is the latest MM:start_s before that end. So for a 20:10 video,
    [-43.-51] -> 19:43-19:51 (20:51 would be past the end). A start second
    bigger than the end second wraps back a minute: [-50.-10] -> 19:50-20:10.
    [-27] -> 19:27-20:10 (20:27 would be past the end).
    """
    if end_s is None:
        end_sec = duration
    else:
        end_minute = math.floor((duration - end_s) / 60)
        if end_minute < 0:
            return None
        end_sec = end_minute * 60 + end_s
    # Latest minute whose MM:start_s is still before the end.
    start_minute = math.ceil((end_sec - start_s) / 60) - 1
    if start_minute < 0:
        return None
    return start_minute * 60 + start_s, end_sec


def resolve_last_minute_match(match, duration):
    """resolve_last_minute_range for a BULK_LAST_MINUTE_RE match. None when
    the duration is unknown, a second is over 59, or the video is too short."""
    start_s = int(match.group(1))
    end_s = None if match.group(2) is None else int(match.group(2))
    if duration is None or start_s > 59 or (end_s is not None and end_s > 59):
        return None
    return resolve_last_minute_range(start_s, end_s, duration)


def find_bulk_cut_ranges(filename: str, duration=None):
    """Return every [start-end], [-SS.-SS] and [-SS] range in a filename, in
    filename order. A [-SS.-SS] / [-SS] range needs the video's duration; without
    it (or when the video is too short for it) it comes back as
    (None, None), same as an unreadable bracket."""
    found = []
    for match in BULK_BRACKET_RE.finditer(filename):
        try:
            start_sec = parse_bracket_time(match.group(1))
            end_sec = parse_bracket_time(match.group(2))
            found.append((match.start(), (start_sec, end_sec)))
        except ValueError:
            found.append((match.start(), (None, None)))
    for match in BULK_LAST_MINUTE_RE.finditer(filename):
        resolved = resolve_last_minute_match(match, duration)
        found.append((match.start(), resolved or (None, None)))
    found.sort(key=lambda pair: pair[0])
    return [rng for _pos, rng in found]


def find_bulk_cut_range(filename: str):
    """Backward-compatible helper returning the first bracketed range."""
    ranges = find_bulk_cut_ranges(filename)
    for start_sec, end_sec in ranges:
        if start_sec is not None:
            return start_sec, end_sec
    return None


def bulk_time_text(seconds: float) -> str:
    """A time in Bulk Cut's list: HH:MM:SS, with .mmm when it is not whole
    seconds (ranges sent from the Cut tab)."""
    if abs(seconds - round(seconds)) < 0.0005:
        return seconds_to_hms(seconds)
    return seconds_to_ffmpeg_time(seconds)


def bulk_ranges_text(ranges) -> str:
    return " | ".join(f"{bulk_time_text(s)} - {bulk_time_text(e)}" for s, e in ranges)


def bulk_lengths_text(ranges) -> str:
    return " | ".join(bulk_time_text(e - s) for s, e in ranges)


def strip_bracket_from_name(name_no_ext: str) -> str:
    """Remove all [start-end] brackets from a filename and tidy separators."""
    stripped = BULK_LAST_MINUTE_RE.sub("", BULK_BRACKET_RE.sub("", name_no_ext))
    return stripped.strip(" _-.") or name_no_ext


# --------------------------------------------------------------------------
# Settings -- the few preferences that survive a restart (dark mode), kept
# in %APPDATA%\BBVideoCutter\settings.json (~/.config/... elsewhere).
# --------------------------------------------------------------------------
def _settings_path():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "BBVideoCutter", "settings.json")


def load_settings():
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    path = _settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass  # a preference that can't be saved just won't survive a restart


# --------------------------------------------------------------------------
# Updates -- the app's latest release on GitHub. The built app (installer or
# portable) looks once a day, in the background; Help > Check for updates
# looks any time. A newer release is installed only when asked to.
# --------------------------------------------------------------------------
UPDATE_REPO = "Thmyris/BBVideoCutter"
UPDATE_API_URL = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
UPDATE_PAGE_URL = f"https://github.com/{UPDATE_REPO}/releases/latest"
UPDATE_CHECK_INTERVAL = 24 * 3600     # seconds between the automatic checks
# What an installed / a portable copy downloads; the names before v1.2
# after them, for a release that has only those.
UPDATE_SETUP_ASSETS = ("BBVideoCutter-Win-Setup.exe", "BBVideoCutter-Setup.exe")
UPDATE_PORTABLE_ASSETS = ("BBVideoCutter-Win-Portable.zip", "BBVideoCutter-Portable.zip")


def version_tuple(text):
    """(1, 10) for "v1.10" or "1.10", trailing zeros dropped (1.0 is 1);
    None when text has no version number."""
    m = re.match(r"\s*[vV]?(\d+(?:\.\d+)*)", str(text or ""))
    if not m:
        return None
    parts = [int(p) for p in m.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def https_context():
    """TLS for talking to GitHub: the system's trusted roots plus the
    standard (Mozilla) set from certifi. Windows adds a root to its store
    only when its own TLS first needs it, so a machine can lack the one
    GitHub's chain ends at (Sectigo Root E46, in 2026), and Python, which
    only reads the store, then can't verify GitHub at all."""
    import ssl
    context = ssl.create_default_context()
    try:
        import certifi
        context.load_verify_locations(certifi.where())
    except (ImportError, OSError, ssl.SSLError):
        pass   # the system's roots alone
    return context


def fetch_latest_release(timeout=15):
    """The latest release, as GitHub's API describes it: a dict with
    tag_name, html_url, body (the release notes) and assets (name,
    browser_download_url, size). Raises OSError or ValueError when it
    can't be had."""
    import urllib.request
    request = urllib.request.Request(UPDATE_API_URL, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"BBVideoCutter/{APP_VERSION}",
    })
    with urllib.request.urlopen(request, timeout=timeout, context=https_context()) as response:
        release = json.loads(response.read().decode("utf-8"))
    if not isinstance(release, dict) or version_tuple(release.get("tag_name")) is None:
        raise ValueError("GitHub's answer names no release version.")
    return release


def markdown_runs(line):
    """[(text, style)] of one line of release notes: style "bold" for
    **text** (or __text__), "code" for `text`, "" otherwise; a link
    [text](url) is its text."""
    runs = []
    pattern = re.compile(r"\*\*(.+?)\*\*|__(.+?)__|`([^`]+)`|\[([^\]]+)\]\([^)]*\)")
    at = 0
    for match in pattern.finditer(line):
        if match.start() > at:
            runs.append((line[at:match.start()], ""))
        bold, bold2, code, link = match.groups()
        if code is not None:
            runs.append((code, "code"))
        elif link is not None:
            runs.append((link, ""))
        else:
            runs.append((bold if bold is not None else bold2, "bold"))
        at = match.end()
    if at < len(line):
        runs.append((line[at:], ""))
    return runs


def markdown_blocks(notes):
    """GitHub-style release notes as [(kind, runs)]: kind "bullet" (a
    "- " or "* " item), "heading" ("#" lines) or "para"; a line that
    goes on the one before (a wrapped item or paragraph) is joined to it,
    as Markdown shows it."""
    blocks, current = [], None
    for raw in str(notes).splitlines():
        line = raw.strip()
        if not line:
            current = None
            continue
        bullet = re.match(r"[-*+]\s+(.*)", line)
        heading = re.match(r"#{1,6}\s+(.*)", line)
        if bullet or heading:
            current = ["bullet" if bullet else "heading", (bullet or heading).group(1)]
            blocks.append(current)
            if heading:
                current = None
        elif current is not None:
            current[1] += " " + line
        else:
            current = ["para", line]
            blocks.append(current)
    return [(kind, markdown_runs(text)) for kind, text in blocks]


def insert_markdown(text, blocks):
    """Writes markdown_blocks into a Text widget with its styles: bold,
    code, headings, and bullets with a hanging indent."""
    base = tkfont.Font(font=text.cget("font"))
    bold = base.copy()
    bold.configure(weight="bold")
    heading = base.copy()
    heading.configure(weight="bold", size=base.cget("size") + 1)
    mono = tkfont.Font(family="Consolas" if os.name == "nt" else "DejaVu Sans Mono",
                       size=base.cget("size"))
    text._bbvc_fonts = (bold, heading, mono)   # kept alive with the widget
    gap = base.metrics("linespace") // 2
    text.tag_configure("bold", font=bold)
    text.tag_configure("code", font=mono)
    text.tag_configure("heading", font=heading)
    dot = "•  "
    text.tag_configure("bullet", lmargin1=0, lmargin2=base.measure(dot))
    text.tag_configure("gap", spacing1=gap)
    for i, (kind, runs) in enumerate(blocks):
        block = (kind,) if kind != "para" else ()
        block += ("gap",) if i else ()
        if kind == "bullet":
            text.insert("end", dot, block)
        for run, style in runs:
            text.insert("end", run, block + ((style,) if style else ()))
        if i < len(blocks) - 1:
            text.insert("end", "\n", block)
    # The height it takes, in pixels, wrapped by words as Tk wraps them at
    # the box's width (it can't measure itself before it is shown).
    room = int(text.cget("width")) * base.measure("0")
    fonts = {"": base, "bold": bold, "code": mono}
    tall = 0
    for i, (kind, runs) in enumerate(blocks):
        indent = base.measure(dot) if kind == "bullet" else 0
        x, lines = indent, 1
        for run, style in runs:
            font = heading if kind == "heading" else fonts[style]
            for word in re.findall(r"\S+\s*", run):
                w = font.measure(word.rstrip())
                if x > indent and x + w > room:
                    x, lines = indent, lines + 1
                x += font.measure(word)
        tall += lines * (heading if kind == "heading" else base).metrics("linespace") + (gap if i else 0)
    return tall


def is_newer_release(release, current=APP_VERSION):
    new = version_tuple(release.get("tag_name"))
    return new is not None and new > version_tuple(current)


def release_asset(release, names):
    """The release's downloadable file called names[0] (or the next of
    names it has; names can also be one name), or None."""
    for name in ([names] if isinstance(names, str) else names):
        for asset in release.get("assets") or []:
            if isinstance(asset, dict) and asset.get("name") == name and asset.get("browser_download_url"):
                return asset
    return None


def download_file(url, dest, report=None, timeout=30):
    """Saves url to dest, calling report(fraction done) as it comes in.
    Returns the number of bytes written."""
    import urllib.request
    request = urllib.request.Request(url, headers={
        "Accept": "application/octet-stream",
        "User-Agent": f"BBVideoCutter/{APP_VERSION}",
    })
    done = 0
    with urllib.request.urlopen(request, timeout=timeout, context=https_context()) as response, \
            open(dest, "wb") as f:
        total = int(response.headers.get("Content-Length") or 0)
        while True:
            chunk = response.read(1 << 18)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if report is not None and total:
                report(done / total)
    return done


def running_built_app():
    """True in the PyInstaller build (installer or portable), which can
    update itself; False run from source (BBVideoCutter.py)."""
    return bool(getattr(sys, "frozen", False))


def app_is_installed():
    """True for the installer's copy: Inno Setup's uninstaller is next to
    it. A portable copy has none."""
    return os.path.isfile(os.path.join(_app_dir(), "unins000.exe"))


def folder_is_writable(folder):
    try:
        with tempfile.TemporaryFile(dir=folder):
            pass
        return True
    except OSError:
        return False


def _ps_quote(text):
    return "'" + str(text).replace("'", "''") + "'"


def update_helper_script(pid, app_dir, setup=None, new_build=None, relaunch=True):
    """PowerShell that waits for the app (process pid) to exit, then
    installs the update: runs the installer setup silently over the
    installed copy, or copies the unpacked portable build new_build over
    app_dir (its _internal folder put back as it was if that fails). Then
    the app starts again (relaunch=False only in tests)."""
    lines = [
        "$ErrorActionPreference = 'Stop'",
        f"Wait-Process -Id {int(pid)} -ErrorAction SilentlyContinue",
    ]
    if setup is not None:
        args = ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"] + (["/RELAUNCH=1"] if relaunch else [])
        lines.append(f"Start-Process -FilePath {_ps_quote(setup)} -ArgumentList "
                     + ",".join(_ps_quote(a) for a in args))
        return "\n".join(lines) + "\n"
    app, new = _ps_quote(app_dir), _ps_quote(new_build)
    lines += [
        f"$app = {app}",
        f"$new = {new}",
        "$internal = Join-Path $app '_internal'",
        "$old = Join-Path $app '_internal.old'",
        "if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force }",
        "if (Test-Path -LiteralPath $internal) { Rename-Item -LiteralPath $internal -NewName '_internal.old' }",
        "try {",
        "    Copy-Item -Path (Join-Path $new '*') -Destination $app -Recurse -Force",
        "    if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force }",
        "} catch {",
        "    if (Test-Path -LiteralPath $old) {",
        "        if (Test-Path -LiteralPath $internal) { Remove-Item -LiteralPath $internal -Recurse -Force }",
        "        Rename-Item -LiteralPath $old -NewName '_internal'",
        "    }",
        "}",
        "Remove-Item -LiteralPath (Split-Path -Parent $new) -Recurse -Force -ErrorAction SilentlyContinue",
    ]
    if relaunch:
        lines.append("Start-Process -FilePath (Join-Path $app 'BBVideoCutter.exe')")
    return "\n".join(lines) + "\n"


def launch_detached(cmd):
    """Starts cmd with no window, to live on after the app has closed."""
    flags = 0
    if os.name == "nt":
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            # Out of any job the app runs in, which could end it with the app.
            return subprocess.Popen(cmd, creationflags=flags | 0x01000000,   # CREATE_BREAKAWAY_FROM_JOB
                                    close_fds=True)
        except OSError:
            pass
    return subprocess.Popen(cmd, creationflags=flags, close_fds=True)


def powershell_hidden(script):
    """powershell.exe command line running script with no window; the
    script goes in encoded, so any quotes and spaces in it survive."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden", "-EncodedCommand", encoded]


def restart_script(pid, cmd, cwd):
    """PowerShell that waits for the app (process pid) to exit, then runs
    cmd (a list: program and its arguments) in cwd."""
    args = ""
    if cmd[1:]:
        # Start-Process joins the arguments with spaces as they are, so each
        # one carries its own double quotes.
        args = " -ArgumentList " + ",".join(_ps_quote('"' + a + '"') for a in cmd[1:])
    return "\n".join([
        f"Wait-Process -Id {int(pid)} -ErrorAction SilentlyContinue",
        f"Start-Process -FilePath {_ps_quote(cmd[0])}{args} -WorkingDirectory {_ps_quote(cwd)}",
    ]) + "\n"


def _git(folder, *args, timeout=30):
    """git run in folder, never asking for a password (no window, no
    prompt): a remote that needs one fails instead of hanging."""
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git isn't installed (or isn't on PATH).")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return subprocess.run([git, "-C", folder, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env,
                          **SUBPROCESS_KWARGS)


def git_pull_source(folder, timeout=120):
    """Run from source: brings the checkout in folder up to date with
    git pull --ff-only. Returns (commit before, commit after, the branch
    pulled from); raises RuntimeError saying why it couldn't."""
    if _git(folder, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        raise RuntimeError("its folder isn't a git checkout.")
    upstream = _git(folder, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream.returncode != 0:
        branch = _git(folder, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "the current branch"
        raise RuntimeError(f"{branch} has no upstream branch to pull from (no remote set up for it).")
    upstream = upstream.stdout.strip()
    before = _git(folder, "rev-parse", "HEAD").stdout.strip()
    try:
        pull = _git(folder, "pull", "--ff-only", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git pull from {upstream} took over {timeout // 60} minutes.") from None
    if pull.returncode != 0:
        out = (pull.stderr.strip() or pull.stdout.strip())[-1500:]
        raise RuntimeError(f"git pull from {upstream} failed:\n\n{out}")
    after = _git(folder, "rev-parse", "HEAD").stdout.strip()
    return before, after, upstream


def source_app_version(path):
    """APP_VERSION as written in the source file path, or None."""
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(r'^APP_VERSION = "([^"]+)"', f.read(), re.M)
    except OSError:
        return None
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# Themes
#
# The light theme is the app's original look: the literal colors every
# widget is created with. Dark mode swaps only the light "chrome" colors
# (window and panel backgrounds, buttons, entries, their text); areas that
# are already dark in the light theme (video previews, the Combine list,
# the audio track rows) look the same in both.
# --------------------------------------------------------------------------
DARK = {
    "panel": "#202023",
    "button": "#38383c",
    "button_active": "#46464b",
    "field": "#18181b",
    "status": "#2b2b2f",
    "border": "#4a4a50",
    "text": "#e4e4e7",
    "text_dim": "#a9a9ae",
    "text_hint": "#8e8e93",
    "text_disabled": "#6e6e73",
    "text_warn": "#e8a94a",
    "select": "#3d8bfd",
}

# Colors that code draws or sets itself (canvas items, highlights), per theme.
PLAYHEAD_COLOR = "#e8453c"   # the player's position on the Cut slider, both themes

THEME_COLORS = {
    "light": {
        "slider_track": "#c0c0c0",
        "slider_start": "#1a1a1a",
        "slider_keyframe": "#8c8c8c",
        "wave": "#a6a6a6",            # the Cut waveform outside the selection (inside: blue)
        "range": "#5cb87a",           # ranges marked for Bulk Cut, on the slider and waveform
        "range_edge": "#2e7d4a",
        "timeslider_track": "#d5d5d5",
        "aspect_active": "#cfe2ff",
        "icon": "#1a1a1a",            # the Cut tab's speaker
        "icon_hover": "#dadada",      # ...and the square behind it under the mouse
        # The menu bar matches the title bar above it, so the two read as
        # one header: white like the Windows 10 caption in light mode...
        "menubar_bg": "#ffffff",
        "menubar_fg": "#000000",
        "menubar_hover": "#e5f3ff",
        # The tab boxes on the menu bar: border, the one under the mouse,
        # and the open tab's box (its icon in the accent color).
        "tabbox_border": "#cccccc",
        "tabbox_hover": "#e5f3ff",
        "tabbox_selected": "#cce4f7",
        "tabbox_selected_border": "#0078d4",
        "tabbox_selected_icon": "#005a9e",
    },
    "dark": {
        "slider_track": "#505056",
        "slider_start": "#d0d0d4",
        "slider_keyframe": "#86868c",
        "wave": "#5e5e64",
        "range": "#3f8f5c",
        "range_edge": "#7fd19c",
        "timeslider_track": "#505056",
        "aspect_active": "#28456f",
        "icon": DARK["text"],
        "icon_hover": DARK["button_active"],
        # ...and the panel color, next to the dark caption, in dark mode.
        "menubar_bg": DARK["panel"],
        "menubar_fg": DARK["text"],
        "menubar_hover": DARK["button"],
        "tabbox_border": DARK["border"],
        "tabbox_hover": DARK["button"],
        "tabbox_selected": "#28456f",
        "tabbox_selected_border": DARK["select"],
        "tabbox_selected_icon": "#8ab4ff",
    },
}

# Dropdown menu colors in dark mode. In light mode the dropdowns keep the
# system menu colors Tk gives them.
DARK_MENU = {
    "background": DARK["status"],
    "foreground": DARK["text"],
    "activebackground": DARK["button_active"],
    "activeforeground": DARK["text"],
    "disabledforeground": DARK["text_disabled"],
    "selectcolor": DARK["text"],  # the Dark mode / overwrite check marks
}

# A list or field's background, themed to the dark field.
FIELD_BG = "SystemWindow"

# Light chrome backgrounds. A widget whose background is one of these is
# part of the chrome and gets themed; any other background (black, #1e1e1e,
# #2a2a2a...) is a content area and is left alone, text colors included.
_CHROME_BG = {
    "systembuttonface": "panel", "#f0f0f0": "panel", "#d9d9d9": "panel",
    "systemwindow": "field",
    "#e6e6e6": "status",
    "#828790": "border",   # a list's edge, like the Treeview's
    "#cfe2ff": None,  # the Crop tab's active ratio button, see _dark_bg
}
_BG_OPTIONS = ("background", "activebackground", "highlightbackground",
               "selectcolor", "disabledbackground", "readonlybackground")
_FG_OPTIONS = {
    "systembuttontext": "text", "systemwindowtext": "text",
    "black": "text", "#000000": "text",
    "systemdisabledtext": "text_disabled", "#a3a3a3": "text_disabled",
    "#555555": "text_dim", "#777777": "text_hint",
    "#a35200": "text_warn",   # a notice, e.g. Combine will re-encode
    "systemwindowframe": "border",
}
_FG_OPTION_NAMES = ("foreground", "activeforeground", "disabledforeground",
                    "insertbackground", "highlightcolor")


def _dark_bg(widget_class, option, value):
    """Dark replacement for a light chrome background option, or None."""
    value = value.lower()
    if widget_class in ("Entry", "Spinbox") and value in ("white", "#ffffff"):
        return DARK["field"]
    if option == "selectcolor" and value in ("systemwindow", "white", "#ffffff"):
        return DARK["field"]
    if value == "#cfe2ff":
        return THEME_COLORS["dark"]["aspect_active"]
    role = _CHROME_BG.get(value)
    if role is None:
        return None
    if widget_class == "Button" and role == "panel":
        if option == "background":
            return DARK["button"]
        if option == "activebackground":
            return DARK["button_active"]
        return DARK["panel"]  # the focus ring's surround matches the panel
    if widget_class in ("Entry", "Spinbox") and option in ("disabledbackground",
                                                          "readonlybackground"):
        return DARK["panel"]
    return DARK[role]


# --------------------------------------------------------------------------
# High-DPI. Without this, Windows treats the app as DPI-unaware: it draws
# at 96 DPI and Windows stretches the finished bitmap to the display's
# scaling (125%, 150%...), which makes all text blurry. Declaring "system
# DPI aware" makes Windows hand Tk the real DPI instead: fonts (sized in
# points) come out sharp and at the right size on their own, and every
# size the code gives in raw pixels goes through px() to grow with them.
# Per-monitor awareness is deliberately not used -- Tk 8.6 doesn't
# re-layout when the window moves to a monitor with different scaling.
# --------------------------------------------------------------------------
UI_SCALE = 1.0  # display DPI / 96, set once the main window exists


def px(n):
    """A size designed at 100% scaling, in pixels at the current scaling."""
    return int(round(n * UI_SCALE))


def enable_dpi_awareness():
    """Must run before the first window is created; harmless to repeat."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # Vista/7 fallback
        except (AttributeError, OSError):
            pass


_menu_margin_brushes = {}  # "#rrggbb" -> GDI brush, made once and kept


def post_with_menu_margin(post, color):
    """Runs post(), which opens a Tk menu and (on Windows) returns once it
    has closed, with the menu's margin painted `color` ("#rrggbb"), or the
    system menu color for None.

    Tk draws every menu item itself, in the menu's own colors, but Windows
    paints the 2 px margin around the items with the menu's background
    brush, which Tk leaves at the system menu color: a light frame around a
    dark menu. Tk keeps the menu handle to itself, so a hook on this thread
    catches it as Windows opens the menu (WM_INITMENUPOPUP, before anything
    is drawn) and sets the brush. The hook exists only while post() runs."""
    if os.name != "nt":
        return post()
    from ctypes import wintypes

    class CWPSTRUCT(ctypes.Structure):
        _fields_ = [("lParam", wintypes.LPARAM), ("wParam", wintypes.WPARAM),
                    ("message", wintypes.UINT), ("hwnd", wintypes.HWND)]

    class MENUINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.DWORD),
                    ("dwStyle", wintypes.DWORD), ("cyMax", wintypes.UINT),
                    ("hbrBack", wintypes.HBRUSH), ("dwContextHelpID", wintypes.DWORD),
                    ("dwMenuData", ctypes.c_size_t)]

    WH_CALLWNDPROC, WM_INITMENUPOPUP, MIM_BACKGROUND = 4, 0x0117, 0x2
    try:
        # Private handles, so these argtypes don't leak into other ctypes calls.
        user32 = ctypes.WinDLL("user32")
        gdi32 = ctypes.WinDLL("gdi32")
        hookproc_t = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                        wintypes.WPARAM, wintypes.LPARAM)
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, hookproc_t,
                                             wintypes.HINSTANCE, wintypes.DWORD]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        user32.SetMenuInfo.argtypes = [wintypes.HMENU, ctypes.POINTER(MENUINFO)]
        gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
        gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
        brush = None
        if color:
            brush = _menu_margin_brushes.get(color)
            if brush is None:
                r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
                brush = _menu_margin_brushes[color] = gdi32.CreateSolidBrush(r | g << 8 | b << 16)
    except (AttributeError, OSError):
        return post()

    def on_message(code, wparam, lparam):
        try:
            if code >= 0:
                msg = CWPSTRUCT.from_address(lparam)
                if msg.message == WM_INITMENUPOPUP:
                    info = MENUINFO(cbSize=ctypes.sizeof(MENUINFO),
                                    fMask=MIM_BACKGROUND, hbrBack=brush)
                    user32.SetMenuInfo(msg.wParam, ctypes.byref(info))
        except Exception:
            pass
        return user32.CallNextHookEx(None, code, wparam, lparam)

    proc = hookproc_t(on_message)  # kept referenced until unhooked below
    hook = user32.SetWindowsHookExW(WH_CALLWNDPROC, proc, None,
                                    ctypes.windll.kernel32.GetCurrentThreadId())
    try:
        return post()
    finally:
        if hook:
            user32.UnhookWindowsHookEx(hook)


# --------------------------------------------------------------------------
# TimeSlider -- a flat, modern single-handle scrubber bar.
#
# Replaces the native tk.Scale widget, which renders as the dated, grooved
# OS slider control and looks out of place next to the app's other
# canvas-drawn controls (e.g. the Cut tab's dual-handle range slider).
# Reused anywhere a single value needs to be scrubbed: the Resize/Crop
# tab's frame position, the Audio tab's playback position, and each audio
# track's volume control.
# --------------------------------------------------------------------------
def file_identity(path):
    """What a per-file cache is keyed on: the path, size and modification
    time, so a file changed on disk is read again. Raises OSError."""
    st = os.stat(path)
    return os.path.normcase(os.path.abspath(path)), st.st_size, st.st_mtime_ns


def render_waveform(path, a_index, width, height, color="white", span=None, on_start=None):
    """A picture of audio track a_index (counted among the audio tracks) of
    the file, drawn by ffmpeg's showwavespic: a PIL image, width x height,
    in `color` on black. Raises if it can't be drawn (no such track, say).

    span=None spreads the track's own sound across the width. span=seconds
    spreads the file's clock instead, 0 to span seconds from the file's
    start time (what ffmpeg -ss and the Cut slider count), with silence
    where the track has no sound (it starts late, has gaps or ends early),
    so the picture lines up with a time slider.

    a_index can also be a list of tracks: their sound summed, as the
    player's mix plays it.

    on_start(process) gets the ffmpeg process, so it can be killed; a
    killed render raises."""
    align = ""
    if span is not None:
        align = (f"aresample=async=1:first_pts=0,"
                 f"apad=whole_dur={span:.6f},atrim=end={span:.6f},")
    if isinstance(a_index, (list, tuple)):
        first = "aresample=async=1:first_pts=0," if span is not None else ""
        mix = ";".join(f"[0:a:{k}]{first}anull[m{k}]" for k in a_index)
        mix += ";" + "".join(f"[m{k}]" for k in a_index)
        mix += f"amix=inputs={len(a_index)}:duration=longest:normalize=0,"
        filt = (f"{mix}{align}showwavespic=s={width}x{height}:colors={color}:"
                f"scale=cbrt:draw=full[wv]")
    else:
        filt = None
    # draw=full ignores amplitude when deciding pixel brightness (only the
    # height reflects amplitude) -- with draw=scale (the default) quiet
    # passages fade to near-invisible, which is what made the waveform so
    # hard to see. scale=cbrt compresses the dynamic range so quiet parts
    # still show visible height, not just the loudest peaks.
    if filt is None:
        filt = (f"[0:a:{a_index}]{align}showwavespic=s={width}x{height}:colors={color}:"
                f"scale=cbrt:draw=full[wv]")
    cmd = [find_exe("ffmpeg"), "-v", "error", "-i", path, "-filter_complex", filt,
           "-map", "[wv]", "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            **SUBPROCESS_KWARGS)
    if on_start is not None:
        on_start(proc)
    out, err = proc.communicate()
    if proc.returncode != 0 or not out:
        raise RuntimeError(err.decode("utf-8", "replace").strip() or "ffmpeg failed")
    img = Image.open(io.BytesIO(out))
    img.load()
    return img


# ---- smooth shapes ----------------------------------------------------------
# Tk's canvas draws ovals, polygons and round line ends without
# anti-aliasing, so their edges step. The sliders' knobs and round track
# ends are drawn with Pillow instead: SMOOTH_SCALE times as large, averaged
# down (Image.reduce), at the quarter pixel they fall on. Each one is drawn
# once and kept (per Tk interpreter, which an image belongs to).
SMOOTH_SCALE = 4


def color_hex(widget, color):
    """Any Tk colour (a name, "System...", #rgb) as #rrggbb, for Pillow."""
    r, g, b = widget.winfo_rgb(color)
    return f"#{r >> 8:02x}{g >> 8:02x}{b >> 8:02x}"


def draw_smooth(canvas, cx, cy, reach, key, paint, **item_options):
    """Puts a smooth shape on canvas, centred on (cx, cy) and no more than
    reach px from it. paint(img, draw, ox, oy, k) draws it on img (RGBA, k
    times the size; draw is its ImageDraw), centred on (ox, oy) in img's
    pixels, and returns the image to show: img, or a new one (e.g. with a
    blurred shadow under it). key names the shape for the cache. Returns
    the canvas item."""
    from PIL import ImageDraw
    k = SMOOTH_SCALE
    reach = int(math.ceil(reach)) + 1
    left, top = cx - reach, cy - reach
    ix, iy = math.floor(left), math.floor(top)
    fx, fy = round((left - ix) * k) / k, round((top - iy) * k) / k
    size = 2 * reach + 2
    cache = canvas._root().__dict__.setdefault("_bbvc_smooth", {})
    full_key = (key, size, fx, fy)
    photo = cache.get(full_key)
    if photo is None:
        img = Image.new("RGBA", (size * k, size * k), (0, 0, 0, 0))
        img = paint(img, ImageDraw.Draw(img), (reach + fx) * k, (reach + fy) * k, k) or img
        photo = cache[full_key] = ImageTk.PhotoImage(img.reduce(k), master=canvas)
    return canvas.create_image(ix, iy, anchor="nw", image=photo, **item_options)


def smooth_disc(canvas, cx, cy, r, color, **item_options):
    """A smooth filled circle of radius r (px) on canvas."""
    def paint(img, d, ox, oy, k):
        d.ellipse((ox - r * k, oy - r * k, ox + r * k, oy + r * k), fill=color)
    return draw_smooth(canvas, cx, cy, r, ("disc", r, color), paint, **item_options)


class SmoothPainter:
    """Takes the canvas drawing calls an icon is made of (create_line,
    create_arc, create_oval, create_polygon, create_rectangle, with the
    canvas's coordinates and options) and draws them with Pillow
    SMOOTH_SCALE times as large; photo() gives them back scaled down,
    smooth, for one create_image. Line ends and corners come out round
    where Tk's capstyle / joinstyle say so."""

    def __init__(self, widget, width, height):
        from PIL import ImageDraw
        self.widget, self.k = widget, SMOOTH_SCALE
        self.img = Image.new("RGBA", (math.ceil(width) * self.k, math.ceil(height) * self.k), (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.img)

    def _color(self, color):
        return color_hex(self.widget, color) if color else None

    def _dot(self, x, y, r, color):
        self.draw.ellipse((x - r, y - r, x + r, y + r), fill=color)

    def _stroke(self, pts, color, width, round_ends):
        """A line through pts (big image pixels), width wide."""
        self.draw.line(pts, fill=color, width=max(1, round(width)))
        if round_ends:
            for x, y in zip(pts[::2], pts[1::2]):
                self._dot(x, y, width / 2, color)

    def create_line(self, *pts, fill="black", width=1, capstyle="butt", **_kw):
        self._stroke([v * self.k for v in pts], self._color(fill), width * self.k, capstyle == "round")

    def create_arc(self, x0, y0, x1, y1, start=0, extent=90, outline="black", width=1, **_kw):
        # Tk: degrees counter-clockwise from 3 o'clock, the line centred on
        # the oval; Pillow: clockwise, the line inside the box.
        k, w = self.k, width * self.k
        box = (x0 * k - w / 2, y0 * k - w / 2, x1 * k + w / 2, y1 * k + w / 2)
        self.draw.arc(box, -(start + extent), -start, fill=self._color(outline), width=max(1, round(w)))

    def create_oval(self, x0, y0, x1, y1, fill="", **_kw):
        k = self.k
        if fill:
            self.draw.ellipse((x0 * k, y0 * k, x1 * k, y1 * k), fill=self._color(fill))

    def create_rectangle(self, x0, y0, x1, y1, fill="", **_kw):
        k = self.k
        if fill:
            self.draw.rectangle((x0 * k, y0 * k, x1 * k, y1 * k), fill=self._color(fill))

    def create_polygon(self, *pts, fill="black", outline="", width=1, joinstyle="miter", **_kw):
        big = [v * self.k for v in pts]
        if fill:
            self.draw.polygon(big, fill=self._color(fill))
        if outline:
            # The outline round the shape, with round corners as Tk's
            # joinstyle "round" (and near enough for its thin miter ones).
            self._stroke(big + big[:2], self._color(outline), width * self.k, True)

    def photo(self):
        return ImageTk.PhotoImage(self.img.reduce(self.k), master=self.widget)


class TimeSlider(tk.Canvas):
    # The marks (set_marks): where the parts of a joined video meet. Gray
    # shows on the track in both themes.
    MARK_COLOR = "#8c8c8c"

    def __init__(self, parent, command=None, height=28, track_color="#d5d5d5",
                 fill_color="#3d8bfd", handle_color="#1a73e8", bg="#f0f0f0",
                 on_release=None, **kwargs):
        if "width" in kwargs:
            kwargs["width"] = px(kwargs["width"])
        super().__init__(parent, height=px(height), bg=bg, highlightthickness=0, **kwargs)
        self._command = command
        self._on_release_command = on_release   # gets the value a drag ended on
        self._minv = 0.0
        self._maxv = 1.0
        self._value = 0.0
        self._marks = []
        self._dragging = False
        self._track_color = track_color
        self._fill_color = fill_color
        self._handle_color = handle_color
        self._knob = "classic"        # the knob (set_look; KNOBS)
        self._track_width = 5
        self._hover = False
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", lambda e: self._set_hover(True), add="+")
        self.bind("<Leave>", lambda e: self._set_hover(False), add="+")

    # The knobs set_look takes: "classic", the blue ball with a white edge
    # (the seek sliders); "none", no knob but a dot under the mouse or while
    # dragging (the volume sliders).
    KNOBS = ("classic", "none")
    # The knobs that change under the mouse.
    HOVER_KNOBS = ("none",)

    def set_look(self, knob, track_width):
        """The ball's look (one of KNOBS) and the track's width (unscaled px)."""
        if (knob, track_width) != (self._knob, self._track_width):
            self._knob, self._track_width = knob, track_width
            self._redraw()

    def _set_hover(self, on):
        self._hover = on
        if self._knob in self.HOVER_KNOBS:
            self._redraw()

    def config_range(self, to, frm=0.0):
        self._minv = frm
        self._maxv = to if to > frm else frm + 0.0001
        self._value = min(self._maxv, max(self._minv, self._value))
        self._redraw()

    # Kept for drop-in compatibility with code that used to call the
    # native tk.Scale's `.config(to=...)`.
    def config(self, **kwargs):
        if "to" in kwargs:
            self.config_range(kwargs.pop("to"))
        if kwargs:
            super().config(**kwargs)

    def set(self, value):
        self._value = min(self._maxv, max(self._minv, value))
        self._redraw()

    def get(self):
        return self._value

    @property
    def dragging(self):
        return self._dragging

    def set_marks(self, values):
        values = list(values)
        if values != self._marks:
            self._marks = values
            self._redraw()

    def set_track_color(self, color):
        self._track_color = color
        self._redraw()

    def _track_bounds(self):
        w = self.winfo_width()
        if w < 2:
            w = px(780)
        return px(10), max(px(20), w - px(10))

    def _x_for(self, v):
        x0, x1 = self._track_bounds()
        span = self._maxv - self._minv
        frac = (v - self._minv) / span if span else 0.0
        return x0 + frac * (x1 - x0)

    def _v_for_x(self, x):
        x0, x1 = self._track_bounds()
        frac = (x - x0) / max(1, (x1 - x0))
        frac = min(1.0, max(0.0, frac))
        return self._minv + frac * (self._maxv - self._minv)

    def _redraw(self):
        self.delete("all")
        x0, x1 = self._track_bounds()
        h = self.winfo_height() or px(28)
        y = h / 2
        tw = max(1, px(self._track_width))
        hx = self._x_for(self._value)
        if PIL_AVAILABLE:
            # The track on whole pixel rows, with smooth round ends; the
            # knob centred on it to the quarter pixel.
            top = round(y - tw / 2)
            y = top + tw / 2
            track, fill = color_hex(self, self._track_color), color_hex(self, self._fill_color)
            self.create_rectangle(x0, top, x1, top + tw, fill=track, outline="")
            smooth_disc(self, x1, y, tw / 2, fill if hx >= x1 else track)
            smooth_disc(self, x0, y, tw / 2, fill if hx > x0 else track)
            if hx > x0:
                self.create_rectangle(x0, top, hx, top + tw, fill=fill, outline="")
                smooth_disc(self, hx, y, tw / 2, fill)
        else:
            self.create_line(x0, y, x1, y, fill=self._track_color, width=tw, capstyle="round")
            if hx > x0:
                self.create_line(x0, y, hx, y, fill=self._fill_color, width=tw, capstyle="round")
        for v in self._marks:
            mx = self._x_for(v)
            self.create_line(mx, y - px(7), mx, y + px(7), fill=self.MARK_COLOR,
                             width=max(1, px(2)), tags=("mark",))
        if PIL_AVAILABLE:
            self._draw_smooth_knob(hx, y)
        else:
            self._draw_knob(hx, y)

    def _draw_smooth_knob(self, x, y):
        """The knob (KNOBS) drawn smooth with Pillow; sizes as _draw_knob's."""
        knob, drag, hover = self._knob, self._dragging, self._hover
        if knob == "none" and not (drag or hover):
            return
        u = UI_SCALE
        accent = color_hex(self, self._handle_color)

        def paint(img, d, ox, oy, k):
            def disc(r, fill):
                d.ellipse((ox - r * k, oy - r * k, ox + r * k, oy + r * k), fill=fill)
            if knob == "none":            # just the fill; a dot while dragging or under the mouse
                disc(6 * u, accent)
            else:                         # classic
                r = (9 if drag else 7) * u
                disc(r + u, "#ffffff")
                disc(r - u, accent)
            return img

        key = ("knob", knob, drag, hover and knob in self.HOVER_KNOBS, accent, u)
        draw_smooth(self, x, y, 11 * u, key, paint)

    def _draw_knob(self, x, y):
        """The knob on the canvas itself (no Pillow)."""
        knob, accent, drag = self._knob, self._handle_color, self._dragging
        if knob == "none":
            if drag or self._hover:
                r = px(6)
                self.create_oval(x - r, y - r, x + r, y + r, fill=accent, outline="")
        else:                         # classic
            r = px(9) if drag else px(7)
            self.create_oval(x - r, y - r, x + r, y + r, fill=accent, outline="#ffffff",
                             width=max(1, px(2)))

    def _emit(self):
        if self._command:
            self._command(self._value)

    def _on_press(self, event):
        self._dragging = True
        self._value = self._v_for_x(event.x)
        self._redraw()
        self._emit()

    def _on_drag(self, event):
        if not self._dragging:
            return
        self._value = self._v_for_x(event.x)
        self._redraw()
        self._emit()

    def _on_release(self, event):
        was_dragging, self._dragging = self._dragging, False
        self._redraw()
        if was_dragging and self._on_release_command:
            self._on_release_command(self._value)


# --------------------------------------------------------------------------
# Embedded video player (libmpv)
# --------------------------------------------------------------------------
# The video plays inside the window through libmpv, the library behind the
# mpv player: GPU decoding and drawing, so even 4K60 HEVC plays smoothly
# with next to no CPU. It needs the python-mpv package and libmpv-2.dll
# (https://github.com/shinchiro/mpv-winbuild-cmake/releases, the
# "mpv-dev-x86_64" download). Without them the app still works and shows
# still frames from ffmpeg instead, as it always did.
LIBMPV_DLL = "libmpv-2.dll"
_mpv_module = None
_mpv_error = None
_mpv_dll_dir_handle = None   # keeps os.add_dll_directory's entry alive


def find_libmpv_dir():
    """The folder holding libmpv-2.dll, or None: next to the app (or in an
    "mpv" folder beside it), inside a PyInstaller bundle, in release\\build
    (where the build scripts take it from, so a source checkout finds it
    too), or on PATH."""
    if os.name != "nt":
        return None
    app = _app_dir()
    dirs = [app, os.path.join(app, "mpv")]
    if getattr(sys, "_MEIPASS", None):
        dirs.append(sys._MEIPASS)
    dirs.append(os.path.join(app, "release", "build"))
    dirs += os.environ.get("PATH", "").split(os.pathsep)
    for d in dirs:
        if d and os.path.isfile(os.path.join(d, LIBMPV_DLL)):
            return d
    return None


def mpv_available():
    """Cheap check (no import) that the player can be used at all."""
    if _mpv_module is not None:
        return True
    if _mpv_error is not None:
        return False
    import importlib.util
    return find_libmpv_dir() is not None and importlib.util.find_spec("mpv") is not None


def load_mpv():
    """Imports python-mpv on first use, pointing it at libmpv-2.dll. Returns
    the module, or None if it can't be loaded (the reason is in _mpv_error)."""
    global _mpv_module, _mpv_error, _mpv_dll_dir_handle
    if _mpv_module is not None or _mpv_error is not None:
        return _mpv_module
    d = find_libmpv_dir()
    if d is None:
        _mpv_error = f"{LIBMPV_DLL} was not found"
        return None
    # python-mpv looks the DLL up on PATH.
    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
    try:
        _mpv_dll_dir_handle = os.add_dll_directory(d)
        import mpv
    except Exception as exc:
        _mpv_error = str(exc)
        return None
    _mpv_module = mpv
    return mpv


def probe_shown_video_size(ffprobe, path):
    """(width, height, turn) of path's first video stream as players show
    it: turn is the clockwise quarter turn (0, 90, 180 or 270) the file
    asks for, and width and height are swapped when it is 90 or 270.
    ffmpeg turns the frames the same way before any filter (-vf) runs."""
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:stream_tags=rotate:stream_side_data=rotation",
         "-of", "json", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
        **SUBPROCESS_KWARGS
    )
    streams = json.loads(result.stdout or "{}").get("streams") or []
    if not streams or not streams[0].get("width") or not streams[0].get("height"):
        raise RuntimeError("Could not determine video dimensions.")
    stream = streams[0]
    width, height = int(stream["width"]), int(stream["height"])
    # The display matrix's rotation is counterclockwise; the old "rotate"
    # tag (which ffprobe only shows when there is no matrix) is clockwise.
    turn = 0.0
    matrix = [d for d in stream.get("side_data_list") or [] if "rotation" in d]
    if matrix:
        turn = -float(matrix[0]["rotation"])
    elif (stream.get("tags") or {}).get("rotate"):
        turn = float(stream["tags"]["rotate"])
    turn = int(round(turn / 90.0)) % 4 * 90
    if turn in (90, 270):
        width, height = height, width
    return width, height, turn


def _plain_number(value):
    return f"{value:.3f}".rstrip("0").rstrip(".")


def describe_audio_stream(stream):
    """'aac · 48 kHz · 2 ch' for one of ffprobe's audio stream entries."""
    parts = [stream.get("codec_name") or "?"]
    try:
        parts.append(f"{_plain_number(int(stream['sample_rate']) / 1000)} kHz")
    except (KeyError, TypeError, ValueError):
        pass
    if stream.get("channels"):
        parts.append(f"{stream['channels']} ch")
    return " · ".join(parts)


def probe_file_summary(ffprobe, path):
    """(length, video, audio) texts for the Combine list, e.g.
    ("00:01:05", "h264 · 1920×1080 · 29.97 fps", "aac · 48 kHz · 2 ch").
    The video is its first stream as stored; the audio is the first track,
    with how many there are when there are several. "—" for what can't be
    read."""
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate,"
             "sample_rate,channels",
             "-of", "json", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            **SUBPROCESS_KWARGS)
        info = json.loads(result.stdout or "{}")
    except Exception:
        return "—", "—", "—"
    try:
        length = seconds_to_hms(float(info.get("format", {}).get("duration")))
    except (TypeError, ValueError):
        length = "—"
    streams = info.get("streams") or []
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]

    video = "none"
    if videos:
        v = videos[0]
        parts = [v.get("codec_name") or "?"]
        if v.get("width") and v.get("height"):
            parts.append(f"{v['width']}×{v['height']}")
        try:
            num, den = (int(x) for x in str(v.get("avg_frame_rate", "0/0")).split("/"))
            if num and den:
                parts.append(f"{_plain_number(num / den)} fps")
        except ValueError:
            pass
        video = " · ".join(parts)
    audio = "none"
    if audios:
        audio = describe_audio_stream(audios[0])
        if len(audios) > 1:
            audio = f"{len(audios)} tracks: {audio}"
    return length, video, audio


def describe_audio_track(track):
    """Menu label for one of mpv's track-list entries, e.g.
    '2: Commentary [eng] (aac, 2 ch)'."""
    label = str(track.get("id", "?"))
    name = " ".join(str(v) for v in (track.get("title"),
                                      f"[{track['lang']}]" if track.get("lang") else None) if v)
    details = ", ".join(str(v) for v in (
        track.get("codec"),
        f"{track['demux-channel-count']} ch" if track.get("demux-channel-count") else None) if v)
    return label + (f": {name}" if name else "") + (f" ({details})" if details else "")


class VideoPlayer:
    """libmpv playing into a Tk frame (self.frame), created on the first
    load. mpv reports property changes on its own thread; those only land
    in self.state, and a Tk timer (about 30 times a second) passes them on
    to on_change, so Tk is only ever touched from the Tk thread."""

    # Extra mpv options for every player; the tests use {"ao": "null"} so
    # nothing is heard.
    extra_options = {}
    # mpv options another platform needs, over the ones below (see the end
    # of this file).
    platform_options = {}
    OBSERVED = ("time-pos", "pause", "duration", "eof-reached", "container-fps", "path",
                "track-list", "aid", "chapter-list")

    def __init__(self, parent, on_change=None, options=None):
        self.frame = tk.Frame(parent, bg="black", takefocus=0)
        self.on_change = on_change
        self.options = options or {}     # this player's own mpv options
        self.mpv = None
        self.state = {}
        self._poll_id = None
        self._last_seen = None
        self._disabled_windows = set()
        # Kept here too, so they can be set before mpv is started.
        self.volume = 100.0
        self.muted = False
        self._range = None   # the play_range running: its end and progress
        # Every audio track played at once on each file loaded (the Cut
        # tab's player); audio_mix: the track ids being mixed, or None.
        self.mix_all_audio = False
        self.audio_mix = None
        self._mix_pending = False
        self._mix_graph = ""   # the lavfi-complex set
        self._audio_delay = 0.0

    # ---- life cycle ----------------------------------------------------
    def _ensure_mpv(self):
        if self.mpv is not None:
            return
        mpv = load_mpv()
        if mpv is None:
            raise RuntimeError(f"The video player could not be started: {_mpv_error}")
        options = dict(
            wid=str(self.frame.winfo_id()),
            vo="gpu-next,gpu", hwdec="auto-safe",
            keep_open="yes", idle="yes", force_window="yes", pause=True,
            # Exact seeks; the offset makes them land right in MPEG-TS
            # files too, where they otherwise overshoot by up to a second.
            hr_seek="yes", hr_seek_demuxer_offset=1.0,
            # mpv must not react to the keyboard or mouse itself, or show
            # its own controls and messages; the app does all of that.
            input_default_bindings=False, input_vo_keyboard=False,
            osc=False, osd_level=0, load_scripts=False, ytdl=False,
            sub_auto="no", audio_file_auto="no",
            volume=self.volume, mute="yes" if self.muted else "no",
        )
        options.update(self.platform_options)
        options.update(self.options)
        options.update(self.extra_options)
        self.mpv = mpv.MPV(**options)
        for name in self.OBSERVED:
            self.mpv.observe_property(name, self._observe)
        self.mpv.register_event_callback(self._on_event)
        self._poll()

    def _observe(self, name, value):
        # mpv's event thread: only store the value.
        self.state[name] = value

    def _on_event(self, event):
        # mpv's event thread too. "ready": the file asked for last is open
        # (a property such as path would not change when the same file is
        # opened again); "failed": mpv could not open it.
        eid = event.event_id.value
        if eid == _mpv_module.MpvEventID.FILE_LOADED:
            self.state["ready"] = True
        elif eid == _mpv_module.MpvEventID.END_FILE:
            if event.data.reason == _mpv_module.MpvEventEndFile.ERROR:
                self.state["ready"] = False
                self.state["failed"] = True

    def terminate(self):
        if self._poll_id is not None:
            try:
                self.frame.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None
        if self.mpv is not None:
            player, self.mpv = self.mpv, None

            def stop():
                try:
                    player.terminate()
                except Exception:
                    pass

            if os.name != "nt":
                stop()
                return
            # While mpv shuts down, its video thread can send a window
            # message to this thread's windows and wait for the answer (it
            # does once a transient dialog has been open), so this thread
            # must not just block in terminate(): it waits with mpv
            # shutting down on another thread, letting Windows deliver such
            # messages (PeekMessage does that; nothing is taken off the
            # queue, so no Tk event runs here).
            from ctypes import wintypes
            stopper = threading.Thread(target=stop, daemon=True)
            stopper.start()
            msg = wintypes.MSG()
            deadline = time.monotonic() + 5.0
            while stopper.is_alive() and time.monotonic() < deadline:
                ctypes.windll.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)   # PM_NOREMOVE
                stopper.join(0.01)

    def _poll(self):
        self._poll_id = self.frame.after(33, self._poll)
        self._disable_mpv_windows()
        self._check_range()
        if self._mix_pending and self.loaded and "track-list" in self.state:
            self._mix_pending = False
            ids = [t["id"] for t in self.audio_tracks]
            if len(ids) > 1:
                self.set_audio_mix(ids)
        seen = tuple(self.state.get(name) for name in self.OBSERVED + ("ready", "failed")) \
            + (tuple(self.audio_mix or ()),)
        if seen != self._last_seen:
            self._last_seen = seen
            if self.on_change:
                self.on_change()

    def _disable_mpv_windows(self):
        """mpv draws into a child window of self.frame, which would take
        every click, wheel turn and file drop over the video. Disabled, it
        keeps drawing but Windows skips it when finding the window under
        the mouse, so all of those reach the Tk frame instead. (mpv's own
        input-cursor-passthrough option does not do this on Windows.)"""
        if os.name != "nt":
            return
        kids = []
        _user32.EnumChildWindows(self.frame.winfo_id(),
                                 _ENUM_CHILD_PROC(lambda h, _: kids.append(h) or True), 0)
        for hwnd in kids:
            if hwnd not in self._disabled_windows:
                _user32.EnableWindow(hwnd, False)
                self._disabled_windows.add(hwnd)

    # ---- state -----------------------------------------------------------
    @property
    def loaded(self):
        return self.mpv is not None and bool(self.state.get("ready"))

    @property
    def failed(self):
        return bool(self.state.get("failed"))

    @property
    def position(self):
        """Time of the frame on screen, in seconds from the file's start
        (the same clock ffmpeg -ss uses, also for files whose timestamps
        don't start at 0), or None while nothing is loaded."""
        return self.state.get("time-pos")

    @property
    def paused(self):
        return self.state.get("pause") is not False

    @property
    def at_end(self):
        # eof-reached only turns on once playback runs into the end, not
        # when a seek lands on the last frame.
        pos, dur = self.position, self.state.get("duration")
        return bool(self.state.get("eof-reached")) or (
            pos is not None and bool(dur) and pos >= dur - 1.5 * self.frame_duration)

    @property
    def frame_duration(self):
        fps = self.state.get("container-fps")
        return 1.0 / fps if fps and fps > 0 else 1.0 / 30

    @property
    def audio_tracks(self):
        """The loaded file's audio tracks: mpv's track-list entries (id,
        title, lang, codec, demux-channel-count...), in file order."""
        if not self.loaded:
            return []
        return [t for t in self.state.get("track-list") or []
                if t.get("type") == "audio" and not t.get("external")]

    def add_audio_file(self, path):
        """Adds the audio file path to the loaded file as an extra track,
        not played until a mix names it (external_audio_id). It goes when
        another file is loaded."""
        if self.mpv is not None:
            self._async("audio-add", path, "auto")

    def external_audio_id(self, path):
        """mpv's id of the track add_audio_file(path) added, once it's in."""
        want = os.path.normcase(os.path.abspath(path))
        for t in self.state.get("track-list") or []:
            name = t.get("external-filename")
            if t.get("type") == "audio" and t.get("external") and name \
                    and os.path.normcase(os.path.abspath(name)) == want:
                return t.get("id")
        return None

    @property
    def audio_track(self):
        """The id of the audio track being played, or None."""
        aid = self.state.get("aid")
        return aid if isinstance(aid, int) and not isinstance(aid, bool) else None

    @property
    def part_starts(self):
        """Where each part of an edl_url() video starts (mpv makes a chapter
        per part), in seconds; [0.0] for a plain file."""
        starts = [c.get("time") for c in self.state.get("chapter-list") or []]
        starts = sorted(t for t in starts if isinstance(t, (int, float)))
        return starts if starts and starts[0] <= 0.001 else [0.0] + starts

    # ---- commands --------------------------------------------------------
    def _async(self, *args):
        # Asynchronous, so the UI never waits on mpv; errors (a seek past
        # the end, say) need no handling.
        self.mpv.command_async(*args, callback=lambda error, result: None)

    def load(self, path, start=None):
        """Opens path (a file or an edl_url()), paused, at time start."""
        self._ensure_mpv()
        self._clear_range()
        # The last file's mix names its tracks, which this one may not have.
        self.set_audio_mix(None)
        self._mix_pending = self.mix_all_audio
        self.state.pop("time-pos", None)
        self.state.pop("track-list", None)   # the old file's, until the new one's arrive
        self.state.pop("aid", None)
        self.state.pop("chapter-list", None)
        self.state["ready"] = False
        self.state["failed"] = False
        self.mpv.pause = True
        if start:
            self.mpv.loadfile(path, start=f"{start:.6f}")
        else:
            self.mpv.loadfile(path)

    def unload(self):
        if self.mpv is not None:
            self._clear_range()
            self.mpv.pause = True
            self._async("stop")

    def set_paused(self, paused):
        if self.mpv is None:
            return
        if not paused and self.at_end:
            self._async("seek", 0, "absolute+exact")   # play again from the top
        self.mpv.pause = paused

    def toggle_pause(self):
        self.set_paused(not self.paused)

    def seek(self, t, exact=True):
        if self.mpv is not None:
            self._async("seek", max(0.0, t), "absolute+exact" if exact else "absolute+keyframes")

    def seek_relative(self, delta):
        if self.mpv is not None:
            self._async("seek", delta, "relative+exact")

    def show_first_frame_from(self, t):
        """Shows the first frame at or after time t. (mpv's exact seek also
        takes a frame up to 5 ms before the target; 5 ms more rules that
        out, and no two frames are that close together.)"""
        self.seek(t + 0.005)

    def show_last_frame_before(self, t):
        """Shows the last frame that starts before time t: the last frame a
        cut ending at t keeps."""
        self.show_first_frame_from(t - self.frame_duration)

    def frame_step(self, forward=True):
        if self.mpv is not None:
            self._async("frame-step" if forward else "frame-back-step")

    def play_range(self, start, end):
        """Plays from start to end once, then stops on the last frame
        before end. mpv's own end option does the stopping, so it stops
        right there, not up to a poll later; it is cleared once playback
        stops (there, or paused earlier) and on the next load."""
        if self.mpv is None:
            return
        self.mpv["end"] = f"{end:.6f}"
        self._range = {"end": end, "since": time.monotonic(), "started": False}
        self.show_first_frame_from(start)
        self.mpv.pause = False

    def _check_range(self):
        """From _poll: is the play_range over?"""
        rng = self._range
        if rng is None or self.mpv is None:
            return
        if not self.paused:
            rng["started"] = True
            return
        # Paused: at the end, or by the user. (Right after play_range the
        # state can still say paused from before; give it a moment.)
        if rng["started"] or time.monotonic() - rng["since"] > 0.5:
            reached_end = bool(self.state.get("eof-reached"))
            self._clear_range()
            if reached_end:
                self.show_last_frame_before(rng["end"])

    def _clear_range(self):
        if self._range is not None:
            self._range = None
            if self.mpv is not None:
                self.mpv["end"] = "none"

    def set_volume(self, volume):
        """volume: 0-100 (percent)."""
        self.volume = max(0.0, min(100.0, float(volume)))
        if self.mpv is not None:
            self.mpv.volume = self.volume

    def set_muted(self, muted):
        self.muted = bool(muted)
        if self.mpv is not None:
            self.mpv.mute = self.muted

    def set_audio_track(self, track_id):
        if self.mpv is not None:
            self.set_audio_mix(None)
            self.mpv.aid = track_id

    def set_audio_delay(self, seconds):
        """Plays the audio this much later than the video (earlier when
        negative)."""
        if self.mpv is not None and seconds != self._audio_delay:
            self.mpv["audio-delay"] = seconds
            self._audio_delay = seconds

    def set_audio_mix(self, track_ids, volumes=None, filters=None):
        """Plays the audio tracks track_ids (mpv's ids) at once, summed as
        the Audio tab's mix does; None (or a single track) for one track
        the usual way. volumes: each track's gain (1.0 = as it is); with
        volumes, one track goes through the mix too, and none is silence.
        filters: with volumes, a filter chain for each track to go through
        before its volume ("" for none).
        mpv's lavfi-complex does the mixing; it can be set and cleared
        while a file plays (mpv restarts its audio for that, so it is only
        set when it changes)."""
        if self.mpv is None:
            return
        ids = list(track_ids or ())
        if volumes is not None and ids:
            chains = [f"[aid{i}]{f + ',' if f else ''}volume={g:.4f}"
                      for i, g, f in zip(ids, volumes, filters or [""] * len(ids))]
            if len(ids) == 1:
                graph = chains[0] + "[ao]"
            else:
                graph = (";".join(f"{c}[m{k}]" for k, c in enumerate(chains)) + ";"
                         + "".join(f"[m{k}]" for k in range(len(ids)))
                         + f"amix=inputs={len(ids)}:normalize=0[ao]")
        elif len(ids) > 1:
            graph = "".join(f"[aid{i}]" for i in ids) + f"amix=inputs={len(ids)}:normalize=0[ao]"
        else:
            graph = None
        if graph is not None:
            if graph != self._mix_graph:
                self.mpv["lavfi-complex"] = graph
                self._mix_graph = graph
            self.audio_mix = ids
        elif volumes is not None:
            # Nothing to play: silence.
            if self._mix_graph or self.audio_mix != []:
                self.mpv["lavfi-complex"] = ""
                self._mix_graph = ""
                self.mpv.aid = "no"
            self.audio_mix = []
        elif self.audio_mix is not None:
            self.mpv["lavfi-complex"] = ""
            self._mix_graph = ""
            self.audio_mix = None
            self.mpv.aid = ids[0] if ids else "auto"

    def set_video_crop(self, spec):
        """Shows only part of the video: spec "WxH+X+Y" in the video's own
        pixels, or "" for all of it."""
        if self.mpv is not None:
            self.mpv.video_crop = spec

    def set_video_rotate(self, degrees):
        """Turns the video clockwise by degrees (0/90/180/270), on top of the
        file's own rotation. video-crop stays in the stored frame's pixels."""
        if self.mpv is not None:
            self.mpv.video_rotate = degrees

    def show_overlay(self, ass_events, width, height):
        """Draws ASS events (one per line) over the video, in a coordinate
        space of width x height spanning the player's frame."""
        if self.mpv is not None:
            self._async("osd-overlay", 1, "ass-events", ass_events, width, height, 0, "no", "no")

    def hide_overlay(self):
        if self.mpv is not None:
            self._async("osd-overlay", 1, "none", "", 0, 0, 0, "no", "no")


def edl_url(segments):
    """An mpv edl:// URL that plays segments back to back as one video.
    Each segment is (path, start, length): length seconds from start, on
    the file's clock (the one ffmpeg -ss counts), or (path, None, None) for
    the whole file. Each path goes in with its length in bytes (%n%path),
    so commas and semicolons in it need no escaping."""
    entries = []
    for path, start, length in segments:
        path = os.path.abspath(path).replace("\\", "/")
        entry = f"%{len(path.encode('utf-8'))}%{path}"
        if start is not None:
            entry += f",start={start:.6f},length={length:.6f}"
        entries.append(entry)
    return "edl://" + ";".join(entries)


class CenteredRow(tk.Frame):
    """A row of three frames: self.left at the left edge, self.right at the
    right edge and self.center centered in the whole row (or on one of its
    widgets, see center_on), moved aside only when the row is too narrow
    for that. Fill each one with pack/grid."""

    GAP = 8

    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        bg = kw.get("bg", "#f0f0f0")
        self.left, self.center, self.right = (tk.Frame(self, bg=bg) for _ in range(3))
        self.left.place(x=0, rely=0.5, anchor="w")
        self.right.place(relx=1.0, rely=0.5, anchor="e")
        self.center.place(x=0, rely=0.5, anchor="w")
        self._asked = self._center_x = None
        self.pivot = None
        for w in (self, self.left, self.center, self.right):
            w.bind("<Configure>", lambda e: self._arrange(), add="+")

    def center_on(self, widget):
        """Center the row on this widget of self.center, not on the whole
        center part."""
        self.pivot = widget
        widget.bind("<Configure>", lambda e: self._arrange(), add="+")

    def _arrange(self):
        parts = (self, self.left, self.center, self.right) + ((self.pivot,) if self.pivot else ())
        if not all(p.winfo_exists() for p in parts):
            return   # being destroyed: the parts go one by one
        gap = px(self.GAP)
        lw, cw, rw = (p.winfo_reqwidth() for p in (self.left, self.center, self.right))
        # place doesn't size the row; ask for room for all three side by side.
        need_w = lw + cw + rw + 2 * gap
        need_h = max(p.winfo_reqheight() for p in (self.left, self.center, self.right))
        if (need_w, need_h) != self._asked:
            self._asked = (need_w, need_h)
            self.configure(width=need_w, height=need_h)
        w = self.winfo_width()
        pivot = self.pivot
        if pivot is not None and pivot.winfo_ismapped():
            x = w // 2 - (pivot.winfo_x() + pivot.winfo_width() // 2)
        else:
            x = (w - cw) // 2
        x = max(lw + gap, min(x, w - rw - gap - cw))
        if x != self._center_x:
            self._center_x = x
            self.center.place_configure(x=x)


class PlayerAudioControls:
    """Mute (a speaker icon), volume and the audio track heard, for a
    VideoPlayer, packed left to right into parent: the Cut tab's and the
    Crop/Rotate tab's. The player plays every audio track at once (each
    video starts that way) or the one picked in the track menu, which is
    shown only for a video with more than one. on_change() follows a change
    of volume or mute made here; on_refresh(mixing, current track id, track
    ids), each time the controls show something new."""

    def __init__(self, app, parent, player, on_change, on_refresh=None):
        self.app, self.player = app, player
        self.on_change, self.on_refresh = on_change, on_refresh
        player.mix_all_audio = True

        # A speaker icon, clicked to mute / unmute.
        self.mute_btn = tk.Canvas(parent, width=px(26), height=px(22), bg="#f0f0f0",
                                  highlightthickness=0, cursor="hand2")
        self.mute_btn.pack(side="left", padx=(0, 2))
        self.mute_btn.bind("<Button-1>", lambda e: self.toggle_mute())
        self.hover = False
        self.icon = None    # what it shows: "muted", or "waves N"

        def hover(on):
            self.hover = on
            self.draw_speaker()

        self.mute_btn.bind("<Enter>", lambda e: hover(True), add="+")
        self.mute_btn.bind("<Leave>", lambda e: hover(False), add="+")
        Tooltip(self.mute_btn,
                lambda: f"{'Unmute' if self.player.muted else 'Mute'} (shortcut: M)")
        self.volume_slider = TimeSlider(parent, command=self.volume_changed, width=100, height=22)
        self.volume_slider.config_range(to=100.0)
        self.volume_slider.set(player.volume)
        app._style_volume_slider(self.volume_slider)
        self.volume_slider.pack(side="left")
        self.volume_slider.bind(
            "<MouseWheel>", lambda e: self.volume_step(5 if e.delta > 0 else -5))
        Tooltip(self.volume_slider, "Shortcuts: Up / Down, or the mouse wheel")
        self.volume_lbl = tk.Label(parent, bg="#f0f0f0", width=4, anchor="w")
        self.volume_lbl.pack(side="left")

        # Packed only while the video has more than one audio track.
        self.track_btn = tk.Button(parent, text="", command=self.post_track_menu)
        self.track_menu = tk.Menu(self.track_btn, tearoff=0)
        self.track_var = tk.IntVar(value=0)
        self._track_list = None       # the tracks the menu was built for
        self._shown = None
        self.refresh()

    def refresh(self):
        player = self.player
        tracks = player.audio_tracks
        key = [(t.get("id"), t.get("title"), t.get("lang"), t.get("codec")) for t in tracks]
        mixing = player.audio_mix is not None
        shown = (key, player.audio_track, mixing, player.muted, player.volume)
        if shown == self._shown:
            return   # called on every player change, ~30 times a second in playback
        self._shown = shown
        self.draw_speaker()
        self.volume_lbl.config(text=f"{int(round(player.volume))}%")
        if key != self._track_list:
            self._track_list = key
            self.track_menu.delete(0, "end")
            if len(tracks) > 1:
                self.track_menu.add_radiobutton(
                    label="All tracks together", variable=self.track_var, value=-1,
                    command=lambda: self.player.set_audio_mix(
                        [t["id"] for t in self.player.audio_tracks]))
                self.track_menu.add_separator()
            for t in tracks:
                self.track_menu.add_radiobutton(
                    label=describe_audio_track(t), variable=self.track_var, value=t["id"],
                    command=lambda tid=t["id"]: self.player.set_audio_track(tid))
        current = player.audio_track
        self.track_var.set(-1 if mixing else current if current is not None else 0)
        ids = [t["id"] for t in tracks]
        if self.on_refresh is not None:
            self.on_refresh(mixing, current, ids)
        if len(tracks) > 1:
            n = ids.index(current) + 1 if current in ids else 0
            text = (f"Audio: all {len(tracks)} ▾" if mixing else
                    f"Audio {n}/{len(tracks)} ▾" if n else f"Audio off/{len(tracks)} ▾")
            self.track_btn.config(text=text)
            if not self.track_btn.winfo_manager():
                self.track_btn.pack(side="left", padx=(px(8), 0))
        elif self.track_btn.winfo_manager():
            self.track_btn.pack_forget()

    def draw_speaker(self):
        player, c = self.player, self.mute_btn
        if player.muted:
            icon = "muted"
        else:
            icon = f"waves {0 if player.volume <= 0 else 1 if player.volume < 50 else 2}"
        self.icon = icon
        c.delete("all")
        k = px(100) / 100              # the drawing is laid out on a 26 x 22 grid
        color = self.app._theme_color("icon")
        if self.hover:
            c.create_rectangle(0, 0, 26 * k, 22 * k, fill=self.app._theme_color("icon_hover"),
                               width=0)
        muted, waves = icon == "muted", 0 if icon == "muted" else int(icon[-1])
        g = lambda *pts: [v * k for v in pts]  # noqa: E731
        if self.app.ICON_FONT_FAMILY in tkfont.families(c):
            # The Windows icon font's speakers: muted, then 0-3 waves.
            level = 0 if self.player.volume <= 0 else 1 + min(2, int(self.player.volume // 34))
            glyph = "" if muted else ""[level]
            c.create_text(*g(13, 11), text=glyph, fill=color,
                          font=(self.app.ICON_FONT_FAMILY, 12))
            return
        # Without that font (Linux): a drawn speaker, smooth with Pillow
        # (SmoothPainter), one image per state and theme, kept.
        canvas = c
        if PIL_AVAILABLE:
            key = (icon, color, k)
            cache = self.app.__dict__.setdefault("_speaker_icons", {})
            if key in cache:
                canvas.create_image(0, 0, anchor="nw", image=cache[key])
                return
            c = SmoothPainter(canvas, 26 * k, 22 * k)
        line = max(1.0, 1.6 * k)
        c.create_polygon(*g(3, 8.5, 7, 8.5, 12, 4, 12, 18, 7, 13.5, 3, 13.5), fill=color, outline=color)
        if muted:
            for y0, y1 in ((8, 14), (14, 8)):
                c.create_line(*g(15, y0, 21, y1), fill=color, width=line, capstyle="round")
        else:
            for r in (4, 7.5)[:waves]:
                c.create_arc(*g(12 - r, 11 - r, 12 + r, 11 + r), start=-50, extent=100,
                             style="arc", outline=color, width=line)
        if PIL_AVAILABLE:
            cache[key] = c.photo()
            canvas.create_image(0, 0, anchor="nw", image=cache[key])

    def post_track_menu(self):
        menu = self.track_menu
        if menu.index("end") is None:
            return
        dark = self.app._is_dark()
        menu.configure(DARK_MENU if dark else self.app._menu_light_colors)
        b = self.track_btn
        x, y = b.winfo_rootx(), b.winfo_rooty() + b.winfo_height()
        post_with_menu_margin(lambda: menu.tk_popup(x, y), DARK_MENU["background"] if dark else None)

    def toggle_mute(self):
        self.player.set_muted(not self.player.muted)
        self.on_change()

    def volume_changed(self, value):
        player = self.player
        player.set_volume(value)
        if player.muted and value > 0:
            player.set_muted(False)   # turning the volume up means wanting to hear it
        self.on_change()

    def volume_step(self, delta):
        self.player.set_volume(self.player.volume + delta)
        self.volume_slider.set(self.player.volume)
        self.volume_changed(self.player.volume)

    def set_from(self, volume, muted):
        """Gives the player (and shows) a volume and mute set elsewhere."""
        self.player.set_volume(volume)
        self.player.set_muted(muted)
        self.volume_slider.set(self.player.volume)
        self.refresh()


class Tooltip:
    """A hint shown just under the mouse pointer once it has rested on a
    widget for a moment, gone when the mouse leaves or clicks. It is a label placed in
    the widget's window rather than a window of its own, so it can never
    take the focus. text is a string or a function returning one (for a
    hint that follows the widget's state). Light colors, themed like any
    other widget."""
    DELAY_MS = 500

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self._after_id = None
        self.label = None
        self._pointer = None    # (x_root, y_root) of the mouse, from its events
        widget.tooltip = self
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Motion>", self._track, add="+")
        for sequence in ("<Leave>", "<ButtonPress>", "<Destroy>"):
            widget.bind(sequence, self.hide, add="+")

    def _schedule(self, event=None):
        self.hide()
        self._track(event)
        self._after_id = self.widget.after(self.DELAY_MS, self.show)

    def _track(self, event=None):
        # Where the mouse rests when the hint comes up; a shown hint stays put.
        if event is not None and self.label is None:
            self._pointer = (event.x_root, event.y_root)

    def show(self):
        self._after_id = None
        text = self.text() if callable(self.text) else self.text
        if not text or not self.widget.winfo_ismapped():
            return
        root = self.widget.winfo_toplevel()
        # Made afresh each time: the newest child is drawn over the others.
        self.label = tk.Label(root, text=text, bg="#f0f0f0", fg="#000000",
                              font=("Segoe UI", 9), padx=px(6), pady=px(2),
                              highlightthickness=1, highlightbackground="#828790",
                              highlightcolor="#828790")
        self.label.update_idletasks()
        w, h = self.label.winfo_reqwidth(), self.label.winfo_reqheight()
        if self._pointer is not None:
            # Just below the pointer's arrow; no room below: just above it.
            x = self._pointer[0] - root.winfo_rootx()
            top = self._pointer[1] - root.winfo_rooty()
            below, above = top + px(20), top - h - px(4)
        else:    # shown without a mouse event: under the widget
            x = self.widget.winfo_rootx() - root.winfo_rootx()
            top = self.widget.winfo_rooty() - root.winfo_rooty()
            below, above = top + self.widget.winfo_height() + px(3), top - h - px(3)
        y = below if below + h <= root.winfo_height() else above
        x = max(0, min(x, root.winfo_width() - w))
        self.label.place(x=x, y=max(0, y))

    def hide(self, event=None):
        if self._after_id is not None:
            self.widget.after_cancel(self._after_id)
            self._after_id = None
        if self.label is not None:
            self.label.destroy()
            self.label = None


class ClipPreview:
    """A player with its own controls, for watching clips back to back as
    one video (an edl_url): the Combine tab's videos in their joined
    order, a Bulk Cut row's ranges. The owner places self.frame; mpv starts
    on the first show(). The slider marks where the parts meet, and the
    label names the part on screen."""

    def __init__(self, parent, on_close):
        self.frame = tk.Frame(parent, bg="#f0f0f0")
        self.frame.rowconfigure(0, weight=1)
        self.frame.columnconfigure(0, weight=1)
        video = tk.Frame(self.frame, bg="black")
        video.grid(row=0, column=0, sticky="nsew")
        self.player = VideoPlayer(video, on_change=self._on_player_changed)
        self.player.frame.place(x=0, y=0, relwidth=1, relheight=1)
        self.player.frame.bind("<Button-1>", lambda e: self.toggle_play())

        self.slider = TimeSlider(self.frame, command=self._on_slide,
                                 on_release=self._on_slide_done)
        self.slider.grid(row=1, column=0, sticky="ew")
        row = tk.Frame(self.frame, bg="#f0f0f0")
        row.grid(row=2, column=0, sticky="ew", pady=(0, 2))
        tk.Button(row, text="✕ Close", command=on_close).pack(side="left", padx=(4, 8))
        self.buttons = []
        for text, command, width in (("|◀ Part", lambda: self.go_to_part(-1), None),
                                     ("▶ Play", self.toggle_play, 8),
                                     ("Part ▶|", lambda: self.go_to_part(1), None)):
            b = tk.Button(row, text=text, command=command, width=width, state="disabled")
            b.pack(side="left", padx=1)
            self.buttons.append(b)
        self.play_btn = self.buttons[1]
        Tooltip(self.play_btn, "Shortcut: Space")
        self.time_lbl = tk.Label(row, text="", bg="#f0f0f0", width=27, font=("Segoe UI", 9))
        self.time_lbl.pack(side="left", padx=6)
        # width=1: it takes the room left over (and is cut short with … to
        # fit it) instead of asking for as much as its text needs.
        self.part_lbl = tk.Label(row, text="", bg="#f0f0f0", anchor="w", width=1,
                                 font=("Segoe UI", 9))
        self.part_lbl.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.part_lbl.bind("<Configure>", lambda e: self._fit_part_label())
        self._part_font = tkfont.Font(font=self.part_lbl.cget("font"))
        self._part_text = ""
        self.labels = []     # one per part, shown while that part plays
        self.key = None      # what the owner says is loaded (show's key)
        self._duration = None

    @property
    def loaded(self):
        return self.key is not None and self.player.loaded

    def show(self, segments, labels, key, start=0.0, volume=100.0, muted=False):
        """Opens segments (see edl_url), paused at time start. Raises
        RuntimeError if the player can't be started."""
        self.player.set_volume(volume)
        self.player.set_muted(muted)
        self.player.load(edl_url(segments), start=start)
        self.labels = list(labels)
        self.key = key
        self._duration = None
        self._set_part_text(self.labels[0] if self.labels else "")

    def unload(self):
        self.key = None
        self.player.unload()
        self.time_lbl.config(text="")
        self._set_part_text("")
        for b in self.buttons:
            b.config(state="disabled")

    def _set_part_text(self, text):
        if text != self._part_text:
            self._part_text = text
            self._fit_part_label()

    def _fit_part_label(self):
        text, font = self._part_text, self._part_font
        room = self.part_lbl.winfo_width() - px(4)
        if room > 1 and font.measure(text) > room:
            while text and font.measure(text + "…") > room:
                text = text[:-1]
            text += "…"
        self.part_lbl.config(text=text)

    def part_lengths(self):
        """Each part's length in seconds as mpv plays it, or None until the
        video is open."""
        dur = self.player.state.get("duration")
        if not self.loaded or not dur:
            return None
        starts = self.player.part_starts
        return [b - a for a, b in zip(starts, starts[1:] + [dur])]

    def part_index(self, t):
        """The part playing at time t (0-based)."""
        starts = self.player.part_starts
        i = 0
        while i + 1 < len(starts) and starts[i + 1] <= t + 0.001:
            i += 1
        return i

    def _on_player_changed(self):
        p = self.player
        if self.key is None:
            return
        if p.failed:
            for b in self.buttons:
                b.config(state="disabled")
            self.time_lbl.config(text="")
            self._set_part_text("The player can't open this.")
            return
        if not p.loaded:
            return
        for b in self.buttons:
            b.config(state="normal")
        self.play_btn.config(text="▶ Play" if p.paused else "❚❚ Pause")
        dur = p.state.get("duration") or 0.0
        if dur != self._duration:
            self._duration = dur
            self.slider.config_range(to=max(dur, 0.01))
        self.slider.set_marks(p.part_starts[1:])
        pos = p.position
        if pos is None:
            return
        self.time_lbl.config(text=f"{seconds_to_ffmpeg_time(pos)} / "
                                  f"{seconds_to_ffmpeg_time(dur)}")
        if not self.slider.dragging:
            self.slider.set(pos)
        i = self.part_index(pos)
        self._set_part_text(self.labels[i] if i < len(self.labels) else "")

    def _on_slide(self, value):
        if self.loaded:
            self.player.seek(value, exact=False)   # keyframes while dragging: fast

    def _on_slide_done(self, value):
        if self.loaded:
            self.player.seek(value)                # then the exact frame

    def toggle_play(self):
        if self.loaded:
            self.player.toggle_pause()

    def seek_by(self, seconds):
        if self.loaded:
            self.player.seek_relative(seconds)

    def frame_step(self, forward):
        if self.loaded:
            self.player.frame_step(forward)

    def go_to_part(self, direction):
        """To the next part's first frame, or back to the start of this
        part (the previous one's when already within a second of it)."""
        if not self.loaded:
            return
        starts = self.player.part_starts
        pos = self.player.position or 0.0
        i = self.part_index(pos)
        if direction > 0:
            if i + 1 >= len(starts):
                return
            target = starts[i + 1]
        else:
            target = starts[i] if pos - starts[i] > 1.0 else starts[max(0, i - 1)]
        self.player.show_first_frame_from(target)


class FileTable:
    """The Combine and Bulk Cut lists: a table of video files where each
    row is a thumbnail, the row's number and the file name, then the
    owner's columns. Ctrl/Shift-click selects several rows, and Delete
    calls the owner's on_delete. Rows are reordered with move(±1), or by
    dragging: a ghost of the dragged rows follows the cursor, a line shows
    where they will land, and they move there on release (Esc cancels).
    After every move on_reorder() is called; order() is then the list's
    new order. The owner keeps its own list of what the rows stand for."""

    STYLE = "Files.Treeview"

    def __init__(self, parent, root, columns, thumb_size, get_thumbnail,
                 on_reorder, on_delete, empty_text, name_width=340):
        """columns: (id, heading, width, anchor) for each column after the
        file name. get_thumbnail(path) is a PhotoImage, or None while there
        is none (thumbnail_ready brings it later). root is the window the
        drag ghost is drawn in."""
        self.root = root
        self.get_thumbnail = get_thumbnail
        self.on_reorder = on_reorder
        self.on_delete = on_delete
        self.paths = {}            # iid -> path
        self.frame = tk.Frame(parent, bg="#f0f0f0")
        self.frame.rowconfigure(0, weight=1)
        self.frame.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(self.frame, columns=[c[0] for c in columns],
                                 show="tree headings", selectmode="extended", style=self.STYLE)
        self.tree.heading("#0", text="File Name", anchor="w")
        self.tree.column("#0", width=px(name_width), anchor="w")
        for cid, heading, width, anchor in columns:
            self.tree.heading(cid, text=heading)
            self.tree.column(cid, width=width, anchor=anchor)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(self.frame, command=self.tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=vsb.set)
        # Rows without their thumbnail yet keep its room, so the names
        # don't shift when it comes.
        self.blank = tk.PhotoImage(width=thumb_size[0], height=thumb_size[1])
        # In the field's color: FIELD_BG is themed to the dark field.
        self.empty_lbl = tk.Label(self.frame, text=empty_text, bg=FIELD_BG, fg="#777777",
                                  anchor="w", font=("Segoe UI", 10))
        self.tree.bind("<ButtonPress-1>", self._press)
        self.tree.bind("<B1-Motion>", self._motion)
        self.tree.bind("<ButtonRelease-1>", self._release)
        self.tree.bind("<Delete>", self._delete_key)
        self.tree.bind("<Escape>", lambda e: self._end_drag(drop=False))
        self._press_at = None      # (row, x, y) of the button press on a row
        self._keep_selection = False
        self._drag = None          # while dragging: {"rows", "ghost", "line", "gap"...}
        self._update_empty()

    # ---- rows ------------------------------------------------------------
    def insert(self, path, values):
        iid = self._insert(path, values)
        self.renumber()
        self._update_empty()
        return iid

    def replace(self, rows):
        """All the rows anew: rows is (path, values) for each one. Returns
        their ids."""
        self.tree.delete(*self.tree.get_children())
        self.paths = {}
        iids = [self._insert(path, values) for path, values in rows]
        self.renumber()
        self._update_empty()
        return iids

    def _insert(self, path, values):
        iid = self.tree.insert("", "end", image=self.get_thumbnail(path) or self.blank,
                               values=values)
        self.paths[iid] = path
        return iid

    def delete(self, iids):
        iids = [i for i in iids if self.tree.exists(i)]
        if iids:
            self.tree.delete(*iids)
        for iid in iids:
            self.paths.pop(iid, None)
        self.renumber()
        self._update_empty()

    def order(self):
        return list(self.tree.get_children())

    def selected(self):
        """The selected rows, top to bottom."""
        sel = set(self.tree.selection())
        return [i for i in self.order() if i in sel]

    def select(self, iids):
        self.tree.selection_set(iids)
        if iids:
            self.tree.focus(iids[0])
            self.tree.see(iids[0])

    def renumber(self):
        for n, iid in enumerate(self.tree.get_children(), 1):
            self.tree.item(iid, text=f"  {n}.  {os.path.basename(self.paths[iid])}")

    def thumbnail_ready(self, path, photo):
        for iid, p in self.paths.items():
            if p == path and self.tree.exists(iid):
                self.tree.item(iid, image=photo)

    def _update_empty(self):
        if self.tree.get_children():
            self.empty_lbl.place_forget()
        else:
            self.empty_lbl.place(in_=self.tree, x=px(10), y=px(34), relwidth=1, width=-px(20))

    # ---- moving rows -------------------------------------------------------
    def move(self, step):
        """Moves the selected rows one place up (step -1) or down (+1)."""
        rows = self.selected()
        order = self.order()
        if not rows or (step < 0 and order.index(rows[0]) == 0) \
                or (step > 0 and order.index(rows[-1]) == len(order) - 1):
            return
        for iid in (rows if step < 0 else reversed(rows)):
            self.tree.move(iid, "", self.tree.index(iid) + step)
        self.renumber()
        self.tree.see(rows[0] if step < 0 else rows[-1])
        self.on_reorder()

    def move_rows_to(self, rows, gap):
        """Moves rows (kept in their order) to gap: the place before row
        number gap of the current order (len(order) = the end)."""
        order = self.order()
        moving = set(rows)
        rows = [i for i in order if i in moving]
        rest = [i for i in order if i not in moving]
        before = sum(1 for i in order[:gap] if i not in moving)
        new = rest[:before] + rows + rest[before:]
        if new == order:
            return
        for pos, iid in enumerate(new):
            self.tree.move(iid, "", pos)
        self.renumber()
        self.on_reorder()

    def _delete_key(self, event):
        if self.tree.selection():
            self.on_delete()
        return "break"

    # ---- dragging ----------------------------------------------------------
    def _press(self, event):
        self._end_drag(drop=False)
        self._press_at = None
        iid = self.tree.identify_row(event.y)
        if not iid or self.tree.identify_region(event.x, event.y) not in ("tree", "cell"):
            return None
        self._press_at = (iid, event.x, event.y)
        self._keep_selection = False
        if iid in self.tree.selection() and not event.state & (0x1 | 0x4):
            # A press on a selected row keeps the whole selection, so it can
            # all be dragged; a click without a drag selects just this row
            # on release, as usual.
            self._keep_selection = True
            self.tree.focus(iid)
            self.tree.focus_set()
            return "break"
        return None    # Tk selects the row, with Ctrl/Shift as usual

    def _motion(self, event):
        if self._press_at is None:
            return None
        if self._drag is None:
            _iid, x0, y0 = self._press_at
            if abs(event.x - x0) + abs(event.y - y0) < px(6):
                return "break"
            self._start_drag()
        self._drag["xy"] = (event.x, event.y)
        self._drag["root_xy"] = (event.x_root, event.y_root)
        self._show_drag()
        return "break"

    def _release(self, event):
        pressed = self._press_at
        self._press_at = None
        if self._drag is not None:
            self._end_drag(drop=True)
            return "break"
        if pressed and self._keep_selection:
            self.select([pressed[0]])
        return None

    def _start_drag(self):
        rows = self.selected() or [self._press_at[0]]
        ghost = tk.Frame(self.root, bg="#f0f0f0", highlightthickness=px(1),
                         highlightbackground="#3d8bfd", highlightcolor="#3d8bfd")
        name = (os.path.basename(self.paths[rows[0]]) if len(rows) == 1
                else f"{len(rows)} files")
        tk.Label(ghost, image=self.get_thumbnail(self.paths[rows[0]]) or self.blank,
                 text=f"  {name}", compound="left", bg="#f0f0f0", fg="#000000",
                 font=("Segoe UI", 9), padx=px(4), pady=px(3)).pack()
        line = tk.Frame(self.frame, bg="#3d8bfd", height=px(2))
        self._drag = {"rows": rows, "ghost": ghost, "line": line, "gap": None,
                      "xy": None, "root_xy": None, "scroll_id": None}
        self.tree.configure(cursor="fleur")

    def _gap_at(self, y):
        """Where rows dropped at y (tree coordinates) would go, and the y of
        the line that shows it (None when that place is scrolled out of view)."""
        order = self.order()
        iid = self.tree.identify_row(y)
        if iid:
            x0, y0, w, h = self.tree.bbox(iid)
            gap = order.index(iid) + (1 if y >= y0 + h / 2 else 0)
        elif self.tree.identify_region(px(4), y) == "heading":
            first = next((i for i in order if self.tree.bbox(i)), None)
            gap = order.index(first) if first else 0
        else:
            gap = len(order)
        if gap < len(order):
            box = self.tree.bbox(order[gap])
            line_y = box[1] if box else None
        else:
            box = self.tree.bbox(order[-1]) if order else None
            line_y = box[1] + box[3] if box else None
        return gap, line_y

    def _show_drag(self):
        d = self._drag
        x, y = d["xy"]
        gap, line_y = self._gap_at(y)
        d["gap"] = gap
        if line_y is None:
            d["line"].place_forget()
        else:
            d["line"].place(in_=self.tree, x=0, y=max(0, line_y - px(1)), relwidth=1)
        rx, ry = d["root_xy"]
        d["ghost"].place(x=rx - self.root.winfo_rootx() + px(14),
                         y=ry - self.root.winfo_rooty() + px(10))
        # Held near the top or bottom edge, the list scrolls.
        h = self.tree.winfo_height()
        near = -1 if self.tree.identify_region(px(4), y) == "heading" or y < px(8) else \
            1 if y > h - px(12) else 0
        if near and d["scroll_id"] is None:
            d["scroll_id"] = self.tree.after(80, self._autoscroll)
        d["near"] = near

    def _autoscroll(self):
        d = self._drag
        if d is None:
            return
        d["scroll_id"] = None
        if d.get("near"):
            self.tree.yview_scroll(d["near"], "units")
            self._show_drag()

    def _end_drag(self, drop):
        d = self._drag
        if d is None:
            return
        self._drag = None
        self._press_at = None
        if d["scroll_id"] is not None:
            self.tree.after_cancel(d["scroll_id"])
        d["ghost"].destroy()
        d["line"].destroy()
        self.tree.configure(cursor="")
        if drop and d["gap"] is not None:
            self.move_rows_to(d["rows"], d["gap"])
            self.select(d["rows"])


if os.name == "nt":
    from ctypes import wintypes as _wintypes
    # A private user32 handle, so these argtypes don't affect other code.
    _user32 = ctypes.WinDLL("user32")
    _ENUM_CHILD_PROC = ctypes.WINFUNCTYPE(_wintypes.BOOL, _wintypes.HWND, _wintypes.LPARAM)
    _user32.EnumChildWindows.argtypes = [_wintypes.HWND, _ENUM_CHILD_PROC, _wintypes.LPARAM]
    _user32.EnableWindow.argtypes = [_wintypes.HWND, _wintypes.BOOL]


# --------------------------------------------------------------------------
# Main application
# --------------------------------------------------------------------------

class LosslessVideoApp(_BaseTk):
    def __init__(self):
        enable_dpi_awareness()  # before super().__init__() creates the window
        super().__init__()
        global UI_SCALE
        UI_SCALE = max(1.0, round(self.winfo_fpixels("1i") / 96.0, 2))
        # The list thumbnails' size, scaled like everything else.
        self.THUMB_SIZE = (px(self.THUMB_SIZE[0]), px(self.THUMB_SIZE[1]))
        self.title("BarkBark Lossless Video Cutter")
        # 900x700 at 100% scaling, never bigger than the screen at high scaling.
        # Plus the app-drawn menu bar, which is inside this size where the
        # native one (20 px at 100%) was outside it, so the tabs get as much
        # room as they always had.
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        win_w = min(px(900), int(screen_w * 0.95))
        win_h = min(px(700 + 20), int(screen_h * 0.9))
        self.geometry(f"{win_w}x{win_h}")
        # The real minimum window size, enforced. 860 wide: the Audio tab's
        # footer text (wrapped at 850) needs 840-851 at 100-200% scaling.
        self.minsize(min(px(860), win_w), min(px(560 + 20), win_h))

        self.input_path = None
        self.duration = 0.0
        self.cut_video_start = 0.0      # first video frame's time (_load_video_for_cut)
        # The video last opened on Cut, Crop/Rotate or Audio, which the other
        # two open when shown; tab index -> the shared video it last tried.
        self.shared_video = None
        self._shared_tried = {}
        self.file_size = 0
        self.start_sec = 0.0
        self.end_sec = 0.0
        self.active_handle = "end"
        self.raw_preview_img = None      # full-res PIL image of current frame
        self.preview_photo = None        # keep ref so Tk doesn't GC it
        self._preview_after_id = None    # pending debounce timer for scrubbing
        self._preview_request_id = 0     # monotonic id to discard stale/late frames
        self.cut_keyframes = []          # keyframe times of the Cut video (_start_keyframe_scan)
        self._keyframe_cache = {}        # (path, size, mtime) -> keyframe times, this session
        self._keyframe_proc = None       # the running scan's ffprobe
        self._keyframe_gen = 0           # bumped per scan, so a stale scan's results are dropped
        self._keyframe_xs = (None, [])   # (what they were computed for, tick x positions)
        self._wave_cache = {}            # (path, size, mtime, track) -> waveform mask, this session
        self._wave_proc = None           # the running render's ffmpeg
        self._wave_gen = 0               # bumped per render, so a stale render is dropped
        self._wave_track = None          # the audio track (0-based) shown or being drawn
        self._wave_mask = None           # its picture: PIL "L", white where there is sound
        self._wave_note = ""             # shown instead while drawing, or when it can't be drawn
        self._wave_sized = (None, None)  # (size, the mask resized to it)
        self._wave_photo = (None, None)  # (what the strip's image was made for, PhotoImage)
        # The scenes marked on the slider are the open video's ranges waiting
        # on the Bulk Cut list; see the cut_ranges property.

        # Resize/Crop tab state
        self.resize_input_path = None
        self.resize_duration = 0.0
        # The video's size as it is shown, i.e. after its rotation and the
        # Rotate buttons' turn: the still frames, the player and ffmpeg's
        # crop filter all work on the turned frame, so the crop box does too.
        self.resize_src_w = 0
        self.resize_src_h = 0
        self.resize_rotation = 0         # clockwise turn the file asks for, on the stored frames
        self.resize_user_rotation = 0    # clockwise turn picked with the Rotate buttons, on top
        self.resize_rotate_buttons = {}  # 0/90/180/270 -> its button
        self.resize_frame_raw = None     # the still frame as ffmpeg gives it (file's turn only)
        self.resize_frame_img = None     # ...and turned by the Rotate buttons' turn too
        self.resize_frame_photo = None
        self.resize_crop = None          # x0, y0, x1, y1 in (shown) video pixels
        self.resize_drag_mode = None     # "move", "nw", "ne", "sw", "se", "new"
        self.resize_drag_start = None
        self.resize_crop_start = None
        self.resize_display_rect = None  # x0, y0, x1, y1 on canvas
        self.resize_after_id = None
        self.resize_request_id = 0
        self.resize_time_sec = 0.0
        self.resize_zoom = 1.0            # 1.0 = fit-to-canvas; higher = zoomed in
        self.resize_entry_vars = {}       # "x0"/"y0"/"x1"/"y1" -> tk.StringVar
        self.resize_aspect_lock = None    # (w, h) ratio corner drags keep, or None = Freeform
        self.resize_aspect_key = None     # the Aspect menu's pick: "freeform", "original", "16:9"...
        self._resize_bg_image_id = None   # cached canvas image item (perf)
        self._resize_bg_cache_key = None  # (frame id, dw, dh) the cached photo was built for
        # With libmpv, the video plays under the crop box: a player placed
        # over the picture's area of the canvas (_resize_place_player),
        # which draws the box too (_resize_draw_player_overlay).
        self.resize_player = None
        self.resize_audio = None          # its sound's controls (PlayerAudioControls)
        self._resize_player_on = False    # the player (not a still frame) shows the video
        self._resize_view = None          # where the player is placed, and its video-crop

        # Audio tab state
        self.audio_input_path = None
        self.audio_duration = 0.0
        self.audio_frame_img = None       # current preview frame (PIL)
        self.audio_frame_photo = None
        self._audio_bg_image_id = None
        self._audio_bg_cache_key = None
        self.audio_time_sec = 0.0
        self.audio_after_id = None        # pending debounce timer for scrubbing
        self.audio_request_id = 0
        self.audio_playing = False
        self.audio_play_after_id = None
        self.audio_ffplay_proc = None      # subprocess.Popen of the audio-only ffplay player
        self.audio_ffmpeg_pipe_proc = None  # subprocess.Popen of the ffmpeg mixer feeding it
        self._audio_restart_after_id = None  # debounce for restarting playback after a volume/track change
        self.audio_player = None          # VideoPlayer over the preview (None without libmpv)
        self._audio_player_on = False     # the player shows the video (else still frames + ffplay)
        self._audio_wave_scrubbing = False  # the mouse is moving the playback on a waveform
        self.audio_tracks = []            # list of dicts, see _load_video_for_audio
        self._audio_note = None           # shown instead of the open prompt (_audio_show_no_audio)
        # Noise reduction (DENOISE_LEVELS). The DeepFilterNet levels are
        # jobs run one at a time in the background, for the loaded video.
        self._denoise_files = None        # RNNOISE_MODEL / DEEP_FILTER_EXE -> path or None
        self._dfn_session_dir = None      # this run's temp folder for the cleaned WAVs
        self._dfn_video_dir = None        # the loaded video's folder in it
        self._dfn_done = {}               # (a_index, level) -> the cleaned WAV
        self._dfn_failed = {}             # (a_index, level) -> why it failed
        self._dfn_queue = []              # (a_index, level) waiting their turn
        self._dfn_job = None              # the running job (dict, _dfn_start_next)
        self._dfn_jobs_started = 0
        self._dfn_waiters = []            # (keys, callback, on_fail) for Finalize / Auto Balance
        self._dfn_rate = 1.0              # last run's time against its estimate
        self._dfn_tick_id = None
        self._audio_added_wavs = set()    # cleaned WAVs given to the player for this file

        # Thumbnails for the Combine and Bulk Cut lists, which share them.
        self.thumb_cache = {}            # path -> PhotoImage, or "" when it couldn't be made
        self.thumb_pending = set()       # paths with an extraction already queued
        # Few workers on purpose: decoding a frame from several large videos
        # at once just makes them all fight over disk/CPU and finish later.
        self.thumb_pool = ThreadPoolExecutor(max_workers=2)

        self.combine_files = []          # list of paths queued to join
        self.combine_table = None        # FileTable, once the tab is built
        self.combine_info = {}           # path -> (length, video, audio) texts, once probed
        self.combine_info_pending = set()
        self._combine_formats = {}       # path -> ((mtime, size), probe_join_format result)
        self._combine_match_id = None    # pending re-check of whether the list matches
        self.combine_preview = None      # ClipPreview of the joined videos, made on first use
        self._combine_preview_sync_id = None

        self.bulk_items = []             # list of dicts: path, ranges, status, iid
        self._bulk_lock = threading.Lock()
        self._bulk_running = False
        self.bulk_preview = None         # ClipPreview of a row's ranges, made on first use
        self.bulk_table = None           # FileTable, once the tab is built

        self.warn_overwrite_var = tk.BooleanVar(value=True)
        # How the Cut and Bulk Cut tabs cut (Cutting Mode menu): "classic" (Lossless),
        # "reencode" (Re-encode edges) or "editlist" (Edit-list trim).
        self.cut_mode_var = tk.StringVar(value="classic")
        self.cut_search_window = self.DEFAULT_SEARCH_WINDOW
        self._search_window_dialog = None   # the box, while it's open
        self._progress_jobs = {}         # running job name -> time.monotonic() it started

        self.settings = load_settings()
        self.dark_mode_var = tk.BooleanVar(value=bool(self.settings.get("dark_mode", True)))
        self.show_frame_buttons_var = tk.BooleanVar(
            value=bool(self.settings.get("show_frame_buttons", False)))
        # Every tab saves next to the source video, or asks where to save.
        self.save_to_source_var = tk.BooleanVar(
            value=bool(self.settings.get("save_to_current_folder", True)))
        self._ttk_light_theme = ttk.Style(self).theme_use()
        self._set_treeview_row_height(ttk.Style(self))
        self._hide_notebook_tabs(ttk.Style(self))

        self._build_menu()

        # Its tabs are chosen with the boxes on the menu bar (_build_tab_boxes).
        self.notebook = ttk.Notebook(self, style=self.NOTEBOOK_STYLE)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.cut_tab = tk.Frame(self.notebook, bg="#f0f0f0")
        self.combine_tab = tk.Frame(self.notebook, bg="#f0f0f0")
        self.bulk_tab = tk.Frame(self.notebook, bg="#f0f0f0")
        self.resize_tab = tk.Frame(self.notebook, bg="#f0f0f0")
        self.audio_tab = tk.Frame(self.notebook, bg="#f0f0f0")
        self.notebook.add(self.cut_tab, text="Cut Clip")
        self.notebook.add(self.combine_tab, text="Combine Videos")
        self.notebook.add(self.bulk_tab, text="Bulk Cut")
        self.notebook.add(self.resize_tab, text="Crop/Rotate")
        self.notebook.add(self.audio_tab, text="Audio")

        # Lazy tab construction: only the Cut tab (the one visible at
        # launch) is built eagerly. Combine/Bulk/Resize/Audio each build a
        # non-trivial widget tree (canvases, sliders, per-track rows...),
        # so building all five up front was the main cost of starting the
        # app. Building each one for the first time only when its tab is
        # actually selected (or targeted via the File menu / a drop) keeps
        # startup down to just the one tab the user actually sees first.
        self._tab_build_fns = [
            None,  # Cut Clip -- built eagerly below
            self._build_combine_tab,
            self._build_bulk_tab,
            self._build_resize_tab,
            self._build_audio_tab,
        ]
        self._tab_built = [True, False, False, False, False]

        self._build_cut_tab()
        self._build_status_bar()

        self._set_cut_controls_enabled(False)
        self._enable_drag_and_drop()
        self.notebook.bind("<<NotebookTabChanged>>", self._on_notebook_tab_changed)
        self.protocol("WM_DELETE_WINDOW", self._on_app_close)

        # Widgets created later (lazily built tabs, rebuilt list rows) pick
        # up the current theme the moment they are first shown.
        self.bind_all("<Map>", self._on_widget_mapped, add="+")
        if self.dark_mode_var.get():
            self._apply_theme()

        # A newer release, looked for once the window is up.
        self._update_busy = False         # checking or downloading
        self._update_dialog = None
        self.after(3000, self._auto_check_for_update)

    def _on_notebook_tab_changed(self, event=None):
        idx = self.notebook.index("current")
        self._style_tab_boxes()
        # No sound from a tab out of sight.
        if idx != 0 and self._cut_player_on:
            self.cut_player.set_paused(True)
        for tab, player in self._tab_players():
            if tab != idx:
                player.set_paused(True)
        if idx != 4 and self.audio_playing:
            self._audio_pause()
        if idx == 3 and getattr(self, "resize_audio", None) is not None:
            # The volume and mute the Cut tab keeps, maybe changed there.
            self.resize_audio.set_from(*self._player_audio_settings())
        self._ensure_tab_built(idx)
        self._show_shared_video(idx)

    def open_video_from_menu(self):
        """File > Open Video: opens on the Crop/Rotate or Audio tab when that
        is showing, otherwise on the Cut tab. The other two get it too
        (_share_video)."""
        idx = self.notebook.index("current")
        if idx == 3:
            self.open_file_for_resize()
        elif idx == 4:
            self.open_file_for_audio()
        else:
            self.notebook.select(self.cut_tab)
            self.open_file_for_cut()

    def _share_video(self, tab, path):
        """path was opened on tab (Cut, Crop/Rotate or Audio): the other two
        open it too, each when it is next shown."""
        self.shared_video = path
        self._shared_tried[tab] = path

    def _show_shared_video(self, tab):
        """Opens the shared video on tab, once: a tab that couldn't open it,
        or has since opened another one, isn't made to again."""
        path = self.shared_video
        if tab not in (0, 3, 4) or path is None or self._shared_tried.get(tab) == path:
            return
        self._shared_tried[tab] = path
        if not os.path.isfile(path):
            return
        load = {0: self._load_video_for_cut, 3: self._load_video_for_resize,
                4: self._load_video_for_audio}[tab]
        # After the tab is drawn, so its player gets its real size.
        self.after_idle(lambda: load(path, quiet=True))

    def _tab_players(self):
        """(tab index, VideoPlayer) of the started players on tabs other
        than Cut."""
        found = []
        if self.combine_preview is not None:
            found.append((1, self.combine_preview.player))
        if self.bulk_preview is not None:
            found.append((2, self.bulk_preview.player))
        if self.resize_player is not None:
            found.append((3, self.resize_player))
        if self.audio_player is not None:
            found.append((4, self.audio_player))
        return found

    def _player_audio_settings(self):
        """The volume and mute set on the Cut tab, which the other tabs'
        players use too: (volume, muted)."""
        if self.cut_player is not None:
            return self.cut_player.volume, self.cut_player.muted
        return float(self.settings.get("player_volume", 100.0)), bool(self.settings.get("player_muted", False))

    def _ensure_tab_built(self, idx):
        if self._tab_built[idx]:
            return
        self._tab_built[idx] = True
        self._tab_build_fns[idx]()

    def _on_app_close(self):
        # Otherwise a still-playing ffplay child (spawned by the Audio tab)
        # is left running after the window closes.
        self._audio_stop_ffplay()
        self._stop_keyframe_scan()
        self._stop_waveform()
        if self.cut_player is not None:
            if self._cut_audio_save_id is not None:   # a volume change not saved yet
                self.after_cancel(self._cut_audio_save_id)
                self._save_cut_audio_settings()
            self.cut_player.terminate()
        for _tab, player in self._tab_players():
            player.terminate()
        self._dfn_shutdown()     # after the player has let go of the cleaned WAVs
        self.destroy()

    # ====================================================================
    # Menu
    # ====================================================================
    def _build_menu(self):
        """The menu bar is drawn by the app (a strip of menubuttons) rather
        than being the native Windows menu bar, which Windows 10 always
        paints light, so it can follow the light / dark theme. Each button
        opens an ordinary Tk dropdown menu."""
        self.menu_bar = tk.Frame(self, bg=self._theme_color("menubar_bg"))
        self.menu_bar.pack(side="top", fill="x")
        # The empty part of the bar moves the window, like a title bar.
        self._bind_window_drag(self.menu_bar, lambda e: True)
        self._menu_bar_items = []        # (menubutton, its dropdown menu)

        file_menu = self._add_menu_bar_menu("File")
        file_menu.add_command(label="Open Video...", command=self.open_video_from_menu)
        file_menu.add_command(label="Add Videos (Combine tab)...", command=self.add_files_for_combine)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_app_close)

        # For the Cut and Bulk Cut tabs. Choosing Re-encode edges or Edit-list
        # trim opens a box asking for the keyframe search window.
        self.cut_menu = self._add_menu_bar_menu("Cutting Mode")
        for label, mode, command in (
                ("Lossless (recommended — fastest, may be a few ms off)", "classic", None),
                ("Re-encode edges (slower, frame-accurate)...", "reencode",
                 self._ask_search_window),
                ("Edit-list trim (experimental — may not work on some players)...", "editlist",
                 self._ask_search_window)):
            self.cut_menu.add_radiobutton(label=label, variable=self.cut_mode_var, value=mode,
                                          command=command)

        options_menu = self._add_menu_bar_menu("Options")
        options_menu.add_checkbutton(
            label="Dark mode", variable=self.dark_mode_var, command=self._on_dark_mode_toggled,
        )
        options_menu.add_separator()
        options_menu.add_checkbutton(
            label="Save to current folder",
            variable=self.save_to_source_var,
            command=lambda: self._save_option("save_to_current_folder", self.save_to_source_var),
        )
        options_menu.add_checkbutton(
            label="Warn before overwriting existing files",
            variable=self.warn_overwrite_var,
        )
        options_menu.add_checkbutton(
            label="Show frame by frame movement buttons in the video player",
            variable=self.show_frame_buttons_var,
            command=self._on_show_frame_buttons_toggled,
        )

        help_menu = self._add_menu_bar_menu("Help")
        help_menu.add_command(label="Check for updates...", command=self._check_for_update)
        help_menu.add_separator()
        help_menu.add_command(label="About", command=self._show_about)

        self._build_tab_boxes()

        # The system menu colors, put back on the dropdowns in light mode.
        self._menu_light_colors = {opt: str(file_menu.cget(opt)) for opt in DARK_MENU}
        self._style_menu_bar()

        # Keyboard: Alt + the underlined letter opens that menu, F10 the
        # first one. Tk forwards every other Alt key and F10 to Windows
        # (Alt+F4, Alt+Space...); a menu letter is no longer forwarded,
        # since with no native menu bar Windows would only beep at it.
        self.bind_all("<Alt-KeyPress>", self._on_menu_bar_alt_key)
        self.bind_all("<KeyPress-F10>", lambda e: self._post_menu_bar_menu(
            self._menu_bar_items[0][0]))
        self.bind_all("<KeyRelease-F10>", lambda e: "break")

    def _add_menu_bar_menu(self, label):
        """A menubutton on the menu bar plus its dropdown menu (returned)."""
        button = tk.Menubutton(
            self.menu_bar, text=label, underline=0, font="TkMenuFont",
            relief="flat", borderwidth=0, highlightthickness=0,
            padx=px(7), pady=px(3),
        )
        # Tk wants a menubutton's menu to be its child.
        menu = tk.Menu(button, tearoff=0)
        button.configure(menu=menu)
        button.pack(side="left")
        # Posted here, not by the class binding: on Windows the menu is
        # open for as long as the post call runs, and Tk's own binding
        # never clears its "posted" mark afterwards when a click (rather
        # than a drag) chose the item.
        button.bind("<ButtonPress-1>", lambda e: self._post_menu_bar_menu(button))
        self._menu_bar_items.append((button, menu))
        return menu

    def _post_menu_bar_menu(self, button):
        """Opens button's dropdown below it. On Windows this returns only
        once the menu has closed again."""
        # Highlighted while its menu is open, also when opened by keyboard.
        button.configure(state="active")
        margin = DARK_MENU["background"] if self._is_dark() else None
        post_with_menu_margin(lambda: self.tk.call("tk::MbPost", button), margin)
        try:
            self.tk.call("tk::MenuUnpost", "")
            under = self.tk.call("winfo", "containing", *self.winfo_pointerxy())
            if str(under) != str(button):
                button.configure(state="normal")
        except tk.TclError:
            pass  # the chosen item closed the app (File > Exit)
        return "break"

    def _on_menu_bar_alt_key(self, event):
        char = (event.keysym if len(event.keysym) == 1 else event.char or "").lower()
        for button, _menu in self._menu_bar_items:
            text = button.cget("text")
            if char and text[int(button.cget("underline"))].lower() == char:
                return self._post_menu_bar_menu(button)
        # Not a menu letter: what Tk itself does with Alt keys on Windows.
        self.tk.call("tk::WinMenuKey", event.widget, event.keysym_num)
        return None

    def _style_menu_bar(self):
        """Colors the menu bar and its dropdowns for the current theme.
        Their colors are none of the light chrome ones, so the general
        dark-mode swap (_theme_widget) leaves them to this."""
        bg = self._theme_color("menubar_bg")
        fg = self._theme_color("menubar_fg")
        self.menu_bar.configure(background=bg)
        dropdown = DARK_MENU if self._is_dark() else self._menu_light_colors
        for button, menu in self._menu_bar_items:
            button.configure(
                background=bg, foreground=fg, activeforeground=fg,
                activebackground=self._theme_color("menubar_hover"),
            )
            menu.configure(dropdown)
            for sub in menu.winfo_children():   # submenus, e.g. Options' picker
                if isinstance(sub, tk.Menu):
                    sub.configure(dropdown)
        self._style_tab_boxes()

    # The tabs, as boxes on the menu bar: (box label, the tab's full name,
    # its icon in Segoe MDL2 Assets, Windows 10's icon font), in the order
    # shown, each with its tab's index in the notebook.
    TAB_BOXES = (
        (0, "Cut", "Cut Clip", "\ue8c6"),               # scissors
        (2, "Bulk Cut", "Bulk Cut", "\ue8b9"),          # a stack of clips
        (1, "Combine", "Combine Videos", "\ue71b"),     # chain links
        (3, "Crop/Rotate", "Crop/Rotate", "\ue7a8"),    # crop corners
        (4, "Audio", "Audio", "\ue8d6"),                # notes
    )
    ICON_FONT_FAMILY = "Segoe MDL2 Assets"
    NOTEBOOK_STYLE = "Boxed.TNotebook"   # the notebook, its own tabs hidden

    @classmethod
    def _hide_notebook_tabs(cls, style):
        """No tab strip on the notebook: the menu bar's boxes choose its
        tabs. Layouts belong to a theme, so this runs again after a switch."""
        style.layout(cls.NOTEBOOK_STYLE + ".Tab", [])

    def _build_tab_boxes(self):
        """The tab boxes, in the middle of the menu bar (_place_tab_boxes).
        Each is a framed icon + name; a click opens its tab. Where they
        don't fit next to the menus, only the icons show (the name comes
        up when the mouse rests on one)."""
        bar = self.tab_box_bar = tk.Frame(self.menu_bar)
        has_icons = self.ICON_FONT_FAMILY in tkfont.families(self)
        # (box, icon label or None, name label), by notebook index.
        self._tab_boxes = [None] * len(self.TAB_BOXES)
        for idx, label, name, glyph in self.TAB_BOXES:
            box = tk.Frame(bar, highlightthickness=px(1), cursor="hand2")
            box.pack(side="left", padx=px(3))
            icon = None
            if has_icons:
                icon = tk.Label(box, text=glyph, font=(self.ICON_FONT_FAMILY, 13), cursor="hand2")
                icon.pack(side="left", padx=(px(8), px(5)), pady=px(3))
            text = tk.Label(box, text=label, font="TkMenuFont", cursor="hand2")
            text.pack(side="left", padx=(0 if has_icons else px(10), px(10)), pady=px(3))
            for w in (box, icon, text):
                if w is not None:
                    w.bind("<ButtonPress-1>", lambda e, i=idx: self.notebook.select(i))
                    w.bind("<Enter>", lambda e: self._style_tab_boxes(), add="+")
                    w.bind("<Leave>", lambda e: self.after_idle(self._style_tab_boxes), add="+")
                    if w is not box:   # the labels cover the box
                        Tooltip(w, name)
            self._tab_boxes[idx] = (box, icon, text)
        # Between the boxes the bar still moves the window.
        self._bind_window_drag(bar, lambda e: True)
        self._tab_boxes_compact = False
        self._tab_boxes_full_width = None
        # Makes the bar tall enough for the boxes (they are placed, which
        # doesn't size it).
        self._tab_box_strut = tk.Frame(self.menu_bar, width=1, height=1)
        self._tab_box_strut.pack(side="left")
        self.menu_bar.bind("<Configure>", self._place_tab_boxes, add="+")
        bar.bind("<Configure>", self._place_tab_boxes, add="+")

    def _place_tab_boxes(self, event=None):
        """Centers the boxes on the window, or as near as the menus on the
        left allow; only icons when even that doesn't fit."""
        bar = self.tab_box_bar
        last = self._menu_bar_items[-1][0]
        left = last.winfo_x() + last.winfo_width() + px(16)
        room = self.menu_bar.winfo_width() - left - px(8)
        has_icons = self._tab_boxes[0][1] is not None
        if not self._tab_boxes_compact:
            self._tab_boxes_full_width = bar.winfo_reqwidth()
        if has_icons and last.winfo_width() > 1:
            compact = self._tab_boxes_full_width > room
            if compact != self._tab_boxes_compact:
                self._tab_boxes_compact = compact
                for _box, icon, text in self._tab_boxes:
                    if compact:
                        text.pack_forget()
                        icon.pack_configure(padx=px(8))
                    else:
                        icon.pack_configure(padx=(px(8), px(5)))
                        text.pack(side="left", padx=(0, px(10)), pady=px(3))
                return   # the bar's new size calls this again
        width = bar.winfo_reqwidth()
        x = max(left, (self.menu_bar.winfo_width() - width) // 2)
        info = bar.place_info()
        if info.get("x") != str(x):
            bar.place(x=x, rely=0.5, anchor="w")
        height = bar.winfo_reqheight() + px(6)
        if int(self._tab_box_strut.cget("height")) != height:
            self._tab_box_strut.configure(height=height)

    def _style_tab_boxes(self):
        """Colors the boxes: the open tab's highlighted, the one under the
        mouse lit up."""
        boxes = getattr(self, "_tab_boxes", None)
        if not boxes:
            return
        notebook = getattr(self, "notebook", None)
        current = notebook.index("current") if notebook is not None else 0
        try:
            x, y = self.winfo_pointerxy()
            under = self.winfo_containing(x, y)
        except (tk.TclError, KeyError):
            under = None
        bar_bg = self._theme_color("menubar_bg")
        fg = self._theme_color("menubar_fg")
        self.tab_box_bar.configure(background=bar_bg)
        self._tab_box_strut.configure(background=bar_bg)
        for idx, (box, icon, text) in enumerate(boxes):
            parts = [w for w in (box, icon, text) if w is not None]
            if idx == current:
                bg, border = self._theme_color("tabbox_selected"), self._theme_color("tabbox_selected_border")
                icon_fg = self._theme_color("tabbox_selected_icon")
            else:
                bg = self._theme_color("tabbox_hover") if under in parts else bar_bg
                border, icon_fg = self._theme_color("tabbox_border"), fg
            box.configure(background=bg, highlightbackground=border, highlightcolor=border)
            text.configure(background=bg, foreground=fg)
            if icon is not None:
                icon.configure(background=bg, foreground=icon_fg)

    # ====================================================================
    # Updates (the module's "Updates" section has the GitHub side)
    # ====================================================================
    def _auto_check_for_update(self):
        """A little after the built app starts, at most once a day: a
        newer release (not one skipped) is offered. Silent when there's
        none or GitHub can't be reached."""
        if not running_built_app():
            return    # run from source: updated with git, not by itself
        if not self.settings.get("update_auto", True):
            return    # turned off in the update box
        last = self.settings.get("update_checked_at")
        if isinstance(last, (int, float)) and 0 <= time.time() - last < UPDATE_CHECK_INTERVAL:
            return
        self._check_for_update(manual=False)

    def _check_for_update(self, manual=True):
        """Asks GitHub for the latest release in the background. manual
        (Help > Check for updates): also says when there's no newer one,
        or why it couldn't look, and offers a skipped version again."""
        if self._update_busy:
            if manual:
                messagebox.showinfo("Check for updates", "Already checking for or downloading an update.")
            return
        self._update_busy = True
        if manual:
            self.status_lbl.config(text="Checking for updates...")

        def work():
            try:
                release, error = fetch_latest_release(), None
            except Exception as exc:
                release, error = None, exc
            self.after(0, lambda: self._update_check_done(release, error, manual))

        threading.Thread(target=work, daemon=True).start()

    def _update_check_done(self, release, error, manual):
        self._update_busy = False
        if manual:
            self.status_lbl.config(text="Ready")
        if error is not None:
            if manual:
                messagebox.showwarning("Check for updates",
                                       f"Couldn't find out about updates from GitHub:\n{error}")
            return
        self.settings["update_checked_at"] = time.time()
        save_settings(self.settings)
        tag = str(release.get("tag_name"))
        if not is_newer_release(release):
            if manual:
                messagebox.showinfo("Check for updates",
                                    f"You have the latest version ({APP_VERSION}).")
            return
        if not manual and self.settings.get("update_skipped") == tag:
            return
        if self._update_dialog is not None:
            return    # already asking
        choice = self._ask_update(release)
        if choice == "skip":
            self.settings["update_skipped"] = tag
            save_settings(self.settings)
        elif choice == "update":
            self._start_update(release)

    def _ask_update(self, release):
        """The newer release's version and notes, with Update now, Later and
        Skip this version (which the daily check then stays quiet about),
        and the "Automatically check for updates" switch. Returns "update", "later" or "skip". Waits until it is closed."""
        version = str(release.get("tag_name")).lstrip("vV")
        dlg = tk.Toplevel(self, bg="#f0f0f0")
        dlg.withdraw()
        dlg.title("Update available")
        dlg.transient(self)
        dlg.resizable(False, False)
        self._update_dialog = dlg     # for the tests
        answer = {"choice": "later"}
        body = tk.Frame(dlg, bg="#f0f0f0", padx=px(16), pady=px(12))
        body.pack(fill="both", expand=True)
        tk.Label(body, bg="#f0f0f0", anchor="w", font=("Segoe UI", 11, "bold"),
                 text=f"Version {version} is available").pack(fill="x")
        tk.Label(body, bg="#f0f0f0", anchor="w",
                 text=f"You have version {APP_VERSION}.").pack(fill="x", pady=(px(2), 0))
        notes = str(release.get("body") or "").strip()
        if notes:
            tk.Label(body, bg="#f0f0f0", anchor="w", font=("Segoe UI", 9, "bold"),
                     text="What's new").pack(fill="x", pady=(px(10), px(2)))
            box = tk.Frame(body, bg="#f0f0f0")
            box.pack(fill="both", expand=True)
            text = tk.Text(box, width=62, height=1, wrap="word", relief="solid",
                           borderwidth=1, padx=px(6), pady=px(4), font=("Segoe UI", 9))
            tall = insert_markdown(text, markdown_blocks(notes))
            text.config(state="disabled")
            text.pack(side="left", fill="both", expand=True)
            bar = tk.Scrollbar(box, command=text.yview)
            line = tkfont.Font(font=text.cget("font")).metrics("linespace")

            def fit(tall):
                """As tall as the notes (tall pixels), up to 12 lines; more
                scrolls."""
                lines = max(1, -(-tall // max(1, line)))
                text.config(height=min(lines, 12))
                if lines > 12 and not bar.winfo_manager():
                    text.config(yscrollcommand=bar.set)
                    bar.pack(side="right", fill="y")

            fit(tall)
            self._update_notes = text     # for the tests

        def choose(choice):
            answer["choice"] = choice
            dlg.destroy()

        # The daily check (_auto_check_for_update); saved as soon as it's clicked.
        auto_var = tk.BooleanVar(dlg, value=bool(self.settings.get("update_auto", True)))
        self._update_auto_var = auto_var     # for the tests

        def auto_toggled():
            self.settings["update_auto"] = auto_var.get()
            save_settings(self.settings)

        tk.Checkbutton(body, text="Automatically check for updates", variable=auto_var,
                       command=auto_toggled, bg="#f0f0f0", anchor="w"
                       ).pack(fill="x", pady=(px(8), 0))

        buttons = tk.Frame(body, bg="#f0f0f0")
        buttons.pack(fill="x", pady=(px(10), 0))
        tk.Button(buttons, text="Later", width=10, command=lambda: choose("later")).pack(side="right")
        update_btn = tk.Button(buttons, text="Update now", width=12, default="active",
                               command=lambda: choose("update"))
        update_btn.pack(side="right", padx=(0, px(6)))
        tk.Button(buttons, text="Skip this version",
                  command=lambda: choose("skip")).pack(side="left")
        dlg.bind("<Return>", lambda e: choose("update"))
        dlg.bind("<Escape>", lambda e: choose("later"))
        dlg.protocol("WM_DELETE_WINDOW", lambda: choose("later"))

        self._theme_tree(dlg)
        dlg.update_idletasks()
        self._set_title_bar_dark(self._is_dark(), dlg)
        w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        x = self.winfo_rootx() + (self.winfo_width() - w) // 2
        y = self.winfo_rooty() + (self.winfo_height() - h) // 3
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        dlg.deiconify()
        if notes:
            # Laid out at its real width only now (hidden, a Text is 1 px
            # wide): Tk's own measure corrects the estimate.
            dlg.update_idletasks()
            tall = text.count("1.0", "end", "update", "ypixels")
            fit(tall[0] if isinstance(tall, tuple) else tall or 0)
        update_btn.focus_set()
        dlg.grab_set()
        self.wait_window(dlg)
        self._update_dialog = None
        return answer["choice"]

    def _update_offer_page(self, release, why):
        """The update can't be installed from here (why): offers the
        release's page in the browser instead."""
        page = release.get("html_url") or UPDATE_PAGE_URL
        if messagebox.askyesno("Update", f"{why}\n\nOpen the release's page in the browser?"):
            import webbrowser
            webbrowser.open(page)

    def _start_update(self, release):
        """Downloads the release's installer (installed copy) or portable
        zip (portable copy, unpacked next to it) in the background, then
        hands over to _install_update. Run from source: _update_source."""
        version = str(release.get("tag_name")).lstrip("vV")
        if not running_built_app():
            self._update_source(release)
            return
        installed = app_is_installed()
        names = UPDATE_SETUP_ASSETS if installed else UPDATE_PORTABLE_ASSETS
        asset = release_asset(release, names)
        if asset is None:
            self._update_offer_page(release, f"Version {version}'s release has no {names[0]} to download.")
            return
        name = asset["name"]
        app_dir = _app_dir()
        if not installed and not folder_is_writable(app_dir):
            self._update_offer_page(
                release, f"The app can't write to its own folder ({app_dir}), so it can't "
                         f"replace itself there.")
            return
        self._update_busy = True
        report = self._progress_reporter(f"Downloading version {version}...", "update")
        report(0.0)
        folder = os.path.join(tempfile.gettempdir(), "BBVideoCutter-update")

        def work():
            try:
                shutil.rmtree(folder, ignore_errors=True)
                os.makedirs(folder, exist_ok=True)
                path = os.path.join(folder, name)
                size = download_file(asset["browser_download_url"], path, report)
                if asset.get("size") and size != asset["size"]:
                    raise OSError(f"The download is incomplete ({size} of {asset['size']} bytes).")
                new_build = None
                if not installed:
                    staging = os.path.join(folder, "new")
                    with zipfile.ZipFile(path) as z:
                        z.extractall(staging)
                    new_build = os.path.join(staging, "BBVideoCutter")
                    if not os.path.isfile(os.path.join(new_build, "BBVideoCutter.exe")):
                        raise OSError(f"{name} has no BBVideoCutter\\BBVideoCutter.exe in it.")
                    os.remove(path)
                done = lambda: self._install_update(release, folder, path if installed else None,
                                                    new_build)
            except Exception as exc:
                error = exc
                done = lambda: self._update_failed(version, error)
            self.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    def _update_failed(self, version, error):
        self._update_busy = False
        self._end_progress("update")
        self.status_lbl.config(text="Ready")
        messagebox.showerror("Update", f"Couldn't download version {version}:\n{error}")

    def _install_update(self, release, folder, setup, new_build):
        """The update is downloaded: once agreed to, the app closes and a
        helper with no window installs it and starts the app again."""
        self._update_busy = False
        self._end_progress("update")
        version = str(release.get("tag_name")).lstrip("vV")
        running = [job for job in self._progress_jobs if job != "update"]
        stop = "\n\nWhat's still running (" + ", ".join(running) + ") will be stopped." if running else ""
        if not messagebox.askokcancel(
                "Update",
                f"Version {version} is downloaded.\n\nThe app will close, update itself and "
                f"start again.{stop}"):
            self.status_lbl.config(text="Update cancelled.")
            shutil.rmtree(folder, ignore_errors=True)
            return
        script = os.path.join(folder, "update.ps1")
        try:
            with open(script, "w", encoding="utf-8-sig") as f:
                f.write(update_helper_script(os.getpid(), _app_dir(), setup=setup, new_build=new_build))
            self._launch_update_helper(script)
        except OSError as exc:
            messagebox.showerror("Update", f"Couldn't start the update:\n{exc}")
            return
        self._on_app_close()

    def _update_source(self, release):
        """Run from source: the checkout is brought up to date with git
        pull (in the background), then the app offers to restart. When git
        can't (not a checkout, no upstream, local changes in the way...),
        the release's page is offered instead."""
        self._update_busy = True
        self.status_lbl.config(text="Updating the source with git pull...")
        folder = _app_dir()

        def work():
            try:
                result, error = git_pull_source(folder), None
            except Exception as exc:
                result, error = None, exc
            self.after(0, lambda: self._update_source_done(release, folder, result, error))

        threading.Thread(target=work, daemon=True).start()

    def _update_source_done(self, release, folder, result, error):
        self._update_busy = False
        if error is not None:
            self.status_lbl.config(text="Ready")
            self._update_offer_page(
                release, f"This copy runs from source (BBVideoCutter.py), and git couldn't "
                         f"update it: {error}")
            return
        before, after, upstream = result
        if before == after:
            self.status_lbl.config(text="Ready")
            messagebox.showinfo("Update", f"git pull found nothing new on {upstream}.")
            return
        version = source_app_version(os.path.join(folder, "BBVideoCutter.py"))
        now = f", now version {version}" if version and version != APP_VERSION else ""
        self.status_lbl.config(text="Updated with git pull: restart the app to use it.")
        if messagebox.askyesno(
                "Update",
                f"The source is updated from {upstream} ({before[:7]} to {after[:7]}{now}).\n\n"
                f"Restart the app to use it now?"):
            self._restart_from_source()

    def _restart_from_source(self):
        """Closes the app; a helper with no window starts it again (the same
        Python and script) once it is gone."""
        python = sys.executable
        windowless = os.path.join(os.path.dirname(python), "pythonw.exe")
        if os.path.basename(python).lower() == "python.exe" and os.path.isfile(windowless):
            python = windowless     # no console window popping up with it
        cmd = [python, os.path.abspath(sys.argv[0])] + sys.argv[1:]
        try:
            launch_detached(powershell_hidden(restart_script(os.getpid(), cmd, os.getcwd())))
        except OSError as exc:
            messagebox.showerror("Update", f"Couldn't restart the app:\n{exc}")
            return
        self._on_app_close()

    def _launch_update_helper(self, script):
        launch_detached(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                         "Bypass", "-WindowStyle", "Hidden", "-File", script])

    def _show_about(self):
        messagebox.showinfo(
            "About",
            f"BarkBark Lossless Video Cutter\nVersion {APP_VERSION}\n\n"
            "Cutting and joining both use ffmpeg stream-copy (-c copy) for the "
            "bulk of the video, so almost nothing is re-encoded: it's fast and "
            "lossless. Cutting only re-encodes a fraction of a second right at "
            "an edge that doesn't land exactly on a keyframe, so the cut lands "
            "on the exact second you asked for (GPU-accelerated when "
            "available) — everything else stays byte-identical.\n\n"
            "Drag & drop is enabled — drop a video onto the Cut, Crop/Rotate or Audio tab, "
            "or onto the Combine tab's list. The Crop/Rotate tab uses a visual crop editor; "
            "arbitrary spatial cropping requires video re-encoding.\n\n"
            "Built with Python by Thmyris.\n\n"
            "Special thanks to\n"
            "Josh (Madcat) - for testing and feedback",
        )

    # ====================================================================
    # Shared: the empty "drop a video" area -- a click opens a video, a drag
    # moves the window
    # ====================================================================
    OPEN_PROMPT = "Click here or drop a video to begin"

    def _draw_open_prompt(self, canvas, w, h):
        canvas.create_text(w / 2, h / 2, text=self.OPEN_PROMPT, fill="#888888",
                           font=("Segoe UI", 12), tags=("placeholder",))

    DRAG_THRESHOLD = 4   # pixels the pointer moves before a press is a drag, not a click

    def _bind_window_drag(self, widget, is_empty, on_click=None):
        """Dragging `widget` with the left button moves the whole window,
        but only while `is_empty(event)` is true, i.e. while the press lands
        on an empty drop-here area where a click has no other meaning.
        There, a click (press and release without moving) calls on_click."""
        widget.bind("<ButtonPress-1>", lambda e: self._window_drag_start(e, is_empty), add="+")
        widget.bind("<B1-Motion>", self._window_drag_motion, add="+")
        widget.bind("<ButtonRelease-1>", lambda e: self._window_drag_end(e, on_click), add="+")

    def _window_drag_start(self, event, is_empty):
        self._window_drag_press = None
        self._window_drag_offset = None
        if not is_empty(event):
            return
        self._window_drag_press = (event.x_root, event.y_root)
        self._window_drag_moved = False
        if self.state() != "zoomed":
            # winfo_x/y of a toplevel is its outer frame, the same corner
            # that geometry("+x+y") positions, so the offset stays exact.
            self._window_drag_offset = (event.x_root - self.winfo_x(),
                                        event.y_root - self.winfo_y())

    def _window_drag_motion(self, event):
        press = getattr(self, "_window_drag_press", None)
        if press is None:
            return
        if not self._window_drag_moved:
            if max(abs(event.x_root - press[0]), abs(event.y_root - press[1])) < px(self.DRAG_THRESHOLD):
                return   # still a click
            self._window_drag_moved = True
        offset = self._window_drag_offset
        if offset is not None:
            self.geometry(f"+{event.x_root - offset[0]}+{event.y_root - offset[1]}")

    def _window_drag_end(self, event, on_click=None):
        press = getattr(self, "_window_drag_press", None)
        self._window_drag_press = None
        self._window_drag_offset = None
        if press is not None and not self._window_drag_moved and on_click is not None:
            on_click()

    # ====================================================================
    # Shared: light / dark theme (Options menu)
    # ====================================================================
    def _is_dark(self):
        return self.dark_mode_var.get()

    def _theme_color(self, name):
        return THEME_COLORS["dark" if self._is_dark() else "light"][name]

    def _on_dark_mode_toggled(self):
        self.settings["dark_mode"] = self._is_dark()
        save_settings(self.settings)
        self._apply_theme()

    def _apply_theme(self):
        """Re-themes every existing widget, the ttk widgets, the title bar,
        and everything code draws with theme colors."""
        self._apply_ttk_theme()
        self._theme_tree(self)
        self._style_menu_bar()
        self._set_title_bar_dark(self._is_dark())
        self._redraw_slider()  # canvas items drawn with theme colors
        self._style_marker_picker()
        if getattr(self, "cut_mute_btn", None) is not None:
            self._draw_cut_speaker()
        if getattr(self, "resize_audio", None) is not None:
            self.resize_audio.draw_speaker()

    def _configure_light(self, w, **options):
        """Configure w using light-theme colors, as code always does; in dark
        mode they are swapped for their dark versions right away."""
        saved = getattr(w, "_bbvc_light_colors", None)
        if saved is not None:
            w.configure(saved)
            del w._bbvc_light_colors
        w.configure(**options)
        self._theme_widget(w)

    def _theme_tree(self, widget):
        self._theme_widget(widget)
        for child in widget.winfo_children():
            self._theme_tree(child)

    def _on_widget_mapped(self, event):
        if isinstance(event.widget, tk.Misc):
            self._theme_widget(event.widget)

    def _theme_widget(self, w):
        """Dark: swap w's light chrome colors for dark ones, remembering the
        originals on the widget. Light: put the originals back. Widgets
        whose background is not light chrome (content areas) and ttk
        widgets (styled separately) are left alone."""
        saved = getattr(w, "_bbvc_light_colors", None)
        if not self._is_dark():
            if saved is None:
                return
            for option, original in saved.items():
                w.configure({option: original})
            del w._bbvc_light_colors
            if isinstance(w, TimeSlider):
                w.set_track_color(self._theme_color("timeslider_track"))
            return
        if saved is not None or isinstance(w, (tk.Menu, ttk.Widget)):
            return
        try:
            options = w.keys()
            cls = w.winfo_class()
            bg = str(w.cget("background"))
        except (tk.TclError, AttributeError):
            return
        if _dark_bg(cls, "background", bg) is None:
            return
        saved = {}
        changes = {}
        for option in _BG_OPTIONS:
            if option in options:
                value = str(w.cget(option))
                new = _dark_bg(cls, option, value)
                if new:
                    saved[option], changes[option] = value, new
        for option in _FG_OPTION_NAMES:
            if option in options:
                value = str(w.cget(option))
                role = _FG_OPTIONS.get(value.lower())
                if role:
                    saved[option], changes[option] = value, DARK[role]
        if changes:
            w.configure(changes)
        w._bbvc_light_colors = saved
        if isinstance(w, TimeSlider):
            w.set_track_color(self._theme_color("timeslider_track"))

    def _set_treeview_row_height(self, style):
        """Treeview rows are a fixed 20 px by default, which is the bare line
        height of the font at 125% scaling; size them from the font instead.
        Style settings belong to a theme, so this runs again after a switch."""
        linespace = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        style.configure("Treeview", rowheight=max(px(20), linespace + px(4)))
        # The Combine and Bulk Cut lists: room for a thumbnail on each row.
        style.configure(FileTable.STYLE, rowheight=max(self.THUMB_SIZE[1] + px(8), linespace + px(4)))

    def _apply_ttk_theme(self):
        style = ttk.Style(self)
        if not self._is_dark():
            style.theme_use(self._ttk_light_theme)
            self._set_treeview_row_height(style)
            self._hide_notebook_tabs(style)
            return
        # The native Windows ttk theme can't be recolored; clam can.
        style.theme_use("clam")
        d = DARK
        style.configure(
            ".", background=d["panel"], foreground=d["text"], fieldbackground=d["field"],
            bordercolor=d["border"], lightcolor=d["panel"], darkcolor=d["panel"],
            troughcolor=d["panel"], selectbackground=d["select"],
            selectforeground="#ffffff", insertcolor=d["text"], arrowcolor=d["text"],
        )
        style.map(".", foreground=[("disabled", d["text_disabled"])])
        style.configure("TNotebook", background=d["panel"], bordercolor=d["border"])
        style.configure("TNotebook.Tab", background=d["button"], foreground=d["text"],
                        bordercolor=d["border"], lightcolor=d["button"])
        style.map("TNotebook.Tab",
                  background=[("selected", d["panel"]), ("active", d["button_active"])],
                  lightcolor=[("selected", d["panel"])])
        style.configure("Treeview", background=d["field"], fieldbackground=d["field"],
                        foreground=d["text"])
        self._set_treeview_row_height(style)
        style.map("Treeview", background=[("selected", d["select"])],
                  foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background=d["button"], foreground=d["text"],
                        bordercolor=d["border"], lightcolor=d["button"])
        style.map("Treeview.Heading", background=[("active", d["button_active"])])
        self._hide_notebook_tabs(style)
        style.configure("TScrollbar", background=d["button"], troughcolor=d["panel"],
                        bordercolor=d["border"], arrowcolor=d["text"],
                        lightcolor=d["button"], darkcolor=d["button"])
        style.map("TScrollbar", background=[("active", d["button_active"])])
        style.configure("Horizontal.TProgressbar", background=d["select"], troughcolor=d["field"],
                        bordercolor=d["border"], lightcolor=d["select"], darkcolor=d["select"])

    def _set_title_bar_dark(self, dark, window=None):
        """Windows 10 1809+ / 11: a dark caption to match, on the main
        window or another top-level `window`. Anywhere else, or if it
        fails, the title bar just stays as the system draws it."""
        if os.name != "nt":
            return
        window = window or self
        try:
            window.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
            value = ctypes.c_int(1 if dark else 0)
            # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE; 19 on builds before 20H1.
            for attribute in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    break
            # Windows 10 keeps drawing the old caption until the window's
            # active state changes (a redraw or SWP_FRAMECHANGED doesn't do
            # it), so tell it the state changed: flip it and put it back.
            # This only repaints the frame; nothing is activated.
            WM_NCACTIVATE = 0x0086
            active = ctypes.windll.user32.GetForegroundWindow() == hwnd
            ctypes.windll.user32.SendMessageW(hwnd, WM_NCACTIVATE, int(not active), 0)
            ctypes.windll.user32.SendMessageW(hwnd, WM_NCACTIVATE, int(active), 0)
        except Exception:
            pass

    # ====================================================================
    # Shared: status-bar progress + overwrite protection
    # ====================================================================
    def _progress_reporter(self, label, job):
        """Returns a thread-safe callback(fraction) that shows progress on
        the status bar: the text, the progress bar and a time-left estimate.
        `label` is either a prefix string ("Cropping...") or a
        callable(percent) -> full status text. `job` names the running
        operation ("cut", "crop"...); _end_progress(job) hides the bar
        again. The text only repaints when the whole-number percentage
        changes; the bar and estimate follow every report."""
        last = {"pct": None}

        def report(frac):
            frac = max(0.0, min(1.0, frac))
            pct = int(frac * 100)
            text = None
            if pct != last["pct"]:
                last["pct"] = pct
                text = label(pct) if callable(label) else f"{label} {pct}%"
            self.after(0, lambda: self._show_progress(job, frac, text))

        return report

    def _show_progress(self, job, frac, text=None):
        """Main thread only. Shows `job`'s progress on the status bar. The
        time left is extrapolated from how long the job has taken so far
        for the fraction it has done, so it firms up as the job goes on."""
        now = time.monotonic()
        started = self._progress_jobs.setdefault(job, now)
        if text is not None:
            self.status_lbl.config(text=text)
        if not self.progress_bar.winfo_manager():
            self.progress_bar.pack(side="right", padx=(4, 6), pady=2)
            self.progress_eta_lbl.pack(side="right")
        self.progress_bar["value"] = frac * 1000
        elapsed = now - started
        if frac >= 0.02 and elapsed >= 2.0 and frac < 1.0:
            self.progress_eta_lbl.config(
                text=f"about {format_duration(elapsed * (1.0 - frac) / frac)} left"
            )
        elif frac >= 1.0:
            self.progress_eta_lbl.config(text="finishing...")
        else:
            self.progress_eta_lbl.config(text="estimating time...")

    def _end_progress(self, job):
        """Main thread only. `job` is done; the bar goes away once no other
        job is still reporting."""
        self._progress_jobs.pop(job, None)
        if not self._progress_jobs and self.progress_bar.winfo_manager():
            self.progress_bar.pack_forget()
            self.progress_eta_lbl.pack_forget()
            self.progress_eta_lbl.config(text="")

    def _ask_overwrite(self, existing_paths, alt_hint=None):
        """Main thread only. Asks what to do about output files that
        already exist. Returns "overwrite", "rename", or None (cancel)."""
        if len(existing_paths) == 1:
            p = existing_paths[0]
            what = f'"{os.path.basename(p)}" already exists in:\n{os.path.dirname(p)}'
        else:
            names = "\n".join(os.path.basename(p) for p in existing_paths[:8])
            more = f"\n...and {len(existing_paths) - 8} more" if len(existing_paths) > 8 else ""
            what = f"{len(existing_paths)} output files already exist:\n{names}{more}"
        keep_both = f' (save as "{alt_hint}")' if alt_hint else " (save with a (2), (3)... suffix)"
        answer = messagebox.askyesnocancel(
            "File already exists",
            f"{what}\n\n"
            f"Yes = overwrite\n"
            f"No = keep both{keep_both}\n"
            f"Cancel = don't save\n\n"
            f"(This warning can be turned off under Options.)",
            icon="warning",
        )
        if answer is None:
            return None
        return "overwrite" if answer else "rename"

    def _check_output_path(self, out_path, source_paths, prompt_overwrite=True):
        """Main thread only. Validates a single output path before a save.
        Returns the path to write to (possibly renamed to keep both files),
        or None if the save should be cancelled."""
        out_abs = os.path.normcase(os.path.abspath(out_path))
        for src in source_paths:
            if out_abs == os.path.normcase(os.path.abspath(src)):
                messagebox.showerror(
                    "Can't overwrite the source",
                    "The output file would replace one of the videos being "
                    "read. Choose a different name.",
                )
                return None
        if prompt_overwrite and self.warn_overwrite_var.get() and os.path.exists(out_path):
            alt = unique_output_path(out_path)
            choice = self._ask_overwrite([out_path], alt_hint=os.path.basename(alt))
            if choice is None:
                return None
            if choice == "rename":
                return alt
        return out_path

    def _ask_overwrite_from_worker(self, existing_paths):
        """Worker thread only: shows _ask_overwrite on the main thread and
        blocks until the user answers."""
        done = threading.Event()
        box = {}

        def ask():
            box["answer"] = self._ask_overwrite(existing_paths)
            done.set()

        self.after(0, ask)
        done.wait()
        return box["answer"]

    # ====================================================================
    # CUT TAB
    # ====================================================================
    def _build_cut_tab(self):
        root = self.cut_tab
        # Row layout: header(fixed) / preview(expands) / controls(fixed)
        root.rowconfigure(1, weight=1)
        root.columnconfigure(0, weight=1)

        # ---- header
        header = tk.Frame(root, bg="#f0f0f0")
        header.grid(row=0, column=0, sticky="ew", padx=3, pady=(6, 2))
        self.filename_lbl = tk.Label(header, text="No file loaded",
                                      anchor="w", bg="#f0f0f0",
                                      font=("Segoe UI", 10, "bold"))
        self.filename_lbl.pack(side="left")
        self.info_lbl = tk.Label(header, text="", anchor="e", bg="#f0f0f0",
                                  font=("Segoe UI", 9))
        self.info_lbl.pack(side="right")

        # ---- preview (a Canvas, which scales properly with the window --
        #      unlike a Label, a Canvas's width/height are always pixels)
        preview_frame = tk.Frame(root, bg="black")
        preview_frame.grid(row=1, column=0, sticky="nsew", padx=3, pady=4)
        self.preview_canvas = tk.Canvas(preview_frame, bg="black", highlightthickness=0)
        self.preview_canvas.pack(fill="both", expand=True)
        self.preview_canvas.bind("<Configure>", lambda e: self._redraw_preview_canvas())
        self._bind_window_drag(self.preview_canvas, lambda e: not self.input_path,
                               on_click=self.open_file_for_cut)
        self._draw_preview_placeholder(self.OPEN_PROMPT)

        # The embedded player covers the still-frame canvas while a video is
        # loaded (see _cut_use_player); without libmpv there is no player
        # and the canvas shows ffmpeg-extracted still frames as before.
        self.cut_player = None
        self._cut_player_on = False      # the player (not the canvas) shows the video
        self._cut_scrubbing = False      # the slider's playhead is being dragged
        self._cut_scrub_time = 0.0
        if mpv_available():
            self.cut_player = VideoPlayer(preview_frame, on_change=self._on_cut_player_changed)
            self.cut_player.frame.bind("<Button-1>", self._on_cut_video_click)

        # ---- bottom controls (fixed height, full width)
        controls = tk.Frame(root, bg="#f0f0f0")
        controls.grid(row=2, column=0, sticky="ew", padx=3, pady=2)

        self.cut_transport_buttons = []  # need a loaded video
        self.cut_transport_row = None
        if self.cut_player is not None:
            self._build_cut_transport(controls)

        # The markers' row, centred and symmetric: Mark Cut Start, the Cut
        # Start card, Replay (what lies between them), the Cut End card, Mark
        # Cut End (_build_marker_picker puts them in that order).
        time_row = tk.Frame(controls, bg="#f0f0f0")
        time_row.pack(pady=(px(3), px(2)))
        self.cut_replay_btn = None
        if self.cut_player is not None:
            replay = tk.Button(time_row, text="▶ Replay scene", command=self._cut_replay,
                               state="disabled")
            Tooltip(replay, "Plays from Cut Start to Cut End once, then stops")
            self.cut_transport_buttons.append(replay)
            self.cut_replay_btn = replay
        self.handle_var = tk.StringVar(value="end")
        # The time fields take only what a time is made of: digits, ":" and
        # "." (the milliseconds); a typed or pasted letter is refused.
        # %S: the text being inserted (or deleted).
        time_chars = (self.register(lambda s: all(c in "0123456789:." for c in s)), "%S")
        self.start_entry = tk.Entry(time_row, width=13, justify="center",
                                    validate="key", validatecommand=time_chars)
        self.start_entry.pack(side="left", padx=(2, 20))
        self.start_entry.bind("<Return>", lambda e: self._on_start_entry())
        self.start_entry.bind("<FocusOut>", lambda e: self._on_start_entry())
        # A click (or Tab) into a field picks its marker. Not <FocusIn>: that
        # also comes when the window is switched back to.
        for event in ("<Button-1>", "<<TraverseIn>>"):
            self.start_entry.bind(event, lambda e: self._field_picks_marker("start"), add="+")

        self.end_entry = tk.Entry(time_row, width=13, justify="center",
                                  validate="key", validatecommand=time_chars)
        self.end_entry.pack(side="left", padx=2)
        self.end_entry.bind("<Return>", lambda e: self._on_end_entry())
        self.end_entry.bind("<FocusOut>", lambda e: self._on_end_entry())
        for event in ("<Button-1>", "<<TraverseIn>>"):
            self.end_entry.bind(event, lambda e: self._field_picks_marker("end"), add="+")
        self._marker_pickers = {}      # "start" / "end" -> its picker widget
        self._marker_styled = None     # what _style_marker_picker last styled for
        self.cut_mark_buttons = ()
        if self.cut_player is not None:
            # Mark Cut Start left of Cut Start, Mark Cut End right of Cut End
            # (placed by _build_marker_picker).
            mark_start = tk.Button(time_row, text="Mark Cut Start", state="disabled",
                                   command=lambda: self._cut_mark("start"))
            mark_end = tk.Button(time_row, text="Mark Cut End", state="disabled",
                                 command=lambda: self._cut_mark("end"))
            Tooltip(mark_start, "Sets the start at the frame on screen. Shortcut: I")
            Tooltip(mark_end, "Sets the end at the frame on screen. Shortcut: O")
            self.cut_mark_buttons = (mark_start, mark_end)
            self.cut_transport_buttons += [mark_start, mark_end]
        self._build_marker_picker()
        self.handle_var.trace_add("write", lambda *a: self._style_marker_picker())

        step_row = tk.Frame(controls, bg="#f0f0f0")
        step_row.pack(pady=(1, 2))  # no fill -> centers horizontally by default
        self.step_buttons = []

        def step_tip(step):
            # These move a cut marker, not the playhead: the hint names the
            # marker they move now, e.g. "Cut start -> -5s".
            which = "start" if self.active_handle == "start" else "end"
            return f"Cut {which} -> {step}"

        # Mirrored round the middle, like the markers' row above: back
        # (−3 min ... −1 s) on the left, forward (+1 s ... +3 min) on the
        # right, evenly spaced, the amount on each button. step_buttons keeps
        # the order 1 s back, 1 s forward, 5 s back, ... 3 min forward.
        amounts = (("1 s", 1), ("5 s", 5), ("30 s", 30), ("3 min", 180))
        made = {}
        for sign, order in ((-1, reversed(amounts)), (1, amounts)):
            for text, secs in order:
                b = tk.Button(step_row, text=("−" if sign < 0 else "+") + text,
                              width=self.STEP_BUTTON_CHARS,
                              command=lambda s=sign * secs: self._nudge(s))
                b.pack(side="left", padx=1)
                amount = text.replace(" ", "")
                Tooltip(b, lambda a=("-" if sign < 0 else "+") + amount: step_tip(a))
                made[sign, secs] = b
        self.step_buttons = [made[sign, secs] for _text, secs in amounts for sign in (-1, 1)]
        self.cut_step_row = step_row
        self._init_cut_button_fit()

        # ---- slider, and under it the waveform of the audio heard, on the
        # slider's time axis. One canvas for both, so the playhead through
        # them is one line: on two canvases Windows paints each on its own,
        # and a fast drag showed the needle broken where they meet.
        self._strip_on = PIL_AVAILABLE
        self._strip_y0, self._strip_h = px(24) + 1, px(30)
        height = self._strip_y0 + self._strip_h if self._strip_on else px(24)
        self.slider_canvas = tk.Canvas(controls, height=height, bg="#f0f0f0", highlightthickness=0)
        self.slider_canvas.pack(fill="x", pady=(1, 1))
        self.slider_canvas.bind("<Configure>", lambda e: self._redraw_slider())
        self.slider_canvas.bind("<Button-1>", self._slider_click)
        self.slider_canvas.bind("<B1-Motion>", self._slider_drag)
        self.slider_canvas.bind("<ButtonRelease-1>", self._slider_release)
        self.slider_canvas.bind("<Button-3>", self._cut_ranges_menu)
        self._drag_target = None
        self._grab_x = None   # where a handle was pressed, until it is dragged
        self._scrub_seek_id = None   # the seek waiting for the dragging mouse to rest
        # While a handle is dragged the video shows its frame, but the
        # playhead stays: where the player was, to go back to on release.
        self._handle_preview = None
        # (time, deadline): a seek just sent; the playhead shows it at once
        # and stays there until the player reports a position at it.
        self._playhead_pin = None

        save_row = tk.Frame(controls, bg="#f0f0f0")
        save_row.pack(fill="x", pady=(2, 1))
        # Several scenes: each added to the Bulk Cut list, then all cut there,
        # each into its own clip.
        self.cut_ranges_btn = tk.Button(save_row, text="Cut selected\nscenes", height=2, width=17,
                                        command=self._cut_selected_scenes)
        self.cut_ranges_btn.pack(side="right", padx=(2, 0))
        self.cut_add_range_btn = tk.Button(save_row,
                                           text="Add selected scene to\nthe bulk cutting list",
                                           height=2, width=20, command=self._cut_add_range)
        Tooltip(self.cut_add_range_btn, "Shortcut: A")
        self.cut_add_range_btn.pack(side="right", padx=(2, 0))
        self.save_btn = tk.Button(save_row, text="Save this Scene!", height=2,
                                   font=("Segoe UI", 10, "bold"),
                                   command=self._save_scene_clicked)
        self.save_btn.pack(side="left", fill="x", expand=True)
        self._update_cut_range_buttons()

    DEFAULT_SEARCH_WINDOW = 8.0   # seconds; see _lossless_cut

    @staticmethod
    def _format_seconds(value):
        return f"{value:.3f}".rstrip("0").rstrip(".")

    def _ask_search_window(self):
        """Opened by choosing Re-encode edges or Edit-list trim in the Cut
        menu: a small dialog to set the keyframe search window, stating what
        it does and its default. Cancel keeps the current value. Waits until
        it is closed."""
        default = self._format_seconds(self.DEFAULT_SEARCH_WINDOW)
        dlg = tk.Toplevel(self, bg="#f0f0f0")
        dlg.withdraw()
        dlg.title("Keyframe search window")
        dlg.transient(self)
        dlg.resizable(False, False)
        self._search_window_dialog = dlg     # for the tests
        body = tk.Frame(dlg, bg="#f0f0f0", padx=px(16), pady=px(12))
        body.pack(fill="both", expand=True)
        tk.Label(body, bg="#f0f0f0", justify="left", anchor="w", wraplength=px(380),
                 text="How far before and after each cut point to look for a keyframe, "
                      "in seconds. Used by Re-encode edges and Edit-list trim; "
                      "Lossless doesn't use it.").pack(fill="x")
        tk.Label(body, bg="#f0f0f0", anchor="w", font=("Segoe UI", 9, "bold"),
                 text=f"Default: {default} seconds").pack(fill="x", pady=(px(8), px(6)))
        row = tk.Frame(body, bg="#f0f0f0")
        row.pack(fill="x")
        tk.Label(row, text="Search window:", bg="#f0f0f0").pack(side="left")
        value_var = tk.StringVar(value=self._format_seconds(self.cut_search_window))
        entry = tk.Entry(row, textvariable=value_var, width=8, justify="center")
        entry.pack(side="left", padx=px(6))
        tk.Label(row, text="seconds", bg="#f0f0f0").pack(side="left")
        error_lbl = tk.Label(body, text="", bg="#f0f0f0", fg="#c42b1c", anchor="w")
        error_lbl.pack(fill="x", pady=(px(4), 0))

        def ok(event=None):
            try:
                value = float(value_var.get().strip().replace(",", "."))
            except ValueError:
                value = None
            if value is None or not 0 < value <= 600:
                error_lbl.config(text="Enter a number of seconds above 0 (at most 600).")
                return
            self.cut_search_window = value
            dlg.destroy()

        def reset():
            value_var.set(default)
            error_lbl.config(text="")

        buttons = tk.Frame(body, bg="#f0f0f0")
        buttons.pack(fill="x", pady=(px(10), 0))
        tk.Button(buttons, text="Cancel", width=9, command=dlg.destroy).pack(side="right")
        tk.Button(buttons, text="OK", width=9, command=ok).pack(side="right", padx=(0, px(6)))
        tk.Button(buttons, text=f"Reset to default ({default} s)",
                  command=reset).pack(side="left")
        dlg.bind("<Return>", ok)
        dlg.bind("<Escape>", lambda e: dlg.destroy())

        self._theme_tree(dlg)
        dlg.update_idletasks()
        self._set_title_bar_dark(self._is_dark(), dlg)
        w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        x = self.winfo_rootx() + (self.winfo_width() - w) // 2
        y = self.winfo_rooty() + (self.winfo_height() - h) // 3
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        dlg.deiconify()
        entry.focus_set()
        entry.select_range(0, "end")
        dlg.grab_set()
        self.wait_window(dlg)
        self._search_window_dialog = None

    def _set_cut_controls_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self.start_entry.config(state=state)
        self.end_entry.config(state=state)
        self.save_btn.config(state=state)
        for b in self.step_buttons:
            b.config(state=state)
        self._update_cut_range_buttons()

    # ---- embedded player ---------------------------------------------
    def _build_cut_transport(self, parent):
        """One row: the playhead time at the left, the play / step / jump
        buttons in the middle, the audio controls at the right."""
        row = CenteredRow(parent, bg="#f0f0f0")
        row.pack(fill="x", pady=(2, 1))

        def button(text, command, **kw):
            b = tk.Button(row.center, text=text, command=command, **kw)
            b.pack(side="left", padx=1)
            self.cut_transport_buttons.append(b)
            return b

        # Mark Cut Start / End are in the Start / End row, either side of
        # the fields (_build_cut_mark_buttons).
        button("|◀ Start", lambda: self._cut_go_to("start"))
        frame_back = button("◀ Frame", lambda: self._cut_frame_step(False))
        self.cut_play_btn = button("▶ Play", self._cut_toggle_play, width=8)
        row.center_on(self.cut_play_btn)
        frame_fwd = button("Frame ▶", lambda: self._cut_frame_step(True))
        button("End ▶|", lambda: self._cut_go_to("end"))
        # Shown only with Options > Show frame by frame movement buttons in
        # the video player;
        # the , and . keys move the selected Start / End a frame either way.
        self.cut_frame_buttons = (frame_back, frame_fwd)
        Tooltip(self.cut_play_btn, "Shortcut: Space")
        Tooltip(frame_back, "Moves the selected Start / End one frame back. Shortcut: ,")
        Tooltip(frame_fwd, "Moves the selected Start / End one frame forward. Shortcut: .")
        self._on_show_frame_buttons_toggled(save=False)
        self.cut_time_lbl = tk.Label(row.left, text="", bg="#f0f0f0", anchor="w",
                                     font=("Segoe UI", 9))
        self.cut_time_lbl.pack(side="left", padx=(px(4), 0))
        self._build_cut_audio_controls(row.right)
        self.cut_transport_row = row
        for b in self.cut_transport_buttons:
            b.config(state="disabled")

        # Keyboard: Space play/pause, I / O set start / end, , / . move the
        # selected start / end one frame back / forward, Left / Right 5 s
        # (with Shift 1 s).
        keys = {
            "<KeyPress-space>": lambda: self._cut_toggle_play(),
            "<KeyPress-i>": lambda: self._cut_mark("start"),
            "<KeyPress-I>": lambda: self._cut_mark("start"),
            "<KeyPress-o>": lambda: self._cut_mark("end"),
            "<KeyPress-O>": lambda: self._cut_mark("end"),
            "<KeyPress-comma>": lambda: self._cut_frame_step(False),
            "<KeyPress-period>": lambda: self._cut_frame_step(True),
            "<KeyPress-Left>": lambda: self._cut_seek_by(-5),
            "<KeyPress-Right>": lambda: self._cut_seek_by(5),
            "<Shift-KeyPress-Left>": lambda: self._cut_seek_by(-1),
            "<Shift-KeyPress-Right>": lambda: self._cut_seek_by(1),
            # A adds the scene to the bulk cutting list.
            "<KeyPress-a>": lambda: self._cut_add_range(),
            "<KeyPress-A>": lambda: self._cut_add_range(),
            # M mute, Up / Down volume.
            "<KeyPress-m>": lambda: self._cut_toggle_mute(),
            "<KeyPress-M>": lambda: self._cut_toggle_mute(),
            "<KeyPress-Up>": lambda: self._cut_volume_step(5),
            "<KeyPress-Down>": lambda: self._cut_volume_step(-5),
        }
        for sequence, action in keys.items():
            self.bind(sequence, lambda e, a=action, s=sequence: self._on_cut_key(e, a, s))
            # Clicking a tab gives the notebook the focus, and its own Left /
            # Right bindings would switch tabs; on the Cut tab with a video,
            # the arrows seek instead.
            if "Left" in sequence or "Right" in sequence:
                self.notebook.bind(sequence, lambda e, a=action, s=sequence: self._on_cut_key(e, a, s))

    def _save_option(self, key, var):
        self.settings[key] = var.get()
        save_settings(self.settings)

    def _on_show_frame_buttons_toggled(self, save=True):
        if getattr(self, "cut_frame_buttons", None):
            back, fwd = self.cut_frame_buttons
            if self.show_frame_buttons_var.get():
                back.pack(side="left", padx=1, before=self.cut_play_btn)
                fwd.pack(side="left", padx=1, after=self.cut_play_btn)
            else:
                back.pack_forget()
                fwd.pack_forget()
            self._fit_cut_buttons()
        if getattr(self, "resize_frame_buttons", None):
            back, fwd = self.resize_frame_buttons
            if self.show_frame_buttons_var.get():
                back.pack(side="left", padx=1, before=self.resize_play_btn)
                fwd.pack(side="left", padx=1, after=self.resize_play_btn)
            else:
                back.pack_forget()
                fwd.pack_forget()
            self._fit_resize_buttons()
        if save:
            self._save_option("show_frame_buttons", self.show_frame_buttons_var)

    # The Cut tab's play / step / jump buttons and the 1 sec ... 3 min ones
    # are made wider than their text: 45%, or less in 5% steps until their
    # row fits the window, each row on its own. The Play / Pause button
    # always keeps room for "Pause".
    CUT_BUTTON_EXTRA = (0.45, 0.40, 0.35, 0.30, 0.25, 0.20, 0.15, 0.10, 0.05, 0.0)
    # The −3 min ... +3 min buttons' width in characters. When their row
    # doesn't fit even with no extra (a wider font, e.g. Linux's DejaVu
    # Sans), they give up characters, down to STEP_BUTTON_MIN_CHARS: the
    # longest text, "−3 min".
    STEP_BUTTON_CHARS = 7
    STEP_BUTTON_MIN_CHARS = 6

    def _init_cut_button_fit(self):
        """Notes each button's own width and padding, and refits on resize."""
        self._cut_fit_base = {}   # button -> (its width without extra, its padx)
        self._cut_fit_extra = None
        row = self.cut_transport_row
        buttons = list(self.step_buttons)
        if row is not None:
            buttons += [w for w in row.center.winfo_children() if isinstance(w, tk.Button)]
        for b in buttons:
            self._cut_fit_base[b] = (b.winfo_reqwidth(), b.winfo_pixels(b.cget("padx")))
        self._cut_pause_min = 0
        if row is not None:
            # "Pause" needs more room than the 8 characters Play gets on some
            # systems, where its symbol comes from a wider fallback font.
            play = self.cut_play_btn
            play.config(width=10)
            self._cut_pause_min = play.winfo_reqwidth()
            play.config(width=8)
            for w in (row, row.left, row.right):
                w.bind("<Configure>", lambda e: self._fit_cut_buttons(), add="+")
        self.cut_step_row.master.bind("<Configure>", lambda e: self._fit_cut_buttons(), add="+")

    def _fit_cut_buttons(self):
        if not getattr(self, "_cut_fit_base", None):
            return
        row, steps = self.cut_transport_row, self.cut_step_row
        room = steps.master.winfo_width()
        if room < 2:
            return   # not laid out yet

        def pad(b, extra):
            """b's extra padding each side, for the share extra."""
            width = self._cut_fit_base[b][0]
            add = int(round(width * extra / 2))
            if b is getattr(self, "cut_play_btn", None):
                add = max(add, (self._cut_pause_min - width + 1) // 2)
            return add

        def pack_padx(w):
            padx = w.pack_info().get("padx", 0)
            parts = padx if isinstance(padx, (tuple, list)) else str(padx).split()
            return sum(w.winfo_pixels(str(p)) for p in parts) * (2 if len(parts) == 1 else 1)

        def needed(extra):
            """The widest the two rows get with the share extra."""
            step_w = sum(self._cut_fit_base[w][0] + 2 * pad(w, extra) if w in self._cut_fit_base
                         else w.winfo_reqwidth()
                         for w in steps.winfo_children()) \
                + sum(pack_padx(w) for w in steps.winfo_children())
            if row is None:
                return step_w, 0
            shown = [w for w in row.center.pack_slaves() if w in self._cut_fit_base]
            widths = [self._cut_fit_base[w][0] + 2 * pad(w, extra) + pack_padx(w) for w in shown]
            # Play in the middle of the row: each half holds its side (the
            # time, or the volume) and its half of the buttons.
            i = shown.index(self.cut_play_btn) if self.cut_play_btn in shown else len(shown) // 2
            half_left = sum(widths[:i]) + widths[i] / 2 if widths else 0
            half_right = sum(widths[i + 1:]) + widths[i] / 2 if widths else 0
            gap = px(CenteredRow.GAP)
            row_w = 2 * max(row.left.winfo_reqwidth() + gap + half_left,
                            row.right.winfo_reqwidth() + gap + half_right)
            return step_w, row_w

        # Each row by its own share, so the frame buttons (when shown) take
        # room from the play row only, not from the step buttons too.
        row_room = row.winfo_width() if row is not None else room
        step_extra = next((x for x in self.CUT_BUTTON_EXTRA if needed(x)[0] <= room), 0.0)
        row_extra = next((x for x in self.CUT_BUTTON_EXTRA if needed(x)[1] <= row_room), 0.0)
        # Too wide even with no extra: the step buttons drop characters (a
        # Tk button's character is the width of its font's "0").
        chars = self.STEP_BUTTON_CHARS
        over = needed(0.0)[0] - room
        if over > 0:
            per_char = tkfont.Font(font=self.step_buttons[0].cget("font")).measure("0")
            drop = -(-over // (max(1, per_char) * len(self.step_buttons)))
            chars = max(self.STEP_BUTTON_MIN_CHARS, chars - drop)
        if (step_extra, row_extra, chars) == self._cut_fit_extra:
            return
        self._cut_fit_extra = (step_extra, row_extra, chars)
        steps_set = set(self.step_buttons)
        for b, (width, padx) in self._cut_fit_base.items():
            b.config(padx=padx + pad(b, step_extra if b in steps_set else row_extra))
            if b in steps_set:
                b.config(width=chars)

    def _build_cut_audio_controls(self, parent):
        """Mute, volume and the audio track heard. A cut keeps every audio
        track; the picker only chooses what the player plays: every track
        at once (each video starts that way) or one of them. Volume and
        mute are remembered between runs."""
        player = self.cut_player
        player.set_volume(self.settings.get("player_volume", 100.0))
        player.set_muted(self.settings.get("player_muted", False))
        self._cut_audio_save_id = None
        a = self.cut_audio = PlayerAudioControls(self, parent, player, self._cut_audio_settings_changed,
                                                 on_refresh=self._cut_waveform_follows)
        self.cut_mute_btn, self.cut_volume_slider, self.cut_volume_lbl = (
            a.mute_btn, a.volume_slider, a.volume_lbl)
        self.cut_track_btn, self.cut_track_menu, self.cut_track_var = (
            a.track_btn, a.track_menu, a.track_var)

    def _cut_waveform_follows(self, mixing, current, ids):
        """The waveform shows what is heard."""
        if mixing and len(ids) > 1 and self._wave_track != "all":
            self._start_waveform("all")
        elif not mixing and current in ids and ids.index(current) != self._wave_track:
            self._start_waveform(ids.index(current))

    def _update_cut_audio_controls(self):
        self.cut_audio.refresh()

    def _draw_cut_speaker(self):
        self.cut_audio.draw_speaker()

    def _cut_toggle_mute(self):
        if self.cut_player is not None:
            self.cut_audio.toggle_mute()

    def _cut_volume_step(self, delta):
        if self.cut_player is not None:
            self.cut_audio.volume_step(delta)

    def _cut_audio_settings_changed(self):
        self._update_cut_audio_controls()
        # Saved once the slider has rested, not on every step of a drag.
        if self._cut_audio_save_id is not None:
            self.after_cancel(self._cut_audio_save_id)
        self._cut_audio_save_id = self.after(500, self._save_cut_audio_settings)

    def _save_cut_audio_settings(self):
        self._cut_audio_save_id = None
        self.settings["player_volume"] = self.cut_player.volume
        self.settings["player_muted"] = self.cut_player.muted
        save_settings(self.settings)

    def _on_cut_key(self, event, action, sequence):
        tab = self.notebook.index("current")
        if tab != 0:
            # The other tabs' players take the play / seek / step keys.
            action = self._tab_player_key_action(tab, sequence)
            if action is None:
                return None
        elif not self._cut_player_on:
            return None
        if event.state & (0x4 | 0x20000):   # Ctrl or Alt: someone else's shortcut
            return None
        try:
            focus = self.focus_get()
        except (KeyError, tk.TclError):
            focus = None
        # Typing in a field types; Space on a focused button presses it.
        if isinstance(focus, (tk.Entry, tk.Text, tk.Spinbox, ttk.Entry)):
            return None
        if "space" in sequence and isinstance(
                focus, (tk.Button, tk.Checkbutton, tk.Radiobutton, ttk.Button,
                        ttk.Checkbutton, ttk.Radiobutton)):
            return None
        action()
        return "break"

    def _tab_player_key_action(self, tab, sequence):
        """What a player key does on a tab other than Cut, or None when
        that tab shows no video in a player (the key then does what it
        normally does)."""
        target = None
        if tab == 1 and self._combine_preview_open():
            target = self.combine_preview
        elif tab == 2 and self._bulk_preview_open():
            target = self.bulk_preview
        if target is not None and target.loaded:
            play, step, seek_by = target.toggle_play, target.frame_step, target.seek_by
        elif tab == 3 and self._resize_player_ready():
            p = self.resize_player
            play, step, seek_by = p.toggle_pause, p.frame_step, p.seek_relative
        elif tab == 4 and self._audio_player_ready():
            p = self.audio_player
            play, step, seek_by = p.toggle_pause, p.frame_step, p.seek_relative
        else:
            return None
        keys = {
            "<KeyPress-space>": play,
            "<KeyPress-comma>": lambda: step(False),
            "<KeyPress-period>": lambda: step(True),
            "<KeyPress-Left>": lambda: seek_by(-5),
            "<KeyPress-Right>": lambda: seek_by(5),
            "<Shift-KeyPress-Left>": lambda: seek_by(-1),
            "<Shift-KeyPress-Right>": lambda: seek_by(1),
        }
        if tab == 3:
            # Crop/Rotate has the Cut tab's sound controls, and their keys.
            a = self.resize_audio
            keys.update({
                "<KeyPress-m>": a.toggle_mute,
                "<KeyPress-M>": a.toggle_mute,
                "<KeyPress-Up>": lambda: a.volume_step(5),
                "<KeyPress-Down>": lambda: a.volume_step(-5),
            })
        return keys.get(sequence)

    def _cut_use_player(self, on):
        """Shows the player over the still-frame canvas, or takes it away."""
        self._cut_player_on = on
        if on:
            self.cut_player.frame.place(x=0, y=0, relwidth=1, relheight=1)
        else:
            self.cut_player.frame.place_forget()
        for b in self.cut_transport_buttons:
            b.config(state="normal" if on else "disabled")

    def _on_cut_video_click(self, event):
        # Clicking the video plays / pauses it, and takes the keyboard focus
        # off a time field so the keys work right away.
        self.cut_tab.focus_set()
        self._cut_toggle_play()

    def _cut_toggle_play(self):
        if self._cut_player_on:
            self.cut_player.toggle_pause()

    def _cut_frame_step(self, forward):
        """Moves the selected Start or End one frame back / forward and
        shows the frame now first / last in the cut. As with _cut_mark, the
        time lands half a frame before / after that frame, at the
        millisecond. Frames are taken one frame duration apart from the
        video's first one (not from the player's position: after a seek
        that is the seek's target, not the frame's own time)."""
        if not self._cut_player_on or str(self.save_btn.cget("state")) == "disabled":
            return
        p = self.cut_player
        fd = p.frame_duration
        phase = self.cut_video_start
        step = 1 if forward else -1
        # A time within 5% of a frame of a frame's own time counts as that
        # frame's (a file's timestamps can be rounded to the millisecond).
        tol = 0.05
        p.set_paused(True)
        if self.active_handle == "start":
            # The first frame in the cut is the first one at or after Start.
            first = math.ceil((self.start_sec - phase) / fd - tol) + step
            self.start_sec = min(max(0.0, phase + (first - 0.5) * fd), self.end_sec)
            self._cut_show_time(self.start_sec, "start")
        else:
            # The last frame in the cut is the last one before End.
            last = math.ceil((self.end_sec - phase) / fd - tol) - 1 + step
            self.end_sec = max(min(self.duration, phase + (last + 0.5) * fd), self.start_sec)
            self._cut_show_time(self.end_sec, "end")
        self._sync_entries()
        self._redraw_slider()

    def _cut_replay(self):
        """Plays the selected part, Start to End, once."""
        if self._cut_player_on and self.end_sec > self.start_sec:
            self.cut_player.play_range(self.start_sec, self.end_sec)

    def _cut_seek_by(self, seconds):
        if self._cut_player_on:
            self.cut_player.seek_relative(seconds)

    def _cut_show_time(self, t, edge="start"):
        """Shows the video at time t: the frame a cut starting at t begins
        with (edge="start") or the last frame a cut ending at t keeps
        (edge="end"). Without the player, a still frame from ffmpeg."""
        if self._cut_player_on:
            if edge == "end":
                self.cut_player.show_last_frame_before(t)
            else:
                self.cut_player.show_first_frame_from(t)
            if not self._cut_scrubbing:
                self._pin_playhead(t)
        else:
            self._update_preview(t)

    def _cut_go_to(self, which):
        if self._cut_player_on:
            self.cut_player.set_paused(True)
            self._cut_show_time(self.start_sec if which == "start" else self.end_sec, which)

    def _cut_mark(self, which):
        """Sets the start or end at the frame on screen, so that frame is the
        first / last one in the cut. The time stored is half a frame before
        (start) or after (end) the frame's own time: a frame is in a cut
        when its time is at or after the start and before the end, and the
        half frame keeps that true for the marked frames even after the
        time is rounded to whole milliseconds for the time field and
        ffmpeg (frame times like 1.4666... s aren't whole milliseconds)."""
        if not self._cut_player_on or str(self.save_btn.cget("state")) == "disabled":
            return
        t = self.cut_player.position
        if t is None:
            return
        half = self.cut_player.frame_duration / 2
        if which == "start":
            self.start_sec = max(0.0, t - half)
            if self.start_sec >= self.end_sec:
                self.end_sec = self.duration
            self.handle_var.set("start")
        else:
            self.end_sec = min(self.duration, t + half)
            if self.end_sec <= self.start_sec:
                self.start_sec = 0.0
            self.handle_var.set("end")
        self.active_handle = which
        self._sync_entries()
        self._redraw_slider()
        self.status_lbl.config(
            text=f"{which.capitalize()} set at the frame at {seconds_to_ffmpeg_time(t)}")

    def _on_cut_player_changed(self):
        """Called (on the Tk thread) when the player's position, play state
        or file changed."""
        if not self._cut_player_on:
            return
        player = self.cut_player
        if player.failed:
            # mpv can't play this file (ffprobe could read it): still frames.
            self._cut_use_player(False)
            self._redraw_slider()
            self._update_preview(self.end_sec)
            self.status_lbl.config(text="The player can't open this file; showing still frames")
            return
        playing, pos = not player.paused, player.position
        pin = self._playhead_pin
        if pin is not None:
            target, until = pin
            if pos is None or abs(pos - target) <= 2 * player.frame_duration + 0.01 \
                    or time.monotonic() > until:
                # The player is there (or never got there, or a new file is opening).
                self._playhead_pin = None
            else:
                pos = target
        if self._handle_preview is not None:
            # A handle's frame is shown for the drag only: the controls
            # keep showing where the player is and goes back to.
            playing, pos = self._handle_preview["playing"], self._handle_preview["pos"]
        self.cut_play_btn.config(text="❚❚ Pause" if playing else "▶ Play")
        self._update_cut_audio_controls()
        if pos is None:
            self.cut_time_lbl.config(text="")
        else:
            self.cut_time_lbl.config(text=f"{seconds_to_ffmpeg_time(pos)} / "
                                          f"{seconds_to_ffmpeg_time(self.duration)}")
        if not self._cut_scrubbing:
            self._move_playhead(pos)

    # ---- preview canvas drawing --------------------------------------
    def _draw_preview_placeholder(self, text):
        self.raw_preview_img = None
        c = self.preview_canvas
        c.delete("all")
        w = c.winfo_width() or 400
        h = c.winfo_height() or 225
        if text == self.OPEN_PROMPT:
            self._draw_open_prompt(c, w, h)
        else:
            c.create_text(w / 2, h / 2, text=text, fill="#888888",
                          font=("Segoe UI", 12))

    def _redraw_preview_canvas(self):
        c = self.preview_canvas
        if self.raw_preview_img is None:
            self._draw_preview_placeholder(self.OPEN_PROMPT if not self.input_path else "")
            return
        cw = c.winfo_width()
        ch = c.winfo_height()
        if cw < 2 or ch < 2:
            return
        img = self.raw_preview_img
        scale = min(cw / img.width, ch / img.height)
        new_w = max(1, int(img.width * scale))
        new_h = max(1, int(img.height * scale))
        resized = img.resize((new_w, new_h), Image.LANCZOS)
        photo = ImageTk.PhotoImage(resized)
        self.preview_photo = photo
        c.delete("all")
        c.create_image(cw / 2, ch / 2, image=photo, anchor="center")

    # ---- loading --------------------------------------------------------
    def open_file_for_cut(self):
        path = filedialog.askopenfilename(
            title="Open video",
            filetypes=[("Video files", "*" + " *".join(VIDEO_EXTS)), ("All files", "*.*")],
        )
        if path:
            self._load_video_for_cut(path)

    def _load_video_for_cut(self, path, quiet=False):
        """quiet: opened from another tab (_show_shared_video), so a failure
        only shows in the status bar."""
        try:
            self.status_lbl.config(text="Reading video info...")
            self.update_idletasks()
            ffprobe = find_exe("ffprobe")
            result = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "format=duration,start_time:stream=start_time",
                 "-of", "json", path],
                capture_output=True, text=True, check=True, **SUBPROCESS_KWARGS,
            )
            info = json.loads(result.stdout or "{}")
            duration = float(info["format"]["duration"])
        except Exception as exc:
            if quiet:
                self.status_lbl.config(text=f"Couldn't open {os.path.basename(path)} on the Cut tab")
                return
            messagebox.showerror("Error loading video", str(exc))
            self.status_lbl.config(text="Ready")
            return
        self._share_video(0, path)
        # Where the video's frames start on the cut's clock (ffmpeg -ss's,
        # from the file's start time): the frame buttons step Start / End
        # on that grid (_cut_frame_step).
        try:
            streams = info.get("streams") or [{}]
            self.cut_video_start = (float(streams[0]["start_time"])
                                    - float(info["format"].get("start_time", 0)))
        except (KeyError, ValueError, TypeError):
            self.cut_video_start = 0.0

        self.input_path = path
        self.duration = duration
        self.file_size = os.path.getsize(path)
        self.start_sec = 0.0
        self.end_sec = duration

        self.filename_lbl.config(text=os.path.basename(path))
        self.info_lbl.config(text=f"[{seconds_to_hms(duration)}]   {format_size(self.file_size)}")
        # Takes away the empty-area prompt (the player covers the canvas,
        # which isn't redrawn otherwise).
        self._redraw_preview_canvas()

        self._set_cut_controls_enabled(True)
        self._sync_entries()
        self._start_keyframe_scan(path)
        self._start_waveform(0)   # the player may pick another track: _update_cut_audio_controls
        status = "Ready"
        if self.cut_player is not None:
            try:
                self.cut_player.load(path)
                self._cut_use_player(True)
            except Exception as exc:
                # No player after all: back to still frames.
                self.cut_player.frame.place_forget()
                self.cut_player = None
                self._cut_player_on = False
                status = str(exc)
        self._redraw_slider()
        if not self._cut_player_on:
            self._update_preview(self.end_sec)
        self.status_lbl.config(text=status)

    # ---- time controls ----------------------------------------------
    def _sync_entries(self):
        self.start_entry.delete(0, tk.END)
        self.start_entry.insert(0, seconds_to_ffmpeg_time(self.start_sec))
        self.end_entry.delete(0, tk.END)
        self.end_entry.insert(0, seconds_to_ffmpeg_time(self.end_sec))

    def _on_start_entry(self):
        try:
            val = hms_ms_to_seconds(self.start_entry.get())
        except ValueError as e:
            messagebox.showerror("Invalid time", str(e))
            self._sync_entries()
            return
        changed = val != hms_ms_to_seconds(seconds_to_ffmpeg_time(self.start_sec))
        self.start_sec = max(0.0, min(val, self.end_sec))
        self._sync_entries()
        self._redraw_slider()
        # Leaving an unchanged field (a click on the video, say) must not
        # move the player away from where it is.
        if changed or not self._cut_player_on:
            self._cut_show_time(self.start_sec, "start")

    def _on_end_entry(self):
        try:
            val = hms_ms_to_seconds(self.end_entry.get())
        except ValueError as e:
            messagebox.showerror("Invalid time", str(e))
            self._sync_entries()
            return
        changed = val != hms_ms_to_seconds(seconds_to_ffmpeg_time(self.end_sec))
        self.end_sec = min(self.duration, max(val, self.start_sec))
        self._sync_entries()
        self._redraw_slider()
        if changed or not self._cut_player_on:
            self._cut_show_time(self.end_sec, "end")

    def _set_active_handle(self, which):
        self.active_handle = which

    # ---- Cut Start / Cut End picker: a card round each time field ----------

    # The scissors on the Cut Start / Cut End cards: the Cut tab box's glyph
    # (font file on Windows, character), open on Cut Start, shut on Cut End
    # (_shut_scissors). Where the font isn't there (Linux) they are drawn.
    SCISSORS_GLYPH = ("segmdl2.ttf", "\ue8c6")

    # The volume sliders' look: (knob (TimeSlider.KNOBS), track width): no
    # knob but a dot under the mouse, a thick track. Their speaker is the
    # Windows 11 one (PlayerAudioControls.draw_speaker).
    VOLUME_LOOK = ("none", 6)

    def _style_volume_slider(self, slider):
        slider.set_look(*self.VOLUME_LOOK)

    def _field_picks_marker(self, which):
        """Clicking into a time field picks its marker."""
        if self.handle_var.get() != which:
            self.handle_var.set(which)
            self._set_active_handle(which)

    def _build_marker_picker(self):
        """(Re)makes the Cut Start / Cut End picker: a card holding each
        time field (_make_marker_card)."""
        row = self.start_entry.master
        # The fields back in the row on their own (the cards hold them).
        self.start_entry.pack_forget()
        self.end_entry.pack_forget()
        for w in set(self._marker_pickers.values()):
            w.destroy()
        self._marker_pickers = {}
        self.start_entry.pack(in_=row, side="left", padx=(2, 20))
        self.end_entry.pack(in_=row, side="left", padx=2)
        for which, entry, key in (("start", self.start_entry, "I"), ("end", self.end_entry, "O")):
            name = "Cut Start" if which == "start" else "Cut End"
            w = self._make_marker_card(which, name, entry)
            Tooltip(w, f"Shortcut: {key} sets the {which} at the frame on screen")
            self._marker_pickers[which] = w
        if self.cut_replay_btn is not None:
            # Between the cards: it plays what lies between them.
            self.cut_replay_btn.pack(side="left", padx=px(5), fill="y", after=self._marker_pickers["start"])
        if self.cut_mark_buttons:
            # Outside the cards: first and last in the row. All as tall as the
            # cards (fill y), so the row lines up.
            mark_start, mark_end = self.cut_mark_buttons
            mark_start.pack_forget()
            mark_end.pack_forget()
            others = row.pack_slaves()
            mark_start.pack(side="left", padx=(0, px(5)), fill="y", before=others[0])
            mark_end.pack(side="left", padx=(px(5), 0), fill="y", after=others[-1])
        self._style_marker_picker()
        self._marker_outer_padx = {}
        for w in row.pack_slaves() + [row.master]:
            if not getattr(w, "_bbvc_balances", False):
                w.bind("<Configure>", lambda e: self._balance_marker_row(), add="+")
                w._bbvc_balances = True
        self._balance_marker_row()

    @staticmethod
    def _pack_padx(w):
        """(left, right) of w's pack padx, in pixels."""
        padx = w.pack_info().get("padx", 0)
        parts = padx if isinstance(padx, (tuple, list)) else str(padx).split()
        pads = [w.winfo_pixels(str(p)) for p in parts] or [0]
        return pads[0], pads[-1]

    def _balance_marker_row(self):
        """The markers' row's middle (Replay, or the gap between the cards)
        at the row's centre, like Play above it and the step buttons' gap
        under it: the narrower side gets the difference as room at its
        outer end. When that doesn't fit (Linux's wider buttons), the Mark
        buttons and Replay give up their inner padding, all alike."""
        row = self.start_entry.master
        slaves = row.pack_slaves()
        if len(slaves) < 2:
            return
        if self.cut_replay_btn in slaves:
            i = slaves.index(self.cut_replay_btn)
            left, middle, right = slaves[:i], [self.cut_replay_btn], slaves[i + 1:]
        else:
            i = len(slaves) // 2
            left, middle, right = slaves[:i], [], slaves[i:]
        if not left or not right:
            return
        # What each had before any balancing: outer padding, button padding.
        outer = self._marker_outer_padx
        for w in (left[0], right[-1]):
            outer.setdefault(w, self._pack_padx(w))
        buttons = [w for w in (*self.cut_mark_buttons, self.cut_replay_btn) if w in slaves]
        inner = self.__dict__.setdefault("_marker_button_padx", {})
        for w in buttons:
            inner.setdefault(w, w.winfo_pixels(w.cget("padx")))

        def width(ws, shrink=0):
            total = 0
            for w in ws:
                pad = outer.get(w) or self._pack_padx(w)
                req = w.winfo_reqwidth()
                if w in inner:   # as it is with its own padding, less shrink
                    req += 2 * (inner[w] - w.winfo_pixels(w.cget("padx")) - shrink)
                total += req + pad[0] + pad[1]
            return total

        room = row.master.winfo_width()
        shrink = 0
        if room > 1 and buttons:
            most = min(inner[w] for w in buttons) - 1   # at least 1 px of padding left
            while shrink < most and (2 * max(width(left, shrink), width(right, shrink))
                                     + width(middle, shrink)) > room:
                shrink += 1
        for w in buttons:
            if w.winfo_pixels(w.cget("padx")) != inner[w] - shrink:
                w.configure(padx=inner[w] - shrink)
        diff = width(left, shrink) - width(right, shrink)
        (l0, l1), (r0, r1) = outer[left[0]], outer[right[-1]]
        for w, padx in ((left[0], (l0 + max(0, -diff), l1)), (right[-1], (r0, r1 + max(0, diff)))):
            if self._pack_padx(w) != padx:
                w.pack_configure(padx=padx)

    def _pick_marker(self, which):
        self.handle_var.set(which)
        self._set_active_handle(which)

    def _make_marker_card(self, which, name, entry):
        """A card holding a coloured bar, the label and the time field; a
        click anywhere on it (but the field) picks its marker."""
        row = entry.master
        card = tk.Frame(row, highlightthickness=1, borderwidth=0, cursor="hand2")
        card.pack(side="left", padx=px(5), before=entry)
        bar = tk.Frame(card, width=px(4))
        bar.pack(side="left", fill="y")
        # As wide as the name in bold, so bolding the picked one moves
        # nothing (a label with an image is sized in pixels).
        font, bold = self._marker_fonts()
        if PIL_AVAILABLE:
            # The scissors on the left (_style_marker_picker sets them),
            # the name after them, from a fixed place.
            text = " " + name
            image = self._scissors_image("#000000", shut=which == "end")
            label = tk.Label(card, text=text, cursor="hand2", image=image, compound="left", anchor="w",
                             width=image.width() + bold.measure(text),
                             height=bold.metrics("linespace"))
        else:
            label = tk.Label(card, text=name, cursor="hand2", image=self._pixel_image(),
                             compound="center", width=bold.measure(name),
                             height=bold.metrics("linespace"))
        label.pack(side="left", padx=(px(6), px(4)), pady=px(3))
        # The field is the row's child; the card only lays it out, above it.
        entry.pack_forget()
        entry.pack(in_=card, side="left", padx=(0, px(6)), pady=px(3))
        entry.tkraise(card)
        for w in (card, bar, label):
            w.bind("<Button-1>", lambda e, m=which: self._pick_marker(m))
        card._bbvc_parts = (bar, label)
        return card

    def _pixel_image(self):
        """A blank 1 x 1 image: a label or button showing it (compound
        "center") takes its width and height in pixels, not characters."""
        image = self.__dict__.get("_blank_image")
        if image is None:
            image = self._blank_image = tk.PhotoImage(width=1, height=1)
        return image

    def _marker_fonts(self):
        """(normal, bold) fonts for the pickers' labels."""
        font = tkfont.nametofont("TkDefaultFont")
        bold = self.__dict__.get("_marker_bold")
        if bold is None:
            bold = self._marker_bold = tkfont.Font(font=font)
            bold.configure(weight="bold")
        return font, bold

    @classmethod
    def _scissors_font_file(cls):
        """The scissors glyph's font file, or None where it isn't there
        (it is Windows' own)."""
        if os.name != "nt":
            return None
        path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", cls.SCISSORS_GLYPH[0])
        return path if os.path.isfile(path) else None

    def _scissors_image(self, color, shut=False):
        """The scissors as a smooth image as tall as a line of the cards'
        text, in color; shut: with their blades closed, for Cut End. Kept,
        so each is made once."""
        size = self._marker_fonts()[1].metrics("linespace")
        key = (color, size, shut)
        cache = self.__dict__.setdefault("_scissors_cache", {})
        if key not in cache:
            k = SMOOTH_SCALE
            big = size * k
            img = self._paint_scissors_glyph(color, big) if self._scissors_font_file() else None
            if img is not None and shut:
                img = self._shut_scissors(img)
            if img is None:
                img = self._paint_scissors_drawn("closed" if shut else "open", color, big)
            cache[key] = ImageTk.PhotoImage(img.reduce(k), master=self)
        return cache[key]

    @staticmethod
    def _shut_scissors(img):
        """The upright open scissors glyph (blades crossing at the pivot,
        rings below) with its blades closed and its rings swung in until
        they nearly touch, as scissors look shut. Each quarter round the
        pivot is turned about it (blades 90% of the way, rings a third);
        a round pivot screw covers the seam. img: RGBA, one colour."""
        from PIL import ImageChops, ImageDraw
        alpha = img.getchannel("A")
        w, h = alpha.size
        px_ = alpha.load()

        def runs(y):
            out, start = [], None
            for x in range(w):
                on = px_[x, y] > 127
                if on and start is None:
                    start = x
                elif not on and start is not None:
                    out.append((start, x))
                    start = None
            if start is not None:
                out.append((start, w))
            return out

        ink = [y for y in range(h) if runs(y)]
        if not ink:
            return img
        top, bottom = ink[0], ink[-1]
        tall = bottom - top
        # The pivot: the rows, in the top two thirds, where the blades cross
        # as one stroke.
        single = [y for y in range(top + tall // 10, top + tall * 2 // 3) if len(runs(y)) == 1]
        tip_row = top + tall // 12
        tips = runs(tip_row)
        if not single or len(tips) < 2:
            return img
        py = (single[0] + single[-1]) / 2
        run = runs(int(py))[0]
        cx = (run[0] + run[1]) / 2
        left, right = tips[0], tips[-1]
        a_left = math.degrees(math.atan2(cx - (left[0] + left[1]) / 2, py - tip_row))
        a_right = math.degrees(math.atan2((right[0] + right[1]) / 2 - cx, py - tip_row))
        stroke = left[1] - left[0]
        lift = max(1, int(h * 0.02))   # each quarter reaches past the pivot: no gap there
        out = Image.new("L", alpha.size, 0)
        for box, turn in (((0, 0, int(cx), int(py) + lift), -a_left * 0.9),
                          ((int(cx), 0, w, int(py) + lift), a_right * 0.9),
                          ((0, int(py) - lift, int(cx), h), a_right * 0.33),
                          ((int(cx), int(py) - lift, w, h), -a_left * 0.33)):
            piece = Image.new("L", alpha.size, 0)
            piece.paste(alpha.crop(box), box[:2])
            out = ImageChops.lighter(out, piece.rotate(turn, center=(cx, py), resample=Image.BICUBIC))
        r = stroke * 1.1
        ImageDraw.Draw(out).ellipse((cx - r, py - r, cx + r, py + r), fill=255)
        color = img.convert("RGB").getpixel((int(cx), int(py)))
        shut = Image.new("RGBA", alpha.size, color + (0,))
        shut.putalpha(out)
        return shut

    def _paint_scissors_glyph(self, color, big):
        """The scissors glyph on a big x big image, fitted inside it with a
        little margin, or None if its font can't be drawn with."""
        from PIL import ImageDraw, ImageFont
        char = self.SCISSORS_GLYPH[1]
        path = self._scissors_font_file()
        try:
            font = ImageFont.truetype(path, big)
            box = font.getbbox(char)
            fit = big * 0.92 / max(box[2] - box[0], box[3] - box[1], 1)
            font = ImageFont.truetype(path, max(1, int(big * fit)))
            box = font.getbbox(char)
            img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
            pos = ((big - (box[2] - box[0])) / 2 - box[0], (big - (box[3] - box[1])) / 2 - box[1])
            ImageDraw.Draw(img).text(pos, char, font=font, fill=color)
        except (OSError, ValueError, TypeError):
            return None
        return img if img.getbbox() else None

    @staticmethod
    def _paint_scissors_drawn(style, color, big):
        """Scissors pointing right, drawn on a big x big image: two finger
        rings on the left, blades crossing at the pivot to their tips."""
        from PIL import ImageDraw
        img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        s = big
        ring_r, ring_w = 0.17 * s, max(1, int(0.075 * s))
        rings = ((0.22 * s, 0.27 * s), (0.22 * s, 0.73 * s))
        spread = 0.24 if style == "open" else 0.035
        tips = ((0.97 * s, (0.5 - spread) * s), (0.97 * s, (0.5 + spread) * s))
        for (rx, ry), (tx, ty) in zip(rings, reversed(tips)):
            d.ellipse((rx - ring_r, ry - ring_r, rx + ring_r, ry + ring_r), outline=color, width=ring_w)
            # The blade: from the ring's edge toward the pivot, on to the
            # opposite tip, wide at the ring and sharp at the tip.
            ax, ay = rx + ring_r * 0.8, ry + (0.5 * s - ry) * 0.35
            length = math.hypot(tx - ax, ty - ay)
            nx, ny = -(ty - ay) / length, (tx - ax) / length
            w = 0.06 * s
            d.polygon(((ax + nx * w, ay + ny * w), (ax - nx * w, ay - ny * w), (tx, ty)), fill=color)
        return img

    # The picked marker's card edge and bar, Cut Start and Cut End alike: the
    # blue of the End handle.
    MARKER_PICKED_COLOR = "#1a73e8"

    def _style_marker_picker(self):
        """Colours the picker for the picked marker and the theme. Also runs
        after a theme switch."""
        pickers = getattr(self, "_marker_pickers", None)
        if not pickers:
            return
        picked = self.handle_var.get()
        dark = self._is_dark()
        # A handle drag sets the pick on every mouse move: only real changes.
        if (picked, dark, pickers.get("start")) == self._marker_styled:
            return
        self._marker_styled = (picked, dark, pickers.get("start"))
        panel = DARK["panel"] if dark else "#f0f0f0"
        text = DARK["text"] if dark else "#000000"
        for which, card in pickers.items():
            bar, label = card._bbvc_parts
            on = which == picked
            bg = self._theme_color("tabbox_selected") if on else panel
            edge = (self.MARKER_PICKED_COLOR if on
                    else DARK["border"] if dark else "#c4c4c8")
            for w in (card, label):
                self._configure_light(w, background=bg)
            self._configure_light(card, highlightbackground=edge, highlightcolor=edge)
            self._configure_light(bar, background=self.MARKER_PICKED_COLOR if on else bg)
            font, bold = self._marker_fonts()
            self._configure_light(label, foreground=text, font=bold if on else font)
            if PIL_AVAILABLE:
                label.configure(image=self._scissors_image(
                    self.MARKER_PICKED_COLOR if on else text, shut=which == "end"))

    def _nudge(self, delta_seconds):
        if not self.input_path:
            return
        # A nudge keeps the milliseconds: -5s from 12.345 is 7.345.
        if self.active_handle == "start":
            self.start_sec = round(max(0.0, min(self.start_sec + delta_seconds, self.end_sec)), 3)
            self._cut_show_time(self.start_sec, "start")
        else:
            self.end_sec = round(min(self.duration, max(self.end_sec + delta_seconds, self.start_sec)), 3)
            self._cut_show_time(self.end_sec, "end")
        self._sync_entries()
        self._redraw_slider()

    # ---- scenes for the bulk cutting list ---------------------------------
    # Several scenes of one video: each Start-End is added to the Bulk Cut
    # list (A), on the video's own row, and "Cut selected scenes" starts the
    # bulk cut there. The open video's scenes still waiting on that list are
    # shown in green on the slider and the waveform.
    @staticmethod
    def _same_file(a, b):
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))

    def _bulk_waiting_rows(self, path):
        """The Bulk Cut rows of this video that have not been cut yet."""
        return [it for it in list(self.bulk_items)
                if it["status"] in ("Pending", "Queued") and self._same_file(it["path"], path)]

    @property
    def cut_ranges(self):
        """The open video's scenes waiting on the Bulk Cut list, by start."""
        if not self.input_path:
            return []
        return sorted({tuple(r) for it in self._bulk_waiting_rows(self.input_path)
                       for r in it["ranges"]})

    def _cut_add_range(self):
        if not self.input_path or str(self.save_btn.cget("state")) == "disabled":
            return
        start, end = round(self.start_sec, 3), round(self.end_sec, 3)
        if end <= start:
            self.status_lbl.config(text="Nothing to add: the end is not after the start")
            return
        if (start, end) in self.cut_ranges:
            self.status_lbl.config(text="This scene is already on the bulk cutting list")
            return
        self._ensure_tab_built(2)
        path = self.input_path
        # Onto the row this tab made for the video, while it waits to be cut.
        row = next((it for it in self._bulk_waiting_rows(path) if it.get("from_cut")), None)
        if row is None:
            self._bulk_add_row(path, [(start, end)], from_cut=True)
            n = 1
        else:
            with self._bulk_lock:
                row["ranges"] = sorted(row["ranges"] + [(start, end)])
            self._bulk_show_ranges(row)
            n = len(row["ranges"])
        self.status_lbl.config(
            text=f"Scene added to the bulk cutting list: {seconds_to_ffmpeg_time(start)} - "
                 f"{seconds_to_ffmpeg_time(end)} ({n} scene{'' if n == 1 else 's'} of "
                 f"{os.path.basename(path)} there)")
        self._bulk_list_changed()

    def _update_cut_range_buttons(self):
        if not hasattr(self, "cut_ranges_btn"):
            return   # still building the tab
        enabled = bool(self.input_path) and str(self.save_btn.cget("state")) != "disabled"
        self.cut_add_range_btn.config(state="normal" if enabled else "disabled")
        # Starts the bulk cut: needs a row waiting and no run going.
        pending = any(it["status"] == "Pending" for it in self.bulk_items)
        self.cut_ranges_btn.config(
            state="normal" if pending and not self._bulk_running else "disabled")

    def _cut_ranges_at(self, x):
        t = self._x_to_exact_time(x)
        tol = px(3) * self.duration / max(1, self._track_x1 - self._track_x0)   # 3 px either side
        return [i for i, (s, e) in enumerate(self.cut_ranges) if s - tol <= t <= e + tol]

    def _cut_ranges_menu(self, event):
        """Right-click on the slider or the waveform: select or remove the
        marked scene there, or remove them all from the bulk cutting list."""
        ranges = self.cut_ranges
        if not self.input_path or not ranges:
            return
        menu = tk.Menu(self, tearoff=0)
        for i in self._cut_ranges_at(event.x):
            s, e = ranges[i]
            label = f"scene {i + 1} ({seconds_to_ffmpeg_time(s)} - {seconds_to_ffmpeg_time(e)})"
            menu.add_command(label=f"Select {label}",
                             command=lambda r=(s, e): self._cut_select_range(r))
            menu.add_command(label=f"Remove {label}",
                             command=lambda r=(s, e): self._cut_remove_ranges([r]))
            menu.add_separator()
        n = len(ranges)
        menu.add_command(label=f"Remove all {n} scene{'' if n == 1 else 's'}",
                         command=lambda: self._cut_remove_ranges(ranges))
        menu.configure(DARK_MENU if self._is_dark() else self._menu_light_colors)
        self._cut_ranges_menu_widget = menu   # for the tests
        x, y = event.x_root, event.y_root
        post_with_menu_margin(lambda: menu.tk_popup(x, y),
                              DARK_MENU["background"] if self._is_dark() else None)

    def _cut_select_range(self, rng):
        """Makes a marked scene the Start-End again, to look at or change it
        (an adjusted scene is added as a new one; remove the old one)."""
        self.start_sec, self.end_sec = rng
        self._sync_entries()
        self._redraw_slider()
        self._cut_show_time(self.start_sec, "start")

    def _cut_remove_ranges(self, ranges):
        """Takes the open video's scenes off the Bulk Cut list; a row left
        with none is removed."""
        gone = set(ranges)
        emptied = []
        changed = []
        with self._bulk_lock:
            for it in self._bulk_waiting_rows(self.input_path):
                kept = [r for r in it["ranges"] if tuple(r) not in gone]
                if len(kept) == len(it["ranges"]):
                    continue
                it["ranges"] = kept
                (changed if kept else emptied).append(it)
            if emptied:
                ids = {it["iid"] for it in emptied}
                self.bulk_items = [it for it in self.bulk_items if it["iid"] not in ids]
                self.bulk_table.delete(list(ids))
        for it in changed:
            self._bulk_show_ranges(it)
        if (emptied and self._bulk_preview_open()
                and self.bulk_preview.key in {it["iid"] for it in emptied}):
            self._bulk_close_preview()
        n = len(ranges)
        self.status_lbl.config(
            text=f"Removed {n} scene{'' if n == 1 else 's'} from the bulk cutting list")
        self._bulk_list_changed()

    def _cut_selected_scenes(self):
        """Goes to the Bulk Cut tab and starts the bulk cut there."""
        self._ensure_tab_built(2)
        self.notebook.select(self.bulk_tab)
        self.update_idletasks()
        self._bulk_start_clicked()

    # ---- keyframe ticks -----------------------------------------------
    def _start_keyframe_scan(self, path):
        """Ticks under the Cut slider mark the video's keyframes: where a
        classic cut snaps to, and where the exact modes copy instead of
        re-encoding. ffprobe lists the video packets without decoding them
        (about half a second per 2 GB of MP4 or MKV, a few seconds for
        MPEG-TS, which has no index) in the background, and the ticks fill
        in from left to right as it reads. Results are kept for the session."""
        self._stop_keyframe_scan()
        self.cut_keyframes = []
        try:
            key = file_identity(path)
            ffprobe = find_exe("ffprobe")
        except (OSError, RuntimeError):
            return
        if key in self._keyframe_cache:
            self.cut_keyframes = self._keyframe_cache[key]
            return
        try:
            proc = subprocess.Popen(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "packet=pts_time,dts_time,flags", "-of", "csv=p=0", path],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, **SUBPROCESS_KWARGS)
        except OSError:
            return
        self._keyframe_proc = proc
        threading.Thread(target=self._keyframe_scan_worker,
                         args=(self._keyframe_gen, proc, ffprobe, path, key), daemon=True).start()

    def _stop_keyframe_scan(self):
        self._keyframe_gen += 1   # whatever the old scan still sends is dropped
        proc, self._keyframe_proc = self._keyframe_proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def _keyframe_scan_worker(self, gen, proc, ffprobe, path, key):
        # Packet times are the file's own; the slider counts from its start
        # time, like ffmpeg -ss (see _probe_keyframes_near).
        origin = self._probe_start_time(ffprobe, path)
        batch, sent = [], time.monotonic()
        for line in proc.stdout:
            fields = line.strip().split(",")
            if len(fields) < 3 or "K" not in fields[2]:
                continue
            try:
                t = float(fields[0] if fields[0] not in ("", "N/A") else fields[1])
            except ValueError:
                continue
            batch.append(t - origin)
            if time.monotonic() - sent >= 0.25:
                self._keyframe_send(gen, batch, False, key)
                batch, sent = [], time.monotonic()
        proc.wait()
        self._keyframe_send(gen, batch, proc.returncode == 0, key)

    def _keyframe_send(self, gen, batch, done, key):
        try:
            self.after(0, self._on_keyframes_found, gen, batch, done, key)
        except (RuntimeError, tk.TclError):
            pass   # the window is gone

    def _on_keyframes_found(self, gen, batch, done, key):
        if gen != self._keyframe_gen:
            return
        self.cut_keyframes.extend(batch)
        if done:
            self.cut_keyframes.sort()
            self._keyframe_cache[key] = self.cut_keyframes
            self._keyframe_proc = None
        if batch:
            self._redraw_slider()

    def _keyframe_tick_xs(self, x_for):
        """Where the ticks go: one per pixel column, however many keyframes
        share it. Kept between redraws, which come with every drag step."""
        made_for = (self._keyframe_gen, len(self.cut_keyframes), self.duration,
                    self._track_x0, self._track_x1)
        if self._keyframe_xs[0] != made_for:
            xs = sorted({round(x_for(t)) for t in self.cut_keyframes if 0 <= t <= self.duration})
            self._keyframe_xs = (made_for, xs)
        return self._keyframe_xs[1]

    # ---- waveform strip -----------------------------------------------
    def _start_waveform(self, track):
        """Draws audio track `track` (0-based; "all": every track summed)
        under the slider, on the slider's time axis: what the player plays,
        so it follows the audio-track picker. ffmpeg draws it in the background
        (render_waveform: about 0.2 s for 2 GB of MP4, 0.7 s of MPEG-TS)
        and it is kept for the session."""
        if not self._strip_on or not self.input_path:
            return
        self._stop_waveform()
        self._wave_track = track
        self._wave_mask = None
        try:
            key = file_identity(self.input_path) + (track,)
        except OSError:
            key = None
        if key in self._wave_cache:
            self._wave_mask, self._wave_note = self._wave_cache[key], ""
            self._redraw_waveform()
            return
        self._wave_note = "Drawing the waveform…"
        self._redraw_waveform()
        gen, path, span = self._wave_gen, self.input_path, self.duration
        which = track
        if track == "all":
            which = list(range(len(self.cut_player.audio_tracks)))

        def started(proc):
            self._wave_proc = proc
            if gen != self._wave_gen:   # stopped before ffmpeg was even running
                proc.kill()

        def worker():
            mask, note = None, ""
            try:
                mask = render_waveform(path, which, 2400, 64, span=span,
                                       on_start=started).convert("L")
            except Exception as exc:
                # No audio stream: the strip stays empty, without a note.
                note = "" if "matches no streams" in str(exc) else "Waveform unavailable"
            try:
                self.after(0, self._on_waveform_ready, gen, key, mask, note)
            except (RuntimeError, tk.TclError):
                pass   # the window is gone

        threading.Thread(target=worker, daemon=True).start()

    def _stop_waveform(self):
        self._wave_gen += 1   # whatever the old render still sends is dropped
        proc, self._wave_proc = self._wave_proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def _on_waveform_ready(self, gen, key, mask, note):
        if gen != self._wave_gen:
            return
        self._wave_proc = None
        self._wave_mask, self._wave_note = mask, note
        if mask is not None and key is not None:
            self._wave_cache[key] = mask
        self._redraw_waveform()

    def _redraw_waveform(self):
        """The strip, at the slider canvas's bottom: the waveform across the
        same span as the slider's track, blue inside the selection, green in
        ranges marked for Bulk Cut and gray elsewhere. The playhead runs
        down through it from the slider (_redraw_slider)."""
        if not self._strip_on:
            return
        c = self.slider_canvas
        c.delete("wave")
        w, y0, h = c.winfo_width(), self._strip_y0, self._strip_h
        if w < 2 or not self.input_path or self.duration <= 0:
            return
        x0, x1 = px(10), max(px(20), w - px(10))   # as the slider's track
        if self._wave_mask is not None:
            c.create_image(x0, y0, image=self._waveform_photo(x0, x1, h), anchor="nw",
                           tags=("wave",))
        elif self._wave_note:
            c.create_text(x0, y0 + h / 2, text=self._wave_note, anchor="w",
                          font=("Segoe UI", 8), fill=self._theme_color("wave"), tags=("wave",))
        c.tag_lower("wave")   # under the playhead

    def _waveform_photo(self, x0, x1, h):
        width = x1 - x0
        sel0 = round((x1 - x0) * self.start_sec / self.duration)
        sel1 = round((x1 - x0) * self.end_sec / self.duration)
        spans = tuple((round(width * s / self.duration), round(width * e / self.duration))
                      for s, e in self.cut_ranges)
        bg = self.slider_canvas.cget("bg")
        made_for = (id(self._wave_mask), width, h, sel0, sel1, spans, bg,
                    self._theme_color("wave"), self._theme_color("range"))
        if self._wave_photo[0] == made_for:
            return self._wave_photo[1]
        if self._wave_sized[0] != (id(self._wave_mask), width, h):
            self._wave_sized = ((id(self._wave_mask), width, h),
                                self._wave_mask.resize((width, h), Image.BILINEAR))
        mask = self._wave_sized[1]
        rgb = tuple(v // 257 for v in self.slider_canvas.winfo_rgb(bg))
        img = Image.new("RGB", (width, h), rgb)
        img.paste(self._theme_color("wave"), (0, 0, width, h), mask)
        for a, b in spans:   # ranges marked for Bulk Cut
            if b > a:
                img.paste(self._theme_color("range"), (a, 0, b, h), mask.crop((a, 0, b, h)))
        if sel1 > sel0:
            img.paste("#3d8bfd", (sel0, 0, sel1, h), mask.crop((sel0, 0, sel1, h)))
        photo = ImageTk.PhotoImage(img)
        self._wave_photo = (made_for, photo)
        return photo

    def _wave_click(self, event):
        """With the player, a press on the strip moves the playhead (never a
        handle); without it, the strip works like the slider."""
        self.cut_tab.focus_set()   # off a time field, so the keys work
        self._drag_target = "playhead"
        self._cut_scrubbing = True
        self._slider_drag(event)

    # ---- slider -------------------------------------------------------
    def _redraw_slider(self):
        c = self.slider_canvas
        c.delete("all")
        w = c.winfo_width() or px(780)
        y = px(12)
        track_x0, track_x1 = px(10), max(px(20), w - px(10))
        # Always draw the bare track, even with no file loaded yet, so the
        # slider doesn't visibly pop into existence the moment a video is
        # opened -- it's there from the start, just inert.
        self._smooth_bar(c, track_x0, track_x1, y, px(4), self._theme_color("slider_track"))
        self._track_x0, self._track_x1 = track_x0, track_x1
        self._redraw_waveform()   # the selection is shown there too
        if not self.input_path or self.duration <= 0:
            return

        def x_for(t):
            frac = t / self.duration
            return track_x0 + frac * (track_x1 - track_x0)

        if self.cut_keyframes:
            tick = self._theme_color("slider_keyframe")
            for x in self._keyframe_tick_xs(x_for):
                c.create_line(x, y + px(4), x, y + px(9), fill=tick, width=max(1, px(1)),
                              tags=("keyframe",))
        if self.cut_ranges:
            # Ranges marked for Bulk Cut: a wider band under the selection,
            # with a bar at each end so neighbouring ranges stay apart.
            band, edge = self._theme_color("range"), self._theme_color("range_edge")
            for s, e in self.cut_ranges:
                a, b = x_for(s), x_for(e)
                c.create_line(a, y, b, y, fill=band, width=px(8), tags=("range",))
                for x in (a, b):
                    c.create_line(x, y - px(5), x, y + px(5), fill=edge, width=max(1, px(1)),
                                  tags=("range",))
        sx, ex = x_for(self.start_sec), x_for(self.end_sec)
        self._smooth_bar(c, sx, ex, y, px(4), "#3d8bfd")
        if self._cut_player_on:
            # The playhead: where the player is. Under the handles, so they
            # stay visible and grabbable when it sits on one.
            c.create_line(0, 0, 0, 0, fill=PLAYHEAD_COLOR, width=px(2), tags=("playhead",))
            if PIL_AVAILABLE:   # its tip smooth: an image, moved with the line
                c.create_image(0, 0, anchor="n", image=self._playhead_tip_image(),
                               tags=("playhead_top",))
            else:
                c.create_polygon(0, 0, 0, 0, 0, 0, fill=PLAYHEAD_COLOR, outline="",
                                 tags=("playhead_top",))
            back, pin = self._handle_preview, self._playhead_pin
            if back is not None:           # a dragged handle's frame is on screen
                self._move_playhead(back["pos"])
            elif pin is not None:          # a seek on its way
                self._move_playhead(pin[0])
            else:
                self._move_playhead(self.cut_player.position)
        r = px(7)
        start_color = self._theme_color("slider_start")
        if PIL_AVAILABLE:   # smooth edges (draw_smooth)
            smooth_disc(c, sx, y, r, color_hex(c, start_color))
            smooth_disc(c, ex, y, r, "#1a73e8")
        else:
            c.create_oval(sx - r, y - r, sx + r, y + r, fill=start_color, outline="")
            c.create_oval(ex - r, y - r, ex + r, y + r, fill="#1a73e8", outline="")

    @staticmethod
    def _smooth_bar(c, x0, x1, y, width, color):
        """A line from x0 to x1 at y with round ends: smooth ends with
        Pillow, otherwise the canvas's own."""
        if not PIL_AVAILABLE:
            c.create_line(x0, y, x1, y, fill=color, width=width, capstyle="round")
            return
        color = color_hex(c, color)
        top = round(y - width / 2)
        mid = top + width / 2
        c.create_rectangle(x0, top, x1, top + width, fill=color, outline="")
        for x in (x0, x1):
            smooth_disc(c, x, mid, width / 2, color)

    def _move_playhead(self, t):
        c = self.slider_canvas
        if t is None or not self.duration:
            c.itemconfigure("playhead", state="hidden")
            c.itemconfigure("playhead_top", state="hidden")
            return
        x = self._time_to_x(min(max(t, 0.0), self.duration))
        h = c.winfo_height() or int(c.cget("height"))   # down through the strip
        a = px(5)
        c.coords("playhead", x, a, x, h)
        if c.type("playhead_top") == "image":
            c.coords("playhead_top", x, 0)
        else:
            c.coords("playhead_top", x - a, 0, x + a, 0, x, a + px(1))
        c.itemconfigure("playhead", state="normal")
        c.itemconfigure("playhead_top", state="normal")

    def _playhead_tip_image(self):
        """The playhead's tip on the Cut slider: a smooth downward triangle
        2 x px(5) wide, px(6) tall, its middle on the line. Made once."""
        image = self.__dict__.get("_playhead_tip")
        if image is None:
            a = px(5)
            p = SmoothPainter(self.slider_canvas, 2 * a, a + px(1) + 1)
            p.create_polygon(0, 0, 2 * a, 0, a, a + px(1), fill=PLAYHEAD_COLOR)
            image = self._playhead_tip = p.photo()
        return image

    def _pin_playhead(self, t):
        """For a seek just sent to time t: the playhead goes there now, not
        when the player's next report (polled every 33 ms) arrives, and
        stays until the player is there, so it never steps back to the old
        position in between. Released after a second regardless."""
        self._playhead_pin = (t, time.monotonic() + 1.0)
        self._move_playhead(t)

    def _x_to_time(self, x):
        # A dragged handle's time, to the millisecond like the playhead's:
        # whole milliseconds, as the time fields show and ffmpeg is given,
        # so the time on screen is exactly the one the cut uses.
        return round(self._x_to_exact_time(x), 3)

    def _time_to_x(self, t):
        frac = t / self.duration if self.duration else 0
        return self._track_x0 + frac * (self._track_x1 - self._track_x0)

    def _slider_click(self, event):
        """Without the player, a click moves the nearer handle there. With
        it, only a press on a handle drags the handle; anywhere else moves
        the playhead (scrubbing), and I / O then set the range.
        A press on a handle only grabs it, and makes it the one the step
        buttons move: the handle only moves once it is dragged, so a click
        on it leaves it (and its exact time) alone. While it is dragged the
        video shows its frame, but the playhead stays, and on release the
        video goes back there."""
        if not self.input_path:
            return
        if self._cut_player_on and self._strip_on and event.y >= self._strip_y0:
            self._wave_click(event)
            return
        sx = self._time_to_x(self.start_sec)
        ex = self._time_to_x(self.end_sec)
        self._drag_target = "start" if abs(event.x - sx) <= abs(event.x - ex) else "end"
        on_handle = min(abs(event.x - sx), abs(event.x - ex)) <= px(9)
        self._grab_x = None
        if self._cut_player_on:
            self.cut_tab.focus_set()   # off a time field, so the keys work
            if not on_handle:
                self._drag_target = "playhead"
                self._cut_scrubbing = True
        if on_handle:
            self._grab_x = event.x
            self.handle_var.set(self._drag_target)
            self.active_handle = self._drag_target
            return
        self._slider_drag(event)

    def _slider_drag(self, event):
        if not self.input_path or not self._drag_target:
            return
        if self._grab_x is not None:
            if abs(event.x - self._grab_x) <= px(3):
                return   # a wobble while clicking: still a click
            self._grab_x = None
        if self._drag_target == "playhead":
            t = self._x_to_exact_time(event.x)
            self._cut_scrub_time = t
            self._move_playhead(t)
            self._scrub_seek_later(t)
            return
        t = self._x_to_time(event.x)
        if self._drag_target == "start":
            self.start_sec = min(t, self.end_sec)
            self.handle_var.set("start")
            self.active_handle = "start"
        else:
            self.end_sec = max(t, self.start_sec)
            self.handle_var.set("end")
            self.active_handle = "end"
        if self._cut_player_on and self._handle_preview is None:
            player = self.cut_player
            self._handle_preview = {"pos": player.position, "playing": not player.paused}
            self._cut_scrubbing = True    # the playhead stays put
            player.set_paused(True)
        self._sync_entries()
        self._redraw_slider()
        if self._cut_player_on:
            self._scrub_seek_later(t, self._drag_target)
        else:
            self._update_preview(t)

    SCRUB_REST_MS = 5

    def _scrub_seek_later(self, t, edge=None):
        """While a playhead or handle is dragged, the video follows once the
        mouse rests (SCRUB_REST_MS). A seek on every mouse move kept the
        player decoding frames that were stale a moment later, and the drag
        stuttered. The playhead's is a keyframe seek (fast; release shows
        the exact frame); a handle's (edge "start" / "end") shows exactly
        the frame the cut starts or ends with."""
        self._cancel_scrub_seek()
        self._scrub_seek_id = self.after(self.SCRUB_REST_MS, self._scrub_seek_now, t, edge)

    def _scrub_seek_now(self, t, edge):
        self._scrub_seek_id = None
        if not self._cut_player_on:
            return
        if edge is None:
            self.cut_player.seek(t, exact=False)
        else:
            self._cut_show_time(t, edge)

    def _cancel_scrub_seek(self):
        if self._scrub_seek_id is not None:
            self.after_cancel(self._scrub_seek_id)
            self._scrub_seek_id = None

    def _slider_release(self, event):
        self._cancel_scrub_seek()   # release seeks itself, exactly
        target, self._drag_target = self._drag_target, None
        clicked, self._grab_x = self._grab_x is not None, None
        back, self._handle_preview = self._handle_preview, None
        if back is not None:
            # A handle was dragged: the video goes back to the playhead.
            self._cut_scrubbing = False
            if self._cut_player_on and back["pos"] is not None:
                self.cut_player.seek(back["pos"])
                self._pin_playhead(back["pos"])   # not at the handle's frame meanwhile
                if back["playing"]:
                    self.cut_player.set_paused(False)
            return
        if not self._cut_player_on or target is None or clicked:
            return
        if target == "playhead":
            self._cut_scrubbing = False
            self.cut_player.seek(self._cut_scrub_time)
            self._pin_playhead(self._cut_scrub_time)

    def _x_to_exact_time(self, x):
        frac = (x - self._track_x0) / max(1, (self._track_x1 - self._track_x0))
        return min(1.0, max(0.0, frac)) * self.duration

    # ---- preview frame extraction -------------------------------------
    def _update_preview(self, at_seconds):
        if not self.input_path or not PIL_AVAILABLE:
            return
        # While the slider is being dragged, this gets called on every
        # single mouse-motion tick -- dozens of times a second. Firing an
        # ffmpeg process per tick is what caused the flood of command
        # prompts flashing open/close. Debounce it: cancel any pending
        # extraction and only actually start one after motion pauses
        # briefly (50ms), so a drag results in one extraction, not fifty.
        if self._preview_after_id is not None:
            self.after_cancel(self._preview_after_id)
        self._preview_after_id = self.after(
            50, lambda: self._start_preview_extraction(at_seconds)
        )

    def _start_preview_extraction(self, at_seconds):
        self._preview_after_id = None
        self._preview_request_id += 1
        request_id = self._preview_request_id
        threading.Thread(
            target=self._extract_frame, args=(at_seconds, request_id), daemon=True
        ).start()

    def _extract_frame(self, at_seconds, request_id):
        # Each request gets its own temp filename so an old, still-running
        # extraction can never overwrite the file a newer one is reading.
        out_path = os.path.join(
            tempfile.gettempdir(), f"_lossless_cutter_preview_{request_id}.jpg"
        )
        try:
            ffmpeg = find_exe("ffmpeg")
            # Seeking to (or past) the container's exact reported duration
            # lands after the last frame's own timestamp -- ffmpeg then
            # extracts nothing at all ("Output file is empty") instead of
            # falling back to the last frame, which is why the very first
            # preview (initially showing the end of the full-range
            # selection) used to come up blank until the slider moved
            # somewhere safely inside the video. Keep the seek target a
            # hair before the end whenever it's this close to it.
            seek_at = at_seconds
            if self.duration > 0:
                seek_at = min(seek_at, max(0.0, self.duration - 0.05))
            cmd = [ffmpeg, "-y", "-ss", seconds_to_ffmpeg_time(seek_at), "-i", self.input_path,
                   "-frames:v", "1", "-q:v", "3", out_path]
            subprocess.run(cmd, capture_output=True, check=True, **SUBPROCESS_KWARGS)
            img = Image.open(out_path).convert("RGB")
            img.load()
            self.after(0, lambda: self._set_raw_preview_if_current(img, request_id))
        except Exception:
            pass
        finally:
            try:
                os.remove(out_path)
            except OSError:
                pass

    def _set_raw_preview_if_current(self, img, request_id):
        # If a newer scrub position has already been requested, this result
        # is stale (the user has since moved on) -- drop it instead of
        # flickering the preview back to an older frame.
        if request_id != self._preview_request_id:
            return
        self.raw_preview_img = img
        self._redraw_preview_canvas()

    # ---- save (lossless cut) -------------------------------------------
    def _save_scene_clicked(self):
        if not self.input_path:
            return
        if self.end_sec <= self.start_sec:
            messagebox.showerror("Invalid range", "End time must be after start time.")
            return

        source_dir = os.path.dirname(os.path.abspath(self.input_path))
        base_name = os.path.splitext(os.path.basename(self.input_path))[0]
        ext = os.path.splitext(self.input_path)[1]
        suffix = format_clip_suffix(self.start_sec, self.end_sec)
        default_name = f"{base_name}.CUT.{suffix}{ext}"

        from_dialog = not self.save_to_source_var.get()
        if not from_dialog:
            # Save straight into the source video's folder, no dialog.
            out_path = os.path.join(source_dir, default_name)
        else:
            out_path = filedialog.asksaveasfilename(
                title="Save cut scene as", initialdir=source_dir,
                initialfile=default_name, defaultextension=ext,
                filetypes=[("Same format", f"*{ext}"), ("All files", "*.*")],
            )
            if not out_path:
                return
        # The native save dialog already asks before replacing a file.
        out_path = self._check_output_path(out_path, [self.input_path], prompt_overwrite=not from_dialog)
        if not out_path:
            return

        cut_mode = self.cut_mode_var.get()
        search_window = self.cut_search_window

        self._set_cut_controls_enabled(False)
        self.status_lbl.config(text="Cutting... 0%")
        threading.Thread(
            target=self._run_cut, args=(out_path, cut_mode, search_window), daemon=True
        ).start()

    def _run_cut(self, out_path, cut_mode, search_window):
        try:
            result = self._lossless_cut(
                self.input_path, self.start_sec, self.end_sec, out_path,
                mode=cut_mode, search_window=search_window,
                progress=self._progress_reporter("Cutting...", "cut"),
            )
            if result["mode"] == "classic":
                # Snapped to keyframes: check where it landed, to say by how
                # many frames (_describe_cut_result).
                self.after(0, lambda: self.status_lbl.config(text="Checking where the cut landed..."))
                try:
                    result["slide"] = self._measure_cut_slide(
                        find_exe("ffmpeg"), find_exe("ffprobe"), self.input_path,
                        self.start_sec, self.end_sec, out_path)
                except (OSError, ValueError):
                    result["slide"] = None
            self.after(0, lambda: self._cut_done(out_path, True, result))
        except Exception as exc:
            self.after(0, lambda e=str(exc): self._cut_done(e, False, None))

    # ---- frame-accurate lossless cutting ---------------------------------
    # A pure stream copy (-c copy) can only ever *start* output at a
    # keyframe -- there's no decoding happening, so ffmpeg can't trim mid-
    # GOP. Left alone, that means a cut can start noticeably earlier than
    # requested if the nearest keyframe is a couple of seconds back. This is
    # the same "smart cut" approach editors like Moo0 Video Cutter use to
    # land on the exact requested second: only the small boundary segments
    # that don't already sit on a keyframe get decoded and re-encoded (GPU-
    # accelerated when available); everything else stays a byte-identical
    # stream copy, joined back together with the same TS-intermediate
    # concat technique _run_combine uses (see its docstring for why TS
    # avoids the "frozen frame" MP4-concat bug). Shared by Cut and Bulk Cut.
    def _probe_keyframes_near(self, ffprobe_exe, path, center_sec, pad=8.0):
        """Keyframe times within `pad` seconds of center_sec, on ffmpeg -ss's
        clock: seconds from the file's start time. ffprobe itself reads and
        reports the file's own timestamps, which don't start at 0 in e.g.
        many MPEG-TS files (1.4 s is common), so they are shifted here."""
        origin = self._probe_start_time(ffprobe_exe, path)
        cmd = [
            ffprobe_exe, "-v", "error", "-select_streams", "v:0",
            "-read_intervals", probe_interval(origin, center_sec - pad, center_sec + pad),
            "-skip_frame", "nokey",
            "-show_entries", "frame=best_effort_timestamp_time",
            "-of", "csv=p=0", path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, **SUBPROCESS_KWARGS)
        times = []
        for line in proc.stdout.splitlines():
            line = line.strip().rstrip(",")
            if not line:
                continue
            try:
                times.append(float(line) - origin)
            except ValueError:
                pass
        return sorted(times)

    def _probe_start_time(self, ffprobe_exe, path):
        """The file's start time: what ffmpeg -ss (and the player) count
        from. 0 when there is none."""
        try:
            out = subprocess.run(
                [ffprobe_exe, "-v", "error", "-show_entries", "format=start_time",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, **SUBPROCESS_KWARGS,
            ).stdout.strip()
            return float(out)
        except (ValueError, OSError):
            return 0.0

    def _probe_frame_duration(self, ffprobe_exe, path):
        """Seconds per frame of the first video stream (1/30 if unknown)."""
        try:
            out = subprocess.run(
                [ffprobe_exe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=avg_frame_rate,r_frame_rate", "-of", "csv=p=0", path],
                capture_output=True, text=True, **SUBPROCESS_KWARGS,
            ).stdout.strip().splitlines()
        except OSError:
            out = []
        for rate in (out[0].split(",") if out else []):
            num, _, den = rate.partition("/")
            try:
                fps = float(num) / float(den or 1)
            except (ValueError, ZeroDivisionError):
                continue
            if 1 <= fps <= 1000:
                return 1.0 / fps
        return 1.0 / 30

    @staticmethod
    def _frame_hashes(ffmpeg, args):
        """[(time, md5)] of the video frames ffmpeg decodes with args (its
        input options and -i), each frame's time from its own timestamps.
        Every frame kept, none made up (passthrough)."""
        for sync in (["-fps_mode", "passthrough"], ["-vsync", "0"]):   # -fps_mode: ffmpeg 5.1+
            proc = subprocess.run(
                [ffmpeg, "-v", "error", *args, "-map", "0:v:0", *sync, "-f", "framemd5", "-"],
                capture_output=True, text=True, **SUBPROCESS_KWARGS)
            if proc.returncode == 0:
                break
        else:
            return []
        tb, frames = 1.0, []
        for line in proc.stdout.splitlines():
            if line.startswith("#tb 0:"):
                num, _, den = line.split(":", 1)[1].strip().partition("/")
                tb = float(num) / float(den or 1)
            elif line and not line.startswith("#"):
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 6 and parts[0] == "0":
                    frames.append((int(parts[1]) * tb, parts[5]))
        return frames

    def _measure_cut_slide(self, ffmpeg, ffprobe, input_path, start_sec, end_sec, out_path):
        """How many frames a cut's first and last frames are from the ones
        asked for (the frame shown at start_sec, the last one before
        end_sec): (start, end), negative = earlier, positive = later. Found
        by matching the cut's first and last frames to the source's frames
        near start_sec and end_sec. None when they can't be matched."""
        origin = self._probe_start_time(ffprobe, input_path)

        def around(at_sec, pad):
            seek = max(0.0, at_sec - pad)
            frames = self._frame_hashes(ffmpeg, [
                "-copyts", "-ss", seconds_to_ffmpeg_time(seek),
                "-t", f"{at_sec + pad - seek:.3f}", "-i", input_path])
            return [(t - origin, h) for t, h in frames]

        def slide(frames, at_sec, md5, before_end):
            eps = 0.0005   # times are whole milliseconds
            if before_end:
                want = [i for i, (t, _h) in enumerate(frames) if t < at_sec - eps]
            else:
                want = [i for i, (t, _h) in enumerate(frames) if t <= at_sec + eps]
            found = [i for i, (_t, h) in enumerate(frames) if h == md5]
            if not (want and found):
                return None
            # Identical frames (a still picture): the nearest.
            return min((i - want[-1] for i in found), key=abs)

        # Each decode starts at a keyframe, so the four run side by side.
        with ThreadPoolExecutor(4) as pool:
            jobs = [pool.submit(self._frame_hashes, ffmpeg, ["-i", out_path, "-frames:v", "1"]),
                    pool.submit(self._frame_hashes, ffmpeg, ["-sseof", "-0.5", "-i", out_path]),
                    pool.submit(around, start_sec, 0.5), pool.submit(around, end_sec, 0.5)]
            first, last, near_start, near_end = (j.result() for j in jobs)
            if not first or not last:
                return None
            start = slide(near_start, start_sec, first[0][1], False)
            end = slide(near_end, end_sec, last[-1][1], True)
            # Further than that (up to a long keyframe interval): wider.
            wide = [pool.submit(around, at, 12.0) if got is None else None
                    for at, got in ((start_sec, start), (end_sec, end))]
            if wide[0] is not None:
                start = slide(wide[0].result(), start_sec, first[0][1], False)
            if wide[1] is not None:
                end = slide(wide[1].result(), end_sec, last[-1][1], True)
        if start is None or end is None:
            return None
        return start, end

    def _probe_keyframe_dts(self, ffprobe_exe, path, keyframe_sec):
        """Decode timestamp (same clock as _probe_keyframes_near) of the
        keyframe packet at keyframe_sec, or None. With B-frames, a packet's
        decode time comes before its display time, and a stream copy's -to
        stops at the first packet whose decode time reaches it."""
        origin = self._probe_start_time(ffprobe_exe, path)
        at = keyframe_sec + origin
        try:
            out = subprocess.run(
                [ffprobe_exe, "-v", "error", "-select_streams", "v:0",
                 "-read_intervals", probe_interval(origin, keyframe_sec - 2, keyframe_sec + 1),
                 "-show_entries", "packet=pts_time,dts_time,flags", "-of", "csv=p=0", path],
                capture_output=True, text=True, **SUBPROCESS_KWARGS,
            ).stdout.splitlines()
        except OSError:
            return None
        for line in out:
            parts = line.strip().split(",")
            if len(parts) < 3 or "K" not in parts[2]:
                continue
            try:
                pts, dts = float(parts[0]), float(parts[1])
            except ValueError:
                continue
            if abs(pts - at) < 0.001:
                return dts - origin
        return None

    def _probe_av_offset(self, ffprobe_exe, path):
        """How far after the file's start time its first video or audio
        stream starts (0 unless, say, a subtitle stream starts first). See
        keep_av_streams_args for why the cut's commands never seek to less."""
        origin = self._probe_start_time(ffprobe_exe, path)
        try:
            out = subprocess.run(
                [ffprobe_exe, "-v", "error", "-show_entries", "stream=codec_type,start_time",
                 "-of", "csv=p=0", path],
                capture_output=True, text=True, **SUBPROCESS_KWARGS,
            ).stdout.split()
        except OSError:
            return 0.0
        starts = []
        for row in out:
            kind, _, value = row.strip(",").partition(",")
            if kind in ("video", "audio"):
                try:
                    starts.append(float(value))
                except ValueError:
                    pass
        return max(0.0, min(starts) - origin) if starts else 0.0

    def _probe_first_frame_at(self, ffprobe_exe, path, at_sec):
        """Display time (same clock as _probe_keyframes_near) of the first
        video frame shown at or after at_sec; at_sec if none is found."""
        origin = self._probe_start_time(ffprobe_exe, path)
        at = at_sec + origin
        try:
            out = subprocess.run(
                [ffprobe_exe, "-v", "error", "-select_streams", "v:0",
                 "-read_intervals", probe_interval(origin, at_sec - 2, at_sec + 2),
                 "-show_entries", "packet=pts_time", "-of", "csv=p=0", path],
                capture_output=True, text=True, **SUBPROCESS_KWARGS,
            ).stdout.split()
        except OSError:
            return at_sec
        later = []
        for value in out:
            try:
                pts = float(value.strip(","))
            except ValueError:
                continue
            if pts >= at - 1e-6:
                later.append(pts)
        return min(later) - origin if later else at_sec

    def _ffmpeg_stream_copy_cut(self, ffmpeg, input_path, start_sec, end_sec, out_path,
                                progress=None):
        # No -avoid_negative_ts here: on a multi-audio-track source, "make_zero"
        # was found (empirically, while building this) to pull in ~0.5s of
        # extra lead-in video to keep every stream's zero point aligned, even
        # when -ss already landed exactly on a keyframe -- silently undoing
        # the frame-accuracy this method exists to guarantee. Omitting it
        # (ffmpeg's own default for this muxer) produced a clean, exactly
        # zero-based, correctly-durationed result in that same test.
        cmd = [
            ffmpeg, "-y", "-ss", seconds_to_ffmpeg_time(start_sec),
            "-to", seconds_to_ffmpeg_time(end_sec), "-i", input_path,
            # Every audio track: ffmpeg on its own keeps only one.
            "-map", "0:v:0", "-map", "0:a?",
            "-c", "copy", out_path,
        ]
        proc = run_ffmpeg(cmd, end_sec - start_sec, progress)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[-2000:])

    # The cut's video is made of pieces (MPEG-TS files, one video stream
    # each) that _join_cut_pieces joins byte for byte. That only works if
    # they share one clock, so every piece is written with each frame's
    # time in the source minus zero_sec, the cut's start. ffmpeg's
    # avoid_negative_ts would move a piece whose first decode time is just
    # below 0 (a B-frame stream decodes ahead of what it shows), so it is
    # off; the MPEG-TS muxer's own 1.4 s offset keeps the stored times
    # positive. These commands write only the video (or only the audio), so
    # they keep the source's other streams in use (keep_av_streams_args),
    # never seek to less than min_seek (_probe_av_offset), and give -ss in
    # microseconds so that holds exactly.
    def _ffmpeg_reencode_segment(self, ffmpeg, input_path, start_sec, end_sec, seek_sec,
                                  zero_sec, min_seek, out_path, encoder_args, dts_lead=0.0,
                                  progress=None):
        """Re-encodes the video frames shown from start_sec up to (not
        including) end_sec into a piece for _join_cut_pieces.

        The frames are picked by the trim filter on their own times (counted
        from the -ss point). -ss and -to can't do it exactly: -to becomes a
        length counted from the first frame kept, which can let one frame
        too many through, and in an MPEG-TS file -ss can land between
        keyframes, so the frames up to the next keyframe can't be decoded
        and are lost. So ffmpeg starts reading at seek_sec, which the
        caller puts a second before the keyframe the first frame needs
        (mpv's player seeks a second early in the same way), and -to, a
        second after end_sec, only stops it reading on to the end of the
        file.

        B-frames are off (-bf 0): in a piece this short the encoder can't
        drain its reorder buffer, and the last frames came out with no
        display time. A copied piece with B-frames, though, decodes ahead
        of what it shows: each packet's decode time (dts) is dts_lead before
        the display time of its frame. With this piece's dts equal to its
        pts, the copied piece after it would start with a dts at or before
        this piece's last one, so setts moves this piece's decode times the
        same amount earlier."""
        ss_sec = round(max(min_seek, min(start_sec, max(0.0, seek_sec))), 6)
        ss = f"{ss_sec:.6f}"
        first = max(0.0, start_sec - ss_sec)
        keep = max(first, end_sec - ss_sec)
        vf = (f"trim=start={first:.6f}:end={keep:.6f},"
              f"setpts=PTS+{ss_sec - zero_sec:.6f}/TB")
        lead_bsf = ["-bsf:v", f"setts=dts=PTS-{dts_lead:.6f}/TB"] if dts_lead > 0 else []

        def build(enc_args):
            return [
                ffmpeg, "-y", "-ss", ss,
                "-to", seconds_to_ffmpeg_time(end_sec + 1.0), "-i", input_path,
                "-map", "0:v:0", "-vf", vf,
                # The encoder would otherwise count time in frames
                # (1/framerate) and round each frame's time to that grid,
                # off the copied pieces' exact times by up to half a frame.
                "-enc_time_base:v", "1/90000", "-fps_mode", "passthrough",
                *enc_args, "-bf", "0", "-pix_fmt", "yuv420p", *lead_bsf,
                "-avoid_negative_ts", "disabled", "-f", "mpegts", out_path,
                *keep_av_streams_args(0),
            ]

        duration = end_sec - start_sec
        proc = run_ffmpeg(build(encoder_args), duration, progress)
        if proc.returncode != 0 and encoder_args[1] not in ("libx264", "libx265"):
            # Fall back to the software encoder in the SAME codec family as
            # what was requested -- falling back to libx264 unconditionally
            # here would reintroduce the codec-mismatch bug this method's
            # caller works around (see get_video_encoder_args's docstring)
            # whenever the GPU encoder for an HEVC source fails.
            is_hevc = encoder_args[1].startswith("hevc_")
            software_args = (
                ["-c:v", "libx265", "-preset", "veryfast", "-crf", "20"] if is_hevc
                else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
            )
            proc = run_ffmpeg(build(software_args), duration, progress)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[-2000:])

    def _ffmpeg_copy_gops(self, ffmpeg, input_path, kf_start, kf_end, kf_end_dts, frame_sec,
                          zero_sec, min_seek, out_path, video_bsf, progress=None):
        """Stream-copies the video frames from keyframe kf_start up to (not
        including) keyframe kf_end into a piece for _join_cut_pieces.

        Start: ffmpeg starts reading a second before kf_start (in an MPEG-TS
        file -ss can land anywhere before its target, not on a keyframe, so
        a -ss close to kf_start could start reading after kf_start's
        packet). -itsoffset puts the point half a frame before kf_start at
        time 0, and -copypriorss 0 drops every packet shown before 0, so the
        copy starts at kf_start, its first keyframe after 0. (-ss as an
        output option would drop by decode time, which for a B-frame stream
        comes before kf_start's display time, and lose kf_start.) setts then
        moves the times onto the pieces' shared clock.

        End: with B-frames the packets aren't stored in display order, and a
        copy's -to stops at the first packet whose decode time (dts) reaches
        it. Cut at kf_end's display time, it would let kf_end itself and
        frames after it through (their dts come earlier), so it is cut half
        a frame before kf_end's own dts (kf_end_dts) instead: everything
        stored before kf_end's packet, which is every frame before kf_end.
        -to is counted on the clock -itsoffset shifted, so it is shifted the
        same way."""
        half = frame_sec / 2
        ss_sec = round(max(min_seek, kf_start - CUT_SEEK_PREROLL, 0.0), 6)
        ss = f"{ss_sec:.6f}"
        offset = ss_sec - (kf_start - half)
        to = (kf_end_dts if kf_end_dts is not None else kf_end) - half + offset
        # (setts=ts=... would give dts the pts's value.)
        shift = kf_start - half - zero_sec
        bsf = f"setts=pts=PTS+{shift:.6f}/TB:dts=DTS+{shift:.6f}/TB"
        if video_bsf:
            bsf = f"{video_bsf},{bsf}"
        cmd = [
            ffmpeg, "-y", "-itsoffset", f"{offset:.6f}", "-ss", ss,
            "-to", seconds_to_ffmpeg_time(to), "-i", input_path,
            "-map", "0:v:0", "-copypriorss", "0", "-c", "copy", "-bsf:v", bsf,
            "-avoid_negative_ts", "disabled", "-f", "mpegts", out_path,
            *keep_av_streams_args(0),
        ]
        proc = run_ffmpeg(cmd, kf_end - kf_start, progress)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[-2000:])

    def _join_cut_pieces(self, ffmpeg, pieces, input_path, start_sec, end_sec, video_start,
                         min_seek, out_path, audio_bsf, progress=None):
        """Joins the video pieces (paths, in order) and copies every audio
        track of input_path from start_sec to end_sec beside them, into
        out_path.

        Video: the pieces already share one clock, so they are joined byte
        for byte (the concat: protocol) and read as one MPEG-TS stream. The
        format is named because ffmpeg mistakes an MPEG-TS file of only a
        frame or two (fewer than 10 TS packets) for MPEG-PS. (The concat
        demuxer, used before, can't be told the format, and it places each
        piece by its longest stream's end, which left gaps once audio was
        in the pieces.)

        Audio: copied in one go from the source, so it has no seams and
        isn't re-encoded. Cut into pieces along with the video, it left a
        gap of up to an audio frame (~21 ms) at every join, because audio
        frames don't end where video frames do, plus the AAC encoder's
        start-up samples at the start of each re-encoded piece.

        Both are put on one clock with start_sec at 0: ffmpeg starts each
        input at 0, so the video, whose first frame is shown at video_start
        in the source, gets -itsoffset video_start - start_sec. The audio is
        read from a second early, -itsoffset puts start_sec - lead at 0 and
        -copypriorss 0 drops the audio before it. -to is counted on the
        shifted clock, so it is shifted the same way.

        lead: an audio packet holds ~20-30 ms, and the one that start_sec
        falls in starts before it; a decoder also needs the packet before
        a packet to decode it fully. Dropped, the cut's sound would start up
        to a packet late, and its first packet would play damaged. So for
        an MP4 the packets from CUT_AUDIO_LEAD before start_sec are kept,
        setts moves them back to negative times, and the MP4 muxer writes
        an edit list that skips them, as an encoder does with AAC's own
        start-up samples: playback starts exactly at start_sec. Matroska
        and MPEG-TS have no such list (ffmpeg would shift every stream to
        make the times positive), so there the audio starts at the first
        packet at or after start_sec."""
        lead = CUT_AUDIO_LEAD if out_path.lower().endswith((".mp4", ".mov", ".m4v")) else 0.0
        audio_ss_sec = round(max(min_seek, start_sec - lead - CUT_SEEK_PREROLL, 0.0), 6)
        audio_ss = f"{audio_ss_sec:.6f}"
        audio_offset = audio_ss_sec - (start_sec - lead)
        cmd = [
            ffmpeg, "-y",
            "-itsoffset", f"{video_start - start_sec:.6f}",
            "-f", "mpegts", "-i", "concat:" + "|".join(pieces),
            "-itsoffset", f"{audio_offset:.6f}", "-ss", audio_ss,
            "-to", seconds_to_ffmpeg_time(end_sec + audio_offset), "-i", input_path,
            # 1:a? -- every audio track, not just the one ffmpeg would pick.
            "-map", "0:v:0", "-map", "1:a?", "-copypriorss:a", "0", "-c", "copy",
        ]
        audio_bsfs = [audio_bsf] if audio_bsf else []
        if lead:
            audio_bsfs.append(f"setts=pts=PTS-{lead:.6f}/TB:dts=DTS-{lead:.6f}/TB")
        if audio_bsfs:
            cmd += ["-bsf:a", ",".join(audio_bsfs)]
        cmd += ["-movflags", "+faststart", out_path, *keep_av_streams_args(1)]
        proc = run_ffmpeg(cmd, end_sec - start_sec, progress)
        if proc.returncode != 0:
            raise RuntimeError(
                "Couldn't join the cut segments back together.\n\n" + proc.stderr[-2000:]
            )

    def _lossless_cut(self, input_path, start_sec, end_sec, out_path,
                       mode="reencode", search_window=8.0, progress=None):
        """Cut [start_sec, end_sec) out of input_path into out_path.
        Raises RuntimeError on failure. Returns a dict describing what
        actually happened, for status/notification text:
          mode: "classic" (plain -ss/-to -c copy, snaps to nearest keyframe,
                always 100% lossless but not necessarily frame-exact) |
                "copy" (smart-cut's fast path: already keyframe-aligned, so
                also 100% stream copy) | "partial" (smart-cut: mostly copy,
                small edges re-encoded) | "full" (smart-cut: whole requested
                range re-encoded -- shorter than one GOP) | "editlist" (video
                cut keyframe-to-keyframe with zero re-encoding, audio cut
                exactly, video's MP4 edit list patched to hide the small
                keyframe margin -- also 100% stream copy)
          encoder: encoder label used, or None if nothing was re-encoded
          reencoded_sec: total seconds of video that were re-encoded
          verified: False only when no keyframe data was found to confirm
                    alignment against (falls back to a plain copy)

        `mode` selects the strategy:
          "classic"  -- the original, pre-smart-cut behavior.
          "reencode" (default) -- smart-cut: re-encode only the edges.
          "editlist" -- try the edit-list trim; if it's not safely possible
                        for this particular file/cut (no matching keyframe
                        data, no edit list to patch, or the patched result
                        fails verification), transparently falls back to
                        "reencode" so a cut never just fails outright.
        `search_window` is how far before/after the requested point to look
        for a keyframe (seconds) -- used by both "reencode" and "editlist".
        `progress`, if given, is called with 0..1 as the cut advances.
        """
        ffmpeg = find_exe("ffmpeg")
        ffprobe = find_exe("ffprobe")

        if mode == "classic":
            self._ffmpeg_stream_copy_cut(ffmpeg, input_path, start_sec, end_sec, out_path, progress)
            return {"mode": "classic", "encoder": None, "reencoded_sec": 0.0, "verified": True}

        if mode == "editlist":
            result = self._editlist_cut(
                ffmpeg, ffprobe, input_path, start_sec, end_sec, out_path, search_window, progress
            )
            if result is not None:
                return result
            # Not safely possible for this cut -- fall through to smart-cut.

        return self._reencode_mode_cut(
            ffmpeg, ffprobe, input_path, start_sec, end_sec, out_path, search_window, progress
        )

    def _editlist_cut(self, ffmpeg, ffprobe, input_path, start_sec, end_sec,
                       out_path, search_window, progress=None):
        """Try the edit-list trim (see _lossless_cut's docstring). Returns
        a result dict on success, or None if it's not safely possible for
        this cut -- the caller falls back to the smart-cut re-encode path."""
        kf_near_start = self._probe_keyframes_near(ffprobe, input_path, start_sec, pad=search_window)
        kf_before_start = max((t for t in kf_near_start if t <= start_sec), default=None)
        kf_near_end = self._probe_keyframes_near(ffprobe, input_path, end_sec, pad=search_window)
        kf_after_end = min((t for t in kf_near_end if t >= end_sec), default=None)
        if kf_before_start is None or kf_after_end is None:
            return None

        def run(cmd, total=None, stage=None):
            return run_ffmpeg(cmd, total, stage).returncode == 0

        wide_sec = kf_after_end - kf_before_start
        clip_sec = end_sec - start_sec
        tmpdir = tempfile.mkdtemp(prefix="lossless_editlist_")
        try:
            # kf_before_start is an exact probed keyframe -- ceil-round it
            # (see seconds_to_ffmpeg_time_keyframe_ss) so -ss can't land the
            # demuxer on the *previous* keyframe instead, which would pull
            # in an extra GOP of unwanted footage at the front of
            # video_wide.mp4 and throw off the edit-list math below (which
            # assumes video_wide starts exactly at kf_before_start).
            video_wide = os.path.join(tmpdir, "video_wide.mp4")
            if not run([
                ffmpeg, "-y", "-ss", seconds_to_ffmpeg_time_keyframe_ss(kf_before_start),
                "-to", seconds_to_ffmpeg_time(kf_after_end), "-i", input_path,
                "-map", "0:v:0", "-c", "copy", video_wide,
            ], wide_sec, sub_progress(progress, 0.0, 0.5)):
                return None

            has_audio = self._probe_codec(ffprobe, input_path, "a") is not None
            if has_audio:
                audio_accurate = os.path.join(tmpdir, "audio_accurate.mp4")
                if not run([
                    ffmpeg, "-y", "-ss", seconds_to_ffmpeg_time(start_sec),
                    "-to", seconds_to_ffmpeg_time(end_sec), "-i", input_path,
                    "-map", "0:a", "-c", "copy", audio_accurate,
                ], clip_sec, sub_progress(progress, 0.5, 0.7)):
                    return None
                if not run([
                    ffmpeg, "-y", "-i", video_wide, "-i", audio_accurate,
                    "-map", "0:v:0", "-map", "1:a",
                    "-c", "copy", "-movflags", "+faststart", out_path,
                ], wide_sec, sub_progress(progress, 0.7, 1.0)):
                    return None
            else:
                if not run([
                    ffmpeg, "-y", "-i", video_wide, "-c", "copy",
                    "-movflags", "+faststart", out_path,
                ], wide_sec, sub_progress(progress, 0.5, 1.0)):
                    return None

            try:
                structure = parse_mp4_edit_structure(out_path)
            except Exception:
                return None
            video_tracks = [t for t in structure["tracks"] if t["is_video"]]
            if (structure["mvhd"] is None or len(video_tracks) != 1
                    or not video_tracks[0]["elst_entries"]):
                # No safe (same-size) patch target -- would need to insert a
                # new box, which this deliberately never attempts.
                return None

            try:
                patch_video_edit_list(out_path, start_sec, end_sec, kf_before_start, structure)
            except ValueError:
                return None

            try:
                check = subprocess.run(
                    [ffprobe, "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", out_path],
                    capture_output=True, text=True, timeout=10, **SUBPROCESS_KWARGS,
                )
                reported = float(check.stdout.strip())
            except Exception:
                return None
            if check.returncode != 0 or abs(reported - (end_sec - start_sec)) > 0.5:
                return None

            return {"mode": "editlist", "encoder": None, "reencoded_sec": 0.0, "verified": True}
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _reencode_mode_cut(self, ffmpeg, ffprobe, input_path, start_sec, end_sec,
                            out_path, search_window=8.0, progress=None):
        # The cut holds the frames shown at or after start_sec and before
        # end_sec. Pieces: the frames before the first keyframe in it
        # (head, re-encoded), keyframe to keyframe (middle, copied), the
        # frames from the last keyframe on (tail, re-encoded).
        kf_near_start = self._probe_keyframes_near(ffprobe, input_path, start_sec, pad=search_window)
        kf_at_start = next(
            (t for t in kf_near_start if t >= start_sec - CUT_TIME_SLACK), None
        )
        kf_near_end = self._probe_keyframes_near(ffprobe, input_path, end_sec, pad=search_window)
        kf_before_end = max(
            (t for t in kf_near_end if t <= end_sec + CUT_TIME_SLACK), default=None
        )
        # Where decoding of a re-encoded piece starting at start_sec begins:
        # before the keyframe at or before start_sec (from the file's start
        # if none was found).
        kf_before_start = max(
            (t for t in kf_near_start if t <= start_sec + CUT_TIME_SLACK), default=0.0
        )
        head_seek = kf_before_start - CUT_SEEK_PREROLL

        if kf_at_start is None or kf_before_end is None:
            # No keyframe found in the probed window (extremely sparse GOPs,
            # or right at the very start/end of the file) -- fall back to a
            # plain copy rather than act on incomplete data.
            self._ffmpeg_stream_copy_cut(ffmpeg, input_path, start_sec, end_sec, out_path, progress)
            return {"mode": "copy", "encoder": None, "reencoded_sec": 0.0, "verified": False}

        # An edge needs no re-encoding when no frame lies between it and a
        # keyframe: the start less than a frame before the keyframe, the end
        # less than a frame before one (marks end half a frame after the
        # last frame) or at one (within the millisecond rounding).
        frame_sec = self._probe_frame_duration(ffprobe, input_path)
        start_aligned = kf_at_start - start_sec < frame_sec - CUT_TIME_SLACK
        kf_after_end = next(
            (t for t in kf_near_end if t >= end_sec - CUT_TIME_SLACK), None
        )
        end_aligned = (kf_after_end is not None
                       and kf_after_end - end_sec < frame_sec - CUT_TIME_SLACK)
        if end_aligned:
            kf_before_end = kf_after_end
        video_codec = self._probe_codec(ffprobe, input_path, "v")
        video_bsf = {"h264": "h264_mp4toannexb", "hevc": "hevc_mp4toannexb"}.get(video_codec)

        if start_aligned and end_aligned and kf_at_start < kf_before_end:
            # Nothing to re-encode; the copy below still goes through the
            # same TS piece and join as the other cuts.
            encoder_args, encoder_label = None, None
        else:
            # Re-encode with an encoder that matches the source's own video
            # codec -- this result may get concatenated with a stream-copied
            # (unchanged-codec) piece of the same file below, and mixing
            # codecs in that concat silently corrupts whichever segment
            # doesn't match the codec the muxer locks onto (see
            # get_video_encoder_args).
            encoder_args, encoder_label = get_video_encoder_args(ffmpeg, video_codec)

        # No whole GOP inside the range: re-encode all of its video.
        full = kf_at_start >= kf_before_end

        audio_codec = self._probe_codec(ffprobe, input_path, "a")
        audio_bsf = {"aac": "aac_adtstoasc"}.get(audio_codec)

        # Rough relative cost of each step, used only to split the progress
        # bar: head/tail re-encodes are tiny but slow per second, the copied
        # middle and the final join each pass over (almost) the whole clip.
        if full:
            weights = {"head": 1.0, "join": 2.0}
        else:
            weights = {"middle": 3.0, "join": 2.0}
            if not start_aligned:
                weights["head"] = 1.0
            if not end_aligned:
                weights["tail"] = 1.0
        stage_bounds = {}
        acc = 0.0
        total_weight = sum(weights.values())
        for name in ("head", "middle", "tail", "join"):
            if name in weights:
                stage_bounds[name] = (acc / total_weight, (acc + weights[name]) / total_weight)
                acc += weights[name]

        def stage(name):
            return sub_progress(progress, *stage_bounds[name])

        reencoded_sec = 0.0
        tmpdir = tempfile.mkdtemp(prefix="lossless_cut_")
        try:
            pieces = []
            # The pieces meet half a frame before each keyframe, between two
            # frames, so neither rounding to milliseconds nor how ffmpeg
            # treats a frame exactly on a boundary can move a frame across.
            # Each piece's clock has start_sec at 0.
            if start_aligned and not full:
                video_start = kf_at_start
            else:
                video_start = self._probe_first_frame_at(ffprobe, input_path, start_sec)
            min_seek = self._probe_av_offset(ffprobe, input_path)
            if full:
                full_path = os.path.join(tmpdir, "full.ts")
                self._ffmpeg_reencode_segment(
                    ffmpeg, input_path, start_sec, end_sec, head_seek, start_sec,
                    min_seek, full_path, encoder_args, progress=stage("head"),
                )
                pieces.append(full_path)
                reencoded_sec = end_sec - start_sec
            else:
                if not start_aligned:
                    head_path = os.path.join(tmpdir, "head.ts")
                    start_dts = self._probe_keyframe_dts(ffprobe, input_path, kf_at_start)
                    self._ffmpeg_reencode_segment(
                        ffmpeg, input_path, start_sec, kf_at_start - frame_sec / 2, head_seek,
                        start_sec, min_seek, head_path, encoder_args,
                        dts_lead=0.0 if start_dts is None else max(0.0, kf_at_start - start_dts),
                        progress=stage("head"),
                    )
                    pieces.append(head_path)
                    reencoded_sec += kf_at_start - start_sec

                middle_path = os.path.join(tmpdir, "middle.ts")
                self._ffmpeg_copy_gops(
                    ffmpeg, input_path, kf_at_start, kf_before_end,
                    self._probe_keyframe_dts(ffprobe, input_path, kf_before_end), frame_sec,
                    start_sec, min_seek, middle_path, video_bsf, progress=stage("middle"),
                )
                pieces.append(middle_path)

                if not end_aligned:
                    tail_path = os.path.join(tmpdir, "tail.ts")
                    self._ffmpeg_reencode_segment(
                        ffmpeg, input_path, kf_before_end - frame_sec / 2, end_sec,
                        kf_before_end - CUT_SEEK_PREROLL, start_sec, min_seek, tail_path,
                        encoder_args, progress=stage("tail"),
                    )
                    pieces.append(tail_path)
                    reencoded_sec += end_sec - kf_before_end

            self._join_cut_pieces(
                ffmpeg, pieces, input_path, start_sec, end_sec, video_start,
                min_seek, out_path, audio_bsf, progress=stage("join"),
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        if encoder_args is None:
            return {"mode": "copy", "encoder": None, "reencoded_sec": 0.0, "verified": True}
        return {
            "mode": "full" if full else "partial", "encoder": encoder_label,
            "reencoded_sec": reencoded_sec, "verified": True,
        }

    @staticmethod
    def _describe_cut_result(result):
        """Turn a _lossless_cut() result dict into a short, accurate
        status phrase -- so the UI never just says "lossless" when part of
        the video was actually re-encoded."""
        if result is None:
            return ""
        mode = result["mode"]
        if mode == "classic":
            if "slide" not in result:
                return "100% lossless, no re-encoding (classic -- snapped to the nearest keyframe, may not be frame-exact)"
            return f"100% lossless (snapped to the nearest keyframe: {LosslessVideoApp._describe_slide(result['slide'])})"
        if mode == "editlist":
            return "100% lossless, no re-encoding (edit-list trim -- exact cut point, hidden via MP4 metadata)"
        if mode == "copy":
            if result.get("verified", True):
                return "100% lossless, no re-encoding"
            return "lossless (stream copy; couldn't verify keyframe alignment)"
        secs = result["reencoded_sec"]
        encoder = result["encoder"] or "CPU"
        if mode == "full":
            return f"fully re-encoded ({secs:.2f}s, via {encoder}) -- cut is shorter than one keyframe interval"
        return f"mostly lossless -- {secs:.2f}s re-encoded near the cut edges (via {encoder})"

    @staticmethod
    def _describe_slide(slide):
        """_measure_cut_slide's (start, end) in words."""
        if slide is None:
            return "couldn't check by how many frames"
        if slide == (0, 0):
            return "frame-exact"

        def part(name, n):
            if n == 0:
                return f"{name} exact"
            return f"{name} {abs(n)} frame{'s' if abs(n) != 1 else ''} {'early' if n < 0 else 'late'}"

        return f"{part('start', slide[0])}, {part('end', slide[1])}"

    def _saved_ask_open(self, text, path, tab, what):
        """The success message for a saved video, then a second one asking
        whether to open it (what: "newly cut video", "combined video", ...)
        here: Yes opens it on tab (0 Cut, 3 Crop/Rotate, 4 Audio)."""
        messagebox.showinfo("Done", text)
        if not messagebox.askyesno("Open in the editor?", f"Open the {what} in the editor?"):
            return
        self._ensure_tab_built(tab)
        self.notebook.select(tab)
        load = {0: self._load_video_for_cut, 3: self._load_video_for_resize,
                4: self._load_video_for_audio}[tab]
        load(path)

    def _cut_done(self, info, success, result=None):
        self._end_progress("cut")
        self._set_cut_controls_enabled(True)
        if success:
            how = self._describe_cut_result(result)
            self.status_lbl.config(text=f"Saved ({how}): {info}")
            self._saved_ask_open(f"Cut successful.\n\nScene saved to:\n{info}\n\n"
                                 f"Cutting Mode: {how[0].upper()}{how[1:]}", info, 0, "newly cut video")
        else:
            self.status_lbl.config(text="Cut failed")
            messagebox.showerror("ffmpeg error",
                                  "The cut failed. Details:\n\n" + info)

    # ====================================================================
    # COMBINE TAB
    # ====================================================================
    THUMB_SIZE = (72, 40)    # the Combine and Bulk Cut lists' thumbnails
    LIST_EMPTY_TEXT = "(drag & drop videos here, or click 'Add Files...')"

    def _build_combine_tab(self):
        root = self.combine_tab
        root.rowconfigure(1, weight=1)
        root.columnconfigure(0, weight=1)

        header = tk.Frame(root, bg="#f0f0f0")
        header.grid(row=0, column=0, sticky="ew", padx=3, pady=(6, 2))
        tk.Label(header, text="Videos will be joined in the order listed below "
                 "(top to bottom). Drag rows to reorder them.",
                 bg="#f0f0f0", anchor="w", wraplength=px(600), justify="left"
                 ).pack(side="left", fill="x", expand=True)

        # list + side buttons
        body = tk.Frame(root, bg="#f0f0f0")
        body.grid(row=1, column=0, sticky="nsew", padx=3, pady=4)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        # The list, and under it (once opened) the preview of the joined
        # video, with a sash between them.
        self.combine_paned = tk.PanedWindow(body, orient="vertical", bg="#f0f0f0", bd=0,
                                            sashwidth=px(6), sashrelief="flat",
                                            opaqueresize=True)
        self.combine_paned.grid(row=0, column=0, sticky="nsew")
        self.combine_table = FileTable(
            self.combine_paned, self,
            columns=[("length", "Length", px(70), "center"),
                     ("video", "Video", px(190), "center"),
                     ("audio", "Audio", px(200), "center")],
            name_width=260,
            thumb_size=self.THUMB_SIZE, get_thumbnail=self._list_thumbnail,
            on_reorder=self._combine_table_changed, on_delete=self._combine_remove_selected,
            empty_text=self.LIST_EMPTY_TEXT)
        self.combine_paned.add(self.combine_table.frame, stretch="always", minsize=px(60))
        # Only the blank area below the rows; headings keep column resizing.
        tree = self.combine_table.tree
        self._bind_window_drag(tree, lambda e: tree.identify_region(e.x, e.y) == "nothing")
        self._bind_window_drag(self.combine_table.empty_lbl, lambda e: True)
        self._rebuild_combine_rows()

        side = tk.Frame(body, bg="#f0f0f0")
        side.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        tk.Button(side, text="Add Files...", width=16, command=self.add_files_for_combine).pack(pady=2)
        remove_btn = tk.Button(side, text="Remove Selected", width=16,
                               command=self._combine_remove_selected)
        remove_btn.pack(pady=2)
        Tooltip(remove_btn, "Shortcut: Delete")
        tk.Button(side, text="Move Up", width=16, command=lambda: self._combine_move(-1)).pack(pady=2)
        tk.Button(side, text="Move Down", width=16, command=lambda: self._combine_move(1)).pack(pady=2)
        tk.Button(side, text="Clear All", width=16, command=self._combine_clear).pack(pady=2)
        self.combine_preview_btn = None
        if mpv_available():
            self.combine_preview_btn = tk.Button(side, text="Preview", width=16,
                                                 command=self._combine_toggle_preview)
            self.combine_preview_btn.pack(pady=(10, 2))

        footer = tk.Frame(root, bg="#f0f0f0")
        footer.grid(row=2, column=0, sticky="ew", padx=3, pady=(4, 2))
        tk.Label(footer, text="Note: Videos that share the same codec are joined losslessly. "
                 "Mixed videos are re-encoded to match the first one instead.",
                 bg="#f0f0f0", fg="#555555", wraplength=px(850), justify="left"
                 ).pack(fill="x")
        # Says when the current list will be re-encoded, and why.
        self.combine_match_lbl = tk.Label(footer, text="", bg="#f0f0f0", fg="#a35200",
                                          wraplength=px(850), justify="left", anchor="w")
        self.combine_match_lbl.pack(fill="x")

        self.combine_btn = tk.Button(root, text="Combine Videos!", height=2,
                                      font=("Segoe UI", 10, "bold"),
                                      command=self._combine_clicked)
        self.combine_btn.grid(row=3, column=0, sticky="ew", padx=3, pady=(4, 6))

        for target in (tree, self.combine_table.empty_lbl):
            target.drop_target_register(DND_FILES)
            target.dnd_bind("<<Drop>>", self._on_drop_combine)

    # ---- rows ---------------------------------------------------------------
    def _combine_row_values(self, path):
        info = self.combine_info.get(path)
        if info is None:
            self._request_combine_info(path)
            return ("…", "…", "…")
        return info

    def _rebuild_combine_rows(self):
        """Shows combine_files in the list, keeping the selected places."""
        self._schedule_combine_preview_sync()
        table = self.combine_table
        if table is None:
            return
        order = table.order()
        selected = [order.index(i) for i in table.selected()]
        iids = table.replace([(p, self._combine_row_values(p)) for p in self.combine_files])
        table.select([iids[i] for i in selected if i < len(iids)])
        self._schedule_combine_match_check()

    def _combine_table_changed(self):
        """The rows were reordered or removed in the list itself."""
        table = self.combine_table
        self.combine_files = [table.paths[i] for i in table.order()]
        self._schedule_combine_preview_sync()
        self._schedule_combine_match_check()

    # ---- will the join be lossless? ------------------------------------------
    def _combine_format(self, ffprobe, path):
        """probe_join_format of path, kept until the file changes."""
        try:
            st = os.stat(path)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            return None
        cached = self._combine_formats.get(path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        fmt = probe_join_format(ffprobe, path)
        self._combine_formats[path] = (stamp, fmt)
        return fmt

    def _combine_differences(self, ffprobe, files):
        return join_format_differences([self._combine_format(ffprobe, p) for p in files])

    def _schedule_combine_match_check(self):
        if getattr(self, "combine_match_lbl", None) is None:
            return
        if self._combine_match_id is not None:
            self.after_cancel(self._combine_match_id)
        self._combine_match_id = self.after(200, self._combine_match_check)

    def _combine_match_check(self):
        """Probes the list in the background and updates the note under it."""
        self._combine_match_id = None
        files = tuple(self.combine_files)
        if len(files) < 2:
            self._show_combine_match(files, [])
            return

        def probe():
            try:
                diffs = self._combine_differences(find_exe("ffprobe"), files)
            except FileNotFoundError:
                diffs = []
            self.after(0, lambda: self._show_combine_match(files, diffs))
        self.thumb_pool.submit(probe)

    def _show_combine_match(self, files, diffs):
        if files != tuple(self.combine_files):
            return   # the list changed meanwhile; a newer check is on its way
        self.combine_match_lbl.config(
            text=(f"These videos differ ({', '.join(diffs)}), so they will be re-encoded "
                  "to match the first one.") if diffs else "")

    def _request_combine_info(self, path):
        if path in self.combine_info_pending:
            return
        self.combine_info_pending.add(path)

        def probe():
            try:
                info = probe_file_summary(find_exe("ffprobe"), path)
            except FileNotFoundError:
                info = ("—", "—", "—")
            self.after(0, lambda: self._on_combine_info_ready(path, info))
        self.thumb_pool.submit(probe)

    def _on_combine_info_ready(self, path, info):
        self.combine_info_pending.discard(path)
        self.combine_info[path] = info
        table = self.combine_table
        for iid, p in table.paths.items():
            if p == path and table.tree.exists(iid):
                table.tree.item(iid, values=info)

    # ---- thumbnails (Combine and Bulk Cut) ----------------------------------
    def _list_thumbnail(self, path):
        """The list thumbnail of path, or None for now (one is requested and
        put in the rows when it is ready) or for good when there can't be one."""
        cached = self.thumb_cache.get(path)
        if cached is None:
            self._request_thumbnail(path)
        return cached or None

    def _request_thumbnail(self, path):
        # One extraction per file, however often its rows are redrawn: the
        # same files decoded over and over in parallel made thumbnails slow.
        if not PIL_AVAILABLE or path in self.thumb_pending:
            return
        self.thumb_pending.add(path)
        self.thumb_pool.submit(self._extract_thumbnail, path)

    def _extract_thumbnail(self, path):
        img = None
        try:
            ffmpeg = find_exe("ffmpeg")
            # -skip_frame nokey: only keyframes get decoded (the first frame
            # always is one), and the JPEG comes back over a pipe instead of
            # through a temp file.
            cmd = [ffmpeg, "-v", "error", "-skip_frame", "nokey", "-i", path,
                   "-map", "0:v:0", "-an", "-sn", "-dn", "-frames:v", "1",
                   "-vf", f"scale={self.THUMB_SIZE[0] * 2}:-2:flags=fast_bilinear",
                   "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "5", "pipe:1"]
            proc = subprocess.run(cmd, capture_output=True, check=True, timeout=60,
                                  **SUBPROCESS_KWARGS)
            img = Image.open(io.BytesIO(proc.stdout)).convert("RGB")
            img.thumbnail(self.THUMB_SIZE)
        except Exception:
            pass  # thumbnail is a nice-to-have; failures are silent
        self.after(0, lambda: self._on_thumbnail_ready(path, img))

    def _on_thumbnail_ready(self, path, pil_img):
        self.thumb_pending.discard(path)
        if pil_img is None:
            # Remember the failure so redraws don't keep retrying it.
            self.thumb_cache[path] = ""
            return
        photo = ImageTk.PhotoImage(pil_img)
        self.thumb_cache[path] = photo
        for table in (self.combine_table, self.bulk_table):
            if table is not None:
                table.thumbnail_ready(path, photo)

    # ---- file list management --------------------------------------------
    def _combine_select(self, idx):
        """Selects the row at idx (just that one)."""
        order = self.combine_table.order()
        self.combine_table.select([order[idx]] if 0 <= idx < len(order) else [])

    def add_files_for_combine(self):
        self._ensure_tab_built(1)
        self.notebook.select(self.combine_tab)
        paths = filedialog.askopenfilenames(
            title="Add videos to combine",
            filetypes=[("Video files", "*" + " *".join(VIDEO_EXTS)), ("All files", "*.*")],
        )
        if paths:
            self.combine_files.extend(paths)
            self._rebuild_combine_rows()

    def _combine_remove_selected(self):
        table = self.combine_table
        rows = table.selected()
        if not rows:
            return
        after = table.order()[table.order().index(rows[-1]) + 1:]
        next_row = next((i for i in after if i not in rows), None)
        table.delete(rows)
        self._combine_table_changed()
        # The next row takes the selection, so Delete can be pressed again.
        rest = table.order()
        if next_row is None and rest:
            next_row = rest[-1]
        table.select([next_row] if next_row else [])

    def _combine_move(self, direction):
        self.combine_table.move(direction)

    def _combine_clear(self):
        self.combine_files = []
        self._rebuild_combine_rows()

    # ---- preview of the joined video ---------------------------------------
    def _combine_preview_open(self):
        return bool(getattr(self, "_combine_preview_shown", False))

    def _combine_toggle_preview(self):
        if self._combine_preview_open():
            self._combine_close_preview()
        else:
            self._combine_open_preview()

    def _combine_open_preview(self):
        """Plays the list's videos back to back, as Combine will join them,
        in a player under the list."""
        if not self.combine_files:
            self.status_lbl.config(text="Add videos first, then preview them joined")
            return
        if self.combine_preview is None:
            self.combine_preview = ClipPreview(self.combine_paned,
                                               on_close=self._combine_close_preview)
        self._combine_preview_shown = True
        self.combine_paned.add(self.combine_preview.frame, stretch="always", minsize=px(140))
        # The list keeps about a third of the height.
        self.combine_paned.update_idletasks()
        self.combine_paned.sash_place(
            0, 0, max(px(60), int(self.combine_paned.winfo_height() * 0.35)))
        self.combine_preview_btn.config(text="Close preview")
        try:
            self._combine_preview_load()
        except RuntimeError as exc:
            self._combine_close_preview()
            messagebox.showerror("Preview", str(exc))

    def _combine_close_preview(self):
        if not self._combine_preview_open():
            return
        self._combine_preview_shown = False
        self.combine_preview.unload()
        self.combine_paned.forget(self.combine_preview.frame)
        self.combine_preview_btn.config(text="Preview")

    def _combine_preview_load(self, start=0.0):
        files = list(self.combine_files)
        labels = [f"Video {i}/{len(files)}: {os.path.basename(p)}"
                  for i, p in enumerate(files, 1)]
        volume, muted = self._player_audio_settings()
        self.combine_preview.show([(p, None, None) for p in files], labels, key=tuple(files),
                                  start=start, volume=volume, muted=muted)

    def _schedule_combine_preview_sync(self):
        # Once the list has settled: a drag reorders it at every row it passes.
        if not self._combine_preview_open():
            return
        if self._combine_preview_sync_id is not None:
            self.after_cancel(self._combine_preview_sync_id)
        self._combine_preview_sync_id = self.after(300, self._combine_preview_sync)

    def _combine_preview_sync(self):
        """The list changed with the preview open: show the new order,
        starting with the video that was on screen if it is still listed."""
        self._combine_preview_sync_id = None
        preview = self.combine_preview
        files = tuple(self.combine_files)
        if not self._combine_preview_open() or files == preview.key:
            return
        if not files:
            self._combine_close_preview()
            return
        start = 0.0
        old, lengths = preview.key, preview.part_lengths()
        if old and lengths and len(lengths) == len(old):
            length_of = dict(zip(old, lengths))
            current = old[preview.part_index(preview.player.position or 0.0)]
            if current in files:
                before = files[:files.index(current)]
                if all(f in length_of for f in before):
                    start = sum(length_of[f] for f in before)
        try:
            self._combine_preview_load(start)
        except RuntimeError:
            self._combine_close_preview()

    def _combine_clicked(self):
        if len(self.combine_files) < 2:
            messagebox.showerror("Not enough files", "Add at least 2 videos to combine.")
            return

        first_path = self.combine_files[0]
        first_dir = os.path.dirname(os.path.abspath(first_path))
        first_base = os.path.splitext(os.path.basename(first_path))[0]
        first_ext = os.path.splitext(first_path)[1]
        default_name = f"{first_base}.COMBINED{first_ext}"

        from_dialog = not self.save_to_source_var.get()
        if not from_dialog:
            out_path = os.path.join(first_dir, default_name)
        else:
            out_path = filedialog.asksaveasfilename(
                title="Save combined video as", initialdir=first_dir,
                initialfile=default_name, defaultextension=first_ext,
                filetypes=[("Same format", f"*{first_ext}"), ("All files", "*.*")],
            )
            if not out_path:
                return
        out_path = self._check_output_path(out_path, self.combine_files, prompt_overwrite=not from_dialog)
        if not out_path:
            return

        self.combine_btn.config(state="disabled")
        self.status_lbl.config(text="Combining... 0%")
        threading.Thread(target=self._run_combine, args=(out_path,), daemon=True).start()

    def _probe_codec(self, ffprobe_exe, path, stream_kind):
        """stream_kind is 'v' (video) or 'a' (audio). Returns codec name, or None."""
        try:
            result = subprocess.run(
                [ffprobe_exe, "-v", "error", "-select_streams", f"{stream_kind}:0",
                 "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
                capture_output=True, text=True, check=True, **SUBPROCESS_KWARGS,
            )
            name = result.stdout.strip()
            return name or None
        except Exception:
            return None

    class _LosslessJoinFailed(Exception):
        """The files can't be joined by stream copy; the message says why."""

    def _run_combine(self, out_path):
        """Joins the list losslessly (stream copy) when every file has the
        same video and audio format; otherwise, or when ffmpeg can't copy
        them together, re-encodes them all to match the first file and
        says so when done."""
        tmpdir = None
        try:
            ffmpeg = find_exe("ffmpeg")
            ffprobe = find_exe("ffprobe")
            tmpdir = tempfile.mkdtemp(prefix="lossless_combine_")
            diffs = self._combine_differences(ffprobe, list(self.combine_files))
            reason = f"they differ ({', '.join(diffs)})" if diffs else None
            if reason is None:
                try:
                    self._combine_lossless(ffmpeg, ffprobe, tmpdir, out_path)
                except self._LosslessJoinFailed as exc:
                    reason = str(exc)
            if reason is None:
                self.after(0, lambda: self._combine_done(out_path, True))
            else:
                done_path, how = self._combine_reencode(ffmpeg, ffprobe, out_path, reason)
                self.after(0, lambda: self._combine_done(done_path, True, how))
        except Exception as exc:
            self.after(0, lambda e=str(exc): self._combine_done(e, False))
        finally:
            if tmpdir and os.path.isdir(tmpdir):
                shutil.rmtree(tmpdir, ignore_errors=True)

    def _combine_lossless(self, ffmpeg, ffprobe, tmpdir, out_path):
        """
        Joins clips via an MPEG-TS intermediate step instead of feeding MP4s
        straight into the concat demuxer. Plain "concat demuxer + -c copy" on
        MP4 files is prone to a well-known issue where, after the first file
        ends, playback shows a frozen frame while audio keeps playing --
        caused by MP4-specific timestamp/edit-list metadata that the TS
        intermediate format doesn't carry, so this route avoids the problem
        entirely while remaining 100% stream-copy (no re-encoding, so still
        lossless -- only the container packaging changes, twice).

        The file list stays editable while this runs and is re-read before
        every step: a file removed before its turn is skipped, a file added
        mid-run is included, and the final join uses the list's order at
        that moment. Raises _LosslessJoinFailed when the final list doesn't
        match or ffmpeg can't copy the files together.
        """
        prepared = {}    # path -> (ts path, duration)
        durations = {}   # path -> duration (None if unreadable)
        audio_codec = None

        def overall_fraction(current_path, current_frac):
            # Every file costs its own duration twice: once to repackage
            # it, once more during the final join.
            files = list(self.combine_files)
            for p in files:
                if p not in durations:
                    durations[p] = probe_duration(ffprobe, p) or 1.0
            total = 2 * sum(durations[p] for p in files) or 1.0
            done = sum(durations[p] for p in files if p in prepared)
            if current_path is not None:
                done += durations.get(current_path, 1.0) * current_frac
            return done / total

        while True:
            files = list(self.combine_files)
            remaining = list(dict.fromkeys(p for p in files if p not in prepared))
            if not remaining:
                break
            p = remaining[0]
            step = len(prepared) + 1
            step_total = len(prepared) + len(remaining)
            report = self._progress_reporter(
                lambda pct, s=step, n=step_total: f"Combining {s}/{n}... {pct}%", "combine"
            )
            if audio_codec is None:
                audio_codec = self._probe_codec(ffprobe, p, "a") or ""
            video_codec = self._probe_codec(ffprobe, p, "v")
            video_bsf = {"h264": "h264_mp4toannexb", "hevc": "hevc_mp4toannexb"}.get(video_codec)

            ts_path = os.path.join(tmpdir, f"part_{len(prepared)}.ts")
            # Every audio track: ffmpeg on its own keeps only one.
            cmd = [ffmpeg, "-y", "-i", p, "-map", "0:v:0", "-map", "0:a?", "-c", "copy"]
            if video_bsf:
                cmd += ["-bsf:v", video_bsf]
            cmd += ["-f", "mpegts", ts_path]
            overall_fraction(p, 0.0)  # makes sure p's duration is known
            proc = run_ffmpeg(
                cmd, durations[p],
                lambda f, p=p, report=report: report(overall_fraction(p, f)),
            )
            if proc.returncode != 0:
                raise self._LosslessJoinFailed(
                    f"ffmpeg couldn't copy '{os.path.basename(p)}' without re-encoding")
            prepared[p] = (ts_path, durations[p])

        final_files = list(self.combine_files)
        if len(final_files) < 2:
            raise RuntimeError(
                "Fewer than 2 videos were left in the list, so nothing was combined."
            )
        # Files added while this ran are checked too.
        diffs = self._combine_differences(ffprobe, final_files)
        if diffs:
            raise self._LosslessJoinFailed(f"they differ ({', '.join(diffs)})")
        audio_bsf = {"aac": "aac_adtstoasc"}.get(audio_codec)
        base_frac = overall_fraction(None, 0.0)
        report = self._progress_reporter(
            lambda pct, n=len(final_files): f"Combining: joining {n} files... {pct}%", "combine"
        )
        ts_paths = [prepared[p][0] for p in final_files]
        concat_arg = "concat:" + "|".join(ts_paths)
        cmd = [ffmpeg, "-y", "-i", concat_arg, "-map", "0:v:0", "-map", "0:a?", "-c", "copy"]
        if audio_bsf:
            cmd += ["-bsf:a", audio_bsf]
        cmd += [out_path]
        proc = run_ffmpeg(
            cmd, sum(prepared[p][1] for p in final_files),
            lambda f: report(base_frac + (1.0 - base_frac) * f),
        )
        if proc.returncode != 0:
            raise self._LosslessJoinFailed("ffmpeg couldn't join them without re-encoding")

    # Containers that can't hold H.264 + AAC; the re-encode writes MP4 instead.
    _REENCODE_EXT_SWAP = {".webm": ".mp4"}
    _LAYOUTS = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}

    def _combine_reencode(self, ffmpeg, ffprobe, out_path, reason):
        """Re-encodes the list (as it is now) into one video shaped like the
        first file: its size (others scaled to fit, with black bars), frame
        rate and audio format, H.264 + AAC. One ffmpeg run with the concat
        filter, which also pads a clip's missing or short audio with
        silence. Returns (the path written, what was done for the user)."""
        files = list(self.combine_files)
        if len(files) < 2:
            raise RuntimeError("Fewer than 2 videos were left in the list, so nothing was combined.")
        formats = [self._combine_format(ffprobe, p) for p in files]
        for p, fmt in zip(files, formats):
            if fmt is None:
                raise RuntimeError(f"'{os.path.basename(p)}' couldn't be read as a video.")
        first = formats[0]
        width, height = first["width"] // 2 * 2, first["height"] // 2 * 2
        fps_text = first["fps_text"] or "30"
        with_audio = next((f for f in formats if f["acodec"]), None)
        # Every audio track is kept: track k of the result joins each file's
        # track k (silence for a file with fewer tracks), in the sample rate
        # and channels of the first file that has a track k.
        n_tracks = max(len(f["audio_tracks"]) for f in formats)
        track_formats = []
        for k in range(n_tracks):
            _, rate, ch = next(f["audio_tracks"][k] for f in formats if len(f["audio_tracks"]) > k)
            track_formats.append((rate or 48000, self._LAYOUTS.get(ch or 2, "stereo")))

        root, ext = os.path.splitext(out_path)
        new_ext = self._REENCODE_EXT_SWAP.get(ext.lower())
        if new_ext:
            out_path = root + new_ext
            if os.path.exists(out_path):
                out_path = unique_output_path(out_path)

        inputs, chains, pairs = [], [], []
        for i, (p, fmt) in enumerate(zip(files, formats)):
            inputs += ["-i", p]
            chains.append(
                f"[{i}:v:0]scale={width}:{height}:force_original_aspect_ratio=decrease:"
                f"force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                f"fps={fps_text},format=yuv420p[v{i}]")
            pairs.append(f"[v{i}]")
            for k, (sample_rate, layout) in enumerate(track_formats):
                if k < len(fmt["audio_tracks"]):
                    chains.append(f"[{i}:a:{k}]aresample={sample_rate},"
                                  f"aformat=sample_rates={sample_rate}:channel_layouts={layout}"
                                  f"[a{i}_{k}]")
                else:
                    length = fmt["duration"] or 0.1
                    chains.append(f"anullsrc=r={sample_rate}:cl={layout},"
                                  f"atrim=duration={length:.3f}[a{i}_{k}]")
                pairs.append(f"[a{i}_{k}]")
        chains.append("".join(pairs) + f"concat=n={len(files)}:v=1:a={n_tracks}[v]"
                      + "".join(f"[a{k}]" for k in range(n_tracks)))
        graph = ";".join(chains)
        total = sum(f["duration"] or 0.0 for f in formats) or None
        report = self._progress_reporter(
            lambda pct: f"Combining (re-encoding: {reason})... {pct}%", "combine")

        def build_cmd(encoder_args):
            cmd = [ffmpeg, "-y", *inputs, "-filter_complex", graph, "-map", "[v]"]
            for k in range(n_tracks):
                cmd += ["-map", f"[a{k}]"]
            if n_tracks:
                cmd += ["-c:a", "aac", "-b:a", "192k"]
            cmd += [*encoder_args, "-pix_fmt", "yuv420p"]
            if os.path.splitext(out_path)[1].lower() in (".mp4", ".mov", ".m4v"):
                cmd += ["-movflags", "+faststart"]
            return cmd + [out_path]

        encoder_args, label = get_video_encoder_args(ffmpeg)
        proc = run_ffmpeg(build_cmd(encoder_args), total, report)
        if proc.returncode != 0 and encoder_args[1] != "libx264":
            # A GPU encoder can refuse a size or fail in the driver: the
            # software one, once, before giving up.
            encoder_args, label = (["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"],
                                   "CPU (libx264)")
            proc = run_ffmpeg(build_cmd(encoder_args), total, report)
        if proc.returncode != 0:
            raise RuntimeError("Re-encoding the videos to combine them failed:\n\n"
                               + proc.stderr[-2000:])
        rate = _parse_rate(fps_text) or 30.0
        how = (f"The videos couldn't be joined losslessly because {reason}, so they were "
               f"re-encoded to match the first one: {width}x{height} at "
               f"{rate:.3f}".rstrip("0").rstrip(".") + f" fps, H.264 ({label})"
               + (", AAC audio" if with_audio else "") + ". This is not lossless.")
        if new_ext:
            how += f"\n\nWebM can't hold H.264, so it was saved as {new_ext[1:].upper()}."
        return out_path, how

    def _combine_done(self, info, success, reencoded=None):
        self._end_progress("combine")
        self.combine_btn.config(state="normal")
        if success:
            self.status_lbl.config(text=f"Saved{' (re-encoded)' if reencoded else ''}: {info}")
            self._saved_ask_open(f"Combined video saved to:\n{info}"
                                 + (f"\n\n{reencoded}" if reencoded else ""), info, 0, "combined video")
        else:
            self.status_lbl.config(text="Combine failed")
            messagebox.showerror("ffmpeg error", info)

    # ====================================================================
    # BULK CUT TAB
    # ====================================================================
    def _build_bulk_tab(self):
        root = self.bulk_tab
        root.rowconfigure(1, weight=1)
        root.columnconfigure(0, weight=1)

        header = tk.Frame(root, bg="#f0f0f0")
        header.grid(row=0, column=0, sticky="ew", padx=3, pady=(6, 2))
        tk.Label(
            header,
            text="Drop video files whose name contains one or more cut ranges, e.g. "
                 "\"clip [00.00.00-00.00.17] [01.10-01.25].mp4\". "
                 "MM.SS and HH.MM.SS are both accepted. Each bracket creates its own lossless clip. "
                 "Scenes added from the Cut tab are listed here too.",
            bg="#f0f0f0", anchor="w", wraplength=px(850), justify="left",
        ).pack(side="left", fill="x", expand=True)

        body = tk.Frame(root, bg="#f0f0f0")
        body.grid(row=1, column=0, sticky="nsew", padx=3, pady=4)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        # The list, and under it (once a row is double-clicked) a player
        # for that row's ranges, with a sash between them.
        self.bulk_paned = tk.PanedWindow(body, orient="vertical", bg="#f0f0f0", bd=0,
                                         sashwidth=px(6), sashrelief="flat", opaqueresize=True)
        self.bulk_paned.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self.bulk_table = FileTable(
            self.bulk_paned, self,
            columns=[("range", "Cut Range", px(170), "center"),
                     ("length", "Final Length", px(110), "center"),
                     ("status", "Status", px(140), "center")],
            thumb_size=self.THUMB_SIZE, get_thumbnail=self._list_thumbnail,
            on_reorder=self._bulk_table_reordered, on_delete=self._bulk_remove_selected,
            empty_text=self.LIST_EMPTY_TEXT)
        self.bulk_paned.add(self.bulk_table.frame, stretch="always", minsize=px(60))
        self.bulk_tree = self.bulk_table.tree

        side = tk.Frame(body, bg="#f0f0f0")
        side.grid(row=0, column=2, sticky="ns", padx=(8, 0))
        tk.Button(side, text="Add Files...", width=16, command=self.add_files_for_bulk).pack(pady=2)
        remove_btn = tk.Button(side, text="Remove Selected", width=16,
                               command=self._bulk_remove_selected)
        remove_btn.pack(pady=2)
        Tooltip(remove_btn, "Shortcut: Delete")
        tk.Button(side, text="Move Up", width=16, command=lambda: self.bulk_table.move(-1)).pack(pady=2)
        tk.Button(side, text="Move Down", width=16, command=lambda: self.bulk_table.move(1)).pack(pady=2)
        tk.Button(side, text="Clear All", width=16, command=self._bulk_clear).pack(pady=2)
        self.bulk_preview_btn = None
        if mpv_available():
            # Also: double-click a row.
            self.bulk_preview_btn = tk.Button(side, text="Preview Selected", width=16,
                                              command=self._bulk_preview_selected)
            self.bulk_preview_btn.pack(pady=(10, 2))
            self.bulk_tree.bind("<Double-Button-1>", self._bulk_tree_double_click)

        self.bulk_btn = tk.Button(
            root, text="Start Bulk Cut!", height=2,
            font=("Segoe UI", 10, "bold"), command=self._bulk_start_clicked,
        )
        self.bulk_btn.grid(row=3, column=0, sticky="ew", padx=3, pady=(4, 6))

        for target in (self.bulk_tree, self.bulk_table.empty_lbl):
            target.drop_target_register(DND_FILES)
            target.dnd_bind("<<Drop>>", self._on_drop_bulk)
        # Only the blank area below the rows; headings keep column resizing.
        self._bind_window_drag(
            self.bulk_tree,
            lambda e: self.bulk_tree.identify_region(e.x, e.y) == "nothing",
        )
        self._bind_window_drag(self.bulk_table.empty_lbl, lambda e: True)

    # ---- adding / removing rows ------------------------------------------
    def add_files_for_bulk(self):
        paths = filedialog.askopenfilenames(
            title="Add videos for bulk cut",
            filetypes=[("Video files", "*" + " *".join(VIDEO_EXTS)), ("All files", "*.*")],
        )
        if paths:
            self._bulk_add_paths(paths)

    def _bulk_add_paths(self, paths):
        for p in paths:
            if not p.lower().endswith(VIDEO_EXTS):
                continue

            name = os.path.basename(p)
            # A [-SS.-SS] / [-SS] range is relative to the end of the video, so only
            # those files pay for an ffprobe call to read the length.
            last_minute = BULK_LAST_MINUTE_RE.search(name)
            duration = None
            if last_minute:
                try:
                    duration = probe_duration(find_exe("ffprobe"), p)
                except FileNotFoundError:
                    duration = None
            ranges = find_bulk_cut_ranges(name, duration)
            valid_ranges = [(s, e) for s, e in ranges if s is not None and e is not None]
            invalid_count = len(ranges) - len(valid_ranges)

            if last_minute and duration is None:
                range_txt = "—"
                length_txt = "—"
                status = "Failed (unreadable)"
            elif last_minute and any(
                    resolve_last_minute_match(m, duration) is None
                    and all(int(g) <= 59 for g in m.groups() if g is not None)
                    for m in BULK_LAST_MINUTE_RE.finditer(name)):
                range_txt = "—"
                length_txt = "—"
                status = "Failed (too short)"
            elif not valid_ranges:
                range_txt = "—"
                length_txt = "—"
                status = "Failed (no valid bracket)"
            elif any(e <= s for s, e in valid_ranges):
                range_txt = bulk_ranges_text(valid_ranges)
                length_txt = "—"
                status = "Failed (end <= start)"
            else:
                range_txt = bulk_ranges_text(valid_ranges)
                if invalid_count:
                    status = "Failed (invalid bracket present)"
                    length_txt = "—"
                else:
                    length_txt = bulk_lengths_text(valid_ranges)
                    status = "Pending"
            self._bulk_insert_row(p, valid_ranges, range_txt, length_txt, status)

    def _bulk_add_row(self, path, ranges, from_cut=False):
        """A row for ranges that are already known (sent from the Cut tab):
        valid (start, end) seconds, end after start. Returns its tree id."""
        return self._bulk_insert_row(path, list(ranges), bulk_ranges_text(ranges),
                                     bulk_lengths_text(ranges), "Pending", from_cut=from_cut)

    def _bulk_insert_row(self, path, ranges, range_txt, length_txt, status, from_cut=False):
        with self._bulk_lock:
            # Added while a run is going: join its queue straight away.
            if status == "Pending" and self._bulk_running:
                status = "Queued"
            iid = self.bulk_table.insert(path, (range_txt, length_txt, status))
            self.bulk_items.append({
                "path": path, "ranges": ranges,
                "status": status, "iid": iid,
                "from_cut": from_cut,   # made by the Cut tab, which adds its scenes to it
            })
        self._bulk_list_changed()
        return iid

    def _bulk_show_ranges(self, item):
        """Shows a row's ranges again after they changed."""
        if self.bulk_tree.exists(item["iid"]):
            self.bulk_tree.set(item["iid"], "range", bulk_ranges_text(item["ranges"]))
            self.bulk_tree.set(item["iid"], "length", bulk_lengths_text(item["ranges"]))

    def _bulk_list_changed(self):
        """The Cut tab shows the open video's scenes waiting here, and its
        Cut selected scenes button starts this list."""
        if self.input_path:
            self._redraw_slider()
        self._update_cut_range_buttons()

    def _bulk_table_reordered(self):
        # bulk_items is the queue a run takes its next row from, top first,
        # so a run cuts the rows in their new order.
        with self._bulk_lock:
            by_iid = {it["iid"]: it for it in self.bulk_items}
            self.bulk_items = [by_iid[i] for i in self.bulk_table.order() if i in by_iid]

    # Removing a queued row takes it out of bulk_items, which is the list the
    # running worker picks its next item from -- so it is never processed.
    # A row that's already processing finishes its current ffmpeg run.
    def _bulk_remove_selected(self):
        table = self.bulk_table
        rows = table.selected()
        if not rows:
            return
        sel_set = set(rows)
        after = table.order()[table.order().index(rows[-1]) + 1:]
        next_row = next((i for i in after if i not in sel_set), None)
        with self._bulk_lock:
            table.delete(rows)
            self.bulk_items = [it for it in self.bulk_items if it["iid"] not in sel_set]
        if self._bulk_preview_open() and self.bulk_preview.key in sel_set:
            self._bulk_close_preview()
        # The next row takes the selection, so Delete can be pressed again.
        rest = table.order()
        if next_row is None and rest:
            next_row = rest[-1]
        table.select([next_row] if next_row else [])
        self._bulk_list_changed()

    def _bulk_clear(self):
        with self._bulk_lock:
            self.bulk_table.replace([])
            self.bulk_items = []
        self._bulk_close_preview()
        self._bulk_list_changed()

    # ---- preview of a row's ranges -----------------------------------------
    def _bulk_preview_open(self):
        return bool(getattr(self, "_bulk_preview_shown", False))

    def _bulk_tree_double_click(self, event):
        iid = self.bulk_tree.identify_row(event.y)
        if not iid or self.bulk_tree.identify_region(event.x, event.y) not in ("tree", "cell"):
            return None
        item = next((it for it in self.bulk_items if it["iid"] == iid), None)
        if item is not None:
            self._bulk_preview_item(item)
        return "break"

    def _bulk_preview_selected(self):
        sel = self.bulk_tree.selection()
        item = next((it for it in self.bulk_items if sel and it["iid"] == sel[0]), None)
        if item is None:
            self.status_lbl.config(text="Select a row to preview its ranges")
            return
        self._bulk_preview_item(item)

    def _bulk_preview_item(self, item):
        """Plays the row's ranges back to back (each one is a clip Bulk Cut
        will make) in a player under the list."""
        name = os.path.basename(item["path"])
        ranges = [(s, e) for s, e in item["ranges"] if e > s]
        # A range running past the end gives a clip up to the end, so it
        # plays only that far (mpv would show the last frame for the rest).
        try:
            length = probe_duration(find_exe("ffprobe"), item["path"])
        except FileNotFoundError:
            length = None
        if length:
            ranges = [(s, min(e, length)) for s, e in ranges if s < length]
        if not ranges:
            self.status_lbl.config(text=f"{name}: no range to play")
            return
        if self.bulk_preview is None:
            self.bulk_preview = ClipPreview(self.bulk_paned, on_close=self._bulk_close_preview)
        if not self._bulk_preview_open():
            self._bulk_preview_shown = True
            self.bulk_paned.add(self.bulk_preview.frame, stretch="always", minsize=px(140))
            # The list keeps about a third of the height.
            self.bulk_paned.update_idletasks()
            self.bulk_paned.sash_place(
                0, 0, max(px(60), int(self.bulk_paned.winfo_height() * 0.35)))
        labels = [f"Range {i}/{len(ranges)}: {bulk_time_text(s)} – {bulk_time_text(e)}  ·  {name}"
                  for i, (s, e) in enumerate(ranges, 1)]
        volume, muted = self._player_audio_settings()
        try:
            self.bulk_preview.show([(item["path"], s, e - s) for s, e in ranges], labels,
                                   key=item["iid"], volume=volume, muted=muted)
        except RuntimeError as exc:
            self._bulk_close_preview()
            messagebox.showerror("Preview", str(exc))

    def _bulk_close_preview(self):
        if not self._bulk_preview_open():
            return
        self._bulk_preview_shown = False
        self.bulk_preview.unload()
        self.bulk_paned.forget(self.bulk_preview.frame)

    @staticmethod
    def _bulk_output_paths(item, output_dir):
        """Output path for each of an item's ranges: name.CUT.(<range>).ext
        for a single range, name.CUT01.(<range>).ext, name.CUT02.(<range>).ext...
        for several."""
        path = item["path"]
        ranges = item["ranges"]
        name_no_ext, ext = os.path.splitext(os.path.basename(path))
        stripped_name = strip_bracket_from_name(name_no_ext)
        dest_dir = output_dir or os.path.dirname(os.path.abspath(path))
        outs = []
        for idx, (start_sec, end_sec) in enumerate(ranges, 1):
            suffix = format_clip_suffix(start_sec, end_sec)
            tag = f"CUT{idx:02d}" if len(ranges) > 1 else "CUT"
            outs.append(os.path.join(dest_dir, f"{stripped_name}.{tag}.({suffix}){ext}"))
        return outs

    # ---- status helpers (always called back on the main thread via .after) --
    def _bulk_set_status(self, item, status_text):
        item["status"] = status_text
        if self.bulk_tree.exists(item["iid"]):
            self.bulk_tree.set(item["iid"], "status", status_text)
        self._bulk_list_changed()

    # ---- running the batch -------------------------------------------------
    def _bulk_start_clicked(self):
        pending = [it for it in self.bulk_items if it["status"] == "Pending"]
        if not pending:
            messagebox.showinfo(
                "Nothing to cut",
                "No pending files with a valid, unprocessed cut range.\n\n"
                "Files need a name like 'clip [00.00.00-00.00.17].mp4', and must "
                "not already show a Completed/Failed status.",
            )
            return

        if self.save_to_source_var.get():
            output_dir = None  # each file saved next to itself
        else:
            chosen = filedialog.askdirectory(title="Choose output folder for bulk cuts")
            if not chosen:
                return
            output_dir = chosen

        # Decide up front what to do about outputs that already exist, so the
        # user is asked once instead of once per clip. None = nothing clashes
        # yet; the worker asks later if a file added mid-run does clash.
        warn_overwrite = self.warn_overwrite_var.get()
        overwrite_policy = None if warn_overwrite else "overwrite"
        if warn_overwrite:
            existing = [
                p for it in pending for p in self._bulk_output_paths(it, output_dir)
                if os.path.exists(p)
            ]
            if existing:
                overwrite_policy = self._ask_overwrite(existing)
                if overwrite_policy is None:
                    return

        cut_mode = self.cut_mode_var.get()
        search_window = self.cut_search_window

        with self._bulk_lock:
            for it in pending:
                it["status"] = "Queued"
                if self.bulk_tree.exists(it["iid"]):
                    self.bulk_tree.set(it["iid"], "status", "Queued")
            self._bulk_running = True

        self._bulk_list_changed()
        self.bulk_btn.config(state="disabled")
        self.status_lbl.config(text="Bulk cutting... 0%")
        threading.Thread(
            target=self._run_bulk,
            args=(output_dir, cut_mode, search_window, overwrite_policy),
            daemon=True,
        ).start()

    def _run_bulk(self, output_dir, cut_mode, search_window, overwrite_policy):
        """Works through bulk_items as a live queue: each round it takes the
        first row still marked "Queued", so rows added mid-run are picked up
        and rows removed before their turn are never processed."""
        clips_ok = 0
        clips_failed = 0
        files_failed = 0
        # Progress is weighted by clip length: cutting time grows with how
        # long the clip is, so a 10-minute clip counts 10x a 1-minute one.
        done_sec = 0.0  # total length of the clips already finished (or failed)
        last_status = {"text": None}

        def clip_seconds(rng):
            return max(0.0, rng[1] - rng[0])

        def queued_clips():
            """(clip count, total clip seconds) of the rows still queued."""
            with self._bulk_lock:
                queued = [it for it in self.bulk_items if it["status"] == "Queued"]
                return (sum(len(it["ranges"]) for it in queued),
                        sum(clip_seconds(r) for it in queued for r in it["ranges"]))

        def show_progress(text, frac):
            if text == last_status["text"]:
                text = None
            else:
                last_status["text"] = text
            self.after(0, lambda: self._show_progress("bulk", frac, text))

        while True:
            with self._bulk_lock:
                item = next((it for it in self.bulk_items if it["status"] == "Queued"), None)
                if item is None:
                    self._bulk_running = False
                    break
                item["status"] = "Processing"

            path = item["path"]
            ranges = item["ranges"]
            n_clips = len(ranges)
            clip_lengths = [clip_seconds(r) for r in ranges]
            finished_before = clips_ok + clips_failed
            done_sec_before = done_sec
            generated = []
            try:
                for idx, ((start_sec, end_sec), out_path) in enumerate(
                    zip(ranges, self._bulk_output_paths(item, output_dir)), 1
                ):
                    if end_sec <= start_sec:
                        raise RuntimeError(
                            f"Bracket {idx} has an end time that is not after its start time."
                        )
                    if os.path.normcase(os.path.abspath(out_path)) == os.path.normcase(os.path.abspath(path)):
                        raise RuntimeError("Output path would overwrite the source video.")

                    if os.path.exists(out_path):
                        if overwrite_policy is None:
                            overwrite_policy = self._ask_overwrite_from_worker([out_path]) or "skip"
                        if overwrite_policy == "skip":
                            raise RuntimeError(
                                f"Skipped: {os.path.basename(out_path)} already exists."
                            )
                        if overwrite_policy == "rename":
                            out_path = unique_output_path(out_path)

                    item_status = f"Processing clip {idx}/{n_clips}" if n_clips > 1 else "Processing"
                    self.after(0, lambda it=item, s=item_status: self._bulk_set_status(it, s))

                    def clip_progress(frac, idx=idx):
                        q_count, q_sec = queued_clips()
                        total = finished_before + n_clips + q_count
                        total_sec = done_sec_before + sum(clip_lengths) + q_sec
                        if total_sec > 0:
                            cur_sec = (done_sec_before + sum(clip_lengths[:idx - 1])
                                       + frac * clip_lengths[idx - 1])
                            overall = cur_sec / total_sec
                        else:
                            overall = (finished_before + (idx - 1) + frac) / max(1, total)
                        overall = max(0.0, min(1.0, overall))
                        pct = int(overall * 100)
                        if total > 1:
                            failed_txt = f", {clips_failed} failed" if clips_failed else ""
                            show_progress(
                                f"Bulk cutting: {clips_ok}/{total} clips done{failed_txt} — {pct}%",
                                overall,
                            )
                        else:
                            show_progress(f"Bulk cutting... {pct}%", overall)

                    clip_progress(0.0)
                    try:
                        self._lossless_cut(
                            path, start_sec, end_sec, out_path,
                            mode=cut_mode, search_window=search_window,
                            progress=clip_progress,
                        )
                    except Exception as cut_exc:
                        err = str(cut_exc)[-1000:].replace("\n", " ").strip()
                        raise RuntimeError(f"Bracket {idx} failed: {err}")
                    generated.append(out_path)
                    clips_ok += 1

                if not generated:
                    raise RuntimeError("No valid ranges found.")

                n_done = len(generated)
                done_text = f"Completed {n_done} clip" + ("" if n_done == 1 else "s")
                self.after(0, lambda it=item, s=done_text: self._bulk_set_status(it, s))
            except Exception as exc:
                clips_failed += n_clips - len(generated)
                files_failed += 1
                self.after(
                    0,
                    lambda it=item, e=str(exc):
                        self._bulk_set_status(it, f"Failed: {e}")
                )
            done_sec = done_sec_before + sum(clip_lengths)

        self.after(0, lambda: self._end_progress("bulk"))
        self.after(0, lambda: self.bulk_btn.config(state="normal"))
        self.after(0, self._bulk_list_changed)
        done_text = f"Bulk cut finished — {clips_ok} clip" + ("" if clips_ok == 1 else "s") + " completed"
        if files_failed:
            done_text += f", {files_failed} file" + ("" if files_failed == 1 else "s") + " failed"
        self.after(0, lambda t=done_text: self.status_lbl.config(text=t))

    # ====================================================================
    # RESIZE / CROP TAB
    # ====================================================================
    def _build_resize_tab(self):
        root = self.resize_tab
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        header = tk.Frame(root, bg="#f0f0f0")
        header.grid(row=0, column=0, sticky="ew", padx=3, pady=(6, 2))

        self.resize_filename_lbl = tk.Label(
            header,
            text="No file loaded",
            anchor="w", bg="#f0f0f0", font=("Segoe UI", 10, "bold")
        )
        self.resize_filename_lbl.pack(side="left")

        self.resize_info_lbl = tk.Label(
            header, text="", anchor="e", bg="#f0f0f0", font=("Segoe UI", 9)
        )
        self.resize_info_lbl.pack(side="right")

        body = tk.Frame(root, bg="#f0f0f0")
        body.grid(row=1, column=0, sticky="nsew", padx=3, pady=4)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        preview_wrap = tk.Frame(body, bg="#161616")
        preview_wrap.grid(row=0, column=0, sticky="nsew")
        preview_wrap.rowconfigure(0, weight=1)
        preview_wrap.columnconfigure(0, weight=1)

        self.resize_canvas = tk.Canvas(
            preview_wrap, bg="#161616", highlightthickness=0,
            width=px(700), height=px(400)
        )
        self.resize_canvas.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        self.resize_canvas.bind("<Configure>", lambda e: self._redraw_resize_canvas())
        self.resize_canvas.bind("<ButtonPress-1>", self._resize_mouse_down)
        self.resize_canvas.bind("<B1-Motion>", self._resize_mouse_drag)
        self.resize_canvas.bind("<ButtonRelease-1>", self._resize_mouse_up)
        self.resize_canvas.bind("<Motion>", self._resize_mouse_move)
        # Mouse wheel zoom: <MouseWheel> fires on Windows/Mac (with .delta),
        # Button-4/5 fire on Linux/X11 instead.
        self.resize_canvas.bind("<MouseWheel>", self._resize_mouse_wheel)
        self.resize_canvas.bind("<Button-4>", self._resize_mouse_wheel)
        self.resize_canvas.bind("<Button-5>", self._resize_mouse_wheel)
        self._bind_window_drag(self.resize_canvas, lambda e: not self.resize_input_path,
                               on_click=self.open_file_for_resize)
        self._draw_resize_placeholder(self.OPEN_PROMPT)

        controls = tk.Frame(root, bg="#f0f0f0")
        controls.grid(row=2, column=0, sticky="ew", padx=3, pady=(2, 4))

        # The player's row, as on the Cut tab: the time at the left, the
        # play / step / jump buttons in the middle, the sound at the right
        # (its controls come with the player, in _resize_start_player).
        transport = CenteredRow(controls, bg="#f0f0f0")
        transport.pack(fill="x", pady=(2, 1))
        self.resize_transport_row = transport
        self.resize_time_lbl = tk.Label(transport.left, text="", bg="#f0f0f0", anchor="w",
                                        font=("Segoe UI", 9))
        self.resize_time_lbl.pack(side="left", padx=(px(4), 0))
        self.resize_play_btn = None
        self.resize_transport_buttons = []   # need a video in the player
        self.resize_frame_buttons = ()
        if mpv_available():
            def button(text, command, **kw):
                b = tk.Button(transport.center, text=text, command=command, state="disabled", **kw)
                b.pack(side="left", padx=1)
                self.resize_transport_buttons.append(b)
                return b

            button("|◀ Start", lambda: self._resize_go_to(end=False))
            frame_back = button("◀ Frame", lambda: self._resize_frame_step(False))
            self.resize_play_btn = button("▶ Play", self._resize_toggle_play, width=8)
            transport.center_on(self.resize_play_btn)
            frame_fwd = button("Frame ▶", lambda: self._resize_frame_step(True))
            button("End ▶|", lambda: self._resize_go_to(end=True))
            Tooltip(self.resize_play_btn, "Shortcut: Space")
            Tooltip(frame_back, "One frame back. Shortcut: ,")
            Tooltip(frame_fwd, "One frame forward. Shortcut: .")
            # Shown with Options > Show frame by frame movement buttons, as
            # on the Cut tab.
            self.resize_frame_buttons = (frame_back, frame_fwd)
            self._init_resize_button_fit()
            self._on_show_frame_buttons_toggled(save=False)

        self.resize_slider = TimeSlider(controls, command=self._resize_slider_changed,
                                        on_release=self._resize_slider_released)
        self.resize_slider.pack(fill="x", padx=8, pady=(2, 4))

        # The crop box: its shape, a reset; then the turn and the zoom.
        edit_row = tk.Frame(controls, bg="#f0f0f0")
        edit_row.pack(pady=(0, 2))
        self.resize_aspect_var = tk.StringVar(value="freeform")
        self.resize_aspect_btn = tk.Button(edit_row, width=len("Aspect: Freeform ▾") + 2,
                                           command=self._resize_post_aspect_menu)
        self.resize_aspect_btn.pack(side="left", padx=(4, 2))
        self.resize_aspect_menu = tk.Menu(self.resize_aspect_btn, tearoff=0)
        Tooltip(self.resize_aspect_btn,
                "The crop box's shape, which dragging a corner keeps.\n"
                "Freeform: any shape; hold Shift, Ctrl or Alt while\n"
                "dragging a corner to keep the box's shape anyway.")
        tk.Button(edit_row, text="Reset Crop", command=self._resize_reset_crop).pack(
            side="left", padx=(2, 2))

        # Turn the picture clockwise, on top of however the file already
        # shows it. The saved video comes out turned the same way.
        rotate_lbl = tk.Label(edit_row, text="Rotate ↻:", bg="#f0f0f0")
        rotate_lbl.pack(side="left", padx=(18, 2))
        self.resize_rotate_buttons = {}
        # The picked one is tinted, edged in blue and bold, like the Cut
        # tab's picked marker. Sized in pixels for "270°" in bold, so the
        # bolding moves nothing.
        font, bold = self._marker_fonts()
        blank = self._pixel_image()
        for turn in (0, 90, 180, 270):
            # A native button draws no coloured edge: a frame round it does.
            edge = tk.Frame(edit_row, bg="#f0f0f0")
            edge.pack(side="left")
            # padx as Windows' default: X11's (3m) would add ~20 px a button.
            btn = tk.Button(edge, text=f"{turn}°", image=blank, compound="center", padx=1,
                            width=bold.measure("270°") + px(14), height=bold.metrics("linespace"),
                            command=lambda t=turn: self._resize_set_rotation(t))
            btn.pack(padx=px(2), pady=px(2))
            btn._bbvc_edge = edge
            self.resize_rotate_buttons[turn] = btn
        self._resize_button_default_bg = btn.cget("bg")
        self._resize_highlight_rotate_button()

        # Zoom, for fine-tuning the crop edges precisely.
        zoom_lbl = tk.Label(edit_row, text="Zoom:", bg="#f0f0f0")
        zoom_lbl.pack(side="left", padx=(18, 2))
        Tooltip(zoom_lbl, "The mouse wheel over the video zooms too")
        tk.Button(edit_row, text="−", width=3, command=self._resize_zoom_out).pack(side="left")
        self.resize_zoom_lbl = tk.Label(
            edit_row, text="100%", width=6, bg="#f0f0f0", font=("Consolas", 10)
        )
        self.resize_zoom_lbl.pack(side="left", padx=2)
        tk.Button(edit_row, text="+", width=3, command=self._resize_zoom_in).pack(side="left")
        fit_btn = tk.Button(edit_row, text="Fit", command=self._resize_zoom_reset)
        fit_btn.pack(side="left", padx=(6, 2))
        self._resize_edit_row = (edit_row, rotate_lbl, zoom_lbl, fit_btn)
        self._resize_edit_compact = False
        self._resize_edit_full_width = None
        controls.bind("<Configure>", lambda e: self._fit_resize_edit_row(), add="+")

        # Precise crop entry: type the four corner coordinates directly.
        # These are overwritten automatically whenever the crop changes via
        # dragging, the Aspect menu, or Reset Crop, so they always mirror
        # whatever is currently selected.
        coords_row = tk.Frame(controls, bg="#f0f0f0")
        coords_row.pack(pady=(0, 2))

        tk.Label(coords_row, text="Corners:", bg="#f0f0f0").pack(side="left", padx=(4, 4))

        self.resize_entry_vars = {}
        for key, label_text in (
            ("x0", "X0:"), ("y0", "Y0:"), ("x1", "X1:"), ("y1", "Y1:")
        ):
            tk.Label(coords_row, text=label_text, bg="#f0f0f0").pack(side="left", padx=(6, 2))
            var = tk.StringVar(value="0")
            entry = tk.Entry(coords_row, textvariable=var, width=6, font=("Consolas", 10))
            entry.pack(side="left")
            entry.bind("<Return>", self._resize_apply_crop_entries)
            entry.bind("<FocusOut>", self._resize_apply_crop_entries)
            self.resize_entry_vars[key] = var

        tk.Button(
            coords_row, text="Apply", command=self._resize_apply_crop_entries
        ).pack(side="left", padx=(8, 2))
        self.resize_size_lbl = tk.Label(
            coords_row, text="Crop: —", bg="#f0f0f0", font=("Consolas", 10)
        )
        self.resize_size_lbl.pack(side="left", padx=(18, 4))
        self._resize_highlight_aspect_button()

        save_row = tk.Frame(controls, bg="#f0f0f0")
        save_row.pack(fill="x", padx=2, pady=(4, 2))
        # Only the turn, written into the file's rotation info: the video
        # and audio are copied as they are, so it is instant and lossless.
        # The crop box is left out. Enabled while a turn is picked.
        self.resize_rotate_save_btn = tk.Button(
            save_row, text="Save Rotation Only\n(lossless, no crop)", height=2,
            command=self._resize_save_rotation_clicked, state="disabled"
        )
        self.resize_rotate_save_btn.pack(side="right", padx=(4, 0))
        self.resize_save_btn = tk.Button(
            save_row, text="Crop & Save Video!", height=2,
            font=("Segoe UI", 10, "bold"),
            command=self._resize_save_clicked
        )
        self.resize_save_btn.pack(side="left", fill="x", expand=True)
        self.resize_save_btn.config(state="disabled")
        Tooltip(self.resize_save_btn, "Crop must be re-encoded; audio is stream-copied when possible.")

    def _draw_resize_placeholder(self, text):
        self.resize_canvas.delete("all")
        w = self.resize_canvas.winfo_width() or 700
        h = self.resize_canvas.winfo_height() or 400
        if text == self.OPEN_PROMPT:
            self._draw_open_prompt(self.resize_canvas, w, h)
            return
        self.resize_canvas.create_text(
            w / 2, h / 2, text=text, fill="#888888",
            font=("Segoe UI", 12), tags=("placeholder",)
        )

    def open_file_for_resize(self):
        self._ensure_tab_built(3)
        self.notebook.select(self.resize_tab)
        path = filedialog.askopenfilename(
            title="Open video for crop",
            filetypes=[("Video files", "*" + " *".join(VIDEO_EXTS)), ("All files", "*.*")]
        )
        if path:
            self._load_video_for_resize(path)

    def _load_video_for_resize(self, path, quiet=False):
        """quiet: as in _load_video_for_cut."""
        try:
            self.status_lbl.config(text="Reading video info...")
            self.update_idletasks()
            ffprobe = find_exe("ffprobe")

            duration_result = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, check=True, **SUBPROCESS_KWARGS
            )
            duration = float(duration_result.stdout.strip())
            src_w, src_h, rotation = probe_shown_video_size(ffprobe, path)
        except Exception as exc:
            if quiet:
                self.status_lbl.config(
                    text=f"Couldn't open {os.path.basename(path)} on the Crop/Rotate tab")
                return
            messagebox.showerror("Error loading video", str(exc))
            self.status_lbl.config(text="Ready")
            return
        self._share_video(3, path)

        self.resize_input_path = path
        self.resize_duration = duration
        self.resize_src_w = src_w
        self.resize_src_h = src_h
        self.resize_rotation = rotation
        self.resize_user_rotation = 0
        self._resize_highlight_rotate_button()
        self._resize_update_rotate_save_btn()
        self.resize_frame_raw = None
        self.resize_time_sec = 0.0
        self.resize_crop = (0, 0, src_w, src_h)
        self.resize_aspect_lock = None
        self.resize_aspect_key = "freeform"
        self._resize_highlight_aspect_button()
        self.resize_zoom = 1.0
        self._resize_update_zoom_label()

        self.resize_filename_lbl.config(text=os.path.basename(path))
        self.resize_info_lbl.config(text=f"{src_w}×{src_h}   [{seconds_to_hms(duration)}]")
        self.resize_slider.config(to=max(duration, 0.01))
        self.resize_slider.set(0)

        self.resize_save_btn.config(state="normal")
        self._resize_update_labels()
        self._resize_show_time(0.0)
        if not self._resize_start_player(path):
            self._resize_update_preview(0)
        self.status_lbl.config(text="Ready")

    # ---- the video player under the crop box ---------------------------
    # With libmpv the frame on screen is the playing video itself, shown by
    # a player placed over the canvas exactly where the still frame would
    # be. Over the video mpv draws the crop box (an OSD overlay, since no Tk
    # item can be drawn on top of the player); the canvas still draws the
    # parts that stick out past the video's edges. Mouse events on the
    # player go to the canvas's own handlers.
    def _resize_start_player(self, path):
        """Opens path in the Crop tab's player. False when there is no
        player, and the tab shows still frames instead."""
        if not mpv_available():
            return False
        try:
            if self.resize_player is None:
                player = VideoPlayer(self.resize_canvas, on_change=self._on_resize_player_changed,
                                     options={"keepaspect": "no", "cursor_autohide": "no"})
                for sequence, handler in (("<ButtonPress-1>", self._resize_mouse_down),
                                          ("<B1-Motion>", self._resize_mouse_drag),
                                          ("<ButtonRelease-1>", self._resize_mouse_up),
                                          ("<Motion>", self._resize_mouse_move)):
                    player.frame.bind(sequence, lambda e, h=handler: h(self._resize_player_event(e)))
                player.frame.bind("<MouseWheel>", self._resize_mouse_wheel)
                self.resize_player = player
                self.resize_audio = PlayerAudioControls(
                    self, self.resize_transport_row.right, player, self._resize_audio_changed)
            self.resize_audio.set_from(*self._player_audio_settings())
            self.resize_player.set_video_rotate(self.resize_user_rotation)
            self.resize_player.load(path)
        except RuntimeError as exc:
            self.status_lbl.config(text=str(exc))
            return False
        self._resize_use_player(True)
        return True

    def _resize_player_event(self, event):
        """event, moved from the player's frame to canvas coordinates."""
        event.x += self.resize_player.frame.winfo_x()
        event.y += self.resize_player.frame.winfo_y()
        return event

    def _resize_use_player(self, on):
        """Shows the player in place of the still frame, or goes back to
        still frames (when the player can't open the file)."""
        self._resize_player_on = on
        self._resize_view = None
        self.resize_frame_raw = None
        self.resize_frame_img = None
        self.resize_canvas.delete("all")
        self._resize_bg_image_id = None
        self._resize_bg_cache_key = None
        if not on:
            self.resize_player.frame.place_forget()
            self.resize_player.hide_overlay()
            self.resize_play_btn.config(text="▶ Play")
            for b in self.resize_transport_buttons:
                b.config(state="disabled")
            self._resize_update_preview(self.resize_time_sec)
        self._redraw_resize_canvas()

    def _on_resize_player_changed(self):
        p = self.resize_player
        if not self._resize_player_on:
            return
        if p.failed:
            self.status_lbl.config(text="The player can't open this video; showing still frames.")
            self._resize_use_player(False)
            return
        if not p.loaded:
            return
        if str(self.resize_play_btn.cget("state")) == "disabled":
            for b in self.resize_transport_buttons:
                b.config(state="normal")
        self.resize_play_btn.config(text="▶ Play" if p.paused else "❚❚ Pause")
        self.resize_audio.refresh()
        pos = p.position
        if pos is None:
            return
        self.resize_time_sec = pos
        self._resize_show_time(pos)
        if not self.resize_slider.dragging:
            self.resize_slider.set(pos)

    def _resize_player_ready(self):
        return self._resize_player_on and self.resize_player.loaded

    def _resize_toggle_play(self):
        if self._resize_player_ready():
            self.resize_player.toggle_pause()

    def _resize_frame_step(self, forward):
        if self._resize_player_ready():
            self.resize_player.frame_step(forward)

    def _resize_go_to(self, end):
        """The first frame (end=False) or the last one, paused."""
        if self._resize_player_ready():
            p = self.resize_player
            p.set_paused(True)
            if end:
                p.show_last_frame_before(self.resize_duration)
            else:
                p.show_first_frame_from(0.0)

    def _resize_show_time(self, t):
        self.resize_time_lbl.config(text=f"{seconds_to_ffmpeg_time(t)} / "
                                         f"{seconds_to_ffmpeg_time(self.resize_duration)}")

    def _resize_audio_changed(self):
        """The volume or mute changed here: the Cut tab's player, which
        keeps them for every tab (and saves them), takes them too."""
        self.resize_audio.refresh()
        p = self.resize_player
        if getattr(self, "cut_audio", None) is not None:
            self.cut_audio.set_from(p.volume, p.muted)
            self._cut_audio_settings_changed()

    def _init_resize_button_fit(self):
        """The player row's buttons get the Cut tab's extra width
        (_fit_cut_buttons), as much of it as the row has room for."""
        self._resize_fit_base = {b: (b.winfo_reqwidth(), b.winfo_pixels(b.cget("padx")))
                                 for b in self.resize_transport_buttons}
        self._resize_fit_extra = None
        play = self.resize_play_btn
        play.config(width=10)
        self._resize_pause_min = play.winfo_reqwidth()
        play.config(width=8)
        row = self.resize_transport_row
        for w in (row, row.left, row.right):
            w.bind("<Configure>", lambda e: self._fit_resize_buttons(), add="+")

    def _fit_resize_buttons(self):
        if not getattr(self, "_resize_fit_base", None):
            return
        row = self.resize_transport_row
        room = row.winfo_width()
        if room < 2:
            return   # not laid out yet

        def pad(b, extra):
            width = self._resize_fit_base[b][0]
            add = int(round(width * extra / 2))
            if b is self.resize_play_btn:
                add = max(add, (self._resize_pause_min - width + 1) // 2)
            return add

        def needed(extra):
            shown = [b for b in self._resize_fit_base if b.winfo_manager()]
            return (row.left.winfo_reqwidth() + row.right.winfo_reqwidth() + 2 * px(CenteredRow.GAP)
                    + sum(self._resize_fit_base[b][0] + 2 * pad(b, extra) + 2 for b in shown))

        extra = next((x for x in self.CUT_BUTTON_EXTRA if needed(x) <= room), 0.0)
        if extra == self._resize_fit_extra:
            return
        self._resize_fit_extra = extra
        for b, (width, padx) in self._resize_fit_base.items():
            b.config(padx=padx + pad(b, extra))

    def _resize_slider_released(self, value):
        if self._resize_player_ready():
            self.resize_player.seek(value)          # the exact frame, after keyframes while dragging

    def _resize_place_player(self, geom):
        """Puts the player over the part of the canvas the video covers,
        showing (with mpv's video-crop) just the part of the video that is
        in view when zoomed in."""
        _scale, dx0, dy0, dw, dh = geom
        c = self.resize_canvas
        cw, ch = c.winfo_width(), c.winfo_height()
        sw, sh = self.resize_src_w, self.resize_src_h
        kx, ky = dw / sw, dh / sh
        sx0 = max(0, math.floor(-dx0 / kx))
        sx1 = min(sw, math.ceil((cw - dx0) / kx))
        sy0 = max(0, math.floor(-dy0 / ky))
        sy1 = min(sh, math.ceil((ch - dy0) / ky))
        x, y = int(round(dx0 + sx0 * kx)), int(round(dy0 + sy0 * ky))
        w = max(1, int(round(dx0 + sx1 * kx)) - x)
        h = max(1, int(round(dy0 + sy1 * ky)) - y)
        view = (x, y, w, h, sx0, sy0, sx1, sy1)
        if view != self._resize_view:
            self._resize_view = view
            self.resize_player.set_video_crop(
                "" if (sx0, sy0, sx1, sy1) == (0, 0, sw, sh)
                else self._resize_stored_rect_spec(sx0, sy0, sx1, sy1))
            self.resize_player.frame.place(x=x, y=y, width=w, height=h)

    def _resize_stored_rect_spec(self, x0, y0, x1, y1):
        """mpv's video-crop "WxH+X+Y" for a rect of the shown (turned)
        frame: video-crop is in the stored frame's pixels, before mpv turns
        it the way the file asks and then by the Rotate buttons' turn."""
        w, h = self.resize_src_w, self.resize_src_h   # shown size
        turn = (self.resize_rotation + self.resize_user_rotation) % 360
        if turn == 90:      # shown x = stored height - stored y, shown y = stored x
            x0, x1, y0, y1 = y0, y1, w - x1, w - x0
        elif turn == 180:
            x0, x1, y0, y1 = w - x1, w - x0, h - y1, h - y0
        elif turn == 270:   # shown x = stored y, shown y = stored width - stored x
            x0, x1, y0, y1 = h - y1, h - y0, x0, x1
        return f"{x1-x0}x{y1-y0}+{x0}+{y0}"

    def _resize_draw_player_overlay(self, label_x, label_y):
        """The crop box over the video, drawn by mpv: the same shading,
        outline, corner handles and size label (centered on label_x,
        label_y of the canvas) the canvas draws."""
        x, y, w, h, sx0, sy0, sx1, sy1 = self._resize_view
        kx, ky = w / (sx1 - sx0), h / (sy1 - sy0)
        x0, y0, x1, y1 = self.resize_crop
        # The crop box and the whole video, in the player's own pixels.
        bx0, by0 = (x0 - sx0) * kx, (y0 - sy0) * ky
        bx1, by1 = (x1 - sx0) * kx, (y1 - sy0) * ky
        vx0, vy0 = -sx0 * kx, -sy0 * ky
        vx1, vy1 = (self.resize_src_w - sx0) * kx, (self.resize_src_h - sy0) * ky

        def rect(rx0, ry0, rx1, ry1, color, alpha="00"):
            rx0, ry0 = max(rx0, -2), max(ry0, -2)     # off-screen parts aren't drawn anyway
            rx1, ry1 = min(rx1, w + 2), min(ry1, h + 2)
            if rx1 <= rx0 or ry1 <= ry0:
                return None
            rx0, ry0, rx1, ry1 = (int(round(v)) for v in (rx0, ry0, rx1, ry1))
            return (f"{{\\an7\\pos(0,0)\\bord0\\shad0\\1c&H{color}&\\1a&H{alpha}&\\p1}}"
                    f"m {rx0} {ry0} l {rx1} {ry0} {rx1} {ry1} {rx0} {ry1}{{\\p0}}")

        events = [
            # Shade outside the box (four strips tiling the video minus it).
            rect(vx0, vy0, vx1, by0, "000000", "80"),
            rect(vx0, by1, vx1, vy1, "000000", "80"),
            rect(vx0, by0, bx0, by1, "000000", "80"),
            rect(bx1, by0, vx1, by1, "000000", "80"),
        ]
        t = px(2) / 2
        cyan = "FFE500"   # #00e5ff, as BGR
        events += [
            rect(bx0 - t, by0 - t, bx1 + t, by0 + t, cyan),
            rect(bx0 - t, by1 - t, bx1 + t, by1 + t, cyan),
            rect(bx0 - t, by0 - t, bx0 + t, by1 + t, cyan),
            rect(bx1 - t, by0 - t, bx1 + t, by1 + t, cyan),
        ]
        handle = px(7)
        for hx, hy in ((bx0, by0), (bx1, by0), (bx0, by1), (bx1, by1)):
            events.append(rect(hx - handle, hy - handle, hx + handle, hy + handle, "111111"))
            events.append(rect(hx - handle + 1, hy - handle + 1, hx + handle - 1, hy + handle - 1,
                               "FFFFFF"))
        font_px = int(round(9 * UI_SCALE * 96 / 72))
        events.append(f"{{\\an5\\pos({int(round(label_x - x))},{int(round(label_y - y))})"
                      f"\\fnSegoe UI\\b1\\fs{font_px}\\bord1\\shad0\\1c&HFFFFFF&\\3c&H000000&}}"
                      f"{x1-x0}×{y1-y0}")
        self.resize_player.show_overlay("\n".join(e for e in events if e), w, h)

    def _resize_update_labels(self):
        if not self.resize_crop:
            self.resize_size_lbl.config(text="Crop: —")
            return
        x0, y0, x1, y1 = self.resize_crop
        self.resize_size_lbl.config(
            text=f"Crop: {x1-x0}×{y1-y0}  @ ({x0},{y0})"
        )
        self._resize_sync_entries()

    def _resize_sync_entries(self):
        # Mirror the current crop into the corner entry fields. Called after
        # every crop change (drag, aspect buttons, reset, apply) so typed-in
        # values never go stale, and get overwritten the moment you drag.
        if not self.resize_crop or not self.resize_entry_vars:
            return
        x0, y0, x1, y1 = self.resize_crop
        self.resize_entry_vars["x0"].set(str(x0))
        self.resize_entry_vars["y0"].set(str(y0))
        self.resize_entry_vars["x1"].set(str(x1))
        self.resize_entry_vars["y1"].set(str(y1))

    def _resize_apply_crop_entries(self, event=None):
        if not self.resize_input_path or not self.resize_crop or not self.resize_entry_vars:
            return
        try:
            x0 = int(round(float(self.resize_entry_vars["x0"].get())))
            y0 = int(round(float(self.resize_entry_vars["y0"].get())))
            x1 = int(round(float(self.resize_entry_vars["x1"].get())))
            y1 = int(round(float(self.resize_entry_vars["y1"].get())))
        except (ValueError, KeyError):
            # Bad input — snap the fields back to the last valid crop.
            self._resize_sync_entries()
            return

        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        x0 = max(0, min(self.resize_src_w - 2, x0))
        y0 = max(0, min(self.resize_src_h - 2, y0))
        x1 = max(x0 + 2, min(self.resize_src_w, x1))
        y1 = max(y0 + 2, min(self.resize_src_h, y1))

        self.resize_crop = (x0, y0, x1, y1)
        self._resize_update_labels()
        self._redraw_resize_canvas()

    def _resize_update_zoom_label(self):
        if hasattr(self, "resize_zoom_lbl"):
            self.resize_zoom_lbl.config(text=f"{int(round(self.resize_zoom * 100))}%")

    def _resize_zoom_in(self):
        if not self.resize_input_path:
            return
        self.resize_zoom = min(8.0, round(self.resize_zoom * 1.25, 3))
        self._resize_update_zoom_label()
        self._redraw_resize_canvas()

    def _resize_zoom_out(self):
        if not self.resize_input_path:
            return
        self.resize_zoom = max(1.0, round(self.resize_zoom / 1.25, 3))
        self._resize_update_zoom_label()
        self._redraw_resize_canvas()

    def _resize_zoom_reset(self):
        self.resize_zoom = 1.0
        self._resize_update_zoom_label()
        self._redraw_resize_canvas()

    def _resize_mouse_wheel(self, event):
        if not self.resize_input_path:
            return
        delta = getattr(event, "delta", 0)
        num = getattr(event, "num", None)
        if delta > 0 or num == 4:
            self._resize_zoom_in()
        elif delta < 0 or num == 5:
            self._resize_zoom_out()

    def _resize_slider_changed(self, value):
        if not self.resize_input_path:
            return
        if self._resize_player_on:
            if self.resize_player.loaded:
                self.resize_player.seek(float(value), exact=False)   # keyframes while dragging: fast
            return
        self.resize_time_sec = float(value)
        self._resize_show_time(self.resize_time_sec)
        self._resize_update_preview(self.resize_time_sec)

    def _resize_update_preview(self, at_seconds):
        if not self.resize_input_path or not PIL_AVAILABLE:
            return
        if self.resize_after_id is not None:
            self.after_cancel(self.resize_after_id)
        self.resize_after_id = self.after(
            100, lambda: self._resize_start_preview_extraction(at_seconds)
        )

    def _resize_start_preview_extraction(self, at_seconds):
        self.resize_after_id = None
        self.resize_request_id += 1
        request_id = self.resize_request_id
        threading.Thread(
            target=self._resize_extract_frame,
            args=(at_seconds, request_id),
            daemon=True
        ).start()

    def _resize_extract_frame(self, at_seconds, request_id):
        out_path = os.path.join(
            tempfile.gettempdir(),
            f"_lossless_cutter_resize_preview_{request_id}.jpg"
        )
        try:
            ffmpeg = find_exe("ffmpeg")
            cmd = [
                ffmpeg, "-y", "-ss", seconds_to_hms(at_seconds),
                "-i", self.resize_input_path, "-frames:v", "1",
                "-q:v", "3", out_path
            ]
            subprocess.run(cmd, capture_output=True, check=True, **SUBPROCESS_KWARGS)
            img = Image.open(out_path).convert("RGB")
            img.load()
            self.after(
                0, lambda: self._resize_set_frame_if_current(img, request_id)
            )
        except Exception:
            pass
        finally:
            try:
                os.remove(out_path)
            except OSError:
                pass

    def _resize_set_frame_if_current(self, img, request_id):
        if request_id != self.resize_request_id:
            return
        self.resize_frame_raw = img
        self.resize_frame_img = self._resize_turned_frame(img)
        self._redraw_resize_canvas()

    def _resize_turned_frame(self, img):
        """img (a still frame, which ffmpeg already turned the way the file
        asks) turned clockwise by the Rotate buttons' turn."""
        if img is None or not self.resize_user_rotation:
            return img
        transpose = getattr(Image, "Transpose", Image)    # PIL's turns are counterclockwise
        return img.transpose({90: transpose.ROTATE_270, 180: transpose.ROTATE_180,
                              270: transpose.ROTATE_90}[self.resize_user_rotation])

    def _resize_canvas_geometry(self):
        c = self.resize_canvas
        cw = c.winfo_width()
        ch = c.winfo_height()
        if self._resize_player_on:
            img_w, img_h = self.resize_src_w, self.resize_src_h
        elif self.resize_frame_img:
            img_w, img_h = self.resize_frame_img.width, self.resize_frame_img.height
        else:
            return None
        if cw < 2 or ch < 2:
            return None

        fit_scale = min((cw - px(20)) / img_w, (ch - px(20)) / img_h)
        fit_scale = max(0.01, fit_scale)
        scale = fit_scale * max(1.0, self.resize_zoom)

        dw = max(1, int(img_w * scale))
        dh = max(1, int(img_h * scale))

        # When zoomed in, center the view on the crop rectangle so you can
        # fine-tune whichever edges you're currently working on, instead of
        # always re-centering on the whole frame.
        if self.resize_crop:
            x0, y0, x1, y1 = self.resize_crop
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        else:
            cx, cy = img_w / 2, img_h / 2

        dx0 = cw / 2 - cx * scale
        dy0 = ch / 2 - cy * scale

        # Keep the image pinned to the canvas: centered when it's smaller
        # than the viewport on an axis, clamped so it never leaves a blank
        # gap on that axis when it's larger (i.e. while zoomed in).
        if dw <= cw:
            dx0 = (cw - dw) / 2
        else:
            dx0 = min(0, max(cw - dw, dx0))
        if dh <= ch:
            dy0 = (ch - dh) / 2
        else:
            dy0 = min(0, max(ch - dh, dy0))

        return scale, dx0, dy0, dw, dh

    def _redraw_resize_canvas(self, overlay_only=False):
        # Perf note: this used to call PIL .resize(LANCZOS) + build a brand
        # new PhotoImage on every single call -- including every mouse-motion
        # tick while dragging a crop handle (dozens of events/sec). Resizing
        # a full-resolution video frame that often is what made the crop
        # editor feel laggy. Now the resized background image is cached and
        # only rebuilt when the source frame or the on-screen image size
        # actually changes; dragging the crop rectangle only touches the
        # cheap overlay (a few rectangles/lines), never the image.
        c = self.resize_canvas

        if not self.resize_frame_img and not self._resize_player_on:
            c.delete("all")
            self._resize_bg_image_id = None
            self._resize_bg_cache_key = None
            self._draw_resize_placeholder(
                self.OPEN_PROMPT if not self.resize_input_path else "Loading frame..."
            )
            return

        geom = self._resize_canvas_geometry()
        if not geom:
            return

        scale, dx0, dy0, dw, dh = geom
        cache_key = (id(self.resize_frame_img), dw, dh)
        c.delete("placeholder")   # the empty-area prompt

        if self._resize_player_on:
            self._resize_place_player(geom)
        elif cache_key != getattr(self, "_resize_bg_cache_key", None):
            img = self.resize_frame_img.resize((dw, dh), Image.BILINEAR)
            self.resize_frame_photo = ImageTk.PhotoImage(img)
            self._resize_bg_cache_key = cache_key
            if getattr(self, "_resize_bg_image_id", None) is None:
                self._resize_bg_image_id = c.create_image(
                    dx0, dy0, image=self.resize_frame_photo, anchor="nw", tags=("bgimg",)
                )
            else:
                c.itemconfig(self._resize_bg_image_id, image=self.resize_frame_photo)
                c.coords(self._resize_bg_image_id, dx0, dy0)
        else:
            # Same source frame + same on-screen size (nothing was zoomed or
            # resized) -- just the crop rectangle changed, so the image
            # itself doesn't need to move... except while zoomed in, where
            # the view re-centers on the crop as it's dragged.
            c.coords(self._resize_bg_image_id, dx0, dy0)

        c.delete("overlay")

        if self.resize_crop:
            x0, y0, x1, y1 = self.resize_crop
            px0 = dx0 + x0 * dw / self.resize_src_w
            py0 = dy0 + y0 * dh / self.resize_src_h
            px1 = dx0 + x1 * dw / self.resize_src_w
            py1 = dy0 + y1 * dh / self.resize_src_h
            self.resize_display_rect = (px0, py0, px1, py1)

            # Dim everything outside the selected crop area. These four
            # rectangles must exactly tile the full dw×dh image area minus
            # the crop rect (top/bottom span the full width; left/right
            # only fill the middle band between them) — otherwise the two
            # side strips and the far corners are left undimmed, which is
            # what produced the old "fade only works in some areas" bug.
            c.create_rectangle(
                dx0, dy0, dx0 + dw, py0, fill="#000000", stipple="gray50",
                outline="", tags=("overlay",)
            )
            c.create_rectangle(
                dx0, py1, dx0 + dw, dy0 + dh, fill="#000000", stipple="gray50",
                outline="", tags=("overlay",)
            )
            c.create_rectangle(
                dx0, py0, px0, py1, fill="#000000", stipple="gray50",
                outline="", tags=("overlay",)
            )
            c.create_rectangle(
                px1, py0, dx0 + dw, py1, fill="#000000", stipple="gray50",
                outline="", tags=("overlay",)
            )
            c.create_rectangle(
                px0, py0, px1, py1, fill="", outline="#00e5ff", width=px(2), tags=("overlay",)
            )

            handle = px(7)
            for hx, hy in (
                (px0, py0), (px1, py0), (px0, py1), (px1, py1)
            ):
                c.create_rectangle(
                    hx - handle, hy - handle, hx + handle, hy + handle,
                    fill="#ffffff", outline="#111111", width=1, tags=("overlay",)
                )

            # The size goes just above the box, or just inside its top edge
            # when there is no room above it: past the canvas's top, or (with
            # the player) half on the video and half off it, where it would
            # be drawn in two halves by two different renderers.
            label_x, label_y = (px0 + px1) / 2, py0 - px(14)
            top = max(0, self._resize_view[1]) if self._resize_player_on else 0
            if label_y - px(9) < top:
                label_y = py0 + px(14)
            c.create_text(
                label_x, label_y,
                text=f"{x1-x0}×{y1-y0}",
                fill="#ffffff", font=("Segoe UI", 9, "bold"), tags=("overlay",)
            )
            if self._resize_player_on:
                self._resize_draw_player_overlay(label_x, label_y)

    def _resize_source_point(self, event):
        geom = self._resize_canvas_geometry()
        if not geom:
            return None
        _, dx0, dy0, dw, dh = geom
        x = min(self.resize_src_w, max(0, (event.x - dx0) * self.resize_src_w / dw))
        y = min(self.resize_src_h, max(0, (event.y - dy0) * self.resize_src_h / dh))
        return x, y

    def _resize_mouse_hit(self, event):
        if not self.resize_display_rect:
            return None
        px0, py0, px1, py1 = self.resize_display_rect
        r = px(12)

        near_left = abs(event.x - px0) <= r
        near_right = abs(event.x - px1) <= r
        near_top = abs(event.y - py0) <= r
        near_bottom = abs(event.y - py1) <= r

        if near_left and near_top:
            return "nw"
        if near_right and near_top:
            return "ne"
        if near_left and near_bottom:
            return "sw"
        if near_right and near_bottom:
            return "se"

        # Border (edge) handles: drag just one side without moving the
        # opposite one, same as dragging a corner but along one axis only.
        if near_top and px0 <= event.x <= px1:
            return "n"
        if near_bottom and px0 <= event.x <= px1:
            return "s"
        if near_left and py0 <= event.y <= py1:
            return "w"
        if near_right and py0 <= event.y <= py1:
            return "e"

        inside = px0 <= event.x <= px1 and py0 <= event.y <= py1
        if inside:
            return "move"
        return None

    def _resize_mouse_down(self, event):
        if not self.resize_input_path or not self.resize_crop:
            return

        hit = self._resize_mouse_hit(event)
        point = self._resize_source_point(event)
        if point is None:
            return

        if hit:
            self.resize_drag_mode = hit
        else:
            # Start a brand-new crop rectangle by dragging from the clicked point.
            self.resize_drag_mode = "new"
            x, y = point
            self.resize_crop = (
                int(round(x)), int(round(y)),
                int(round(x)), int(round(y))
            )

        self.resize_drag_start = point
        self.resize_crop_start = self.resize_crop
        self._redraw_resize_canvas()

    def _resize_mouse_drag(self, event):
        if not self.resize_input_path or not self.resize_drag_mode:
            return

        point = self._resize_source_point(event)
        if point is None:
            return
        mx, my = point

        x0, y0, x1, y1 = self.resize_crop_start
        min_size = 2

        ratio = None if self.resize_drag_mode == "move" else self._resize_drag_ratio(event)
        if ratio:
            self.resize_crop = self._resize_ratio_rect(self.resize_drag_mode, mx, my, ratio)
        elif self.resize_drag_mode == "move":
            sx, sy = self.resize_drag_start
            dx, dy = mx - sx, my - sy
            w, h = x1 - x0, y1 - y0
            nx0 = min(self.resize_src_w - w, max(0, x0 + dx))
            ny0 = min(self.resize_src_h - h, max(0, y0 + dy))
            self.resize_crop = (
                int(round(nx0)), int(round(ny0)),
                int(round(nx0 + w)), int(round(ny0 + h))
            )
        elif self.resize_drag_mode == "nw":
            self.resize_crop = (
                int(round(min(mx, x1-min_size))),
                int(round(min(my, y1-min_size))),
                x1, y1
            )
        elif self.resize_drag_mode == "ne":
            self.resize_crop = (
                x0,
                int(round(min(my, y1-min_size))),
                int(round(max(mx, x0+min_size))),
                y1
            )
        elif self.resize_drag_mode == "sw":
            self.resize_crop = (
                int(round(min(mx, x1-min_size))),
                y0,
                x1,
                int(round(max(my, y0+min_size)))
            )
        elif self.resize_drag_mode == "se":
            self.resize_crop = (
                x0, y0,
                int(round(max(mx, x0+min_size))),
                int(round(max(my, y0+min_size)))
            )
        elif self.resize_drag_mode == "n":
            self.resize_crop = (
                x0, int(round(min(my, y1-min_size))), x1, y1
            )
        elif self.resize_drag_mode == "s":
            self.resize_crop = (
                x0, y0, x1, int(round(max(my, y0+min_size)))
            )
        elif self.resize_drag_mode == "w":
            self.resize_crop = (
                int(round(min(mx, x1-min_size))), y0, x1, y1
            )
        elif self.resize_drag_mode == "e":
            self.resize_crop = (
                x0, y0, int(round(max(mx, x0+min_size))), y1
            )
        elif self.resize_drag_mode == "new":
            sx, sy = self.resize_drag_start
            self.resize_crop = (
                int(round(min(sx, mx))),
                int(round(min(sy, my))),
                int(round(max(sx, mx))),
                int(round(max(sy, my)))
            )

        x0, y0, x1, y1 = self.resize_crop
        x0 = max(0, min(self.resize_src_w-1, x0))
        y0 = max(0, min(self.resize_src_h-1, y0))
        x1 = max(x0+1, min(self.resize_src_w, x1))
        y1 = max(y0+1, min(self.resize_src_h, y1))
        self.resize_crop = (x0, y0, x1, y1)

        self._resize_update_labels()
        self._redraw_resize_canvas()

    # Modifier bits in a Tk event's .state: Shift, Control, and Alt -- which
    # is 0x20000 on Windows, where 0x0008 means NumLock instead of Alt.
    _RATIO_LOCK_MODIFIERS = 0x0001 | 0x0004 | (0x20000 if os.name == "nt" else 0x0008)

    def _resize_drag_ratio(self, event):
        """Width/height ratio the current drag must keep, or None to let it
        distort freely (Freeform with no modifier key held)."""
        if self.resize_aspect_lock:
            w, h = self.resize_aspect_lock
            return w / h
        if event.state & self._RATIO_LOCK_MODIFIERS:
            if self.resize_drag_mode == "new":
                return 1.0  # a brand-new box has no shape to keep yet: square
            x0, y0, x1, y1 = self.resize_crop_start
            return max(1, x1 - x0) / max(1, y1 - y0)
        return None

    def _resize_ratio_rect(self, mode, mx, my, ratio):
        """New crop rect for a corner/edge/new-box drag that keeps `ratio`,
        following the mouse as closely as the ratio and frame edges allow."""
        x0, y0, x1, y1 = self.resize_crop_start
        src_w, src_h = self.resize_src_w, self.resize_src_h
        min_size = 2

        if mode in ("n", "s", "e", "w"):
            # One edge follows the mouse; the other dimension follows the
            # ratio, growing/shrinking evenly around the box's center.
            if mode in ("e", "w"):
                w = (mx - x0) if mode == "e" else (x1 - mx)
                w = max(min_size, min(w, (src_w - x0) if mode == "e" else x1))
                h = w / ratio
                if h > src_h:
                    h = src_h
                    w = h * ratio
                nx0 = x0 if mode == "e" else x1 - w
                ny0 = max(0, min(src_h - h, (y0 + y1) / 2 - h / 2))
            else:
                h = (my - y0) if mode == "s" else (y1 - my)
                h = max(min_size, min(h, (src_h - y0) if mode == "s" else y1))
                w = h * ratio
                if w > src_w:
                    w = src_w
                    h = w / ratio
                ny0 = y0 if mode == "s" else y1 - h
                nx0 = max(0, min(src_w - w, (x0 + x1) / 2 - w / 2))
            return (int(round(nx0)), int(round(ny0)),
                    int(round(nx0 + w)), int(round(ny0 + h)))

        # Corners (and drawing a new box): the opposite corner stays put.
        if mode == "new":
            ax, ay = self.resize_drag_start
            dir_x = 1 if mx >= ax else -1
            dir_y = 1 if my >= ay else -1
        else:
            dir_x = 1 if mode in ("ne", "se") else -1
            dir_y = 1 if mode in ("sw", "se") else -1
            ax = x0 if dir_x > 0 else x1
            ay = y0 if dir_y > 0 else y1
        want_w = (mx - ax) * dir_x
        want_h = (my - ay) * dir_y
        max_w = (src_w - ax) if dir_x > 0 else ax
        max_h = (src_h - ay) if dir_y > 0 else ay

        # Follow whichever axis the mouse has pulled further, then shrink
        # to fit inside the frame.
        w = max(min_size, want_w, want_h * ratio)
        w = min(w, max_w, max_h * ratio)
        h = w / ratio
        nx0 = ax if dir_x > 0 else ax - w
        ny0 = ay if dir_y > 0 else ay - h
        return (int(round(nx0)), int(round(ny0)),
                int(round(nx0 + w)), int(round(ny0 + h)))

    def _resize_mouse_up(self, event):
        self.resize_drag_mode = None
        self.resize_drag_start = None
        self.resize_crop_start = None

    def _resize_mouse_move(self, event):
        hit = self._resize_mouse_hit(event)
        cursors = {
            "nw": "size_nw_se", "se": "size_nw_se",
            "ne": "size_ne_sw", "sw": "size_ne_sw",
            "n": "size_ns", "s": "size_ns",
            "e": "size_we", "w": "size_we",
            "move": "fleur",
        }
        self.resize_canvas.config(cursor=cursors.get(hit, ""))
        if self.resize_player is not None:
            self.resize_player.frame.config(cursor=cursors.get(hit, ""))

    # The Aspect menu's shapes past Freeform and Original: (heading, ((ratio,
    # what it's for), ...)); the tall ones mirror the wide ones.
    RESIZE_ASPECTS = (
        (None, (("1:1", "Square"),)),
        ("Wide", (("4:3", "Classic TV"), ("16:9", "Widescreen, YouTube"),
                  ("16:10", "Laptop screen"), ("21:9", "Cinema, ultrawide"))),
        ("Tall", (("3:4", "Portrait photo"), ("9:16", "Shorts, Reels, TikTok"),
                  ("10:16", "Tablet, upright"), ("9:21", "Tall phone"))),
    )

    def _resize_highlight_aspect_button(self):
        """Shows the shape picked on the Aspect button (and in its menu)."""
        key = self.resize_aspect_key or "freeform"
        self.resize_aspect_var.set(key)
        name = {"freeform": "Freeform", "original": "Original"}.get(key, key)
        self.resize_aspect_btn.config(text=f"Aspect: {name} ▾")

    def _resize_post_aspect_menu(self):
        menu = self.resize_aspect_menu
        menu.delete(0, "end")
        no_video = "disabled" if not self.resize_input_path else "normal"
        if self.resize_input_path:
            w, h = self.resize_src_w, self.resize_src_h
            g = math.gcd(w, h) or 1
            own = f"{w // g}:{h // g}" if max(w, h) // g <= 64 else f"{w}×{h}"
        else:
            own = ""
        menu.add_radiobutton(label="Freeform", accelerator="any shape",
                             variable=self.resize_aspect_var, value="freeform",
                             command=self._resize_set_freeform)
        menu.add_radiobutton(label="Original", accelerator=own, state=no_video,
                             variable=self.resize_aspect_var, value="original",
                             command=self._resize_set_original_aspect)
        for heading, shapes in self.RESIZE_ASPECTS:
            menu.add_separator()
            if heading:
                menu.add_command(label=heading, state="disabled")
            for ratio, use in shapes:
                aw, ah = (int(v) for v in ratio.split(":"))
                menu.add_radiobutton(label=f"{ratio}   {use}", state=no_video,
                                     variable=self.resize_aspect_var, value=ratio,
                                     command=lambda aw=aw, ah=ah: self._resize_set_aspect(aw, ah))
        dark = self._is_dark()
        menu.configure(DARK_MENU if dark else self._menu_light_colors)
        self.resize_aspect_var.set(self.resize_aspect_key or "freeform")
        b = self.resize_aspect_btn
        x, y = b.winfo_rootx(), b.winfo_rooty() + b.winfo_height()
        post_with_menu_margin(lambda: menu.tk_popup(x, y), DARK_MENU["background"] if dark else None)

    def _resize_reset_crop(self):
        if not self.resize_input_path:
            return
        self.resize_crop = (0, 0, self.resize_src_w, self.resize_src_h)
        # A full-frame box only matches the source's own ratio, so a preset
        # lock (say 9:16 on a 16:9 video) goes back to Original. Freeform
        # stays Freeform.
        if self.resize_aspect_key != "freeform":
            self.resize_aspect_lock = (self.resize_src_w, self.resize_src_h)
            self.resize_aspect_key = "original"
            self._resize_highlight_aspect_button()
        self._resize_update_labels()
        self._redraw_resize_canvas()

    def _resize_set_original_aspect(self):
        if not self.resize_input_path:
            return
        self.resize_aspect_key = "original"
        self._resize_reset_crop()

    def _resize_set_freeform(self):
        self.resize_aspect_lock = None
        self.resize_aspect_key = "freeform"
        self._resize_highlight_aspect_button()

    def _resize_set_aspect(self, aspect_w, aspect_h):
        if not self.resize_input_path:
            return
        self.resize_aspect_lock = (aspect_w, aspect_h)
        self.resize_aspect_key = f"{aspect_w}:{aspect_h}"
        self._resize_highlight_aspect_button()

        x0, y0, x1, y1 = self.resize_crop
        cx = (x0 + x1) / 2
        cy = (y0 + y1) / 2
        cur_w = max(2, x1 - x0)
        cur_h = max(2, y1 - y0)
        target_h = cur_w * aspect_h / aspect_w
        target_w = cur_w

        if target_h > self.resize_src_h:
            target_h = self.resize_src_h
            target_w = target_h * aspect_w / aspect_h
        if target_w > self.resize_src_w:
            target_w = self.resize_src_w
            target_h = target_w * aspect_h / aspect_w

        nx0 = max(0, min(self.resize_src_w-target_w, cx-target_w/2))
        ny0 = max(0, min(self.resize_src_h-target_h, cy-target_h/2))
        self.resize_crop = (
            int(round(nx0)), int(round(ny0)),
            int(round(nx0 + target_w)), int(round(ny0 + target_h))
        )
        self._resize_update_labels()
        self._redraw_resize_canvas()

    def _fit_resize_edit_row(self):
        """The Aspect / Rotate / Zoom row, compact when it doesn't fit the
        window (a wider font, e.g. Linux's): smaller gaps, the Aspect button
        and the rotate buttons only as wide as their text. Otherwise its
        right end (Fit) would be cut off."""
        row, rotate_lbl, zoom_lbl, fit_btn = self._resize_edit_row
        room = row.master.winfo_width()
        if room < 2:
            return   # not laid out yet
        if not self._resize_edit_compact:
            self._resize_edit_full_width = row.winfo_reqwidth()
        compact = self._resize_edit_full_width > room
        if compact == self._resize_edit_compact:
            return
        self._resize_edit_compact = compact
        gap = px(6) if compact else 18
        rotate_lbl.pack_configure(padx=(gap, 2))
        zoom_lbl.pack_configure(padx=(gap, 2))
        fit_btn.pack_configure(padx=(2 if compact else 6, 2))
        self.resize_aspect_btn.configure(width=0 if compact else len("Aspect: Freeform ▾") + 2)
        bold = self._marker_fonts()[1]
        for btn in self.resize_rotate_buttons.values():
            btn.configure(width=bold.measure("270°") + px(4 if compact else 14))

    def _resize_highlight_rotate_button(self):
        font, bold = self._marker_fonts()
        for turn, btn in self.resize_rotate_buttons.items():
            active = turn == self.resize_user_rotation
            self._configure_light(btn._bbvc_edge,
                                  bg=self.MARKER_PICKED_COLOR if active else "#f0f0f0")
            self._configure_light(
                btn,
                bg="#cfe2ff" if active else self._resize_button_default_bg,
                font=bold if active else font,
            )

    def _resize_set_rotation(self, turn):
        """Turns the picture clockwise by turn degrees, on top of the file's
        own rotation. The crop box turns with the picture, so it keeps
        covering the same part of the video."""
        if not self.resize_input_path:
            return
        steps = (turn - self.resize_user_rotation) % 360 // 90
        if not steps:
            return
        for _ in range(steps):
            # 90° clockwise: the point (x, y) of a W×H frame goes to (H - y, x)
            # of the H×W one.
            x0, y0, x1, y1 = self.resize_crop
            h = self.resize_src_h
            self.resize_crop = (h - y1, x0, h - y0, x1)
            self.resize_src_w, self.resize_src_h = h, self.resize_src_w
        if steps % 2:
            # The box's shape turned too, so 16:9 is now 9:16 (Original and
            # Freeform stay as they are).
            if self.resize_aspect_lock:
                self.resize_aspect_lock = self.resize_aspect_lock[::-1]
            if ":" in (self.resize_aspect_key or ""):
                self.resize_aspect_key = ":".join(self.resize_aspect_key.split(":")[::-1])
            self._resize_highlight_aspect_button()
        self.resize_user_rotation = turn
        self._resize_highlight_rotate_button()
        self._resize_update_rotate_save_btn()
        self.resize_info_lbl.config(
            text=f"{self.resize_src_w}×{self.resize_src_h}   [{seconds_to_hms(self.resize_duration)}]")
        if self._resize_player_on:
            self.resize_player.set_video_rotate(turn)
            self._resize_view = None                  # re-place it at the turned shape
        else:
            self.resize_frame_img = self._resize_turned_frame(self.resize_frame_raw)
        self._resize_update_labels()
        self._redraw_resize_canvas()

    def _resize_save_clicked(self):
        if not self.resize_input_path or not self.resize_crop:
            return

        x0, y0, x1, y1 = self.resize_crop
        crop_w, crop_h = x1-x0, y1-y0
        if crop_w < 2 or crop_h < 2:
            messagebox.showerror("Invalid crop", "The crop area is too small.")
            return

        source_dir = os.path.dirname(os.path.abspath(self.resize_input_path))
        base_name = os.path.splitext(os.path.basename(self.resize_input_path))[0]
        turn = self.resize_user_rotation
        default_name = f"{base_name}.CROP.{crop_w}x{crop_h}{f'.ROT{turn}' if turn else ''}.mp4"

        from_dialog = not self.save_to_source_var.get()
        if not from_dialog:
            out_path = os.path.join(source_dir, default_name)
            if os.path.abspath(out_path) == os.path.abspath(self.resize_input_path):
                out_path = os.path.join(source_dir, base_name + ".CROP.mp4")
        else:
            out_path = filedialog.asksaveasfilename(
                title="Save cropped video as", initialdir=source_dir,
                initialfile=default_name, defaultextension=".mp4",
                filetypes=[("MP4 video", "*.mp4"), ("All files", "*.*")],
            )
            if not out_path:
                return
        out_path = self._check_output_path(
            out_path, [self.resize_input_path], prompt_overwrite=not from_dialog
        )
        if not out_path:
            return

        self.resize_save_btn.config(state="disabled")
        self.resize_rotate_save_btn.config(state="disabled")
        self.status_lbl.config(text="Cropping (re-encoding video)... 0%")
        crop = self.resize_crop
        threading.Thread(
            target=self._resize_run_crop,
            args=(out_path, crop, turn),
            daemon=True
        ).start()

    def _resize_run_crop(self, out_path, crop, turn=0):
        """Saves the crop box's part of the video, after turning the picture
        clockwise by turn (crop is in the turned frame's pixels)."""
        try:
            ffmpeg = find_exe("ffmpeg")
            x0, y0, x1, y1 = crop
            crop_w, crop_h = x1-x0, y1-y0

            # Arbitrary spatial cropping cannot be done with -c copy because the
            # encoded macroblocks/frames still contain the pixels outside the crop.
            # Use fast H.264 re-encoding (GPU-accelerated when available) while
            # copying the existing audio stream. ffmpeg has already turned the
            # frames the way the file asks before -vf, so the Rotate buttons'
            # turn goes on top, then the crop.
            vf = f"crop={crop_w}:{crop_h}:{x0}:{y0}"
            rotate = {90: "transpose=clock", 180: "hflip,vflip", 270: "transpose=cclock"}.get(turn)
            if rotate:
                vf = f"{rotate},{vf}"

            def build_cmd(encoder_args):
                return [
                    ffmpeg, "-y", "-i", self.resize_input_path,
                    "-map", "0:v:0", "-map", "0:a?",
                    "-vf", vf,
                    *encoder_args,
                    "-pix_fmt", "yuv420p",
                    "-c:a", "copy",
                    "-movflags", "+faststart",
                    out_path
                ]

            report = self._progress_reporter("Cropping (re-encoding video)...", "crop")
            encoder_args, _label = get_video_encoder_args(ffmpeg)
            proc = run_ffmpeg(build_cmd(encoder_args), self.resize_duration, report)
            if proc.returncode != 0 and encoder_args[1] != "libx264":
                # The hardware encoder can occasionally fail for a clip it
                # doesn't like (resolution/profile limits, driver hiccup) --
                # fall back to the software encoder once before giving up.
                software_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
                proc = run_ffmpeg(build_cmd(software_args), self.resize_duration, report)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr[-3000:])

            self.after(0, lambda p=out_path: self._resize_done(p, True))
        except Exception as exc:
            self.after(0, lambda e=str(exc): self._resize_done(e, False))

    def _resize_update_rotate_save_btn(self):
        self.resize_rotate_save_btn.config(
            state="normal" if self.resize_input_path and self.resize_user_rotation else "disabled")

    def _resize_save_rotation_clicked(self):
        """Saves the video turned by the Rotate buttons' turn, without
        re-encoding: only the file's rotation info changes."""
        turn = self.resize_user_rotation
        if not self.resize_input_path or not turn:
            return
        source_dir = os.path.dirname(os.path.abspath(self.resize_input_path))
        base_name, ext = os.path.splitext(os.path.basename(self.resize_input_path))
        # The rotation info is a display matrix, which MP4 and MOV keep.
        ext = ext.lower() if ext.lower() in (".mp4", ".m4v", ".mov") else ".mp4"
        default_name = f"{base_name}.ROT{turn}{ext}"

        from_dialog = not self.save_to_source_var.get()
        if not from_dialog:
            out_path = os.path.join(source_dir, default_name)
        else:
            out_path = filedialog.asksaveasfilename(
                title="Save rotated video as", initialdir=source_dir,
                initialfile=default_name, defaultextension=ext,
                filetypes=[(f"{ext[1:].upper()} video", "*" + ext), ("All files", "*.*")],
            )
            if not out_path:
                return
        out_path = self._check_output_path(
            out_path, [self.resize_input_path], prompt_overwrite=not from_dialog
        )
        if not out_path:
            return

        self.resize_save_btn.config(state="disabled")
        self.resize_rotate_save_btn.config(state="disabled")
        self.status_lbl.config(text="Rotating (copying, no re-encode)... 0%")
        total = (self.resize_rotation + turn) % 360
        threading.Thread(
            target=self._resize_run_rotation,
            args=(out_path, total),
            daemon=True
        ).start()

    def _resize_run_rotation(self, out_path, total_turn):
        """Copies the video and audio into out_path as they are, with the
        rotation info set so players show it turned clockwise by total_turn
        from the stored frames (the file's own turn plus the Rotate
        buttons' one)."""
        try:
            ffmpeg = find_exe("ffmpeg")
            # -display_rotation replaces the input's display matrix; it is
            # counterclockwise, and 0 leaves none.
            cmd = [
                ffmpeg, "-y", "-display_rotation", str((360 - total_turn) % 360),
                "-i", self.resize_input_path,
                "-map", "0:v:0", "-map", "0:a?",
                "-c", "copy",
                "-movflags", "+faststart",
                out_path
            ]
            report = self._progress_reporter("Rotating (copying, no re-encode)...", "crop")
            proc = run_ffmpeg(cmd, self.resize_duration, report)
            if proc.returncode != 0:
                hint = ""
                if "display_rotation" in proc.stderr:
                    hint = ("This ffmpeg has no -display_rotation option; "
                            "lossless rotation needs ffmpeg 6.0 or newer.\n\n")
                raise RuntimeError(hint + proc.stderr[-3000:])
            self.after(0, lambda p=out_path: self._resize_done(p, True, "Rotated"))
        except Exception as exc:
            self.after(0, lambda e=str(exc): self._resize_done(e, False, "Rotated"))

    def _resize_done(self, info, success, what="Cropped"):
        self._end_progress("crop")
        self.resize_save_btn.config(state="normal")
        self._resize_update_rotate_save_btn()
        if success:
            self.status_lbl.config(text=f"Saved: {info}")
            self._saved_ask_open(f"{what} video saved to:\n{info}", info, 3, f"{what.lower()} video")
        else:
            verb = "rotation" if what == "Rotated" else "crop"
            self.status_lbl.config(text=f"{verb.capitalize()} failed")
            messagebox.showerror("ffmpeg error", f"The {verb} failed.\n\n" + info)

    def _on_drop_resize(self, event):
        self._ensure_tab_built(3)
        paths = parse_dropped_paths(event.data)
        video_paths = [p for p in paths if p.lower().endswith(VIDEO_EXTS)]
        if video_paths:
            self._load_video_for_resize(video_paths[0])
        elif paths:
            messagebox.showwarning(
                "Unsupported file",
                "That doesn't look like a supported video file."
            )

    # ====================================================================
    # AUDIO TAB
    #
    # A visual scrubber/player for the video (Play/Pause and ±5s just step
    # through extracted preview frames -- like the rest of this app, there
    # is no real-time decode/audio-output pipeline here, only on-demand
    # single-frame ffmpeg extraction, so this is a frame slideshow rather
    # than smooth real-time playback with audible sound), one waveform +
    # volume control per audio track, and a Finalize step that mixes the
    # enabled tracks into a single audible output. The video stream is
    # always stream-copied (untouched, lossless); only the audio is
    # re-encoded, because combining separate compressed audio streams into
    # one requires decoding them first -- there's no lossless way to mix.
    # ====================================================================
    # The Audio tab's volume sliders go up to this (400%, +12 dB). Above
    # 100% a loud track's peaks can go past full scale and clip.
    AUDIO_MAX_GAIN = 4.0

    def _build_audio_tab(self):
        root = self.audio_tab
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=3)
        root.rowconfigure(4, weight=2)

        # ---- header
        header = tk.Frame(root, bg="#f0f0f0")
        header.grid(row=0, column=0, sticky="ew", padx=3, pady=(6, 2))
        self.audio_filename_lbl = tk.Label(
            header, text="No file loaded",
            anchor="w", bg="#f0f0f0", font=("Segoe UI", 10, "bold")
        )
        self.audio_filename_lbl.pack(side="left")
        self.audio_info_lbl = tk.Label(
            header, text="", anchor="e", bg="#f0f0f0", font=("Segoe UI", 9)
        )
        self.audio_info_lbl.pack(side="right")

        # ---- preview
        preview_frame = tk.Frame(root, bg="black")
        preview_frame.grid(row=1, column=0, sticky="nsew", padx=3, pady=4)
        self.audio_canvas = tk.Canvas(preview_frame, bg="black", highlightthickness=0)
        self.audio_canvas.pack(fill="both", expand=True)
        self.audio_canvas.bind("<Configure>", lambda e: self._redraw_audio_canvas())
        self._bind_window_drag(self.audio_canvas, lambda e: not self.audio_input_path,
                               on_click=self.open_file_for_audio)
        self._draw_audio_placeholder(self.OPEN_PROMPT)
        if mpv_available():
            # Plays the video with the mix of the enabled tracks, as Finalize
            # writes it (see _audio_apply_mix); clicking it plays / pauses.
            self.audio_player = VideoPlayer(preview_frame, on_change=self._on_audio_player_changed)
            self.audio_player.frame.bind("<Button-1>", lambda e: self._audio_toggle_play())

        # ---- playback controls
        controls = tk.Frame(root, bg="#f0f0f0")
        controls.grid(row=2, column=0, sticky="ew", padx=3, pady=(2, 4))

        time_row = tk.Frame(controls, bg="#f0f0f0")
        time_row.pack(pady=(2, 2))
        self.audio_time_lbl = tk.Label(
            time_row, text="00:00:00", width=9, bg="#f0f0f0", font=("Consolas", 10)
        )
        self.audio_time_lbl.pack(side="left")
        self.audio_duration_lbl = tk.Label(
            time_row, text="/ 00:00:00", width=12, anchor="w",
            bg="#f0f0f0", font=("Consolas", 10)
        )
        self.audio_duration_lbl.pack(side="left")

        btn_row = tk.Frame(controls, bg="#f0f0f0")
        btn_row.pack(pady=(0, 4))
        tk.Button(
            btn_row, text="◄◄ 5s", width=8, command=lambda: self._audio_step(-5)
        ).pack(side="left", padx=4)
        self.audio_play_btn = tk.Button(
            btn_row, text="▶ Play", width=10, command=self._audio_toggle_play
        )
        self.audio_play_btn.pack(side="left", padx=4)
        tk.Button(
            btn_row, text="5s ►►", width=8, command=lambda: self._audio_step(5)
        ).pack(side="left", padx=4)

        self.audio_slider = TimeSlider(controls, command=self._audio_slider_changed,
                                       on_release=self._audio_slider_released)
        self.audio_slider.pack(fill="x", padx=8, pady=(0, 4))

        # ---- tracks section
        tracks_header = tk.Frame(root, bg="#f0f0f0")
        tracks_header.grid(row=3, column=0, sticky="ew", padx=3, pady=(6, 2))
        tk.Label(
            tracks_header,
            text="Audio tracks — enable the ones to include in the output, "
                 "and set each one's volume:",
            bg="#f0f0f0", anchor="w"
        ).pack(side="left")

        # The track list + side buttons, laid out like the Combine and Bulk
        # Cut lists.
        tracks_body = tk.Frame(root, bg="#f0f0f0")
        tracks_body.grid(row=4, column=0, sticky="nsew", padx=3, pady=2)
        tracks_body.rowconfigure(0, weight=1)
        tracks_body.columnconfigure(0, weight=1)

        tracks_list = tk.Frame(tracks_body, bg="#f0f0f0", highlightthickness=1,
                               highlightbackground="#828790", highlightcolor="#828790")
        tracks_list.grid(row=0, column=0, sticky="nsew")
        tracks_list.rowconfigure(0, weight=1)
        tracks_list.columnconfigure(0, weight=1)
        tracks_scrollbar = ttk.Scrollbar(tracks_list)
        tracks_scrollbar.grid(row=0, column=1, sticky="ns")
        self.audio_tracks_canvas = tk.Canvas(
            tracks_list, bg=FIELD_BG, highlightthickness=0,
            yscrollcommand=tracks_scrollbar.set
        )
        self.audio_tracks_canvas.grid(row=0, column=0, sticky="nsew")
        tracks_scrollbar.config(command=self.audio_tracks_canvas.yview)

        side = tk.Frame(tracks_body, bg="#f0f0f0")
        side.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        self.audio_balance_btn = tk.Button(
            side, text="Auto Balance Audio", width=16, state="disabled",
            command=self._audio_auto_balance_clicked)
        self.audio_balance_btn.pack(pady=2)

        self.audio_tracks_inner = tk.Frame(self.audio_tracks_canvas, bg=FIELD_BG)
        self._audio_tracks_inner_window = self.audio_tracks_canvas.create_window(
            (0, 0), window=self.audio_tracks_inner, anchor="nw"
        )
        self.audio_tracks_inner.bind(
            "<Configure>",
            lambda e: self.audio_tracks_canvas.configure(
                scrollregion=self.audio_tracks_canvas.bbox("all")
            ),
        )

        def _on_tracks_canvas_configure(e):
            self.audio_tracks_canvas.itemconfig(self._audio_tracks_inner_window, width=e.width)
            self._audio_resize_waveforms()

        self.audio_tracks_canvas.bind("<Configure>", _on_tracks_canvas_configure)

        self._audio_rebuild_track_rows()

        # ---- finalize
        self.audio_finalize_btn = tk.Button(
            root, text="Save enabled tracks together!", height=2,
            font=("Segoe UI", 10, "bold"), command=self._audio_finalize_clicked
        )
        self.audio_finalize_btn.grid(row=6, column=0, sticky="ew", padx=3, pady=(4, 6))
        self.audio_finalize_btn.config(state="disabled")
        Tooltip(self.audio_finalize_btn, "Only the audio part will be re-encoded.")

        targets = [self.audio_canvas, self.audio_tracks_canvas, self.audio_tracks_inner]
        if self.audio_player is not None:
            targets.append(self.audio_player.frame)
        for target in targets:
            target.drop_target_register(DND_FILES)
            target.dnd_bind("<<Drop>>", self._on_drop_audio)

    # ---- preview canvas drawing (same caching approach as the crop tab) --
    def _draw_audio_placeholder(self, text):
        c = self.audio_canvas
        c.delete("all")
        self._audio_bg_image_id = None
        self._audio_bg_cache_key = None
        w = c.winfo_width() or 700
        h = c.winfo_height() or 350
        if text == self.OPEN_PROMPT:
            self._draw_open_prompt(c, w, h)
        else:
            c.create_text(w / 2, h / 2, text=text, fill="#888888", font=("Segoe UI", 12),
                          justify="center")

    def _audio_show_no_audio(self, path):
        """The tab empties, saying the video (shared from another tab) has
        no audio to mix."""
        self._audio_stop_playback_state()
        if self.audio_player is not None:
            self.audio_player.set_paused(True)
            self._audio_use_player(False)
        self.audio_input_path = None
        self.audio_duration = 0.0
        self.audio_frame_img = None
        self.audio_tracks = []
        self._dfn_reset()
        self._audio_rebuild_track_rows()
        for btn in (self.audio_balance_btn, self.audio_finalize_btn):
            btn.config(state="disabled")
        self.audio_filename_lbl.config(text=os.path.basename(path))
        self.audio_info_lbl.config(text="no audio tracks")
        self.audio_slider.set(0)
        self.audio_time_lbl.config(text=seconds_to_hms(0))
        self.audio_duration_lbl.config(text=f"/ {seconds_to_hms(0)}")
        self._audio_note = ("This video has no audio tracks to mix.\n\n"
                            "Click here or drop another video.")
        self._redraw_audio_canvas()
        self.status_lbl.config(text="Ready")

    def _redraw_audio_canvas(self):
        c = self.audio_canvas
        if not self.audio_frame_img:
            self._draw_audio_placeholder(
                (self._audio_note or self.OPEN_PROMPT) if not self.audio_input_path
                else "Loading frame..."
            )
            return
        cw = c.winfo_width()
        ch = c.winfo_height()
        if cw < 2 or ch < 2:
            return
        img = self.audio_frame_img
        scale = min(cw / img.width, ch / img.height)
        dw = max(1, int(img.width * scale))
        dh = max(1, int(img.height * scale))
        dx0, dy0 = (cw - dw) / 2, (ch - dh) / 2
        cache_key = (id(img), dw, dh)
        if cache_key != self._audio_bg_cache_key:
            resized = img.resize((dw, dh), Image.BILINEAR)
            self.audio_frame_photo = ImageTk.PhotoImage(resized)
            self._audio_bg_cache_key = cache_key
            c.delete("all")
            self._audio_bg_image_id = c.create_image(
                dx0, dy0, image=self.audio_frame_photo, anchor="nw"
            )
        else:
            c.coords(self._audio_bg_image_id, dx0, dy0)

    # ---- loading -------------------------------------------------------
    def open_file_for_audio(self):
        self._ensure_tab_built(4)
        self.notebook.select(self.audio_tab)
        path = filedialog.askopenfilename(
            title="Open video for audio mixing",
            filetypes=[("Video files", "*" + " *".join(VIDEO_EXTS)), ("All files", "*.*")]
        )
        if path:
            self._load_video_for_audio(path)

    def _load_video_for_audio(self, path, quiet=False):
        """quiet: as in _load_video_for_cut; a video without audio then
        empties the tab, saying so (_audio_show_no_audio)."""
        try:
            self.status_lbl.config(text="Reading video info...")
            self.update_idletasks()
            ffprobe = find_exe("ffprobe")

            duration_result = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, check=True, **SUBPROCESS_KWARGS
            )
            duration = float(duration_result.stdout.strip())

            audio_result = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=codec_name,channels,sample_rate",
                 "-of", "json", path],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                check=True, **SUBPROCESS_KWARGS
            )
            audio_streams = json.loads(audio_result.stdout or "{}").get("streams") or []
        except Exception as exc:
            if quiet:
                self.status_lbl.config(text=f"Couldn't open {os.path.basename(path)} on the Audio tab")
                return
            messagebox.showerror("Error loading video", str(exc))
            self.status_lbl.config(text="Ready")
            return

        if not audio_streams and quiet:
            self._audio_show_no_audio(path)
            return
        if not audio_streams:
            messagebox.showerror(
                "No audio tracks",
                "This video doesn't have any audio streams to mix."
            )
            self.status_lbl.config(text="Ready")
            return

        self._share_video(4, path)
        self._audio_stop_playback_state()
        self._dfn_reset()
        self._audio_note = None
        self.audio_input_path = path
        self.audio_duration = duration

        self.audio_filename_lbl.config(text=os.path.basename(path))
        self.audio_info_lbl.config(
            text=f"[{seconds_to_hms(duration)}]   {format_size(os.path.getsize(path))}"
        )
        self.audio_slider.config_range(to=max(duration, 0.01))
        self.audio_slider.set(0)
        self.audio_time_lbl.config(text="00:00:00")
        self.audio_duration_lbl.config(text=f"/ {seconds_to_hms(duration)}")

        self.audio_tracks = []
        for i, stream in enumerate(audio_streams):
            self.audio_tracks.append({
                "a_index": i,
                "desc": describe_audio_stream(stream),
                "channels": int(stream.get("channels") or 2),
                "enabled_var": tk.BooleanVar(value=True),
                "denoise_var": tk.StringVar(value="off"),
                "mono_var": tk.BooleanVar(value=False),
                "volume_slider": None,
                "volume_lbl": None,
                "name_label": None,
                "waveform_canvas": None,
                "waveform_img": None,
                "waveform_photo": None,
                "_wave_cache_key": None,
            })

        self._audio_rebuild_track_rows()
        for btn in (self.audio_balance_btn, self.audio_finalize_btn):
            btn.config(state="normal")
        status = "Ready"
        if self.audio_player is not None:
            try:
                volume, muted = self._player_audio_settings()
                self.audio_player.set_volume(volume)
                self.audio_player.set_muted(muted)
                self.audio_player.load(path)
                self._audio_use_player(True)
            except RuntimeError as exc:
                self._audio_use_player(False)
                status = str(exc)
        if not self._audio_player_on:
            self._audio_update_preview(0)
        self.status_lbl.config(text=status)

    # ---- track rows / waveforms -----------------------------------------
    def _audio_rebuild_track_rows(self):
        for child in self.audio_tracks_inner.winfo_children():
            child.destroy()

        if not self.audio_tracks:
            # Like the Combine and Bulk Cut lists' empty text.
            placeholder = tk.Label(
                self.audio_tracks_inner,
                text="(drag & drop a video here, or click the video area above)",
                bg=FIELD_BG, fg="#777777", anchor="w", font=("Segoe UI", 10),
            )
            placeholder.pack(fill="x", padx=px(10), pady=px(8))
            placeholder.drop_target_register(DND_FILES)
            placeholder.dnd_bind("<<Drop>>", self._on_drop_audio)
            return

        for track in self.audio_tracks:
            row = tk.Frame(self.audio_tracks_inner, bg="#f0f0f0")
            row.pack(fill="x", padx=4, pady=4)

            top = tk.Frame(row, bg="#f0f0f0")
            top.pack(fill="x", padx=3, pady=(4, 2))

            tk.Checkbutton(
                top, variable=track["enabled_var"], bg="#f0f0f0",
                activebackground="#f0f0f0", selectcolor=FIELD_BG,
                command=lambda t=track: self._audio_track_toggled(t),
            ).pack(side="left")

            label_text = (
                f"Track {track['a_index'] + 1}   ({track['desc']})"
            )
            name_lbl = tk.Label(
                top, text=label_text, bg="#f0f0f0", font=("Segoe UI", 10), anchor="w",
                fg="#000000" if track["enabled_var"].get() else "#777777",
            )
            track["name_label"] = name_lbl

            vol_frame = tk.Frame(top, bg="#f0f0f0")
            vol_frame.pack(side="right")
            tk.Label(
                vol_frame, text="Volume:", bg="#f0f0f0", fg="#555555"
            ).pack(side="left", padx=(0, 4))
            vol_lbl = tk.Label(
                vol_frame, text="100%", bg="#f0f0f0", fg="#000000",
                width=5, font=("Segoe UI", 10)
            )
            vol_lbl.pack(side="right")
            vol_slider = TimeSlider(
                vol_frame, command=lambda v, t=track: self._audio_volume_changed(t, v),
                width=160, height=22,
            )
            vol_slider.config_range(to=self.AUDIO_MAX_GAIN)
            vol_slider.set(1.0)
            self._style_volume_slider(vol_slider)
            vol_slider.pack(side="right", padx=(6, 6))
            track["volume_slider"] = vol_slider
            track["volume_lbl"] = vol_lbl

            mono_box = tk.Checkbutton(
                top, text="Mono", variable=track["mono_var"], bg="#f0f0f0",
                activebackground="#f0f0f0", selectcolor=FIELD_BG,
                state="disabled" if track["channels"] < 2 else "normal",
                command=self._audio_maybe_restart_playback,
            )
            mono_box.pack(side="right", padx=(0, px(10)))
            Tooltip(mono_box,
                    "The same in both ears: this track's channels averaged\n"
                    "(e.g. a mic recorded on one side only). A sound on one\n"
                    "side comes out 6 dB quieter, one in the middle as loud."
                    if track["channels"] >= 2 else "This track has one channel: it already\n"
                    "sounds the same in both ears.")

            denoise_btn = tk.Button(
                top, width=max(len(name) for _key, name in DENOISE_LEVELS) + 2,
                command=lambda t=track: self._audio_post_denoise_menu(t))
            denoise_btn.pack(side="right", padx=(0, px(14)))
            Tooltip(denoise_btn,
                    "Noise reduction for this track.\n"
                    "Light: RNNoise, applied as the video plays.\n"
                    "Medium / Extreme: DeepFilterNet, worked out in the background "
                    "first.\nLight and Medium leave some of the background in, so it "
                    "doesn't\ncut in and out with the voice; Extreme takes it all.")
            track["denoise_btn"] = denoise_btn
            track["denoise_menu"] = tk.Menu(denoise_btn, tearoff=0)
            self._audio_show_denoise_level(track)
            # Packed last: in a narrow window the name is cut, not the controls.
            name_lbl.pack(side="left", padx=(2, 12))

            wave_canvas = tk.Canvas(row, height=px(96), bg="#0a0a0a", highlightthickness=0)
            wave_canvas.pack(fill="x", padx=3, pady=(0, 6))
            wave_canvas.bind("<Configure>", lambda e, t=track: self._audio_redraw_waveform(t))
            wave_canvas.bind("<ButtonPress-1>", self._audio_wave_press)
            wave_canvas.bind("<B1-Motion>", self._audio_wave_drag)
            wave_canvas.bind("<ButtonRelease-1>", self._audio_wave_release)
            wave_canvas.create_text(
                px(10), px(48), text="Generating waveform…", fill="#666666",
                anchor="w", font=("Segoe UI", 9), tags=("placeholder",)
            )
            track["waveform_canvas"] = wave_canvas
            track["wave_image_id"] = None
            track["playhead_id"] = None
            self._audio_show_denoise_status(track)

            self._audio_generate_waveform(track)

    def _audio_track_toggled(self, track):
        lbl = track.get("name_label")
        if lbl is not None:
            self._configure_light(lbl, fg="#000000" if track["enabled_var"].get() else "#777777")
        self._audio_apply_mix()
        self._audio_maybe_restart_playback()

    def _audio_volume_changed(self, track, value):
        lbl = track.get("volume_lbl")
        if lbl is not None:
            lbl.config(text=f"{int(round(value * 100))}%")
        self._audio_redraw_waveform(track)
        self._audio_maybe_restart_playback()

    def _audio_generate_waveform(self, track):
        path = self.audio_input_path
        a_index = track["a_index"]

        def worker():
            try:
                img = render_waveform(path, a_index, 1200, 260, color="0x00e5ff").convert("RGB")
                self.after(0, lambda: self._audio_set_waveform(path, track, img))
            except Exception:
                self.after(0, lambda: self._audio_waveform_failed(path, track))

        threading.Thread(target=worker, daemon=True).start()

    def _audio_set_waveform(self, path, track, img):
        if path != self.audio_input_path:
            return  # a different video has since been loaded
        c = track.get("waveform_canvas")
        if c is not None and c.winfo_exists():
            c.delete("placeholder")
        track["waveform_img"] = img
        track["_wave_cache_key"] = None
        self._audio_redraw_waveform(track)

    def _audio_waveform_failed(self, path, track):
        if path != self.audio_input_path:
            return
        c = track.get("waveform_canvas")
        if c is not None and c.winfo_exists():
            c.delete("placeholder")
            c.create_text(
                px(10), (c.winfo_height() or px(96)) / 2, text="Waveform unavailable",
                fill="#666666", anchor="w", font=("Segoe UI", 9), tags=("placeholder",)
            )

    def _audio_compose_waveform_image(self, img, target_w, target_h, volume):
        # Literalizes "the waves should get smaller/bigger when I adjust volume":
        # stretch/squash the (already-rendered) waveform vertically around its
        # centerline by the volume factor. Regenerating from ffmpeg per volume
        # tick would be far too slow; this is a cheap PIL resize on a small image.
        base = img.resize((target_w, img.height), Image.BILINEAR)
        scale = max(0.1, min(self.AUDIO_MAX_GAIN, volume))
        scaled_h = max(4, int(round(base.height * scale)))
        scaled = base.resize((target_w, scaled_h), Image.BILINEAR)
        canvas_img = Image.new("RGB", (target_w, target_h), (10, 10, 10))
        if scaled_h <= target_h:
            canvas_img.paste(scaled, (0, (target_h - scaled_h) // 2))
        else:
            top = (scaled_h - target_h) // 2
            canvas_img.paste(scaled.crop((0, top, target_w, top + target_h)), (0, 0))
        return canvas_img

    def _audio_redraw_waveform(self, track):
        c = track.get("waveform_canvas")
        img = track.get("waveform_img")
        if c is None or not c.winfo_exists():
            return
        cw = c.winfo_width()
        ch = c.winfo_height()
        if cw < 2 or ch < 2:
            return

        if img is not None:
            volume = track["volume_slider"].get() if track["volume_slider"] else 1.0
            cache_key = (id(img), cw, ch, round(volume, 2))
            if track.get("_wave_cache_key") != cache_key:
                composed = self._audio_compose_waveform_image(img, cw, ch, volume)
                photo = ImageTk.PhotoImage(composed)
                track["waveform_photo"] = photo
                track["_wave_cache_key"] = cache_key
                if track.get("wave_image_id") is None:
                    track["wave_image_id"] = c.create_image(
                        0, 0, image=photo, anchor="nw", tags=("wave",)
                    )
                    c.tag_lower("wave")
                else:
                    c.itemconfig(track["wave_image_id"], image=photo)

        self._audio_draw_playhead(track, cw, ch)

    def _audio_draw_playhead(self, track, cw, ch):
        c = track.get("waveform_canvas")
        if c is None or not c.winfo_exists():
            return
        frac = (self.audio_time_sec / self.audio_duration) if self.audio_duration else 0.0
        x = min(cw, max(0, frac * cw))
        if track.get("playhead_id") is None:
            track["playhead_id"] = c.create_line(
                x, 0, x, ch, fill="#ffffff", width=px(2), tags=("playhead",)
            )
        else:
            c.coords(track["playhead_id"], x, 0, x, ch)
            c.tag_raise(track["playhead_id"])

    def _audio_resize_waveforms(self):
        for track in self.audio_tracks:
            self._audio_redraw_waveform(track)

    def _audio_update_playheads(self):
        for track in self.audio_tracks:
            c = track.get("waveform_canvas")
            if c is None or not c.winfo_exists():
                continue
            cw, ch = c.winfo_width(), c.winfo_height()
            if cw < 2 or ch < 2:
                continue
            self._audio_draw_playhead(track, cw, ch)

    def _audio_wave_press(self, event):
        """A press on a track's waveform moves the playback there; dragging
        scrubs, like the slider (keyframes while moving, the exact frame on
        release)."""
        if not self.audio_input_path or self.audio_duration <= 0:
            return
        self._audio_wave_scrubbing = True
        self._audio_wave_seek(event, exact=False)

    def _audio_wave_drag(self, event):
        if self._audio_wave_scrubbing:
            self._audio_wave_seek(event, exact=False)

    def _audio_wave_release(self, event):
        if self._audio_wave_scrubbing:
            self._audio_wave_scrubbing = False
            self._audio_wave_seek(event, exact=True)

    def _audio_wave_seek(self, event, exact):
        width = max(1, event.widget.winfo_width())
        t = max(0.0, min(1.0, event.x / width)) * self.audio_duration
        self.audio_slider.set(t)
        if self._audio_player_on:
            if self._audio_player_ready():
                self.audio_player.seek(t, exact=exact)
            self._audio_show_time(t)
        elif exact:
            self._audio_slider_changed(t)   # ffplay restarts once, on release
        else:
            self._audio_show_time(t)

    # ---- playback ---------------------------------------------------------
    # With libmpv the video plays in the player (as on the Cut tab), with
    # the enabled tracks mixed at their volumes as Finalize mixes them
    # (_audio_apply_mix); the time, slider and waveform playheads follow
    # it. Without it, the preview steps to a still frame every ~300ms and
    # the sound is handed off to ffplay (bundled with ffmpeg) running the
    # same mix filter graph, with no video output (-nodisp), started at the
    # current scrub position on Play and killed on Pause/seek/close.
    def _audio_player_ready(self):
        return self._audio_player_on and self.audio_player.loaded

    def _audio_use_player(self, on):
        """Shows the player over the still-frame canvas, or takes it away."""
        self._audio_player_on = on
        if on:
            self.audio_player.frame.place(x=0, y=0, relwidth=1, relheight=1)
        else:
            if self.audio_player is not None:
                self.audio_player.frame.place_forget()
            self.audio_play_btn.config(text="▶ Play")

    def _on_audio_player_changed(self):
        """Called (on the Tk thread) when the player's position, play state
        or file changed."""
        p = self.audio_player
        if not self._audio_player_on:
            return
        if p.failed:
            self.status_lbl.config(text="The player can't open this video; showing still frames.")
            self._audio_use_player(False)
            self._audio_update_preview(self.audio_time_sec)
            return
        if not p.loaded or "track-list" not in p.state:
            return
        if self._audio_restart_after_id is None:
            self._audio_apply_mix()   # the file just opened; else it's already set
        self.audio_play_btn.config(text="▶ Play" if p.paused else "⏸ Pause")
        pos = p.position
        if pos is None or self._audio_wave_scrubbing:
            return   # while scrubbing, the playheads stay under the mouse
        self.audio_time_sec = max(0.0, min(self.audio_duration, pos))
        self.audio_time_lbl.config(text=seconds_to_hms(self.audio_time_sec))
        if not self.audio_slider.dragging:
            self.audio_slider.set(self.audio_time_sec)
        self._audio_update_playheads()

    def _audio_apply_mix(self):
        """Gives the player the enabled tracks at their volumes, each with
        its noise reduction: Light as a filter, Medium / Extreme as the
        cleaned WAV once it's there (added to the player as an extra
        track; the track plays as it is until then)."""
        if not self._audio_player_ready():
            return
        player = self.audio_player
        ids, volumes, chains, folds = [], [], [], []
        for t in self.audio_tracks:
            if not t["enabled_var"].get():
                continue
            wav, chain = self._audio_denoise_source(t)
            chains.append(chain)
            folds.append(self._audio_track_fold(t))
            track_id = t["a_index"] + 1     # mpv counts audio tracks from 1
            if wav is not None:
                added = player.external_audio_id(wav)
                if added is not None:
                    track_id = added
                elif wav not in self._audio_added_wavs:
                    self._audio_added_wavs.add(wav)
                    player.add_audio_file(wav)
            ids.append(track_id)
            volumes.append(t["volume_slider"].get() if t["volume_slider"] else 1.0)
        # Light comes RNNOISE_DELAY late: the other tracks wait as long, and
        # the mix plays that much earlier.
        late = any(chains)
        if late:
            wait = f"adelay=delays={RNNOISE_DELAY * 1000:g}:all=1"
            chains = [c or wait for c in chains]
        filters = [",".join(f for f in pair if f) for pair in zip(chains, folds)]
        player.set_audio_delay(-RNNOISE_DELAY if late else 0.0)
        player.set_audio_mix(ids, volumes, filters)

    def _audio_slider_released(self, value):
        if self._audio_player_ready():
            self.audio_player.seek(float(value))     # the exact frame, after keyframes while dragging

    def _audio_slider_changed(self, value):
        if not self.audio_input_path:
            return
        if self._audio_player_on:
            if self._audio_player_ready():
                self.audio_player.seek(float(value), exact=not self.audio_slider.dragging)
            self._audio_show_time(float(value))
            return
        was_playing = self.audio_playing
        self._audio_pause()
        self._audio_set_time(float(value))
        if was_playing:
            self._audio_play()

    def _audio_step(self, delta):
        if not self.audio_input_path:
            return
        if self._audio_player_on:
            if self._audio_player_ready():
                self.audio_player.seek_relative(delta)
            return
        was_playing = self.audio_playing
        self._audio_pause()
        self._audio_set_time(self.audio_time_sec + delta)
        if was_playing:
            self._audio_play()

    def _audio_set_time(self, t):
        self.audio_time_sec = max(0.0, min(self.audio_duration, t))
        self.audio_slider.set(self.audio_time_sec)
        self.audio_time_lbl.config(text=seconds_to_hms(self.audio_time_sec))
        self._audio_update_preview(self.audio_time_sec)
        self._audio_update_playheads()

    def _audio_show_time(self, t):
        """The time label and playheads at t, right away (the player's own
        position follows once its seek lands)."""
        self.audio_time_sec = max(0.0, min(self.audio_duration, t))
        self.audio_time_lbl.config(text=seconds_to_hms(self.audio_time_sec))
        self._audio_update_playheads()

    def _audio_toggle_play(self):
        if not self.audio_input_path:
            return
        if self._audio_player_on:
            if self._audio_player_ready():
                self.audio_player.toggle_pause()
            return
        if self.audio_playing:
            self._audio_pause()
        else:
            self._audio_play()

    def _audio_play(self):
        if self.audio_time_sec >= self.audio_duration:
            self.audio_time_sec = 0.0
        self.audio_playing = True
        self.audio_play_btn.config(text="⏸ Pause")
        self._audio_start_ffplay(self.audio_time_sec)
        self._audio_play_tick()

    def _audio_play_tick(self):
        if not self.audio_playing:
            return
        next_t = self.audio_time_sec + 0.3
        if next_t >= self.audio_duration:
            self._audio_set_time(self.audio_duration)
            self._audio_pause()
            return
        self._audio_set_time(next_t)
        # Paced at 1x: 0.3s of video time advances per 300ms of wall time,
        # which is what keeps this roughly in sync with ffplay's own clock.
        self.audio_play_after_id = self.after(300, self._audio_play_tick)

    def _audio_pause(self):
        self.audio_playing = False
        self.audio_play_btn.config(text="▶ Play")
        if self.audio_play_after_id is not None:
            self.after_cancel(self.audio_play_after_id)
            self.audio_play_after_id = None
        self._audio_stop_ffplay()

    def _audio_build_filter_complex(self, track_settings):
        """track_settings: list of (source, filters, volume) for the tracks
        to include, as _audio_mix_sources gives them. Shared by live
        playback (ffplay) and Finalize (ffmpeg) so what you hear while
        scrubbing matches what gets written to the output file."""
        n = len(track_settings)
        parts = [
            f"[{source}]{filters + ',' if filters else ''}volume={vol:.4f}[v{i}]"
            for i, (source, filters, vol) in enumerate(track_settings)
        ]
        if n == 1:
            return parts[0].replace("[v0]", "[aout]")
        mix_inputs = "".join(f"[v{i}]" for i in range(n))
        return (
            ";".join(parts) + ";" + mix_inputs
            + f"amix=inputs={n}:duration=longest:dropout_transition=0:normalize=0[aout]"
        )

    def _audio_start_ffplay(self, start_sec):
        self._audio_stop_ffplay()
        enabled = [t for t in self.audio_tracks if t["enabled_var"].get()]
        if not enabled or not self.audio_input_path:
            return
        try:
            ffmpeg = find_exe("ffmpeg")
            ffplay = find_exe("ffplay")
        except Exception:
            return  # ffmpeg/ffplay not on PATH -- visual scrubbing still works, just silently
        extra_inputs, track_settings = self._audio_mix_sources(
            [(t, t["volume_slider"].get() if t["volume_slider"] else 1.0) for t in enabled])
        filter_complex = self._audio_build_filter_complex(track_settings)
        # Some ffplay builds don't register -filter_complex at all (it fails
        # with "Option not found" and exits immediately, silently, since
        # stderr is suppressed) -- ffmpeg reliably supports it, so the mix is
        # done there and the resulting PCM is piped straight into ffplay just
        # for playback, rather than asking ffplay to do the mixing itself.
        ffmpeg_cmd = [
            ffmpeg, "-v", "quiet", "-ss", seconds_to_hms(start_sec),
            "-i", self.audio_input_path,
        ]
        for wav in extra_inputs:
            ffmpeg_cmd += ["-ss", seconds_to_hms(start_sec), "-i", wav]
        ffmpeg_cmd += [
            "-filter_complex", filter_complex, "-map", "[aout]",
            "-f", "wav", "-",
        ]
        ffplay_cmd = [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", "-i", "-"]
        try:
            mixer = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, **SUBPROCESS_KWARGS)
            player = subprocess.Popen(ffplay_cmd, stdin=mixer.stdout, **SUBPROCESS_KWARGS)
            mixer.stdout.close()  # let `mixer` get SIGPIPE once `player` exits/is killed
            self.audio_ffmpeg_pipe_proc = mixer
            self.audio_ffplay_proc = player
        except Exception:
            self.audio_ffmpeg_pipe_proc = None
            self.audio_ffplay_proc = None

    def _audio_stop_ffplay(self):
        for attr in ("audio_ffplay_proc", "audio_ffmpeg_pipe_proc"):
            proc = getattr(self, attr, None)
            if proc is not None:
                setattr(self, attr, None)
                try:
                    proc.terminate()
                except Exception:
                    pass

    def _audio_maybe_restart_playback(self):
        # Debounced so dragging a volume slider or toggling several tracks
        # doesn't spawn a burst of ffplay processes (or mpv audio restarts)
        # -- only the settle point after motion pauses triggers a restart,
        # same pattern as the frame preview debounce elsewhere in this app.
        if self._audio_player_on:
            if self._audio_restart_after_id is not None:
                self.after_cancel(self._audio_restart_after_id)
            self._audio_restart_after_id = self.after(100, self._audio_mix_settled)
            return
        if not self.audio_playing:
            return
        if self._audio_restart_after_id is not None:
            self.after_cancel(self._audio_restart_after_id)
        self._audio_restart_after_id = self.after(150, self._audio_restart_playback_now)

    def _audio_mix_settled(self):
        self._audio_restart_after_id = None
        self._audio_apply_mix()

    def _audio_restart_playback_now(self):
        self._audio_restart_after_id = None
        if self.audio_playing:
            self._audio_start_ffplay(self.audio_time_sec)

    def _audio_stop_playback_state(self):
        self._audio_pause()
        self.audio_time_sec = 0.0

    def _audio_update_preview(self, at_seconds):
        if not self.audio_input_path or not PIL_AVAILABLE or self._audio_player_on:
            return
        if self.audio_after_id is not None:
            self.after_cancel(self.audio_after_id)
        self.audio_after_id = self.after(
            80, lambda: self._audio_start_preview_extraction(at_seconds)
        )

    def _audio_start_preview_extraction(self, at_seconds):
        self.audio_after_id = None
        self.audio_request_id += 1
        request_id = self.audio_request_id
        threading.Thread(
            target=self._audio_extract_frame, args=(at_seconds, request_id), daemon=True
        ).start()

    def _audio_extract_frame(self, at_seconds, request_id):
        out_path = os.path.join(
            tempfile.gettempdir(), f"_lossless_cutter_audio_preview_{request_id}.jpg"
        )
        try:
            ffmpeg = find_exe("ffmpeg")
            cmd = [ffmpeg, "-y", "-ss", seconds_to_hms(at_seconds), "-i", self.audio_input_path,
                   "-frames:v", "1", "-q:v", "3", out_path]
            subprocess.run(cmd, capture_output=True, check=True, **SUBPROCESS_KWARGS)
            img = Image.open(out_path).convert("RGB")
            img.load()
            self.after(0, lambda: self._audio_set_frame_if_current(img, request_id))
        except Exception:
            pass
        finally:
            try:
                os.remove(out_path)
            except OSError:
                pass

    def _audio_set_frame_if_current(self, img, request_id):
        if request_id != self.audio_request_id:
            return
        self.audio_frame_img = img
        self._redraw_audio_canvas()

    # ---- auto balance -----------------------------------------------------
    def _audio_auto_balance_clicked(self):
        if not self.audio_input_path or not self.audio_tracks:
            return
        self.audio_balance_btn.config(state="disabled")
        levels = {t["a_index"]: t["denoise_var"].get() for t in self.audio_tracks}
        monos = {t["a_index"]: t["mono_var"].get() for t in self.audio_tracks}

        def start():
            # Each track is measured as it will sound: with its noise
            # reduction and its Mono.
            plan = self._audio_balance_plan(levels, monos)
            self.status_lbl.config(text="Analyzing track loudness...")
            threading.Thread(target=self._audio_run_auto_balance, args=(plan,), daemon=True).start()

        self._dfn_when_ready(
            self._dfn_keys(levels), start,
            lambda error: self._audio_auto_balance_done({}, False, error))

    def _audio_balance_plan(self, levels=None, monos=None):
        """(a_index, ffmpeg input arguments, filters) to measure each track
        by: its cleaned WAV, or its own stream with its live filter; then
        its Mono."""
        plan = []
        for track in self.audio_tracks:
            level = levels.get(track["a_index"]) if levels else None
            wav, chain = self._audio_denoise_source(track, level)
            fold = self._audio_track_fold(track, monos.get(track["a_index"]) if monos else None)
            if wav is not None:
                input_args, chain = ["-i", wav], ""
            else:
                input_args = ["-i", self.audio_input_path, "-map", f"0:a:{track['a_index']}"]
            plan.append((track["a_index"], input_args, ",".join(f for f in (chain, fold) if f)))
        return plan

    def _audio_run_auto_balance(self, plan=None):
        try:
            if plan is None:
                plan = self._audio_balance_plan()
            ffmpeg = find_exe("ffmpeg")
            null_target = "NUL" if os.name == "nt" else "/dev/null"
            mean_re = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")
            means = {}
            n_tracks = len(plan)
            report = self._progress_reporter("Analyzing track loudness...", "audio_balance")
            for i, (a_index, input_args, chain) in enumerate(plan):
                cmd = [
                    ffmpeg, *input_args,
                    "-af", f"{chain},volumedetect" if chain else "volumedetect",
                    "-f", "null", null_target,
                ]
                proc = run_ffmpeg(
                    cmd, self.audio_duration, sub_progress(report, i / n_tracks, (i + 1) / n_tracks)
                )
                m = mean_re.search(proc.stderr)
                means[a_index] = float(m.group(1)) if m else None

            # A (nearly) silent track has nothing to match: it keeps 100%
            # and doesn't pull the others' target down.
            means = {a: v if v is not None and v > -60.0 else None for a, v in means.items()}
            known = [v for v in means.values() if v is not None]
            if not known:
                raise RuntimeError("Could not measure loudness for any track.")
            # Target the average loudness across tracks -- quiet tracks get
            # boosted, loud ones get pulled down, so nothing dominates.
            target_db = sum(known) / len(known)

            gains_db = {a: max(-24.0, min(24.0, target_db - v))
                        for a, v in means.items() if v is not None}
            # Past the volume slider's top, every track is turned down the
            # same amount instead, so they still come out equally loud.
            over = max(gains_db.values()) - 20 * math.log10(self.AUDIO_MAX_GAIN)
            gains = {a: 1.0 for a in means}
            for a, db in gains_db.items():
                gains[a] = 10 ** ((db - max(0.0, over)) / 20)

            self.after(0, lambda: self._audio_auto_balance_done(gains, True, None))
        except Exception as exc:
            self.after(0, lambda e=str(exc): self._audio_auto_balance_done({}, False, e))

    def _audio_auto_balance_done(self, gains, success, error):
        self._end_progress("audio_balance")
        self.audio_balance_btn.config(state="normal")
        if not success:
            self.status_lbl.config(text="Auto balance failed")
            messagebox.showerror("Auto balance failed", error)
            return
        for track in self.audio_tracks:
            gain = gains.get(track["a_index"])
            if gain is None or track["volume_slider"] is None:
                continue
            track["volume_slider"].set(gain)
            if track["volume_lbl"] is not None:
                track["volume_lbl"].config(text=f"{int(round(gain * 100))}%")
            self._audio_redraw_waveform(track)
        self._audio_maybe_restart_playback()   # play the new levels
        self.status_lbl.config(text="Auto balance applied — review levels, then Finalize")

    # ---- finalize (mix + save) ---------------------------------------------
    def _audio_finalize_clicked(self):
        if not self.audio_input_path:
            return
        enabled = [t for t in self.audio_tracks if t["enabled_var"].get()]
        if not enabled:
            messagebox.showerror(
                "No tracks selected",
                "Enable at least one audio track to include in the output."
            )
            return

        source_dir = os.path.dirname(os.path.abspath(self.audio_input_path))
        base_name = os.path.splitext(os.path.basename(self.audio_input_path))[0]
        ext = os.path.splitext(self.audio_input_path)[1] or ".mp4"
        default_name = f"{base_name}.AUDIOMIX{ext}"

        from_dialog = not self.save_to_source_var.get()
        if not from_dialog:
            out_path = os.path.join(source_dir, default_name)
            if os.path.abspath(out_path) == os.path.abspath(self.audio_input_path):
                out_path = os.path.join(source_dir, base_name + ".AUDIOMIX2" + ext)
        else:
            out_path = filedialog.asksaveasfilename(
                title="Save mixed video as", initialdir=source_dir,
                initialfile=default_name, defaultextension=ext,
                filetypes=[("Same format", f"*{ext}"), ("All files", "*.*")],
            )
            if not out_path:
                return
        out_path = self._check_output_path(
            out_path, [self.audio_input_path], prompt_overwrite=not from_dialog
        )
        if not out_path:
            return

        mix = [(t, t["volume_slider"].get() if t["volume_slider"] else 1.0, t["denoise_var"].get(),
                t["mono_var"].get()) for t in enabled]

        def start():
            extra_inputs, track_settings = self._audio_mix_sources(mix)
            self.status_lbl.config(text="Mixing audio tracks... 0%")
            threading.Thread(
                target=self._audio_run_finalize, args=(out_path, track_settings, extra_inputs),
                daemon=True
            ).start()

        self.audio_finalize_btn.config(state="disabled")
        keys = self._dfn_keys({t["a_index"]: level for t, _vol, level, _mono in mix})
        if any(key not in self._dfn_done for key in keys):
            self.status_lbl.config(text="Saving once the noise reduction is done...")
        self._dfn_when_ready(keys, start, lambda error: self._audio_finalize_done(error, False))

    def _audio_mix_sources(self, mix):
        """mix: (track, volume), (track, volume, noise reduction level) or
        (track, volume, level, mono) for each track to mix. Returns (extra
        input files, track settings for _audio_build_filter_complex): a
        track's own stream from input 0, with its live filter (Light), or
        its cleaned WAV (Medium / Extreme) as input 1, 2..., as it is until
        that's there; then its Mono."""
        extra_inputs, settings = [], []
        for track, vol, *how in mix:
            level, mono = (how + [None, None])[:2]
            wav, chain = self._audio_denoise_source(track, level)
            if chain:
                chain += "," + RNNOISE_TRIM   # Light, back in place
            if wav is not None:
                extra_inputs.append(wav)
                source, chain = f"{len(extra_inputs)}:a:0", ""
            else:
                source = f"0:a:{track['a_index']}"
            fold = self._audio_track_fold(track, mono)
            settings.append((source, ",".join(f for f in (chain, fold) if f), vol))
        return extra_inputs, settings

    def _audio_run_finalize(self, out_path, track_settings, extra_inputs=()):
        try:
            ffmpeg = find_exe("ffmpeg")
            filter_complex = self._audio_build_filter_complex(track_settings)
            cmd = [ffmpeg, "-y", "-i", self.audio_input_path]
            for wav in extra_inputs:
                cmd += ["-i", wav]
            cmd += [
                "-filter_complex", filter_complex,
                "-map", "0:v:0", "-map", "[aout]",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "256k",
                "-movflags", "+faststart",
                out_path,
            ]
            proc = run_ffmpeg(
                cmd, self.audio_duration, self._progress_reporter("Mixing audio tracks...", "audio_mix")
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr[-3000:])

            self.after(0, lambda p=out_path: self._audio_finalize_done(p, True))
        except Exception as exc:
            self.after(0, lambda e=str(exc): self._audio_finalize_done(e, False))

    def _audio_finalize_done(self, info, success):
        self._end_progress("audio_mix")
        self.audio_finalize_btn.config(state="normal")
        if success:
            self.status_lbl.config(text=f"Saved: {info}")
            self._saved_ask_open(f"Mixed video saved to:\n{info}", info, 4, "mixed video")
        else:
            self.status_lbl.config(text="Audio mix failed")
            messagebox.showerror("ffmpeg error", "The audio mix failed.\n\n" + info)

    # ---- noise reduction ------------------------------------------------------
    def _audio_denoise_files(self, refresh=False):
        """RNNOISE_MODEL / DEEP_FILTER_EXE -> where it is, or None; looked
        up once (the mix asks on every player update), again on refresh."""
        if self._denoise_files is None or refresh:
            self._denoise_files = {name: find_denoise_file(name)
                                   for name in (RNNOISE_MODEL, DEEP_FILTER_EXE)}
        return self._denoise_files

    def _audio_denoise_source(self, track, level=None):
        """(cleaned WAV or None, filter chain) for playing or saving track
        at its noise reduction level (or the one given): the RNNoise filter
        for Light, the DeepFilterNet WAV once it's there for Medium /
        Extreme; (None, "") for the track as it is."""
        if level is None:
            level = track["denoise_var"].get()
        if level == "light":
            model = self._audio_denoise_files()[RNNOISE_MODEL]
            return None, f"arnndn=m={lavfi_value(model)}:mix={RNNOISE_MIX}" if model else ""
        if level in DFN_ATTEN_LIMIT_DB:
            return self._dfn_done.get((track["a_index"], level)), ""
        return None, ""

    @staticmethod
    def _audio_track_fold(track, mono=None):
        """The filter for track's Mono (or the one given): TRACK_MONO, or
        "" when off or the track has one channel."""
        if mono is None:
            mono = track["mono_var"].get()
        return TRACK_MONO if mono and track["channels"] >= 2 else ""

    def _audio_post_denoise_menu(self, track):
        files = self._audio_denoise_files(refresh=True)
        menu = track["denoise_menu"]
        menu.delete(0, "end")
        for key, name in DENOISE_LEVELS:
            missing = (RNNOISE_MODEL if key == "light" else
                       DEEP_FILTER_EXE if key in DFN_ATTEN_LIMIT_DB else None)
            if missing and files[missing]:
                missing = None
            menu.add_radiobutton(
                label=name + (f" (needs {missing})" if missing else ""),
                variable=track["denoise_var"], value=key, state="disabled" if missing else "normal",
                command=lambda t=track: self._audio_denoise_changed(t))
        menu.configure(DARK_MENU if self._is_dark() else self._menu_light_colors)
        b = track["denoise_btn"]
        x, y = b.winfo_rootx(), b.winfo_rooty() + b.winfo_height()
        margin = DARK_MENU["background"] if self._is_dark() else None
        post_with_menu_margin(lambda: menu.tk_popup(x, y), margin)

    def _audio_show_denoise_level(self, track):
        level = track["denoise_var"].get()
        name = dict(DENOISE_LEVELS)[level]
        track["denoise_btn"].config(text=("Noise reduction" if level == "off" else name) + " ▾")

    def _audio_denoise_changed(self, track):
        level = track["denoise_var"].get()
        self._dfn_failed.pop((track["a_index"], level), None)   # picked again: try again
        self._audio_show_denoise_level(track)
        self._dfn_schedule()
        self._audio_maybe_restart_playback()

    def _audio_show_denoise_status(self, track):
        """Writes how track's DeepFilterNet level is coming along over its
        waveform: waiting, working (with the estimated percentage), or
        failed; nothing once it's done, or for the other levels."""
        c = track.get("waveform_canvas")
        if c is None or not c.winfo_exists():
            return
        c.delete("denoise")
        level = track["denoise_var"].get()
        key = (track["a_index"], level)
        if level not in DFN_ATTEN_LIMIT_DB or key in self._dfn_done:
            return
        name = dict(DENOISE_LEVELS)[level]
        job = self._dfn_job
        if key in self._dfn_failed:
            reason = self._dfn_failed[key].strip().splitlines() or ["?"]
            text = f"{name} failed: {reason[-1][:120]}"
        elif job is not None and job["key"] == key:
            text = f"{name}: working... {int(job['frac'] * 100)}%   (plays unfiltered until it's done)"
        else:
            text = f"{name}: waiting its turn   (plays unfiltered until it's done)"
        item = c.create_text(px(8), px(6), text=text, anchor="nw", fill="#e8e8e8",
                             font=("Segoe UI", 9), tags=("denoise",))
        x0, y0, x1, y1 = c.bbox(item)
        c.create_rectangle(x0 - px(4), y0 - px(2), x1 + px(4), y1 + px(2), fill="#000000",
                           outline="#444444", tags=("denoise",))
        c.tag_raise(item)

    def _audio_show_denoise_states(self):
        for track in self.audio_tracks:
            self._audio_show_denoise_status(track)

    # DeepFilterNet jobs: each one cleans one track at one level to a WAV
    # in a temp folder, one at a time (deep-filter takes ~65 MB of memory
    # per minute of stereo, and uses one core).
    def _dfn_keys(self, levels):
        """The (a_index, level) jobs that levels ({a_index: level}) need."""
        return {(a, level) for a, level in levels.items() if level in DFN_ATTEN_LIMIT_DB}

    def _dfn_wanted(self):
        keys = self._dfn_keys({t["a_index"]: t["denoise_var"].get() for t in self.audio_tracks})
        for waiter_keys, _callback, _on_fail in self._dfn_waiters:
            keys |= waiter_keys
        return keys

    def _dfn_when_ready(self, keys, callback, on_fail):
        """Calls callback() once the jobs keys are all done (right away if
        they are), or on_fail(why) if one of them fails."""
        self._dfn_waiters.append((set(keys), callback, on_fail))
        self._dfn_schedule()

    def _dfn_check_waiters(self):
        waiters, self._dfn_waiters = self._dfn_waiters, []
        for waiter in waiters:
            keys, callback, on_fail = waiter
            failed = [k for k in keys if k in self._dfn_failed]
            if failed:
                a_index, level = failed[0]
                on_fail(f"Noise reduction ({dict(DENOISE_LEVELS)[level]}) failed on "
                        f"track {a_index + 1}:\n\n{self._dfn_failed[failed[0]]}")
            elif all(k in self._dfn_done for k in keys):
                callback()
            else:
                self._dfn_waiters.append(waiter)

    def _dfn_schedule(self):
        """Brings the jobs in line with what the tracks (and a waiting
        Finalize or Auto Balance) ask for: drops the waiting jobs no longer
        wanted, stops the running one if it isn't, and queues the new ones."""
        self._dfn_check_waiters()
        wanted = self._dfn_wanted()
        self._dfn_queue = [k for k in self._dfn_queue if k in wanted]
        if self._dfn_job is not None and self._dfn_job["key"] not in wanted:
            self._dfn_stop_job()
        running = self._dfn_job["key"] if self._dfn_job is not None else None
        for key in sorted(wanted):
            if key not in self._dfn_done and key not in self._dfn_failed \
                    and key not in self._dfn_queue and key != running:
                self._dfn_queue.append(key)
        self._dfn_start_next()
        self._audio_show_denoise_states()

    def _dfn_start_next(self):
        if self._dfn_job is not None or not self._dfn_queue or not self.audio_input_path:
            return
        key = self._dfn_queue.pop(0)
        if self._dfn_video_dir is None:
            if self._dfn_session_dir is None:
                self._dfn_session_dir = self._dfn_make_session_dir()
            self._dfn_video_dir = tempfile.mkdtemp(dir=self._dfn_session_dir)
        channels = next((t["channels"] for t in self.audio_tracks if t["a_index"] == key[0]), 2)
        self._dfn_jobs_started += 1
        job = {
            "key": key, "path": self.audio_input_path, "dir": self._dfn_video_dir,
            "name": f"track{key[0] + 1}_{key[1]}_{self._dfn_jobs_started}",
            "estimate": max(1.0, self.audio_duration * (DFN_TIME_BASE + DFN_TIME_PER_CHANNEL * channels)),
            "started": time.monotonic(), "frac": 0.0,
            "lock": threading.Lock(), "proc": None, "stopped": False,
        }
        self._dfn_job = job
        threading.Thread(target=self._dfn_run, args=(job,), daemon=True).start()
        self._dfn_tick()

    @staticmethod
    def _dfn_make_session_dir():
        """A new temp folder for this run's WAVs. Folders a run left behind
        (it crashed, say) go once they're a day old."""
        root = os.path.join(tempfile.gettempdir(), "BBVideoCutter_denoise")
        os.makedirs(root, exist_ok=True)
        for name in os.listdir(root):
            old = os.path.join(root, name)
            try:
                if time.time() - os.path.getmtime(old) > 86400:
                    shutil.rmtree(old, ignore_errors=True)
            except OSError:
                pass
        return tempfile.mkdtemp(dir=root)

    def _dfn_run(self, job):
        """Worker thread: the track to a 48 kHz WAV, padded with silence to
        start where the video starts (so it lines up when played or mixed
        in), then deep-filter on it. Hands the result to _dfn_job_finished."""
        a_index, level = job["key"]
        src = os.path.join(job["dir"], job["name"] + ".wav")
        out_dir = os.path.join(job["dir"], level)
        try:
            deep_filter = find_denoise_file(DEEP_FILTER_EXE)
            if not deep_filter:
                raise RuntimeError(f"{DEEP_FILTER_EXE} was not found.")
            self._dfn_step(job, [
                find_exe("ffmpeg"), "-y", "-v", "error", "-i", job["path"], "-map", f"0:a:{a_index}",
                "-af", "aresample=48000:async=1:first_pts=0", "-c:a", "pcm_s16le", src])
            os.makedirs(out_dir, exist_ok=True)
            cmd = [deep_filter, "-D", "-o", out_dir]   # -D: no delay added at the start
            if DFN_ATTEN_LIMIT_DB[level] is not None:
                cmd += ["-a", str(DFN_ATTEN_LIMIT_DB[level])]
            self._dfn_step(job, cmd + [src])
            out = os.path.join(out_dir, os.path.basename(src))
            if not os.path.isfile(out):
                raise RuntimeError(f"{DEEP_FILTER_EXE} didn't write its output.")
            ok, result = True, out
        except Exception as exc:
            ok, result = False, str(exc)
        try:
            os.remove(src)
        except OSError:
            pass
        self.after(0, lambda: self._dfn_job_finished(job, ok, result))

    @staticmethod
    def _dfn_step(job, cmd):
        """Runs one program of job, unless the job was stopped."""
        with job["lock"]:
            if job["stopped"]:
                raise RuntimeError("stopped")
            proc = job["proc"] = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                **SUBPROCESS_KWARGS)
        err = proc.communicate()[1].decode("utf-8", "replace")
        if proc.returncode != 0:
            raise RuntimeError(err.strip()[-2000:] or f"{os.path.basename(cmd[0])} failed "
                               f"(exit code {proc.returncode}).")

    def _dfn_tick(self):
        """While a job runs: its estimated progress on the status bar and
        over its track's waveform (deep-filter reports none of its own)."""
        self._dfn_tick_id = None
        job = self._dfn_job
        if job is None:
            return
        elapsed = time.monotonic() - job["started"]
        job["frac"] = min(0.95, elapsed / (job["estimate"] * self._dfn_rate))
        a_index, level = job["key"]
        self._show_progress("denoise", job["frac"],
                            f"Noise reduction ({dict(DENOISE_LEVELS)[level]}), track {a_index + 1}... "
                            f"{int(job['frac'] * 100)}%")
        for track in self.audio_tracks:
            if track["a_index"] == a_index:
                self._audio_show_denoise_status(track)
        self._dfn_tick_id = self.after(250, self._dfn_tick)

    def _dfn_job_finished(self, job, ok, result):
        if job is not self._dfn_job:
            return   # stopped: not wanted any more, or another video was loaded
        self._dfn_job = None
        self._end_progress("denoise")
        a_index, level = job["key"]
        if ok:
            self._dfn_done[job["key"]] = result
            self._dfn_rate = max(0.2, min(20.0, (time.monotonic() - job["started"]) / job["estimate"]))
            self.status_lbl.config(text=f"Noise reduction ({dict(DENOISE_LEVELS)[level]}) "
                                        f"done on track {a_index + 1}")
        else:
            self._dfn_failed[job["key"]] = result
            self.status_lbl.config(text=f"Noise reduction failed on track {a_index + 1}")
        self._dfn_schedule()
        if ok:
            self._audio_maybe_restart_playback()   # play the cleaned track

    def _dfn_stop_job(self, wait=False):
        """Stops the running job (kills its program); wait: until it's gone,
        so its files can be deleted."""
        job, self._dfn_job = self._dfn_job, None
        if job is None:
            return
        self._end_progress("denoise")
        with job["lock"]:
            job["stopped"] = True
            proc = job["proc"]
        if proc is not None:
            try:
                proc.kill()
                if wait:
                    proc.wait(timeout=3)
            except Exception:
                pass

    def _dfn_reset(self):
        """Another video (or none): stops the jobs and deletes the last
        video's WAVs."""
        self._dfn_stop_job()
        self._dfn_queue = []
        self._dfn_waiters = []
        self._dfn_done = {}
        self._dfn_failed = {}
        self._audio_added_wavs = set()
        old, self._dfn_video_dir = self._dfn_video_dir, None
        if old:
            # In the background: the player may still hold a WAV open for
            # a moment, until it has loaded the next video.
            def remove():
                for _attempt in range(20):
                    shutil.rmtree(old, ignore_errors=True)
                    if not os.path.exists(old):
                        return
                    time.sleep(0.5)

            threading.Thread(target=remove, daemon=True).start()

    def _dfn_shutdown(self):
        """App closing: stops the job and deletes this run's WAVs."""
        self._dfn_stop_job(wait=True)
        if self._dfn_session_dir:
            shutil.rmtree(self._dfn_session_dir, ignore_errors=True)

    def _on_drop_audio(self, event):
        self._ensure_tab_built(4)
        paths = parse_dropped_paths(event.data)
        video_paths = [p for p in paths if p.lower().endswith(VIDEO_EXTS)]
        if video_paths:
            self._load_video_for_audio(video_paths[0])
        elif paths:
            messagebox.showwarning(
                "Unsupported file",
                "That doesn't look like a supported video file."
            )

    # ====================================================================
    # Status bar
    # ====================================================================
    def _build_status_bar(self):
        bar = tk.Frame(self, bg="#e6e6e6")
        # Packed ahead of the notebook so it claims its row first. Otherwise,
        # once a tall tab (Crop, Audio) has been built, the notebook asks for
        # more height than the window has and pack drops the status bar.
        bar.pack(fill="x", side="bottom", before=self.notebook)
        self.status_lbl = tk.Label(
            bar, text="Ready", bg="#e6e6e6", anchor="w", font=("Segoe UI", 8)
        )
        self.status_lbl.pack(side="left", padx=4, pady=0)
        # Shown (packed on the right) only while a job is running.
        self.progress_bar = ttk.Progressbar(bar, mode="determinate", maximum=1000, length=px(180))
        self.progress_eta_lbl = tk.Label(
            bar, text="", bg="#e6e6e6", fg="#555555", anchor="e", font=("Segoe UI", 8)
        )

    # ====================================================================
    # Drag & drop
    # ====================================================================
    def _enable_drag_and_drop(self):
        # Cut tab is built eagerly (it's the tab visible at launch), so wire
        # its inner widgets up in full right away.
        self.cut_tab.drop_target_register(DND_FILES)
        self.cut_tab.dnd_bind("<<Drop>>", self._on_drop_cut)
        self.preview_canvas.drop_target_register(DND_FILES)
        self.preview_canvas.dnd_bind("<<Drop>>", self._on_drop_cut)
        if self.cut_player is not None:
            self.cut_player.frame.drop_target_register(DND_FILES)
            self.cut_player.frame.dnd_bind("<<Drop>>", self._on_drop_cut)

        # The other tabs are built lazily, only on first visit -- but their
        # empty shell Frame already exists, so it can still accept a drop
        # before that: each drop handler builds its tab on demand (via
        # _ensure_tab_built) before touching any tab-specific widget. Their
        # INNER widgets (e.g. the Combine list canvas) only exist once the
        # tab is built, so those extra drop targets are registered at the
        # end of each tab's own _build_..._tab() instead of here.
        self.combine_tab.drop_target_register(DND_FILES)
        self.combine_tab.dnd_bind("<<Drop>>", self._on_drop_combine)

        self.bulk_tab.drop_target_register(DND_FILES)
        self.bulk_tab.dnd_bind("<<Drop>>", self._on_drop_bulk)

        self.resize_tab.drop_target_register(DND_FILES)
        self.resize_tab.dnd_bind("<<Drop>>", self._on_drop_resize)

        self.audio_tab.drop_target_register(DND_FILES)
        self.audio_tab.dnd_bind("<<Drop>>", self._on_drop_audio)

    def _on_drop_cut(self, event):
        paths = parse_dropped_paths(event.data)
        video_paths = [p for p in paths if p.lower().endswith(VIDEO_EXTS)]
        if video_paths:
            self._load_video_for_cut(video_paths[0])
        elif paths:
            messagebox.showwarning("Unsupported file",
                                    "That doesn't look like a supported video file.")

    def _on_drop_combine(self, event):
        self._ensure_tab_built(1)
        paths = parse_dropped_paths(event.data)
        video_paths = [p for p in paths if p.lower().endswith(VIDEO_EXTS)]
        if video_paths:
            self.combine_files.extend(video_paths)
            self._rebuild_combine_rows()
        elif paths:
            messagebox.showwarning("Unsupported file",
                                    "That doesn't look like a supported video file.")

    def _on_drop_bulk(self, event):
        self._ensure_tab_built(2)
        paths = parse_dropped_paths(event.data)
        video_paths = [p for p in paths if p.lower().endswith(VIDEO_EXTS)]
        if video_paths:
            self._bulk_add_paths(video_paths)
        elif paths:
            messagebox.showwarning("Unsupported file",
                                    "That doesn't look like a supported video file.")


# What differs on Linux lives in bbvc_linux.py, which swaps it in here. The
# Windows build leaves that file out (--exclude-module bbvc_linux).
if sys.platform.startswith("linux"):
    import bbvc_linux
    bbvc_linux.install(sys.modules[__name__])


if __name__ == "__main__":
    app = LosslessVideoApp()
    app.mainloop()
