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
#define AppVersion     "2.8.0"
#define AppPublisher   "Convrse Media Private Limited"
#define AppExeName     "Convrse Device Control.exe"
; Overridable so the build can stage somewhere else when the usual output
; folder is held open by a scanner or an Explorer window.
#ifndef SourceDir
  #define SourceDir    "..\dist\ConvrseDeviceControl"
#endif

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
; The startup trace is written next to the executable at runtime; a stale one
; from the build machine must not ship inside the installer.
Source: "{#SourceDir}\*"; DestDir: "{app}"; \
  Excludes: "*.log"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

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
var
  KeyPage: TInputFileWizardPage;

function StoredKeyPath(): String;
begin
  Result := ExpandConstant('{localappdata}\Convrse\DeviceControl\keys\cdm-key.pem');
end;

function StagedKeyPath(): String;
begin
  Result := ExpandConstant('{localappdata}\Convrse\DeviceControl\keys\setup-import.pem');
end;

procedure InitializeWizard();
var
  Suggested: String;
begin
  // Asked once here so the first launch can connect straight away. The app
  // imports the file itself, with the same checks as File > Add or change SSH
  // key, which is also where it is changed later.
  KeyPage := CreateInputFilePage(wpSelectTasks,
    'SSH key',
    'Choose the cdm.convrse.ai key this computer connects with.',
    'Pick the .pem private key file. It is copied into this account''s private ' +
    'app data, so the original can be moved or deleted afterwards.' + #13#10#13#10 +
    'To change it later, open the app, press Alt, then File > Add or change SSH key.');
  KeyPage.Add('Key file (.pem):', 'PEM private key (*.pem)|*.pem|All files (*.*)|*.*', '.pem');
  Suggested := GetEnv('USERPROFILE') + '\Downloads\cdm-key.pem';
  if FileExists(Suggested) then
    KeyPage.Values[0] := Suggested;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  // An upgrade keeps the key already on this computer; nothing to ask.
  Result := (KeyPage <> nil) and (PageID = KeyPage.ID) and FileExists(StoredKeyPath());
end;

function LooksLikePrivateKey(const Path: String; var Problem: String): Boolean;
var
  Body: AnsiString;
begin
  Result := False;
  if not FileExists(Path) then
  begin
    Problem := 'That file does not exist.';
    Exit;
  end;
  if not LoadStringFromFile(Path, Body) then
  begin
    Problem := 'That file could not be read.';
    Exit;
  end;
  if Pos('ssh-rsa', Body) = 1 then
  begin
    Problem := 'That is the public half of the key (it starts with ssh-rsa). ' +
               'Choose the private .pem file instead.';
    Exit;
  end;
  if (Pos('-----BEGIN', Body) = 0) or (Pos('PRIVATE KEY-----', Body) = 0) then
  begin
    Problem := 'That does not look like a private key. It must contain a line ' +
               'such as -----BEGIN RSA PRIVATE KEY-----.';
    Exit;
  end;
  Result := True;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Path, Problem: String;
begin
  Result := True;
  if (KeyPage = nil) or (CurPageID <> KeyPage.ID) then
    Exit;
  Path := Trim(KeyPage.Values[0]);
  if Path = '' then
  begin
    Result := MsgBox('Continue without an SSH key?' + #13#10#13#10 +
      'The app will ask for it before the first connection. You can also add ' +
      'it from File > Add or change SSH key (press Alt).',
      mbConfirmation, MB_YESNO) = IDYES;
    Exit;
  end;
  if not LooksLikePrivateKey(Path, Problem) then
  begin
    MsgBox(Problem, mbError, MB_OK);
    Result := False;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Path: String;
begin
  if (CurStep <> ssPostInstall) or (KeyPage = nil) then
    Exit;
  Path := Trim(KeyPage.Values[0]);
  if (Path = '') or FileExists(StoredKeyPath()) then
    Exit;
  ForceDirectories(ExtractFileDir(StagedKeyPath()));
  if not CopyFile(Path, StagedKeyPath(), False) then
    MsgBox('The SSH key could not be copied. Add it from File > Add or change ' +
           'SSH key after setup.', mbError, MB_OK);
end;

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
