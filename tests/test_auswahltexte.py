"""Auswahltexte (OUT_TEXTAUSWAHL, JA/NEIN, EIN/AUS): Protokoll, Konfiguration, Anzeige."""

from __future__ import annotations

import csv
import io

import pytest

from gateway.can_lc2 import messages
from gateway.config import load_config, save_config
from gateway.devices.names import fixed_texts, texts_hint, value_text
from gateway.drivers import ValueInfo
from gateway.registermap import export_csv
from gateway.service import Service
from test_service import FakeDriver, make_cfg

BA = {0: "Aus", 1: "Automatik", 2: "Heizen", 3: "Absenken", 4: "Frostschutz", 5: "Party"}


# --- Protokoll ---------------------------------------------------------------------------------
def test_antwort_parse_wie_firmware():
    # Kopf: Len, Messwert, Sprache, Offset, Anzahl; Texte nullterminiert; 1 Füllbyte; CRC
    blocks = messages.build_auswahltexte(0x9041, 0, list(BA.values()))
    got = {}
    for b in blocks:
        a = messages.AuswahltexteAntwort.parse(b)
        assert a.nr == 0x9041 and len(a.texte) <= 10
        got.update({a.offset + j: t for j, t in enumerate(a.texte)})
    assert got == BA


def test_antwort_mit_min_offset_und_sonderzeichen():
    blocks = messages.build_auswahltexte(0x0001, 2, ["O₂ hoch", "λ ok"])
    a = messages.AuswahltexteAntwort.parse(blocks[0])
    assert a.offset == 2 and a.texte == ("O₂ hoch", "λ ok")


def test_antwort_zu_kurz():
    with pytest.raises(ValueError):
        messages.AuswahltexteAntwort.parse(b"\x05\x00\x00")


# --- Hilfsfunktionen -----------------------------------------------------------------------------
def test_feste_texte_und_wert_text():
    assert fixed_texts(1) == {0: "AUS", 1: "EIN"} and fixed_texts(2) == {0: "NEIN", 1: "JA"}
    assert fixed_texts(0) is None and fixed_texts(None) is None
    assert value_text(BA, 2.0) == "Heizen" and value_text(BA, 2.5) is None and value_text(BA, 9) is None
    assert value_text(None, 1) is None
    assert texts_hint({1: "EIN", 0: "AUS"}) == "0=AUS; 1=EIN"


# --- Konfiguration und Anzeige -------------------------------------------------------------------
def make_svc(tmp_path) -> Service:
    s = Service(make_cfg(), tmp_path / "gateway.yaml")
    s._drivers["can"] = FakeDriver()
    return s


def test_uebernahme_speichern_laden(tmp_path):
    svc = make_svc(tmp_path)
    dev = svc.draft.device("can-01")
    svc._merge_enum(dev, [ValueInfo(kind="param", key=0x0141, name="Betriebsart", vartyp=0, min=0, max=5,
                                    out_textauswahl=True, texts=BA)])
    p = next(x for x in dev.params if x.key == 0x0141)
    assert p.texts == BA
    save_config(svc.draft, tmp_path / "g.yaml")
    back = load_config(tmp_path / "g.yaml")
    assert next(x for x in back.devices[0].params if x.key == 0x0141).texts == BA
    assert back.devices[0].values[0].texts is None                 # ohne Auswahl: nichts gespeichert
    assert "texts:" not in (tmp_path / "g.yaml").read_text(encoding="utf-8").split("params:")[0]


def test_anzeige_dashboard_parameter_registerliste(tmp_path):
    svc = make_svc(tmp_path)
    dev = svc.cfg.devices[0]
    dev.values[0].texts = BA
    dev.values[0].dashboard = True
    p = dev.params[0]
    p.texts, p.min, p.max, p.dashboard = BA, 0, 5, True
    svc.store.set_value("can-01", "value", 0x0003, 3.0)
    svc.store.set_value("can-01", "param", 0x0141, 1.0)
    d = svc.dashboard()["groups"][0]
    assert d["values"][0]["text"] == "Absenken" and d["values"][0]["value"] == 3.0
    assert d["params"][0]["text"] == "Automatik" and d["params"][0]["texts"] == BA
    assert svc.param_overview("can-01")["params"][0]["text"] == "Automatik"
    assert svc.device_values("can-01")[0]["text"] == "Absenken"
    rows = list(csv.reader(io.StringIO(export_csv(svc.cfg).lstrip("﻿")), delimiter=";"))
    v = next(r for r in rows if r[:3] == ["1", "0", "04"])
    assert "Auswahl: 0=Aus; 1=Automatik; 2=Heizen" in v[9]


async def test_modbus_bleibt_index_und_schreiben_per_index(tmp_path):
    svc = make_svc(tmp_path)
    p = svc.cfg.devices[0].params[0]
    p.texts, p.min, p.max = BA, 0, 5
    svc.store.set_value("can-01", "param", 0x0141, 1.0)
    entry = await svc.write_param_web("can-01", 0x0141, 4)          # „Frostschutz“
    assert entry["ok"] and entry["readback"] == 4
    from gateway.registermap import RegisterMap, decode_float
    rm = RegisterMap(svc.cfg, svc.store)
    assert decode_float(rm.read(1, "hr", 0, 2)) == 4.0              # Modbus: Index, kein Text


def test_rest_werteliste_und_verlauf_katalog(tmp_path):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from gateway.web.app import create_app
    svc = make_svc(tmp_path)
    svc.cfg.service.data_dir = str(tmp_path)
    svc.draft = svc.cfg.model_copy(deep=True)
    svc.cfg.devices[0].values[0].texts = BA
    svc.draft.devices[0].values[0].texts = BA
    svc.store.set_value("can-01", "value", 0x0003, 2.0)
    from gateway.history import History
    svc.history = History(tmp_path)
    svc._record_once({})
    c = TestClient(create_app(svc))
    rows = c.get("/api/draft/devices/can-01/values").json()["rows"]
    assert next(r for r in rows if r["key"] == 0x0003)["texts"] == {str(k): v for k, v in BA.items()}
    cat = c.get("/api/history/catalog?from=0&to=4000000000").json()["series"]
    assert cat[0]["texts"]["2"] == "Heizen"
    svc.history.close()
