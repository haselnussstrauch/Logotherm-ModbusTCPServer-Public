"""Logotherm Gateway – Launcher (Windows).

Kleines Statusfenster für den Windows-Dienst `LogothermGateway`: zeigt Status, Version, Geräte
online und Warnungen (Abfrage von http://127.0.0.1:<port>/api/status alle 3 s) und bietet
„Einrichtung öffnen“, „Dienst neu starten“, „Protokoll öffnen“ und „USB-CAN-Treiber installieren“.
Prüft beim Start und danach täglich, ob es eine neue Version gibt (updater.py, GitHub-Releases), und
installiert sie auf Knopfdruck.

Nur Standardbibliothek (PyYAML wird genutzt, falls vorhanden). Die Status-Logik steht oben im
Modul und kommt ohne tkinter aus (testbar, siehe tests/test_launcher.py); die Oberfläche
(`LauncherWindow`) importiert tkinter erst bei Bedarf.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

SERVICE_NAME = "LogothermGateway"
DEFAULT_PORT = 8080
POLL_INTERVAL_S = 3.0
HTTP_TIMEOUT_S = 2.0

# Zustände von StatusResult.state
RUNNING = "running"          # Weboberfläche antwortet
STOPPED = "stopped"          # Windows-Dienst ist angehalten
UNREACHABLE = "unreachable"  # keine Antwort, Dienst nicht als angehalten bekannt


# --- Pfade / Konfiguration ----------------------------------------------------------------------
def data_dir() -> Path:
    """Datenverzeichnis wie im Gateway (`GATEWAY_DATA_DIR`, sonst %ProgramData%\\LogothermGateway)."""
    if env := os.environ.get("GATEWAY_DATA_DIR"):
        return Path(env)
    return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "LogothermGateway"


def config_path() -> Path:
    return data_dir() / "gateway.yaml"


def log_path() -> Path:
    return data_dir() / "logs" / "gateway.log"


def _port_from_text(text: str) -> int | None:
    """Liest `web.port` ohne YAML-Bibliothek: erster `port:` im Abschnitt `web:`."""
    in_web = False
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].rstrip()
        if not stripped.strip():
            continue
        if not line[:1].isspace():                      # Zeile auf oberster Ebene
            in_web = re.fullmatch(r"web\s*:", stripped) is not None
            continue
        if in_web and (m := re.fullmatch(r"\s+port\s*:\s*['\"]?(\d+)['\"]?", stripped)):
            return int(m.group(1))
    return None


def read_web_port(path: Path | None = None) -> int:
    """`web.port` aus gateway.yaml; bei jedem Problem Default 8080."""
    path = path or config_path()
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return DEFAULT_PORT
    port: int | None = None
    try:
        import yaml  # optional

        data = yaml.safe_load(text)
        web = data.get("web") if isinstance(data, dict) else None
        if isinstance(web, dict) and "port" in web:
            port = int(web["port"])
    except ImportError:
        port = _port_from_text(text)
    except Exception:  # noqa: BLE001 - kaputtes YAML: tolerant auf Textsuche zurückfallen
        port = _port_from_text(text)
    return port if port is not None and 1 <= port <= 65535 else DEFAULT_PORT


# --- Status ---------------------------------------------------------------------------------
@dataclass
class StatusResult:
    state: str                         # RUNNING | STOPPED | UNREACHABLE
    version: str = ""
    devices_total: int | None = None   # None = unbekannt (z. B. Passwortschutz)
    devices_online: int | None = None
    warnings: list[str] = field(default_factory=list)
    detail: str = ""                   # Fehlertext bei nicht erreichbar

    @property
    def headline(self) -> str:
        return {RUNNING: "Dienst läuft", STOPPED: "Dienst gestoppt",
                UNREACHABLE: "Dienst nicht erreichbar"}[self.state]

    @property
    def devices_text(self) -> str:
        if self.devices_online is None:
            return "–"
        if self.devices_total is None:
            return f"{self.devices_online} online"
        return f"{self.devices_online} von {self.devices_total} online"


def parse_status(data: object) -> StatusResult:
    """Wertet die JSON-Antwort von /api/status aus (auch die reduzierte Fassung bei Passwortschutz)."""
    if not isinstance(data, dict):
        return StatusResult(UNREACHABLE, detail="Unerwartete Antwort")
    total = online = None
    devices = data.get("devices")
    if isinstance(devices, list):
        total = len(devices)
        online = sum(1 for d in devices if isinstance(d, dict) and d.get("online"))
    else:
        # Reduzierte Fassung (gateway/web/app.py, Passwortschutz): {"online": n, "devices": m}
        if isinstance(data.get("online"), int):
            online = data["online"]
        elif isinstance(data.get("devices_online"), int):
            online = data["devices_online"]
        if online is not None and isinstance(devices, int):
            total = devices
    raw_warnings = data.get("warnings")
    warnings = [str(w) for w in raw_warnings] if isinstance(raw_warnings, list) else []
    if not data.get("running", True):
        return StatusResult(UNREACHABLE, str(data.get("version", "")), total, online, warnings,
                            "Dienst meldet running=false")
    return StatusResult(RUNNING, str(data.get("version", "")), total, online, warnings)


def fetch_status(port: int, timeout: float = HTTP_TIMEOUT_S, host: str = "127.0.0.1") -> StatusResult:
    """Fragt /api/status ab. Wirft nie; Fehler werden zu UNREACHABLE."""
    url = f"http://{host}:{port}/api/status"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return parse_status(json.load(resp))
    except urllib.error.HTTPError as exc:
        # 401/403 bei Passwortschutz: Dienst läuft, nur ohne Details.
        if exc.code in (401, 403):
            return StatusResult(RUNNING, detail="Details durch Passwortschutz verborgen")
        return StatusResult(UNREACHABLE, detail=f"HTTP {exc.code}")
    except (OSError, ValueError) as exc:  # URLError ist ein OSError; ValueError = kein JSON
        return StatusResult(UNREACHABLE, detail=str(getattr(exc, "reason", exc)))


def service_state(name: str = SERVICE_NAME) -> str | None:
    """Zustand des Windows-Dienstes laut `sc query` (z. B. 'RUNNING', 'STOPPED'); None wenn unbekannt."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(["sc", "query", name], capture_output=True, text=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"STATE\s*:\s*\d+\s+(\w+)", out)
    return m.group(1) if m else None


def get_status(port: int | None = None) -> StatusResult:
    """HTTP-Abfrage; bei Fehlschlag zusätzlich Dienstzustand (gestoppt vs. nicht erreichbar)."""
    result = fetch_status(port if port is not None else read_web_port())
    if result.state != RUNNING:
        svc = service_state()
        if svc == "STOPPED":
            result.state = STOPPED
        elif svc is None and sys.platform == "win32":
            result.detail = (result.detail + " (Dienst nicht installiert?)").strip()
    return result


# --- Aktionen -------------------------------------------------------------------------------
def _is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


RESTART_COMMAND = f"Restart-Service -Name '{SERVICE_NAME}' -Force"


def restart_service() -> bool:
    """Startet den Dienst neu. Als Administrator direkt, sonst per UAC-Abfrage („runas“)."""
    if sys.platform != "win32":
        return False
    args = ["-NoProfile", "-WindowStyle", "Hidden", "-Command", RESTART_COMMAND]
    if _is_admin():
        subprocess.Popen(["powershell.exe", *args],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    import ctypes

    # ShellExecuteW liefert einen Wert > 32 bei Erfolg (UAC bestätigt), sonst einen Fehlercode.
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe",
                                             subprocess.list2cmdline(args), None, 0)
    return rc > 32


def driver_script(exe: Path | None = None) -> Path:
    """install-winusb.cmd der Installation: {app}/launcher/LogothermGateway.exe -> {app}/driver."""
    exe = exe or Path(sys.executable if getattr(sys, "frozen", False) else __file__)
    return exe.resolve().parent.parent / "driver" / "install-winusb.cmd"


def install_driver(script: Path | None = None) -> bool:
    """Startet die WinUSB-Installation (wdi-simple) mit Administratorrechten (UAC-Abfrage)."""
    script = script or driver_script()
    if sys.platform != "win32" or not script.exists():
        return False
    import ctypes

    params = subprocess.list2cmdline(["/c", str(script)])
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", params, str(script.parent), 0)
    return rc > 32


def open_setup(port: int | None = None) -> None:
    webbrowser.open(f"http://127.0.0.1:{port if port is not None else read_web_port()}/")


def open_log() -> None:
    """Öffnet gateway.log (sonst den Logordner, sonst das Datenverzeichnis)."""
    for target in (log_path(), log_path().parent, data_dir()):
        if target.exists():
            os.startfile(str(target))  # type: ignore[attr-defined]  # nur Windows
            return
    raise FileNotFoundError(f"Kein Protokoll gefunden unter {data_dir()}")


# --- Oberfläche -----------------------------------------------------------------------------
class LauncherWindow:
    """Tk-Fenster; fragt den Status in einem Hintergrund-Thread ab und rendert per `after`."""

    def __init__(self, check_updates: bool | None = None) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.root = tk.Tk()
        self.root.title("Logotherm Gateway")
        self.root.resizable(False, False)
        frm = ttk.Frame(self.root, padding=16)
        frm.grid()

        self.headline = tk.StringVar(value="Prüfe …")
        self.version = tk.StringVar(value="–")
        self.devices = tk.StringVar(value="–")
        self.warn_text = tk.StringVar(value="")

        self.headline_label = tk.Label(frm, textvariable=self.headline,
                                       font=("Segoe UI", 14, "bold"), anchor="w")
        self.headline_label.grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(frm, text="Version:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Label(frm, textvariable=self.version).grid(row=1, column=1, sticky="w", pady=(8, 0), padx=8)
        ttk.Label(frm, text="Geräte:").grid(row=2, column=0, sticky="w")
        ttk.Label(frm, textvariable=self.devices).grid(row=2, column=1, sticky="w", padx=8)
        ttk.Label(frm, text="Warnungen:").grid(row=3, column=0, sticky="nw")
        ttk.Label(frm, textvariable=self.warn_text, wraplength=320, justify="left").grid(
            row=3, column=1, sticky="w", padx=8)

        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=2, pady=(14, 0), sticky="ew")
        ttk.Button(btns, text="Einrichtung öffnen", command=self._on_open).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(btns, text="Dienst neu starten", command=self._on_restart).grid(row=0, column=1, padx=6)
        ttk.Button(btns, text="Protokoll öffnen", command=self._on_log).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(btns, text="USB-CAN-Treiber installieren", command=self._on_driver).grid(
            row=1, column=0, columnspan=3, pady=(6, 0), sticky="ew")

        # Update-Zeile
        upd = ttk.Frame(frm)
        upd.grid(row=5, column=0, columnspan=2, pady=(14, 0), sticky="ew")
        self.update_text = tk.StringVar(value="")
        ttk.Label(upd, textvariable=self.update_text, wraplength=340, justify="left").grid(row=0, column=0, sticky="w")
        self.update_btn = ttk.Button(upd, text="Nach Updates suchen", command=self._on_update_button)
        self.update_btn.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self._upd: dict = {"state": "idle", "text": "", "release": None, "file": None}
        self._upd_lock = threading.Lock()
        if check_updates is None:
            check_updates = os.environ.get("LOGOTHERM_UPDATE_CHECK", "1") != "0"
        if check_updates:
            self.root.after(3000, self._auto_check)

        self._latest: StatusResult | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._poller = threading.Thread(target=self._poll_loop, daemon=True)
        self._poller.start()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(200, self._render)

    # Hintergrund-Thread: blockierende Abfrage darf die Oberfläche nicht einfrieren.
    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            result = get_status(read_web_port())
            with self._lock:
                self._latest = result
            self._stop.wait(POLL_INTERVAL_S)

    def _render(self) -> None:
        with self._lock:
            result = self._latest
        if result:
            color = {RUNNING: "#1a7f37", STOPPED: "#9a6700", UNREACHABLE: "#cf222e"}[result.state]
            self.headline.set(result.headline)
            self.headline_label.configure(fg=color)
            self.version.set(result.version or "–")
            self.devices.set(result.devices_text if result.state == RUNNING else "–")
            lines = list(result.warnings)
            if result.state != RUNNING and result.detail:
                lines.append(result.detail)
            self.warn_text.set("\n".join(lines) if lines else "keine")
        self._render_update()
        self.root.after(500, self._render)

    # --- Update ------------------------------------------------------------------------------
    def _set_upd(self, **kw) -> None:
        with self._upd_lock:
            self._upd.update(kw)

    def _installed_version(self) -> str | None:
        import updater
        if v := updater.installed_version():
            return v
        with self._lock:
            return self._latest.version if self._latest and self._latest.version else None

    def _auto_check(self) -> None:
        import updater
        self._start_check(silent=True)
        self.root.after(int(updater.CHECK_INTERVAL_S * 1000), self._auto_check)

    def _start_check(self, silent: bool) -> None:
        with self._upd_lock:
            if self._upd["state"] in ("checking", "downloading"):
                return
            self._upd.update(state="checking", text="" if silent else "Suche nach Updates …")

        def work() -> None:
            import updater
            try:
                info = updater.fetch_latest()
            except updater.UpdateError as exc:
                self._set_upd(state="idle", text="" if silent else str(exc))
                return
            inst = self._installed_version()
            if updater.is_newer(info.version, inst):
                self._set_upd(state="available", release=info,
                              text=f"Neue Version {info.version} verfügbar (installiert: {inst or 'unbekannt'}).")
            else:
                self._set_upd(state="idle", text="" if silent else f"Aktuell – Version {inst or info.version}.")
        threading.Thread(target=work, daemon=True).start()

    def _on_update_button(self) -> None:
        from tkinter import messagebox
        with self._upd_lock:
            state, info = self._upd["state"], self._upd["release"]
        if state != "available" or info is None:
            self._start_check(silent=False)
            return
        notes = (info.notes or "").strip()
        if len(notes) > 700:
            notes = notes[:700].rsplit("\n", 1)[0] + "\n…"
        if not messagebox.askokcancel(
                "Logotherm Gateway – Update",
                f"Version {info.version} herunterladen und installieren?\n\n"
                + (f"Neu:\n{notes}\n\n" if notes else "")
                + "Der Dienst wird während der Installation kurz angehalten. Konfiguration, Passwort und "
                  "Aufzeichnung bleiben erhalten. Das Setup fragt nach Administratorrechten."):
            return
        self._set_upd(state="downloading", text="Lade herunter …")

        def work() -> None:
            import tempfile
            import updater

            def prog(done: int, total: int) -> None:
                pct = f"{100 * done // total} %" if total else f"{done // (1024 * 1024)} MB"
                self._set_upd(text=f"Lade herunter … {pct}")
            try:
                path = updater.download(info, Path(tempfile.gettempdir()) / "LogothermGateway-Update", progress=prog)
            except updater.UpdateError as exc:
                self._set_upd(state="available", text=f"Update fehlgeschlagen: {exc}")
                return
            self._set_upd(state="ready", file=path, text="Prüfsumme in Ordnung – starte Setup …")
        threading.Thread(target=work, daemon=True).start()

    def _render_update(self) -> None:
        with self._upd_lock:
            state, text, path = self._upd["state"], self._upd["text"], self._upd["file"]
        if state == "ready":
            import updater
            from tkinter import messagebox
            self._set_upd(state="idle", file=None)
            if updater.run_installer(path):
                self._on_close()           # Setup ersetzt auch den Launcher und startet ihn danach neu
                return
            messagebox.showerror("Logotherm Gateway", "Das Setup konnte nicht gestartet werden "
                                 f"(Administratorrechte verweigert?). Datei: {path}")
            self._set_upd(state="available", text=f"Setup liegt bereit: {path}")
            return
        self.update_text.set(text)
        label = {"available": "Herunterladen und installieren", "downloading": "Lädt …",
                 "checking": "Suche …"}.get(state, "Nach Updates suchen")
        self.update_btn.configure(text=label, state="disabled" if state in ("downloading", "checking") else "normal")

    def _on_open(self) -> None:
        open_setup()

    def _on_restart(self) -> None:
        from tkinter import messagebox

        if not restart_service():
            messagebox.showerror("Logotherm Gateway", "Der Dienst konnte nicht neu gestartet werden "
                                 "(Administratorrechte verweigert oder Dienst nicht installiert).")

    def _on_driver(self) -> None:
        from tkinter import messagebox

        if not messagebox.askokcancel(
                "Logotherm Gateway",
                "Den WinUSB-Treiber für den USB-CAN-Adapter (InnoMaker USB2CAN) jetzt installieren?\n\n"
                "Adapter bitte vorher anstecken. Die Installation läuft im Hintergrund (bis ca. 1 min); "
                "danach „Dienst neu starten“."):
            return
        if not install_driver():
            messagebox.showerror("Logotherm Gateway", "Die Treiber-Installation konnte nicht gestartet "
                                 f"werden (Administratorrechte verweigert oder {driver_script()} fehlt).")

    def _on_log(self) -> None:
        from tkinter import messagebox

        try:
            open_log()
        except OSError as exc:
            messagebox.showinfo("Logotherm Gateway", str(exc))

    def _on_close(self) -> None:
        self._stop.set()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    LauncherWindow().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
