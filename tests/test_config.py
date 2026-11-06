"""Tests für das Konfigurationsmodell (gateway.yaml)."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from gateway.config import (
    GatewayConfig, ParamDef, ValueDef, VarTyp, assign_registers, load_config, renumber_registers,
    save_config, suggest_unit_id,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "docs" / "gateway.example.yaml"


def can_dev(**kw):
    d = dict(id="can-01", bus="can", address=0x01, unit_id=1)
    d.update(kw)
    return d


def lc1_dev(**kw):
    d = dict(id="lc1-1", bus="lc1", port="COM5", unit_id=101)
    d.update(kw)
    return d


def test_defaults_safe():
    cfg = GatewayConfig()
    assert cfg.safety.write_enabled is False
    assert cfg.web.bind == "127.0.0.1"
    assert cfg.can.gateway_address == 0xA0
    assert cfg.can.bitrate == 50000
    assert cfg.modbus.diag_unit_id == 247


def test_example_loads():
    cfg = load_config(EXAMPLE)
    assert [d.id for d in cfg.devices] == ["can-01", "lc1-1"]
    assert cfg.devices[0].values[1].reg == 2


def test_unknown_key_rejected():
    with pytest.raises(ValidationError):
        GatewayConfig.model_validate({"modbus": {"prot": 502}})


def test_min_gap_not_below_20ms():
    with pytest.raises(ValidationError):
        GatewayConfig.model_validate({"can": {"timing": {"min_gap_ms": 10}}})


@pytest.mark.parametrize("bad", [
    {"devices": [can_dev(), can_dev(id="can-02", address=0x02)]},             # Unit-ID doppelt
    {"devices": [can_dev(unit_id=247)]},                                      # Diagnose-Unit
    {"devices": [can_dev(), can_dev(id="can-01", address=0x02, unit_id=2)]},  # ID doppelt
    {"devices": [can_dev(), can_dev(id="x", unit_id=2)]},                     # CAN-Adresse doppelt
    {"devices": [can_dev(address=0xA0)]},                                     # = Gateway-Adresse
    {"devices": [lc1_dev(), lc1_dev(id="lc1-2", unit_id=102)]},               # Port doppelt
    {"devices": [can_dev(unit_id=0)]},
    {"devices": [can_dev(unit_id=248)]},
    {"devices": [can_dev(port="COM1")]},                                      # Port an CAN-Gerät
    {"devices": [lc1_dev(address=1)]},
    {"devices": [lc1_dev(port=None)]},
    {"devices": [can_dev(values=[{"k": 1}])]},                                # LC1-Feld an CAN
    {"devices": [lc1_dev(values=[{"nr": 1}])]},
    {"devices": [lc1_dev(params=[{"e": 64}])]},
    {"devices": [can_dev(values=[{"nr": 1, "reg": 3}])]},                     # ungerade
    {"devices": [can_dev(values=[{"nr": 1, "reg": 0}, {"nr": 2, "reg": 0}])]},
    {"devices": [can_dev(values=[{"nr": 1}, {"nr": 1}])]},
    {"devices": [can_dev(values=[{"nr": 1, "reg": 1000}])]},                  # > 998
    {"devices": [can_dev(values=[{"nr": 1, "vartyp": VarTyp.DATUM}])]},       # nicht numerisch
    {"devices": [can_dev(params=[{"nr": 0x41, "vartyp": VarTyp.ZYKLEN}])]},
])
def test_invalid_configs(bad):
    with pytest.raises(ValidationError):
        GatewayConfig.model_validate(bad)


def test_non_numeric_allowed_when_inactive():
    cfg = GatewayConfig.model_validate(
        {"devices": [can_dev(values=[{"nr": 1, "vartyp": VarTyp.ZEIT, "active": False}])]})
    assert cfg.devices[0].values[0].active is False


def test_max_entries():
    vals = [{"nr": i} for i in range(501)]
    with pytest.raises(ValidationError):
        GatewayConfig.model_validate({"devices": [can_dev(values=vals)]})
    cfg = GatewayConfig.model_validate({"devices": [can_dev(values=vals[:500])]})
    assign_registers(cfg.devices[0].values)
    assert cfg.devices[0].values[-1].reg == 998


def test_register_assignment_keeps_addresses():
    entries = [ValueDef(nr=1, reg=0), ValueDef(nr=2, reg=2, active=False), ValueDef(nr=3, reg=4)]
    entries.append(ValueDef(nr=4))
    assign_registers(entries)
    # abgewählter Wert (reg 2) behält seine Adresse, neuer Wert hinten angehängt
    assert [e.reg for e in entries] == [0, 2, 4, 6]


def test_register_assignment_appends_after_highest():
    entries = [ValueDef(nr=1, reg=10), ValueDef(nr=2), ValueDef(nr=3)]
    assign_registers(entries)
    assert [e.reg for e in entries] == [10, 12, 14]


def test_renumber_only_explicit():
    entries = [ValueDef(nr=1, reg=4), ValueDef(nr=2, reg=0, active=False), ValueDef(nr=3, reg=8)]
    kept = renumber_registers(entries)
    assert [(e.nr, e.reg) for e in kept] == [(1, 0), (3, 2)]


def test_suggest_unit_id():
    cfg = GatewayConfig.model_validate({"devices": [can_dev(), lc1_dev()]})
    assert suggest_unit_id(cfg, "can", 0x02) == 2
    assert suggest_unit_id(cfg, "can", 0x01) == 2          # belegt -> nächste freie
    assert suggest_unit_id(cfg, "can", 0xE0) == 224        # Sonde, <= 246 -> übernommen
    assert suggest_unit_id(cfg, "can", 0xFC) == 2          # Databox 252 > 246 -> erste freie
    assert suggest_unit_id(cfg, "lc1") == 102


def test_save_roundtrip(tmp_path):
    cfg = load_config(EXAMPLE)
    out = tmp_path / "gateway.yaml"
    save_config(cfg, out)
    text = out.read_text(encoding="utf-8")
    assert "nr: 0x0003" in text and "gateway_address: 0xA0" in text
    assert load_config(out) == cfg
    assert yaml.safe_load(text)["devices"][0]["address"] == 1


def test_missing_file_gives_defaults(tmp_path):
    assert load_config(tmp_path / "fehlt.yaml") == GatewayConfig()


def test_empty_allowlist_warns(tmp_path, caplog):
    p = tmp_path / "g.yaml"
    p.write_text("modbus: {allow: []}\n", encoding="utf-8")
    load_config(p)
    assert "Allowlist ist leer" in caplog.text


def test_writable_locked_param_rejected():
    pytest.importorskip("gateway.safety")
    from gateway.devices.fw_numbers import NUMBERS
    bad = {"devices": [can_dev(params=[{"nr": NUMBERS["P_BUSADR"], "vartyp": 1, "writable": True}])]}
    with pytest.raises(ValidationError, match="gesperrt"):
        GatewayConfig.model_validate(bad)
    bad = {"devices": [lc1_dev(params=[{"e": 0, "vartyp": 1, "writable": True}])]}
    with pytest.raises(ValidationError, match="gesperrt"):
        GatewayConfig.model_validate(bad)


def test_paramdef_has_no_k():
    with pytest.raises(ValidationError):
        ParamDef(k=1)


def test_inactive_without_reg_gets_no_address():
    entries = [ValueDef(nr=1, active=False), ValueDef(nr=2), ValueDef(nr=3, active=False)]
    assign_registers(entries)
    assert [e.reg for e in entries] == [None, 0, None]
    entries[2].active = True
    assign_registers(entries)
    assert [e.reg for e in entries] == [None, 0, 2]
