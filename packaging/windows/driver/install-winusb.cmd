@echo off
rem ====================================================================================
rem  Stille Installation des WinUSB-Treibers fuer den USB-CAN-Adapter (gs_usb/candleLight).
rem  Der Installer ruft dieses Skript standardmaessig auf; der Launcher bietet
rem  "USB-CAN-Treiber installieren" zum Wiederholen. Zadig bleibt nur der Notfallweg.
rem
rem  - Muss als Administrator laufen (Installer/Launcher fordern das an).
rem  - Adapter nicht angesteckt: wdi-simple installiert den Treiber vor (Treiberspeicher);
rem    er greift beim ersten Anstecken.
rem  - Ausgabe: %ProgramData%\LogothermGateway\logs\winusb-install.log
rem
rem  TODO(Feldtest): VID/PID am Geraet verifizieren (Geraetemanager > Details > Hardware-IDs).
rem        1D50:606F ist die uebliche candleLight/gs_usb-Kennung; weicht der InnoMaker ab,
rem        unten einen weiteren Aufruf je VID/PID ergaenzen.
rem  Hinweis: libwdi erzeugt ein selbstsigniertes Zertifikat und traegt es lokal als
rem        vertrauenswuerdig ein (wie Zadig).
rem ====================================================================================
setlocal
set "HERE=%~dp0"
set "WDI=%HERE%wdi-simple.exe"
set "LOGDIR=%ProgramData%\LogothermGateway\logs"
set "LOG=%LOGDIR%\winusb-install.log"
set "EXTRACT=%TEMP%\LogothermGateway-usb_driver"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1

echo [%DATE% %TIME%] WinUSB-Installation gestartet >> "%LOG%"
if not exist "%WDI%" (
    echo wdi-simple.exe fehlt in "%HERE%". Treiber bitte mit Zadig installieren, siehe docs\windows.md. >> "%LOG%"
    exit /b 2
)

call :install 0x1D50 0x606F "InnoMaker USB2CAN"
set "RC=%ERRORLEVEL%"
echo [%DATE% %TIME%] Ende, Fehlercode %RC% >> "%LOG%"
exit /b %RC%

:install
"%WDI%" --vid %1 --pid %2 --type 0 --name %3 --dest "%EXTRACT%" --timeout 120000 >> "%LOG%" 2>&1
exit /b %ERRORLEVEL%
