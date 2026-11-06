# Änderungen

Format: je Version ein Abschnitt `## <Version> – <Datum>`. Der Abschnitt der aktuellen Version wird
beim Veröffentlichen als Release-Text verwendet (`packaging/windows/build-docker.ps1 -Release`).

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
