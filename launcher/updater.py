"""Update-Prüfung für den Launcher: neueste Version aus den GitHub-Releases des öffentlichen Repos.

Ablauf: ``fetch_latest()`` fragt ``/repos/<repo>/releases/latest`` ab (nur veröffentlichte, keine
Vorab-Releases), sucht das Asset ``LogothermGateway-Setup-<Version>.exe`` und ``SHA256SUMS.txt``.
``download()`` lädt beides, prüft die SHA256-Summe und legt den Installer erst danach unter seinem
Namen ab. ``run_installer()`` startet das Setup mit Administratorrechten (UAC).

Nur Standardbibliothek. Netzwerkzugriffe sind über ``opener`` austauschbar (Tests).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: Öffentliches Repo mit den Releases (überschreibbar per Umgebungsvariable, z. B. für Tests)
UPDATE_REPO = os.environ.get("LOGOTHERM_UPDATE_REPO", "haselnussstrauch/Logotherm-ModbusTCPServer-Public")
API_LATEST = "https://api.github.com/repos/{repo}/releases/latest"
ASSET_RE = re.compile(r"^LogothermGateway-Setup-(\d+(?:\.\d+){1,3})\.exe$")
SUMS_NAME = "SHA256SUMS.txt"
USER_AGENT = "LogothermGateway-Updater"
#: AppId aus packaging/windows/setup.iss -> Uninstall-Eintrag mit DisplayVersion
APP_ID = "{6B0B6F57-4C0B-4C2E-9D58-3E6A3C1B7A10}_is1"
CHECK_INTERVAL_S = 24 * 3600


class UpdateError(Exception):
    """Prüfung/Download fehlgeschlagen (Text für den Anwender)."""


@dataclass
class ReleaseInfo:
    version: str
    tag: str
    name: str
    notes: str
    page_url: str
    exe_name: str
    exe_url: str
    exe_size: int
    sums_url: str


def parse_version(text: str | None) -> tuple[int, ...] | None:
    m = re.search(r"(\d+(?:\.\d+){1,3})", text or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def is_newer(candidate: str | None, installed: str | None) -> bool:
    c, i = parse_version(candidate), parse_version(installed)
    if c is None:
        return False
    if i is None:
        return True
    n = max(len(c), len(i))
    return c + (0,) * (n - len(c)) > i + (0,) * (n - len(i))


def _get(url: str, opener, timeout: float, accept: str = "application/octet-stream"):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    return opener(req, timeout=timeout)


def fetch_latest(repo: str = UPDATE_REPO, *, timeout: float = 10.0, opener=urllib.request.urlopen) -> ReleaseInfo:
    """Neuestes Release. Wirft ``UpdateError`` (offline, kein Release, kein Installer im Release)."""
    try:
        with _get(API_LATEST.format(repo=repo), opener, timeout, "application/vnd.github+json") as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("Noch keine Version veröffentlicht") from exc
        if exc.code == 403:
            raise UpdateError("GitHub hat die Abfrage begrenzt – später erneut versuchen") from exc
        raise UpdateError(f"Update-Server antwortet mit Fehler {exc.code}") from exc
    except (OSError, ValueError) as exc:
        raise UpdateError(f"Update-Prüfung nicht möglich (keine Internetverbindung?): {exc}") from exc
    assets = {a.get("name"): a for a in data.get("assets") or [] if isinstance(a, dict)}
    exe = next(((n, a) for n, a in assets.items() if n and ASSET_RE.match(n)), None)
    sums = assets.get(SUMS_NAME)
    if exe is None or sums is None:
        raise UpdateError("Im neuesten Release fehlt der Installer oder die Prüfsummendatei")
    name, a = exe
    return ReleaseInfo(version=ASSET_RE.match(name).group(1), tag=str(data.get("tag_name") or ""),
                       name=str(data.get("name") or ""), notes=str(data.get("body") or ""),
                       page_url=str(data.get("html_url") or ""), exe_name=name,
                       exe_url=str(a.get("browser_download_url")), exe_size=int(a.get("size") or 0),
                       sums_url=str(sums.get("browser_download_url")))


def expected_sha256(sums_text: str, file_name: str) -> str:
    """SHA256 einer Datei aus ``SHA256SUMS.txt`` (Format ``<hex>  <name>``, auch ``*<name>``)."""
    for line in sums_text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip("*") == file_name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    raise UpdateError(f"Keine Prüfsumme für {file_name} gefunden")


def download(info: ReleaseInfo, dest_dir: Path, *, progress: Callable[[int, int], None] | None = None,
             timeout: float = 30.0, opener=urllib.request.urlopen) -> Path:
    """Lädt den Installer, prüft SHA256 und gibt den Pfad zurück. Bei Abweichung wird nichts abgelegt."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        with _get(info.sums_url, opener, timeout) as r:
            want = expected_sha256(r.read().decode("utf-8", "replace"), info.exe_name)
        part = dest_dir / (info.exe_name + ".part")
        h = hashlib.sha256()
        done = 0
        with _get(info.exe_url, opener, timeout) as r, part.open("wb") as fh:
            total = int(r.headers.get("Content-Length") or info.exe_size or 0)
            while chunk := r.read(256 * 1024):
                fh.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except UpdateError:
        raise
    except OSError as exc:
        raise UpdateError(f"Download fehlgeschlagen: {exc}") from exc
    if h.hexdigest() != want:
        part.unlink(missing_ok=True)
        raise UpdateError("Prüfsumme stimmt nicht – Download verworfen (beschädigt oder manipuliert)")
    final = dest_dir / info.exe_name
    final.unlink(missing_ok=True)
    part.rename(final)
    return final


def installed_version() -> str | None:
    """Installierte Version laut Uninstall-Eintrag des Setups (Windows), sonst None."""
    if sys.platform != "win32":
        return None
    import winreg
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}",
                                    0, winreg.KEY_READ | view) as k:
                    return str(winreg.QueryValueEx(k, "DisplayVersion")[0])
            except OSError:
                continue
    return None


def run_installer(path: Path) -> bool:
    """Startet das Setup mit UAC-Abfrage (normaler Assistent, damit die Aufgaben sichtbar sind)."""
    if sys.platform != "win32":
        return False
    import ctypes
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(path), None, str(Path(path).parent), 1)
    return rc > 32
