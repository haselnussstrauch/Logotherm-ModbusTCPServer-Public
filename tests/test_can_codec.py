"""Codec-Tests: ID, CRC16, Multiframe, Nachrichten, VarTyp – gegen die Firmware-Logik."""

from __future__ import annotations

import math
import struct

import pytest

from gateway import safety
from gateway.can_lc2 import idcodec, messages, multiframe, vartyp
from gateway.config import NUMERIC_VARTYPES, VarTyp as VT


# ===== ID ======================================================================================
@pytest.mark.parametrize("fields", [(0x0B, 0xA0, 0x01, 0x20), (0x0A, 0x01, 0xA0, 0x38), (0x08, 0xA0, 0xFE, 0x00),
                                    (0xFF, 0xFF, 0xFF, 0xF8), (0, 0, 0, 0)])
def test_id_roundtrip_und_gleich_safety(fields):
    arb = idcodec.encode(*fields)
    assert arb == safety.encode_can_id(*fields)
    f = idcodec.decode(arb)
    s = safety.decode_can_id(arb)
    assert (f.kanal, f.sender, f.empfaenger, f.subkanal) == fields == (s.kanal, s.sender, s.empfaenger, s.subkanal)


def test_id_layout_firmware():
    # DWORD Kanal<<24|Sender<<16|Empf<<8|Sub, gesendet >> 3 (CCAN::Encode)
    dword = 0x0B << 24 | 0xA0 << 16 | 0x01 << 8 | 0x20
    assert idcodec.encode(0x0B, 0xA0, 0x01, 0x20) == dword >> 3


def test_id_ungueltig():
    with pytest.raises(ValueError):
        idcodec.encode(0x0B, 1, 2, 0x21)
    with pytest.raises(ValueError):
        idcodec.decode(1 << 29)


# ===== CRC16 ===================================================================================
def test_crc_tabelle_wie_firmware():
    # Stichproben aus crc_ccitt_table, os2/Kernel/CRC16.cpp:55-89
    t = multiframe.CRC_TABLE
    assert t[:8] == (0x0000, 0x1021, 0x2042, 0x3063, 0x4084, 0x50a5, 0x60c6, 0x70e7)
    assert t[8:16] == (0x8108, 0x9129, 0xa14a, 0xb16b, 0xc18c, 0xd1ad, 0xe1ce, 0xf1ef)
    assert t[16] == 0x1231 and t[17] == 0x0210
    assert t[0x80:0x84] == (0x9188, 0x81a9, 0xb1ca, 0xa1eb)
    assert t[248:256] == (0x6e17, 0x7e36, 0x4e55, 0x5e74, 0x2e93, 0x3eb2, 0x0ed1, 0x1ef0)


def _crc_c(data: bytes) -> int:
    """Wörtliche Nachbildung von _crc16 mit 16-Bit-Arithmetik (WORD)."""
    s = 0
    for b in data:
        s = (multiframe.CRC_TABLE[((s >> 8) ^ b) & 0xFF] ^ ((s << 8) & 0xFFFF)) & 0xFFFF
    return s


def test_crc_vektoren():
    assert multiframe.crc16(b"123456789") == 0x31C3          # CRC-16/XMODEM
    assert multiframe.crc16(b"") == 0
    for data in (b"\x00", b"\xff" * 7, bytes(range(256)), b"\x1b\x00\x0b\x00"):
        assert multiframe.crc16(data) == _crc_c(data)


def test_generate_crc_big_endian_und_rest_null():
    buf = bytes(range(10))
    blk = multiframe.append_crc(buf)
    crc = multiframe.crc16(buf)
    assert blk[-2] == crc >> 8 and blk[-1] == crc & 0xFF
    assert multiframe.crc16(blk) == 0 and multiframe.checksum_ok(blk)


# ===== Multiframe ==============================================================================
def test_make_block_len_inkl_crc():
    blk = multiframe.make_block(b"abcdef")
    assert int.from_bytes(blk[:2], "little") == len(blk) == 10


def test_split_frames():
    blk = multiframe.make_block(bytes(15))      # 19 Byte
    frames = multiframe.split_frames(blk)
    assert [len(f) for f in frames] == [8, 8, 3]
    assert b"".join(frames) == blk


def test_split_len_vielfaches_von_8_zusatzframe():
    blk = multiframe.make_block(bytes(12))      # 16 Byte
    frames = multiframe.split_frames(blk, pad=0x77)
    assert [len(f) for f in frames] == [8, 8, 1] and frames[-1] == b"\x77"


def _feed_all(r, arb, frames, t=0.0):
    out = []
    for f in frames:
        out.append(r.feed(arb, f, now=t))
    return out


def test_reassembly_ok_und_zusatzframe_ignoriert():
    r = multiframe.Reassembler()
    blk = multiframe.make_block(bytes(range(12)))   # 16 Byte -> Zusatzframe
    res = _feed_all(r, 1, multiframe.split_frames(blk))
    assert [x.status for x in res] == ["partial", "ok", "ignored"]
    assert res[1].block == blk and len(r) == 0


def test_reassembly_kurzer_rahmen_beendet():
    r = multiframe.Reassembler()
    blk = multiframe.make_block(bytes(range(21)))
    res = _feed_all(r, 7, multiframe.split_frames(blk))
    assert res[-1].status == "ok" and res[-1].block == blk


def test_reassembly_crc_fehler():
    r = multiframe.Reassembler()
    blk = bytearray(multiframe.make_block(bytes(21)))
    blk[5] ^= 1
    res = _feed_all(r, 1, multiframe.split_frames(bytes(blk)))
    assert res[-1].status == "crc" and len(r) == 0


def test_reassembly_ungueltiger_start():
    r = multiframe.Reassembler()
    assert r.feed(1, b"\x10\x00\x00").status == "ignored"            # DLC < 4
    assert r.feed(1, b"\x07\x00\x00\x00\x00\x00\x00\x00").status == "ignored"    # Len < 8
    assert r.feed(1, (257).to_bytes(2, "little") + bytes(6)).status == "ignored"   # Len > 256
    assert len(r) == 0


def test_reassembly_ueberlauf():
    r = multiframe.Reassembler()
    first = (12).to_bytes(2, "little") + bytes(6)
    assert r.feed(1, first).status == "partial"
    assert r.feed(1, bytes(8)).status == "overflow"
    assert len(r) == 0


def test_reassembly_timeout_10s():
    r = multiframe.Reassembler()
    blk = multiframe.make_block(bytes(20))
    frames = multiframe.split_frames(blk)
    assert r.feed(1, frames[0], now=0.0).status == "partial"
    assert r.feed(1, frames[1], now=9.9).status == "partial"        # verlängert
    # 10 s nach dem letzten Rahmen verworfen -> der Rest wird als neuer Start gewertet
    assert r.feed(1, frames[2], now=19.95).status in ("ignored", "partial", "crc")
    r2 = multiframe.Reassembler()
    r2.feed(1, frames[0], now=0.0)
    assert r2.expire(now=10.0) == 1 and len(r2) == 0


def test_reassembly_getrennt_nach_id():
    r = multiframe.Reassembler()
    a, b = multiframe.make_block(b"A" * 10), multiframe.make_block(b"B" * 10)
    fa, fb = multiframe.split_frames(a), multiframe.split_frames(b)
    assert r.feed(1, fa[0]).status == "partial"
    assert r.feed(2, fb[0]).status == "partial"
    assert r.feed(2, fb[1]).block == b
    assert r.feed(1, fa[1]).block == a


# ===== Nachrichten =============================================================================
def test_anfrage_format():
    d = messages.build_anfrage(0x0141)
    assert d == b"\x41\x01\x00\xff\x00\x00\x00\x00"
    a = messages.Anfrage.parse(d)
    assert (a.nr, a.code) == (0x0141, 0xFF)


def test_parameter_setzen_format():
    d = messages.build_parameter_setzen(0x0042, VT.SHORT, vartyp.encode_raw(VT.SHORT, 725))
    assert d == b"\x42\x00\x01\xd5\x02\x00\x00\x00"


def test_identify_v1_roundtrip_und_len():
    i = messages.IdentifyAntwort(0x1234, 2, 29, 0x0B02, 12345, 1, 0x20261103, "LambdaControl 2")
    blk = i.build()
    assert len(blk) == 24 + len("LambdaControl 2") + 1 + 2 == int.from_bytes(blk[:2], "little")
    p = messages.IdentifyAntwort.parse(blk)
    assert p == i and p.version_text == "2.29"


def test_identify_v0():
    i = messages.IdentifyAntwort(1, 1, 5, 0x0901, 99, 0, None, "LC2 alt")
    p = messages.IdentifyAntwort.parse(i.build())
    assert p.name == "LC2 alt" and p.datum is None and p.regler_typ == 0x0901


def test_detail_param_roundtrip_mit_fuellbytes():
    d = messages.DetailAntwort(0x0042, 0, 0, 1, 1, 600, 1200, 800, VT.SHORT, 0,
                               vartyp.encode_raw(VT.SHORT, 800, 8), "5.12: Kessel Soll", "Tipp O₂")
    blk = d.build(parameter=True)
    assert len(blk) == 33 + 2 + 1 + len(d.name) + 1 + len("Tipp O2") + 1
    assert messages.DetailAntwort.parse(blk) == d
    blk_m = d.build(parameter=False)
    assert len(blk_m) == 33 + 2 + len(d.name) + len("Tipp O2") + 1
    assert messages.DetailAntwort.parse(blk_m).tip == "Tipp O₂"


def test_detail_offsets():
    d = messages.DetailAntwort(0xABCD, 0, 8, 0, 2, -5, 7, 3, VT.SSHORT, 0x10, bytes(range(8)), "N", "")
    blk = d.build()
    assert struct.unpack_from("<HH", blk) == (len(blk), 0xABCD)
    assert blk[5] == 8 and blk[7] == 2 and struct.unpack_from("<iii", blk, 8) == (-5, 7, 3)
    assert blk[20] == VT.SSHORT and blk[21] == 0x10 and blk[24:32] == bytes(range(8)) and blk[32:33] == b"N"


def test_einheiten_und_zeichen():
    assert messages.unit_text(1) == "°C" and messages.unit_text(18) == "Ω" and messages.unit_text(26) == "W"
    assert messages.decode_text(b"O\x81 Soll") == "O₂ Soll"
    assert messages.encode_text("Lambda (λ)") == b"Lambda (\x84)"
    assert messages.decode_text("Rücklauf".encode("cp1252")) == "Rücklauf"


# ===== VarTyp ==================================================================================
def test_size_of_wie_get_sizeoftyp():
    exp = {VT.CHAR: 1, VT.SCHAR: 1, VT.SHORT: 2, VT.SSHORT: 2, VT.SOMMER: 2, VT.LONG: 4, VT.SLONG: 4,
           VT.FLOAT: 4, VT.FUEHLER: 4, VT.DOUBLE: 4, VT.BINARY: 4, VT.URLAUB: 4, VT.ZEIT: 3, VT.DATUM: 5,
           VT.ZYKLEN: 8, VT.STRING: 1}
    for vt, n in exp.items():
        assert vartyp.size_of(vt) == n, vt


def test_bus_typ():
    assert vartyp.bus_typ(VT.FUEHLER) == VT.FLOAT and vartyp.bus_typ(VT.SHORT) == VT.SHORT


CASES = [
    (VT.CHAR, 0, b"\x00"), (VT.CHAR, 255, b"\xff"),
    (VT.SCHAR, -128, b"\x80"), (VT.SCHAR, -1, b"\xff"), (VT.SCHAR, 127, b"\x7f"),
    (VT.SHORT, 65535, b"\xff\xff"), (VT.SOMMER, 0x1234, b"\x34\x12"),
    (VT.SSHORT, -300, struct.pack("<h", -300)), (VT.SSHORT, -32768, b"\x00\x80"),
    (VT.LONG, 0xFFFFFFFF, b"\xff\xff\xff\xff"), (VT.SLONG, -2, struct.pack("<i", -2)),
    (VT.FLOAT, 72.5, struct.pack("<f", 72.5)), (VT.FUEHLER, -12.25, struct.pack("<f", -12.25)),
]


@pytest.mark.parametrize("vt,raw,data", CASES)
def test_encode_decode(vt, raw, data):
    enc = vartyp.encode_raw(vt, raw)
    assert len(enc) == 5 and enc.startswith(data) and not any(enc[len(data):])
    assert vartyp.decode_raw(vt, enc) == raw


def test_alle_numerischen_typen_abgedeckt():
    assert {c[0] for c in CASES} >= set(NUMERIC_VARTYPES)


@pytest.mark.parametrize("vt,raw", [(VT.CHAR, 256), (VT.CHAR, -1), (VT.SCHAR, 128), (VT.SSHORT, 32768),
                                    (VT.SHORT, -1), (VT.LONG, 1 << 32), (VT.SLONG, -(1 << 31) - 1)])
def test_encode_bereich(vt, raw):
    with pytest.raises(ValueError):
        vartyp.encode_raw(vt, raw)


def test_encode_nicht_numerisch_und_float_ungueltig():
    for vt in (VT.DATUM, VT.ZEIT, VT.ZYKLEN, VT.STRING):
        with pytest.raises(ValueError):
            vartyp.encode_raw(vt, 1)
    for v in (math.nan, math.inf, 1e39):
        with pytest.raises(ValueError):
            vartyp.encode_raw(VT.FLOAT, v)


def test_float32_bereinigt():
    assert vartyp.decode_raw(VT.FLOAT, struct.pack("<f", 72.4)) == 72.4


def test_skalierung():
    assert vartyp.to_value(725, VT.SHORT, 1) == 72.5
    assert vartyp.to_value(-123, VT.SSHORT, 2) == -1.23
    assert vartyp.to_value(72.5, VT.FLOAT, 1) == 72.5          # FLOAT nicht skaliert
    assert vartyp.to_value(5400, VT.LONG, 1, safety.OUT_SECTOH) == 1.5
    assert vartyp.to_value(90, VT.LONG, 0, safety.OUT_SECTOMIN) == 1.5
    assert vartyp.to_value(61, VT.SHORT, 0, safety.OUT_CNTSECMIN) == 2.0      # (61+59)/60
    assert vartyp.to_value(3, VT.CHAR, 0, safety.OUT_TEXTAUSWAHL) == 3.0


def test_grenzen():
    assert vartyp.limit_to_value(600, VT.SHORT, 1) == 60.0
    assert vartyp.limit_to_value(150, VT.FLOAT, 1) == 150.0
    assert vartyp.limit_to_value(-1, VT.LONG, 0) == float(0xFFFFFFFF)   # uint32 bei unsigned
    assert vartyp.limit_to_value(-12, VT.SCHAR, 0) == -12.0


def test_value_to_raw_rundung():
    assert vartyp.value_to_raw(72.45, VT.SHORT, 1) == 725        # halbe Stelle weg von 0
    assert vartyp.value_to_raw(72.44, VT.SHORT, 1) == 724
    assert vartyp.value_to_raw(-1.25, VT.SSHORT, 1) == -13
    assert vartyp.value_to_raw(0.1 + 0.2, VT.SHORT, 1) == 3
    assert vartyp.value_to_raw(7.5, VT.FLOAT, 1) == 7.5
    with pytest.raises(ValueError):
        vartyp.value_to_raw(6553.6, VT.SHORT, 1)
    with pytest.raises(ValueError):
        vartyp.value_to_raw(-1, VT.CHAR, 0)
    with pytest.raises(ValueError):
        vartyp.value_to_raw(math.nan, VT.SHORT, 0)
    with pytest.raises(ValueError):
        vartyp.value_to_raw(1.0, VT.LONG, 0, safety.OUT_SECTOH)
