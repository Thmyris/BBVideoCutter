@echo off
REM ===================================================================
REM Builds both distributables, one after the other, from one app build:
REM   1) ..\BBVideoCutter-Portable.zip  (same as build_portable.bat)
REM   2) ..\BBVideoCutter-Setup.exe     (same as build_setup.bat)
REM
REM Running build_portable.bat and then build_setup.bat would build the
REM app with PyInstaller twice; this builds it once and makes the zip
REM and the installer from that same dist\BBVideoCutter\ folder.
REM
REM RUN THIS ON WINDOWS. Requires:
REM   - Python + pip on PATH
REM   - Inno Setup 6 installed (https://jrsoftware.org/isinfo.php), for
REM     the installer. Without it the portable zip is still made, then
REM     the script stops with an error at the installer step.
REM   - This .bat sits in release\build\, next to installer.iss and the
REM     other build_*.bat files, two levels under the project root.
REM     ffmpeg.exe/ffprobe.exe, libmpv-2.dll and deep-filter.exe (if
REM     used) live in this same release\build\ folder; see build_exe.bat
REM     for what each one
REM     adds to the build.
REM ===================================================================

setlocal
cd /d "%~dp0"

set ISCC="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="C:\Program Files\Inno Setup 6\ISCC.exe"
set HAVE_ISCC=1
if not exist %ISCC% set HAVE_ISCC=0
if "%HAVE_ISCC%"=="0" (
    echo WARNING: Inno Setup 6 was not found -- only the portable zip can be made.
    echo          Install it from https://jrsoftware.org/isinfo.php for the installer.
)

echo.
echo [1/5] Installing/updating build dependencies...
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
echo [2/5] Building the app with PyInstaller...
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
echo [3/5] Portable: zipping dist\BBVideoCutter\ into BBVideoCutter-Portable.zip...
if exist "..\BBVideoCutter-Portable.zip" del /f /q "..\BBVideoCutter-Portable.zip"
powershell -NoProfile -Command "Compress-Archive -Path 'dist\BBVideoCutter' -DestinationPath '..\BBVideoCutter-Portable.zip' -Force"
if errorlevel 1 goto :error
echo       BBVideoCutter-Portable.zip is ready in release\.

echo.
echo [4/5] Setup: compiling the installer with Inno Setup...
if "%HAVE_ISCC%"=="0" (
    echo Inno Setup 6 was not found, so no installer was made.
    goto :error
)
%ISCC% installer.iss
if errorlevel 1 goto :error
copy /y "installer_output\BBVideoCutter-Setup.exe" "..\BBVideoCutter-Setup.exe" >nul
if errorlevel 1 goto :error
echo       BBVideoCutter-Setup.exe is ready in release\.

echo.
echo [5/5] Cleaning up build byproducts...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "installer_output" rmdir /s /q "installer_output"
if exist "BBVideoCutter.spec" del /f /q "BBVideoCutter.spec"

echo.
echo Done! BBVideoCutter-Portable.zip and BBVideoCutter-Setup.exe are ready in release\.
goto :end

:error
echo.
echo Build failed -- scroll up to see which step errored.

:end
endlocal
pause
