"""Update aus der Weboberfläche (gateway/update_manager.py, /api/update*)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import __version__, update_manager, updater
from gateway.update_manager import TASK_NAME, UpdateManager, launch_setup_windows


def _info(version: str) -> updater.ReleaseInfo:
    return updater.ReleaseInfo(version=version, tag=f"v{version}", name="", notes="Neu: alles",
                               page_url=f"https://example/releases/v{version}",
                               exe_name=f"LogothermGateway-Setup-{version}.exe", exe_url="x", exe_size=1, sums_url="y")


def _mgr(tmp_path, version="99.0.0", mode="windows", fail=None, launched=None):
    def fetch():
        if fail:
            raise updater.UpdateError(fail)
        return _info(version)

    def download(info, dest, progress=None):
        dest.mkdir(parents=True, exist_ok=True)
        if progress:
            progress(1, 2)
        p = dest / info.exe_name
        p.write_bytes(b"setup")
        return p

    def launch(setup, logf, work):
        (launched if launched is not None else []).append((setup, logf, work))

    return UpdateManager(tmp_path, lambda: True, mode=mode, fetch=fetch, download=download, launch=launch)


def test_neue_version_wird_angezeigt(tmp_path):
    m = _mgr(tmp_path)
    st = asyncio.run(m.check())
    assert st["available"] and st["latest"] == "99.0.0" and st["current"] == __version__
    assert st["can_install"] and st["notes"] == "Neu: alles" and st["checked_at"]


def test_gleiche_version_kein_hinweis(tmp_path):
    st = asyncio.run(_mgr(tmp_path, version=__version__).check())
    assert not st["available"] and not st["can_install"] and st["latest"] == __version__


def test_offline_meldet_fehler_ohne_hinweis(tmp_path):
    st = asyncio.run(_mgr(tmp_path, fail="keine Internetverbindung").check())
    assert not st["available"] and "Internet" in st["error"]


def test_docker_zeigt_befehl_statt_knopf(tmp_path):
    st = asyncio.run(_mgr(tmp_path, mode="docker").check())
    assert st["available"] and not st["can_install"] and "docker compose up -d --build" in st["docker_command"]
    with pytest.raises(ValueError, match="nur beim installierten Windows-Dienst"):
        asyncio.run(_mgr(tmp_path, mode="docker").install())


def test_installieren(tmp_path):
    launched = []
    m = _mgr(tmp_path, launched=launched)

    async def run():
        await m.check()
        return await m.install()
    st = asyncio.run(run())
    assert st["state"] == "installing" and len(launched) == 1
    setup, logf, work = launched[0]
    assert setup == tmp_path / "updates" / "LogothermGateway-Setup-99.0.0.exe"
    assert logf == tmp_path / "logs" / "update-setup-99.0.0.log"


def test_installieren_ohne_neue_version(tmp_path):
    m = _mgr(tmp_path, version=__version__)
    with pytest.raises(ValueError, match="Keine neuere Version"):
        asyncio.run(m.install())


def test_installieren_fehler_wird_gemeldet(tmp_path):
    def launch(*a):
        raise RuntimeError("Aufgabe konnte nicht angelegt werden")
    m = _mgr(tmp_path)
    m._launch = launch

    async def run():
        await m.check()
        return await m.install()
    st = asyncio.run(run())
    assert st["state"] == "error" and "Aufgabe" in st["error"]


def test_aufgabenplanung_befehle(tmp_path):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    setup = tmp_path / "updates" / "LogothermGateway-Setup-1.2.3.exe"
    launch_setup_windows(setup, tmp_path / "logs" / "u.log", tmp_path / "updates", run=run)
    create, start = calls
    assert create[:3] == ["schtasks", "/Create", "/F"] and TASK_NAME in create
    assert create[create.index("/RU") + 1] == "SYSTEM"
    assert start == ["schtasks", "/Run", "/TN", TASK_NAME]
    script = (tmp_path / "updates" / "run-update.cmd").read_text(encoding="utf-8")
    assert "/VERYSILENT" in script and "/MERGETASKS=!winusb" in script and "/NORESTART" in script
    assert str(setup) in script and f"schtasks /Delete /TN {TASK_NAME}" in script


def test_aufgabenplanung_fehler(tmp_path):
    def run(cmd, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="Zugriff verweigert")
    with pytest.raises(RuntimeError, match="Zugriff verweigert"):
        launch_setup_windows(tmp_path / "s.exe", tmp_path / "l.log", tmp_path, run=run)


def test_install_mode_entwicklung():
    assert update_manager.install_mode() in ("manual", "docker")      # nicht eingefroren


def test_api(tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from gateway.web.app import create_app
    from test_dashboard import CSRF, make_svc

    svc = make_svc(tmp_path)
    svc.updates = _mgr(tmp_path, mode="docker")
    c = TestClient(create_app(svc))
    assert c.get("/api/update").json()["available"] is False
    st = c.post("/api/update/check", headers=CSRF).json()
    assert st["available"] and st["docker_command"]
    r = c.post("/api/update/install", headers=CSRF)
    assert r.status_code == 409
    assert c.get("/api/config").json()["version"] == __version__
    assert "updates" in c.get("/api/config").json()["draft"]
