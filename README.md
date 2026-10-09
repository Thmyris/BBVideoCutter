# BarkBark Lossless Video Cutter

A Windows desktop app for cutting, combining, cropping, and audio-mixing
video, keeping edits lossless wherever possible.

## Download

Two builds in `release/` (ffmpeg/ffprobe bundled, nothing else to install):

- **`release/BBVideoCutter-Setup.exe`** — Windows installer.
- **`release/BBVideoCutter-Portable.zip`** — extract and run
  `BBVideoCutter.exe`, no install.

## Features

- **Cut** — trim a clip, three modes: Lossless (fastest), Re-encode edges
  (frame-exact), Edit-list trim (experimental).
- **Combine** — join videos without re-encoding (same codec/format).
- **Bulk Cut** — batch-cut files by cut ranges in their filenames, e.g.
  `clip [01.10-01.25].mp4`. `[-43.-51]` cuts seconds 43–51 of the last
  minute that has them: 19:43–19:51 in a 20:10 video. `[-27]` cuts from
  second 27 of the last minute that has it to the end: 19:27–20:10.
- **Crop** — visual crop editor with widescreen/vertical aspect presets, and
  a 90°/180°/270° rotate done in the same re-encode.
- **Audio** — mute/mix individual audio tracks.

Built-in video player (libmpv): play and frame-step the video on the Cut
tab, with keyframe ticks and the audio waveform under the slider; preview
the joined video on Combine, a row's ranges on Bulk Cut, and the video
playing under the crop box on Crop.

Drag & drop supported throughout. Dark mode under **View**.

## Running from source

```
pip install pillow tkinterdnd2 python-mpv
python BBVideoCutter.py
```

Requires ffmpeg + ffprobe on `PATH`. On Windows, double-click `run.pyw`
to start it without a console window.

The player also needs `libmpv-2.dll` (from an mpv "dev" build for Windows
x86_64, e.g. shinchiro's `mpv-dev-x86_64-*.7z`) next to
`BBVideoCutter.py`, in `release/build/`, or on `PATH`. Without it the app
shows still frames instead.

## Building it yourself

Scripts in `release/build/` (PyInstaller onedir + optional Inno Setup
installer):

- `build_exe.bat` — quick test build
- `build_setup.bat` — installer → `release/BBVideoCutter-Setup.exe`
- `build_portable.bat` — zip → `release/BBVideoCutter-Portable.zip`

Drop `ffmpeg.exe`/`ffprobe.exe` and `libmpv-2.dll` into `release/build/`
first to bundle them.

## License

[MIT](LICENSE).
