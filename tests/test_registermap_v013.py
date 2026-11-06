"""0.1.3: exakte 32-Bit-Ganzzahlen, Nr-Spalte, Ersatznamen, Fehlercode-Rücksetzung."""

from __future__ import annotations

import csv
import io

from gateway.config import GatewayConfig, VarTyp
from gateway.devices.names import display_name, fallback_name, is_placeholder
from gateway.registermap import RegisterMap, decode_float, encode_int32, export_csv
from gateway.safety import OUT_SECTOH
from gateway.store import DeviceError, ValueStore

SERIAL = 294_934_309          # echte SN von Gerät 0x02 am Test-PC (Float32: 294 934 304)


def make_cfg() -> GatewayConfig:
    return GatewayConfig.model_validate({
        "modbus": {"diag_unit_id": 247},
        "safety": {"write_enabled": True, "daily_write_budget": 50},
        "devices": [
            {"id": "can-02", "bus": "can", "address": 0x02, "unit_id": 2, "name": "LC2",
             "values": [
                 {"nr": 0x0103, "reg": 0, "name": "Kesseltemp.", "unit": "°C", "decimals": 1},
                 {"nr": 0xF001, "reg": 2, "name": "Ser.-Nr.", "vartyp": int(VarTyp.LONG)},
                 {"nr": 0x060D, "reg": 4, "name": "0x060d", "unit": "mV"},            # alter Ersatzname
                 {"nr": 0x703F, "reg": 6, "name": "", "unit": "%"},                   # leer
                 {"nr": 0x0200, "reg": 8, "name": "Offset", "vartyp": int(VarTyp.SLONG), "decimals": 1},
                 {"nr": 0x0014, "reg": 10, "name": "Kessel AUS", "unit": "h",
                  "vartyp": int(VarTyp.LONG), "out_type": OUT_SECTOH},               # Sekunden -> h
             ],
             "params": [
                 {"nr": 0x0042, "reg": 0, "name": "Kessel Soll", "unit": "°C", "writable": True,
                  "min": 60, "max": 90},
                 {"nr": 0x0300, "reg": 2, "name": "Zähler", "vartyp": int(VarTyp.LONG)},
             ]},
            {"id": "lc1-com1", "bus": "lc1", "port": "COM1", "unit_id": 101,
             "values": [{"k": 7, "reg": 0, "name": "Kesseltemp."}],
             "params": [{"e": 44, "reg": 0, "name": "Kessel Soll"}]},
        ],
    })


def rows():
    text = export_csv(make_cfg()).lstrip("﻿")
    out = list(csv.reader(io.StringIO(text), delimiter=";"))
    return out[0], {(r[0], r[1], r[2]): r for r in out[1:]}


# --- exakte 32-Bit-Ganzzahl --------------------------------------------------------------------
def test_seriennummer_exakt_auf_3000_plus_reg():
    store = ValueStore()
    store.set_value("can-02", "value", 0xF001, float(SERIAL))
    rm = RegisterMap(make_cfg(), store)
    f32 = decode_float(rm.read(2, "ir", 2, 2))
    assert f32 != SERIAL and abs(f32 - SERIAL) <= 16          # Float32 rundet
    hi, lo = rm.read(2, "ir", 3002, 2)
    assert (hi << 16) | lo == SERIAL                          # UInt32 exakt


def test_int32_vorzeichen_skalierung_und_kein_wert():
    store = ValueStore()
    store.set_value("can-02", "value", 0x0200, -12.5)         # SLONG, 1 Nachkommastelle
    rm = RegisterMap(make_cfg(), store)
    hi, lo = rm.read(2, "ir", 3008, 2)
    raw = (hi << 16) | lo
    assert raw - (1 << 32) == -125
    assert rm.read(2, "ir", 3002, 2) == [0xFFFF, 0xFFFF]      # LONG ohne Wert
    assert encode_int32(None, 0, True) == [0x8000, 0x0000]
    assert encode_int32(5.0, 0, False, "CDAB") == [5, 0]


def test_nur_long_typen_ohne_zeitumrechnung():
    rm = RegisterMap(make_cfg(), ValueStore())
    u32 = {(s.table, s.address) for s in rm.slots() if s.kind == "u32"}
    assert u32 == {("ir", 3002), ("ir", 3008), ("hr", 3002)}  # nicht: FLOAT, Kessel AUS (s->h)


def test_u32_im_holding_bereich_nicht_schreibbar():
    rm = RegisterMap(make_cfg(), ValueStore())
    assert rm.param_at(2, 3002) is None
    assert rm.param_at(2, 0) is not None


def test_csv_hinweis_und_typ():
    _, body = rows()
    sn = body[("2", "2", "04")]
    assert "exakt auf Register 3002" in sn[9]
    exakt = body[("2", "3002", "04")]
    assert exakt[5] == "Ser.-Nr. exakt" and exakt[7].startswith("UInt32")
    assert body[("2", "3008", "04")][7].startswith("Int32") and "x10^1" in body[("2", "3008", "04")][6]
    assert ("2", "3010", "04") not in body


# --- Nr-Spalte und Ersatznamen -----------------------------------------------------------------
def test_nr_spalte():
    header, body = rows()
    assert header[4] == "Nr"
    assert body[("2", "0", "04")][4] == "0x0103"
    assert body[("101", "0", "04")][4] == "K7"
    assert body[("101", "0", "03")][4] == "E44"
    assert body[("247", "0", "04")][4] == ""


def test_ersatznamen_aus_firmware_tabelle():
    _, body = rows()
    assert body[("2", "4", "04")][5] == "M_O2OFFSET (0x060D)"
    assert body[("2", "6", "04")][5] == "M_HKFREIGHOLZ (0x703F)"
    assert body[("2", "1003", "04")][5] == "M_HKFREIGHOLZ (0x703F) Status"
    assert body[("2", "0", "04")][5] == "Kesseltemp."          # echter Name bleibt


def test_names_helfer():
    assert is_placeholder("") and is_placeholder("  ") and is_placeholder("0x060d")
    assert not is_placeholder("Sonde")
    assert fallback_name(0xFFFE) == "0xFFFE"                   # unbekannte Nummer
    assert display_name("Sonde", 0x060D) == "Sonde"
    assert display_name("", None) == ""


# --- Fehlercode --------------------------------------------------------------------------------
def test_fehlercode_geht_bei_erfolg_zurueck():
    st = ValueStore()
    st.set_error("d", DeviceError.INTERFACE, "Access denied")
    assert st.device("d").last_error == DeviceError.INTERFACE
    st.set_online("d")
    d = st.device("d")
    assert d.online and d.last_error == DeviceError.NONE and d.last_error_text == ""


def test_schreibfehler_bleibt_stehen():
    st = ValueStore()
    st.set_error("d", DeviceError.WRITE_MISMATCH, "Soll 65, Ist 70", offline=False)
    st.set_online("d")
    d = st.device("d")
    assert d.last_error == DeviceError.WRITE_MISMATCH and d.write_alarm
