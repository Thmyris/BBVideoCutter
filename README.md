# BarkBark Lossless Video Cutter

A free Windows desktop app for cutting, combining, cropping, and audio-mixing
video while avoiding unnecessary re-encoding wherever ffmpeg stream copy
(`-c copy`) can do the job instead.

## Download

Two ready-to-use builds in `release/` (both bundle ffmpeg/ffprobe
internally — nothing else to install):

- **`release/BBVideoCutter-Setup.exe`** — standard Windows installer.
  Installs a Start Menu shortcut and a proper uninstaller in Add/Remove
  Programs.
- **`release/BBVideoCutter-Portable.zip`** — extract anywhere and run
  `BBVideoCutter.exe` directly. No install, no admin rights, nothing
  written outside that folder.

## Features

### Cut

Trims a clip out of a video. Three interchangeable cut modes (also
available on the Bulk Cut tab):

| Mode | Re-encoding | Accuracy |
|---|---|---|
| **Lossless** | None — pure stream copy | May land up to one keyframe interval early, fastest |
| **Re-encode edges** | Only the small boundary slivers, GPU-accelerated (NVENC/QSV/AMF, matched to the source's own codec) | Exact, to the millisecond |
| **Edit-list trim** *(experimental)* | None — patches the MP4's own edit-list metadata instead | Exact, but depends on the player honoring that metadata (some, like VLC, are less consistent about it) |

Start/end times can be typed with millisecond precision (`HH:MM:SS.mmm`);
dragging the slider or clicking a step button always rounds to a whole
second instead, so repeated attempts at "the same second" land on the
same result every time.

### Combine

Joins multiple videos together, also without re-encoding, as long as they
share the same codec/format. Goes through an MPEG-TS intermediate step
rather than feeding files straight into an MP4 concat, avoiding a
well-known bug where playback freezes on a frame while audio keeps
playing after the first file ends.

### Bulk Cut

Batch version of the Cut tab: drop in files whose names contain one or
more cut ranges (e.g. `clip [00.00.00-00.00.17].mp4`) and cut them all in
one pass, using whichever cut mode is selected.

### Crop

Visual crop editor with aspect-ratio presets — widescreen (`16:9`,
`16:10`, `21:9`, `4:3`, `1:1`) and their vertical/portrait mirrors
(`9:16`, `10:16`, `9:21`, `3:4`) for Shorts/Reels/TikTok-style crops.
Arbitrary spatial cropping can't be done with stream copy, so this tab
always re-encodes the video (GPU-accelerated when available) while
copying the audio through untouched.

### Audio

Enable/mute individual audio tracks in a file and mix the enabled ones
down together into one track. The video stream is always stream-copied
(untouched, lossless); only the audio is re-encoded, since mixing
multiple tracks into one requires decoding them first.

---

Every tab that saves a file has a **"Save to current folder"** checkbox
that skips the save dialog and writes the output right next to the
source. Drag & drop is supported throughout.

## Running from source

```
pip install pillow tkinterdnd2
python BBVideoCutter.py
```

Requires ffmpeg + ffprobe on your system `PATH`:

- Windows: [gyan.dev builds](https://www.gyan.dev/ffmpeg/builds/) (unzip, add `bin` to PATH)
- macOS: `brew install ffmpeg`
- Linux: `sudo apt install ffmpeg`

## Building it yourself

All build tooling lives in `release/build/`. Each script builds a
PyInstaller **onedir** app (a folder, not a single self-extracting exe,
so it starts up instantly regardless of how much is bundled) using
`assets/barkbark2.ico` as the icon, then cleans up its own intermediate
files afterward. Drop `ffmpeg.exe`/`ffprobe.exe` into `release/build/`
before running any of them to have ffmpeg bundled in automatically;
otherwise the built app falls back to requiring them on `PATH`.

- `release/build/build_exe.bat` — quick onedir build for testing, output
  stays at `release/build/dist/BBVideoCutter/`
- `release/build/build_setup.bat` — builds the installer with
  [Inno Setup](https://jrsoftware.org/isinfo.php) (`installer.iss`),
  copies the result to `release/BBVideoCutter-Setup.exe`
- `release/build/build_portable.bat` — zips the onedir build, copies the
  result to `release/BBVideoCutter-Portable.zip`

## License

[MIT](LICENSE) — do what you want with it, just keep the copyright
notice.

## Credits

Built with Python by Thmyris.
