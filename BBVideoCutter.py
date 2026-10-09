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
View > Dark mode switches the whole app (title bar included on Windows)
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
        # The menu bar matches the title bar above it, so the two read as
        # one header: white like the Windows 10 caption in light mode...
        "menubar_bg": "#ffffff",
        "menubar_fg": "#000000",
        "menubar_hover": "#e5f3ff",
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
        # ...and the panel color, next to the dark caption, in dark mode.
        "menubar_bg": DARK["panel"],
        "menubar_fg": DARK["text"],
        "menubar_hover": DARK["button"],
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

# Light chrome backgrounds. A widget whose background is one of these is
# part of the chrome and gets themed; any other background (black, #1e1e1e,
# #2a2a2a...) is a content area and is left alone, text colors included.
_CHROME_BG = {
    "systembuttonface": "panel", "#f0f0f0": "panel", "#d9d9d9": "panel",
    "systemwindow": "field",
    "#e6e6e6": "status",
    "#cfe2ff": None,  # the Crop tab's active ratio button, see _dark_bg
}
_BG_OPTIONS = ("background", "activebackground", "highlightbackground",
               "selectcolor", "disabledbackground", "readonlybackground")
_FG_OPTIONS = {
    "systembuttontext": "text", "systemwindowtext": "text",
    "black": "text", "#000000": "text",
    "systemdisabledtext": "text_disabled", "#a3a3a3": "text_disabled",
    "#555555": "text_dim", "#777777": "text_hint",
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

    on_start(process) gets the ffmpeg process, so it can be killed; a
    killed render raises."""
    align = ""
    if span is not None:
        align = (f"aresample=async=1:first_pts=0,"
                 f"apad=whole_dur={span:.6f},atrim=end={span:.6f},")
    # draw=full ignores amplitude when deciding pixel brightness (only the
    # height reflects amplitude) -- with draw=scale (the default) quiet
    # passages fade to near-invisible, which is what made the waveform so
    # hard to see. scale=cbrt compresses the dynamic range so quiet parts
    # still show visible height, not just the loudest peaks.
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
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)

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
        self.create_line(x0, y, x1, y, fill=self._track_color, width=px(5), capstyle="round")
        hx = self._x_for(self._value)
        if hx > x0:
            self.create_line(x0, y, hx, y, fill=self._fill_color, width=px(5), capstyle="round")
        for v in self._marks:
            mx = self._x_for(v)
            self.create_line(mx, y - px(7), mx, y + px(7), fill=self.MARK_COLOR,
                             width=max(1, px(2)), tags=("mark",))
        r = px(9) if self._dragging else px(7)
        self.create_oval(hx - r, y - r, hx + r, y + r,
                          fill=self._handle_color, outline="#ffffff", width=px(2))

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
            try:
                player.terminate()
            except Exception:
                pass

    def _poll(self):
        self._poll_id = self.frame.after(33, self._poll)
        self._disable_mpv_windows()
        seen = tuple(self.state.get(name) for name in self.OBSERVED + ("ready", "failed"))
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
        return [t for t in self.state.get("track-list") or [] if t.get("type") == "audio"]

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
        self.clear_loop()
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
            self.mpv.pause = True
            self._async("stop")

    def set_paused(self, paused):
        if self.mpv is None:
            return
        if not paused and self.at_end and not self.looping:
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
            self.mpv.aid = track_id

    def set_video_crop(self, spec):
        """Shows only part of the video: spec "WxH+X+Y" in the video's own
        pixels, or "" for all of it."""
        if self.mpv is not None:
            self.mpv.video_crop = spec

    def show_overlay(self, ass_events, width, height):
        """Draws ASS events (one per line) over the video, in a coordinate
        space of width x height spanning the player's frame."""
        if self.mpv is not None:
            self._async("osd-overlay", 1, "ass-events", ass_events, width, height, 0, "no", "no")

    def hide_overlay(self):
        if self.mpv is not None:
            self._async("osd-overlay", 1, "none", "", 0, 0, 0, "no", "no")

    # A-B loop: playback jumps back to a on reaching b.
    looping = False

    def set_loop(self, a, b):
        if self.mpv is None:
            return
        self.looping = True
        self.mpv.ab_loop_a = a
        self.mpv.ab_loop_b = b

    def clear_loop(self):
        self.looping = False
        if self.mpv is not None:
            self.mpv.ab_loop_a = "no"
            self.mpv.ab_loop_b = "no"


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
        # Pixel sizes of the Combine rows, scaled like everything else.
        self.ROW_HEIGHT = px(self.ROW_HEIGHT)
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
        self.minsize(min(px(720), win_w), min(px(560 + 20), win_h))   # <-- real minimum window size, enforced

        self.input_path = None
        self.duration = 0.0
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
        self.cut_ranges = []             # ranges marked for Bulk Cut: (start, end), by start
        self._cut_ranges_by_file = {}    # path -> its marked ranges, kept when switching videos

        # Resize/Crop tab state
        self.resize_input_path = None
        self.resize_duration = 0.0
        self.resize_src_w = 0
        self.resize_src_h = 0
        self.resize_frame_img = None
        self.resize_frame_photo = None
        self.resize_crop = None          # x0, y0, x1, y1 in source pixels
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
        self.resize_aspect_key = None     # which ratio button is active: "16:9", "original", "freeform"...
        self.resize_aspect_buttons = {}
        self._resize_bg_image_id = None   # cached canvas image item (perf)
        self._resize_bg_cache_key = None  # (frame id, dw, dh) the cached photo was built for
        # With libmpv, the video plays under the crop box: a player placed
        # over the picture's area of the canvas (_resize_place_player),
        # which draws the box too (_resize_draw_player_overlay).
        self.resize_player = None
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
        self.audio_tracks = []            # list of dicts, see _load_video_for_audio

        self.combine_files = []          # list of paths queued to join
        self.combine_preview = None      # ClipPreview of the joined videos, made on first use
        self._combine_preview_sync_id = None

        self.bulk_items = []             # list of dicts: path, ranges, status, iid
        self._bulk_lock = threading.Lock()
        self._bulk_running = False
        self.bulk_preview = None         # ClipPreview of a row's ranges, made on first use

        self.warn_overwrite_var = tk.BooleanVar(value=True)
        self._progress_jobs = {}         # running job name -> time.monotonic() it started

        self.settings = load_settings()
        self.dark_mode_var = tk.BooleanVar(value=bool(self.settings.get("dark_mode", False)))
        self._ttk_light_theme = ttk.Style(self).theme_use()
        self._set_treeview_row_height(ttk.Style(self))

        self._build_menu()

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.cut_tab = tk.Frame(self.notebook, bg="#f0f0f0")
        self.combine_tab = tk.Frame(self.notebook, bg="#f0f0f0")
        self.bulk_tab = tk.Frame(self.notebook, bg="#f0f0f0")
        self.resize_tab = tk.Frame(self.notebook, bg="#f0f0f0")
        self.audio_tab = tk.Frame(self.notebook, bg="#f0f0f0")
        self.notebook.add(self.cut_tab, text="Cut Clip")
        self.notebook.add(self.combine_tab, text="Combine Videos")
        self.notebook.add(self.bulk_tab, text="Bulk Cut")
        self.notebook.add(self.resize_tab, text="Crop")
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

    def _on_notebook_tab_changed(self, event=None):
        idx = self.notebook.index("current")
        # No sound from a tab out of sight.
        if idx != 0 and self._cut_player_on:
            self.cut_player.set_paused(True)
        for tab, player in self._tab_players():
            if tab != idx:
                player.set_paused(True)
        self._ensure_tab_built(idx)

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
        file_menu.add_command(label="Open Video (Cut tab)...", command=self.open_file_for_cut)
        file_menu.add_command(label="Open Video (Crop tab)...", command=self.open_file_for_resize)
        file_menu.add_command(label="Open Video (Audio tab)...", command=self.open_file_for_audio)
        file_menu.add_command(label="Add Videos (Combine tab)...", command=self.add_files_for_combine)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_app_close)

        view_menu = self._add_menu_bar_menu("View")
        view_menu.add_checkbutton(
            label="Dark mode", variable=self.dark_mode_var, command=self._on_dark_mode_toggled,
        )

        options_menu = self._add_menu_bar_menu("Options")
        options_menu.add_checkbutton(
            label="Warn before overwriting existing files",
            variable=self.warn_overwrite_var,
        )

        help_menu = self._add_menu_bar_menu("Help")
        help_menu.add_command(label="About", command=self._show_about)

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

    def _show_about(self):
        messagebox.showinfo(
            "About",
            "BarkBark Lossless Video Cutter\n\n"
            "Cutting and joining both use ffmpeg stream-copy (-c copy) for the "
            "bulk of the video, so almost nothing is re-encoded: it's fast and "
            "lossless. Cutting only re-encodes a fraction of a second right at "
            "an edge that doesn't land exactly on a keyframe, so the cut lands "
            "on the exact second you asked for (GPU-accelerated when "
            "available) — everything else stays byte-identical.\n\n"
            "Drag & drop is enabled — drop a video onto the Cut or Crop tab, "
            "or onto the Combine tab's list. The Crop tab uses a visual crop editor; "
            "arbitrary spatial cropping requires video re-encoding.\n\n"
            "Built with Python by Thmyris.",
        )

    # ====================================================================
    # Shared: move the window by dragging an empty "drop a video" area
    # ====================================================================
    def _bind_window_drag(self, widget, is_empty):
        """Dragging `widget` with the left button moves the whole window,
        but only while `is_empty(event)` is true, i.e. while the press lands
        on an empty drop-here area where a click has no other meaning."""
        widget.bind("<ButtonPress-1>", lambda e: self._window_drag_start(e, is_empty), add="+")
        widget.bind("<B1-Motion>", self._window_drag_motion, add="+")
        widget.bind("<ButtonRelease-1>", self._window_drag_end, add="+")

    def _window_drag_start(self, event, is_empty):
        self._window_drag_offset = None
        if not is_empty(event) or self.state() == "zoomed":
            return
        # winfo_x/y of a toplevel is its outer frame, the same corner that
        # geometry("+x+y") positions, so the offset stays exact.
        self._window_drag_offset = (event.x_root - self.winfo_x(),
                                    event.y_root - self.winfo_y())

    def _window_drag_motion(self, event):
        offset = getattr(self, "_window_drag_offset", None)
        if offset is None:
            return
        self.geometry(f"+{event.x_root - offset[0]}+{event.y_root - offset[1]}")

    def _window_drag_end(self, event):
        self._window_drag_offset = None

    # ====================================================================
    # Shared: light / dark theme (View menu)
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

    def _apply_ttk_theme(self):
        style = ttk.Style(self)
        if not self._is_dark():
            style.theme_use(self._ttk_light_theme)
            self._set_treeview_row_height(style)
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
        style.configure("TScrollbar", background=d["button"], troughcolor=d["panel"],
                        bordercolor=d["border"], arrowcolor=d["text"],
                        lightcolor=d["button"], darkcolor=d["button"])
        style.map("TScrollbar", background=[("active", d["button_active"])])
        style.configure("Horizontal.TProgressbar", background=d["select"], troughcolor=d["field"],
                        bordercolor=d["border"], lightcolor=d["select"], darkcolor=d["select"])

    def _set_title_bar_dark(self, dark):
        """Windows 10 1809+ / 11: a dark caption to match. Anywhere else,
        or if it fails, the title bar just stays as the system draws it."""
        if os.name != "nt":
            return
        try:
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
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

        open_btn = tk.Button(header, text="Open Video...", command=self.open_file_for_cut)
        open_btn.pack(side="right", padx=(4, 10))

        self.save_to_source_var = tk.BooleanVar(value=True)
        save_to_source_chk = tk.Checkbutton(
            header, text="Save to current folder", variable=self.save_to_source_var,
            bg="#f0f0f0", anchor="e",
        )
        save_to_source_chk.pack(side="right", padx=(10, 4))

        # ---- preview (a Canvas, which scales properly with the window --
        #      unlike a Label, a Canvas's width/height are always pixels)
        preview_frame = tk.Frame(root, bg="black")
        preview_frame.grid(row=1, column=0, sticky="nsew", padx=3, pady=4)
        self.preview_canvas = tk.Canvas(preview_frame, bg="black", highlightthickness=0)
        self.preview_canvas.pack(fill="both", expand=True)
        self.preview_canvas.bind("<Configure>", lambda e: self._redraw_preview_canvas())
        self._bind_window_drag(self.preview_canvas, lambda e: not self.input_path)
        self._draw_preview_placeholder("Open or drop a video to begin")

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
        self.cut_mark_buttons = []       # also change the range: off while cutting
        if self.cut_player is not None:
            self._build_cut_transport(controls)

        time_row = tk.Frame(controls, bg="#f0f0f0")
        time_row.pack(pady=(1, 2))  # no fill -> centers horizontally by default
        self.handle_var = tk.StringVar(value="end")
        tk.Radiobutton(time_row, text="Start:", variable=self.handle_var, value="start",
                        bg="#f0f0f0", command=lambda: self._set_active_handle("start")
                        ).pack(side="left")
        self.start_entry = tk.Entry(time_row, width=13, justify="center")
        self.start_entry.pack(side="left", padx=(2, 20))
        self.start_entry.bind("<Return>", lambda e: self._on_start_entry())
        self.start_entry.bind("<FocusOut>", lambda e: self._on_start_entry())

        tk.Radiobutton(time_row, text="End:", variable=self.handle_var, value="end",
                        bg="#f0f0f0", command=lambda: self._set_active_handle("end")
                        ).pack(side="left")
        self.end_entry = tk.Entry(time_row, width=13, justify="center")
        self.end_entry.pack(side="left", padx=2)
        self.end_entry.bind("<Return>", lambda e: self._on_end_entry())
        self.end_entry.bind("<FocusOut>", lambda e: self._on_end_entry())
        if self.cut_player is not None:
            self._build_cut_audio_controls(time_row)

        step_row = tk.Frame(controls, bg="#f0f0f0")
        step_row.pack(pady=(1, 2))  # no fill -> centers horizontally by default
        self.step_buttons = []
        for label, secs in (("1 sec", 1), ("5 sec", 5), ("30 sec", 30), ("3 min", 180)):
            tk.Label(step_row, text=f"{label}:", bg="#f0f0f0").pack(side="left", padx=(10, 2))
            b1 = tk.Button(step_row, text="<", width=7, command=lambda s=secs: self._nudge(-s))
            b1.pack(side="left")
            b2 = tk.Button(step_row, text=">", width=7, command=lambda s=secs: self._nudge(s))
            b2.pack(side="left", padx=(0, 4))
            self.step_buttons += [b1, b2]

        # ---- slider
        self.slider_canvas = tk.Canvas(controls, height=px(24), bg="#f0f0f0", highlightthickness=0)
        self.slider_canvas.pack(fill="x", pady=(1, 1))
        self.slider_canvas.bind("<Configure>", lambda e: self._redraw_slider())
        self.slider_canvas.bind("<Button-1>", self._slider_click)
        self.slider_canvas.bind("<B1-Motion>", self._slider_drag)
        self.slider_canvas.bind("<ButtonRelease-1>", self._slider_release)
        self.slider_canvas.bind("<Button-3>", self._cut_ranges_menu)
        self._drag_target = None

        # ---- waveform of the audio heard, on the slider's time axis
        self.wave_canvas = None
        if PIL_AVAILABLE:
            self.wave_canvas = tk.Canvas(controls, height=px(30), bg="#f0f0f0",
                                         highlightthickness=0)
            self.wave_canvas.pack(fill="x", pady=(0, 1))
            self.wave_canvas.bind("<Configure>", lambda e: self._redraw_waveform())
            self.wave_canvas.bind("<Button-1>", self._wave_click)
            self.wave_canvas.bind("<B1-Motion>", self._slider_drag)
            self.wave_canvas.bind("<ButtonRelease-1>", self._slider_release)
            self.wave_canvas.bind("<Button-3>", self._cut_ranges_menu)

        # ---- cut mode: classic (original, may snap to nearest keyframe) vs
        # smart-cut (re-encode just the edges for exact accuracy) vs
        # edit-list trim (exact accuracy, zero re-encode, experimental)
        self.cut_mode_var, self.cut_search_window_var, self.cut_search_window_entry = (
            self._build_cut_mode_controls(controls, default_mode="classic")
        )

        save_row = tk.Frame(controls, bg="#f0f0f0")
        save_row.pack(fill="x", pady=(2, 1))
        # Several ranges of one video: marked here, cut as a batch by Bulk Cut.
        self.cut_send_btn = tk.Button(save_row, height=2, width=19,
                                      command=self._cut_send_ranges_to_bulk)
        self.cut_send_btn.pack(side="right", padx=(2, 0))
        self.cut_add_range_btn = tk.Button(save_row, text="Add range (A)", height=2, width=13,
                                           command=self._cut_add_range)
        self.cut_add_range_btn.pack(side="right", padx=(2, 0))
        self.save_btn = tk.Button(save_row, text="Save this Scene!", height=2,
                                   font=("Segoe UI", 10, "bold"),
                                   command=self._save_scene_clicked)
        self.save_btn.pack(side="left", fill="x", expand=True)
        self._update_cut_range_buttons()

    def _build_cut_mode_controls(self, parent, default_mode="classic"):
        """Build the cut-mode radiobuttons + keyframe-search-window entry
        shared by the Cut tab and Bulk Cut tab. 2x2 grid instead of one tall
        column: Lossless / Re-encode edges stacked on the left, Edit-list
        trim on the right with the search-window control nested right below
        it (the only mode it applies to) -- two rows tall, no header label.
        Returns (mode_var, search_window_var, search_window_entry)."""
        block = tk.Frame(parent, bg="#f0f0f0")
        block.pack(pady=(1, 1))
        mode_var = tk.StringVar(value=default_mode)
        tk.Radiobutton(
            block, text="Lossless (recommended — fastest, may be a few ms off)",
            variable=mode_var, value="classic", bg="#f0f0f0", anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=(0, 18))
        tk.Radiobutton(
            block, text="Re-encode edges (slower, frame-accurate)",
            variable=mode_var, value="reencode", bg="#f0f0f0", anchor="w",
        ).grid(row=1, column=0, sticky="w", padx=(0, 18))
        tk.Radiobutton(
            block, text="Edit-list trim (experimental — may not work on some players)",
            variable=mode_var, value="editlist", bg="#f0f0f0", anchor="w",
        ).grid(row=0, column=1, sticky="w")

        window_row = tk.Frame(block, bg="#f0f0f0")
        window_row.grid(row=1, column=1, sticky="w", padx=(20, 0))
        tk.Label(window_row, text="Keyframe search window:", bg="#f0f0f0").pack(side="left")
        window_var = tk.StringVar(value="8.0")
        window_entry = tk.Entry(
            window_row, textvariable=window_var, width=5, justify="center"
        )
        window_entry.pack(side="left", padx=(4, 2))
        tk.Label(window_row, text="sec", bg="#f0f0f0").pack(side="left")
        window_entry.config(state="disabled")

        def on_mode_changed(*_args):
            window_entry.config(state="normal" if mode_var.get() == "editlist" else "disabled")

        mode_var.trace_add("write", on_mode_changed)
        return mode_var, window_var, window_entry

    @staticmethod
    def _get_search_window_value(window_var):
        try:
            value = float(window_var.get())
            return value if value > 0 else 8.0
        except ValueError:
            return 8.0

    def _set_cut_controls_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self.start_entry.config(state=state)
        self.end_entry.config(state=state)
        self.save_btn.config(state=state)
        for b in self.step_buttons + self.cut_mark_buttons:
            b.config(state=state)
        self._update_cut_range_buttons()

    # ---- embedded player ---------------------------------------------
    def _build_cut_transport(self, parent):
        """Play / step / jump buttons, the playhead time, and the buttons
        that set the start or end at the frame on screen."""
        row = tk.Frame(parent, bg="#f0f0f0")
        row.pack(pady=(2, 1))

        def button(text, command, marks=False, **kw):
            b = tk.Button(row, text=text, command=command, **kw)
            b.pack(side="left", padx=1)
            (self.cut_mark_buttons if marks else self.cut_transport_buttons).append(b)
            return b

        button("|◀ Start", lambda: self._cut_go_to("start"))
        button("◀ Frame", lambda: self._cut_frame_step(False))
        self.cut_play_btn = button("▶ Play", self._cut_toggle_play, width=8)
        button("Frame ▶", lambda: self._cut_frame_step(True))
        button("End ▶|", lambda: self._cut_go_to("end"))
        self.cut_time_lbl = tk.Label(row, text="", bg="#f0f0f0", width=27,
                                     font=("Segoe UI", 9))
        self.cut_time_lbl.pack(side="left", padx=6)
        button("Set start (I)", lambda: self._cut_mark("start"), marks=True)
        button("Set end (O)", lambda: self._cut_mark("end"), marks=True)
        self.cut_loop_btn = button("Play selection", self._cut_toggle_loop, width=13)
        for b in self.cut_transport_buttons + self.cut_mark_buttons:
            b.config(state="disabled")

        # Keyboard: Space play/pause, I / O set start / end, , / . one
        # frame back / forward, Left / Right 5 s (with Shift 1 s).
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
            # A add the range to the ranges for Bulk Cut.
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

    def _build_cut_audio_controls(self, parent):
        """Mute, volume and the audio track heard. A cut keeps every audio
        track; the picker only chooses which one the player plays. Volume
        and mute are remembered between runs."""
        player = self.cut_player
        player.set_volume(self.settings.get("player_volume", 100.0))
        player.set_muted(self.settings.get("player_muted", False))
        self._cut_audio_save_id = None

        self.cut_mute_btn = tk.Button(parent, width=7, command=self._cut_toggle_mute)
        self.cut_mute_btn.pack(side="left", padx=(px(24), 2))
        self.cut_volume_slider = TimeSlider(parent, command=self._cut_volume_changed,
                                            width=100, height=22)
        self.cut_volume_slider.config_range(to=100.0)
        self.cut_volume_slider.set(player.volume)
        self.cut_volume_slider.pack(side="left")
        self.cut_volume_slider.bind(
            "<MouseWheel>", lambda e: self._cut_volume_step(5 if e.delta > 0 else -5))
        self.cut_volume_lbl = tk.Label(parent, bg="#f0f0f0", width=4, anchor="w")
        self.cut_volume_lbl.pack(side="left")

        self.cut_track_btn = tk.Button(parent, text="No audio", state="disabled",
                                       command=self._cut_post_track_menu)
        self.cut_track_btn.pack(side="left", padx=(px(8), 0))
        self.cut_track_menu = tk.Menu(self.cut_track_btn, tearoff=0)
        self.cut_track_var = tk.IntVar(value=0)
        self._cut_track_list = None       # the tracks the menu was built for
        self._update_cut_audio_controls()

    def _update_cut_audio_controls(self):
        player = self.cut_player
        tracks = player.audio_tracks
        key = [(t.get("id"), t.get("title"), t.get("lang"), t.get("codec")) for t in tracks]
        shown = (key, player.audio_track, player.muted, player.volume)
        if shown == getattr(self, "_cut_audio_shown", None):
            return   # called on every player change, ~30 times a second in playback
        self._cut_audio_shown = shown
        self.cut_mute_btn.config(text="Unmute" if player.muted else "Mute")
        self.cut_volume_lbl.config(text=f"{int(round(player.volume))}%")
        if key != self._cut_track_list:
            self._cut_track_list = key
            self.cut_track_menu.delete(0, "end")
            for t in tracks:
                self.cut_track_menu.add_radiobutton(
                    label=describe_audio_track(t), variable=self.cut_track_var, value=t["id"],
                    command=lambda tid=t["id"]: self.cut_player.set_audio_track(tid))
        current = player.audio_track
        self.cut_track_var.set(current if current is not None else 0)
        ids = [t["id"] for t in tracks]
        if current in ids and ids.index(current) != self._wave_track:
            self._start_waveform(ids.index(current))   # the waveform shows the track heard
        if not tracks:
            self.cut_track_btn.config(text="No audio", state="disabled")
        else:
            n = ids.index(current) + 1 if current in ids else 0
            text = f"Audio {n}/{len(tracks)} ▾" if n else f"Audio off/{len(tracks)} ▾"
            self.cut_track_btn.config(text=text,
                                      state="normal" if len(tracks) > 1 else "disabled")

    def _cut_post_track_menu(self):
        menu = self.cut_track_menu
        if menu.index("end") is None:
            return
        menu.configure(DARK_MENU if self._is_dark() else self._menu_light_colors)
        b = self.cut_track_btn
        x, y = b.winfo_rootx(), b.winfo_rooty() + b.winfo_height()
        margin = DARK_MENU["background"] if self._is_dark() else None
        post_with_menu_margin(lambda: menu.tk_popup(x, y), margin)

    def _cut_toggle_mute(self):
        if self.cut_player is None:
            return
        self.cut_player.set_muted(not self.cut_player.muted)
        self._cut_audio_settings_changed()

    def _cut_volume_changed(self, value):
        player = self.cut_player
        player.set_volume(value)
        if player.muted and value > 0:
            player.set_muted(False)   # turning the volume up means wanting to hear it
        self._cut_audio_settings_changed()

    def _cut_volume_step(self, delta):
        if self.cut_player is None:
            return
        self.cut_player.set_volume(self.cut_player.volume + delta)
        self.cut_volume_slider.set(self.cut_player.volume)
        self._cut_volume_changed(self.cut_player.volume)

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
        else:
            return None
        return {
            "<KeyPress-space>": play,
            "<KeyPress-comma>": lambda: step(False),
            "<KeyPress-period>": lambda: step(True),
            "<KeyPress-Left>": lambda: seek_by(-5),
            "<KeyPress-Right>": lambda: seek_by(5),
            "<Shift-KeyPress-Left>": lambda: seek_by(-1),
            "<Shift-KeyPress-Right>": lambda: seek_by(1),
        }.get(sequence)

    def _cut_use_player(self, on):
        """Shows the player over the still-frame canvas, or takes it away."""
        self._cut_player_on = on
        if on:
            self.cut_player.frame.place(x=0, y=0, relwidth=1, relheight=1)
        else:
            self.cut_player.frame.place_forget()
        for b in self.cut_transport_buttons:
            b.config(state="normal" if on else "disabled")
        self._update_cut_loop_button()

    def _on_cut_video_click(self, event):
        # Clicking the video plays / pauses it, and takes the keyboard focus
        # off a time field so the keys work right away.
        self.cut_tab.focus_set()
        self._cut_toggle_play()

    def _cut_toggle_play(self):
        if self._cut_player_on:
            self.cut_player.toggle_pause()

    def _cut_frame_step(self, forward):
        if self._cut_player_on:
            self.cut_player.frame_step(forward)

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
        self._cut_range_changed()
        self.status_lbl.config(
            text=f"{which.capitalize()} set at the frame at {seconds_to_ffmpeg_time(t)}")

    def _cut_range_changed(self):
        """Keeps a running "Play selection" loop on the current range."""
        if self._cut_player_on and self.cut_player.looping:
            self.cut_player.set_loop(self.start_sec, self.end_sec)

    def _cut_toggle_loop(self):
        if not self._cut_player_on:
            return
        player = self.cut_player
        if player.looping:
            player.clear_loop()
            player.set_paused(True)
        else:
            player.set_loop(self.start_sec, self.end_sec)
            player.show_first_frame_from(self.start_sec)
            player.set_paused(False)
        self._update_cut_loop_button()

    def _update_cut_loop_button(self):
        looping = self._cut_player_on and self.cut_player.looping
        self.cut_loop_btn.config(text="Stop loop" if looping else "Play selection")

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
        playing = not player.paused
        self.cut_play_btn.config(text="❚❚ Pause" if playing else "▶ Play")
        self._update_cut_audio_controls()
        pos = player.position
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
        c.create_text(w / 2, h / 2, text=text, fill="#888888",
                       font=("Segoe UI", 12))

    def _redraw_preview_canvas(self):
        c = self.preview_canvas
        if self.raw_preview_img is None:
            self._draw_preview_placeholder(
                "Open or drop a video to begin" if not self.input_path else ""
            )
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

    def _load_video_for_cut(self, path):
        try:
            self.status_lbl.config(text="Reading video info...")
            self.update_idletasks()
            ffprobe = find_exe("ffprobe")
            result = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, check=True, **SUBPROCESS_KWARGS,
            )
            duration = float(result.stdout.strip())
        except Exception as exc:
            messagebox.showerror("Error loading video", str(exc))
            self.status_lbl.config(text="Ready")
            return

        self.input_path = path
        self.duration = duration
        self.file_size = os.path.getsize(path)
        self.start_sec = 0.0
        self.end_sec = duration
        self.cut_ranges = self._cut_ranges_by_file.setdefault(
            os.path.normcase(os.path.abspath(path)), [])

        self.filename_lbl.config(text=os.path.basename(path))
        self.info_lbl.config(text=f"[{seconds_to_hms(duration)}]   {format_size(self.file_size)}")

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
            self._cut_range_changed()
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
            self._cut_range_changed()
            self._cut_show_time(self.end_sec, "end")

    def _set_active_handle(self, which):
        self.active_handle = which

    def _nudge(self, delta_seconds):
        if not self.input_path:
            return
        # Button nudges always land on a whole second (.000) -- see
        # _x_to_time's docstring for why that matters for reproducibility.
        if self.active_handle == "start":
            self.start_sec = float(round(max(0.0, min(self.start_sec + delta_seconds, self.end_sec))))
            self._cut_show_time(self.start_sec, "start")
        else:
            self.end_sec = float(round(min(self.duration, max(self.end_sec + delta_seconds, self.start_sec))))
            self._cut_show_time(self.end_sec, "end")
        self._sync_entries()
        self._redraw_slider()
        self._cut_range_changed()

    # ---- ranges for Bulk Cut -------------------------------------------
    # Several scenes of one video: each Start-End is added to a list (A),
    # shown in green on the slider and the waveform, and the list is sent
    # to Bulk Cut as one row, which cuts each range into its own clip. The
    # list belongs to the video and stays while another one is open.
    def _cut_add_range(self):
        if not self.input_path or str(self.save_btn.cget("state")) == "disabled":
            return
        start, end = round(self.start_sec, 3), round(self.end_sec, 3)
        if end <= start:
            self.status_lbl.config(text="Nothing to add: the end is not after the start")
            return
        if (start, end) in self.cut_ranges:
            self.status_lbl.config(text="This range is already added")
            return
        self.cut_ranges.append((start, end))
        self.cut_ranges.sort()
        n = self.cut_ranges.index((start, end)) + 1
        self.status_lbl.config(text=f"Range {n} added: {seconds_to_ffmpeg_time(start)} - "
                                    f"{seconds_to_ffmpeg_time(end)}")
        self._cut_ranges_changed()

    def _cut_ranges_changed(self):
        self._redraw_slider()
        self._update_cut_range_buttons()

    def _update_cut_range_buttons(self):
        if not hasattr(self, "cut_send_btn"):
            return   # still building the tab
        n = len(self.cut_ranges)
        self.cut_send_btn.config(
            text=f"Send {n} range{'' if n == 1 else 's'}\nto Bulk Cut" if n else "Send ranges\nto Bulk Cut",
            state="normal" if n else "disabled")
        self.cut_add_range_btn.config(state="normal" if self.input_path else "disabled")

    def _cut_ranges_at(self, x):
        t = self._x_to_exact_time(x)
        tol = px(3) * self.duration / max(1, self._track_x1 - self._track_x0)   # 3 px either side
        return [i for i, (s, e) in enumerate(self.cut_ranges) if s - tol <= t <= e + tol]

    def _cut_ranges_menu(self, event):
        """Right-click on the slider or the waveform: select or remove the
        marked range there, or remove them all."""
        if not self.input_path or not self.cut_ranges:
            return
        menu = tk.Menu(self, tearoff=0)
        for i in self._cut_ranges_at(event.x):
            s, e = self.cut_ranges[i]
            label = f"range {i + 1} ({seconds_to_ffmpeg_time(s)} - {seconds_to_ffmpeg_time(e)})"
            menu.add_command(label=f"Select {label}",
                             command=lambda r=(s, e): self._cut_select_range(r))
            menu.add_command(label=f"Remove {label}",
                             command=lambda r=(s, e): self._cut_remove_ranges([r]))
            menu.add_separator()
        n = len(self.cut_ranges)
        menu.add_command(label=f"Remove all {n} range{'' if n == 1 else 's'}",
                         command=lambda: self._cut_remove_ranges(list(self.cut_ranges)))
        menu.configure(DARK_MENU if self._is_dark() else self._menu_light_colors)
        self._cut_ranges_menu_widget = menu   # for the tests
        x, y = event.x_root, event.y_root
        post_with_menu_margin(lambda: menu.tk_popup(x, y),
                              DARK_MENU["background"] if self._is_dark() else None)

    def _cut_select_range(self, rng):
        """Makes a marked range the Start-End again, to look at or change it
        (an adjusted range is added as a new one; remove the old one)."""
        self.start_sec, self.end_sec = rng
        self._sync_entries()
        self._redraw_slider()
        self._cut_range_changed()
        self._cut_show_time(self.start_sec, "start")

    def _cut_remove_ranges(self, ranges):
        for r in ranges:
            if r in self.cut_ranges:
                self.cut_ranges.remove(r)
        n = len(ranges)
        self.status_lbl.config(text=f"Removed {n} range{'' if n == 1 else 's'}")
        self._cut_ranges_changed()

    def _cut_send_ranges_to_bulk(self):
        """Adds the video with its marked ranges to Bulk Cut as one row and
        shows that tab; the ranges are moved there, not copied."""
        if not self.input_path or not self.cut_ranges:
            return
        self._ensure_tab_built(2)
        ranges = list(self.cut_ranges)
        iid = self._bulk_add_row(self.input_path, ranges)
        self.cut_ranges.clear()
        self._cut_ranges_changed()
        self.notebook.select(self.bulk_tab)
        self.bulk_tree.selection_set(iid)
        self.bulk_tree.see(iid)
        n = len(ranges)
        self.status_lbl.config(text=f"Sent {n} range{'' if n == 1 else 's'} of "
                                    f"{os.path.basename(self.input_path)} to Bulk Cut")

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
        """Draws audio track `track` (0-based) under the slider, on the
        slider's time axis: the track the player plays, so it follows the
        audio-track picker. ffmpeg draws it in the background
        (render_waveform: about 0.2 s for 2 GB of MP4, 0.7 s of MPEG-TS)
        and it is kept for the session."""
        if self.wave_canvas is None or not self.input_path:
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

        def started(proc):
            self._wave_proc = proc
            if gen != self._wave_gen:   # stopped before ffmpeg was even running
                proc.kill()

        def worker():
            mask, note = None, ""
            try:
                mask = render_waveform(path, track, 2400, 64, span=span,
                                       on_start=started).convert("L")
            except Exception as exc:
                note = "No audio" if "matches no streams" in str(exc) else "Waveform unavailable"
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
        """The strip: the waveform across the same span as the slider's
        track, blue inside the selection, green in ranges marked for Bulk
        Cut and gray elsewhere, and the player's playhead."""
        c = self.wave_canvas
        if c is None:
            return
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 2 or h < 2 or not self.input_path or self.duration <= 0:
            return
        x0, x1 = px(10), max(px(20), w - px(10))   # as the slider's track
        if self._wave_mask is not None:
            c.create_image(x0, 0, image=self._waveform_photo(x0, x1, h), anchor="nw")
        elif self._wave_note:
            c.create_text(x0, h / 2, text=self._wave_note, anchor="w", font=("Segoe UI", 8),
                          fill=self._theme_color("wave"))
        if self._cut_player_on:
            c.create_line(0, 0, 0, 0, fill=PLAYHEAD_COLOR, width=px(2), tags=("playhead",))
            self._move_playhead(self.cut_player.position)

    def _waveform_photo(self, x0, x1, h):
        width = x1 - x0
        sel0 = round((x1 - x0) * self.start_sec / self.duration)
        sel1 = round((x1 - x0) * self.end_sec / self.duration)
        spans = tuple((round(width * s / self.duration), round(width * e / self.duration))
                      for s, e in self.cut_ranges)
        bg = self.wave_canvas.cget("bg")
        made_for = (id(self._wave_mask), width, h, sel0, sel1, spans, bg,
                    self._theme_color("wave"), self._theme_color("range"))
        if self._wave_photo[0] == made_for:
            return self._wave_photo[1]
        if self._wave_sized[0] != (id(self._wave_mask), width, h):
            self._wave_sized = ((id(self._wave_mask), width, h),
                                self._wave_mask.resize((width, h), Image.BILINEAR))
        mask = self._wave_sized[1]
        rgb = tuple(v // 257 for v in self.wave_canvas.winfo_rgb(bg))
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
        """With the player, the strip moves the playhead (never a handle);
        without it, it works like the slider."""
        if not self.input_path:
            return
        if not self._cut_player_on:
            self._slider_click(event)
            return
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
        c.create_line(track_x0, y, track_x1, y, fill=self._theme_color("slider_track"),
                      width=px(4), capstyle="round")
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
        c.create_line(sx, y, ex, y, fill="#3d8bfd", width=px(4), capstyle="round")
        if self._cut_player_on:
            # The playhead: where the player is. Under the handles, so they
            # stay visible and grabbable when it sits on one.
            c.create_line(0, 0, 0, 0, fill=PLAYHEAD_COLOR, width=px(2), tags=("playhead",))
            c.create_polygon(0, 0, 0, 0, 0, 0, fill=PLAYHEAD_COLOR, outline="",
                             tags=("playhead_top",))
            self._move_playhead(self.cut_player.position)
        r = px(7)
        c.create_oval(sx - r, y - r, sx + r, y + r, fill=self._theme_color("slider_start"),
                      outline="")
        c.create_oval(ex - r, y - r, ex + r, y + r, fill="#1a73e8", outline="")

    def _move_playhead(self, t):
        c, wc = self.slider_canvas, self.wave_canvas
        if t is None or not self.duration:
            c.itemconfigure("playhead", state="hidden")
            c.itemconfigure("playhead_top", state="hidden")
            if wc is not None:
                wc.itemconfigure("playhead", state="hidden")
            return
        x = self._time_to_x(min(max(t, 0.0), self.duration))
        h = c.winfo_height() or px(24)
        a = px(5)
        c.coords("playhead", x, a, x, h)
        c.coords("playhead_top", x - a, 0, x + a, 0, x, a + px(1))
        c.itemconfigure("playhead", state="normal")
        c.itemconfigure("playhead_top", state="normal")
        if wc is not None:
            wc.coords("playhead", x, 0, x, wc.winfo_height())
            wc.itemconfigure("playhead", state="normal")

    def _x_to_time(self, x):
        # Rounds to a whole second (.000) -- a drag is only pixel-precise,
        # not sub-second-precise (especially on a long video, where one
        # pixel can span several real seconds), but the entry only ever
        # *displays* whole seconds from slider input. Without this, two
        # drags that look identical on screen could silently land on
        # different floats a fraction of a second apart -- and since
        # classic mode's cut point is a stream-copy keyframe snap with no
        # decode to correct it, that invisible difference can be the whole
        # reason "the same second" sometimes snaps a few seconds early and
        # sometimes doesn't. Typing an exact time into the entry field is
        # still the way to get sub-second (millisecond) precision.
        frac = (x - self._track_x0) / max(1, (self._track_x1 - self._track_x0))
        frac = min(1.0, max(0.0, frac))
        return float(round(frac * self.duration))

    def _time_to_x(self, t):
        frac = t / self.duration if self.duration else 0
        return self._track_x0 + frac * (self._track_x1 - self._track_x0)

    def _slider_click(self, event):
        """Without the player, a click moves the nearer handle there. With
        it, only a press on a handle drags the handle; anywhere else moves
        the playhead (scrubbing), and I / O then set the range."""
        if not self.input_path:
            return
        sx = self._time_to_x(self.start_sec)
        ex = self._time_to_x(self.end_sec)
        self._drag_target = "start" if abs(event.x - sx) <= abs(event.x - ex) else "end"
        if self._cut_player_on:
            self.cut_tab.focus_set()   # off a time field, so the keys work
            if min(abs(event.x - sx), abs(event.x - ex)) > px(9):
                self._drag_target = "playhead"
                self._cut_scrubbing = True
        self._slider_drag(event)

    def _slider_drag(self, event):
        if not self.input_path or not self._drag_target:
            return
        if self._drag_target == "playhead":
            # Keyframe seeks while dragging (fast); the exact frame on release.
            t = self._x_to_exact_time(event.x)
            self._cut_scrub_time = t
            self._move_playhead(t)
            self.cut_player.seek(t, exact=False)
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
        self._sync_entries()
        self._redraw_slider()
        if self._cut_player_on:
            self.cut_player.seek(t, exact=False)
        else:
            self._update_preview(t)

    def _slider_release(self, event):
        target, self._drag_target = self._drag_target, None
        if not self._cut_player_on or target is None:
            return
        if target == "playhead":
            self._cut_scrubbing = False
            self.cut_player.seek(self._cut_scrub_time)
        else:
            self._cut_range_changed()
            if target == "start":
                self._cut_show_time(self.start_sec, "start")
            else:
                self._cut_show_time(self.end_sec, "end")

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
        search_window = self._get_search_window_value(self.cut_search_window_var)

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
            return "100% lossless, no re-encoding (classic -- snapped to the nearest keyframe, may not be frame-exact)"
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

    def _cut_done(self, info, success, result=None):
        self._end_progress("cut")
        self._set_cut_controls_enabled(True)
        if success:
            how = self._describe_cut_result(result)
            self.status_lbl.config(text=f"Saved ({how}): {info}")
            messagebox.showinfo("Done", f"Scene saved to:\n{info}\n\n{how[0].upper()}{how[1:]}.")
        else:
            self.status_lbl.config(text="Cut failed")
            messagebox.showerror("ffmpeg error",
                                  "The cut failed. Details:\n\n" + info)

    # ====================================================================
    # COMBINE TAB
    # ====================================================================
    ROW_HEIGHT = 56
    THUMB_SIZE = (72, 40)

    def _build_combine_tab(self):
        root = self.combine_tab
        root.rowconfigure(1, weight=1)
        root.columnconfigure(0, weight=1)

        header = tk.Frame(root, bg="#f0f0f0")
        header.grid(row=0, column=0, sticky="ew", padx=3, pady=(6, 2))
        tk.Label(header, text="Videos will be joined in the order listed below "
                 "(top to bottom). Drag a row to reorder it.",
                 bg="#f0f0f0", anchor="w", wraplength=px(600), justify="left"
                 ).pack(side="left", fill="x", expand=True)

        # list (custom scrollable rows with thumbnails) + side buttons
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
        list_frame = tk.Frame(self.combine_paned, bg="black")
        self.combine_paned.add(list_frame, stretch="always", minsize=px(60))
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)

        scrollbar = ttk.Scrollbar(list_frame)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.combine_canvas = tk.Canvas(list_frame, bg="#1e1e1e", highlightthickness=0,
                                         yscrollcommand=scrollbar.set)
        self.combine_canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.config(command=self.combine_canvas.yview)

        self.combine_inner = tk.Frame(self.combine_canvas, bg="#1e1e1e")
        self._combine_inner_window = self.combine_canvas.create_window(
            (0, 0), window=self.combine_inner, anchor="nw"
        )
        self.combine_inner.bind(
            "<Configure>",
            lambda e: self.combine_canvas.configure(scrollregion=self.combine_canvas.bbox("all")),
        )
        self.combine_canvas.bind(
            "<Configure>",
            lambda e: self.combine_canvas.itemconfig(self._combine_inner_window, width=e.width),
        )
        # Rows and the placeholder cover the canvas where they are, so a
        # press that reaches the canvas itself is on the blank area below.
        self._bind_window_drag(self.combine_canvas, lambda e: True)

        self.combine_thumb_cache = {}    # path -> PhotoImage
        self.combine_thumb_pending = set()  # paths with an extraction already queued
        self.combine_thumb_labels = {}   # path -> thumbnail Labels in the current rows
        # Few workers on purpose: decoding a frame from several large videos
        # at once just makes them all fight over disk/CPU and finish later.
        self.combine_thumb_pool = ThreadPoolExecutor(max_workers=2)
        # Blank stand-in so a row's thumbnail slot is sized in pixels (a
        # Label with no image measures width/height in text characters).
        self.combine_thumb_blank = tk.PhotoImage(width=self.THUMB_SIZE[0], height=self.THUMB_SIZE[1])
        self.combine_selected_index = None
        self._drag_active = False
        self._drag_index = None
        self._rebuild_combine_rows()

        side = tk.Frame(body, bg="#f0f0f0")
        side.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        tk.Button(side, text="Add Files...", width=16, command=self.add_files_for_combine).pack(pady=2)
        tk.Button(side, text="Remove Selected", width=16, command=self._combine_remove_selected).pack(pady=2)
        tk.Button(side, text="Move Up", width=16, command=lambda: self._combine_move(-1)).pack(pady=2)
        tk.Button(side, text="Move Down", width=16, command=lambda: self._combine_move(1)).pack(pady=2)
        tk.Button(side, text="Clear All", width=16, command=self._combine_clear).pack(pady=2)
        self.combine_preview_btn = None
        if mpv_available():
            self.combine_preview_btn = tk.Button(side, text="Preview", width=16,
                                                 command=self._combine_toggle_preview)
            self.combine_preview_btn.pack(pady=(10, 2))

        self.combine_save_to_source_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            side, text="Save to\ncurrent folder", variable=self.combine_save_to_source_var,
            bg="#f0f0f0", justify="left", anchor="w",
        ).pack(pady=(16, 2), anchor="w")

        footer = tk.Frame(root, bg="#f0f0f0")
        footer.grid(row=2, column=0, sticky="ew", padx=3, pady=(4, 2))
        tk.Label(footer, text="Note: lossless joining requires all videos to share the "
                 "same codec/resolution/format (e.g. all H.264 MP4s). Mixed formats will "
                 "fail with an ffmpeg error rather than silently re-encoding.",
                 bg="#f0f0f0", fg="#555555", wraplength=px(850), justify="left"
                 ).pack(fill="x")

        self.combine_btn = tk.Button(root, text="Combine Videos!", height=2,
                                      font=("Segoe UI", 10, "bold"),
                                      command=self._combine_clicked)
        self.combine_btn.grid(row=3, column=0, sticky="ew", padx=3, pady=(4, 6))

        self.combine_canvas.drop_target_register(DND_FILES)
        self.combine_canvas.dnd_bind("<<Drop>>", self._on_drop_combine)
        self.combine_inner.drop_target_register(DND_FILES)
        self.combine_inner.dnd_bind("<<Drop>>", self._on_drop_combine)

    # ---- row rendering --------------------------------------------------
    def _rebuild_combine_rows(self):
        self._schedule_combine_preview_sync()
        for child in self.combine_inner.winfo_children():
            child.destroy()
        self.combine_thumb_labels = {}

        if not self.combine_files:
            placeholder = tk.Label(
                self.combine_inner,
                text="  (drag & drop videos here, or click 'Add Files...')",
                bg="#1e1e1e", fg="#888888", anchor="w", font=("Consolas", 10),
            )
            placeholder.pack(fill="x", ipady=16)
            self._bind_window_drag(placeholder, lambda e: not self.combine_files)
            return

        for i, path in enumerate(self.combine_files):
            selected = (i == self.combine_selected_index)
            row_bg = "#3d8bfd" if selected else "#1e1e1e"
            row = tk.Frame(self.combine_inner, bg=row_bg, height=self.ROW_HEIGHT)
            row.pack(fill="x")
            row.pack_propagate(False)

            thumb_lbl = tk.Label(row, bg="#000000", image=self.combine_thumb_blank,
                                  width=self.THUMB_SIZE[0], height=self.THUMB_SIZE[1])
            cached = self.combine_thumb_cache.get(path)
            if cached:
                thumb_lbl.config(image=cached)
                thumb_lbl.image = cached
            elif cached is None:
                self.combine_thumb_labels.setdefault(path, []).append(thumb_lbl)
                self._request_thumbnail(path)
            thumb_lbl.pack(side="left", padx=8, pady=6)

            name_lbl = tk.Label(row, text=f"{i + 1}. {os.path.basename(path)}",
                                 bg=row_bg, fg="white", anchor="w", font=("Consolas", 10))
            name_lbl.pack(side="left", fill="x", expand=True, padx=(4, 8))

            # any click/drag on the row or its children behaves the same
            for widget in (row, thumb_lbl, name_lbl):
                widget.bind("<ButtonPress-1>", lambda e, idx=i: self._combine_drag_start(e, idx))

    def _request_thumbnail(self, path):
        # Every row rebuild (select, drag, add, each finished thumbnail...)
        # used to start a brand new extraction for every row that didn't
        # have its thumbnail yet, so the same files were decoded over and
        # over in parallel -- the main reason thumbnails took so long.
        if not PIL_AVAILABLE or path in self.combine_thumb_pending:
            return
        self.combine_thumb_pending.add(path)
        self.combine_thumb_pool.submit(self._extract_thumbnail, path)

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
        self.combine_thumb_pending.discard(path)
        if pil_img is None:
            # Remember the failure so rebuilds don't keep retrying it.
            self.combine_thumb_cache[path] = ""
            return
        photo = ImageTk.PhotoImage(pil_img)
        self.combine_thumb_cache[path] = photo
        # Patch just the rows showing this file instead of rebuilding the
        # whole list.
        for lbl in self.combine_thumb_labels.pop(path, []):
            if lbl.winfo_exists():
                lbl.config(image=photo)
                lbl.image = photo

    # ---- selection & drag-to-reorder ------------------------------------
    def _combine_select(self, idx):
        self.combine_selected_index = idx
        self._rebuild_combine_rows()

    def _combine_drag_start(self, event, idx):
        self._combine_select(idx)
        self._drag_active = True
        self._drag_index = idx
        self.bind_all("<B1-Motion>", self._combine_drag_motion)
        self.bind_all("<ButtonRelease-1>", self._combine_drag_end)

    def _combine_drag_motion(self, event):
        if not self._drag_active or not self.combine_files:
            return
        rel_y = event.y_root - self.combine_inner.winfo_rooty()
        hover_index = int(rel_y // self.ROW_HEIGHT)
        hover_index = max(0, min(hover_index, len(self.combine_files) - 1))
        if hover_index != self._drag_index:
            self.combine_files[self._drag_index], self.combine_files[hover_index] = (
                self.combine_files[hover_index], self.combine_files[self._drag_index]
            )
            self._drag_index = hover_index
            self.combine_selected_index = hover_index
            self._rebuild_combine_rows()

    def _combine_drag_end(self, event):
        self._drag_active = False
        self._drag_index = None
        self.unbind_all("<B1-Motion>")
        self.unbind_all("<ButtonRelease-1>")

    # ---- file list management --------------------------------------------
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
        i = self.combine_selected_index
        if i is None or not (0 <= i < len(self.combine_files)):
            return
        del self.combine_files[i]
        self.combine_selected_index = None
        self._rebuild_combine_rows()

    def _combine_move(self, direction):
        i = self.combine_selected_index
        if i is None:
            return
        j = i + direction
        if 0 <= j < len(self.combine_files):
            self.combine_files[i], self.combine_files[j] = self.combine_files[j], self.combine_files[i]
            self.combine_selected_index = j
            self._rebuild_combine_rows()

    def _combine_clear(self):
        self.combine_files = []
        self.combine_selected_index = None
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

        from_dialog = not self.combine_save_to_source_var.get()
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

    def _run_combine(self, out_path):
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
        that moment.
        """
        tmpdir = None
        try:
            ffmpeg = find_exe("ffmpeg")
            ffprobe = find_exe("ffprobe")

            tmpdir = tempfile.mkdtemp(prefix="lossless_combine_")
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
                cmd = [ffmpeg, "-y", "-i", p, "-c", "copy"]
                if video_bsf:
                    cmd += ["-bsf:v", video_bsf]
                cmd += ["-f", "mpegts", ts_path]
                overall_fraction(p, 0.0)  # makes sure p's duration is known
                proc = run_ffmpeg(
                    cmd, durations[p],
                    lambda f, p=p, report=report: report(overall_fraction(p, f)),
                )
                if proc.returncode != 0:
                    raise RuntimeError(
                        f"Failed to prepare '{os.path.basename(p)}' for combining:\n\n"
                        + proc.stderr[-1500:]
                    )
                prepared[p] = (ts_path, durations[p])

            final_files = list(self.combine_files)
            if len(final_files) < 2:
                raise RuntimeError(
                    "Fewer than 2 videos were left in the list, so nothing was combined."
                )
            audio_bsf = {"aac": "aac_adtstoasc"}.get(audio_codec)
            base_frac = overall_fraction(None, 0.0)
            report = self._progress_reporter(
                lambda pct, n=len(final_files): f"Combining: joining {n} files... {pct}%", "combine"
            )
            ts_paths = [prepared[p][0] for p in final_files]
            concat_arg = "concat:" + "|".join(ts_paths)
            cmd = [ffmpeg, "-y", "-i", concat_arg, "-c", "copy"]
            if audio_bsf:
                cmd += ["-bsf:a", audio_bsf]
            cmd += [out_path]
            proc = run_ffmpeg(
                cmd, sum(prepared[p][1] for p in final_files),
                lambda f: report(base_frac + (1.0 - base_frac) * f),
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    "ffmpeg couldn't stream-copy these files together -- this "
                    "usually means they don't share the same codec/format.\n\n"
                    + proc.stderr[-2000:]
                )
            self.after(0, lambda: self._combine_done(out_path, True))
        except Exception as exc:
            self.after(0, lambda e=str(exc): self._combine_done(e, False))
        finally:
            if tmpdir and os.path.isdir(tmpdir):
                shutil.rmtree(tmpdir, ignore_errors=True)

    def _combine_done(self, info, success):
        self._end_progress("combine")
        self.combine_btn.config(state="normal")
        if success:
            self.status_lbl.config(text=f"Saved: {info}")
            messagebox.showinfo("Done", f"Combined video saved to:\n{info}")
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
                 "MM.SS and HH.MM.SS are both accepted, and [-43.-51] / [-27] count from the end "
                 "(see below). Each bracket creates its own lossless clip.",
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
        tree_wrap = tk.Frame(self.bulk_paned, bg="#f0f0f0")
        self.bulk_paned.add(tree_wrap, stretch="always", minsize=px(60))
        tree_wrap.rowconfigure(0, weight=1)
        tree_wrap.columnconfigure(0, weight=1)

        columns = ("range", "length", "status")
        self.bulk_tree = ttk.Treeview(
            tree_wrap, columns=columns, show="tree headings", selectmode="extended"
        )
        self.bulk_tree.heading("#0", text="File Name")
        self.bulk_tree.heading("range", text="Cut Range")
        self.bulk_tree.heading("length", text="Final Length")
        self.bulk_tree.heading("status", text="Status")
        self.bulk_tree.column("#0", width=px(340), anchor="w")
        self.bulk_tree.column("range", width=px(170), anchor="center")
        self.bulk_tree.column("length", width=px(110), anchor="center")
        self.bulk_tree.column("status", width=px(140), anchor="center")
        self.bulk_tree.grid(row=0, column=0, sticky="nsew")

        vsb = ttk.Scrollbar(tree_wrap, command=self.bulk_tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self.bulk_tree.configure(yscrollcommand=vsb.set)

        side = tk.Frame(body, bg="#f0f0f0")
        side.grid(row=0, column=2, sticky="ns", padx=(8, 0))
        tk.Button(side, text="Add Files...", width=16, command=self.add_files_for_bulk).pack(pady=2)
        tk.Button(side, text="Remove Selected", width=16, command=self._bulk_remove_selected).pack(pady=2)
        tk.Button(side, text="Clear All", width=16, command=self._bulk_clear).pack(pady=2)
        self.bulk_preview_btn = None
        if mpv_available():
            # Also: double-click a row.
            self.bulk_preview_btn = tk.Button(side, text="Preview Selected", width=16,
                                              command=self._bulk_preview_selected)
            self.bulk_preview_btn.pack(pady=(10, 2))
            self.bulk_tree.bind("<Double-Button-1>", self._bulk_tree_double_click)

        self.bulk_save_to_source_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            side, text="Save to\ncurrent folder", variable=self.bulk_save_to_source_var,
            bg="#f0f0f0", justify="left", anchor="w",
        ).pack(pady=(16, 2), anchor="w")

        footer = tk.Frame(root, bg="#f0f0f0")
        footer.grid(row=2, column=0, sticky="ew", padx=3, pady=(4, 2))
        tk.Label(
            footer,
            text="Bracket rule: 3 dot-separated numbers = HH.MM.SS, 2 numbers = MM.SS. "
                 "Multiple brackets create multiple clips. A seconds typo such as 01.25.272 "
                 "is normalized to 01.25.27. Files without a valid bracket are skipped.\n"
                 "Last minute: [-43.-51] cuts second 43 to second 51 of the last minute that "
                 "has them. Example: in a 20:10 long video, 20:51 is past the end, so it cuts "
                 "19:43 - 19:51. In a 20:55 long video it cuts 20:43 - 20:51.\n"
                 "To the end: [-27] cuts from second 27 of the last minute that has it to the "
                 "end of the video. Example: in a 20:10 long video it cuts 19:27 - 20:10.",
            bg="#f0f0f0", fg="#555555", wraplength=px(850), justify="left",
        ).pack(fill="x")

        mode_frame = tk.Frame(root, bg="#f0f0f0")
        mode_frame.grid(row=3, column=0, sticky="ew", padx=3, pady=(0, 2))
        self.bulk_cut_mode_var, self.bulk_cut_search_window_var, self.bulk_cut_search_window_entry = (
            self._build_cut_mode_controls(mode_frame, default_mode="classic")
        )

        self.bulk_btn = tk.Button(
            root, text="Start Bulk Cut!", height=2,
            font=("Segoe UI", 10, "bold"), command=self._bulk_start_clicked,
        )
        self.bulk_btn.grid(row=4, column=0, sticky="ew", padx=3, pady=(4, 6))

        self.bulk_tree.drop_target_register(DND_FILES)
        self.bulk_tree.dnd_bind("<<Drop>>", self._on_drop_bulk)
        # Only the blank area below the rows; headings keep column resizing.
        self._bind_window_drag(
            self.bulk_tree,
            lambda e: self.bulk_tree.identify_region(e.x, e.y) == "nothing",
        )

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

    def _bulk_add_row(self, path, ranges):
        """A row for ranges that are already known (sent from the Cut tab):
        valid (start, end) seconds, end after start. Returns its tree id."""
        return self._bulk_insert_row(path, list(ranges), bulk_ranges_text(ranges),
                                     bulk_lengths_text(ranges), "Pending")

    def _bulk_insert_row(self, path, ranges, range_txt, length_txt, status):
        with self._bulk_lock:
            # Added while a run is going: join its queue straight away.
            if status == "Pending" and self._bulk_running:
                status = "Queued"
            iid = self.bulk_tree.insert(
                "", "end", text=os.path.basename(path),
                values=(range_txt, length_txt, status),
            )
            self.bulk_items.append({
                "path": path, "ranges": ranges,
                "status": status, "iid": iid,
            })
        return iid

    # Removing a queued row takes it out of bulk_items, which is the list the
    # running worker picks its next item from -- so it is never processed.
    # A row that's already processing finishes its current ffmpeg run.
    def _bulk_remove_selected(self):
        sel = self.bulk_tree.selection()
        if not sel:
            return
        sel_set = set(sel)
        with self._bulk_lock:
            for iid in sel:
                self.bulk_tree.delete(iid)
            self.bulk_items = [it for it in self.bulk_items if it["iid"] not in sel_set]
        if self._bulk_preview_open() and self.bulk_preview.key in sel_set:
            self._bulk_close_preview()

    def _bulk_clear(self):
        with self._bulk_lock:
            for iid in self.bulk_tree.get_children():
                self.bulk_tree.delete(iid)
            self.bulk_items = []
        self._bulk_close_preview()

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

        if self.bulk_save_to_source_var.get():
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

        cut_mode = self.bulk_cut_mode_var.get()
        search_window = self._get_search_window_value(self.bulk_cut_search_window_var)

        with self._bulk_lock:
            for it in pending:
                it["status"] = "Queued"
                if self.bulk_tree.exists(it["iid"]):
                    self.bulk_tree.set(it["iid"], "status", "Queued")
            self._bulk_running = True

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

        tk.Button(
            header, text="Open Video...", command=self.open_file_for_resize
        ).pack(side="right", padx=(4, 10))

        self.resize_save_to_source_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            header, text="Save to current folder", variable=self.resize_save_to_source_var,
            bg="#f0f0f0", anchor="e",
        ).pack(side="right", padx=(10, 4))

        tk.Button(
            header, text="How does crop re-encoding work?",
            command=self._show_crop_info
        ).pack(side="right", padx=(4, 8))

        tk.Label(
            header,
            text="Crop must be re-encoded; audio is stream-copied when possible.",
            bg="#f0f0f0", fg="#555555"
        ).pack(side="right", padx=(10, 8))

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
        self._bind_window_drag(self.resize_canvas, lambda e: not self.resize_input_path)
        self._draw_resize_placeholder("Open or drop a video to begin")

        controls = tk.Frame(root, bg="#f0f0f0")
        controls.grid(row=2, column=0, sticky="ew", padx=3, pady=(2, 4))

        time_row = tk.Frame(controls, bg="#f0f0f0")
        time_row.pack(pady=(2, 2))

        self.resize_play_btn = None
        if mpv_available():
            self.resize_play_btn = tk.Button(time_row, text="▶ Play", width=8, state="disabled",
                                             command=self._resize_toggle_play)
            self.resize_play_btn.pack(side="left", padx=(0, 12))
        tk.Label(time_row, text="Frame:", bg="#f0f0f0").pack(side="left")
        self.resize_time_lbl = tk.Label(
            time_row, text="00:00:00", width=12, bg="#f0f0f0", font=("Consolas", 10)
        )
        self.resize_time_lbl.pack(side="left", padx=(4, 12))

        self.resize_size_lbl = tk.Label(
            time_row, text="Crop: —", bg="#f0f0f0", font=("Consolas", 10)
        )
        self.resize_size_lbl.pack(side="left")

        self.resize_slider = TimeSlider(controls, command=self._resize_slider_changed,
                                        on_release=self._resize_slider_released)
        self.resize_slider.pack(fill="x", padx=8, pady=(0, 4))

        button_row = tk.Frame(controls, bg="#f0f0f0")
        button_row.pack(pady=(0, 2))

        tk.Button(button_row, text="Reset Crop", command=self._resize_reset_crop).pack(
            side="left", padx=4
        )

        # All widescreen ratios first, then their vertical/portrait mirrors
        # (for Shorts/Reels/TikTok-style crops) in the same relative order
        # -- so 16:9/21:9 sit side by side, and 9:16/9:21 do too further
        # down the row, rather than interleaving landscape and portrait.
        # The highlighted button is the ratio corner drags keep; Freeform
        # lets corners distort the box (hold Shift/Ctrl/Alt to keep its
        # shape anyway).
        self.resize_aspect_buttons = {}
        for w, h in (
            (1, 1), (4, 3), (16, 9), (16, 10), (21, 9),   # widescreen
            (3, 4), (9, 16), (10, 16), (9, 21),           # vertical
        ):
            btn = tk.Button(
                button_row, text=f"{w}:{h}", width=4,
                command=lambda w=w, h=h: self._resize_set_aspect(w, h),
            )
            btn.pack(side="left", padx=1)
            self.resize_aspect_buttons[f"{w}:{h}"] = btn
        btn = tk.Button(button_row, text="Original", command=self._resize_set_original_aspect)
        btn.pack(side="left", padx=(4, 2))
        self.resize_aspect_buttons["original"] = btn
        btn = tk.Button(button_row, text="Freeform", command=self._resize_set_freeform)
        btn.pack(side="left", padx=(2, 2))
        self.resize_aspect_buttons["freeform"] = btn
        self._resize_button_default_bg = btn.cget("bg")
        self._resize_highlight_aspect_button()

        # Precise crop entry: type the four corner coordinates directly.
        # These are overwritten automatically whenever the crop changes via
        # dragging, the aspect buttons, or Reset Crop, so they always mirror
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

        # Zoom controls for fine-tuning the crop edges precisely.
        zoom_row = tk.Frame(controls, bg="#f0f0f0")
        zoom_row.pack(pady=(0, 2))

        tk.Label(zoom_row, text="Zoom:", bg="#f0f0f0").pack(side="left", padx=(4, 2))
        tk.Button(zoom_row, text="−", width=3, command=self._resize_zoom_out).pack(side="left")
        self.resize_zoom_lbl = tk.Label(
            zoom_row, text="100%", width=6, bg="#f0f0f0", font=("Consolas", 10)
        )
        self.resize_zoom_lbl.pack(side="left", padx=2)
        tk.Button(zoom_row, text="+", width=3, command=self._resize_zoom_in).pack(side="left")
        tk.Button(zoom_row, text="Fit", command=self._resize_zoom_reset).pack(
            side="left", padx=(6, 2)
        )
        tk.Label(
            zoom_row, text="(scroll wheel zooms · in Freeform, hold Shift/Ctrl/Alt "
            "while dragging to keep the box's shape)",
            bg="#f0f0f0", fg="#777777"
        ).pack(side="left", padx=(8, 2))

        self.resize_save_btn = tk.Button(
            controls, text="Crop & Save Video!", height=2,
            font=("Segoe UI", 10, "bold"),
            command=self._resize_save_clicked
        )
        self.resize_save_btn.pack(fill="x", padx=2, pady=(4, 2))
        self.resize_save_btn.config(state="disabled")

    def _show_crop_info(self):
        messagebox.showinfo(
            "How crop re-encoding works",
            "Why cropping can't be a simple copy, the way Cut and Combine are:\n\n"
            "Cut and Combine only ever touch whole compressed frames. Ffmpeg's "
            "stream-copy mode (-c copy) just repackages the existing H.264/H.265 "
            "bitstream into a new container between keyframes — no pixel in any "
            "frame is ever decoded or re-drawn, so it's instant and perfectly "
            "lossless.\n\n"
            "Cropping is different because it changes the pixels themselves. "
            "Compressed video stores frames as a grid of macroblocks plus "
            "motion vectors that reference *other* frames — the data that ends "
            "up at, say, pixel (400, 300) is smeared across blocks, transforms "
            "and predictions that only make sense at the video's original "
            "width/height. There is no way to just \"cut out\" a rectangle from "
            "that compressed representation; the only way to produce a video "
            "that is genuinely W×H pixels is to:\n\n"
            "  1. Decode every frame back to raw pixels.\n"
            "  2. Apply ffmpeg's crop filter (crop=W:H:X:Y) to keep only the "
            "pixels inside your selection.\n"
            "  3. Re-encode those cropped frames into a new H.264 stream.\n\n"
            "This app re-encodes using your GPU's hardware encoder when one is "
            "available (NVENC / Quick Sync / AMF, falling back to the CPU's "
            "libx264 otherwise), at a quality setting widely considered "
            "indistinguishable from the source at normal viewing distances, "
            "chosen to keep processing fast rather than maximally compressed. "
            "It is NOT bit-for-bit lossless the way Cut/Combine are, because "
            "re-encoding always involves at least some quantization.\n\n"
            "The pixel format is forced to yuv420p for the widest player/"
            "device compatibility.\n\n"
            "The audio track is NOT re-encoded — cropping is purely a video/"
            "spatial operation, so ffmpeg copies the existing audio stream "
            "straight into the output (-c:a copy), unchanged and lossless, "
            "exactly like the Cut tab does.\n\n"
            "Net effect: crop output is video-lossy (a small, generally "
            "invisible quality loss from the re-encode) but audio-lossless, "
            "and it will always take noticeably longer than Cut or Combine "
            "because every frame has to be fully decoded and re-encoded "
            "instead of just copied byte-for-byte."
        )

    def _draw_resize_placeholder(self, text):
        self.resize_canvas.delete("all")
        w = self.resize_canvas.winfo_width() or 700
        h = self.resize_canvas.winfo_height() or 400
        self.resize_canvas.create_text(
            w / 2, h / 2, text=text, fill="#888888",
            font=("Segoe UI", 12)
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

    def _load_video_for_resize(self, path):
        try:
            self.status_lbl.config(text="Reading video info...")
            self.update_idletasks()
            ffprobe = find_exe("ffprobe")

            duration_result = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, check=True, **SUBPROCESS_KWARGS
            )
            stream_result = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height",
                 "-of", "csv=p=0:s=x", path],
                capture_output=True, text=True, check=True, **SUBPROCESS_KWARGS
            )

            duration = float(duration_result.stdout.strip())
            dims = stream_result.stdout.strip().split("x")
            if len(dims) != 2:
                raise RuntimeError("Could not determine video dimensions.")
            src_w, src_h = int(dims[0]), int(dims[1])
        except Exception as exc:
            messagebox.showerror("Error loading video", str(exc))
            self.status_lbl.config(text="Ready")
            return

        self.resize_input_path = path
        self.resize_duration = duration
        self.resize_src_w = src_w
        self.resize_src_h = src_h
        self.resize_time_sec = 0.0
        self.resize_crop = (0, 0, src_w, src_h)
        self.resize_aspect_lock = (src_w, src_h)
        self.resize_aspect_key = "original"
        self._resize_highlight_aspect_button()
        self.resize_zoom = 1.0
        self._resize_update_zoom_label()

        self.resize_filename_lbl.config(text=os.path.basename(path))
        self.resize_info_lbl.config(text=f"{src_w}×{src_h}   [{seconds_to_hms(duration)}]")
        self.resize_slider.config(to=max(duration, 0.01))
        self.resize_slider.set(0)

        self.resize_save_btn.config(state="normal")
        self._resize_update_labels()
        self.resize_time_lbl.config(text=seconds_to_hms(0))
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
            volume, muted = self._player_audio_settings()
            self.resize_player.set_volume(volume)
            self.resize_player.set_muted(muted)
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
        self.resize_frame_img = None
        self.resize_canvas.delete("all")
        self._resize_bg_image_id = None
        self._resize_bg_cache_key = None
        if not on:
            self.resize_player.frame.place_forget()
            self.resize_player.hide_overlay()
            self.resize_play_btn.config(state="disabled", text="▶ Play")
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
        self.resize_play_btn.config(state="normal", text="▶ Play" if p.paused else "❚❚ Pause")
        pos = p.position
        if pos is None:
            return
        self.resize_time_sec = pos
        self.resize_time_lbl.config(text=seconds_to_ffmpeg_time(pos))
        if not self.resize_slider.dragging:
            self.resize_slider.set(pos)

    def _resize_player_ready(self):
        return self._resize_player_on and self.resize_player.loaded

    def _resize_toggle_play(self):
        if self._resize_player_ready():
            self.resize_player.toggle_pause()

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
        crop = "" if (sx0, sy0, sx1, sy1) == (0, 0, sw, sh) else f"{sx1-sx0}x{sy1-sy0}+{sx0}+{sy0}"
        view = (x, y, w, h, sx0, sy0, sx1, sy1)
        if view != self._resize_view:
            self._resize_view = view
            self.resize_player.set_video_crop(crop)
            self.resize_player.frame.place(x=x, y=y, width=w, height=h)

    def _resize_draw_player_overlay(self):
        """The crop box over the video, drawn by mpv: the same shading,
        outline, corner handles and size label the canvas draws."""
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
        events.append(f"{{\\an5\\pos({int(round((bx0 + bx1) / 2))},{int(round(by0 - px(14)))})"
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
        self.resize_time_lbl.config(text=seconds_to_hms(self.resize_time_sec))
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
        self.resize_frame_img = img
        self._redraw_resize_canvas()

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
                "Open or drop a video to begin"
                if not self.resize_input_path else "Loading frame..."
            )
            return

        geom = self._resize_canvas_geometry()
        if not geom:
            return

        scale, dx0, dy0, dw, dh = geom
        cache_key = (id(self.resize_frame_img), dw, dh)

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

            c.create_text(
                (px0 + px1) / 2, py0 - px(14),
                text=f"{x1-x0}×{y1-y0}",
                fill="#ffffff", font=("Segoe UI", 9, "bold"), tags=("overlay",)
            )
            if self._resize_player_on:
                self._resize_draw_player_overlay()

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

    def _resize_highlight_aspect_button(self):
        for key, btn in self.resize_aspect_buttons.items():
            active = key == self.resize_aspect_key
            self._configure_light(
                btn,
                relief="sunken" if active else "raised",
                bg="#cfe2ff" if active else self._resize_button_default_bg,
            )

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
        default_name = f"{base_name}.CROP.{crop_w}x{crop_h}.mp4"

        from_dialog = not self.resize_save_to_source_var.get()
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
        self.status_lbl.config(text="Cropping (re-encoding video)... 0%")
        crop = self.resize_crop
        threading.Thread(
            target=self._resize_run_crop,
            args=(out_path, crop),
            daemon=True
        ).start()

    def _resize_run_crop(self, out_path, crop):
        try:
            ffmpeg = find_exe("ffmpeg")
            x0, y0, x1, y1 = crop
            crop_w, crop_h = x1-x0, y1-y0

            # Arbitrary spatial cropping cannot be done with -c copy because the
            # encoded macroblocks/frames still contain the pixels outside the crop.
            # Use fast H.264 re-encoding (GPU-accelerated when available) while
            # copying the existing audio stream.
            vf = f"crop={crop_w}:{crop_h}:{x0}:{y0}"

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

    def _resize_done(self, info, success):
        self._end_progress("crop")
        self.resize_save_btn.config(state="normal")
        if success:
            self.status_lbl.config(text=f"Saved: {info}")
            messagebox.showinfo("Done", f"Cropped video saved to:\n{info}")
        else:
            self.status_lbl.config(text="Crop failed")
            messagebox.showerror("ffmpeg error", "The crop failed.\n\n" + info)

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
        tk.Button(
            header, text="Open Video...", command=self.open_file_for_audio
        ).pack(side="right", padx=(4, 10))

        self.audio_save_to_source_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            header, text="Save to current folder", variable=self.audio_save_to_source_var,
            bg="#f0f0f0", anchor="e",
        ).pack(side="right", padx=(10, 4))

        # ---- preview
        preview_frame = tk.Frame(root, bg="black")
        preview_frame.grid(row=1, column=0, sticky="nsew", padx=3, pady=4)
        self.audio_canvas = tk.Canvas(preview_frame, bg="black", highlightthickness=0)
        self.audio_canvas.pack(fill="both", expand=True)
        self.audio_canvas.bind("<Configure>", lambda e: self._redraw_audio_canvas())
        self._bind_window_drag(self.audio_canvas, lambda e: not self.audio_input_path)
        self._draw_audio_placeholder("Open or drop a video to begin")

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

        self.audio_slider = TimeSlider(controls, command=self._audio_slider_changed)
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
        self.audio_balance_btn = tk.Button(
            tracks_header, text="Auto Balance Audio", command=self._audio_auto_balance_clicked
        )
        self.audio_balance_btn.pack(side="right")
        self.audio_balance_btn.config(state="disabled")

        tracks_body = tk.Frame(root, bg="#1e1e1e")
        tracks_body.grid(row=4, column=0, sticky="nsew", padx=3, pady=2)
        tracks_body.rowconfigure(0, weight=1)
        tracks_body.columnconfigure(0, weight=1)

        tracks_scrollbar = ttk.Scrollbar(tracks_body)
        tracks_scrollbar.grid(row=0, column=1, sticky="ns")
        self.audio_tracks_canvas = tk.Canvas(
            tracks_body, bg="#1e1e1e", highlightthickness=0,
            yscrollcommand=tracks_scrollbar.set
        )
        self.audio_tracks_canvas.grid(row=0, column=0, sticky="nsew")
        tracks_scrollbar.config(command=self.audio_tracks_canvas.yview)

        self.audio_tracks_inner = tk.Frame(self.audio_tracks_canvas, bg="#1e1e1e")
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

        # ---- footer / finalize
        footer = tk.Frame(root, bg="#f0f0f0")
        footer.grid(row=5, column=0, sticky="ew", padx=3, pady=(4, 2))
        tk.Label(
            footer,
            text="Finalize keeps the video stream untouched (stream-copied, lossless). "
                 "Because the enabled tracks are mixed together and volume-adjusted, "
                 "the output audio must be re-encoded (AAC) — mixing separate compressed "
                 "audio streams into one isn't possible without decoding them.",
            bg="#f0f0f0", fg="#555555", wraplength=px(850), justify="left",
        ).pack(fill="x")

        self.audio_finalize_btn = tk.Button(
            root, text="Save enabled tracks together!", height=2,
            font=("Segoe UI", 10, "bold"), command=self._audio_finalize_clicked
        )
        self.audio_finalize_btn.grid(row=6, column=0, sticky="ew", padx=3, pady=(4, 6))
        self.audio_finalize_btn.config(state="disabled")

        self.audio_canvas.drop_target_register(DND_FILES)
        self.audio_canvas.dnd_bind("<<Drop>>", self._on_drop_audio)

    # ---- preview canvas drawing (same caching approach as the crop tab) --
    def _draw_audio_placeholder(self, text):
        c = self.audio_canvas
        c.delete("all")
        self._audio_bg_image_id = None
        self._audio_bg_cache_key = None
        w = c.winfo_width() or 700
        h = c.winfo_height() or 350
        c.create_text(w / 2, h / 2, text=text, fill="#888888", font=("Segoe UI", 12))

    def _redraw_audio_canvas(self):
        c = self.audio_canvas
        if not self.audio_frame_img:
            self._draw_audio_placeholder(
                "Open or drop a video to begin" if not self.audio_input_path else "Loading frame..."
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

    def _load_video_for_audio(self, path):
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
                 "-of", "csv=p=0", path],
                capture_output=True, text=True, check=True, **SUBPROCESS_KWARGS
            )
        except Exception as exc:
            messagebox.showerror("Error loading video", str(exc))
            self.status_lbl.config(text="Ready")
            return

        track_lines = [ln for ln in audio_result.stdout.strip().splitlines() if ln.strip()]
        if not track_lines:
            messagebox.showerror(
                "No audio tracks",
                "This video doesn't have any audio streams to mix."
            )
            self.status_lbl.config(text="Ready")
            return

        self._audio_stop_playback_state()
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
        for i, line in enumerate(track_lines):
            codec_name, channels, sample_rate = (line.split(",") + ["", "", ""])[:3]
            self.audio_tracks.append({
                "a_index": i,
                "codec": codec_name or "?",
                "channels": channels or "?",
                "sample_rate": sample_rate or "?",
                "enabled_var": tk.BooleanVar(value=True),
                "volume_slider": None,
                "volume_lbl": None,
                "name_label": None,
                "waveform_canvas": None,
                "waveform_img": None,
                "waveform_photo": None,
                "_wave_cache_key": None,
            })

        self._audio_rebuild_track_rows()
        self.audio_balance_btn.config(state="normal")
        self.audio_finalize_btn.config(state="normal")
        self._audio_update_preview(0)
        self.status_lbl.config(text="Ready")

    # ---- track rows / waveforms -----------------------------------------
    def _audio_rebuild_track_rows(self):
        for child in self.audio_tracks_inner.winfo_children():
            child.destroy()

        if not self.audio_tracks:
            tk.Label(
                self.audio_tracks_inner,
                text="  (open a video to see its audio tracks)",
                bg="#1e1e1e", fg="#888888", anchor="w", font=("Consolas", 10),
            ).pack(fill="x", ipady=16)
            return

        for track in self.audio_tracks:
            row = tk.Frame(self.audio_tracks_inner, bg="#2a2a2a")
            row.pack(fill="x", padx=4, pady=4)

            top = tk.Frame(row, bg="#2a2a2a")
            top.pack(fill="x", padx=3, pady=(4, 2))

            tk.Checkbutton(
                top, variable=track["enabled_var"], bg="#2a2a2a",
                activebackground="#2a2a2a", selectcolor="#3d8bfd",
                command=lambda t=track: self._audio_track_toggled(t),
            ).pack(side="left")

            label_text = (
                f"Track {track['a_index'] + 1}  ({track['codec']}, "
                f"{track['channels']}ch, {track['sample_rate']} Hz)"
            )
            name_lbl = tk.Label(
                top, text=label_text, bg="#2a2a2a", fg="white",
                font=("Consolas", 10), anchor="w"
            )
            name_lbl.pack(side="left", padx=(6, 12))
            track["name_label"] = name_lbl

            vol_frame = tk.Frame(top, bg="#2a2a2a")
            vol_frame.pack(side="right")
            tk.Label(
                vol_frame, text="Volume:", bg="#2a2a2a", fg="#cccccc"
            ).pack(side="left", padx=(0, 4))
            vol_lbl = tk.Label(
                vol_frame, text="100%", bg="#2a2a2a", fg="white",
                width=5, font=("Consolas", 10)
            )
            vol_lbl.pack(side="right")
            vol_slider = TimeSlider(
                vol_frame, command=lambda v, t=track: self._audio_volume_changed(t, v),
                width=160, height=22, bg="#2a2a2a", track_color="#555555",
            )
            vol_slider.config_range(to=2.0)
            vol_slider.set(1.0)
            vol_slider.pack(side="right", padx=(6, 6))
            track["volume_slider"] = vol_slider
            track["volume_lbl"] = vol_lbl

            wave_canvas = tk.Canvas(row, height=px(96), bg="#0a0a0a", highlightthickness=0)
            wave_canvas.pack(fill="x", padx=3, pady=(0, 6))
            wave_canvas.bind("<Configure>", lambda e, t=track: self._audio_redraw_waveform(t))
            wave_canvas.create_text(
                px(10), px(48), text="Generating waveform…", fill="#666666",
                anchor="w", font=("Segoe UI", 9), tags=("placeholder",)
            )
            track["waveform_canvas"] = wave_canvas
            track["wave_image_id"] = None
            track["playhead_id"] = None

            self._audio_generate_waveform(track)

    def _audio_track_toggled(self, track):
        enabled = track["enabled_var"].get()
        lbl = track.get("name_label")
        if lbl is not None:
            lbl.config(fg="white" if enabled else "#777777")
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
        scale = max(0.1, min(2.5, volume))
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

    # ---- playback ---------------------------------------------------------
    # The video preview only ever steps to a still frame every ~300ms (like
    # the rest of this app, there's no real-time video decode/render loop
    # here) -- deliberately low-fps and that's fine. Audio playback is real,
    # though: it's handed off to ffplay (bundled with ffmpeg) running with
    # the same volume/mix filter graph Finalize uses, muted video output
    # (-nodisp), started at the current scrub position on Play and killed on
    # Pause/seek/close.
    def _audio_slider_changed(self, value):
        if not self.audio_input_path:
            return
        was_playing = self.audio_playing
        self._audio_pause()
        self._audio_set_time(float(value))
        if was_playing:
            self._audio_play()

    def _audio_step(self, delta):
        if not self.audio_input_path:
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

    def _audio_toggle_play(self):
        if not self.audio_input_path:
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
        """track_settings: list of (a_index, volume) for the tracks to include.
        Shared by live playback (ffplay) and Finalize (ffmpeg) so what you
        hear while scrubbing matches what gets written to the output file."""
        n = len(track_settings)
        parts = [
            f"[0:a:{a_index}]volume={vol:.4f}[v{i}]"
            for i, (a_index, vol) in enumerate(track_settings)
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
        track_settings = [
            (t["a_index"], t["volume_slider"].get() if t["volume_slider"] else 1.0)
            for t in enabled
        ]
        filter_complex = self._audio_build_filter_complex(track_settings)
        # Some ffplay builds don't register -filter_complex at all (it fails
        # with "Option not found" and exits immediately, silently, since
        # stderr is suppressed) -- ffmpeg reliably supports it, so the mix is
        # done there and the resulting PCM is piped straight into ffplay just
        # for playback, rather than asking ffplay to do the mixing itself.
        ffmpeg_cmd = [
            ffmpeg, "-v", "quiet", "-ss", seconds_to_hms(start_sec),
            "-i", self.audio_input_path,
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
        # doesn't spawn a burst of ffplay processes -- only the settle point
        # after motion pauses triggers a restart, same pattern as the frame
        # preview debounce elsewhere in this app.
        if not self.audio_playing:
            return
        if self._audio_restart_after_id is not None:
            self.after_cancel(self._audio_restart_after_id)
        self._audio_restart_after_id = self.after(150, self._audio_restart_playback_now)

    def _audio_restart_playback_now(self):
        self._audio_restart_after_id = None
        if self.audio_playing:
            self._audio_start_ffplay(self.audio_time_sec)

    def _audio_stop_playback_state(self):
        self._audio_pause()
        self.audio_time_sec = 0.0

    def _audio_update_preview(self, at_seconds):
        if not self.audio_input_path or not PIL_AVAILABLE:
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
        self.status_lbl.config(text="Analyzing track loudness...")
        threading.Thread(target=self._audio_run_auto_balance, daemon=True).start()

    def _audio_run_auto_balance(self):
        try:
            ffmpeg = find_exe("ffmpeg")
            null_target = "NUL" if os.name == "nt" else "/dev/null"
            mean_re = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")
            means = {}
            n_tracks = len(self.audio_tracks)
            report = self._progress_reporter("Analyzing track loudness...", "audio_balance")
            for i, track in enumerate(self.audio_tracks):
                cmd = [
                    ffmpeg, "-i", self.audio_input_path,
                    "-map", f"0:a:{track['a_index']}",
                    "-af", "volumedetect", "-f", "null", null_target,
                ]
                proc = run_ffmpeg(
                    cmd, self.audio_duration, sub_progress(report, i / n_tracks, (i + 1) / n_tracks)
                )
                m = mean_re.search(proc.stderr)
                means[track["a_index"]] = float(m.group(1)) if m else None

            known = [v for v in means.values() if v is not None]
            if not known:
                raise RuntimeError("Could not measure loudness for any track.")
            # Target the average loudness across tracks -- quiet tracks get
            # boosted, loud ones get pulled down, so nothing dominates.
            target_db = sum(known) / len(known)

            gains = {}
            for a_index, mean_db in means.items():
                if mean_db is None:
                    gains[a_index] = 1.0
                    continue
                gain_db = max(-24.0, min(24.0, target_db - mean_db))
                gains[a_index] = max(0.0, min(2.0, 10 ** (gain_db / 20)))

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

        from_dialog = not self.audio_save_to_source_var.get()
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

        track_settings = [
            (t["a_index"], t["volume_slider"].get() if t["volume_slider"] else 1.0)
            for t in enabled
        ]

        self.audio_finalize_btn.config(state="disabled")
        self.status_lbl.config(text="Mixing audio tracks... 0%")
        threading.Thread(
            target=self._audio_run_finalize, args=(out_path, track_settings), daemon=True
        ).start()

    def _audio_run_finalize(self, out_path, track_settings):
        try:
            ffmpeg = find_exe("ffmpeg")
            filter_complex = self._audio_build_filter_complex(track_settings)
            cmd = [
                ffmpeg, "-y", "-i", self.audio_input_path,
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
            messagebox.showinfo("Done", f"Mixed video saved to:\n{info}")
        else:
            self.status_lbl.config(text="Audio mix failed")
            messagebox.showerror("ffmpeg error", "The audio mix failed.\n\n" + info)

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


if __name__ == "__main__":
    app = LosslessVideoApp()
    app.mainloop()
