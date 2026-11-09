# Betrieb mit Docker (Raspberry Pi / Linux)

Das Gateway läuft als Container (`amd64` und `arm64`, also auch Raspberry Pi 4/5 mit 64-bit-OS).
Alle Dateien liegen in `docker/`.

**Raspberry Pi für Einsteiger:** Schritt-für-Schritt-Anleitung in [raspberry-pi.md](raspberry-pi.md)
(Pi 4, 5, 400) bzw. [raspberry-pi-3.md](raspberry-pi-3.md) (Pi 3 B+).

## Überblick

| Was | Wert |
|---|---|
| Datenverzeichnis im Container | `/data` (Volume; `gateway.yaml`, `logs/`, Zustand) |
| Weboberfläche | Port 8080 |
| Modbus-TCP | Port 502 |
| Netzwerk | `network_mode: host` (nötig, damit der Container `can0` des Hosts sieht) |
| Standard-Befehl | `gateway run` |

## 1. CAN-Adapter (InnoMaker USB2CAN) als `can0`

Der InnoMaker-Adapter nutzt die candleLight-Firmware und wird vom Linux-Kernelmodul `gs_usb`
automatisch als SocketCAN-Schnittstelle `can0` bereitgestellt. Prüfen:

```bash
lsusb                       # Adapter sichtbar?
ip -details link show can0  # Schnittstelle vorhanden?
```

Die Schnittstelle muss mit **50 kbit/s** (LC2) gestartet werden. Zwei Möglichkeiten:

### Variante A (empfohlen): der Host richtet `can0` ein

Einmalig von Hand (geht beim Neustart verloren):

```bash
sudo ip link set can0 type can bitrate 50000
sudo ip link set can0 up
```

Dauerhaft mit systemd-networkd (`/etc/systemd/network/80-can.network`):

```ini
[Match]
Name=can0

[CAN]
BitRate=50K
```

```bash
sudo systemctl enable --now systemd-networkd
sudo networkctl reload && sudo networkctl reconfigure can0
```

Hinweis: Auf Raspberry Pi OS wird standardmäßig oft NetworkManager oder dhcpcd benutzt;
`systemd-networkd` kann parallel nur für `can0` aktiv sein (die `.network`-Datei wirkt nur auf `can0`).
Läuft der Adapter mit 20 kbit/s (ältere LC2-Hardware, siehe Konfig `can.bitrate`), `BitRate=20K` setzen.

### Variante B: der Container richtet `can0` ein

In `docker/compose.yaml` die Zeilen `cap_add: [NET_ADMIN]` und die Umgebungsvariablen
`CAN_SETUP: "1"`, `CAN_IFACE`, `CAN_BITRATE` einkommentieren. Der Entrypoint führt dann aus:

```bash
ip link set can0 down
ip link set can0 type can bitrate 50000
ip link set can0 up
```

Das funktioniert nur mit `network_mode: host`, sonst ändert der Container nur seinen eigenen
Netzwerk-Namespace. `NET_ADMIN` erlaubt dem Container, die Netzwerkkonfiguration des Hosts zu ändern;
Variante A vermeidet das.

## 2. Start

```bash
cd docker
docker compose up -d --build
docker compose logs -f gateway
```

Daten liegen in `docker/data/` (Volume `./data:/data`). Beim ersten Start erzeugt der Entrypoint
`/data/gateway.yaml` mit Defaults (SocketCAN, `can0`, Weboberfläche auf `0.0.0.0:8080`), falls sie fehlt.
Eine vorhandene Konfiguration wird nie überschrieben.

**Erster Start:** Weboberfläche `http://<pi-adresse>:8080` öffnen und **sofort ein Passwort setzen**.
Bis dahin ist die Oberfläche (`web.bind: 0.0.0.0`) ungeschützt im Netz erreichbar. Danach in der
Oberfläche CAN aktivieren, Geräte suchen und Werte auswählen. Ebenso unter `modbus.allow` die IPs der
Modbus-Clients eintragen, sonst darf jeder Client im Netz zugreifen.

## 3. Serielle Geräte (LC1 über FTDI)

Nichts einzurichten: `compose.yaml` bindet `/dev` des Hosts ein und erlaubt per
`device_cgroup_rules` nur USB-Seriell-Adapter (`ttyUSB*` = Major 188, `ttyACM*` = Major 166).
Andere Geräte (z. B. die SD-Karte) bleiben für den Container gesperrt. Adapter erscheinen damit
auch nach späterem Anstecken samt stabilem Pfad unter `/dev/serial/by-id/` in der Weboberfläche;
dort diesen Pfad als Port verwenden (`/dev/ttyUSB0` ändert sich je nach Steckreihenfolge).

```bash
ls -l /dev/serial/by-id/
```

Läuft der Container nicht als Root, muss der User Mitglied der Gruppe `dialout` sein
(`group_add: ["dialout"]`).

## 4. Befehle im Container

```bash
docker compose run --rm gateway scan          # Geräte suchen
docker compose run --rm gateway export-csv -o /data/register.csv
docker compose run --rm gateway selftest      # Inbetriebnahme-Test
docker compose run --rm gateway --version
```

`scan` greift auf den CAN-Bus zu: Dafür den laufenden Dienst vorher stoppen
(`docker compose stop gateway`), damit nicht zwei Programme gleichzeitig auf dem Bus senden.
`export-csv` ohne `-o` schreibt nach stdout.

## 5. Nicht als Root laufen

Standard ist Root im Container; bei SocketCAN mit Host-Netzwerk ist das üblich. Ohne Root:

- `user: "1000:1000"` in `compose.yaml`; `./data` muss diesem User gehören.
- Port 502 ist ein privilegierter Port. Entweder `modbus.port` auf einen Wert über 1024 stellen oder
  auf dem Host `sudo sysctl -w net.ipv4.ip_unprivileged_port_start=0` setzen (dauerhaft in
  `/etc/sysctl.d/`). `cap_add: [NET_BIND_SERVICE]` allein genügt für Nicht-Root-Prozesse nicht,
  da Docker keine Ambient-Capabilities setzt.
- `CAN_SETUP=1` funktioniert nicht ohne Root; `can0` dann auf dem Host einrichten (Variante A).

## 6. Image bauen

Aus dem Repo-Root (Build-Kontext ist das Repo, nicht `docker/`):

```bash
docker build -f docker/Dockerfile -t logotherm-gateway .

# Multi-Arch (einmalig: docker buildx create --use)
docker buildx build --platform linux/amd64,linux/arm64 -f docker/Dockerfile \
    -t <registry>/logotherm-gateway:0.1.8 --push .
# ohne Registry nur für die eigene Architektur in den lokalen Docker laden:
docker buildx build --platform linux/arm64 -f docker/Dockerfile -t logotherm-gateway --load .
```

Die Abhängigkeiten werden exakt aus `requirements.lock` installiert. Das Image wird **lokal**
gebaut (kein GitHub Actions).

## 7. Problemlösung

| Symptom | Ursache / Abhilfe |
|---|---|
| `can0` fehlt | Adapter nicht erkannt; `dmesg | tail`, anderes USB-Kabel/Port, `lsmod | grep gs_usb` |
| `Network is down` | `can0` nicht gestartet (Variante A oder B) |
| Container „unhealthy“ | `docker compose logs gateway`; Konfig `gateway.yaml` ungültig? |
| Port 502 belegt | Anderer Modbus-Dienst auf dem Host; `sudo ss -ltnp | grep :502` |
| Web nicht erreichbar | `web.bind` muss `0.0.0.0` sein, Firewall des Hosts prüfen |
