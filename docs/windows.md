# Logotherm Gateway unter Windows

Zwei Wege: der **Installer** (Dienst + Launcher, empfohlen für den Dauerbetrieb) oder der
**Feldtest ohne Installer** direkt aus dem Repo.

## 1. Installation mit Setup.exe

1. `LogothermGateway-Setup-<Version>.exe` als Administrator ausführen.
2. Installiert wird nach `C:\Program Files\LogothermGateway`:
   - `gateway\` – das Gateway (`gateway.exe`)
   - `launcher\` – das Statusfenster (`LogothermGateway.exe`)
   - `LogothermGatewayService.exe/.xml` – Windows-Dienst-Hülle (WinSW)
3. Konfiguration, Logs und Zustand liegen in `C:\ProgramData\LogothermGateway\`
   (`gateway.yaml`, `logs\gateway.log`). Ein **Update überschreibt `gateway.yaml` nicht**; die
   Deinstallation lässt den Ordner ebenfalls stehen.
4. Der Dienst **LogothermGateway** startet automatisch mit Windows und wird bei Abstürzen bis zu
   dreimal neu gestartet (nach 10 s, 30 s, 60 s).
5. Die Windows-Firewall bekommt eine Regel „LogothermGateway-Modbus-502“ (TCP 502 eingehend).
   Wird in der Konfiguration ein anderer Modbus-Port gewählt, die Regel selbst anpassen:
   `netsh advfirewall firewall add rule name=Modbus-Neu dir=in action=allow protocol=TCP localport=<Port>`.
6. Aufgabe **„Weboberfläche im Netzwerk freigeben“** (Standard: an): Firewall-Regel
   „LogothermGateway-Web-8080“ (TCP 8080 eingehend, **nur lokales Subnetz**) und `web.bind = 0.0.0.0`.
   Dann ist die Oberfläche auch von anderen PCs und Smartphones erreichbar: `http://<IP des PCs>:8080`.
   Ohne die Aufgabe bleibt sie nur am PC selbst erreichbar (127.0.0.1). Später umschalten: Setup erneut
   ausführen oder `gateway.exe web-access --network` bzw. `--local` und den Dienst neu starten.
   **Passwort:** Aus dem Netz geht es nur mit Passwort. Das erste Passwort lässt sich nur am Gateway-PC
   selbst festlegen (Launcher → *Einrichtung öffnen* → *1 Schnittstellen* → *Passwort*); bis dahin
   zeigt die Oberfläche aus dem Netz einen Hinweis. Die Verbindung ist unverschlüsselt (HTTP) – nur im
   eigenen Netz verwenden. Bei geändertem `web.port` die Firewall-Regel anpassen.

Im Setup standardmäßig **an**: *WinUSB-Treiber für den USB-CAN-Adapter installieren (empfohlen)*.
Der Treiber wird still installiert, auch wenn der Adapter noch nicht angesteckt ist; er greift
dann beim ersten Anstecken. Siehe Abschnitt 3.

### Launcher

Startmenü, *Logotherm Gateway (Status)*. Das Fenster zeigt alle 3 Sekunden:

- **Dienst läuft / gestoppt / nicht erreichbar**
- Version, Geräte online (x von y), Warnungen

Knöpfe:

- **Einrichtung öffnen** – öffnet die Weboberfläche (`http://127.0.0.1:8080/`, Port aus `gateway.yaml`).
- **Dienst neu starten** – fragt per UAC nach Administratorrechten (nur wenn nötig).
- **Protokoll öffnen** – öffnet `gateway.log` (bzw. den Logordner).

Der Startmenüeintrag *Einrichtung öffnen* verweist fest auf Port 8080; bei geändertem `web.port`
den Launcher verwenden.

**Updates:** Der Launcher prüft beim Start und danach täglich, ob es eine neue Version gibt
(GitHub-Releases). Dann erscheint *Herunterladen und installieren*: Das Setup wird geladen, die
SHA256-Prüfsumme geprüft und das Setup gestartet (Administratorrechte). Konfiguration, Passwort und
Aufzeichnung bleiben erhalten. Ohne Internet bleibt die Prüfung still; *Nach Updates suchen* prüft
von Hand.

### Einrichtung im Browser

1. *Einrichtung öffnen*, **ein Passwort setzen** (Weboberfläche ist nur lokal erreichbar, das
   Passwort schützt trotzdem vor anderen Benutzern des Rechners).
2. CAN aktivieren (Adapter siehe Abschnitt 3), Geräte suchen, Werte auswählen.
3. Unter *Modbus* die IP-Adressen der Clients (Allowlist) eintragen; leer heißt „alle dürfen“.
4. Nach Änderungen, die einen Neustart brauchen, im Launcher *Dienst neu starten*.

### Auswahltexte

Manche Werte zeigt der Regler als Text an (z. B. Betriebsart „Automatik“, Kesselstatus „Heizbetrieb“).
Diese Texte holt das Gateway beim **Laden der Werteliste** vom Regler (Busanfrage 0x0B/0x08, nur
lesend) und speichert sie in `gateway.yaml`; JA/NEIN und EIN/AUS sind fest hinterlegt. Die
Weboberfläche zeigt dann den Text mit der Zahl in Klammern, das Parametermenü eine Auswahlliste.
Per Modbus kommt immer die Zahl; die Zuordnung steht in der Registerliste (Spalte *Hinweis*).
Nach einem Update auf 0.1.8 je Gerät einmal *Werteliste vom Gerät laden*.

### Dashboard (Startseite)

Zeigt Dienststatus, letzte Aktualisierung, *Regler x von y online*, die ausgewählten Messwerte als
Kacheln (Klick → Verlauf der letzten 24 h) und einen Schnellzugriff auf ausgewählte Parameter
(Setzen wie unter *7 Parameter*, gleiche Prüfungen). Auswahl in der *3 Werteliste*, Spalte
**Dashboard**, dann *Änderungen in Entwurf übernehmen* und *4 Speichern*. Aktualisiert sich alle 5 s.

**Nur Dashboard, ohne Leittechnik:** Unter *1 Schnittstellen → Modbus-TCP-Server* den Haken bei
**Modbus-TCP-Server aktiv** entfernen, *Übernehmen* und *4 Speichern*. Dann ist kein Modbus-Port offen;
Dashboard, Verlauf und Parameter funktionieren unverändert. Wieder einschalten geht genauso, ohne Neustart.

### Aufzeichnung und Diagramme

Der Dienst speichert alle aktiven Messwerte und Parameter (Status ok), standardmäßig **alle 60 s**,
und behält sie **14 Tage**. Danach werden die Tage automatisch gelöscht. Pro Tag gibt es eine Datei:
`C:\ProgramData\LogothermGateway\history\JJJJ-MM-TT.sqlite`. Bedienung im Reiter **6 Verlauf**:

- **Diagramm:** Gerät und Zeitraum wählen (Heute, 24 Stunden, 7 Tage, 14 Tage, Tag wählen), bis zu
  8 Werte ankreuzen. Werte mit unterschiedlicher Einheit erscheinen in getrennten Diagrammen mit
  gemeinsamer Zeitachse. Lücken = keine Daten (Gerät offline oder Aufzeichnung aus). Bei langen
  Zeiträumen zeigt die Linie Mittelwerte, das helle Band Minimum bis Maximum je Abschnitt.
  *Link* kopiert einen Link auf das Diagramm (für Lesezeichen).
- **Gespeicherte Tage:** Größe, *CSV herunterladen* (alle Einzelwerte, Semikolon und Dezimalkomma
  für Excel), *Löschen* je Tag.
- **Aufzeichnung:** ein/aus, Abstand (5–3600 s), Aufbewahrung (Tage). Wie alle Einstellungen unter
  *4 Speichern* übernehmen.

Platzbedarf: etwa 32 Byte je Messpunkt, z. B. 100 Werte im Abstand von 60 s ≈ 4,5 MB pro Tag. Bei
weniger als 200 MB freiem Speicher hält die Aufzeichnung an (Warnung auf der Statusseite).

### Parameter setzen

Reiter **7 Parameter**: Gerät wählen, neuen Wert eintragen, *Setzen*, Rückfrage bestätigen. Der Wert
geht über denselben Weg wie ein Modbus-Schreibbefehl (FC 16) mit denselben Prüfungen:

- Schreiben global freigegeben (*1 Schnittstellen → Schreibschutz*) **und** der Parameter in der
  *3 Werteliste* als änderbar markiert; Parameter der Sperrliste sind nie setzbar.
- Wert innerhalb Min … Max, höchstens alle 10 Minuten je Parameter, Tagesbudget.
- Der Regler meldet den Wert zurück; weicht er ab oder fehlt die Bestätigung, gibt es eine Meldung
  und den Schreib-Alarm (IR 9002).

Warum ein Parameter gerade nicht setzbar ist, steht in der Spalte *Hinweis*. Alle Schreibvorgänge
stehen mit Quelle (Web/Modbus) in der Schreib-Historie (Status und unter der Parametertabelle).

## 2. Anschluss der Adapter

### FTDI (RS232, LC1)

Der Treiber kommt automatisch über Windows Update (Rechner dazu einmal online). Danach
Gerätemanager öffnen (`Win + X`, *Geräte-Manager*) → *Anschlüsse (COM & LPT)* →
*USB Serial Port (COMx)*. Die Nummer (z. B. `COM5`) in der Weboberfläche als Port des
LC1-Geräts eintragen. Die Nummer bleibt pro USB-Buchse meist gleich; bei einem anderen Port
ändert sie sich.

### InnoMaker USB2CAN (CAN, LC2)

Der Adapter (candleLight/gs_usb-Firmware) wird von Windows nicht von sich aus mit einem
passenden Treiber versorgt. Das Gateway spricht ihn über **libusb** an, deshalb braucht der
Adapter den **WinUSB-Treiber**. Wege, in dieser Reihenfolge:

1. **Installer** (Standard): installiert den Treiber still per `driver\install-winusb.cmd`
   (wdi-simple aus libwdi, dem Unterbau von Zadig). Protokoll:
   `%ProgramData%\LogothermGateway\logs\winusb-install.log`. Schlägt es fehl, zeigt das Setup
   einen Hinweis und installiert den Rest trotzdem.
2. **Launcher**, Knopf *USB-CAN-Treiber installieren*: dieselbe Installation erneut
   (UAC-Abfrage), z. B. nach einem Adapterwechsel. Danach *Dienst neu starten*.
3. **Zadig** (Notfall bzw. Betrieb ohne Installer, siehe unten) – manuell, einmal pro Adapter.

`wdi-simple.exe` gibt es nicht als fertigen Download; der Installer-Build im Docker-Container
baut es aus libwdi v1.5.1 (siehe Abschnitt 7).

Hinweis: Mit WinUSB-Treiber erscheint der Adapter nicht mehr als „Gerät“ in anderer Software
(z. B. Hersteller-Tools); Zurücksetzen über Geräte-Manager → *Treiber deinstallieren*.

#### WinUSB mit Zadig einrichten

1. Zadig von <https://zadig.akeo.ie/> laden (`zadig-2.x.exe`, keine Installation nötig).
2. Adapter anstecken. Zadig starten (Administratorrechte bestätigen).
3. Menü *Options → List All Devices* aktivieren.
4. In der Geräteliste den Adapter wählen: **„InnoMaker USB2CAN“**, alternativ **„gs_usb“** bzw.
   **„candleLight“**. Daneben stehen USB-ID (voraussichtlich `1D50 606F`; **bitte am Gerät
   prüfen**). Nicht den falschen Eintrag erwischen – bei Unsicherheit Adapter abziehen und
   schauen, welcher Eintrag verschwindet.
5. Als Zieltreiber (grüner Pfeil rechts) **WinUSB** wählen.
6. *Replace Driver* (bzw. *Install Driver*) klicken und die Installation abwarten.
7. Im Geräte-Manager erscheint der Adapter unter *Universal Serial Bus devices* mit WinUSB.

## 3. libusb

Das gs_usb-Backend benötigt `libusb-1.0.dll` (64 Bit).

- **Installer-Version:** liegt bei (`gateway\libusb-1.0.dll`).
- **Feldtest:** Entweder das Python-Paket `libusb-package` installieren
  (`pip install libusb-package`; bringt die DLL mit und stellt sie python-can/pyusb bereit),
  oder die DLL von <https://github.com/libusb/libusb/releases> (Version 1.0.27, Archiv
  `libusb-1.0.27.7z`, Pfad `VS2022\MS64\dll\libusb-1.0.dll`) holen und neben `python.exe`
  bzw. in `.venv\Scripts` legen.

## 4. Feldtest ohne Installer (aus dem Repo)

Voraussetzung: Python 3.12 (64 Bit), Git.

```powershell
cd Logotherm-ModbusTCPServer-Public
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock
python -m pip install -e . --no-deps

# Datenverzeichnis wahlweise lokal statt C:\ProgramData\LogothermGateway:
$env:GATEWAY_DATA_DIR = "$PWD\data"

gateway run
```

- Konfiguration: `<Datenverzeichnis>\gateway.yaml` (Vorlage: `docs\gateway.example.yaml`). Ohne
  Datei werden Defaults verwendet.
- InnoMaker: WinUSB per Zadig (Abschnitt 2). **libusb-1.0.dll kommt automatisch** über das Paket
  `libusb-package` aus `requirements.lock` (nur Windows). Alternativ eine eigene DLL über die
  Umgebungsvariable `LOGOTHERM_LIBUSB_DLL` oder neben `python.exe` legen.
- CAN-Einstellungen in `gateway.yaml`: `can.enabled: true`, `interface: gs_usb`,
  `channel: "0"` (Geräteindex, eine Zahl), `bitrate: 50000`. **Alte LC2 (AT90CAN128,
  Reglertyp 0x09xx) laufen fest mit 20000** (`App/LambdaControl 2/Config.h:45`).
- python-can 4.6.1 kann mit gs_usb keine 50/20 kbit/s einstellen; das Gateway rechnet die Bitzeit
  selbst (48 MHz, BRP 48, 20 TQ, Abtastpunkt 85 % wie die Firmware) – `gateway selftest` zeigt sie.
- Ein ausgesteckter InnoMaker ist unter gs_usb nicht als Fehler erkennbar, man sieht nur Timeouts
  (alle Geräte offline). Dann Adapter/Kabel prüfen und den Dienst neu starten.
- Weboberfläche: `http://127.0.0.1:8080/`; Modbus-TCP auf Port 502. Für Zugriffe von anderen
  Rechnern muss die Firewall Port 502 zulassen (siehe unten).
- Beenden mit `Strg+C`.
- Den Launcher kann man ohne Installation starten: `python launcher\launcher.py`.

## 5. Firewall

Für Modbus von anderen Rechnern (Administrator-PowerShell):

```powershell
netsh advfirewall firewall add rule name=LogothermGateway-Modbus-502 dir=in action=allow protocol=TCP localport=502
```

Entfernen: `netsh advfirewall firewall delete rule name=LogothermGateway-Modbus-502`.
Port 502 darf nur von einem Programm belegt sein; ein anderer Modbus-Dienst auf demselben
Rechner führt zu einem Startfehler (Log prüfen).

## 6. Fehlersuche

| Symptom | Abhilfe |
|---|---|
| Launcher: „Dienst nicht erreichbar“ | Läuft der Dienst (`services.msc`, *Logotherm Gateway*)? Port in `gateway.yaml` geändert? Log öffnen. |
| Launcher: „Dienst gestoppt“ | Im Launcher *Dienst neu starten*; Log auf Konfigurationsfehler prüfen. |
| Kein CAN-Adapter | WinUSB-Treiber (Zadig), libusb, anderes USB-Kabel/Buchse; kein Hersteller-Tool gleichzeitig offen. |
| COM-Port fehlt | Gerätemanager; Windows Update für den FTDI-Treiber ausführen. |
| Modbus-Client erreicht das Gateway nicht | Firewall-Regel, Allowlist (`modbus.allow`), Port. |

## 7. Installer selbst bauen

Der Installer wird **lokal in Docker** gebaut (kein GitHub Actions):
`release/LogothermGateway-Setup-<Version>.exe` plus `release/SHA256SUMS.txt`. `release/` ist nicht im
Git; veröffentlicht wird als **GitHub-Release**. Voraussetzung ist nur Docker Desktop im
Linux-Container-Modus, für `-Release` zusätzlich die GitHub-CLI (`winget install GitHub.cli`,
einmal `gh auth login`).

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build-docker.ps1            # nur bauen
powershell -ExecutionPolicy Bypass -File packaging\windows\build-docker.ps1 -Release   # bauen + Release v<Version>
```

Ablauf (`packaging/windows/docker/Dockerfile`):

1. Stufe `wdi`: `wdi-simple.exe` aus libwdi v1.5.1 per MinGW-w64-Cross-Build (nur WinUSB, 64 Bit).
2. Stufe `build`: Wine mit Windows-Python 3.12.10, Abhängigkeiten exakt aus `requirements.lock`,
   PyInstaller 6.22.3, Inno Setup 6.7.4, WinSW 2.12.0; `packaging/windows/docker/build.sh` baut
   Gateway-Exe und Launcher, prüft `gateway.exe --version` und kompiliert `setup.iss`.
3. Stufe `out`: nur die Setup-Exe → `release/`.

**Vor jedem Release** die Version in `pyproject.toml` (`[project] version`) hochzählen – sie
landet im Dateinamen der Setup-Exe und in `gateway --version` – und in `CHANGELOG.md` einen
Abschnitt `## <Version> – <Datum>` anlegen; er wird zum Release-Text. Der Launcher findet das neue
Release automatisch (Update-Prüfung beim Start und täglich).

Der erste Build dauert länger (Images und Downloads), danach greift der Docker-Cache.

Hinweis für die Entwicklung: Im PyInstaller-Build liegen statische Dateien (Weboberfläche)
unter `sys._MEIPASS` (bei one-folder = `<Installationsordner>\gateway\_internal`).
`gateway/web/app.py` löst `static` über `Path(__file__).parent` auf; PyInstaller setzt `__file__`
eingefrorener Module auf `_MEIPASS\gateway\web\…`, das passt zum Ziel in `gateway.spec`
(geprüft mit 0.1.1: `/static/app.js` usw. liefern 200).
