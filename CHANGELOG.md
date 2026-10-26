# Changelog

What changed in each release. Newest first. "Unreleased" is everything
since the last release; it becomes the next version's notes (which the
app's update box shows as "What's new").

## Unreleased (since v1.0)

### New

- **Linux support (beta).** Runs from source on Linux (tested on
  Ubuntu 24.04 under X11), with its own build:
  `BBVideoCutter-Linux-x86_64.tar.gz`, made by
  `release/build/build_linux.sh`. It needs the system's `ffmpeg` and
  `libmpv2`, and glibc 2.38 or newer. All the Linux-only code lives in
  `bbvc_linux.py`, which the Windows build leaves out, so the Windows app
  is unchanged by it.
  - Uses the system's libmpv for the players, and works on machines
    without a GPU driver (VMs, remote desktops): when OpenGL runs on the
    CPU, the players switch to plain X11 output, which draws correctly.
  - Mouse wheel and clicks reach the app over the video, as on Windows.
  - Medium / Extreme noise reduction (deep-filter) works, bundled in the
    build or found on PATH.
  - The built Linux app can't update itself yet; when a new version is
    out, it opens the release page.
  - Fixed during testing, before any release: a crash when a video was
    opened on the Crop/Rotate or Audio tab first (the player started
    before its window existed on the X server); in the build, waveforms
    that didn't draw (a Pillow module was left out) and an ffmpeg that
    picked up the build's own libraries instead of the system's.

### Changed

- **Cut tab:** the Start and End time fields accept only digits, `:` and
  `.` (the milliseconds). A typed or pasted letter is refused as it is
  entered, instead of an "Invalid time" error afterwards.

### Project and docs

- README rewritten: screenshots, badges (release, Windows, Linux beta,
  GitHub stars), downloads on the releases page, Linux steps.
- Five BarkBark cutter logo options in `assets/logos/`, and
  `make_logos.py`, which draws them. `barkbark2.ico` stays the app icon.
- Tests: `tests/quiet_windows.py` keeps message boxes and file dialogs
  invisible during test runs too, and counts the windows it changed
  correctly.

## v1.0 (2026-10-26)

First public release: Windows installer (`BBVideoCutter-Setup.exe`) and
portable zip (`BBVideoCutter-Portable.zip`).
