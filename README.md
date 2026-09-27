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
- **Bulk Cut** — batch-cut files by cut ranges in their filenames.
- **Crop** — visual crop editor with widescreen/vertical aspect presets.
- **Audio** — mute/mix individual audio tracks.

Drag & drop supported throughout. Dark mode under **View**.

## Running from source

```
pip install pillow tkinterdnd2
python BBVideoCutter.py
```

Requires ffmpeg + ffprobe on `PATH`.

## Building it yourself

Scripts in `release/build/` (PyInstaller onedir + optional Inno Setup
installer):

- `build_exe.bat` — quick test build
- `build_setup.bat` — installer → `release/BBVideoCutter-Setup.exe`
- `build_portable.bat` — zip → `release/BBVideoCutter-Portable.zip`

Drop `ffmpeg.exe`/`ffprobe.exe` into `release/build/` first to bundle them.

## License

[MIT](LICENSE).
