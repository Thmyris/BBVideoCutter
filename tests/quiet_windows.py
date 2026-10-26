"""Keeps the windows a test or probe opens off the screen and out of the way.

GUI probes and screenshot scripts drive the real app window. Run on a desk
that is in use, that window would flash up on the primary screen and take
the keyboard focus. Everything here happens before a window is shown, so
nothing is ever seen or felt:

- every top-level window this thread creates is given WS_EX_NOACTIVATE the
  moment it exists (a CBT hook sees the creation), so showing it, zooming
  it or raising it never takes the focus and puts nothing on the taskbar;
  an activation asked for outright (focus_force) is refused by the same
  hook;
- every Tk root and Toplevel is made fully transparent before it is
  mapped, and parked on the second monitor when there is one, so a window
  the first rule did not reach still lands off the working screen, and a
  click there falls through it;
- every native dialog this thread opens (a messagebox, a file dialog: no
  Tk window, so the rule above misses it) is made fully transparent and
  click-through as it is about to be shown (WM_SHOWWINDOW).

The windows are mapped all the same: they have their real size and get
their <Configure> events, so drawing and layout checks behave exactly as
before. Set BBVC_SHOW_TESTS=1 to watch them, on the second monitor, still
without losing the focus. Windows only; elsewhere install() only records
that.

Usage, before the app (or any tk.Tk()) is created:

    import quiet_windows
    quiet = quiet_windows.install()
    app = BBVideoCutter.LosslessVideoApp()
    ...
    app.destroy()
    quiet.uninstall()

Adapted from The Organizer's tests/quiet_windows.py; this copy also covers
the root window, since this app's main window is a tk.Tk subclass
(TkinterDnD.Tk) rather than a Toplevel.
"""

import ctypes
import os
import tkinter as tk

ENV_SHOW = "BBVC_SHOW_TESTS"
MARGIN = 60                   # how far in from the second monitor's corner

_WH_CBT = 5
_HCBT_CREATEWND = 3
_HCBT_ACTIVATE = 5
_GWL_STYLE = -16
_GWL_EXSTYLE = -20
_WS_CHILD = 0x40000000
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_LWA_ALPHA = 0x2
_WH_CALLWNDPROC = 4
_WM_SHOWWINDOW = 0x0018
_DIALOG_CLASS = "#32770"      # Windows' own dialogs: MessageBox, the file dialogs


class Quiet:
    """What install() set up. uninstall() takes it down again."""

    def __init__(self):
        self.hooked = False       # the CBT hook is in place
        self.show = False         # windows stay visible (on the second monitor)
        self.park = None          # (x, y) on the second monitor, or None
        self.styled = 0           # top-level windows given WS_EX_NOACTIVATE
        self.refused = 0          # activations the hook turned down
        self.dialogs = 0          # native dialogs made transparent
        self._hook = None
        self._proc = None         # the callback, kept alive for the hook
        self._dialog_hook = None
        self._dialog_proc = None
        self._inits = {}          # class -> its __init__ as it was
        self._user32 = None       # the hook's own user32 handle

    def describe(self):
        how = "shown" if self.show else "hidden (transparent)"
        where = ("parked on the second monitor at +{}+{}".format(*self.park)
                 if self.park else "one monitor, not parked")
        focus = "never take the focus" if self.hooked else "may take the focus (no hook)"
        return "{}, {}, {}".format(how, where, focus)

    def foreground_is_ours(self):
        """True when the window the user is working in belongs to this thread -
        exactly what the hook is there to prevent."""
        if os.name != "nt":
            return False
        user32 = ctypes.windll.user32
        h = user32.GetForegroundWindow()
        if not h:
            return False
        tid = user32.GetWindowThreadProcessId(h, None)
        return tid == ctypes.windll.kernel32.GetCurrentThreadId()

    def uninstall(self):
        for cls, init in self._inits.items():
            cls.__init__ = init
        self._inits = {}
        if self._hook:
            self._user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
        if self._dialog_hook:
            self._user32.UnhookWindowsHookEx(self._dialog_hook)
            self._dialog_hook = None
        self.hooked = False


def install(show=None):
    """Set everything up. Call before the first tk.Tk()."""
    q = Quiet()
    q.show = (os.environ.get(ENV_SHOW) == "1") if show is None else bool(show)
    if os.name != "nt":
        return q
    q.park = _second_monitor_corner()
    _hook_activation(q)
    if not q.show:
        _hook_dialogs(q)
    _patch_init(q, tk.Tk)
    _patch_init(q, tk.Toplevel)
    return q


# ---------------------------------------------------------------- pieces

def _second_monitor_corner():
    """(x, y) just inside the first monitor that is not the primary, in the
    coordinates this process sees; None with one monitor."""
    from ctypes import wintypes
    # A private handle, so these argtypes don't leak into the app's own
    # ctypes.windll.user32 calls.
    user32 = ctypes.WinDLL("user32")

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    proc_t = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                                ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    # Without these, ctypes passes the monitor handle as a 32-bit int and a
    # handle above 2**31 fails with OverflowError, so nothing got parked.
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                           proc_t, wintypes.LPARAM]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    found = []

    def cb(hmon, hdc, rect, lparam):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcMonitor
            found.append((r.left, r.top, bool(mi.dwFlags & 1)))
        return 1

    try:
        user32.EnumDisplayMonitors(None, None, proc_t(cb), 0)
    except Exception:                                     # pragma: no cover
        return None
    others = [(x, y) for x, y, primary in found if not primary]
    if not others:
        return None
    x, y = others[0]
    return (x + MARGIN, y + MARGIN)


def _hook_activation(q):
    """A thread CBT hook: NOACTIVATE on every new top-level window, and no
    to every activation."""
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32")          # private: argtypes set below stay here
    kernel32 = ctypes.windll.kernel32
    q._user32 = user32
    hookproc_t = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                    wintypes.WPARAM, wintypes.LPARAM)
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, hookproc_t, wintypes.HINSTANCE,
                                         wintypes.DWORD]
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM,
                                      wintypes.LPARAM]
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    user32.GetWindowLongW.restype = wintypes.LONG
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowLongW.restype = wintypes.LONG
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]

    def cbt(code, wparam, lparam):
        try:
            if code == _HCBT_CREATEWND:
                if not user32.GetWindowLongW(wparam, _GWL_STYLE) & _WS_CHILD:
                    ex = user32.GetWindowLongW(wparam, _GWL_EXSTYLE)
                    if not ex & _WS_EX_NOACTIVATE:
                        user32.SetWindowLongW(wparam, _GWL_EXSTYLE, ex | _WS_EX_NOACTIVATE)
                    q.styled += 1
            elif code == _HCBT_ACTIVATE:
                q.refused += 1
                return 1
        except Exception:                                 # pragma: no cover
            pass
        return user32.CallNextHookEx(None, code, wparam, lparam)

    q._proc = hookproc_t(cbt)
    q._hook = user32.SetWindowsHookExW(_WH_CBT, q._proc, None, kernel32.GetCurrentThreadId())
    q.hooked = bool(q._hook)


def _hook_dialogs(q):
    """A thread WH_CALLWNDPROC hook: a native dialog about to be shown is
    made layered at alpha 0 (nothing drawn) and click-through. Styles set
    when it is created don't hold: CreateWindowEx applies its own after the
    CBT hook has run."""
    from ctypes import wintypes
    user32 = q._user32

    class CWPSTRUCT(ctypes.Structure):
        _fields_ = [("lParam", wintypes.LPARAM), ("wParam", wintypes.WPARAM),
                    ("message", wintypes.UINT), ("hwnd", wintypes.HWND)]

    hookproc_t = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                    wintypes.WPARAM, wintypes.LPARAM)
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF,
                                                  wintypes.BYTE, wintypes.DWORD]
    class_name = ctypes.create_unicode_buffer(64)

    def on_message(code, wparam, lparam):
        try:
            msg = ctypes.cast(lparam, ctypes.POINTER(CWPSTRUCT)).contents
            if msg.message == _WM_SHOWWINDOW and msg.wParam:
                user32.GetClassNameW(msg.hwnd, class_name, 64)
                if class_name.value == _DIALOG_CLASS:
                    ex = user32.GetWindowLongW(msg.hwnd, _GWL_EXSTYLE)
                    user32.SetWindowLongW(msg.hwnd, _GWL_EXSTYLE,
                                          ex | _WS_EX_LAYERED | _WS_EX_TRANSPARENT)
                    user32.SetLayeredWindowAttributes(msg.hwnd, 0, 0, _LWA_ALPHA)
                    q.dialogs += 1
        except Exception:                                 # pragma: no cover
            pass
        return user32.CallNextHookEx(None, code, wparam, lparam)

    q._dialog_proc = hookproc_t(on_message)
    q._dialog_hook = user32.SetWindowsHookExW(_WH_CALLWNDPROC, q._dialog_proc, None,
                                              ctypes.windll.kernel32.GetCurrentThreadId())


def _patch_init(q, cls):
    """Every window of `cls` starts transparent (unless shown) and on the
    second monitor, before anything can map it. tk.Tk and tk.Toplevel only
    map on the next idle pass, so setting this right after __init__ is in
    time."""
    real = cls.__init__

    def quiet_init(self, *args, **kw):
        real(self, *args, **kw)
        try:
            if not q.show:
                self.attributes("-alpha", 0.0)
            # Found again per window: the app turns DPI awareness on as it
            # starts, which changes the coordinates this process sees.
            if os.name == "nt":
                q.park = _second_monitor_corner()
            if q.park:
                self.geometry("+{}+{}".format(*q.park))
        except tk.TclError:                               # pragma: no cover
            pass

    q._inits[cls] = real
    cls.__init__ = quiet_init
