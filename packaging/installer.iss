#ifndef AppVersion
  #define AppVersion "0.6.0"
#endif
#ifndef ReleaseDir
  #define ReleaseDir "..\release"
#endif
[Setup]
AppId={{83E093B9-256E-449C-8434-8520E08AFA9C}
AppName=RefArchive
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\RefArchive
DefaultGroupName=RefArchive
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
DisableProgramGroupPage=yes
OutputDir={#ReleaseDir}
OutputBaseFilename=RefArchive-{#AppVersion}-Setup-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
RestartApplications=no
AppMutex=RefArchive.Application
InfoBeforeFile=..\packaging\start-guide.txt
UninstallDisplayIcon={app}\RefArchive.exe

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: checkedonce

[Files]
Source: "{#ReleaseDir}\RefArchive\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\RefArchive"; Filename: "{app}\RefArchive.exe"
Name: "{userdesktop}\RefArchive"; Filename: "{app}\RefArchive.exe"; Tasks: desktopicon
Name: "{group}\RefArchive 사용설명서"; Filename: "{app}\사용설명서.html"
Name: "{group}\RefArchive 배포 안내"; Filename: "{app}\배포안내.html"

[Run]
Filename: "{app}\RefArchive.exe"; Description: "{cm:LaunchProgram,RefArchive}"; Flags: nowait postinstall skipifsilent
