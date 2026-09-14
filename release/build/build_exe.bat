@echo off
REM ===================================================================
REM Builds a BBVideoCutter\ app folder from ..\..\BBVideoCutter.py.
REM
REM RUN THIS ON WINDOWS. PyInstaller cannot cross-compile: a Windows
REM build has to be made by running PyInstaller ON Windows. Running
REM this .bat file IS that build step.
REM
REM Requirements before running:
REM   - Python installed on this Windows machine (python + pip on PATH)
REM   - This .bat sits in release\build\, next to build_setup.bat and
REM     build_portable.bat, two levels under the project root (where
REM     BBVideoCutter.py and assets\ live). ffmpeg.exe/ffprobe.exe (if
REM     used) live in this same release\build\ folder.
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
REM      of how much is bundled. Uses ..\..\assets\barkbark2.ico as the
REM      app's icon, matching build_setup.bat/build_portable.bat.
REM   3) --collect-data=tkinterdnd2 bundles tkinterdnd2's internal
REM      Tcl/Tk drag-and-drop library files -- without this flag the
REM      built app launches but drag & drop is silently broken (a very
REM      common PyInstaller+tkinterdnd2 gotcha)
REM   4) Deletes the leftover build\ folder and the auto-generated
REM      .spec file -- pure PyInstaller byproducts, not the app itself,
REM      and safe to regenerate any time by re-running this script.
REM      dist\BBVideoCutter\ is NOT deleted -- that folder is this
REM      script's actual deliverable.
REM
REM ffmpeg.exe/ffprobe.exe: if both are sitting in this same folder
REM (release\build\), they get copied straight into the output folder
REM alongside BBVideoCutter.exe, so the built app needs nothing on the
REM system PATH at all (find_exe() in BBVideoCutter.py checks next to
REM the exe first, before falling back to PATH). If they're not there,
REM the build proceeds without them and the app falls back to requiring
REM ffmpeg/ffprobe on the system PATH at runtime, same as the plain .py
REM version.
REM ===================================================================

setlocal
cd /d "%~dp0"

echo.
echo [1/3] Installing/updating build dependencies...
python -m pip install --upgrade pyinstaller pillow tkinterdnd2
if errorlevel 1 goto :error

echo.
if exist "ffmpeg.exe" if exist "ffprobe.exe" (
    echo [2/3] Found ffmpeg.exe/ffprobe.exe here -- including them in the build...
    python -m PyInstaller ^
        --onedir ^
        --windowed ^
        --name "BBVideoCutter" ^
        --icon "..\..\assets\barkbark2.ico" ^
        --collect-data=tkinterdnd2 ^
        --add-binary "ffmpeg.exe;." ^
        --add-binary "ffprobe.exe;." ^
        --exclude-module numpy ^
        --exclude-module yaml ^
        --noconfirm ^
        "..\..\BBVideoCutter.py"
    set BUNDLED_FFMPEG=1
) else (
    echo [2/3] ffmpeg.exe/ffprobe.exe not found here -- building without them...
    python -m PyInstaller ^
        --onedir ^
        --windowed ^
        --name "BBVideoCutter" ^
        --icon "..\..\assets\barkbark2.ico" ^
        --collect-data=tkinterdnd2 ^
        --exclude-module numpy ^
        --exclude-module yaml ^
        --noconfirm ^
        "..\..\BBVideoCutter.py"
    set BUNDLED_FFMPEG=0
)
if errorlevel 1 goto :error

echo.
echo [3/3] Cleaning up build byproducts (keeping dist\BBVideoCutter\)...
if exist "build" rmdir /s /q "build"
if exist "BBVideoCutter.spec" del /f /q "BBVideoCutter.spec"

echo.
echo Done! The app folder is ready: release\build\dist\BBVideoCutter\BBVideoCutter.exe
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
