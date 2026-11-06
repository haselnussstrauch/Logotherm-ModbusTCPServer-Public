# Logotherm-Gateway auf dem Raspberry Pi – Schritt für Schritt

Diese Anleitung bringt das Gateway auf einem **Raspberry Pi 400** zum Laufen (gilt genauso für
Raspberry Pi 4 und 5). Vorkenntnisse in Linux sind nicht nötig: Du kopierst die grau hinterlegten Befehle der Reihe nach
in ein Befehlsfenster und drückst jeweils **Enter**.

**Kein Bildschirm nötig:** Der Pi wird komplett vom Windows-PC aus eingerichtet – über das Netzwerk
per **SSH** (ein Befehlsfenster auf dem PC, das auf dem Pi arbeitet) und danach im Browser.

> **Gut zu wissen:** Befehle mit `sudo` fragen manchmal nach deinem Passwort (das vom Pi-Benutzer).
> Beim Tippen erscheinen dabei **keine Sternchen** – einfach blind eintippen und Enter drücken.

Dauer: etwa 1 Stunde, davon die meiste Zeit Warten.

---

## Was du brauchst

| | |
|---|---|
| Raspberry Pi 400 | mit **originalem Netzteil** (USB-C) – Bildschirm und Maus sind **nicht** nötig |
| microSD-Karte | mindestens 16 GB, gute Marke (z. B. SanDisk/Samsung, „A1“ oder „A2“) |
| Netzwerk | am besten **LAN-Kabel** zum Router (WLAN geht auch) und Internet |
| CAN-Adapter | InnoMaker USB2CAN (für LambdaControl 2, MultiControl 2 usw.) – [getesteter Adapter bei Amazon](https://www.amazon.de/dp/B09K3LL93Q) |
| optional | FTDI-USB-Seriell-Adapter (nur für eine LambdaControl 1 über RS232) – [getesteter Adapter: DIGITUS DA-70156 bei Amazon](https://www.amazon.de/dp/B0030IT780) |
| ein Windows-PC | im selben Netz, mit SD-Kartenleser – zum Beschreiben der Karte und zum Einrichten |

---

## Schritt 1 – Betriebssystem auf die SD-Karte

Am **PC**:

1. **Raspberry Pi Imager** herunterladen und installieren: <https://www.raspberrypi.com/software/>
2. SD-Karte in den PC stecken, Imager starten.
3. *Raspberry-Pi-Modell* → **Raspberry Pi 400**.
4. *Betriebssystem* → *Raspberry Pi OS (other)* → **Raspberry Pi OS Lite (64-bit)**.
   Wichtig: **64-bit**. „Lite“ ist die Version ohne Desktop – ideal ohne Bildschirm
   (die normale „Raspberry Pi OS (64-bit)“ funktioniert aber genauso).
5. *SD-Karte* → deine Karte auswählen → **Weiter**.
6. **Anpassung** (Imager 2.x: Seiten links nacheinander; ältere Imager: „Einstellungen bearbeiten“):
   - **Hostname:** `logogateway`
   - **Lokalisierung:** Zeitzone Europe/Vienna (bzw. Europe/Berlin), Tastatur de
   - **Benutzer:** Benutzername und Passwort festlegen (merken!)
   - **WLAN:** nur eintragen, wenn du kein LAN-Kabel verwendest (Land: AT bzw. DE).
     Besser ist aber ein LAN-Kabel – dann findet der PC den Pi sicher.
   - **Fernzugriff (SSH):** **SSH aktivieren → Ein**, *Passwort zur Authentifizierung verwenden*.
     **Unbedingt einschalten** – ohne SSH kommst du ohne Bildschirm nicht auf den Pi.
   - *Raspberry Pi Connect:* nicht nötig, aus lassen
   - **Schreiben** → Warnung bestätigen. Der Imager schreibt und prüft die Karte (5–15 min).
7. SD-Karte in den Pi 400 stecken (Schlitz hinten), LAN-Kabel anschließen, **zuletzt** das
   Netzteil. Der erste Start dauert **2–5 Minuten** (der Pi startet dabei evtl. einmal neu).

## Schritt 1b – Vom PC aus mit dem Pi verbinden (SSH)

Am **PC**:

1. Startmenü → **PowerShell** (oder *Terminal* / *Eingabeaufforderung*) öffnen.
2. Eingeben – statt `pi` deinen Benutzernamen aus Schritt 1:

   ```powershell
   ssh pi@logogateway.local
   ```

3. Beim ersten Mal kommt die Frage *Are you sure you want to continue connecting?* → `yes` tippen,
   Enter.
4. Passwort eingeben (beim Tippen erscheint **nichts**) → Enter.

Steht vorne eine Zeile wie `pi@logogateway:~ $`, bist du auf dem Pi. **Alle folgenden Befehle
gehören in dieses Fenster.**

> **Einfügen:** in PowerShell mit **Rechtsklick** (oder Strg + V).

> **Klappt `logogateway.local` nicht** („Could not resolve hostname“ oder keine Antwort):
> - 2 Minuten warten und erneut versuchen,
> - sonst im **Router** (z. B. FritzBox: *Heimnetz → Netzwerk*) das Gerät `logogateway` suchen und
>   dessen IP-Adresse verwenden, z. B. `ssh pi@192.168.6.40`.

> **Nach jedem Neustart des Pi** (`sudo reboot`) bricht die Verbindung ab („Connection closed“).
> Eine Minute warten, dann denselben `ssh`-Befehl wieder eingeben (Pfeiltaste ↑ holt ihn zurück).

---

## Schritt 2 – System aktualisieren

```bash
sudo apt update && sudo apt full-upgrade -y
```

Das dauert einige Minuten. Danach neu starten:

```bash
sudo reboot
```

Nach etwa einer Minute wieder per `ssh` verbinden (Schritt 1b).

---

## Schritt 3 – Docker installieren

Docker ist das Programm, in dem das Gateway läuft. Installieren mit dem offiziellen Skript:

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
```

Deinem Benutzer erlauben, Docker zu verwenden, dann neu starten:

```bash
sudo usermod -aG docker $USER
sudo reboot
```

Nach dem Neustart wieder per `ssh` verbinden und prüfen:

```bash
docker --version
docker compose version
```

Beide Befehle sollen eine Versionsnummer zeigen (keine Fehlermeldung).

---

## Schritt 4 – Gateway herunterladen

```bash
sudo apt install -y git
cd ~
git clone https://github.com/haselnussstrauch/Logotherm-ModbusTCPServer-Public.git logotherm-gateway
```

Das Gateway liegt jetzt im Ordner `logotherm-gateway` in deinem Benutzerordner.

---

## Schritt 5 – CAN-Adapter einrichten

1. **InnoMaker USB2CAN** an einen USB-Anschluss des Pi 400 stecken.
2. Prüfen, ob der Pi ihn erkennt:

   ```bash
   ip link show can0
   ```

   Erscheint eine Zeile mit `can0`, ist alles gut. Bei „Device "can0" does not exist“: Adapter
   abziehen, 5 Sekunden warten, an einen anderen USB-Anschluss stecken, Befehl wiederholen.

3. Damit der Adapter nach jedem Start automatisch mit der richtigen Geschwindigkeit läuft
   (**50 kbit/s**), diese vier Befehle nacheinander ausführen:

   ```bash
   printf '[Match]\nName=can0\n\n[CAN]\nBitRate=50K\n' | sudo tee /etc/systemd/network/80-can.network
   sudo systemctl enable --now systemd-networkd
   sudo systemctl disable systemd-networkd-wait-online.service
   sudo networkctl reload && sudo networkctl reconfigure can0
   ```

   (Der dritte Befehl verhindert, dass der Pi beim Start bis zu 2 Minuten auf den CAN-Adapter wartet,
   falls dieser einmal nicht angesteckt ist. Das übrige Netzwerk – LAN/WLAN – bleibt unverändert.)

4. Kontrolle:

   ```bash
   ip -details link show can0
   ```

   In der Ausgabe soll **`state UP`** (bzw. `ERROR-ACTIVE`) und **`bitrate 50000`** stehen.

> **Ältere LambdaControl 2** (Reglertyp 0x09xx) laufen mit 20 kbit/s. Dann in Punkt 3 `BitRate=20K`
> statt `50K` verwenden und später in der Weboberfläche ebenfalls 20 kbit/s einstellen.
> Ein Bus kann nur **eine** Geschwindigkeit.

---

## Schritt 6 (nur mit LambdaControl 1 über RS232) – Serieller Adapter

Ohne LambdaControl 1 am RS232: diesen Schritt überspringen.

Den FTDI-Adapter (z. B. DIGITUS DA-70156) einfach an einen USB-Anschluss stecken – **sonst ist nichts
einzurichten**. Das Gateway sieht ihn automatisch, auch wenn er erst später angesteckt wird. Kontrolle:

```bash
ls /dev/serial/by-id/
```

Es erscheint z. B. `usb-FTDI_FT232R_USB_UART_BG035PRC-if00-port0`. In der Weboberfläche (Schritt 9,
*2 Geräte suchen*) steht der Adapter dann unter den seriellen Anschlüssen – einfach ankreuzen.

---

## Schritt 7 – Gateway starten

```bash
cd ~/logotherm-gateway/docker
docker compose up -d --build
```

Beim **ersten Mal** baut der Pi das Programm zusammen – das dauert **15 bis 30 Minuten**. Einfach
warten, bis wieder die Eingabezeile erscheint. Danach prüfen:

```bash
docker compose ps
```

In der Spalte *STATUS* soll nach etwa einer Minute **`Up … (healthy)`** stehen. Die laufenden
Meldungen ansehen (Beenden mit **Strg + C**, das Gateway läuft weiter):

```bash
docker compose logs -f gateway
```

Das Gateway startet ab jetzt **automatisch bei jedem Einschalten** des Pi.

---

## Schritt 8 – Adresse des Pi herausfinden

```bash
hostname -I
```

Die erste Zahl (z. B. `192.168.6.40`) ist die Adresse des Pi im Netz (meist geht auch einfach
`logogateway.local`). Tipp: Im Router für den Pi eine
**feste IP-Adresse** (DHCP-Reservierung) einstellen – die Leittechnik braucht eine Adresse, die sich
nicht ändert.

---

## Schritt 9 – Einrichten in der Weboberfläche

1. Am PC (oder Smartphone im selben Netz) den Browser öffnen:
   `http://<Adresse des Pi>:8080`, z. B. `http://192.168.6.40:8080` oder <http://logogateway.local:8080>
2. **Sofort ein Passwort festlegen** (mindestens 6 Zeichen). Bis dahin ist die Oberfläche für jeden
   im Netz offen.
3. **1 Schnittstellen → CAN-Bus:**
   - Haken bei **CAN-Bus verwenden**
   - Adapter **`socketcan`**, Kanal **`can0`**, Bitrate **50 kbit/s** (bzw. 20)
   - **Übernehmen (Entwurf)**, dann **CAN-Adapter testen** – alles sollte „OK“ sein
4. **1 Schnittstellen → Modbus-TCP-Server:** unter *Erlaubte Clients* die IP-Adresse der
   Leittechnik bzw. SPS eintragen (sonst darf jedes Gerät im Netz zugreifen) → **Übernehmen**.
5. **2 Geräte suchen:** *Suche starten* → gefundene Regler übernehmen. (LambdaControl 1 über RS232:
   den seriellen Anschluss ankreuzen.)
6. **3 Werteliste:** je Regler *Werteliste vom Gerät laden*, gewünschte Werte bei **bereitstellen**
   ankreuzen, für die Startseite auch bei **Dashboard** → *Änderungen in Entwurf übernehmen*.
7. **4 Speichern:** *Speichern & übernehmen*. Dort gibt es auch die **Registerliste als CSV** für
   die Leittechnik.

Fertig – das **Dashboard** zeigt jetzt die Werte, unter **6 Verlauf** werden sie aufgezeichnet.
Modbus TCP ist unter `<Adresse des Pi>:502` erreichbar.

---

## Updates

Neue Versionen holst du so (Einstellungen und Aufzeichnung bleiben erhalten):

```bash
cd ~/logotherm-gateway
git pull
cd docker
docker compose up -d --build
```

Welche Version läuft: `docker compose exec gateway gateway --version`

---

## Sicherung

Alles Wichtige (Einstellungen, Passwort, Aufzeichnung, Logs) liegt im Ordner
`~/logotherm-gateway/docker/data`. Für eine Sicherung diesen Ordner z. B. auf einen USB-Stick kopieren.

---

## Wichtig zu wissen

- **Uhrzeit:** Der Pi 400 hat **keine eingebaute Uhr mit Batterie**. Er holt die Zeit nach jedem Start
  aus dem Internet. Ohne Internet kann die Uhr nach einem Stromausfall falsch gehen – dann stimmen die
  Zeitstempel der Aufzeichnung nicht. Abhilfe: Internetzugang lassen oder einen Zeitserver im eigenen
  Netz eintragen.
- **Ausschalten:** Nicht einfach den Stecker ziehen, sondern per `ssh` verbinden, `sudo poweroff`
  eingeben und warten, bis die grüne LED aus ist. Plötzliches Abschalten kann die SD-Karte beschädigen.
- **SD-Karte:** Für den Dauerbetrieb ist eine gute Karte wichtig. Noch robuster: Pi von einer
  USB-SSD starten.
- Schreiben auf die Regler ist ab Werk **aus** und nur für freigegebene Parameter möglich.

---

## Wenn etwas nicht klappt

| Problem | Lösung |
|---|---|
| `ssh` findet `logogateway.local` nicht | IP-Adresse aus dem Router verwenden; LAN-Kabel steckt? Pi fertig gestartet (2–5 min)? |
| `ssh`: „Connection refused“ | SSH wurde im Imager nicht eingeschaltet → Karte neu beschreiben (Schritt 1, Fernzugriff) |
| `ssh`: „REMOTE HOST IDENTIFICATION HAS CHANGED“ | Karte wurde neu beschrieben – am PC `ssh-keygen -R logogateway.local` ausführen, dann neu verbinden |
| `ssh`: „Permission denied“ | Benutzername oder Passwort falsch (Groß-/Kleinschreibung beachten) |
| CAN-Test: „Network is down“ oder `networkctl list` zeigt bei `can0` **failed** | `cat /etc/systemd/network/80-can.network` – steht dort `RestartSec`, die Datei mit dem Befehl aus Schritt 5 neu schreiben (der InnoMaker unterstützt das nicht), dann `sudo networkctl reload && sudo networkctl reconfigure can0` |
| Serieller Adapter fehlt in der Weboberfläche | `ls /dev/serial/by-id/` zeigt nichts → anderer USB-Anschluss/Kabel. Zeigt er etwas: Update holen (siehe *Updates*), ältere Versionen brauchten einen Eintrag in `compose.yaml` |
| `can0` gibt es nicht | Adapter in anderen USB-Anschluss, `dmesg \| tail` zeigt, ob er erkannt wird |
| CAN-Test: „kein Busverkehr“ | 1. Zeile *Bitrate* im CAN-Test: steht dort **50000 bit/s**? Sonst Schritt 5 wiederholen. 2. Verkabelung: CAN-H an CAN-H, CAN-L an CAN-L (vertauscht = kein Verkehr), GND verbinden. 3. Abschlusswiderstand: Ist der Pi ein Ende des Busses, am InnoMaker den 120-Ω-Jumper setzen. 4. Regler eingeschaltet? Zum Vergleich am Pi: `candump can0` (aus Paket `can-utils`, Beenden mit Strg + C) |
| `permission denied` bei `docker` | Schritt 3: `sudo usermod -aG docker $USER` und neu starten |
| Status nicht „healthy“ | `docker compose logs gateway` ansehen |
| Weboberfläche nicht erreichbar | `docker compose ps` – läuft das Gateway? Richtige Adresse (`hostname -I`)? |
| Port 502 belegt | ein anderes Modbus-Programm läuft auf dem Pi |
| Passwort vergessen | in `~/logotherm-gateway/docker/data/gateway.yaml` die Zeile `password_hash:` löschen, dann `docker compose restart gateway` und neues Passwort setzen |

Fragen: siehe „Fragen & Kontakt“ im [README](../README.md).
