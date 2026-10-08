; Compiled by atc\installer\build_beta.py, which writes build_defines.iss first.
#include "build_defines.iss"

[Setup]
AppId={{8F4C2A1E-7B63-4D95-A1E0-6C9F3B27D4A8}
AppName=PitBoss ATC
AppVersion={#MyAppVersion}
AppVerName=PitBoss ATC {#MyAppVersion}
AppPublisher=PitBoss ATC
DefaultDirName={localappdata}\PitBoss ATC
DefaultGroupName=PitBoss ATC
DisableProgramGroupPage=yes
OutputDir={#DistDir}
OutputBaseFilename={#OutputBase}
SetupLogging=yes
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
UninstallDisplayName=PitBoss ATC
LicenseFile={#Staging}\LICENSE
InfoBeforeFile={#InstallerDir}\BETA-INFO.txt
VersionInfoVersion={#FileVersion}
VersionInfoProductName=PitBoss ATC
VersionInfoDescription=PitBoss ATC open beta
CloseApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: checkedonce

[Files]
Source: "{#Staging}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\PitBoss ATC"; Filename: "{app}\atc\Open-Flight-Flow.cmd"; WorkingDir: "{app}\atc"
Name: "{group}\PitBoss ATC (Host)"; Filename: "{app}\atc\Start-ATC-Host.cmd"; WorkingDir: "{app}\atc"
Name: "{autodesktop}\PitBoss ATC"; Filename: "{app}\atc\Open-Flight-Flow.cmd"; WorkingDir: "{app}\atc"; Tasks: desktopicon
Name: "{group}\Uninstall PitBoss ATC"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\atc\Setup-Installed.cmd"; WorkingDir: "{app}\atc"; StatusMsg: "Setting up this PC (config and DCS radio export)…"; Flags: waituntilterminated
Filename: "{app}\atc\Open-Flight-Flow.cmd"; WorkingDir: "{app}\atc"; Description: "Launch PitBoss ATC"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "{app}\atc\runtime\python.exe"; Parameters: """{app}\atc\install_dcs_radio_export.py"" --uninstall"; WorkingDir: "{app}\atc"; Flags: runhidden waituntilterminated; RunOnceId: RemoveDcsRadioExport
