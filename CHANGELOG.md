# Changelog

What changed in each release. Newest first. "Unreleased" is everything
since the last release; it becomes the next version's notes (which the
app's update box shows as "What's new").

## Unreleased (since v1.1)

## v1.1 (2026-10-27)

### New

- **Linux (beta):** a `.deb` package for Ubuntu 24.04+ and a `.tar.gz`
  for other distros. Needs `ffmpeg` and `libmpv2` (the `.deb` installs
  them).
- After saving, you can open the new video right away.
- Scissors on Cut Start / Cut End (Options > Scissors to pick a style).

### Changed

- **Cut tab:** Cut Start / Cut End are now cards round their time fields;
  the picked one is blue. Clicking a time field picks it too.
- **Cut tab:** Mark Cut Start / Mark Cut End sit beside their fields.
- **Cut tab:** smoother, more exact dragging and stepping: times keep
  their milliseconds, and the video previews a handle while you drag it.
- New volume control look, and smooth (anti-aliased) sliders and icons.
- Tabs reordered: Cut, Bulk Cut, Combine, Crop/Rotate, Audio.

### Fixed

- The red playhead no longer looks split in two while moving.
- Buttons no longer get cut off in small windows.

## v1.0 (2026-10-26)

First public release: Windows installer (`BBVideoCutter-Setup.exe`) and
portable zip (`BBVideoCutter-Portable.zip`).
