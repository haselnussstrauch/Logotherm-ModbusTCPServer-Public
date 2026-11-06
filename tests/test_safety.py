"""Tests für gateway.safety – insbesondere Negativtests (was darf NIE gesendet werden)."""

from __future__ import annotations

import datetime as dt
import math
import struct

import pytest

from gateway import config, safety
from gateway.devices import fw_numbers as fw
from gateway.errors import SafetyViolation, WriteRejected
from gateway.safety import (
    check_can_tx, check_lc1_byte, check_lc1_command, check_lc1_param_write, check_lc1_tx,
    decode_can_id, encode_can_id, param_lock_reason, verify_readback,
)

N = fw.NUMBERS
OWN = 0xA0
DEV = 0x05
MC2 = 0x0A01
LC2 = 0x0B02
SONDE = 0xFFF2
DATABOX = 0xFFF0


def cid(kanal: int, sub: int, sender: int = OWN, dest: int = DEV) -> int:
    return encode_can_id(kanal, sender, dest, sub)


def anfrage(nr: int, sprache: int = 0, code: int = 0xFF, frei: bytes = b"\0\0\0\0") -> bytes:
    return struct.pack("<HBB", nr, sprache, code) + frei


def setzen(nr: int, vartyp: int = config.VarTyp.SHORT, wert: bytes = b"\x2a\x00") -> bytes:
    return struct.pack("<HB", nr, vartyp) + wert.ljust(5, b"\0")


def tx(arb: int, data: bytes, *, ext: bool = True, own: int = OWN, rt: int | None = MC2) -> None:
    check_can_tx(arb, data, is_extended=ext, own_address=own, regler_typ=rt)


# =============================================================================================
# Generierte Tabelle
# =============================================================================================
class TestFwNumbers:
    def test_bekannte_nummern(self):
        assert N["P_BUSADR"] == 0xFE42
        assert N["P_HKZYK1"] == 0x906C
        assert N["P_ZDELHEIZ"] == 0x026A
        assert N["P_SPEZI"] == 0xFF00
        assert N["P_ZYKLENWT"] == 0xFF40
        assert N["M_ANFHK"] == 0xE004            # innerhalb #if !_ENTFERNEN

    def test_auskommentierte_fehlen(self):
        assert "M_PKSAMMLER" not in N            # //-Kommentar
        assert "P_ZNG" not in N                  # /* … */-Block
        assert "P_KZZ2" not in N

    def test_names_umkehrung(self):
        assert set(fw.NAMES[0xFF00]) == {"M_SPEZI", "P_SPEZI", "T_SPEZI"}
        for name, nr in N.items():
            assert name in fw.NAMES[nr]

    def test_pseudo_pointer(self):
        assert 0x906C in fw.PSEUDO_POINTER_PARAMS               # AG1|P_HKZYK1
        assert (0x0100 | 0x906C) in fw.PSEUDO_POINTER_PARAMS    # AG2|P_HKZYK1
        assert N["P_PFILTERZ1"] in fw.PSEUDO_POINTER_PARAMS     # MultiControl 2
        assert N["P_KZZ1"] in fw.PSEUDO_POINTER_PARAMS
        assert 0x926C not in fw.PSEUDO_POINTER_PARAMS           # AG3 nur im /* */-Block
        assert all(0x40 <= nr & 0xFF <= 0x7F for nr in fw.PSEUDO_POINTER_PARAMS)

    def test_reglertypen(self):
        assert fw.REGLER_TYPEN[0xFFF2].startswith("Sonde")
        assert fw.REGLER_TYPEN[0xFFF0].startswith("Databox")
        assert 0x0B02 in fw.REGLER_TYPEN                       # auskommentierte Variante
        assert 0x0A01 in fw.REGLER_TYPEN


# =============================================================================================
# CAN-ID
# =============================================================================================
class TestCanId:
    def test_layout(self):
        arb = encode_can_id(0x0B, 0xA0, 0x05, 0x20)
        assert arb == (0x0B << 21) | (0xA0 << 13) | (0x05 << 5) | (0x20 >> 3)
        c = decode_can_id(arb)
        assert (c.kanal, c.sender, c.empfaenger, c.subkanal) == (0x0B, 0xA0, 0x05, 0x20)

    def test_firmware_dword(self):
        # Firmware: DWORD = Kanal<<24 | Quelle<<16 | Ziel<<8 | Kanal2; ID = DWORD >> 3
        dword = 0x0B << 24 | 0xA0 << 16 | 0x05 << 8 | 0x38
        assert decode_can_id(dword >> 3) == decode_can_id(encode_can_id(0x0B, 0xA0, 0x05, 0x38))

    @pytest.mark.parametrize("bad", [-1, 1 << 29, True, 1.0, None])
    def test_ungueltig(self, bad):
        with pytest.raises(SafetyViolation):
            decode_can_id(bad)

    def test_subkanal_vielfaches_von_8(self):
        with pytest.raises(ValueError):
            encode_can_id(0x0B, OWN, DEV, 0x24)


# =============================================================================================
# CAN-Sendeprüfung – erlaubte Dienste
# =============================================================================================
class TestCanErlaubt:
    def test_identify(self):
        tx(cid(0x0B, 0x00), b"")

    def test_messwert(self):
        tx(cid(0x0B, 0x20), anfrage(N["M_TKI"]))

    def test_messwert_detail(self):
        tx(cid(0x0B, 0x30), anfrage(N["M_TKI"]))

    def test_parameter_detail(self):
        tx(cid(0x0B, 0x38), anfrage(N["P_TKS"]))

    def test_parameter_schreiben(self):
        tx(cid(0x08, 0x00), setzen(N["P_TKS"], config.VarTyp.SHORT, b"\x46\x00"))

    @pytest.mark.parametrize("vt,size", [(0x00, 1), (0x01, 2), (0x02, 4), (0x04, 4), (0x10, 2),
                                         (0x11, 4), (0x80, 1), (0x81, 2), (0x82, 4)])
    def test_parameter_schreiben_vartypen(self, vt, size):
        tx(cid(0x08, 0x00), setzen(N["P_TKS"], vt, b"\x01" * size))

    def test_parameter_schreiben_aggregat(self):
        tx(cid(0x08, 0x00), setzen(0x0100 | N["P_HKTVS"]))   # AG2|P_HKTVS

    def test_bytearray(self):
        tx(cid(0x0B, 0x20), bytearray(anfrage(N["M_TKI"])))


# =============================================================================================
# CAN-Sendeprüfung – Verbote
# =============================================================================================
class TestCanVerboten:
    def test_standard_frame(self):
        with pytest.raises(SafetyViolation, match="Extended"):
            tx(cid(0x0B, 0x00) & 0x7FF, b"", ext=False)
        with pytest.raises(SafetyViolation):
            tx(cid(0x0B, 0x00), b"", ext=False)

    def test_remote_frame(self):
        with pytest.raises(SafetyViolation, match="RTR"):
            check_can_tx(cid(0x0B, 0x00), b"", is_extended=True, own_address=OWN,
                         is_remote_frame=True)

    def test_broadcast(self):
        with pytest.raises(SafetyViolation, match="Broadcast"):
            tx(cid(0x0B, 0x20, dest=0xFF), anfrage(N["M_TKI"]))

    def test_empfaenger_null(self):
        with pytest.raises(SafetyViolation):
            tx(cid(0x0B, 0x00, dest=0x00), b"")

    def test_empfaenger_selbst(self):
        with pytest.raises(SafetyViolation):
            tx(cid(0x0B, 0x00, dest=OWN), b"")

    @pytest.mark.parametrize("sender", [0x00, 0x01, 0x05, 0xA1, 0xFF])
    def test_falscher_sender(self, sender):
        with pytest.raises(SafetyViolation, match="Sender"):
            tx(cid(0x0B, 0x00, sender=sender), b"")

    @pytest.mark.parametrize("own", [0x00, 0xFF, -1, 256, None])
    def test_ungueltige_eigene_adresse(self, own):
        with pytest.raises(SafetyViolation):
            check_can_tx(cid(0x0B, 0x00), b"", is_extended=True, own_address=own)

    @pytest.mark.parametrize("sub", range(0, 0x100, 8))
    def test_kanal_0a_niemals(self, sub):
        with pytest.raises(SafetyViolation, match="0x0A"):
            tx(cid(0x0A, sub), setzen(N["M_ZUENDUNG"], 0x00, b"\x01"))

    def test_kanal_0a_zuendbefehl(self):
        # 0x0A/0x20 = Prozesswert -> Zündmodul zündet
        data = struct.pack("<HB", N["M_ZUENDUNG"], 0) + b"\x01\0\0\0\0"
        with pytest.raises(SafetyViolation):
            tx(cid(0x0A, 0x20), data)

    @pytest.mark.parametrize("sub", range(0, 0x100, 8))
    def test_kanal_10_bootloader(self, sub):
        with pytest.raises(SafetyViolation):
            tx(cid(0x10, sub), b"\0" * 8)

    @pytest.mark.parametrize("sub", [0x10, 0x18, 0x30])
    def test_kanal_00_uhrzeit(self, sub):
        with pytest.raises(SafetyViolation):
            tx(cid(0x00, sub), b"\0" * 8)

    @pytest.mark.parametrize("kanal", [k for k in range(0x100) if k not in (0x08, 0x0B)])
    def test_alle_anderen_kanaele(self, kanal):
        with pytest.raises(SafetyViolation):
            tx(cid(kanal, 0x00), b"")
        with pytest.raises(SafetyViolation):
            tx(cid(kanal, 0x20), anfrage(N["M_TKI"]))

    @pytest.mark.parametrize("sub", [s for s in range(0, 0x100, 8) if s not in (0x00, 0x08, 0x20, 0x30, 0x38)])
    def test_anfrage_andere_subkanaele(self, sub):
        with pytest.raises(SafetyViolation):
            tx(cid(0x0B, sub), anfrage(N["M_TKI"]))

    def test_auswahltexte_nur_einzeln(self):
        """Anfrage 0x0B/0x08 (Auswahltexte) ist reines Lesen – nur mit konkreter Nummer."""
        tx(cid(0x0B, 0x08), anfrage(0x9041))
        for nr in (0xFFFF, 0x0000):
            with pytest.raises(SafetyViolation):
                tx(cid(0x0B, 0x08), anfrage(nr))
            with pytest.raises(SafetyViolation):
                check_can_tx(cid(0x0B, 0x08), anfrage(nr), is_extended=True, own_address=OWN,
                                    regler_typ=None, is_remote_frame=False, allow_enumeration=True)

    def test_bootloader_befehl_bleibt_verboten(self):
        """Gleicher Subkanal 0x08, aber Befehls-Kanal 0x08 = Bootloader starten."""
        with pytest.raises(SafetyViolation, match="Bootloader"):
            tx(cid(0x08, 0x08), anfrage(0x9041))

    @pytest.mark.parametrize("sub", [s for s in range(0, 0x100, 8) if s != 0x00])
    def test_befehl_andere_subkanaele(self, sub):
        with pytest.raises(SafetyViolation):
            tx(cid(0x08, sub), setzen(N["P_TKS"]))

    def test_bootloader_befehl(self):
        with pytest.raises(SafetyViolation, match="Bootloader"):
            tx(cid(0x08, 0x08), b"xxBootZ\\")

    def test_erweiterte_parameter(self):
        with pytest.raises(SafetyViolation):
            tx(cid(0x08, 0x10), b"\x10\x00\x6c\x90\x0a\0\0\0")

    @pytest.mark.parametrize("cmd", [b"Start\0\0\0", b"Stop\0\0\0\0", b"Alive\0\0\0"])
    def test_aggregatetest(self, cmd):
        with pytest.raises(SafetyViolation, match="Aggregatetest"):
            tx(cid(0x08, 0x18), cmd)

    def test_fehlerspeicher_clr(self):
        with pytest.raises(SafetyViolation, match="Fehlerspeicher"):
            tx(cid(0x0B, 0x28), anfrage(0, frei=b"CLR\0"))

    def test_fehlerspeicher_lesen(self):
        with pytest.raises(SafetyViolation):
            tx(cid(0x0B, 0x28), anfrage(N["M_TKI"]))

    def test_dlc_ueber_8(self):
        with pytest.raises(SafetyViolation):
            tx(cid(0x0B, 0x20), anfrage(N["M_TKI"]) + b"\0")

    def test_keine_bytes(self):
        with pytest.raises(SafetyViolation):
            tx(cid(0x0B, 0x00), "")  # type: ignore[arg-type]

    # --- Inhalte der Anfragen ---
    def test_identify_mit_daten(self):
        with pytest.raises(SafetyViolation, match="Identify"):
            tx(cid(0x0B, 0x00), b"\0")

    @pytest.mark.parametrize("sub", [0x20, 0x30, 0x38])
    @pytest.mark.parametrize("dlc", [0, 1, 4, 7])
    def test_anfrage_dlc(self, sub, dlc):
        with pytest.raises(SafetyViolation, match="DLC"):
            tx(cid(0x0B, sub), anfrage(N["M_TKI"])[:dlc])

    @pytest.mark.parametrize("sub", [0x20, 0x30, 0x38])
    def test_anfrage_code(self, sub):
        with pytest.raises(SafetyViolation, match="Code"):
            tx(cid(0x0B, sub), anfrage(N["M_TKI"], code=0x00))

    @pytest.mark.parametrize("sub", [0x20, 0x30, 0x38])
    def test_anfrage_frei_ungleich_null(self, sub):
        with pytest.raises(SafetyViolation):
            tx(cid(0x0B, sub), anfrage(N["M_TKI"], frei=b"CLR\0"))

    @pytest.mark.parametrize("sub", [0x20, 0x30, 0x38])
    @pytest.mark.parametrize("nr", [0x0000, 0xFFFF])
    def test_anfrage_alle_oder_null(self, sub, nr):
        with pytest.raises(SafetyViolation):
            tx(cid(0x0B, sub), anfrage(nr))

    # --- Parameter schreiben ---
    @pytest.mark.parametrize("dlc", [0, 3, 7])
    def test_setzen_dlc(self, dlc):
        with pytest.raises(SafetyViolation, match="DLC"):
            tx(cid(0x08, 0x00), setzen(N["P_TKS"])[:dlc])

    @pytest.mark.parametrize("name", ["P_BUSADR", "P_SYSTEM", "P_KTYP", "P_BASIS", "P_DATE",
                                      "P_TIME", "P_CODE", "P_KZZ1", "P_PFILTERZ1", "P_ZYKLENWT",
                                      "P_ERWBUS", "P_DELTKS", "P_KDELP", "P_ZDELHEIZ"])
    def test_setzen_gesperrt(self, name):
        with pytest.raises(SafetyViolation, match="gesperrt"):
            tx(cid(0x08, 0x00), setzen(N[name]))

    def test_setzen_hkzyk1_ag2(self):
        with pytest.raises(SafetyViolation, match="gesperrt"):
            tx(cid(0x08, 0x00), setzen(0x0100 | N["P_HKZYK1"]))

    @pytest.mark.parametrize("name", ["M_TKI", "M_ZUENDUNG", "T_PRIPU", "T_ZNG"])
    def test_setzen_kein_parameter(self, name):
        with pytest.raises(SafetyViolation, match="kein Parameter"):
            tx(cid(0x08, 0x00), setzen(N[name]))

    def test_setzen_vt_zyklen(self):
        with pytest.raises(SafetyViolation, match="ZYKLEN"):
            tx(cid(0x08, 0x00), setzen(N["P_TKS"], config.VarTyp.ZYKLEN, b"\x01" * 5))

    @pytest.mark.parametrize("vt", [0x05, 0x07, 0x08, 0x09, 0x0B, 0x0F, 0x40, 0x41, 0xFF])
    def test_setzen_nicht_numerisch(self, vt):
        with pytest.raises(SafetyViolation, match="VarTyp"):
            tx(cid(0x08, 0x00), setzen(N["P_TKS"], vt))

    def test_setzen_ungenutzte_bytes(self):
        with pytest.raises(SafetyViolation, match="ungenutzte"):
            tx(cid(0x08, 0x00), setzen(N["P_TKS"], config.VarTyp.SHORT, b"\x01\x00\x01"))

    def test_setzen_sonde(self):
        for name in ("P_CALWERT", "P_RLABG_M", "P_PWM2", "P_PWM3"):
            with pytest.raises(SafetyViolation):
                tx(cid(0x08, 0x00), setzen(N[name]), rt=SONDE)
        tx(cid(0x08, 0x00), setzen(N["P_PWM1"]), rt=SONDE)
        tx(cid(0x08, 0x00), setzen(N["P_CALWERT"]), rt=MC2)

    def test_setzen_unbekannter_reglertyp(self):
        for rt in (None, 0x1234):
            for name in ("P_CALWERT", "P_IPADDR"):
                with pytest.raises(SafetyViolation):
                    tx(cid(0x08, 0x00), setzen(N[name]), rt=rt)


# =============================================================================================
# Gesperrte Parameter
# =============================================================================================
class TestParamLock:
    @pytest.mark.parametrize("name", ["P_BUSADR", "P_SYSTEM", "P_KTYP", "P_BASIS", "P_DATE",
                                      "P_TIME", "P_CODE", "P_KZZ1", "P_HKZYK1", "P_HKZYK4",
                                      "P_DELTKS", "P_DELPH", "P_DELTAGMAX", "P_DELFLASH",
                                      "P_DELLERR", "P_DELH", "P_KDELP", "P_KDELT", "P_KDELSVGR",
                                      "P_ZDELHEIZ", "P_ZDELGEBL", "P_ZYKLENWT", "P_ERWBUS",
                                      "P_PFILTERZ1", "P_PFILTERZ4", "P_OELDELP", "P_SOLDELT"])
    @pytest.mark.parametrize("rt", [MC2, LC2, SONDE, DATABOX, None])
    def test_immer_gesperrt(self, name, rt):
        assert param_lock_reason("can", rt, N[name])

    def test_alle_namensmuster_gesperrt(self):
        for name, nr in N.items():
            if name.startswith(("P_DEL", "P_KDEL", "P_ZDEL", "P_HKZYK")):
                assert param_lock_reason("can", MC2, nr), name

    def test_spezi_bereich(self):
        for nr in range(0xFF00, 0x10000):
            assert param_lock_reason("can", MC2, nr)

    @pytest.mark.parametrize("ag", range(16))
    def test_aggregatregel_alle_aggregate(self, ag):
        for name in ("P_HKZYK1", "P_DELTKS", "P_KDELP", "P_PFILTERZ1", "P_OELDELP"):
            assert param_lock_reason("can", MC2, N[name] | ag << 8), (name, ag)

    def test_aggregatregel_nur_exakt_mit_aggregatbits(self):
        # P_ZDELHEIZ = 0x026a hat bereits Aggregat-Bits -> nur exakt
        assert param_lock_reason("can", MC2, 0x026A)
        assert param_lock_reason("can", MC2, 0x006A) is None or \
            "ZDELHEIZ" not in param_lock_reason("can", MC2, 0x006A)
        assert param_lock_reason("can", MC2, 0x036A) is None
        assert param_lock_reason("can", MC2, 0x036C) is None      # P_KZZ1 nur exakt

    def test_aggregatregel_ab_f000_nur_exakt(self):
        assert param_lock_reason("can", MC2, N["P_BUSADR"])
        assert param_lock_reason("can", MC2, N["P_BUSADR"] & ~0x0F00) is None or \
            "BUSADR" not in param_lock_reason("can", MC2, N["P_BUSADR"] & ~0x0F00)
        assert param_lock_reason("can", MC2, 0xFD40) is None       # P_PWM1

    def test_pseudo_pointer_alle(self):
        for nr in fw.PSEUDO_POINTER_PARAMS:
            for rt in (MC2, LC2, SONDE, DATABOX, None):
                assert param_lock_reason("can", rt, nr), hex(nr)

    @pytest.mark.parametrize("nr", [0x0000, 0x0001, 0x003F, 0x0080, 0x00FF, 0x9101, 0xFE80])
    def test_kein_parameter(self, nr):
        assert param_lock_reason("can", MC2, nr)

    def test_freie_parameter(self):
        for name in ("P_TKS", "P_TKSAUTO", "P_HKTVS", "P_PWM1"):
            assert param_lock_reason("can", MC2, N[name]) is None, name
        assert param_lock_reason("can", MC2, 0x0100 | N["P_HKTVS"]) is None

    def test_sonde(self):
        sonde = [n for n in N if n.startswith(("P_CAL", "P_RLABG_"))] + ["P_PWM2", "P_PWM3"]
        assert len(sonde) >= 12
        for name in sonde:
            assert param_lock_reason("can", SONDE, N[name]), name
            assert param_lock_reason("can", None, N[name]), name
            assert param_lock_reason("can", 0x4242, N[name]), name
            assert param_lock_reason("can", MC2, N[name]) is None, name
            assert param_lock_reason("can", DATABOX, N[name]) is None, name

    def test_databox(self):
        for name in ("P_CANBUS", "P_IPADDR", "P_DHCP"):
            assert param_lock_reason("can", DATABOX, N[name]), name
            assert param_lock_reason("can", None, N[name]), name
            assert param_lock_reason("can", MC2, N[name]) is None, name
            assert param_lock_reason("can", SONDE, N[name]) is None, name

    @pytest.mark.parametrize("bad", [-1, 0x10000, None, 1.5, True])
    def test_ungueltige_nummer(self, bad):
        assert param_lock_reason("can", MC2, bad)

    def test_unbekannter_bus(self):
        assert param_lock_reason("modbus", MC2, N["P_TKS"])

    def test_lc1(self):
        locked = {0, 1, 2, 3, *range(4, 15), 29, 30, 31, 32, 33, 46, 47, 48, 49, 50, 51, 52, 53,
                  54, 55, 56, 57, 58, 59, 60, 61, 62, 63}
        for e in range(64):
            r = param_lock_reason("lc1", None, e)
            assert bool(r) == (e in locked), e
        for e in (-1, 64, 100):
            assert param_lock_reason("lc1", None, e)

    def test_lc1_ebenen_regel(self, monkeypatch):
        """F8: Ebene >2 sperrt auch ohne Eintrag in LC1_LOCKED; fehlender Tabelleneintrag sperrt."""
        from gateway.devices import lc1_tables
        for e, p in lc1_tables.PARAMS.items():
            if p.level not in {"0", "1", "2"}:
                assert param_lock_reason("lc1", None, e), e
        monkeypatch.setattr(safety, "LC1_LOCKED", {})
        r = param_lock_reason("lc1", None, 4)            # E4: Ebene 4 (Kalibrierung)
        assert r and "Ebene 4" in r
        assert param_lock_reason("lc1", None, 16) is None  # E16: Ebene 2
        monkeypatch.setattr(lc1_tables, "PARAMS", {})
        assert "Parametertabelle" in param_lock_reason("lc1", None, 16)

    def test_config_validator_nutzt_sperre(self):
        dev = {"id": "mc2", "bus": "can", "address": 5, "unit_id": 5, "regler_typ": MC2,
               "params": [{"nr": N["P_BUSADR"], "writable": True, "vartyp": 0}]}
        with pytest.raises(Exception, match="gesperrt"):
            config.GatewayConfig.model_validate({"devices": [dev]})
        dev["params"][0]["nr"] = N["P_TKS"]
        config.GatewayConfig.model_validate({"devices": [dev]})


# =============================================================================================
# LC1
# =============================================================================================
class TestLc1:
    @pytest.mark.parametrize("b", sorted(safety.FORBIDDEN_LC1_BYTES))
    def test_verbotene_bytes(self, b):
        with pytest.raises(SafetyViolation):
            check_lc1_byte(b)
        with pytest.raises(SafetyViolation):
            check_lc1_tx(bytes([0x35, b, 0x0D]))

    @pytest.mark.parametrize("b", [-1, 256, None, True])
    def test_ungueltiges_byte(self, b):
        with pytest.raises(SafetyViolation):
            check_lc1_byte(b)

    def test_erlaubte_bytes(self):
        check_lc1_tx(bytes(b for b in range(256) if b not in safety.FORBIDDEN_LC1_BYTES))

    def test_tx_typ(self):
        with pytest.raises(SafetyViolation):
            check_lc1_tx("05")  # type: ignore[arg-type]

    @pytest.mark.parametrize("func", [48, 49, 50, 53])
    def test_erlaubte_befehle(self, func):
        check_lc1_command(func)

    @pytest.mark.parametrize("func", [51, 52])
    def test_51_52_verboten(self, func):
        with pytest.raises(SafetyViolation, match="verboten"):
            check_lc1_command(func)

    @pytest.mark.parametrize("func", [0, 3, 47, 54, 55, 57, 0x1B, 255, None])
    def test_andere_befehle(self, func):
        with pytest.raises(SafetyViolation):
            check_lc1_command(func)

    def test_param_write_ok(self):
        check_lc1_param_write(16, 0)
        check_lc1_param_write(15, 65535)
        check_lc1_param_write(28, 1234)

    @pytest.mark.parametrize("addr", [-1, 64, 100, None, 3.0])
    def test_param_write_adresse(self, addr):
        with pytest.raises(SafetyViolation):
            check_lc1_param_write(addr, 1)

    @pytest.mark.parametrize("val", [-1, 65536, 1.5, None, True])
    def test_param_write_wert(self, val):
        with pytest.raises(SafetyViolation):
            check_lc1_param_write(3, val)

    @pytest.mark.parametrize("addr", [0, 1, 2, 4, 9, 14, 29, 48, 51, 54, 58, 60, 63])
    def test_param_write_gesperrt(self, addr):
        with pytest.raises(SafetyViolation, match="gesperrt"):
            check_lc1_param_write(addr, 1)


# =============================================================================================
# WriteGuard
# =============================================================================================
class FakeClock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _ts(*args: int) -> float:
    return dt.datetime(*args).timestamp()    # lokale Zeit


def can_dev(**param_kw) -> tuple[config.DeviceConfig, config.ParamDef]:
    kw = {"nr": N["P_TKS"], "writable": True, "decimals": 1, "min": 50.0, "max": 90.0}
    kw.update(param_kw)
    p = config.ParamDef(**kw)
    d = config.DeviceConfig(id="mc2", bus="can", address=5, unit_id=5, regler_typ=MC2, params=[p])
    return d, p


def lc1_dev(**param_kw) -> tuple[config.DeviceConfig, config.ParamDef]:
    kw = {"e": 15, "writable": True, "decimals": 1}
    kw.update(param_kw)
    p = config.ParamDef(**kw)
    d = config.DeviceConfig(id="lc1", bus="lc1", port="COM5", unit_id=101, params=[p])
    return d, p


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(_ts(2026, 11, 3, 12, 0, 0))


@pytest.fixture
def guard(clock) -> safety.WriteGuard:
    return safety.WriteGuard(config.SafetyConfig(write_enabled=True, daily_write_budget=5), clock)


def rejected(guard, dev, p, value, current=None) -> WriteRejected:
    with pytest.raises(WriteRejected) as ei:
        guard.check(dev, p, value, current)
    return ei.value


class TestWriteGuard:
    def test_ok(self, guard):
        d, p = can_dev()
        assert guard.check(d, p, 70.0, 60.0) is True
        assert guard.check(d, p, 70.0, None) is True

    def test_global_aus(self, clock):
        g = safety.WriteGuard(config.SafetyConfig(), clock)
        d, p = can_dev()
        assert rejected(g, d, p, 70.0).kind == "address"

    def test_nicht_freigegeben(self, guard):
        d, p = can_dev(writable=False)
        assert rejected(guard, d, p, 70.0).kind == "address"

    def test_inaktiv(self, guard):
        d, p = can_dev(active=False)
        assert rejected(guard, d, p, 70.0).kind == "address"

    @pytest.mark.parametrize("nr", [N["P_BUSADR"], 0x0100 | N["P_HKZYK1"], N["P_PFILTERZ1"],
                                    N["P_ZYKLENWT"], N["T_PRIPU"], N["M_TKI"]])
    def test_gesperrt(self, guard, nr):
        d, p = can_dev(nr=nr, min=None, max=None)
        assert rejected(guard, d, p, 1.0).kind == "address"

    def test_gesperrt_sonde(self, guard):
        p = config.ParamDef(nr=N["P_CALWERT"], writable=True)
        d = config.DeviceConfig(id="s", bus="can", address=7, unit_id=7, regler_typ=SONDE, params=[p])
        assert rejected(guard, d, p, 1.0).kind == "address"

    def test_gesperrt_lc1(self, guard):
        d, p = lc1_dev(e=29)
        assert rejected(guard, d, p, 1.0).kind == "address"

    @pytest.mark.parametrize("v", [math.nan, math.inf, -math.inf])
    def test_nan_inf(self, guard, v):
        d, p = can_dev(min=None, max=None)
        assert rejected(guard, d, p, v).kind == "value"

    @pytest.mark.parametrize("v", ["70", None, True])
    def test_keine_zahl(self, guard, v):
        d, p = can_dev()
        assert rejected(guard, d, p, v).kind == "value"

    @pytest.mark.parametrize("v", [49.9, 90.1, -1000.0, 1e9])
    def test_min_max(self, guard, v):
        d, p = can_dev()
        assert rejected(guard, d, p, v).kind == "value"

    def test_min_max_grenzen(self, guard):
        d, p = can_dev()
        assert guard.check(d, p, 50.0, None)
        assert guard.check(d, p, 90.0, None)

    def test_lc1_skalierung(self, guard):
        d, p = lc1_dev(decimals=1)
        assert guard.check(d, p, 6553.5, None)          # Rohwert 65535
        assert rejected(guard, d, p, 6553.6).kind == "value"
        assert rejected(guard, d, p, -0.1).kind == "value"
        assert guard.check(d, p, -0.04, None)            # Rohwert round(-0.4) = 0

    def test_totband(self, guard):
        d, p = can_dev(deadband=1.0)
        assert guard.check(d, p, 70.0, 69.0) is False
        assert guard.check(d, p, 70.0, 71.0) is False
        assert guard.check(d, p, 70.0, 68.9) is True

    def test_totband_mindestens_halbe_stelle(self, guard):
        d, p = can_dev(deadband=0.0, decimals=1)
        assert guard.check(d, p, 70.0, 70.04) is False
        assert guard.check(d, p, 70.0, 70.1) is True
        d, p = can_dev(deadband=0.0, decimals=0)
        assert guard.check(d, p, 70.0, 70.5) is False
        assert guard.check(d, p, 70.0, 71.0) is True

    def test_totband_unbekannter_istwert(self, guard):
        d, p = can_dev(deadband=5.0)
        assert guard.check(d, p, 70.0, None) is True
        assert guard.check(d, p, 70.0, math.nan) is True

    def test_totband_verbraucht_kein_budget(self, guard):
        d, p = can_dev(deadband=1.0)
        for _ in range(20):
            assert guard.check(d, p, 70.0, 70.0) is False
        assert guard.writes_today == 0

    def test_ratenlimit(self, guard, clock):
        d, p = can_dev()
        assert guard.check(d, p, 70.0, 60.0)
        guard.commit(d.id, p.key)
        clock.t += 1
        assert rejected(guard, d, p, 75.0, 70.0).kind == "value"
        clock.t += 598
        assert rejected(guard, d, p, 75.0, 70.0).kind == "value"
        clock.t += 1                       # genau 600 s
        assert guard.check(d, p, 75.0, 70.0) is True

    def test_ratenlimit_je_parameter_und_geraet(self, guard):
        d, p = can_dev()
        guard.commit(d.id, p.key)
        d2, p2 = can_dev(nr=N["P_TKSAUTO"], min=None, max=None)
        assert guard.check(d2, p2, 1.0, None)
        d3 = d.model_copy(update={"id": "anderes"})
        assert guard.check(d3, p, 70.0, None)

    def test_ratenlimit_uhr_rueckwaerts(self, guard, clock):
        d, p = can_dev()
        guard.commit(d.id, p.key)
        clock.t -= 3600
        assert rejected(guard, d, p, 70.0).kind == "value"

    def test_ratenlimit_nicht_konfigurierbar(self):
        assert safety.MIN_WRITE_INTERVAL_S == 600
        assert "min_write_interval" not in config.SafetyConfig.model_fields

    def test_budget(self, guard, clock):
        for i in range(5):
            assert guard.remaining_budget == 5 - i
            guard.commit("mc2", 0x1000 + i)
        assert guard.writes_today == 5
        assert guard.remaining_budget == 0
        d, p = can_dev()
        assert rejected(guard, d, p, 70.0).kind == "value"

    def test_budget_null(self, clock):
        g = safety.WriteGuard(config.SafetyConfig(write_enabled=True, daily_write_budget=0), clock)
        d, p = can_dev()
        assert rejected(g, d, p, 70.0).kind == "value"

    def test_mitternachts_reset(self):
        clock = FakeClock(_ts(2026, 11, 3, 23, 50, 0))
        g = safety.WriteGuard(config.SafetyConfig(write_enabled=True, daily_write_budget=2), clock)
        g.commit("a", 1)
        g.commit("a", 2)
        d, p = can_dev()
        assert rejected(g, d, p, 70.0).kind == "value"
        clock.t = _ts(2026, 11, 3, 23, 59, 59)
        assert g.remaining_budget == 0
        clock.t = _ts(2026, 11, 4, 0, 0, 0)
        assert g.writes_today == 0
        assert g.remaining_budget == 2
        assert g.check(d, p, 70.0, None) is True

    def test_mitternacht_ratenlimit_bleibt(self):
        clock = FakeClock(_ts(2026, 11, 3, 23, 58, 0))
        g = safety.WriteGuard(config.SafetyConfig(write_enabled=True, daily_write_budget=5), clock)
        d, p = can_dev()
        g.commit(d.id, p.key)
        clock.t = _ts(2026, 11, 4, 0, 1, 0)
        assert rejected(g, d, p, 70.0).kind == "value"

    def test_hot_reload(self, guard):
        d, p = can_dev()
        guard.commit("x", 1)
        guard.commit("x", 2)
        guard.cfg = config.SafetyConfig(write_enabled=True, daily_write_budget=2)
        assert guard.writes_today == 2
        assert guard.remaining_budget == 0
        assert rejected(guard, d, p, 70.0).kind == "value"
        guard.cfg = config.SafetyConfig(write_enabled=False, daily_write_budget=10)
        assert rejected(guard, d, p, 70.0).kind == "address"
        guard.cfg = config.SafetyConfig(write_enabled=True, daily_write_budget=10)
        assert guard.check(d, p, 70.0, None) is True
        assert guard.remaining_budget == 8

    def test_reihenfolge_adresse_vor_wert(self, clock):
        g = safety.WriteGuard(config.SafetyConfig(write_enabled=False), clock)
        d, p = can_dev()
        assert rejected(g, d, p, math.nan).kind == "address"


# =============================================================================================
# Rückmeldung
# =============================================================================================
class TestReadback:
    def test_gleich(self):
        assert verify_readback(21.5, 21.5, 1)
        f32 = struct.unpack("<f", struct.pack("<f", 0.1))[0]
        assert verify_readback(0.1, f32, 1)
        assert verify_readback(70, 70.0, 0)

    def test_toleranz(self):
        assert verify_readback(21.5, 21.54, 1)
        assert verify_readback(21.5, 21.45, 1)
        assert not verify_readback(21.5, 21.6, 1)
        assert not verify_readback(21.5, 21.56, 1)
        assert verify_readback(70, 70.5, 0)
        assert not verify_readback(70, 71, 0)
        assert not verify_readback(1.234, 1.236, 3)

    @pytest.mark.parametrize("a", [math.nan, math.inf, None, "x"])
    def test_ungueltig(self, a):
        assert not verify_readback(21.5, a, 1)


# --- Aufzählung (Nr=0xFFFF) nur mit expliziter Freigabe -------------------------------------
_ENUM = (0xFFFF).to_bytes(2, "little") + bytes([0, 0xFF, 0, 0, 0, 0])


@pytest.mark.parametrize("sub", [0x30, 0x38])
def test_enumeration_requires_explicit_flag(sub):
    cid = encode_can_id(0x0B, 0xA0, 0x01, sub)
    with pytest.raises(SafetyViolation):
        check_can_tx(cid, _ENUM, is_extended=True, own_address=0xA0)
    check_can_tx(cid, _ENUM, is_extended=True, own_address=0xA0, allow_enumeration=True)


def test_enumeration_flag_not_for_messwert_0x20():
    cid = encode_can_id(0x0B, 0xA0, 0x01, 0x20)
    with pytest.raises(SafetyViolation):
        check_can_tx(cid, _ENUM, is_extended=True, own_address=0xA0, allow_enumeration=True)


def test_enumeration_flag_no_broadcast():
    cid = encode_can_id(0x0B, 0xA0, 0xFF, 0x38)
    with pytest.raises(SafetyViolation):
        check_can_tx(cid, _ENUM, is_extended=True, own_address=0xA0, allow_enumeration=True)


# =============================================================================================
# Sperren nach Ausgabetyp (F4: "Was die Firmware nicht herausgibt oder nicht prüft …")
# =============================================================================================
class TestOutTypeLock:
    FREI = N["P_TKS"]

    @pytest.mark.parametrize("out", [safety.OUT_SECTOH, safety.OUT_ZYKLEN, safety.OUT_ZYKLENZEITEN,
                                     safety.OUT_RAMSTRING, safety.OUT_IPV4, safety.OUT_SERVICECODE,
                                     safety.OUT_DATE, safety.OUT_TIME, safety.OUT_URLAUBVON,
                                     safety.OUT_URLAUBBIS, safety.OUT_SECTOMIN, safety.OUT_CNTSECMIN,
                                     safety.OUT_SECTOTIME, safety.OUT_MINVONBIS, safety.OUT_TAGMONAT,
                                     21, 0x3F, safety.OUT_RAM | safety.OUT_SECTOH])
    def test_gesperrt(self, out):
        assert param_lock_reason("can", LC2, self.FREI, out_type=out)

    def test_werte_aus_firmware(self):
        # OUTPUT_FLAG, Include/Common/MesswertNummern.h:1121-1156
        assert (safety.OUT_SECTOH, safety.OUT_TEXTAUSWAHL, safety.OUT_ZYKLEN, safety.OUT_IPV4,
                safety.OUT_ZYKLENZEITEN, safety.OUT_RAMSTRING) == (3, 8, 17, 18, 20, 0x48)

    @pytest.mark.parametrize("out", [safety.OUT_DECIMAL, safety.OUT_ONOFF, safety.OUT_YESNO,
                                     safety.OUT_TEXTAUSWAHL, safety.OUT_FLASHSTRING, safety.OUT_HEXADECIMAL,
                                     safety.OUT_MINTOTIME, safety.OUT_GRAPH])
    def test_erlaubt(self, out):
        assert param_lock_reason("can", LC2, self.FREI, out_type=out) is None

    def test_abwaertskompatibel(self):
        assert param_lock_reason("can", LC2, self.FREI) is None
        assert param_lock_reason("can", LC2, self.FREI, None) is None
        # gesperrter Parameter bleibt gesperrt, Grund ändert sich nicht
        assert param_lock_reason("can", LC2, N["P_BUSADR"], out_type=0) == param_lock_reason("can", LC2, N["P_BUSADR"])

    def test_lc1_ignoriert_out_type(self):
        assert param_lock_reason("lc1", None, 15, out_type=safety.OUT_SECTOH) is None

    def test_ungueltig(self):
        assert safety.out_type_lock_reason(-1) and safety.out_type_lock_reason(256)
        assert safety.out_type_lock_reason(True)
