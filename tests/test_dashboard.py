"""Dashboard (Startseite), Auswahl über die Werteliste, `gateway web-access`."""

from __future__ import annotations

import pytest

from gateway.config import GatewayConfig, load_config, save_config
from gateway.service import Service
from test_service import FakeDriver, make_cfg

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from gateway.web.app import create_app  # noqa: E402

CSRF = {"X-Requested-With": "gateway"}


def make_svc(tmp_path) -> Service:
    cfg = make_cfg()
    dev = cfg.devices[0]
    dev.values[0].dashboard = True
    dev.params[0].dashboard = True
    s = Service(cfg, tmp_path / "gateway.yaml")
    s._drivers["can"] = FakeDriver()
    return s


def test_dashboard_inhalt(tmp_path):
    svc = make_svc(tmp_path)
    d = svc.dashboard()
    assert d["total"] == 1 and d["online"] == 0 and d["last_update"] is None
    assert d["groups"][0]["values"][0]["value"] is None          # noch nicht abgefragt
    svc.store.set_value("can-01", "value", 0x0003, 61.5)
    svc.store.set_value("can-01", "param", 0x0141, 60.0)
    svc.store.set_online("can-01")
    d = svc.dashboard()
    assert d["online"] == 1 and d["last_update"] and d["last_update_age_s"] < 5
    g = d["groups"][0]
    assert [v["key"] for v in g["values"]] == [0x0003] and g["values"][0]["value"] == 61.5
    assert g["values"][0]["status"] == 0 and g["values"][0]["nr"] == "0x0003"
    p = g["params"][0]
    assert p["key"] == 0x0141 and p["value"] == 60.0 and p["can_write"]


def test_dashboard_nur_markierte_und_aktive(tmp_path):
    svc = make_svc(tmp_path)
    svc.cfg.devices[0].values[0].dashboard = False
    svc.cfg.devices[0].params[0].dashboard = False
    assert svc.dashboard()["groups"] == []
    svc.cfg.devices[0].values[1].dashboard = True               # inaktiver Wert -> nicht
    assert svc.dashboard()["groups"] == []


def test_werteliste_dashboard_haken(tmp_path):
    svc = make_svc(tmp_path)
    svc.update_draft_device("can-01", entries=[
        {"kind": "value", "key": 0x0003, "active": True, "dashboard": False},
        {"kind": "value", "key": 0x0002, "active": False, "dashboard": True},   # inaktiv -> kein Dashboard
        {"kind": "param", "key": 0x0141, "active": True, "dashboard": True, "writable": True}])
    dev = svc.draft.device("can-01")
    assert not dev.values[0].dashboard and not dev.values[1].dashboard and dev.params[0].dashboard


def test_dashboard_flag_wird_gespeichert(tmp_path):
    cfg = make_cfg()
    cfg.devices[0].values[0].dashboard = True
    save_config(cfg, tmp_path / "g.yaml")
    back = load_config(tmp_path / "g.yaml")
    assert back.devices[0].values[0].dashboard and not back.devices[0].params[0].dashboard
    assert GatewayConfig.model_validate({"devices": []}).history.retention_days == 14


def test_rest_dashboard_und_werteliste(tmp_path):
    svc = make_svc(tmp_path)
    c = TestClient(create_app(svc))
    d = c.get("/api/dashboard").json()
    assert d["running"] and d["groups"][0]["id"] == "can-01"
    rows = c.get("/api/draft/devices/can-01/values").json()["rows"]
    assert {(r["kind"], r["key"]): r["dashboard"] for r in rows}[("value", 0x0003)] is True
    r = c.patch("/api/draft/devices/can-01", headers=CSRF,
                json={"entries": [{"kind": "value", "key": 0x0003, "active": True, "dashboard": False}]})
    assert r.status_code == 200 and not svc.draft.device("can-01").values[0].dashboard
    for f in ("dashboard.js", "params.js", "history.js"):
        assert c.get(f"/static/{f}").status_code == 200
    assert "dashboard.js" in c.get("/").text


def test_cli_web_access(tmp_path, capsys):
    from gateway.main import cli
    path = tmp_path / "gateway.yaml"
    save_config(make_cfg(), path)
    assert cli(["-c", str(path), "web-access", "--network"]) == 0
    assert load_config(path).web.bind == "0.0.0.0"
    assert load_config(path).devices[0].params[0].writable        # Rest unverändert
    assert cli(["-c", str(path), "web-access", "--network"]) == 0  # zweimal harmlos
    assert cli(["-c", str(path), "web-access", "--local"]) == 0
    assert load_config(path).web.bind == "127.0.0.1"


def test_warnung_ohne_passwort_im_netz(tmp_path):
    svc = make_svc(tmp_path)
    svc.cfg.web.bind = "0.0.0.0"
    assert any("ohne Passwort" in w for w in svc.warnings())
    svc.set_password("geheim123")
    assert not any("ohne Passwort" in w for w in svc.warnings())


def test_modbus_aus_im_dashboard_ohne_allowlist_warnung(tmp_path):
    svc = make_svc(tmp_path)
    svc.cfg.modbus.allow = []
    assert any("Allowlist" in w for w in svc.warnings())
    assert svc.dashboard()["modbus"]["enabled"] is True
    svc.cfg.modbus.enabled = False
    d = svc.dashboard()
    assert d["modbus"] == {"enabled": False, "running": False, "bind": svc.cfg.modbus.bind,
                           "port": svc.cfg.modbus.port}
    assert not any("Allowlist" in w for w in d["warnings"])
    assert svc.status()["modbus"]["enabled"] is False
