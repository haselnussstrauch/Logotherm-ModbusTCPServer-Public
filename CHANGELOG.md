# Änderungen

Format: je Version ein Abschnitt `## <Version> – <Datum>`. Der Abschnitt der aktuellen Version wird
beim Veröffentlichen als Release-Text verwendet (`packaging/windows/build-docker.ps1 -Release`).

## 0.1.12 – 2026-11-16

- **Update aus der Weboberfläche repariert:** Bei offenem Launcher-Fenster brach das stille Setup ab
  (Launcher-Datei gesperrt), und der Dienst blieb gestoppt. Jetzt schließt sich der Launcher während
  des Updates selbst und öffnet sich danach wieder. Das Setup beendet einen noch laufenden Launcher
  und startet den Dienst auch nach einem Abbruch wieder. Ein abgebrochenes Update meldet die
  Weboberfläche jetzt, statt minutenlang zu warten.
- Hinweis für das Update von 0.1.10/0.1.11: Das Launcher-Fenster wird beim Web-Update geschlossen
  und öffnet sich dieses eine Mal noch nicht von selbst (Startmenü → *Logotherm Gateway (Status)*).

## 0.1.11 – 2026-11-16

- **Menü am Handy:** Auf schmalen Bildschirmen gibt es statt der waagrecht scrollenden Reiterleiste einen
  Menüknopf oben rechts (zeigt die aktuelle Seite); ein Tipp klappt die Liste aller Seiten auf.
- **Update-Hinweis am Raspberry Pi:** *Kopieren* des Update-Befehls funktioniert jetzt auch über
  `http://` (vorher passierte nichts). Das Docker-Image kennzeichnet sich zusätzlich selbst
  (`GATEWAY_IN_DOCKER=1`).

## 0.1.10 – 2026-11-16

- **Update-Knopf in der Weboberfläche:** Gibt es eine neue Version, erscheint oben auf jeder Seite ein
  Hinweis. Beim Windows-Dienst installiert *Jetzt installieren* das Update still (Download mit
  Prüfsumme, Setup über die Aufgabenplanung, Seite lädt danach neu). Beim Raspberry Pi/Docker zeigt der
  Hinweis den Befehl zum Aktualisieren. Abschaltbar unter *1 Schnittstellen → Updates*.

## 0.1.9 – 2026-11-16

- **Launcher: IP-Adresse des PCs.** Das Statusfenster zeigt die aktuelle IP-Adresse (DHCP/manuell) und
  die Adresse der Weboberfläche für andere Geräte. *IP-Adresse ändern …*: DHCP oder feste Adresse mit
  Maske, Gateway und DNS – mit Prüfung der Eingaben und UAC-Abfrage.
- **Modbus-TCP-Server abschaltbar** (*1 Schnittstellen → Modbus-TCP-Server aktiv*), z. B. wenn nur
  das Dashboard gebraucht wird. Kein Modbus-Port offen, keine Allowlist-Warnung; ein/aus ohne Neustart.
  Das Dashboard zeigt den Modbus-Zustand als eigene Kachel.
- **CAN-Adapter im Betrieb ab- und wieder angesteckt:** Das Gateway erkennt das nach etwa 2 s und
  öffnet den Adapter beim nächsten Zugriff selbst neu (bisher war ein Neustart nötig). Unter Linux
  wird dazu auch erkannt, dass `can0` neu angelegt wurde.
- **CAN-Test unter Linux (SocketCAN):** zeigt die tatsächlich eingestellte Bitrate, Abtastpunkt und
  Zustand von `can0` und meldet einen Fehler samt Befehl zur Korrektur, wenn sie nicht zur Konfiguration
  passt.
- **Docker/Raspberry Pi: serielle Adapter ohne Einrichtung.** USB-Seriell-Adapter (FTDI) erscheinen
  automatisch in der Weboberfläche, auch später angesteckte; der Eintrag in `compose.yaml` entfällt.
  Wer ihn früher von Hand eingetragen hat: vor dem Update `git checkout docker/compose.yaml`.
- **Doku:** Raspberry-Pi-Anleitungen ohne Bildschirm (Pi 4/5/400 und Pi 3 B+), `RestartSec` aus der
  `can0`-Konfiguration entfernt (der InnoMaker unterstützt das nicht), neue [FAQ](https://github.com/haselnussstrauch/Logotherm-ModbusTCPServer-Public/blob/main/docs/faq.md)
  (Adresse des Pi finden, feste IP, mehrere Gateways).

## 0.1.8 – 2026-11-06

- **Auswahltexte:** Werte, die der Regler als Text anzeigt (z. B. Betriebsart, Kesselstatus), holt
  das Gateway beim Laden der Werteliste direkt vom Regler – in dessen eingestellter Sprache. Dazu
  JA/NEIN und EIN/AUS. Die Weboberfläche zeigt den Text (Werteliste, Dashboard, Status, Verlauf),
  im Parametermenü gibt es eine Auswahlliste. Die Registerliste nennt die Zuordnung.
  **Modbus liefert weiterhin die Zahl** (Index) – für die Leittechnik ändert sich nichts.
- Bestehende Anlagen: einmal „Werteliste vom Gerät laden“, damit die Texte übernommen werden.

## 0.1.7 – 2026-11-06

- **Update-Funktion im Launcher:** prüft beim Start und danach täglich, ob es auf GitHub eine neue
  Version gibt; „Herunterladen und installieren“ lädt das Setup, prüft die SHA256-Prüfsumme und
  startet es. Konfiguration, Passwort und Aufzeichnung bleiben erhalten.
- Installer werden als GitHub-Release veröffentlicht.

## 0.1.6 – 2026-11-05

- **Dashboard** als Startseite: Dienststatus, letzte Aktualisierung, Regler online, ausgewählte
  Messwerte als Kacheln (Klick öffnet den Verlauf) und Parameter-Schnellzugriff. Auswahl über die
  Spalte „Dashboard“ der Werteliste.
- **Weboberfläche im Netzwerk:** Installer-Aufgabe gibt die Oberfläche für andere PCs und
  Smartphones im lokalen Netz frei (Firewall-Regel TCP 8080, nur lokales Subnetz). Aus dem Netz nur
  mit Passwort; das erste Passwort lässt sich nur am Gateway-PC festlegen.
- Neues Farbschema, Darstellung für Smartphones.

## 0.1.5 – 2026-11-05

- **Parameter setzen** über die Weboberfläche (Reiter „7 Parameter“) – gleiche Prüfungen wie beim
  Schreiben per Modbus (Freigaben, Sperrliste, Min/Max, 10 Minuten je Parameter, Tagesbudget,
  Rückmeldung). Schreib-Historie mit Quelle (Web/Modbus).

## 0.1.4 – 2026-11-05

- **Aufzeichnung** aller aktiven Werte (Standard alle 60 s, 14 Tage), eine Datei je Tag, tageweise
  löschbar, CSV-Export.
- **Diagramme** (Reiter „6 Verlauf“): mehrere Werte, je Einheit ein Diagramm, Tooltip, Tabelle.

## 0.1.3 – 2026-11-05

- 32-Bit-Ganzzahlen (z. B. Seriennummern) zusätzlich exakt als UInt32/Int32 ab Register 3000.
- Registerliste mit Spalte „Nr“; Ersatznamen für Werte ohne Text.
- Gerätestatus „letzter Fehlercode“ geht nach erfolgreichem Abruf auf 0 zurück.
- Weboberfläche: LC1-Anschlüsse waren in der Gerätesuche nicht auswählbar.

## 0.1.2 – 2026-11-05

- USB-CAN-Adapter musste nach Gerätesuche oder CAN-Test neu eingesteckt werden (USB-Zugriff wurde
  nicht freigegeben).

## 0.1.1 – 2026-11-05

- Launcher zeigt bei gesetztem Passwort die Geräteanzahl richtig an.

## 0.1.0 – 2026-11-04

- Erste Version: Modbus-TCP-Server für Logotherm-Regler über CAN (LambdaControl 2, MultiControl 2,
  LC1 BSB, Erweiterungen, Zündung, WRS-D, O2-Sonde) und RS232 (LambdaControl 1), Weboberfläche,
  Windows-Dienst mit Installer und Launcher, Docker.
