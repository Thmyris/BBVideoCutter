# BarkBark Lossless Video Cutter

The fastest and the most stable video editor for your simple video editing needs. Built with ffmpeg and mpv.

## Download

Two builds in `release/`

- **`release/BBVideoCutter-Setup.exe`** — Windows installer.
- **`release/BBVideoCutter-Portable.zip`** — extract and run
  `BBVideoCutter.exe`, no install.

## Features

**Cut:** Losslessly cut any video down to the milisecond easily. Cuts are <u>instant</u>, the only bottleneck is the disk write speed.

**Combine:** Join the cut videos together losslessly.

**Bulk Cut:** Batch cut hundreds of files with one click.

**Crop/Rotate:** Self explanatory.

**Audio:** Mix multiple tracks together while auto-balancing the dB levels with one click. Mute, adjust volume of a voice track. Has a simple noise cancellation filter with 3 strength levels.



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
shows still frames instead (on the Audio tab with the mix played by
ffplay).

Medium / Extreme noise reduction needs `deep-filter.exe` (DeepFilterNet's
`deep-filter-*-x86_64-pc-windows-msvc.exe` from its GitHub releases,
renamed) in the same places; without it those two are greyed out. Light
uses the RNNoise model in `assets/rnnoise/` (`cb.rnnn`, from
GregorR/rnnoise-models).

## Building it yourself

Scripts in `release/build/` (PyInstaller onedir + optional Inno Setup
installer):

- `build_exe.bat` — quick test build
- `build_setup.bat` — installer → `release/BBVideoCutter-Setup.exe`
- `build_portable.bat` — zip → `release/BBVideoCutter-Portable.zip`
- `build_portable_and_setup.bat` — both of the above, one after the
  other, from a single app build

Drop `ffmpeg.exe`/`ffprobe.exe`, `libmpv-2.dll` and `deep-filter.exe`
into `release/build/` first to bundle them. The RNNoise model is always
bundled.

## License

[MIT](LICENSE).
