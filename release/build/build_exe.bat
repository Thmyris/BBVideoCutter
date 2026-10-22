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
REM   1) Installs/updates pyinstaller, pillow, tkinterdnd2, python-mpv (safe to
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
REM
REM libmpv-2.dll: likewise, if it is in this folder it goes into the
REM build, and the app gets its video player (Cut tab, and the previews
REM on Combine, Bulk Cut and Crop). Without it the build still works
REM and shows still frames instead. It comes from an mpv "dev" build
REM for Windows x86_64 (for example shinchiro's mpv-dev-x86_64-*.7z).
REM
REM deep-filter.exe: likewise, if it is in this folder it goes into the
REM build, for the Audio tab's Medium / Extreme noise reduction
REM (DeepFilterNet). Without it those two are greyed out; Light (RNNoise,
REM with the model in ..\..\assets\rnnoise, always bundled) still works.
REM It is deep-filter-*-x86_64-pc-windows-msvc.exe from a DeepFilterNet
REM release on GitHub (Rikorose/DeepFilterNet), renamed.
REM ===================================================================

setlocal
cd /d "%~dp0"

echo.
echo [1/3] Installing/updating build dependencies...
python -m pip install --upgrade pyinstaller pillow tkinterdnd2 python-mpv
if errorlevel 1 goto :error

echo.
set HAVE_FFMPEG=0
if exist "ffmpeg.exe" if exist "ffprobe.exe" set HAVE_FFMPEG=1
set BUNDLE=
if "%HAVE_FFMPEG%"=="1" set BUNDLE=--add-binary "ffmpeg.exe;." --add-binary "ffprobe.exe;."
if exist "libmpv-2.dll" set BUNDLE=%BUNDLE% --add-binary "libmpv-2.dll;."
if exist "deep-filter.exe" set BUNDLE=%BUNDLE% --add-binary "deep-filter.exe;."
set BUNDLE=%BUNDLE% --add-data "..\..\assets\rnnoise\cb.rnnn;."
echo [2/3] Building the app with PyInstaller...
if "%HAVE_FFMPEG%"=="1" (
    echo       including ffmpeg.exe/ffprobe.exe from here
) else (
    echo       ffmpeg.exe/ffprobe.exe not found here -- the app will need them on PATH
)
if exist "libmpv-2.dll" (
    echo       including libmpv-2.dll from here -- the video player
) else (
    echo       libmpv-2.dll not found here -- the app will show still frames instead of a player
)
if exist "deep-filter.exe" (
    echo       including deep-filter.exe from here -- Medium / Extreme noise reduction
) else (
    echo       deep-filter.exe not found here -- the Audio tab will offer Light noise reduction only
)
python -m PyInstaller ^
    --onedir ^
    --windowed ^
    --name "BBVideoCutter" ^
    --icon "..\..\assets\barkbark2.ico" ^
    --collect-data=tkinterdnd2 ^
    --hidden-import mpv ^
    --exclude-module numpy ^
    --exclude-module yaml ^
    %BUNDLE% ^
    --noconfirm ^
    "..\..\BBVideoCutter.py"
if errorlevel 1 goto :error

echo.
echo [3/3] Cleaning up build byproducts (keeping dist\BBVideoCutter\)...
if exist "build" rmdir /s /q "build"
if exist "BBVideoCutter.spec" del /f /q "BBVideoCutter.spec"

echo.
echo Done! The app folder is ready: release\build\dist\BBVideoCutter\BBVideoCutter.exe
if "%HAVE_FFMPEG%"=="1" (
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
