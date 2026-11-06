"""Tests für gateway.registermap (Belegung, Kodierung, Status, Diagnose, CSV)."""

from __future__ import annotations

import csv
import io
import math
import struct
import sys
import types

import pytest


from gateway.config import GatewayConfig
from gateway.registermap import (
    IllegalAddress,
    RegisterMap,
    UnknownUnit,
    decode_float,
    encode_float,
    encode_int16,
    export_csv,
)
from gateway.store import DeviceError, ValueStore


def validate_cfg(raw: dict):
    """GatewayConfig validieren; fehlt gateway.safety noch, wird nur währenddessen ein Stub eingehängt
    (kein Einfluss auf andere Tests, z. B. importorskip)."""
    from importlib.util import find_spec
    if find_spec("gateway.safety") is not None:
        return GatewayConfig.model_validate(raw)
    stub = types.ModuleType("gateway.safety")
    stub.param_lock_reason = lambda *a, **k: None
    sys.modules["gateway.safety"] = stub
    try:
        return GatewayConfig.model_validate(raw)
    finally:
        sys.modules.pop("gateway.safety", None)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make_cfg(modbus: dict | None = None, writable: bool = True) -> GatewayConfig:
    raw = {
        "modbus": {"diag_unit_id": 247, **(modbus or {})},
        "safety": {"write_enabled": True, "daily_write_budget": 50},
        "devices": [{
            "id": "hk1", "bus": "can", "address": 0x10, "unit_id": 16, "name": "Heizkreis",
            "values": [
                {"nr": 1, "reg": 0, "name": "Vorlauf", "unit": "°C", "decimals": 1},
                {"nr": 2, "reg": 2, "name": "Aus", "active": False},
                {"nr": 3, "reg": 4, "name": "Rücklauf", "unit": "°C", "decimals": 1},
            ],
            "params": [
                {"nr": 0x100, "reg": 0, "name": "Soll", "unit": "°C", "decimals": 1,
                 "min": 10, "max": 30},
                {"nr": 0x101, "reg": 2, "name": "Fest", "decimals": 0},
            ],
        }],
    }
    cfg = validate_cfg(raw)
    # writable erst nach der Validierung setzen (validate_assignment prüft nur das Feld)
    cfg.devices[0].params[0].writable = writable
    return cfg


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock: Clock) -> ValueStore:
    s = ValueStore(stale_after_s=60.0, clock=clock)
    s.diag.started = clock()
    return s


# --- Kodierung --------------------------------------------------------------------------------
def test_float_encoding_word_orders():
    assert encode_float(1.5, "ABCD") == [0x3FC0, 0x0000]
    assert encode_float(1.5, "CDAB") == [0x0000, 0x3FC0]
    assert struct.pack(">f", 1.1).hex() == "3f8ccccd"
    assert encode_float(1.1, "ABCD") == [0x3F8C, 0xCCCD]
    assert encode_float(1.1, "CDAB") == [0xCCCD, 0x3F8C]
    assert decode_float([0xCCCD, 0x3F8C], "CDAB") == pytest.approx(1.1)
    assert decode_float(encode_float(-12.25, "ABCD"), "ABCD") == -12.25


def test_nan_for_none():
    assert encode_float(None) == [0x7FC0, 0x0000]
    assert encode_float(float("nan"), "CDAB") == [0x0000, 0x7FC0]
    assert encode_float(1e300) == [0x7FC0, 0x0000]    # nicht als Float32 darstellbar


def test_int16_scaling_and_limits():
    assert encode_int16(21.46, 1) == 215
    assert encode_int16(-5.0, 1) == (-50) & 0xFFFF
    assert encode_int16(1e9, 2) == 32767
    assert encode_int16(-1e9, 2) == (-32767) & 0xFFFF
    assert encode_int16(None, 1) == 0x8000
    assert encode_int16(float("nan"), 1) == 0x8000


# --- Belegung ---------------------------------------------------------------------------------
def test_layout_and_gaps(store: ValueStore):
    rm = RegisterMap(make_cfg(), store)
    store.set_value("hk1", "value", 1, 1.5)
    store.set_value("hk1", "value", 3, 20.25)
    assert rm.read(16, "ir", 0, 2) == [0x3FC0, 0]
    assert decode_float(rm.read(16, "ir", 4, 2)) == 20.25
    # Lücke durch inaktiven Wert (reg 2/3) und Bereich über die Lücke
    for addr, cnt in ((2, 2), (0, 6), (2, 1)):
        with pytest.raises(IllegalAddress):
            rm.read(16, "ir", addr, cnt)
    # Statusregister: 1000 + reg//2; reg 2 (inaktiv) -> 1001 nicht belegt
    assert rm.read(16, "ir", 1000, 1) == [0]
    assert rm.read(16, "ir", 1002, 1) == [0]
    with pytest.raises(IllegalAddress):
        rm.read(16, "ir", 1001, 1)
    # Int16-Spiegel aus -> nicht belegt
    with pytest.raises(IllegalAddress):
        rm.read(16, "ir", 2000, 1)
    # unbelegte Adresse weit außerhalb, negative/zu große Bereiche
    with pytest.raises(IllegalAddress):
        rm.read(16, "ir", 500, 2)


def test_half_pair_at_border_is_illegal(store: ValueStore):
    rm = RegisterMap(make_cfg(), store)
    store.set_value("hk1", "value", 1, 1.5)
    with pytest.raises(IllegalAddress):
        rm.read(16, "ir", 1, 1)          # beginnt in der Mitte des Paares
    with pytest.raises(IllegalAddress):
        rm.read(16, "ir", 0, 1)          # endet in der Mitte des Paares
    with pytest.raises(IllegalAddress):
        rm.read(16, "ir", 1, 2)


def test_unknown_unit_and_missing_tables(store: ValueStore):
    rm = RegisterMap(make_cfg(), store)
    with pytest.raises(UnknownUnit):
        rm.read(99, "ir", 0, 2)
    with pytest.raises(IllegalAddress):
        rm.read(247, "hr", 0, 1)         # Diagnose-Unit hat keine Holding-Register
    assert rm.has_unit(16) and rm.has_unit(247) and not rm.has_unit(1)


@pytest.mark.parametrize("order,expected", [("ABCD", [0x3F8C, 0xCCCD]), ("CDAB", [0xCCCD, 0x3F8C])])
def test_word_order_from_config(store: ValueStore, order, expected):
    rm = RegisterMap(make_cfg({"word_order": order}), store)
    store.set_value("hk1", "value", 1, struct.unpack(">f", struct.pack(">f", 1.1))[0])
    assert rm.read(16, "ir", 0, 2) == expected


def test_nan_when_no_value(store: ValueStore):
    rm = RegisterMap(make_cfg(), store)
    assert rm.read(16, "ir", 0, 2) == [0x7FC0, 0x0000]
    assert math.isnan(decode_float(rm.read(16, "ir", 4, 2)))
    assert rm.read(16, "ir", 1000, 1) == [1]            # noch nie gelesen = Timeout


def test_int16_mirror(store: ValueStore):
    rm = RegisterMap(make_cfg({"int16_mirror": True}), store)
    store.set_value("hk1", "value", 1, 21.46)       # decimals=1 -> 215
    store.set_value("hk1", "value", 3, -5.0)        # -> -50
    assert rm.read(16, "ir", 2000, 1) == [215]
    assert rm.read(16, "ir", 2002, 1) == [(-50) & 0xFFFF]
    assert rm.read(16, "ir", 2000, 1)[0] == 215
    store.set_value("hk1", "value", 1, 99999.0)     # Begrenzung
    assert rm.read(16, "ir", 2000, 1) == [32767]
    store.set_timeout("hk1", "value", 3)            # Wert bleibt, Int16 weiter gültig
    assert rm.read(16, "ir", 2002, 1) == [(-50) & 0xFFFF]
    with pytest.raises(IllegalAddress):
        rm.read(16, "ir", 2001, 1)                  # inaktiv
    # kein Wert -> 0x8000
    rm2 = RegisterMap(make_cfg({"int16_mirror": True}), ValueStore())
    assert rm2.read(16, "ir", 2000, 1) == [0x8000]
    # auch Holding-Spiegel
    store.set_value("hk1", "param", 0x100, 21.5)
    assert rm.read(16, "hr", 2000, 1) == [215]
    assert rm.read(16, "hr", 1000, 1) == [0]


def test_status_timeout_and_stale(store: ValueStore, clock: Clock):
    rm = RegisterMap(make_cfg(), store)
    store.set_value("hk1", "value", 1, 1.0)
    assert rm.read(16, "ir", 1000, 1) == [0]
    clock.t += 30
    assert rm.read(16, "ir", 1000, 1) == [0]
    clock.t += 31
    assert rm.read(16, "ir", 1000, 1) == [2]            # veraltet
    store.set_timeout("hk1", "value", 1)
    assert rm.read(16, "ir", 1000, 1) == [1]            # Timeout
    # Wert bleibt erhalten
    assert decode_float(rm.read(16, "ir", 0, 2)) == 1.0


def test_holding_params(store: ValueStore):
    rm = RegisterMap(make_cfg(), store)
    store.set_value("hk1", "param", 0x100, 21.5)
    assert decode_float(rm.read(16, "hr", 0, 2)) == 21.5
    assert rm.read(16, "hr", 1000, 1) == [0]
    assert rm.read(16, "hr", 1001, 1) == [1]            # zweiter Parameter noch ohne Wert
    hit = rm.param_at(16, 0)
    assert hit is not None and hit[1].key == 0x100
    assert rm.param_at(16, 1) is None                   # Paarmitte
    assert rm.param_at(16, 1000) is None                # Statusregister
    assert rm.param_at(99, 0) is None


def test_device_status_block(store: ValueStore, clock: Clock):
    rm = RegisterMap(make_cfg(), store)
    assert rm.read(16, "ir", 9000, 4) == [0, 0, 0, 0xFFFF]
    store.set_online("hk1")
    clock.t += 7
    store.set_error("hk1", DeviceError.WRITE_MISMATCH, "x", offline=False)
    assert rm.read(16, "ir", 9000, 4) == [1, int(DeviceError.WRITE_MISMATCH), 1, 7]
    store.set_error("hk1", DeviceError.TIMEOUT)
    assert rm.read(16, "ir", 9000, 2) == [0, 1]
    clock.t += 100000
    store.set_online("hk1")
    clock.t += 100000
    assert rm.read(16, "ir", 9003, 1) == [0xFFFF]       # max 65535


def test_diag_unit(store: ValueStore, clock: Clock):
    rm = RegisterMap(make_cfg(), store)
    store.diag.heartbeat = 42
    store.diag.cycle_time_ms = 1234
    store.diag.can_error_count = 0x00012345
    store.diag.writes_today = 3
    clock.t += 90061
    store.set_online("hk1")
    assert rm.read(247, "ir", 0, 9) == [42, 1234, 0x0001, 0x2345, 3, 47, 0x0001, 0x5FCD, 1]
    # u32 mit CDAB
    rm2 = RegisterMap(make_cfg({"word_order": "CDAB"}), store)
    assert rm2.read(247, "ir", 2, 2) == [0x2345, 0x0001]
    with pytest.raises(IllegalAddress):
        rm.read(247, "ir", 3, 1)                         # zweite Hälfte des u32
    with pytest.raises(IllegalAddress):
        rm.read(247, "ir", 9, 1)


def test_diag_budget_callback(store: ValueStore):
    rm = RegisterMap(make_cfg(), store, budget_remaining=lambda: 17)
    assert rm.read(247, "ir", 5, 1) == [17]


def test_rebuild_changes_layout(store: ValueStore):
    rm = RegisterMap(make_cfg(), store)
    with pytest.raises(IllegalAddress):
        rm.read(16, "ir", 2000, 1)
    rm.rebuild(make_cfg({"int16_mirror": True}))
    assert rm.read(16, "ir", 2000, 1) == [0x8000]


# --- CSV --------------------------------------------------------------------------------------
def test_csv_content():
    text = export_csv(make_cfg({"int16_mirror": True}))
    assert text.startswith("﻿")
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))
    assert rows[0] == ["Unit-ID", "Register", "FC", "Gerät", "Nr", "Name", "Einheit", "Typ", "Schreibrecht",
                       "Hinweis"]
    body = {(r[0], r[1], r[2]): r for r in rows[1:]}
    v = body[("16", "0", "04")]
    assert v[3:7] == ["hk1", "0x0001", "Vorlauf", "°C"] and v[7].startswith("Float32") and v[8] == "nein"
    assert ("16", "2", "04") not in body                     # inaktiv
    assert ("16", "1000", "04") in body and ("16", "2000", "04") in body
    assert ("16", "9000", "04") in body and ("16", "9003", "04") in body
    soll = body[("16", "0", "03/16")]
    assert soll[5] == "Soll" and soll[8] == "ja" and "genau 2 Register" in soll[9]
    assert body[("16", "2", "03")][8] == "nein"             # nicht writable
    assert ("16", "1000", "03") in body
    assert body[("247", "0", "04")][5] == "Heartbeat" and body[("247", "0", "04")][4] == ""
    assert body[("16", "1000", "04")][4] == "0x0001"          # Status-Zeile mit Nr des Werts
    assert ("247", "8", "04") in body
    # ohne Int16-Spiegel keine 2000er Zeilen
    assert not any(r[1] == "2000" for r in list(csv.reader(
        io.StringIO(export_csv(make_cfg())), delimiter=";"))[1:])


def test_csv_write_right_requires_global_enable():
    cfg = make_cfg()
    cfg.safety.write_enabled = False
    rows = list(csv.reader(io.StringIO(export_csv(cfg).lstrip("﻿")), delimiter=";"))
    soll = next(r for r in rows if r[:3] == ["16", "0", "03"])
    assert soll[8] == "nein"
