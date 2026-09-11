@echo off
REM ===================================================================
REM Builds a BBVideoCutter\ app folder from BBVideoCutter.py
REM
REM RUN THIS ON WINDOWS. PyInstaller cannot cross-compile: a Windows
REM build has to be made by running PyInstaller ON Windows. Running
REM this .bat file IS that build step.
REM
REM Requirements before running:
REM   - Python installed on this Windows machine (python + pip on PATH)
REM   - This .bat sits in the same folder as BBVideoCutter.py
REM
REM What it does:
REM   1) Installs/updates pyinstaller, pillow, tkinterdnd2 (safe to
REM      re-run any time; pip skips what's already up to date)
REM   2) Runs PyInstaller in --onedir --windowed mode: the output is a
REM      FOLDER (dist\BBVideoCutter\) containing BBVideoCutter.exe plus
REM      its dependencies as loose files, not a single self-extracting
REM      exe. --onefile was dropped because it re-extracts its entire
REM      bundle to a fresh temp folder on *every* launch -- fine for a
REM      small app, but with ffmpeg.exe/ffprobe.exe embedded that meant
REM      unpacking ~260MB every single time the app started. --onedir
REM      has no extraction step at all, so startup is instant regardless
REM      of how much is bundled.
REM   3) --collect-data=tkinterdnd2 bundles tkinterdnd2's internal
REM      Tcl/Tk drag-and-drop library files -- without this flag the
REM      built app launches but drag & drop is silently broken (a very
REM      common PyInstaller+tkinterdnd2 gotcha)
REM
REM ffmpeg.exe/ffprobe.exe: if both are sitting in this same folder next
REM to this script, PyInstaller copies them straight into the output
REM folder alongside BBVideoCutter.exe, so the built app needs nothing
REM on the system PATH at all (find_exe() in BBVideoCutter.py checks
REM next to the exe first, before falling back to PATH). If they're not
REM here, the build proceeds without them and the app falls back to
REM requiring ffmpeg/ffprobe on the system PATH at runtime, same as the
REM plain .py version.
REM
REM The finished app is NOT copied out of dist\ this time -- unlike a
REM single .exe, a onedir build is a whole folder (exe + _internal\),
REM and moving just the .exe out would break it. Use dist\BBVideoCutter\
REM directly, zip it, or feed it to build_installer.bat.
REM ===================================================================

setlocal
cd /d "%~dp0"

echo.
echo [1/2] Installing/updating build dependencies...
python -m pip install --upgrade pyinstaller pillow tkinterdnd2
if errorlevel 1 goto :error

echo.
if exist "ffmpeg.exe" if exist "ffprobe.exe" (
    echo [2/2] Found ffmpeg.exe/ffprobe.exe here -- including them in the build...
    python -m PyInstaller ^
        --onedir ^
        --windowed ^
        --name "BBVideoCutter" ^
        --collect-data=tkinterdnd2 ^
        --add-binary "ffmpeg.exe;." ^
        --add-binary "ffprobe.exe;." ^
        --noconfirm ^
        BBVideoCutter.py
    set BUNDLED_FFMPEG=1
) else (
    echo [2/2] ffmpeg.exe/ffprobe.exe not found here -- building without them...
    python -m PyInstaller ^
        --onedir ^
        --windowed ^
        --name "BBVideoCutter" ^
        --collect-data=tkinterdnd2 ^
        --noconfirm ^
        BBVideoCutter.py
    set BUNDLED_FFMPEG=0
)
if errorlevel 1 goto :error

echo.
echo Done! The app folder is ready: dist\BBVideoCutter\BBVideoCutter.exe
if "%BUNDLED_FFMPEG%"=="1" (
    echo ffmpeg + ffprobe are bundled in -- no separate install needed.
) else (
    echo ffmpeg + ffprobe still need to be on your system PATH to run it.
)
goto :end

:error
echo.
echo Build failed -- scroll up to see which step errored.

:end
endlocal
pause
