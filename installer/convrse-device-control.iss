; Inno Setup script for Convrse Device Control.
;
; Installs per-user so no administrator prompt is needed, which keeps the
; install path off locked-down site machines' UAC path and gives the product a
; stable install location.  A stable location matters: SmartScreen reputation
; accrues against a consistent signed installer, and Defender scans the payload
; once at install time rather than on every launch.
;
; Build with:  ISCC.exe installer\convrse-device-control.iss
; ISCC comes from Inno Setup 6 (https://jrsoftware.org/isdl.php).

#define AppName        "Convrse Device Control"
#define AppVersion     "2.4.0"
#define AppPublisher   "Convrse Media Private Limited"
#define AppExeName     "Convrse Device Control.exe"
#define SourceDir      "..\dist\ConvrseDeviceControl"

[Setup]
AppId={{8E5C1F2A-6D74-4F1B-9C3E-2A7B4D9E0C51}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Setup

; Per-user install: no elevation prompt, no admin rights required.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\Convrse\Device Control
DefaultGroupName=Convrse
DisableProgramGroupPage=yes
DisableDirPage=no
AllowNoIcons=yes

OutputDir=..\dist\installer
OutputBaseFilename=ConvrseDeviceControl-{#AppVersion}-Setup
SetupIconFile=..\assets\convrse-logo.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}

Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Start {#AppName}"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Logs and reports the app writes beside itself; the operator's SSH key in
; %LOCALAPPDATA%\Convrse is deliberately left alone so a reinstall or upgrade
; does not force everyone to add their key again.
Type: files; Name: "{app}\Convrse-Device-Control-startup.log"

[Code]
function InitializeSetup(): Boolean;
var
  Version: TWindowsVersion;
begin
  GetWindowsVersionEx(Version);
  // scrcpy needs Windows 10 1809 or newer for the capture path it uses.
  if (Version.Major < 10) then
  begin
    MsgBox('Convrse Device Control requires Windows 10 or newer.',
           mbCriticalError, MB_OK);
    Result := False;
    Exit;
  end;
  Result := True;
end;
