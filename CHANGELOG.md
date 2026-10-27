# Changelog

What changed in each release. Newest first. "Unreleased" is everything
since the last release; it becomes the next version's notes (which the
app's update box shows as "What's new").

## Unreleased (since v1.1)

## v1.1 (2026-10-27)

### New

- **Linux support (beta).** Runs from source on Linux (tested on
  Ubuntu 24.04 under X11), with its own build:
  `BBVideoCutter-Linux-x86_64.tar.gz`, made by
  `release/build/build_linux.sh`. It needs the system's `ffmpeg` and
  `libmpv2`, and glibc 2.38 or newer. All the Linux-only code lives in
  `bbvc_linux.py`, which the Windows build leaves out, so the Windows app
  is unchanged by it.
  - Also as a package for Ubuntu 24.04 or later (and Debian-based distros
    as new): `BBVideoCutter-Linux-amd64.deb`, made by
    `release/build/build_deb.sh` from the same build.
    `sudo apt install ./BBVideoCutter-Linux-amd64.deb` installs it with
    ffmpeg and libmpv2, adds it to the app menu with its icon, and adds a
    `bbvideocutter` command.
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
    picked up the build's own libraries instead of the system's; the
    Cut tab's last step button (3 min >) cut off at the window's edge,
    as Linux's font is wider (the step buttons now narrow until their
    row fits); the Crop/Rotate tab's rotate buttons, wider on Linux,
    pushed the Zoom controls off the window, and at the default window
    width the Zoom "Fit" button was cut off (that row now tightens up
    when it doesn't fit); the window's class was Tk's
    default ("Tk"), so docks didn't match it to the app's menu entry and
    icon.

### Changed

- **Cut tab:** the Start and End time fields accept only digits, `:` and
  `.` (the milliseconds). A typed or pasted letter is refused as it is
  entered, instead of an "Invalid time" error afterwards.
- **Cut tab:** clicking a slider handle (the ball) only selects it, for
  the step buttons. It no longer jumps the video and the playhead to it,
  or rounds an exact time like `00:00:01.250` to the nearest second.
  Dragging the handle still moves it and shows its frame.
- **Cut tab:** the Start / End fields are labelled "Cut Start:" and
  "Cut End:", so they read as the cut's limits, not the playhead.
- **Cut tab:** each step button (1 sec, 5 sec, 30 sec, 3 min) has a short hint
  naming the marker it moves right now and by how much, e.g.
  "Cut start -> -5s".
- **Cut tab:** while you drag the playhead or a handle, the video shows
  the frame once the mouse rests for 5 ms, instead of seeking on every
  mouse move. Letting go still shows the exact frame.
- **Cut tab:** dragging a Start / End handle no longer moves the
  playhead. The video previews the handle's frame while you drag, and
  goes back to the playhead when you let go (and plays on, if it was
  playing).
- **Cut tab:** dragging a Start / End handle sets its time to the
  millisecond, like dragging the playhead, instead of snapping to whole
  seconds.
- **Cut tab:** the step buttons keep the milliseconds: 5 sec back from
  `00:00:12.345` is `00:00:07.345`, not `00:00:07.000`.
- **Cut tab:** the red playhead moves at once with the step buttons (and
  the time fields, and Go to Start / End), instead of a moment later when
  the video catches up. It also no longer flicks back to an old spot
  when you let go of a playhead or handle drag.

- **Cut tab:** turning on the frame-by-frame buttons no longer makes the
  step buttons (1 sec ... 3 min) smaller. Each button row now shrinks its
  buttons only when that row itself runs out of room.

- **Cut tab (to try out):** the small Cut Start / Cut End radio buttons
  were easy to miss. Each label and its time field now sit in one card,
  and the picked card is tinted, edged in blue and its name bold (Cut
  Start and Cut End alike); picking one moves nothing in the row.
  Options > Cut Start / Cut End picker offers two other looks: big radio
  circles, and outlined boxes (the picked one blue and underlined). The
  choice is remembered.
- **Crop/Rotate tab:** the picked rotate button (0° / 90° / 180° / 270°)
  looks like the Cut tab's picked marker: tinted, edged in blue and bold,
  instead of pressed in. Picking one moves none of them.
- **Cut tab:** scissors in front of Cut Start and Cut End, blue on the
  picked one. Options > Scissors on Cut Start / Cut End offers seven
  (the Cut tab box's icon, solid, outlined, upper blade, colour emoji,
  and two drawn ones: blades open or closed) and None.
- **Options:** the choices under Volume control look and Scissors have
  letters, in keyboard order (Q, W, E, R, ...): Alt+O, then V (or S),
  then a choice's letter switches to it.
- **Cut tab:** Mark Cut Start and Mark Cut End moved from the play row to
  either side of the Cut Start / Cut End fields: Mark Cut Start on the
  left, Mark Cut End on the right.
- **Cut tab:** clicking (or tabbing) into the Cut Start or Cut End time
  field picks that marker for the step buttons, so after typing a time
  you can fine-tune it with the step buttons straight away.
- **Volume controls (to try out):** Options > Volume control look offers
  15 looks for the speaker icon and the slider ball (Windows 11, white
  ball, ring, small dot, bar, square, diamond, pill, round badge, fill
  only, halo and more). The default is the look from before. It applies to
  the Cut and Crop/Rotate volume sliders and the Audio tab's track volume
  sliders, and the choice is remembered.
- **Sliders:** every slider's knob, and the round ends of its track, now
  have smooth edges instead of jagged ones (the Cut slider's Start / End
  balls too). Six more volume looks: macOS, iOS, Material 3, glossy ball,
  blue ball with a soft shadow, and a soft (neumorphic) knob.

- After a save (Cut, Combine, Crop/Rotate, Audio), the "saved" message
  also asks "Open the new video in the editor?". Yes opens it right away:
  on the tab it was saved from (Combine's on the Cut tab).
- The tab boxes on the menu bar are in a new order: Cut, Bulk Cut,
  Combine, Crop/Rotate, Audio (Bulk Cut and Combine swapped places).

### Project and docs

- README rewritten: screenshots, badges (release, Windows, Linux beta,
  GitHub stars), downloads on the releases page, Linux steps.
- Five BarkBark cutter logo options in `assets/logos/`, and
  `make_logos.py`, which draws them. `barkbark2.ico` stays the app icon.
- Tests: `tests/quiet_windows.py` keeps message boxes and file dialogs
  invisible during test runs too, and counts the windows it changed
  correctly.

### Fixed

- **Cut tab:** the red playhead no longer looks broken in two (the part
  on the waveform a few pixels off the part on the slider) while it moves
  fast. The slider and the waveform are now drawn as one, so the playhead
  through them is a single line.

## v1.0 (2026-10-26)

First public release: Windows installer (`BBVideoCutter-Setup.exe`) and
portable zip (`BBVideoCutter-Portable.zip`).
