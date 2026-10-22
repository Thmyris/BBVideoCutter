@echo off
REM ===================================================================
REM Builds ..\BBVideoCutter-Portable.zip from ..\..\BBVideoCutter.py.
REM
REM RUN THIS ON WINDOWS. Requires:
REM   - Python + pip on PATH
REM   - This .bat sits in release\build\, next to build_exe.bat/
REM     build_setup.bat, two levels under the project root (where
REM     BBVideoCutter.py and assets\ live). ffmpeg.exe/ffprobe.exe (if
REM     used) live in this same release\build\ folder.
REM
REM What it does:
REM   1) Installs/updates pyinstaller, pillow, tkinterdnd2, python-mpv
REM   2) Builds a PyInstaller --onedir app to dist\BBVideoCutter\ (no
REM      self-extraction step, so it starts instantly even extracted
REM      fresh on someone else's PC -- see build_exe.bat's comments for
REM      why onedir over onefile). If ffmpeg.exe/ffprobe.exe are sitting
REM      in this same folder, they're included so the app needs nothing
REM      on the system PATH. Uses ..\..\assets\barkbark2.ico as the
REM      app's icon.
REM   3) Zips dist\BBVideoCutter\ into ..\BBVideoCutter-Portable.zip (one
REM      level up, in release\) -- extract it anywhere and run
REM      BBVideoCutter.exe, no install needed.
REM   4) Deletes every intermediate byproduct this script created
REM      (build\, dist\, the .spec file) -- none of that is the
REM      deliverable, just scratch work along the way, and it's all
REM      regenerable by re-running this script.
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
echo [1/4] Installing/updating build dependencies...
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
echo [2/4] Building the app with PyInstaller...
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
echo [3/4] Zipping dist\BBVideoCutter\ into BBVideoCutter-Portable.zip...
if exist "..\BBVideoCutter-Portable.zip" del /f /q "..\BBVideoCutter-Portable.zip"
powershell -NoProfile -Command "Compress-Archive -Path 'dist\BBVideoCutter' -DestinationPath '..\BBVideoCutter-Portable.zip' -Force"
if errorlevel 1 goto :error

echo.
echo [4/4] Cleaning up build byproducts...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "BBVideoCutter.spec" del /f /q "BBVideoCutter.spec"

echo.
echo Done! BBVideoCutter-Portable.zip is ready in release\.
goto :end

:error
echo.
echo Build failed -- scroll up to see which step errored.

:end
endlocal
pause
