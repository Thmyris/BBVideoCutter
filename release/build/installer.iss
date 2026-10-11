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
#define MyAppVersion "1.0"
#define MyAppPublisher "Thmyris"
#define MyAppExeName "BBVideoCutter.exe"
; Fixed GUID so re-installs/upgrades are recognized as the same app
; instead of installing side-by-side -- do not change this once shipped.
#define MyAppGuid "9E7A6B8B-5C2E-4C9A-9E3B-2B7B6D1E4F21"
; Where an all-users install (from before installs were per-user only)
; has its uninstall entry.
#define OldCopyRootA "HKLM64"
#define OldCopyRootB "HKLM32"
#define OldCopyKey "Software\Microsoft\Windows\CurrentVersion\Uninstall\{" + MyAppGuid + "}_is1"

[Setup]
AppId={{{#MyAppGuid}}
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
; the next. An older all-users copy is offered for removal (see [Code]).
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
Source: "dist\BBVideoCutter\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
// The uninstaller of an all-users copy, if one is installed.
function OldCopyUninstaller(var Uninstaller: String): Boolean;
begin
  Result := False;
  if IsWin64 then   // the 64-bit view doesn't exist on 32-bit Windows
    Result := RegQueryStringValue({#OldCopyRootA}, '{#OldCopyKey}', 'UninstallString', Uninstaller);
  if not Result then
    Result := RegQueryStringValue({#OldCopyRootB}, '{#OldCopyKey}', 'UninstallString', Uninstaller);
  Uninstaller := RemoveQuotes(Uninstaller);
end;

// This version installs per user only, so an older all-users copy would
// stay next to it as a second one: offer to remove it.
function InitializeSetup(): Boolean;
var
  Uninstaller: String;
  ErrorCode: Integer;
begin
  Result := True;
  if not OldCopyUninstaller(Uninstaller) then
    Exit;
  Log('An all-users copy is installed; its uninstaller: ' + Uninstaller);
  if WizardSilent then
    Exit;
  if MsgBox('An older copy of {#MyAppName} is installed for all users. This version installs just for you, '
      + 'so the old copy would stay as a second one.' + #13#10#13#10
      + 'Remove the old copy now? Windows will ask for permission.', mbConfirmation, MB_YESNO) = IDYES then
  begin
    if not ShellExec('runas', Uninstaller, '/SILENT /SUPPRESSMSGBOXES /NORESTART', '', SW_SHOWNORMAL,
        ewWaitUntilTerminated, ErrorCode) then
      MsgBox('The old copy could not be removed (' + SysErrorMessage(ErrorCode) + '). '
        + 'You can remove it from Add or Remove Programs.', mbInformation, MB_OK);
  end;
end;
