@echo off
REM ---------------------------------------------------------------
REM Runs BBVideoCutter.py with NO console window at all.
REM Double-click this file (or pin it / put a shortcut on Desktop).
REM
REM How it works:
REM   - "pythonw.exe" is the version of Python that ships with every
REM     normal Python install specifically for GUI apps: it never
REM     opens a console window, so you don't even see a flash.
REM   - If pythonw can't be found for some reason, it falls back to
REM     starting python.exe minimized instead, so the app still runs.
REM ---------------------------------------------------------------

setlocal
set "DIR=%~dp0"

where pythonw >nul 2>nul
if %ERRORLEVEL%==0 (
    start "" pythonw "%DIR%BBVideoCutter.py"
) else (
    start "" /min python "%DIR%BBVideoCutter.py"
)

endlocal
