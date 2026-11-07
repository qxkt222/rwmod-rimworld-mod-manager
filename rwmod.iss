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
; `package` is SteamCMD's ~30 MB self-update cache (the zip archives the client
; re-downloads on first run); excluding it is what keeps the installer small.
Source: "steamcmd\*.*"; DestDir: "{app}\steamcmd"; Flags: recursesubdirs createallsubdirs; Excludes: "steamapps\*,userdata,depotcache,appcache,logs,siteserverui,package,*.old,update_hosts_cached.vdf"

[Dirs]
; SteamCMD writes logs/, steamapps/ and appcache/ under {app}\steamcmd at runtime.
; Standard users cannot write to Program Files, so let them modify this directory.
Name: "{app}\steamcmd"; Permissions: users-modify

[UninstallDelete]
; SteamCMD's downloaded workshop/app content lives outside the installed file
; list ([Files] excludes steamapps), so the uninstaller used to leave it behind
; (REV-013). Delete that directory only — user mods live elsewhere ({app}\mods
; or a user-chosen Mods dir) and must never be removed here.
Type: filesandordirs; Name: "{app}\steamcmd\steamapps"

[Icons]
Name: "{commondesktop}\rwmod"; Filename: "{app}\rwmod.exe"; Parameters: "web"; WorkingDir: "{app}"
Name: "{group}\rwmod"; Filename: "{app}\rwmod.exe"; Parameters: "web"; WorkingDir: "{app}"
Name: "{group}\Uninstall rwmod"; Filename: "{uninstallexe}"
