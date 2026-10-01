"""Double-click launcher with no console window at all.

Windows opens .pyw files with pyw.exe / pythonw.exe, which never create a
console -- unlike run.bat, whose own cmd window shows up while it starts
the app. Point a shortcut at this file to start the app cleanly.
"""
import os
import runpy

runpy.run_path(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "BBVideoCutter.py"),
    run_name="__main__",
)
