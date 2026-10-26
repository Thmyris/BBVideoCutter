"""Linux support for BBVideoCutter.py, kept apart from the Windows app.

BBVideoCutter.py imports this only on Linux and hands itself to install(),
which swaps in what differs there:
- the player: the system's libmpv (Debian/Ubuntu: the libmpv2 package),
  drawing through X11 OpenGL, or plain X11 where there is no GPU or the
  OpenGL there runs on the CPU;
- Xlib told to expect threads, before Tk opens its window;
- the mouse wheel: X11 sends it as Button-4/5, passed on as <MouseWheel>;
- a field color Tk only knows on Windows, and deep-filter's file name;
- restarting after a git pull (sh instead of PowerShell); the Linux build
  (release/build/build_linux.sh) offers the release page instead of
  updating itself, and gives the programs it runs the system's
  libraries, not its own.
The Windows build leaves this file out (--exclude-module bbvc_linux in
release\\build's build scripts).
"""

import ctypes
import ctypes.util
import importlib.util
import os
import subprocess
import sys
import tkinter as tk

app = None   # BBVideoCutter's module, once installed

# The Linux build's file on a release (build_linux.sh makes it).
LINUX_ASSET = "BBVideoCutter-Linux-x86_64.tar.gz"

# mpv options for every player, over the app's own.
MPV_OPTIONS = {
    # gpu (OpenGL), else x11, which needs no GPU (a VM, say). Not
    # gpu-next: where it can't start, its failed setup can crash the app
    # once two players exist.
    "vo": "gpu,x11",
    # The video goes into Tk's X11 window (Wayland desktops run Tk through
    # XWayland), so only mpv's X11 OpenGL context can work. Trying the
    # others too (DRM, Vulkan) where it can't start leaves X in a state
    # that aborts the app.
    "gpu_context": "x11egl",
    # mpv's own X11 window takes the mouse buttons and wheel unless it
    # ignores the mouse; then they go on to the app's frame.
    "input_cursor": False,
}


def install(module):
    """Swaps the Linux pieces into BBVideoCutter's module. Called as it is
    imported, before any window exists."""
    global app
    app = module
    _init_x11_threads()
    _restore_library_path()
    module.FIELD_BG = "#ffffff"
    module._CHROME_BG["#ffffff"] = "field"
    module.DEEP_FILTER_EXE = "deep-filter"
    module.mpv_available = mpv_available
    module.load_mpv = load_mpv
    module.VideoPlayer.platform_options = MPV_OPTIONS
    _wrap_player(module.VideoPlayer)
    cls = module.LosslessVideoApp
    cls._restart_from_source = _restart_from_source
    _wrap_start_update(cls)
    _wrap_init(cls)


def _init_x11_threads():
    """mpv draws on X11 from its own threads while Tk uses Xlib on the main
    one, which aborts the app ("Unknown sequence number while processing
    queue") unless Xlib is told to expect threads before the first display
    connection, i.e. before Tk opens the window."""
    try:
        ctypes.CDLL("libX11.so.6").XInitThreads()
    except (OSError, AttributeError):
        pass


def _restore_library_path():
    """The built app (PyInstaller) starts with LD_LIBRARY_PATH pointing at
    its own bundled libraries, and the programs it runs (the system's
    ffmpeg, the restart's sh) would load those instead of their own. The
    app itself is not affected by the change: the loader read the variable
    when it started."""
    if not getattr(sys, "frozen", False):
        return
    orig = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
    if orig is not None:
        os.environ["LD_LIBRARY_PATH"] = orig
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)


# ---- the player -----------------------------------------------------------

_libmpv = None   # find_system_libmpv's answer, once asked


def find_system_libmpv():
    """The system's libmpv, the one python-mpv loads itself, or None."""
    global _libmpv
    if _libmpv is None:
        _libmpv = ctypes.util.find_library("mpv") or ""   # runs ldconfig, so asked once
    return _libmpv or None


def mpv_available():
    """Cheap check (no import) that the player can be used at all."""
    if app._mpv_module is not None:
        return True
    if app._mpv_error is not None:
        return False
    return find_system_libmpv() is not None and importlib.util.find_spec("mpv") is not None


def load_mpv():
    """Imports python-mpv on first use. Returns the module, or None if it
    can't be loaded (the reason is in the app's _mpv_error)."""
    if app._mpv_module is not None or app._mpv_error is not None:
        return app._mpv_module
    if find_system_libmpv() is None:
        app._mpv_error = "libmpv was not found (on Debian/Ubuntu: the libmpv2 package)"
        return None
    try:
        import mpv
    except Exception as exc:
        app._mpv_error = str(exc)
        return None
    app._mpv_module = mpv
    return mpv


# Set once a player has found OpenGL running on the CPU (Mesa's llvmpipe,
# say: a machine or VM with no GPU driver); the players started after it
# draw with x11 from the start.
_software_gl = False


def _wrap_player(cls):
    """mpv's gpu output takes OpenGL on the CPU too, only warning about it,
    and draws garbage with it (seen: llvmpipe on Xvfb: a sparse grid of dots,
    or nothing). mpv itself, choosing an output on its own, would skip such
    a renderer, so a player whose mpv reports one switches to x11, which
    needs no OpenGL. The switch is made on the Tk thread, in the player's
    poll, where it can't meet the player being closed."""
    real_ensure, real_poll = cls._ensure_mpv, cls._poll

    def _ensure_mpv(self):
        if self.mpv is not None:
            return
        if _software_gl:
            self.platform_options = dict(MPV_OPTIONS, vo="x11")
        else:
            self.platform_options = dict(
                MPV_OPTIONS, loglevel="warn",
                log_handler=lambda level, prefix, text: _on_mpv_log(self, prefix, text))
        real_ensure(self)

    def _poll(self):
        if self.__dict__.pop("_want_x11", False) and self.mpv is not None:
            try:
                self.mpv["vo"] = "x11"
            except Exception:
                pass
        real_poll(self)

    cls._ensure_mpv = _ensure_mpv
    cls._poll = _poll


def _on_mpv_log(player, prefix, text):
    # mpv's event thread: only flags it, for the player's poll.
    global _software_gl
    if prefix.startswith("vo/gpu") and "software renderer" in text:
        _software_gl = True
        player._want_x11 = True


# ---- the mouse wheel --------------------------------------------------------

def _wrap_init(cls):
    real = cls.__init__

    def __init__(self, *args, **kwargs):
        real(self, *args, **kwargs)
        self.bind_all("<Button-4>", lambda e: _wheel(e, 120), add="+")
        self.bind_all("<Button-5>", lambda e: _wheel(e, -120), add="+")

    cls.__init__ = __init__


def _wheel(event, delta):
    """A wheel step (Button-4/5 on X11) passed on as <MouseWheel> to a
    widget that handles that but not Button-4/5 itself, as Tk's own
    scrolling widgets do."""
    w = event.widget
    if isinstance(w, str):   # a window Tk doesn't know as a widget
        return
    try:
        if w.bind("<MouseWheel>") and not w.bind("<Button-4>"):
            w.event_generate("<MouseWheel>", delta=delta, x=event.x, y=event.y)
    except tk.TclError:
        pass


# ---- restart and update -----------------------------------------------------

def restart_command(pid, cmd, cwd):
    """sh that waits for the app (process pid) to exit, then runs cmd (a
    list: program and its arguments) in cwd. The pid, folder and command go
    in as sh's arguments, not into its script, so any quotes and spaces in
    them survive."""
    script = ('pid=$0; cd "$1" || exit 1; shift; '
              'while kill -0 "$pid" 2>/dev/null; do sleep 0.2; done; exec "$@"')
    return ["/bin/sh", "-c", script, str(int(pid)), cwd] + list(cmd)


def launch_detached(cmd):
    """Starts cmd in its own session: out of the app's process group and
    terminal, which could end it with the app."""
    return subprocess.Popen(cmd, close_fds=True, start_new_session=True)


def _restart_from_source(self):
    """Closes the app; sh starts it again (the same Python and script) once
    it is gone."""
    cmd = [sys.executable, os.path.abspath(sys.argv[0])] + sys.argv[1:]
    try:
        launch_detached(restart_command(os.getpid(), cmd, os.getcwd()))
    except OSError as exc:
        app.messagebox.showerror("Update", f"Couldn't restart the app:\n{exc}")
        return
    self._on_app_close()


def _wrap_start_update(cls):
    real = cls._start_update

    def _start_update(self, release):
        # Run from source, git pull updates it as on Windows.
        if app.running_built_app():
            self._update_offer_page(
                release, "The Linux build can't update itself yet: the new version is "
                f"{LINUX_ASSET} on the release's page.")
            return
        real(self, release)

    cls._start_update = _start_update
