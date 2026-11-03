; LiteClip installer - built with Inno Setup (https://jrsoftware.org/isinfo.php)
; build_exe.bat compiles this automatically and passes the version from liteclip.pyw.

#define MyAppName "LiteClip"
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppExe "LiteClip.exe"

[Setup]
AppId={{8F3C2A51-7D4E-4B8A-9C61-2E5F0A7B3D19}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppName}
VersionInfoVersion={#MyAppVersion}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Installs for the current user without admin rights; the user can choose "all users" instead.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=dist
OutputBaseFilename=LiteClip-Setup-{#MyAppVersion}
SetupIconFile=liteclip.ico
UninstallDisplayIcon={app}\{#MyAppExe}
UninstallDisplayName={#MyAppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start LiteClip with Windows (quietly, in the system tray)"; GroupDescription: "Startup:"; Flags: unchecked
Name: "ffmpeg"; Description: "Install FFmpeg - required for recording (free, about 100 MB, uses winget)"; GroupDescription: "Required component:"; Check: NeedsFFmpeg

[Files]
Source: "dist\LiteClip\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExe}"" --minimized"; Tasks: startup

[Run]
Filename: "{cmd}"; Parameters: "/c winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements"; StatusMsg: "Installing FFmpeg - this can take a minute or two..."; Tasks: ffmpeg; Flags: waituntilterminated
Filename: "{app}\{#MyAppExe}"; Description: "Launch LiteClip now"; Flags: nowait postinstall skipifsilent

[Code]
function NeedsFFmpeg: Boolean;
begin
  Result := (FileSearch('ffmpeg.exe', GetEnv('PATH')) = '') and
            (not FileExists(ExpandConstant('{localappdata}\Microsoft\WinGet\Links\ffmpeg.exe')));
end;

procedure CloseLiteClip;
var
  Code: Integer;
begin
  { Close a running LiteClip so its files can be replaced. Its recorder closes with it. }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im LiteClip.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(500);
end;

function InitializeSetup: Boolean;
begin
  CloseLiteClip;
  Result := True;
end;

function InitializeUninstall: Boolean;
begin
  CloseLiteClip;
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#MyAppName}');
    if (not UninstallSilent) and
       (MsgBox('Also remove your LiteClip settings?' + #13#10#13#10 +
               'Your saved clips will NOT be deleted.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
      DelTree(ExpandConstant('{userappdata}\{#MyAppName}'), True, True, True);
  end;
end;
