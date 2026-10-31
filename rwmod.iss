[Setup]
AppName=rwmod
AppVersion=0.7.0

AppPublisher=qxkt222
DefaultDirName={commonpf}\rwmod
DefaultGroupName=rwmod
OutputDir=dist
OutputBaseFilename=rwmod_setup
Compression=lzma2
SolidCompression=yes
UninstallDisplayName=rwmod RimWorld Mod Manager
PrivilegesRequired=admin

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "dist\rwmod.exe"; DestDir: "{app}"
Source: "steamcmd\*.*"; DestDir: "{app}\steamcmd"; Flags: recursesubdirs createallsubdirs; Excludes: "steamapps\*,userdata,depotcache,appcache,logs,siteserverui,steamcmd_siteserverui_win64.zip*,*.old,update_hosts_cached.vdf"

[Dirs]
; SteamCMD writes logs/, steamapps/ and appcache/ under {app}\steamcmd at runtime.
; Standard users cannot write to Program Files, so let them modify this directory.
Name: "{app}\steamcmd"; Permissions: users-modify

[Icons]
Name: "{commondesktop}\rwmod"; Filename: "{app}\rwmod.exe"; Parameters: "web"; WorkingDir: "{app}"
Name: "{group}\rwmod"; Filename: "{app}\rwmod.exe"; Parameters: "web"; WorkingDir: "{app}"
Name: "{group}\Uninstall rwmod"; Filename: "{uninstallexe}"
