"""Tests der Weboberfläche mit FastAPI-TestClient (benötigt httpx)."""

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from gateway.web.app import create_app  # noqa: E402
from test_web_fake import FakeService  # noqa: E402

CSRF = {"X-Requested-With": "gateway"}


@pytest.fixture
def svc():
    return FakeService()


@pytest.fixture
def client(svc):
    return TestClient(create_app(svc))


def add_device(client):
    info = client.post("/api/scan", headers=CSRF, json={"can": True}).json()["result"][0]
    r = client.post("/api/draft/devices", headers=CSRF, json=info)
    assert r.status_code == 200, r.text
    return r.json()


def test_static_pages(client):
    r = client.get("/")
    assert r.status_code == 200 and "Logotherm Gateway" in r.text
    for f in ("app.js", "style.css"):
        assert client.get(f"/static/{f}").status_code == 200


def test_status_and_logs(client):
    st = client.get("/api/status").json()
    assert st["running"] and st["polling"] == "running"
    assert "lines" in client.get("/api/logs?n=5").json()


def test_config_hides_hash(client, svc):
    svc.set_password("geheim1")
    client.post("/api/login", headers=CSRF, json={"password": "geheim1"})
    cfg = client.get("/api/config").json()
    assert "password_hash" not in cfg["active"]["web"] and "password_hash" not in cfg["draft"]["web"]
    assert cfg["password_set"] is True


def test_scan_add_enumerate_values_apply(client, svc):
    dev = add_device(client)
    assert dev["id"] == "can_11" and dev["unit_id"] == 0x11
    cfg = client.get("/api/config").json()
    assert cfg["dirty"] and any("neu" in c for c in cfg["changes"])

    job = client.post("/api/draft/devices/can_11/enumerate", headers=CSRF).json()
    assert client.get(f"/api/jobs/{job['id']}").json()["state"] == "done"
    rows = client.get("/api/draft/devices/can_11/values").json()["rows"]
    by = {(r["kind"], r["key"]): r for r in rows}
    assert by[("value", 0x10)]["value"] == 65.5 and by[("value", 0x10)]["active"] is False
    assert by[("value", 0x11)]["supported"] is False
    assert by[("param", 0x142)]["lock_reason"]

    r = client.patch("/api/draft/devices/can_11", headers=CSRF, json={
        "unit_id": 5, "entries": [{"kind": "value", "key": 0x10, "active": True, "writable": False},
                                  {"kind": "param", "key": 0x141, "active": True, "writable": True}]})
    assert r.status_code == 200
    assert svc.draft.device("can_11").unit_id == 5

    r = client.post("/api/draft/apply", headers=CSRF)
    assert r.json() == {"ok": True, "warnings": []}
    assert svc.cfg.device("can_11").params[0].writable
    assert not client.get("/api/config").json()["dirty"]
    assert client.get("/api/devices/can_11/values").json()["values"][0]["key"] == 0x10
    assert client.get("/api/writes").json()["writes"][0]["ok"] is True


def test_value_error_is_400(client):
    add_device(client)
    client.post("/api/draft/devices/can_11/enumerate", headers=CSRF)
    r = client.patch("/api/draft/devices/can_11", headers=CSRF,
                     json={"entries": [{"kind": "param", "key": 0x142, "active": True, "writable": True}]})
    assert r.status_code == 400 and "gesperrt" in r.json()["detail"]
    assert client.patch("/api/draft/settings/modbus", headers=CSRF, json={"port": 0}).status_code == 400
    assert client.patch("/api/draft/settings/gibtsnicht", headers=CSRF, json={}).status_code == 400


def test_unknown_device_404(client):
    assert client.patch("/api/draft/devices/x", headers=CSRF, json={}).status_code == 404
    assert client.get("/api/jobs/nix").status_code == 404


def test_settings_discard_delete(client, svc):
    assert client.patch("/api/draft/settings/modbus", headers=CSRF,
                        json={"allow": ["10.0.0.0/8"], "word_order": "CDAB"}).status_code == 200
    assert svc.draft.modbus.allow == ["10.0.0.0/8"]
    client.post("/api/draft/discard", headers=CSRF)
    assert svc.draft.modbus.allow == []
    add_device(client)
    assert client.delete("/api/draft/devices/can_11", headers=CSRF).status_code == 200
    assert svc.draft.devices == []


def test_renumber_needs_confirm(client, svc):
    add_device(client)
    r = client.post("/api/draft/devices/can_11/renumber", headers=CSRF, json={"table": "values"})
    assert r.status_code == 400
    r = client.post("/api/draft/devices/can_11/renumber", headers=CSRF, json={"table": "values", "confirm": True})
    assert r.status_code == 200 and svc.renumbered == ("can_11", "values")


def test_can_test_and_ports(client):
    r = client.post("/api/can/test", headers=CSRF, json={"interface": "virtual", "channel": "0", "bitrate": 50000}).json()
    assert r["result"]["ok"]
    r = client.post("/api/can/test", headers=CSRF, json={"interface": "kaputt", "channel": "0", "bitrate": 50000}).json()
    assert r["state"] == "error" and r["error"]
    assert client.get("/api/serial-ports").json()["ports"][0]["ftdi"] is True


def test_csv_download(client):
    r = client.get("/api/registers.csv")
    assert r.status_code == 200
    assert "registerliste.csv" in r.headers["content-disposition"]
    assert r.headers["content-type"].startswith("text/csv")
    assert r.content.startswith("﻿".encode())


def test_csrf_header_required(client):
    assert client.post("/api/draft/discard").status_code == 403
    assert client.post("/api/draft/discard", headers={"X-Requested-With": "x"}).status_code == 403
    assert client.post("/api/draft/discard", headers=CSRF).status_code == 200


def test_password_flow(client, svc):
    r = client.post("/api/password", headers=CSRF, json={"password": "kurz"})
    assert r.status_code == 400
    assert client.post("/api/password", headers=CSRF, json={"password": "geheim123"}).status_code == 200
    anon = TestClient(client.app)   # ohne Cookie
    assert anon.get("/api/config").status_code == 401
    assert anon.post("/api/draft/discard", headers=CSRF).status_code == 401
    assert "Anmeldung" in anon.get("/").text   # Login-Seite
    st = anon.get("/api/status").json()
    assert set(st) == {"running", "version", "online", "devices"}
    assert anon.post("/api/login", headers=CSRF, json={"password": "falsch"}).status_code == 401
    r = anon.post("/api/login", headers=CSRF, json={"password": "geheim123"})
    assert r.status_code == 200
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
    assert anon.get("/api/config").status_code == 200
    assert "diag" in anon.get("/api/status").json()
    anon.post("/api/logout", headers=CSRF)
    assert anon.get("/api/config").status_code == 401


def test_forged_cookie_rejected(client, svc):
    svc.set_password("geheim123")
    c = TestClient(client.app, cookies={"gw_session": "abc.def"})
    assert c.get("/api/config").status_code == 401


def test_first_start_setup_on_open_bind(svc, monkeypatch):
    """Docker/Linux: das erste Passwort darf jeder Client festlegen (Erststart im Container)."""
    monkeypatch.setattr("gateway.web.server.effective_bind", lambda cfg: "0.0.0.0")
    monkeypatch.setattr("gateway.web.app.LOCAL_SETUP_ONLY", False)
    c = TestClient(create_app(svc))
    assert c.get("/api/config").status_code == 403
    assert c.get("/api/auth/state").json()["setup_needed"] is True
    assert c.post("/api/setup-password", headers=CSRF, json={"password": "x"}).status_code == 400
    assert c.post("/api/setup-password", headers=CSRF, json={"password": "geheim123"}).status_code == 200
    assert c.get("/api/config").status_code == 200   # Sitzung gesetzt
    assert c.post("/api/setup-password", headers=CSRF, json={"password": "anderes1"}).status_code == 403


def test_windows_erstes_passwort_nur_am_gateway_pc(svc, monkeypatch):
    """Windows mit web.bind 0.0.0.0 ohne Passwort: am PC selbst ohne Passwort, aus dem Netz gesperrt;
    das erste Passwort lässt sich nur vom PC selbst festlegen."""
    monkeypatch.setattr("gateway.web.server.effective_bind", lambda cfg: "0.0.0.0")
    monkeypatch.setattr("gateway.web.app.LOCAL_SETUP_ONLY", True)
    app = create_app(svc)
    remote = TestClient(app, client=("192.168.6.110", 50000))
    local = TestClient(app, client=("127.0.0.1", 50001))
    st = remote.get("/api/auth/state").json()
    assert st["setup_needed"] and st["setup_local_only"] and not st["authorized"]
    assert remote.get("/api/config").status_code == 403
    assert "Anmeldung" in remote.get("/").text                      # Login-Seite statt Oberfläche
    r = remote.post("/api/setup-password", headers=CSRF, json={"password": "geheim123"})
    assert r.status_code == 403 and "Gateway-PC" in r.json()["detail"]
    assert not svc.cfg.web.password_hash
    # am Gateway-PC: ohne Passwort bedienbar, Passwort setzen geht
    assert local.get("/api/auth/state").json()["authorized"]
    assert local.get("/api/config").status_code == 200
    assert local.post("/api/password", headers=CSRF, json={"password": "geheim123"}).status_code == 200
    # danach aus dem Netz: Anmeldung nötig und möglich
    assert remote.get("/api/config").status_code == 401
    assert remote.post("/api/login", headers=CSRF, json={"password": "geheim123"}).status_code == 200
    assert remote.get("/api/config").status_code == 200
    # und am PC selbst jetzt ebenfalls mit Passwort
    assert TestClient(app, client=("127.0.0.1", 50002)).get("/api/config").status_code == 401
