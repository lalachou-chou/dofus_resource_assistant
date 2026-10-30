#define MyAppName "Dofus Resource Assistant"
#define MyAppVersion "1.0.7"
#define MyAppPublisher "Dofus Resource Assistant"
#define MyAppExeName "pythonw.exe"
#define MyAppId "{C0F0B3A1-9D2D-4B79-8A1C-7D3F6E0A8421}"

[Setup]
AppId={{#MyAppId}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\Dofus Resource Assistant
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=Dofus-Resource-Assistant-Setup-1.0.7
SetupIconFile=dofus_resource_assistant.ico
UninstallDisplayIcon={app}\dofus_resource_assistant.ico
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesAssociations=no

[Files]
Source: "overlay.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "launcher.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "database.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "local_resource_db.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "config.txt"; DestDir: "{app}"; Flags: onlyifdoesntexist
Source: "schema.sql"; DestDir: "{app}"; Flags: ignoreversion
Source: "install-runtime.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "dofus_resource_assistant.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "detector\coordinates.py"; DestDir: "{app}\detector"; Flags: ignoreversion
Source: "detector\__init__.py"; DestDir: "{app}\detector"; Flags: ignoreversion
Source: "data\resource_map.sqlite"; DestDir: "{app}\data"; Flags: ignoreversion

Source: "runtime\python-3.12.10-amd64.exe"; DestDir: "{app}\runtime"; Flags: ignoreversion
; Tesseract est une distribution portable : aucun installateur externe n'est exécuté.
; Tous ses fichiers sont copiés directement dans {app}\tesseract.
Source: "runtime\tesseract-portable\*"; DestDir: "{app}\tesseract"; Flags: ignoreversion recursesubdirs createallsubdirs

[Tasks]
Name: "desktopicon"; Description: "Créer un raccourci sur le Bureau"; GroupDescription: "Raccourcis supplémentaires :"; Flags: unchecked
Name: "startmenuicon"; Description: "Créer un raccourci dans le menu Démarrer"; GroupDescription: "Raccourcis supplémentaires :"; Flags: unchecked

[Icons]
Name: "{app}\Dofus Resource Assistant"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\launcher.py"""; WorkingDir: "{app}"; IconFilename: "{app}\dofus_resource_assistant.ico"; Comment: "Lancer Dofus Resource Assistant"
Name: "{autodesktop}\Dofus Resource Assistant"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\launcher.py"""; WorkingDir: "{app}"; IconFilename: "{app}\dofus_resource_assistant.ico"; Comment: "Dofus Resource Assistant"; Tasks: desktopicon
Name: "{group}\Dofus Resource Assistant"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\launcher.py"""; WorkingDir: "{app}"; IconFilename: "{app}\dofus_resource_assistant.ico"; Comment: "Dofus Resource Assistant"; Tasks: startmenuicon

[Run]
Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\launcher.py"""; WorkingDir: "{app}"; Description: "Lancer Dofus Resource Assistant"; Flags: nowait postinstall skipifsilent unchecked

[UninstallDelete]
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\tesseract"
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}"

[Code]
var
  PythonDirPage: TInputDirWizardPage;

procedure InitializeWizard;
begin
  PythonDirPage := CreateInputDirPage(
    wpSelectDir,
    'Configuration de Python',
    'Dossier Python (utilisé seulement si aucune version compatible n’est trouvée)',
    'Python 3.12 ou supérieur sera réutilisé s’il est déjà installé. Sinon, Python sera installé dans votre profil utilisateur. Vous pouvez choisir un autre emplacement accessible sans privilèges administrateur.',
    False,
    'Python312'
  );
  PythonDirPage.Add('Dossier d’installation de Python :');
  PythonDirPage.Values[0] := ExpandConstant('{localappdata}\Programs\Python312');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  PS: String;
  Params: String;
  RuntimeScript: String;
begin
  if CurStep <> ssPostInstall then
    exit;

  PS := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  RuntimeScript := ExpandConstant('{app}\install-runtime.ps1');
  Params := '-NoProfile -ExecutionPolicy Bypass -File ' + AddQuotes(RuntimeScript) + ' -PythonDir ' + AddQuotes(PythonDirPage.Values[0]);

  if not Exec(PS, Params, ExpandConstant('{app}'), SW_SHOWNORMAL, ewWaitUntilTerminated, ResultCode) then
    Abort;

  if ResultCode <> 0 then
  begin
    MsgBox('L installation du runtime a echoue.' + #13#10 +
      'Consultez le journal : ' + ExpandConstant('{app}\install-runtime.log'), mbError, MB_OK);
    Abort;
  end;
end;
