@echo off
REM ===================================================================
REM Builds ..\BBVideoCutter-Setup.exe (a real Windows installer) from
REM ..\..\BBVideoCutter.py.
REM
REM RUN THIS ON WINDOWS. Requires:
REM   - Python + pip on PATH
REM   - Inno Setup 6 installed (https://jrsoftware.org/isinfo.php) --
REM     this script looks for it at its default install path
REM   - This .bat sits in release\build\, next to installer.iss and
REM     build_exe.bat/build_portable.bat, two levels under the project
REM     root (where BBVideoCutter.py and assets\ live). ffmpeg.exe/
REM     ffprobe.exe (if used) live in this same release\build\ folder.
REM
REM What it does:
REM   1) Installs/updates pyinstaller, pillow, tkinterdnd2
REM   2) Builds a PyInstaller --onedir app to dist\BBVideoCutter\ (no
REM      self-extraction step, so the installed app starts instantly --
REM      see build_exe.bat's comments for why onedir over onefile).
REM      If ffmpeg.exe/ffprobe.exe are sitting in this same folder,
REM      they're included so the installed app needs nothing on the
REM      system PATH. Uses ..\..\assets\barkbark2.ico as the app's icon.
REM   3) Compiles installer.iss with Inno Setup's ISCC.exe, packaging
REM      dist\BBVideoCutter\ into a proper installer (Program Files or
REM      per-user install, Start Menu shortcut, uninstaller) -- also
REM      using ..\..\assets\barkbark2.ico as the installer's own icon.
REM   4) Copies the finished installer out to release\ (one level up)
REM      as BBVideoCutter-Setup.exe, then deletes every intermediate
REM      byproduct this script created (build\, dist\, the .spec file,
REM      installer_output\) -- none of that is the deliverable, just
REM      scratch work along the way, and it's all regenerable by
REM      re-running this script.
REM ===================================================================

setlocal
cd /d "%~dp0"

set ISCC="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="C:\Program Files\Inno Setup 6\ISCC.exe"
if not exist %ISCC% (
    echo Inno Setup 6 was not found. Install it from https://jrsoftware.org/isinfo.php
    goto :error
)

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
echo [3/4] Compiling the installer with Inno Setup...
%ISCC% installer.iss
if errorlevel 1 goto :error

copy /y "installer_output\BBVideoCutter-Setup.exe" "..\BBVideoCutter-Setup.exe" >nul

echo.
echo [4/4] Cleaning up build byproducts...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "installer_output" rmdir /s /q "installer_output"
if exist "BBVideoCutter.spec" del /f /q "BBVideoCutter.spec"

echo.
echo Done! BBVideoCutter-Setup.exe is ready in release\.
goto :end

:error
echo.
echo Build failed -- scroll up to see which step errored.

:end
endlocal
pause
