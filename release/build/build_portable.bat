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
REM   1) Installs/updates pyinstaller, pillow, tkinterdnd2
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
REM ===================================================================

setlocal
cd /d "%~dp0"

echo.
echo [1/4] Installing/updating build dependencies...
python -m pip install --upgrade pyinstaller pillow tkinterdnd2
if errorlevel 1 goto :error

echo.
if exist "ffmpeg.exe" if exist "ffprobe.exe" (
    echo [2/4] Found ffmpeg.exe/ffprobe.exe here -- including them in the build...
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
) else (
    echo [2/4] ffmpeg.exe/ffprobe.exe not found here -- building without them...
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
)
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
