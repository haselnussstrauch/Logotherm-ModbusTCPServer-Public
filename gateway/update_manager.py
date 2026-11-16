"""Update aus der Weboberfläche: täglich prüfen, ob es eine neue Version gibt, und (nur beim
Windows-Dienst) auf Knopfdruck still installieren.

Installation unter Windows: Der Dienst lädt Setup und Prüfsumme (``gateway.updater.download``,
SHA256 geprüft) und startet das Setup **über die Aufgabenplanung** (eine einmalige Aufgabe als
SYSTEM). Ein direkt gestarteter Kindprozess würde beendet, wenn das Setup den Dienst anhält.
Das Setup hält den Dienst an, tauscht die Dateien und startet ihn wieder; die Weboberfläche ist
dabei etwa eine Minute weg.

Docker/Linux: kein Selbst-Update (das Image wird auf dem Host gebaut) – die Oberfläche zeigt den
Befehl zum Aktualisieren.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from gateway import __version__, updater

log = logging.getLogger(__name__)

FIRST_CHECK_DELAY_S = 60.0
TASK_NAME = "LogothermGatewayUpdate"
SERVICE_NAME = "LogothermGateway"
# Liegt während der stillen Installation in <Daten>/updates; der Launcher beendet sich, solange es sie
# gibt, und startet danach neu (sonst wäre LogothermGateway.exe gesperrt und das Setup bricht ab).
UPDATE_FLAG = "update-running"
FAILED_LOG_MAX_AGE_S = 2 * 24 * 3600
DOCKER_COMMAND = "cd ~/logotherm-gateway && git pull && cd docker && docker compose up -d --build"


def install_mode() -> str:
    """``windows`` (installierter Dienst, Selbst-Update möglich), ``docker`` oder ``manual``."""
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        return "windows"
    if Path("/.dockerenv").exists() or os.environ.get("GATEWAY_IN_DOCKER") == "1":
        return "docker"
    return "manual"


class UpdateManager:
    def __init__(self, data_dir: Path, enabled: Callable[[], bool], *, mode: str | None = None,
                 fetch=None, download=None, launch=None) -> None:
        self.data_dir = Path(data_dir)
        self.enabled = enabled
        self.mode = mode or install_mode()
        self._fetch = fetch or updater.fetch_latest
        self._download = download or updater.download
        self._launch = launch or launch_setup_windows
        self.release: updater.ReleaseInfo | None = None
        self.checked_at: float | None = None
        self.error = ""
        self.state = "idle"            # idle | checking | downloading | installing | error
        self.progress = ""
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._note_failed_update()

    def _note_failed_update(self) -> None:
        """Nach dem Neustart: wurde das letzte stille Update abgebrochen (Setup-Protokoll mit Rollback
        für eine neuere Version als die laufende)? Dann als Fehler anzeigen statt stillschweigend."""
        try:
            logs = sorted((self.data_dir / "logs").glob("update-setup-*.log"),
                          key=lambda f: f.stat().st_mtime, reverse=True)
            if not logs or time.time() - logs[0].stat().st_mtime > FAILED_LOG_MAX_AGE_S:
                return
            text = logs[0].read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        m = re.match(r"update-setup-(.+)\.log$", logs[0].name)
        if not m or not updater.is_newer(m.group(1), __version__) or "Rolling back changes" not in text:
            return
        reason = " (eine Datei war in Benutzung – Launcher-Fenster offen?)" if "appears to be in use" in text else ""
        self.state = "error"
        self.error = (f"Das Update auf {m.group(1)} wurde abgebrochen{reason}; Version {__version__} läuft "
                      f"weiter. Protokoll: logs\\{logs[0].name}")
        log.warning("Letztes Update abgebrochen: %s", logs[0])

    # --- Status --------------------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return self.release is not None and updater.is_newer(self.release.version, __version__)

    def status(self) -> dict:
        r = self.release if self.available else None
        return {
            "current": __version__, "check_enabled": bool(self.enabled()), "mode": self.mode,
            "available": r is not None, "latest": self.release.version if self.release else None,
            "notes": r.notes if r else "", "page_url": r.page_url if r else "",
            "checked_at": self.checked_at, "error": self.error, "state": self.state,
            "progress": self.progress, "can_install": r is not None and self.mode == "windows",
            "docker_command": DOCKER_COMMAND if self.mode == "docker" else "",
        }

    # --- Prüfen --------------------------------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="update-check")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task

    async def _loop(self) -> None:
        delay = FIRST_CHECK_DELAY_S
        while True:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            self._wake.clear()
            delay = updater.CHECK_INTERVAL_S
            if self.enabled():
                await self.check()

    async def check(self) -> dict:
        async with self._lock:
            if self.state in ("downloading", "installing"):
                return self.status()
            failed = self.error if self.state == "error" else ""
            self.state = "checking"
            try:
                self.release = await asyncio.to_thread(self._fetch)
                self.error = ""
                if self.available:
                    log.info("Neue Version %s verfügbar (installiert: %s)", self.release.version, __version__)
            except updater.UpdateError as exc:
                self.error = str(exc)
                log.info("Update-Prüfung: %s", exc)
            finally:
                self.checked_at = time.time()
                self.state = "idle"
                if failed and not self.error:       # Hinweis auf abgebrochenes Update bleibt stehen
                    self.state, self.error = "error", failed
        return self.status()

    # --- Installieren --------------------------------------------------------------------------
    async def install(self) -> dict:
        """Lädt das Setup, prüft SHA256 und startet die stille Installation (nur Windows-Dienst)."""
        if self.mode != "windows":
            raise ValueError("Selbst-Update gibt es nur beim installierten Windows-Dienst")
        async with self._lock:
            if self.state in ("downloading", "installing"):
                raise ValueError("Ein Update läuft bereits")
            info = self.release
            if info is None or not updater.is_newer(info.version, __version__):
                raise ValueError("Keine neuere Version verfügbar – zuerst nach Updates suchen")
            self.state, self.error, self.progress = "downloading", "", "Lade herunter …"
        try:
            def prog(done: int, total: int) -> None:
                self.progress = f"Lade herunter … {100 * done // total} %" if total else "Lade herunter …"
            setup = await asyncio.to_thread(self._download, info, self.data_dir / "updates", progress=prog)
            logf = self.data_dir / "logs" / f"update-setup-{info.version}.log"
            self.progress = "Installiere – die Weboberfläche ist gleich etwa eine Minute nicht erreichbar …"
            self.state = "installing"
            await asyncio.to_thread(self._launch, setup, logf, self.data_dir / "updates")
            log.warning("Update auf %s gestartet (%s) – der Dienst wird gleich vom Setup neu gestartet",
                        info.version, setup.name)
        except Exception as exc:  # noqa: BLE001
            self.state, self.error, self.progress = "error", f"Update fehlgeschlagen: {exc}", ""
            log.error("Update fehlgeschlagen: %s", exc)
        return self.status()


def launch_setup_windows(setup: Path, logf: Path, work_dir: Path, run=subprocess.run) -> None:
    """Startet das Setup über eine einmalige Aufgabe (SYSTEM) – unabhängig vom Dienstprozess."""
    work_dir.mkdir(parents=True, exist_ok=True)
    logf.parent.mkdir(parents=True, exist_ok=True)
    cmd = work_dir / "run-update.cmd"
    flag = work_dir / UPDATE_FLAG
    line = subprocess.list2cmdline(updater.silent_install_command(setup, logf))
    # Erst bis zu ~20 s warten, bis sich der Launcher beendet hat (er sieht die Markierung), dann Setup.
    # Danach den Dienst auf jeden Fall starten – bricht das Setup ab, bliebe er sonst gestoppt.
    cmd.write_text("@echo off\r\n"
                   "set n=0\r\n"
                   ":wait\r\n"
                   '"%SystemRoot%\\System32\\tasklist.exe" /FI "IMAGENAME eq LogothermGateway.exe" 2>nul'
                   ' | "%SystemRoot%\\System32\\find.exe" /I "LogothermGateway.exe" >nul || goto run\r\n'
                   "set /a n+=1\r\n"
                   "if %n% geq 10 goto run\r\n"
                   '"%SystemRoot%\\System32\\PING.EXE" -n 3 127.0.0.1 >nul\r\n'
                   "goto wait\r\n"
                   ":run\r\n"
                   f"{line}\r\n"
                   f"net start {SERVICE_NAME} >nul 2>&1\r\n"
                   f'del /F /Q "{flag}" >nul 2>&1\r\n'
                   f"schtasks /Delete /TN {TASK_NAME} /F >nul 2>&1\r\n", encoding="utf-8")
    flag.write_text(setup.name, encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    common = {"capture_output": True, "text": True, "timeout": 30, "creationflags": flags}
    res = run(["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "ONCE", "/ST", "00:00",
               "/RU", "SYSTEM", "/RL", "HIGHEST", "/TR", f'cmd.exe /c "{cmd}"'], **common)
    if res.returncode != 0:
        flag.unlink(missing_ok=True)
        raise RuntimeError(f"Aufgabe konnte nicht angelegt werden: {(res.stderr or res.stdout).strip()[:200]}")
    res = run(["schtasks", "/Run", "/TN", TASK_NAME], **common)
    if res.returncode != 0:
        flag.unlink(missing_ok=True)
        raise RuntimeError(f"Aufgabe konnte nicht gestartet werden: {(res.stderr or res.stdout).strip()[:200]}")
