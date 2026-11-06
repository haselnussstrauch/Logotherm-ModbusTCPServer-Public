; Inno Setup 6 - Installer fuer Logotherm Gateway (Windows)
; Aufruf (aus packaging/windows/docker/build.sh, im Build-Container):  ISCC.exe /DAppVersion=0.1.8 packaging\windows\setup.iss
; Voraussetzungen vor dem Kompilieren (erledigt docker/build.sh):
;   dist\gateway\          PyInstaller-Ausgabe von gateway.spec
;   dist\launcher\         PyInstaller-Ausgabe von launcher.spec
;   third_party\WinSW-x64.exe   (WinSW, vom Dockerfile geladen)

#ifndef AppVersion
  #define AppVersion "0.1.8"
#endif
#define AppName "Logotherm Gateway"
#define ServiceName "LogothermGateway"
#define FwRule "LogothermGateway-Modbus-502"
#define FwRuleWeb "LogothermGateway-Web-8080"
#define DataDir "{commonappdata}\LogothermGateway"

[Setup]
AppId={{6B0B6F57-4C0B-4C2E-9D58-3E6A3C1B7A10}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Logotherm
DefaultDirName={autopf}\LogothermGateway
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\..\dist\installer
OutputBaseFilename=LogothermGateway-Setup-{#AppVersion}
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\launcher\LogothermGateway.exe
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
; F7: WinUSB-Treiber still per wdi-simple installieren - standardmaessig AN (so einfach wie
; moeglich). Wiederholen spaeter ueber den Launcher ("USB-CAN-Treiber installieren").
Name: "winusb"; Description: "WinUSB-Treiber für den USB-CAN-Adapter installieren (empfohlen)"; \
    GroupDescription: "Treiber:"
; Weboberflaeche auch von anderen PCs/Smartphones im lokalen Netz: web.bind = 0.0.0.0 und
; Firewall-Regel TCP 8080 (nur lokales Subnetz). Passwort ist dann Pflicht; das erste Passwort
; laesst sich nur am Gateway-PC selbst festlegen.
Name: "webnet"; Description: "Weboberfläche im Netzwerk freigeben (andere PCs, Smartphones im lokalen Netz)"; \
    GroupDescription: "Netzwerk:"

[Dirs]
Name: "{#DataDir}"; Flags: uninsneveruninstall
Name: "{#DataDir}\logs"; Flags: uninsneveruninstall

[Files]
; Gateway (PyInstaller one-folder)
Source: "..\..\dist\gateway\*"; DestDir: "{app}\gateway"; Flags: recursesubdirs createallsubdirs ignoreversion
; Launcher (GUI)
Source: "..\..\dist\launcher\*"; DestDir: "{app}\launcher"; Flags: recursesubdirs createallsubdirs ignoreversion
; WinSW: Exe und XML muessen gleich heissen. (LogothermGateway.exe ist der Launcher.)
Source: "third_party\WinSW-x64.exe"; DestDir: "{app}"; DestName: "LogothermGatewayService.exe"; Flags: ignoreversion
Source: "LogothermGateway.xml"; DestDir: "{app}"; DestName: "LogothermGatewayService.xml"; Flags: ignoreversion
; Standardkonfiguration nur beim Erstinstall - Updates ueberschreiben nie die Konfiguration
Source: "gateway.default.yaml"; DestDir: "{#DataDir}"; DestName: "gateway.yaml"; \
    Flags: onlyifdoesntexist uninsneveruninstall
; Treiber-Installation (immer mitliefern, damit der Launcher sie wiederholen kann).
; wdi-simple.exe baut das Dockerfile (Stufe wdi), nicht im Repo.
Source: "driver\install-winusb.cmd"; DestDir: "{app}\driver"; Flags: ignoreversion
Source: "driver\wdi-simple.exe"; DestDir: "{app}\driver"; Flags: ignoreversion

[Icons]
Name: "{group}\Logotherm Gateway (Status)"; Filename: "{app}\launcher\LogothermGateway.exe"
Name: "{group}\Protokoll-Ordner"; Filename: "{#DataDir}\logs"
Name: "{group}\Deinstallieren"; Filename: "{uninstallexe}"

[InternetShortcuts]
; Port 8080 ist der Standard; bei geaendertem web.port den Launcher benutzen.
Name: "{group}\Einrichtung öffnen"; Filename: "http://127.0.0.1:8080/"

[Run]
; Firewall: Modbus-TCP 502 eingehend (alte Regel zuerst entfernen -> keine Duplikate)
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name={#FwRule}"; \
    Flags: runhidden waituntilterminated; StatusMsg: "Firewall-Regel wird eingerichtet ..."
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name={#FwRule} dir=in action=allow protocol=TCP localport=502"; \
    Flags: runhidden waituntilterminated
; Weboberflaeche im Netz: Regel immer zuerst entfernen, nur bei Aufgabe "webnet" neu anlegen
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name={#FwRuleWeb}"; \
    Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name={#FwRuleWeb} dir=in action=allow protocol=TCP localport=8080 remoteip=localsubnet"; \
    Flags: runhidden waituntilterminated; Tasks: webnet
Filename: "{app}\gateway\gateway.exe"; Parameters: "web-access --network"; \
    Flags: runhidden waituntilterminated; Tasks: webnet; StatusMsg: "Weboberfläche wird im Netzwerk freigegeben ..."
Filename: "{app}\gateway\gateway.exe"; Parameters: "web-access --local"; \
    Flags: runhidden waituntilterminated; Tasks: not webnet
; WinUSB-Treiber: siehe CurStepChanged (Fehler wird gemeldet, Installation laeuft weiter)
; Dienst: bei Erstinstall registrieren (Update: Dienst existiert bereits), dann starten
Filename: "{app}\LogothermGatewayService.exe"; Parameters: "install"; \
    Flags: runhidden waituntilterminated; Check: not ServiceExists; StatusMsg: "Dienst wird installiert ..."
Filename: "{sys}\net.exe"; Parameters: "start {#ServiceName}"; \
    Flags: runhidden waituntilterminated; StatusMsg: "Dienst wird gestartet ..."
Filename: "{app}\launcher\LogothermGateway.exe"; Description: "Launcher starten"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\net.exe"; Parameters: "stop {#ServiceName}"; Flags: runhidden waituntilterminated; RunOnceId: "StopSvc"
Filename: "{app}\LogothermGatewayService.exe"; Parameters: "uninstall"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveSvc"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name={#FwRule}"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveFw"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name={#FwRuleWeb}"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveFwWeb"

[Code]
function ServiceExists: Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{sys}\sc.exe'), 'query {#ServiceName}', '', SW_HIDE,
                 ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

{ WinUSB-Treiber still installieren; bei Fehler nur Hinweis (Launcher kann wiederholen). }
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('winusb') then
  begin
    WizardForm.StatusLabel.Caption := 'WinUSB-Treiber für den USB-CAN-Adapter wird installiert ...';
    if not Exec(ExpandConstant('{cmd}'), '/c "' + ExpandConstant('{app}\driver\install-winusb.cmd') + '"',
                ExpandConstant('{app}\driver'), SW_HIDE, ewWaitUntilTerminated, ResultCode)
       or (ResultCode <> 0) then
      SuppressibleMsgBox('Der WinUSB-Treiber für den USB-CAN-Adapter konnte nicht installiert werden ' +
        '(Fehlercode ' + IntToStr(ResultCode) + ').' + #13#10#13#10 +
        'Das Gateway wird trotzdem installiert. Bitte den Adapter anstecken und im Launcher ' +
        '"USB-CAN-Treiber installieren" wählen. Details: ' +
        ExpandConstant('{#DataDir}\logs\winusb-install.log'), mbInformation, MB_OK, IDOK);
  end;
end;

{ Update: Dienst vor dem Austausch der Dateien anhalten (net stop wartet bis zum Stopp). }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  if ServiceExists then
    Exec(ExpandConstant('{sys}\net.exe'), 'stop {#ServiceName}', '', SW_HIDE,
         ewWaitUntilTerminated, ResultCode);
end;
