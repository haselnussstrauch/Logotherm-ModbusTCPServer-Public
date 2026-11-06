"""Launcher-Updater: Versionsvergleich, Release-Abfrage, Download mit SHA256-Prüfung (ohne Netz)."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "launcher"))
import updater  # noqa: E402

EXE = b"MZ" + b"\x00" * 300_000
SHA = hashlib.sha256(EXE).hexdigest()


class Resp(io.BytesIO):
    def __init__(self, data: bytes, headers=None):
        super().__init__(data)
        self.headers = headers or {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def release_json(version="0.1.7", with_sums=True, exe_name=None):
    name = exe_name or f"LogothermGateway-Setup-{version}.exe"
    assets = [{"name": name, "size": len(EXE), "browser_download_url": f"https://dl/{name}"}]
    if with_sums:
        assets.append({"name": "SHA256SUMS.txt", "size": 100, "browser_download_url": "https://dl/SHA256SUMS.txt"})
    return {"tag_name": f"v{version}", "name": f"Logotherm Gateway {version}", "body": "- Neu: Updater",
            "html_url": f"https://github.com/x/y/releases/tag/v{version}", "assets": assets}


def make_opener(rel=None, sums=None, exe=EXE, error=None, seen=None):
    def opener(req, timeout=None):
        url = req.full_url
        if seen is not None:
            seen.append((url, req.get_header("User-agent"), req.get_header("Accept")))
        if error is not None:
            raise error
        if url.endswith("/releases/latest"):
            return Resp(json.dumps(rel).encode())
        if url.endswith("SHA256SUMS.txt"):
            return Resp((sums if sums is not None else f"{SHA}  LogothermGateway-Setup-0.1.7.exe\n").encode())
        if url.endswith(".exe"):
            return Resp(exe)
        raise AssertionError(url)
    return opener


def test_versionsvergleich():
    assert updater.is_newer("0.1.7", "0.1.6")
    assert updater.is_newer("0.2.0", "0.1.10")
    assert updater.is_newer("0.1.10", "0.1.9")          # numerisch, nicht als Text
    assert not updater.is_newer("0.1.6", "0.1.6")
    assert not updater.is_newer("0.1.5", "0.1.6")
    assert updater.is_newer("1.0", "0.9.9")
    assert updater.is_newer("0.1.7", None)               # Version unbekannt -> anbieten
    assert not updater.is_newer("kaputt", "0.1.6")
    assert updater.parse_version("v0.1.7") == (0, 1, 7)


def test_fetch_latest():
    seen = []
    info = updater.fetch_latest("x/y", opener=make_opener(release_json(), seen=seen))
    assert info.version == "0.1.7" and info.exe_name == "LogothermGateway-Setup-0.1.7.exe"
    assert info.exe_url == "https://dl/LogothermGateway-Setup-0.1.7.exe" and info.notes == "- Neu: Updater"
    url, ua, accept = seen[0]
    assert url == "https://api.github.com/repos/x/y/releases/latest"
    assert ua == "LogothermGateway-Updater" and "github" in accept


@pytest.mark.parametrize("rel,msg", [
    (release_json(with_sums=False), "Prüfsummendatei"),
    (release_json(exe_name="anderes.exe"), "Installer"),
])
def test_fetch_latest_unvollstaendig(rel, msg):
    with pytest.raises(updater.UpdateError, match=msg):
        updater.fetch_latest("x/y", opener=make_opener(rel))


@pytest.mark.parametrize("err,msg", [
    (urllib.error.HTTPError("u", 404, "nf", {}, None), "Noch keine Version"),
    (urllib.error.HTTPError("u", 403, "rl", {}, None), "begrenzt"),
    (urllib.error.URLError("offline"), "keine Internetverbindung"),
])
def test_fetch_latest_fehler(err, msg):
    with pytest.raises(updater.UpdateError, match=msg):
        updater.fetch_latest("x/y", opener=make_opener(error=err))


def test_download_ok(tmp_path):
    info = updater.fetch_latest("x/y", opener=make_opener(release_json()))
    prog = []
    path = updater.download(info, tmp_path, opener=make_opener(), progress=lambda d, t: prog.append((d, t)))
    assert path.name == "LogothermGateway-Setup-0.1.7.exe" and path.read_bytes() == EXE
    assert prog[-1] == (len(EXE), len(EXE))
    assert not list(tmp_path.glob("*.part"))


def test_download_falsche_pruefsumme_verwirft(tmp_path):
    info = updater.fetch_latest("x/y", opener=make_opener(release_json()))
    with pytest.raises(updater.UpdateError, match="Prüfsumme stimmt nicht"):
        updater.download(info, tmp_path, opener=make_opener(exe=EXE + b"manipuliert"))
    assert list(tmp_path.iterdir()) == []                 # nichts Ausführbares liegen gelassen


def test_download_ohne_eintrag_in_sums(tmp_path):
    info = updater.fetch_latest("x/y", opener=make_opener(release_json()))
    with pytest.raises(updater.UpdateError, match="Keine Prüfsumme"):
        updater.download(info, tmp_path, opener=make_opener(sums=f"{SHA}  andere.exe\n"))


def test_expected_sha256_formate():
    assert updater.expected_sha256(f"{SHA.upper()}  a.exe\r\n", "a.exe") == SHA
    assert updater.expected_sha256(f"{SHA} *a.exe", "a.exe") == SHA
