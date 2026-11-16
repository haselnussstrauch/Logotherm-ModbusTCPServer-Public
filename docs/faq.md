# Häufige Fragen (FAQ)

## Adresse und Netzwerk

### Ich finde die Adresse meines Raspberry Pi nicht mehr. Wie komme ich auf die Weboberfläche?

Über den **Namen** des Pi, die IP-Adresse brauchst du dafür nicht:

```
http://logogateway.local:8080
```

Das tippst du im Browser eines PCs, Laptops oder Handys im **selben Netz** ein. `logogateway` ist der
Hostname, den du im Raspberry Pi Imager vergeben hast (Schritt 1 der Anleitung). Hast du dort einen
anderen Namen gewählt, nimmst du diesen, z. B. `http://heizung.local:8080`. Die Endung `.local`
gehört immer dazu.

Dasselbe gilt für SSH: `ssh <benutzer>@logogateway.local`

**Klappt `.local` nicht?**

- Nach dem Einschalten 2–3 Minuten warten. Der Name ist erst da, wenn der Pi fertig gestartet ist.
- Der Browser muss im **selben Netz** sein, also nicht im Gäste-WLAN und nicht über mobile Daten.
- Manche ältere Android-Geräte und manche Firmennetze kennen `.local` nicht. Dann über einen PC
  gehen oder die IP-Adresse verwenden, wie im nächsten Punkt beschrieben.

**Die IP-Adresse herausfinden:**

- **Im Router:** In der Geräteliste nach `logogateway` suchen. Bei der FritzBox steht sie unter
  *Heimnetz → Netzwerk*.
- **Per SSH am Pi:** `hostname -I` zeigt sie an. Die erste Zahl ist die Adresse.
- Dann im Browser `http://<IP-Adresse>:8080` öffnen, z. B. `http://192.168.6.8:8080`.

### Wie finde ich die Adresse beim Windows-PC?

Im **Launcher** (Startmenü → *Logotherm Gateway (Status)*), Bereich *Netzwerk (dieser PC)*. Dort
stehen die IP-Adresse und die Adresse der Weboberfläche für andere Geräte, z. B.
`http://192.168.6.22:8080`. Der Name des PCs funktioniert meist auch:
`http://<PC-Name>:8080`.

### Wie gebe ich dem Pi eine feste IP-Adresse?

Die Leittechnik bzw. SPS braucht eine Adresse, die sich nicht ändert.

**Empfohlen: im Router.** Dort für das Gerät `logogateway` eine feste Adresse einstellen. Bei der
FritzBox: *Heimnetz → Netzwerk → Gerät bearbeiten → „Diesem Netzwerkgerät immer die gleiche
IPv4-Adresse zuweisen“*. Der Pi bleibt auf DHCP, und du kannst dich nicht aussperren.

**Alternativ am Pi selbst** (per SSH):

1. Namen der Verbindung anzeigen:

   ```bash
   nmcli -t -f NAME,DEVICE connection show --active
   ```

   z. B. `netplan-eth0:eth0` (LAN) oder `netplan-wlan0-MeinWLAN:wlan0`. Den Teil vor dem
   Doppelpunkt brauchst du gleich.

2. Feste Adresse setzen. Name, Adressen und Gateway an dein Netz anpassen:

   ```bash
   sudo nmcli connection modify "netplan-eth0" ipv4.method manual ipv4.addresses 192.168.6.50/24 ipv4.gateway 192.168.6.1 ipv4.dns 192.168.6.1
   sudo nmcli connection up "netplan-eth0"
   ```

   Die SSH-Verbindung bricht dabei ab. Danach neu verbinden: `ssh <benutzer>@192.168.6.50`
   (oder weiter mit `logogateway.local`).

Zurück auf automatisch (DHCP):

```bash
sudo nmcli connection modify "netplan-eth0" ipv4.method auto ipv4.addresses "" ipv4.gateway "" ipv4.dns ""
sudo nmcli connection up "netplan-eth0"
```

> **Vorsicht:** Liegt die feste Adresse nicht im Netz deines Routers, ist der Pi danach nicht mehr
> erreichbar. Dann hilft nur Bildschirm und Tastatur am Pi (oder die SD-Karte neu beschreiben).
> Deshalb ist der Weg über den Router sicherer.

### Und beim Windows-PC?

Im Launcher: **IP-Adresse ändern …**, dann *Automatisch (DHCP)* oder *Manuell*. Windows fragt nach
Administratorrechten. Details: [windows.md](windows.md), Abschnitt *Launcher*.

### Ich habe zwei Gateways im selben Netz. Was muss ich beachten?

Jedes braucht einen **eigenen Hostnamen**, z. B. `logogateway1` und `logogateway2`. Sonst ist
nicht klar, welches Gerät sich unter `logogateway.local` meldet. Den Namen vergibst du am
einfachsten schon im Imager. Nachträglich geht es per SSH:

```bash
sudo hostnamectl set-hostname logogateway2
sudo sed -i 's/\blogogateway\b/logogateway2/g' /etc/hosts
sudo reboot
```

Danach ist der Pi unter `logogateway2.local` erreichbar.
