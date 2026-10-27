# Testing with hidden windows

How to run GUI tests, probes and screenshot scripts for this app **without
the user seeing, hearing or feeling them**. The user works on this PC while
tests run. A test that flashes a window, takes the keyboard focus or plays a
sound has failed, even if every check passes.

Everything below was checked on Windows 10 with Python 3.12 and Tk 8.6 unless
it says otherwise.

## The rules

1. **No window on the user's screen.** Every window a test opens is fully
   transparent (alpha 0) or sits on the second monitor.
2. **No focus change.** Nothing a test starts may become the foreground
   window or take the keyboard, including windows of child processes.
3. **No sound** on the default audio output.
4. **Never touch the user's real settings, files or app windows.**
5. **Never use** `-topmost`, `lift()`, `focus_force()`, `grab_set_global`, or
   `geometry("...+0+0")` in a test.
6. **Kill only your own processes**: list them, prove they're yours by
   command line or parent, then kill by PID. Never kill by name and never
   use `/T`. The user's own Python apps look just like a test.
7. **End every test with a focus check** (`quiet.foreground_is_ours()` must be
   False). Treat it as a real check that can fail.

## The skeleton

Every Windows GUI test starts like this. The order matters.

```python
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# 1. Private settings: the app reads %APPDATA%\BBVideoCutter\settings.json.
#    Point APPDATA elsewhere BEFORE importing the app.
os.environ["APPDATA"] = os.path.join(HERE, "fake_appdata")
SETTINGS = os.path.join(os.environ["APPDATA"], "BBVideoCutter", "settings.json")
os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
with open(SETTINGS, "w") as f:
    json.dump({}, f)                     # or e.g. {"dark_mode": True}

# 2. No sound from ffplay (the Audio tab's playback): SDL's dummy driver.
#    Child processes inherit os.environ.
os.environ["SDL_AUDIODRIVER"] = "dummy"

sys.path.insert(0, r"C:\work\BBVideoCutter")
sys.path.insert(0, r"C:\work\BBVideoCutter\tests")

# 3. Hooks BEFORE the first tk.Tk() (the app's main window is one).
import quiet_windows                     # noqa: E402
quiet = quiet_windows.install()

import BBVideoCutter as m                # noqa: E402

# 4. No sound from the embedded mpv player.
m.VideoPlayer.extra_options = {"ao": "null"}

fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + str(detail) if detail != "" else ""))
    if not cond:
        fails.append(name)


app = m.LosslessVideoApp()


def settle(sec=0.0):
    """Runs Tk's queue; sec > 0 also waits that long for timers."""
    end = time.time() + sec
    while True:
        app.update()
        if time.time() >= end:
            return
        time.sleep(0.005)


settle()
# ... the test ...
check("focus never ours", not quiet.foreground_is_ours())
app._on_app_close()                      # the app's own shutdown (player, threads)
quiet.uninstall()
print("FAILED:" if fails else "ALL PASSED", fails)
```

Never call `app.mainloop()`. Drive Tk yourself with `settle()` / `app.update()`
so the test can't hang forever in the loop.

## What `tests/quiet_windows.py` does

`install()` must run before the first `tk.Tk()`. It returns a `Quiet` object.
Everything it hooks is **this thread only**: windows made by other threads
or other processes are not covered.

| Part | How | Covers |
|---|---|---|
| No activation | `WH_CBT` hook: `HCBT_ACTIVATE` returns 1 (refused) | Tk showing a window, `focus_force`, a click on it |
| No-activate style | Same hook, on `HCBT_CREATEWND`: `WS_EX_NOACTIVATE` added to every top-level (non-`WS_CHILD`) window | Also means no taskbar button |
| Invisible Tk windows | `tk.Tk.__init__` and `tk.Toplevel.__init__` patched: `-alpha 0.0` right after creation (Tk maps on the next idle pass, so this is in time) | Every root and Toplevel |
| Parking | Same patch: `geometry("+x+y")` 60 px inside the first non-primary monitor, worked out again per window, because the app turns DPI awareness on as it starts | Lands off the working screen |
| Invisible native dialogs | `WH_CALLWNDPROC` hook: on `WM_SHOWWINDOW` of class `#32770`, adds `WS_EX_LAYERED \| WS_EX_TRANSPARENT` and sets alpha 0 | `tkinter.messagebox`, the native file dialogs |

Why native dialogs are handled on `WM_SHOWWINDOW`: `SetWindowLongW` refuses
`WS_EX_LAYERED` while a window is still being created, so the CBT hook can't
set it. `WS_EX_NOACTIVATE` *is* accepted at creation; the hook reads the
style back to count it (`quiet.styled`).

The windows are still **mapped**: they have their real size, get
`<Configure>` events and lay out exactly as on screen. Layout checks with
`winfo_width()`, `winfo_rootx()` and so on are valid.

Counters on the `Quiet` object: `styled`, `refused` (activations turned
down), `dialogs` (native dialogs hidden). `quiet.describe()` gives a
one-line summary.

To watch a run, set `BBVC_SHOW_TESTS=1`. Windows then stay visible on the
second monitor, still without taking the focus, and native dialogs are not
hidden. Use this only when the user asks to watch.

On Linux `install()` does nothing (it only records that); see the Linux
section.

## What is NOT covered, and what to do

### Other processes
The hooks are per thread, so anything the app or the test starts as a
separate process is on its own:

- **ffmpeg / ffprobe**: the app passes `SUBPROCESS_KWARGS`
  (`CREATE_NO_WINDOW`) on Windows, so they're fine.
- **ffplay** (Audio tab playback, `_audio_start_ffplay`): `-nodisp`, so there
  is no window. It **does play sound**, so set `SDL_AUDIODRIVER=dummy` (see
  the skeleton).
- **Update helper** (`update_helper_script`): a PowerShell WinForms progress
  window. Generate it with `visible=False` (`Opacity 0`). It is shown with
  `SW_SHOWNOACTIVATE` either way. Run it with
  `powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass ...`.
- **The browser**: `webbrowser.open`, used when an update can't be
  installed and the app offers the release page (`_update_offer_page`).
  Monkeypatch it. `m.webbrowser` doesn't exist (it's imported inside the
  function), so patch `webbrowser.open` itself before the call.
- **Installers / a relaunched app**: never run the real installer in a test.
  The update test used `pythonw.exe -c "import time; time.sleep(1)"` as a
  stand-in process for the installer and for the app PID the helper waits on.

### Threads
A Tk window made from a worker thread wouldn't be hooked. The app only
touches Tk from the main thread; keep tests that way too.

### Finding windows by title
The user may have **their own copy of the app open, with the same title**.
When a test looks windows up by title (`EnumWindows`, `FindWindowW`), also
filter by process ID (`GetWindowThreadProcessId` equals `os.getpid()` or
the helper's `proc.pid`). One test once counted the user's open v1.0 window.

## Dialogs

### `tkinter.messagebox` and `filedialog`: monkeypatch them
Default choice. Patch them on the module the app imported them into
(`m.messagebox`, `m.filedialog`), and record the calls:

```python
boxes = []
m.messagebox.showinfo = lambda title, text, **k: boxes.append(("info", title, text))
m.messagebox.showerror = lambda title, text, **k: boxes.append(("error", title, text))
m.messagebox.askyesno = lambda title, text, **k: boxes.append(("ask", title, text)) and False
m.filedialog.asksaveasfilename = lambda **k: out_path     # "" means Cancel
```

To skip a save dialog entirely, the Cut tab has `app.save_to_source_var`:
`True` saves next to the source with no dialog.

To stop a save before any work starts, patch the step that comes right
before it, e.g. `app._check_output_path = lambda path, *a, **k: reached.append(path)`
(returns None, so the save stops there).

### A real native box, when the test is about the box itself
For example, a picture of the About box's title-bar icon. `quiet_windows`
keeps it at alpha 0, and it is modal: the call blocks until it is closed.
Tested facts:

- **Tk's `after()` timers keep running while a native box is open** (10
  ticks a second at `after(100)`). A thread works too.
- **`WM_COMMAND IDOK` does not close a Tk `showinfo` box**: the box stays
  open and the test hangs. These close it: `PostMessageW(hwnd, WM_CLOSE, 0, 0)`,
  `PostMessageW(hwnd, WM_COMMAND, IDCANCEL, 0)`, or `BM_CLICK` sent to
  `GetDlgItem(hwnd, 2)`. Use `WM_CLOSE`.
- Find the box with `FindWindowW("#32770", title)`, or `EnumWindows`
  filtered by your PID.
- Always add a watchdog, so a box that never closes can't leave a hung
  process:

```python
import threading
threading.Thread(target=lambda: (time.sleep(25), print("WATCHDOG", flush=True), os._exit(3)),
                 daemon=True).start()
```

### The app's own Tk dialogs (Toplevel + `wait_window`)
They block the same way, but Tk keeps running inside `wait_window`, so
schedule the inspection with `app.after()` before the call that opens the
dialog. The app keeps a reference to each one, marked `# for the tests`:

| Attribute | Dialog |
|---|---|
| `app._update_dialog`, `app._update_notes`, `app._update_auto_var` | Update available |
| `app._search_window_dialog` | Keyframe search window |
| `app._refuse_dialog` | A refused save (the whole video, or 0.000 s) |
| `app._cut_ranges_menu_widget` | The scene list menu |

```python
def try_save():
    seen, finished = {}, []
    t0 = time.time()

    def look():
        if finished:                        # this step is over: see below
            return
        dlg = getattr(app, "_refuse_dialog", None)
        if dlg is None:                     # not open yet: try again, for 3 s
            if time.time() - t0 < 3:
                app.after(50, look)
            return
        dlg.update()
        seen["title"] = dlg.title()
        ...                                 # read labels, take a picture
        dlg.destroy()                       # or .invoke() its button

    app.after(50, look)
    app._save_scene_clicked()               # returns once the dialog is closed
    settle(0.1)
    finished.append(True)
    return seen
```

- **Time limit:** stop rescheduling after a few seconds, so a dialog that
  never opens (the case where no box is the right answer) doesn't loop
  forever.
- **Stop each step's checker when the step ends** (`finished`). When a step
  opens no dialog, its `look` is still queued after the step returns. In the
  next step it finds *that* step's dialog, records into the old `seen` and
  closes it, so the new step sees nothing and fails. This happened.
  `after_cancel` on the first id doesn't help, because each reschedule has a
  new id.

When you add a new dialog to
the app, store it the same way (`self._x_dialog = dlg  # for the tests`,
then `None` after `wait_window`).

## Loading a video and waiting

```python
app._load_video_for_cut(path)
t0 = time.time()
while not (app.input_path and app._cut_player_on) and time.time() - t0 < 15:
    settle(0.02)
settle(0.3)
```

Other tabs load with `_load_video_for_resize(path)` (Crop/Rotate) and
`_load_video_for_audio(path)` (Audio). Tabs other than Cut are built lazily:
call `app._ensure_tab_built(i)` first, then `app.notebook.select(i)`. The
indices are the notebook's (0 Cut, 1 Combine, 2 Bulk Cut, 3 Crop/Rotate,
4 Audio), not the order of the tab buttons on screen.

Always wait with a time limit, never `while not ready: settle()`.

Theme switch:

```python
app.dark_mode_var.set(True)
app._on_dark_mode_toggled()
settle(0.1)
```

## Test media

Make test media with ffmpeg rather than using the user's videos: short,
known contents, with keyframes where you want them. For example, 6 s at
640x360, 30 fps, a keyframe every second, and a tone:

```
ffmpeg -v error -f lavfi -i testsrc2=d=6:s=640x360:r=30 -f lavfi -i sine=f=440:d=6:sample_rate=48000 -c:v libx264 -g 30 -pix_fmt yuv420p -c:a aac -shortest a.mp4
```

Keep generated media and outputs in the scratchpad, not in the repo.
Never write into the folders of the user's own videos: a cut saves next to
its source by default.

## Pictures of hidden windows

`PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)` renders a window off screen,
even at alpha 0. It works for Tk windows, native `#32770` boxes, and mpv's
video inside the Tk frame. A screen grab (`ImageGrab`, `BitBlt` from the
screen) does **not**: it would capture what is behind the hidden window.

```python
import ctypes
import time
from ctypes import wintypes
from PIL import Image

user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
PW_RENDERFULLCONTENT = 2


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


def grab_window(hwnd):
    """The whole window (title bar included) and its screen (left, top)."""
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    screen = user32.GetDC(0)
    dc = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    old = gdi32.SelectObject(dc, bmp)
    user32.PrintWindow(hwnd, dc, PW_RENDERFULLCONTENT)
    info = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(dc, bmp, 0, h, buf, ctypes.byref(info), 0)
    gdi32.SelectObject(dc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(dc)
    user32.ReleaseDC(0, screen)
    return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1), (rect.left, rect.top)


def grab_widget(widget, pad=(10, 6)):
    """One Tk widget (with a margin), as drawn in its hidden window."""
    top = widget.winfo_toplevel()
    top.update_idletasks()
    top.update()
    hwnd = user32.GetAncestor(int(top.frame(), 16), 2) or int(top.frame(), 16)   # GA_ROOT
    # PrintWindow returns what the compositor last had, which can be a
    # refresh or two behind what Tk just drew: grab until two grabs match.
    img = None
    for _ in range(12):
        ctypes.windll.dwmapi.DwmFlush()
        new, (left, top_y) = grab_window(hwnd)
        if img is not None and new.tobytes() == img.tobytes():
            break
        img = new
        time.sleep(0.017)
    x0 = widget.winfo_rootx() - left - pad[0]
    y0 = widget.winfo_rooty() - top_y - pad[1]
    return img.crop((x0, y0, x0 + widget.winfo_width() + 2 * pad[0],
                     y0 + widget.winfo_height() + 2 * pad[1]))
```

- For a whole Tk window including its title bar: `grab_window(GetAncestor(int(top.frame(), 16), 2))`.
- For a native box: `grab_window(hwnd)` with the hwnd from `FindWindowW`.
- Small details (an icon, a glyph): crop and scale up with
  `Image.NEAREST` before looking at the picture.
- Look at every picture you take before reporting the result.

## Layout gotchas

- **A withdrawn window's widgets are 1 px wide.** The app builds dialogs
  withdrawn, then measures and shows them. Anything measured before the
  first real `<Configure>` (e.g. a Text widget's wrapped line count) is
  wrong. Measure in a `<Configure>` handler that ignores `event.width < 50`,
  as `_ask_update` does.
- **DPI.** The app makes itself system-DPI-aware and scales raw pixel sizes
  with `px()`. Compare sizes to `px(n)`, not to bare numbers.
- **Smallest window.** Layout tests usually also run at the minimum size
  (`app.minsize()`), where rows are most likely to overflow.
- **Linux is wider.** Buttons and fonts are wider on Linux. A row that fits
  on Windows can overflow there; test both when the layout is tight.

## Running tests

- Write each test as a file (scratchpad), run it with `python <file>`. Don't
  use bash heredocs: they eat backslashes and have corrupted files.
- Use a time limit on every run: `timeout 90 python -u test_x.py` in Git Bash
  (`-u`: output isn't lost if the timeout kills it).
- Add `PYTHONIOENCODING=utf-8` when the output is piped: the app's text has
  characters like `−` and `▶`, which crash `print` with `UnicodeEncodeError`
  under the cp1252 pipe encoding. That is a test-harness error, not an app
  failure.
- After a timeout, check nothing was left running:
  `Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*test_x*' }`
  and kill only those PIDs.
- A test that fails because the test itself is outdated (the UI changed on
  purpose) gets fixed, and the fix reported.

## Linux (WSL + Xvfb)

`quiet_windows` does nothing on Linux. There, the whole X server is
private:

- WSL distro `Ubuntu-24.04`, as root. Python env: `/opt/bbvc-venv/bin/python`.
  The repo is at `/mnt/c/work/BBVideoCutter`.
- Start a private X server and point at it; no Windows window appears (not
  even through WSLg):

```
unset WAYLAND_DISPLAY
export PULSE_SERVER=unix:/nonexistent SDL_AUDIODRIVER=dummy PYTHONDONTWRITEBYTECODE=1
Xvfb :77 -screen 0 1600x1000x24 >/dev/null 2>&1 &
XPID=$!
sleep 1
export DISPLAY=:77
timeout 400 /opt/bbvc-venv/bin/python test_x.py
kill $XPID
```

- `PULSE_SERVER=unix:/nonexistent` stops anything reaching WSLg's PulseAudio
  (which plays on the Windows output). Still set `ao=null` for mpv.
- **Settings**: the app reads `$APPDATA` first, on Linux too, then
  `~/.config`. It does *not* read `XDG_CONFIG_HOME`. Set
  `os.environ["APPDATA"]` to a private folder, as on Windows.
- **Never take Linux screenshots**, not for checks, the README or anything
  else (the owner's rule). Check Linux layout with numbers (`winfo_width()`,
  `winfo_reqwidth()`, positions), not pictures.
- Driving a separately running app (for example the frozen build): Tk `send`
  works over X11. Find the interp with `winfo interps` whose `wm title .`
  contains "BarkBark", then `send` Tcl to it (press buttons, fill Tk's own
  file dialogs `*__tk_filedialog`, answer `*__tk__messagebox`).
- Shell scripts written on Windows have CRLF line endings: run
  `tr -d '\r' < script.sh > /root/script.sh` before running them in WSL.
- Kill your own Xvfb by its PID when done. One left over (`:76`) once had to
  be found and killed by hand.
- WSL sometimes fails with "Insufficient system resources". Retry later; it
  isn't the test.

## Checklist before running anything new

- [ ] `APPDATA` points at a private folder, written before the app import.
- [ ] `quiet_windows.install()` before the first `tk.Tk()`.
- [ ] `m.VideoPlayer.extra_options = {"ao": "null"}` and `SDL_AUDIODRIVER=dummy`.
- [ ] Every `messagebox` / `filedialog` / `webbrowser` call the path can hit is patched, or closed on purpose.
- [ ] Child processes: no window, or a hidden one (`visible=False`), never activated.
- [ ] Every wait has a time limit; real native boxes have a watchdog.
- [ ] Window lookups by title also filter by PID.
- [ ] Media and outputs in the scratchpad, never the user's folders.
- [ ] Ends with `quiet.foreground_is_ours()` checked, `app._on_app_close()`, `quiet.uninstall()`.
- [ ] Afterwards, no process of yours left running.
