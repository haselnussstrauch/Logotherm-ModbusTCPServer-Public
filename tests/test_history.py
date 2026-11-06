"""Aufzeichnung: Tagesdateien, Aufbewahrung, Abfrage mit Verdichtung, CSV, REST."""

from __future__ import annotations

import datetime as dt

import pytest

from gateway.config import GatewayConfig
from gateway.history import History, SeriesMeta, check_day
from gateway.service import Service


def ts(day: str, hh: int = 12, mm: int = 0, ss: int = 0) -> float:
    return dt.datetime.combine(dt.date.fromisoformat(day), dt.time(hh, mm, ss)).timestamp()


META = {("can-02", "value", 0x0103): SeriesMeta("can-02", "value", 0x0103, "Kesseltemp.", "°C", "0x0103", "LC2")}


def fill(h: History, day: str, n: int = 60, step_s: int = 60, start_h: int = 8) -> None:
    t0 = ts(day, start_h)
    h.record([(t0 + i * step_s, "can-02", "value", 0x0103, 20.0 + i) for i in range(n)], META)


# --- Dateien und Aufbewahrung -------------------------------------------------------------------
def test_eine_datei_je_tag_und_loeschen(tmp_path):
    h = History(tmp_path)
    fill(h, "2026-11-04")
    fill(h, "2026-11-05")
    assert [d["date"] for d in h.days()] == ["2026-11-05", "2026-11-04"]
    assert all(d["size"] > 0 for d in h.days())
    assert h.delete_day("2026-11-05")                 # auch der gerade geöffnete Tag
    assert [d["date"] for d in h.days()] == ["2026-11-04"]
    assert not h.delete_day("2026-11-05")
    fill(h, "2026-11-05")                             # nach dem Löschen geht es weiter
    assert len(h.days()) == 2
    h.close()


def test_aufbewahrung_14_tage(tmp_path):
    h = History(tmp_path)
    today = dt.date(2026, 11, 20)
    for back in range(20):
        fill(h, (today - dt.timedelta(days=back)).isoformat(), n=2)
    deleted = h.cleanup(14, today=today)
    days = [d["date"] for d in h.days()]
    assert len(days) == 14 and days[0] == "2026-11-20" and days[-1] == "2026-11-07"
    assert sorted(deleted) == [(today - dt.timedelta(days=b)).isoformat() for b in range(19, 13, -1)]
    h.close()


def test_datum_wird_geprueft(tmp_path):
    h = History(tmp_path)
    for bad in ("../x", "2026-13-01", "20261105", ""):
        with pytest.raises(ValueError):
            h.delete_day(bad)
    assert check_day("2026-11-05") == "2026-11-05"


# --- Abfrage ------------------------------------------------------------------------------------
def test_abfrage_ohne_und_mit_verdichtung(tmp_path):
    h = History(tmp_path)
    fill(h, "2026-11-05", n=600, step_s=10)          # 100 min, alle 10 s
    t_from, t_to = ts("2026-11-05", 8), ts("2026-11-05", 10)
    full = h.query("can-02", "value", [0x0103], t_from, t_to, max_points=5000)["series"][0x0103]
    assert len(full) == 600 and full[0][1] == 20.0
    red = h.query("can-02", "value", [0x0103], t_from, t_to, max_points=60)
    pts = red["series"][0x0103]
    assert 45 <= len(pts) <= 61 and red["bucket_s"] == pytest.approx(120)
    t, avg, lo, hi = pts[0]
    assert lo <= avg <= hi and hi - lo > 0             # Min/Max des Abschnitts
    assert h.query("can-02", "value", [0x9999], t_from, t_to)["series"][0x9999] == []
    h.close()


def test_abfrage_ueber_mitternacht(tmp_path):
    h = History(tmp_path)
    h.record([(ts("2026-11-04", 23, 59), "can-02", "value", 0x0103, 1.0),
              (ts("2026-11-05", 0, 1), "can-02", "value", 0x0103, 2.0)], META)
    assert {d["date"] for d in h.days()} == {"2026-11-04", "2026-11-05"}
    pts = h.query("can-02", "value", [0x0103], ts("2026-11-04", 23), ts("2026-11-05", 1), 5000)["series"][0x0103]
    assert [p[1] for p in pts] == [1.0, 2.0]
    h.close()


def test_katalog_und_csv(tmp_path):
    h = History(tmp_path)
    h.record([(ts("2026-11-05", 9), "can-02", "value", 0x0103, 24.87),
              (ts("2026-11-05", 9), "can-02", "value", 0xF001, 294934309.0)],
             {**META, ("can-02", "value", 0xF001): SeriesMeta("can-02", "value", 0xF001, "Ser.-Nr.", "", "0xF001", "LC2")})
    cat = h.catalog(ts("2026-11-05", 0), ts("2026-11-06", 0))
    assert [(m.key, m.name) for m in cat] == [(0x0103, "Kesseltemp."), (0xF001, "Ser.-Nr.")]
    text = "".join(h.export_csv("2026-11-05"))
    lines = text.lstrip("﻿").splitlines()
    assert lines[0] == "Zeit;Gerät;Gerätename;Art;Nr;Name;Einheit;Wert"
    assert "2026-11-05 09:00:00;can-02;LC2;Messwert;0x0103;Kesseltemp.;°C;24,87" in lines
    assert any(line.endswith(";294934309") for line in lines)      # keine Rundung
    h.close()


# --- Dienst: Aufzeichnung aus dem Wertespeicher ---------------------------------------------
def make_service(tmp_path) -> Service:
    cfg = GatewayConfig.model_validate({
        "service": {"data_dir": str(tmp_path)},
        "devices": [{"id": "can-02", "bus": "can", "address": 2, "unit_id": 2, "name": "LC2",
                     "values": [{"nr": 0x0103, "reg": 0, "name": "Kesseltemp.", "unit": "°C"},
                                {"nr": 0x060D, "reg": 2, "name": "", "unit": "mV"},
                                {"nr": 0x0104, "reg": 4, "name": "Aus", "active": False}]}],
    })
    return Service(cfg, tmp_path / "gateway.yaml")


def test_dienst_zeichnet_nur_frische_gueltige_werte_auf(tmp_path):
    svc = make_service(tmp_path)
    svc.store.set_value("can-02", "value", 0x0103, 24.5)
    svc.store.set_value("can-02", "value", 0x0104, 1.0)          # inaktiv -> nicht
    svc.store.set_timeout("can-02", "value", 0x060D)             # kein Wert -> nicht
    last: dict = {}
    assert svc._record_once(last) == 1
    assert svc._record_once(last) == 0                           # unverändert seit letztem Mal
    svc.store.set_value("can-02", "value", 0x0103, 25.0)
    svc.store.set_value("can-02", "value", 0x060D, -12.0)
    assert svc._record_once(last) == 2
    cat = svc.history.catalog(0, 4e9)
    assert {m.name for m in cat} == {"Kesseltemp.", "M_O2OFFSET (0x060D)"}
    st = svc.history_status()
    assert st["retention_days"] == 14 and st["interval_s"] == 60 and len(st["days"]) == 1
    svc.history.close()


def test_kein_speicherplatz_haelt_an(tmp_path, monkeypatch):
    svc = make_service(tmp_path)
    svc.store.set_value("can-02", "value", 0x0103, 24.5)
    monkeypatch.setattr(svc.history, "disk_free", lambda: 50 * 1024 * 1024)
    assert svc._record_once({}) == 0
    assert any("angehalten" in w for w in svc.warnings())


# --- REST -------------------------------------------------------------------------------------
def test_rest(tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from gateway.web.app import create_app

    svc = make_service(tmp_path)
    svc.store.set_value("can-02", "value", 0x0103, 24.5)
    svc._record_once({})
    today = dt.date.today().isoformat()
    c = TestClient(create_app(svc))
    st = c.get("/api/history").json()
    assert st["days"][0]["date"] == today and st["enabled"]
    now = dt.datetime.now().timestamp()
    cat = c.get(f"/api/history/catalog?from={now - 3600}&to={now + 60}").json()["series"]
    assert cat[0]["name"] == "Kesseltemp." and cat[0]["unit"] == "°C"
    r = c.get(f"/api/history/series?device=can-02&keys=0x0103&from={now - 3600}&to={now + 60}").json()
    assert r["series"][0]["key"] == 0x0103 and r["series"][0]["points"][0][1] == 24.5
    assert r["interval_s"] == 60
    csv_r = c.get(f"/api/history/days/{today}.csv")
    assert csv_r.status_code == 200 and "Kesseltemp." in csv_r.text
    assert c.get("/api/history/days/2000-01-01.csv").status_code == 404
    assert c.delete(f"/api/history/days/{today}").status_code == 403          # ohne CSRF-Header
    assert c.delete(f"/api/history/days/{today}", headers={"X-Requested-With": "gateway"}).json()["ok"]
    assert c.get("/api/history").json()["days"] == []
    assert c.delete("/api/history/days/..%2Fx", headers={"X-Requested-With": "gateway"}).status_code in (400, 404)
    r = c.patch("/api/draft/settings/history", headers={"X-Requested-With": "gateway"},
                json={"retention_days": 30, "interval_s": 30})
    assert r.status_code == 200 and svc.draft.history.retention_days == 30
    assert c.patch("/api/draft/settings/history", headers={"X-Requested-With": "gateway"},
                   json={"retention_days": 0}).status_code == 400
    svc.history.close()
