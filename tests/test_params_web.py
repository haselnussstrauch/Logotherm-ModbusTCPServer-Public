"""Menü „Parameter setzen“: Übersicht, Schreiben über die Weboberfläche (gleicher Weg wie Modbus)."""

from __future__ import annotations

import pytest

from gateway.errors import WriteRejected
from gateway.service import Service
from test_service import FakeDriver, make_cfg

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from gateway.web.app import create_app  # noqa: E402

CSRF = {"X-Requested-With": "gateway"}


def make_svc(tmp_path, write=True) -> Service:
    s = Service(make_cfg(write=write), tmp_path / "gateway.yaml")
    s._drivers["can"] = FakeDriver()
    s.fake = s._drivers["can"]
    s.store.set_value("can-01", "param", 0x0141, 60.0)
    return s


async def test_web_schreiben_gleicher_weg_wie_modbus(tmp_path):
    svc = make_svc(tmp_path)
    entry = await svc.write_param_web("can-01", 0x0141, 65.0)
    assert entry["ok"] and entry["readback"] == 65.0 and entry["source"] == "Web"
    assert svc.fake.writes == [(0x0141, 65.0)]
    assert svc.write_history()[0]["source"] == "Web"
    # 10-Minuten-Regel gilt auch hier
    with pytest.raises(WriteRejected, match="Ratenlimit"):
        await svc.write_param_web("can-01", 0x0141, 70.0)
    assert len(svc.fake.writes) == 1


async def test_web_schreiben_grenzen_und_unbekannt(tmp_path):
    svc = make_svc(tmp_path)
    with pytest.raises(WriteRejected, match="Maximum"):
        await svc.write_param_web("can-01", 0x0141, 95.0)
    with pytest.raises(WriteRejected, match="nicht konfiguriert"):
        await svc.write_param_web("can-01", 0x0999, 1.0)
    assert svc.fake.writes == []


def test_uebersicht_gruende(tmp_path):
    svc = make_svc(tmp_path, write=False)
    p = svc.param_overview("can-01")["params"][0]
    assert not p["can_write"] and "nicht als änderbar" in p["why"]
    svc.cfg.devices[0].params[0].writable = True
    p = svc.param_overview("can-01")["params"][0]
    assert not p["can_write"] and "global gesperrt" in p["why"]
    svc.cfg.safety.write_enabled = True
    ov = svc.param_overview("can-01")
    p = ov["params"][0]
    assert p["can_write"] and p["why"] == "" and p["value"] == 60.0 and p["nr"] == "0x0141"
    assert ov["budget_remaining"] == ov["budget"] == 50
    svc.guard.commit("can-01", 0x0141)
    p = svc.param_overview("can-01")["params"][0]
    assert not p["can_write"] and "Wartezeit" in p["why"] and 590 < p["wait_s"] <= 600


def test_wait_s():
    from gateway.config import SafetyConfig
    from gateway.safety import WriteGuard
    t = [1000.0]
    g = WriteGuard(SafetyConfig(write_enabled=True), clock=lambda: t[0])
    assert g.wait_s("d", 1) == 0
    g.commit("d", 1)
    t[0] += 120
    assert g.wait_s("d", 1) == pytest.approx(480)
    t[0] += 600
    assert g.wait_s("d", 1) == 0
    t[0] -= 5000                                  # Uhr zurückgestellt -> gesperrt
    assert g.wait_s("d", 1) == 600


def test_rest(tmp_path):
    svc = make_svc(tmp_path)
    c = TestClient(create_app(svc))
    ov = c.get("/api/devices/can-01/params").json()
    assert ov["write_enabled"] and ov["params"][0]["can_write"]
    assert c.get("/api/devices/gibtsnicht/params").status_code == 404
    url = "/api/devices/can-01/params/0x0141"
    assert c.post(url, json={"value": 66}).status_code == 403                       # ohne CSRF
    assert c.post(url, headers=CSRF, json={"value": "66"}).status_code == 400      # keine Zahl
    r = c.post(url, headers=CSRF, json={"value": 95})
    assert r.status_code == 409 and "Maximum" in r.json()["detail"]
    r = c.post(url, headers=CSRF, json={"value": 66})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["readback"] == 66
    r = c.post(url, headers=CSRF, json={"value": 67})
    assert r.status_code == 409 and "Ratenlimit" in r.json()["detail"]
    assert c.get("/api/writes").json()["writes"][0]["source"] == "Web"
    assert svc.fake.writes == [(0x0141, 66.0)]


def test_rest_keine_bestaetigung(tmp_path):
    svc = make_svc(tmp_path)
    svc.fake.write_timeout = True
    c = TestClient(create_app(svc))
    r = c.post("/api/devices/can-01/params/0x0141", headers=CSRF, json={"value": 66})
    assert r.status_code == 502 and "am Regler prüfen" in r.json()["detail"]
    assert svc.store.device("can-01").write_alarm
