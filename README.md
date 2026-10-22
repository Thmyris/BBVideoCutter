# BarkBark Lossless Video Cutter

A Windows desktop app for cutting, combining, cropping, and audio-mixing
video, keeping edits lossless wherever possible.

## Download

Two builds in `release/` (ffmpeg/ffprobe bundled, nothing else to install):

- **`release/BBVideoCutter-Setup.exe`** — Windows installer.
- **`release/BBVideoCutter-Portable.zip`** — extract and run
  `BBVideoCutter.exe`, no install.

## Features

- **Cut** — trim a clip, keeping every audio track, three modes: Lossless (fastest), Re-encode edges
  (frame-exact), Edit-list trim (experimental). The mode is set in the
  **Cut** menu, for Bulk Cut too; choosing Re-encode edges or Edit-list
  trim opens a box for the keyframe search window (default 8 s).
  **Mark Start** / **Mark End** (next to Play; keys `I` / `O`) set the
  start / end at the frame on screen, and **Replay selected part** (under
  the volume) plays Start to End once.
  **Add selected scene to the bulk cutting list** (key `A`) puts the
  Start–End on the Bulk Cut list, on the video's own row, and marks it
  green on the slider; **Cut selected scenes** goes to the Bulk Cut tab and
  starts the bulk cut there, each scene into its own clip.
- **Combine** — join videos without re-encoding when they share codec,
  resolution, frame rate and audio tracks. Mixed videos are re-encoded to
  match the first one instead (H.264 + AAC, others scaled to fit with black
  bars; a video with fewer audio tracks gets silence on the others); a note
  under the list says when and why, and so does the message when it's done.
- **Bulk Cut** — batch-cut files by cut ranges in their filenames, e.g.
  `clip [01.10-01.25].mp4`. `[-43.-51]` cuts seconds 43–51 of the last
  minute that has them: 19:43–19:51 in a 20:10 video. `[-27]` cuts from
  second 27 of the last minute that has it to the end: 19:27–20:10.
- **Crop/Rotate** — visual crop editor with widescreen/vertical aspect presets, and
  a 90°/180°/270° rotate done in the same re-encode, or saved on its own
  losslessly (stream copy; only the file's rotation info changes).
- **Audio** — mute/mix individual audio tracks.

Built-in video player (libmpv): play and frame-step the video on the Cut
tab, with keyframe ticks and the audio waveform under the slider; a video's
audio tracks play together, or pick one under **Audio** next to the
volume; preview the joined video on Combine, a row's ranges on Bulk Cut,
the video playing under the crop box on Crop/Rotate, and on Audio the
video with the enabled tracks mixed at their volumes, as Finalize saves
them.

The Combine and Bulk Cut lists work the same way: a thumbnail per file,
Ctrl/Shift-click to select several rows, Delete to remove them, and Move
Up/Down or drag to reorder (the dragged rows follow the cursor, and a line
shows where they will land). Bulk Cut cuts the files in the list's order.

Drag & drop supported throughout. Dark mode under **View**. Under
**Options**: save to the source video's folder (on) or ask where to save,
for every tab; warn before overwriting; show the frame by frame movement
buttons in the video player on the Cut tab: they move the selected Start
or End one frame, to the millisecond (the `,` and `.` keys do it either
way).

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
