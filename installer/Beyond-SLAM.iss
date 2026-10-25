#define ProjectRoot AddBackslash(SourcePath) + ".."
#ifndef AppVersion
#define AppVersion "1.0.0"
#endif
#ifndef PackageDir
#define PackageDir ProjectRoot + "\dist\BeyondSLAMControl"
#endif
#ifdef SmokeTest
#define AppMutexName "BeyondSLAM-Isolated-Installer-Test"
#else
#define AppMutexName "BeyondSLAMControl"
#endif

[Setup]
#ifdef SmokeTest
AppId=BeyondSLAM-Isolated-Installer-Test
CreateUninstallRegKey=no
#else
AppId={{A21C3A13-6D15-4C9A-8E38-271C2173496A}
#endif
AppName=Beyond-SLAM Control
AppVersion={#AppVersion}
AppPublisher=Fovea Labs LLC
DefaultDirName={localappdata}\BSB-SLAM
DefaultGroupName=Beyond-SLAM
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
AppMutex={#AppMutexName}
RestartApplications=no
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
OutputDir={#ProjectRoot}\dist
OutputBaseFilename=Beyond-SLAM-Setup
UninstallDisplayName=Beyond-SLAM Control

[Files]
Source: "{#PackageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#ProjectRoot}\Driver\x64\Release\driver_BSB-SLAM.dll"; DestDir: "{app}\payload\driver\bin\win64"; Flags: ignoreversion
Source: "{#ProjectRoot}\Driver\Install Example\BSB-SLAM\driver.vrdrivermanifest"; DestDir: "{app}\payload\driver"; Flags: ignoreversion
Source: "{#ProjectRoot}\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ProjectRoot}\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "{#ProjectRoot}\dist\notices\*"; DestDir: "{app}\notices"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Remove the former bundled runtime payload before laying down our own driver.
Type: files; Name: "{app}\payload\driver\bin\win64\*.dll"
; Retire the experimental capture workflow from earlier app installs.
Type: files; Name: "{app}\calibrate.py"
Type: files; Name: "{app}\calibration_preflight.py"
Type: files; Name: "{app}\pose_mapping.py"
Type: files; Name: "{app}\CALIBRATION.md"

[Icons]
#ifndef SmokeTest
Name: "{group}\Beyond-SLAM Control"; Filename: "{app}\BeyondSLAMControl.exe"
#endif
[Run]
Filename: "{app}\BeyondSLAMControl.exe"; Description: "Open Beyond-SLAM Control"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\driver"

[Code]
var
  SetupNotice: String;
  UpdateSucceeded: Boolean;

{ The app starts setup with /UPDATE /RESULT=<file> /RELAUNCH=<exe> for in-place updates. }
function IsUpdate(): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/UPDATE') = 0 then
      Result := True;
end;

function InitializeSetup(): Boolean;
var
  Waited: Integer;
begin
  Result := True;
  if IsUpdate() then begin
    { The app exits right after starting an update; wait on its single-instance mutex so its
      files are released. Smoke-test builds use a separate AppMutex but the same app. }
    Waited := 0;
    while CheckForMutexes('BeyondSLAMControl') and (Waited < 240) do begin
      Sleep(250);
      Waited := Waited + 1;
    end;
  end;
end;

function SteamVRClosed(): Boolean;
var
  Locator, Services, Processes: Variant;
begin
  Result := False;
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Services := Locator.ConnectServer('', 'root\CIMV2');
    Processes := Services.ExecQuery('SELECT ProcessId FROM Win32_Process WHERE Name = ''vrserver.exe'' OR Name = ''vrmonitor.exe'' OR Name = ''vrstartup.exe''');
    Result := Processes.Count = 0;
    if not Result then
      SuppressibleMsgBox('Close SteamVR, then run setup again.', mbInformation, MB_OK, IDOK);
  except
    SuppressibleMsgBox('Could not check whether SteamVR is running. Setup cannot continue.', mbError, MB_OK, IDOK);
  end;
end;
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not SteamVRClosed() then
    Result := 'Close SteamVR, then press Retry to continue.';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  Details: AnsiString;
  Command: String;
begin
  if CurStep = ssPostInstall then begin
    SetupNotice := '';
    { Updates refresh driver files but keep the user's current tracking source. }
    if IsUpdate() then
      Command := '--refresh-driver'
    else
      Command := '--setup';
    if not Exec(ExpandConstant('{app}\BeyondSLAMControl.exe'), Command,
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) then
      SetupNotice := 'Open Beyond-SLAM and select Set up tracking.'
    else if ResultCode <> 0 then begin
      if not LoadStringFromFile(ExpandConstant('{app}\setup-result.txt'), Details) then
        Details := 'Open Beyond-SLAM and select Set up tracking.';
      SetupNotice := String(Details);
    end;
  end;
  if CurStep = ssDone then
    UpdateSucceeded := True;
end;

procedure DeinitializeSetup();
var
  ResultFile, Relaunch, Outcome: String;
  ResultCode: Integer;
begin
  if not IsUpdate() then
    exit;
  try
    ResultFile := ExpandConstant('{param:RESULT|}');
    if UpdateSucceeded then begin
      Outcome := 'success';
      if SetupNotice <> '' then
        Outcome := Outcome + ': ' + SetupNotice;
    end else
      Outcome := 'failed';
    if ResultFile <> '' then
      SaveStringToFile(ResultFile, Outcome, False);
    { Reopen the app after success or failure so the user is never left without it. }
    Relaunch := ExpandConstant('{param:RELAUNCH|}');
    if (Relaunch <> '') and FileExists(Relaunch) then
      Exec(Relaunch, '', ExtractFileDir(Relaunch), SW_SHOWNORMAL, ewNoWait, ResultCode);
  except
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and (SetupNotice <> '') then
    WizardForm.FinishedLabel.Caption := 'Beyond-SLAM is installed. Tracking setup needs one more step:' + #13#10 + #13#10 + SetupNotice;
end;

function InitializeUninstall(): Boolean;
var
  ResultCode: Integer;
begin
  Result := SteamVRClosed();
  if not Result then exit;
  Result := Exec(ExpandConstant('{app}\BeyondSLAMControl.exe'), '--unregister',
    ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if Result then Result := ResultCode = 0;
  if not Result then
    SuppressibleMsgBox('Could not unregister the driver. Open Beyond-SLAM, select Tools > Use base stations, and retry. See control.log for details.', mbError, MB_OK, IDOK);
end;
