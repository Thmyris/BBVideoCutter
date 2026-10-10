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
  (frame-exact), Edit-list trim (experimental). The mode is set in the
  **Cut** menu, for Bulk Cut too; choosing Re-encode edges or Edit-list
  trim opens a box for the keyframe search window (default 8 s).
  **Add selected scene to cutter list** marks several ranges in one video;
  **Cut selected ranges** cuts them all into separate clips, named the way
  Bulk Cut names them.
- **Combine** — join videos without re-encoding (same codec/format).
- **Bulk Cut** — batch-cut files by cut ranges in their filenames, e.g.
  `clip [01.10-01.25].mp4`. `[-43.-51]` cuts seconds 43–51 of the last
  minute that has them: 19:43–19:51 in a 20:10 video. `[-27]` cuts from
  second 27 of the last minute that has it to the end: 19:27–20:10.
- **Crop/Rotate** — visual crop editor with widescreen/vertical aspect presets, and
  a 90°/180°/270° rotate done in the same re-encode, or saved on its own
  losslessly (stream copy; only the file's rotation info changes).
- **Audio** — mute/mix individual audio tracks.

Built-in video player (libmpv): play and frame-step the video on the Cut
tab, with keyframe ticks and the audio waveform under the slider; preview
the joined video on Combine, a row's ranges on Bulk Cut, and the video
playing under the crop box on Crop/Rotate.

The Combine and Bulk Cut lists work the same way: a thumbnail per file,
Ctrl/Shift-click to select several rows, Delete to remove them, and Move
Up/Down or drag to reorder (the dragged rows follow the cursor, and a line
shows where they will land). Bulk Cut cuts the files in the list's order.

Drag & drop supported throughout. Dark mode under **View**. Under
**Options**: save to the source video's folder (on) or ask where to save,
for every tab; warn before overwriting; show the Cut tab's frame-step
buttons (the `,` and `.` keys step a frame either way).

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
- `build_portable_and_setup.bat` — both of the above, one after the
  other, from a single app build

Drop `ffmpeg.exe`/`ffprobe.exe` and `libmpv-2.dll` into `release/build/`
first to bundle them.

## License

[MIT](LICENSE).
