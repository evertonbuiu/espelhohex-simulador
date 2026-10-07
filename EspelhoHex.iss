; =====================================================================
;  Instalador .exe do EspelhoHex Simulador (Inno Setup 6)
;  Compile com Compilar-Instalador.bat (ele baixa o Python e a
;  biblioteca 3D para a pasta "build" antes de chamar o Inno Setup).
; =====================================================================
#define AppName "EspelhoHex Simulador"
#define AppVersion "2.3"

[Setup]
AppId={{8C6F4B2E-5A1D-4F3B-9E2C-7D41A6B0E5F1}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=EspelhoHex (projeto DIY)
DefaultDirName={autopf}\EspelhoHex
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=saida
OutputBaseFilename=EspelhoHex-Simulador-Setup-{#AppVersion}
SetupIconFile=app\espelhohex.ico
UninstallDisplayIcon={app}\espelhohex.ico
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Languages]
Name: "pt"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "app\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs
Source: "build\python\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs
Source: "build\vendor\*"; DestDir: "{app}\vendor"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\simulador.py"" --app --porta-http 47454"; WorkingDir: "{app}"; IconFilename: "{app}\espelhohex.ico"; Comment: "Simulador da parede de espelhos (Art-Net)"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\simulador.py"" --app --porta-http 47454"; WorkingDir: "{app}"; IconFilename: "{app}\espelhohex.ico"; Tasks: desktopicon

[Run]
; libera o Art-Net (UDP 6454) no firewall do Windows para o Python do app
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""EspelhoHex Simulador"" dir=in action=allow program=""{app}\python\pythonw.exe"" enable=yes profile=any"; Flags: runhidden; StatusMsg: "Liberando o Art-Net no firewall..."
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\simulador.py"" --app --porta-http 47454"; WorkingDir: "{app}"; Description: "Abrir o {#AppName}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""EspelhoHex Simulador"""; Flags: runhidden; RunOnceId: "RemoverFirewall"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
// Fecha o simulador antes de atualizar ou desinstalar
// (só os processos do Python que está dentro da pasta do app).
procedure FecharSimulador();
var
  Code: Integer;
  Params: String;
begin
  Params := '-NoProfile -ExecutionPolicy Bypass -Command "Get-Process pythonw -ErrorAction SilentlyContinue | ' +
            'Where-Object { $_.Path -like ''' + ExpandConstant('{app}') + '\*'' } | Stop-Process -Force"';
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Params, '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  FecharSimulador();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  FecharSimulador();
  Result := True;
end;
