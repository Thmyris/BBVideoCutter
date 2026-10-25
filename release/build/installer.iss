; Inno Setup script for BarkBark Lossless Video Cutter.
; Builds a real Windows installer (Setup.exe) around the PyInstaller
; --onedir output in dist\BBVideoCutter\ -- installs per user to
; %LOCALAPPDATA%\Programs, adds a Start Menu shortcut (and optional
; Desktop shortcut), and registers a proper uninstaller in "Add or
; Remove Programs".
;
; Not meant to be run directly -- build_setup.bat (in this same
; release\build\ folder) runs the PyInstaller build first, then
; compiles this with Inno Setup's ISCC.exe.

#define MyAppName "BarkBark Lossless Video Cutter"
; The version is APP_VERSION in BBVideoCutter.py, passed in by the build
; scripts (ISCC /DMyAppVersion=...).
#ifndef MyAppVersion
  #error MyAppVersion is not set: build with build_setup.bat or build_portable_and_setup.bat
#endif
#define MyAppPublisher "Thmyris"
#define MyAppExeName "BBVideoCutter.exe"

[Setup]
; Fixed GUID so re-installs/upgrades are recognized as the same app
; instead of installing side-by-side -- do not change this once shipped.
AppId={{9E7A6B8B-5C2E-4C9A-9E3B-2B7B6D1E4F21}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
SetupIconFile=..\..\assets\barkbark2.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=installer_output
OutputBaseFilename=BBVideoCutter-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; The onedir build already contains ffmpeg.exe/ffprobe.exe (~200MB) and
; compresses further here, so a real install shrinks the download size.
ArchitecturesInstallIn64BitMode=x64compatible
; Always a per-user install (%LOCALAPPDATA%\Programs): no UAC prompt, and
; no second copy from someone picking "all users" one time and "just me"
; the next.
PrivilegesRequired=lowest

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[InstallDelete]
; Installing over an older version: clear its bundle first, so files the
; new build no longer has don't pile up next to it.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "dist\BBVideoCutter\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
