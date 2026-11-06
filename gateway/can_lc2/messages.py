"""Nachrichtenstrukturen des LC2-Busprotokolls (Include/Common/BusProtokoll.h, ``#pragma pack 1``).

Alle Felder Little-Endian (AVR). Multiframe-Blöcke enden mit CRC16 (Big-Endian, siehe multiframe).

- ``ANFRAGE_MESSWERT`` {Nummer u16, Sprache u8, Code u8, frei[4]} – Anfrage 0x20/0x30/0x38 (DLC 8).
- ``ANTWORT_MESSWERT`` {Messwert u16, VarTyp u8, Wert[5]} – Antwort 0x20 (Einzelrahmen, DLC 8).
- ``ANTWORT_IDENTIFIER`` (Version 1, 24 Byte) bzw. ``ANTWORT_IDENTIFIER_V0`` (14 Byte), danach
  Name (nullterminiert), CRC – Antwort 0x00 (``Bus_WriteIdentify``, BusOut.cpp:39-73;
  Auswertung ``eBusIdent::InsertIdentify``, BusIn.cpp:355-383: ``Version != 0`` -> V1).
- ``ANTWORT_MESSWERTDETAIL`` (32 Byte + Name\\0 + Tip\\0 [+ Füllbytes] + CRC) – Antwort 0x30/0x38
  (``Bus_WriteMesswertDetail`` BusOut.cpp:77-160, ``Bus_WriteParameterDetail`` BusOut.cpp:296-402).
- ``BUS_PARAMETERSETZEN`` {Nummer u16, VarTyp u8, Wert[5]} – Befehl 0x08/0x00 (DLC 8).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from gateway.can_lc2 import multiframe
from gateway.safety import ANFRAGE_CODE

# --- Einheiten (EINHEIT-Enum MesswertNummern.h:8-37, Texte Messwerte.cpp:30-87) ----------------
EINHEITEN: tuple[str, ...] = (
    "",        # EH_NONE
    "°C", "K", "%", "kW", "kWh", "MWh", "s", "min", "h", "l", "m³", "l/min", "U/min", "m³/h",
    "mV", "Pa", "Imp/l", "Ω", "kg", "ppm", "mg/m³", "mg/MJ",
    "",        # EH_FREI ("Faktor")
    "V", "A", "W",
)


def unit_text(index: int) -> str:
    """Einheiten-Index aus dem Detail -> Text (unbekannt -> ``"?<n>"``)."""
    if 0 <= index < len(EINHEITEN):
        return EINHEITEN[index]
    return f"?{index}"


# --- Zeichensatz -------------------------------------------------------------------------------
#: Sonderzeichen des Display-Fonts in den String-Tabellen (z. B. "O\\x81" = O₂, "\\x85" = Ohm in
#: Messwerte.cpp:47, "Lambda (\\x84)"). Übrige Bytes: cp1252.
#: TODO(Feld): weitere Font-Sonderzeichen (0xA2/0xA3 = Tastensymbole) bei Bedarf ergänzen.
_SPECIAL = {0x81: "₂", 0x84: "λ", 0x85: "Ω", 0xA2: "▲", 0xA3: "▼"}
_SPECIAL_REV = {v: k for k, v in _SPECIAL.items()}


def decode_text(raw: bytes) -> str:
    out = []
    for b in raw:
        if b in _SPECIAL:
            out.append(_SPECIAL[b])
        else:
            try:
                out.append(bytes((b,)).decode("cp1252"))
            except UnicodeDecodeError:
                out.append("?")
    return "".join(out)


def encode_text(text: str) -> bytes:
    out = bytearray()
    for ch in text:
        if ch in _SPECIAL_REV:
            out.append(_SPECIAL_REV[ch])
        else:
            out += ch.encode("cp1252", errors="replace")
    return bytes(out)


def _cstr(buf: bytes, pos: int) -> tuple[str, int]:
    end = buf.find(b"\0", pos)
    if end < 0:
        end = len(buf)
    return decode_text(buf[pos:end]), end + 1


# --- Anfragen ----------------------------------------------------------------------------------
def build_anfrage(nr: int, sprache: int = 0, code: int = ANFRAGE_CODE) -> bytes:
    """``ANFRAGE_MESSWERT`` (DLC 8). Code 0xFF wie ``SendMesswert`` (BusOut.cpp:171)."""
    return struct.pack("<HBB4x", nr, sprache, code)


def build_parameter_setzen(nr: int, vartyp: int, wert: bytes) -> bytes:
    """``BUS_PARAMETERSETZEN`` (DLC 8): Wert[5], ungenutzte Bytes 0 (``memset`` in SetParameter)."""
    if len(wert) != 5:
        raise ValueError("Wert muss 5 Byte lang sein")
    return struct.pack("<HB", nr, vartyp) + bytes(wert)


@dataclass(frozen=True)
class Anfrage:
    nr: int
    sprache: int
    code: int
    frei: bytes

    @classmethod
    def parse(cls, data: bytes) -> Anfrage:
        if len(data) < 8:
            raise ValueError(f"Anfrage: DLC {len(data)} statt 8")
        nr, sprache, code = struct.unpack_from("<HBB", data)
        return cls(nr, sprache, code, bytes(data[4:8]))


@dataclass(frozen=True)
class ParameterSetzen:
    nr: int
    vartyp: int
    wert: bytes

    @classmethod
    def parse(cls, data: bytes) -> ParameterSetzen:
        if len(data) != 8:
            raise ValueError(f"Parameter setzen: DLC {len(data)} statt 8")
        nr, vt = struct.unpack_from("<HB", data)
        return cls(nr, vt, bytes(data[3:8]))


# --- Antworten ---------------------------------------------------------------------------------
@dataclass(frozen=True)
class MesswertAntwort:
    nr: int
    vartyp: int
    wert: bytes      # 5 Byte

    @classmethod
    def parse(cls, data: bytes) -> MesswertAntwort:
        if len(data) != 8:
            raise ValueError(f"Messwert-Antwort: DLC {len(data)} statt 8")
        nr, vt = struct.unpack_from("<HB", data)
        return cls(nr, vt, bytes(data[3:8]))

    def build(self) -> bytes:
        return struct.pack("<HB", self.nr, self.vartyp) + bytes(self.wert).ljust(5, b"\0")[:5]


IDENT_V1_SIZE = 24      # sizeof(ANTWORT_IDENTIFIER)
IDENT_V0_SIZE = 14      # sizeof(ANTWORT_IDENTIFIER_V0)


@dataclass(frozen=True)
class IdentifyAntwort:
    prg_crc: int
    major: int
    minor: int
    regler_typ: int
    seriennummer: int
    version: int          # Strukturversion (0 = alt, 1 = mit Datum)
    datum: int | None
    name: str

    @property
    def version_text(self) -> str:
        # Anzeige M_REGV = Major*100+Minor mit 2 Nachkommastellen (Messwerte.cpp:697-699)
        return f"{self.major}.{self.minor:02d}"

    @classmethod
    def parse(cls, block: bytes) -> IdentifyAntwort:
        if len(block) < IDENT_V0_SIZE + 2:
            raise ValueError("Identify-Antwort zu kurz")
        length, prg_crc, major, minor, rt, serial, version = struct.unpack_from("<HHBBHIB", block)
        payload = block[:min(length, len(block)) - 2]
        if version:
            if len(payload) < IDENT_V1_SIZE:
                raise ValueError("Identify-Antwort (V1) zu kurz")
            datum = struct.unpack_from("<I", payload, 16)[0]
            name, _ = _cstr(payload, IDENT_V1_SIZE)
        else:
            datum = None
            name, _ = _cstr(payload, IDENT_V0_SIZE)
        return cls(prg_crc, major, minor, rt, serial, version, datum, name)

    def build(self) -> bytes:
        """Wie ``Bus_WriteIdentify``: Len = sizeof + strlen + 1 + 2."""
        name = encode_text(self.name)
        if self.version:
            head = struct.pack("<HBBHIB3xI4x", self.prg_crc, self.major, self.minor, self.regler_typ,
                               self.seriennummer, self.version, self.datum or 0)
        else:
            head = struct.pack("<HBBHIBx", self.prg_crc, self.major, self.minor, self.regler_typ,
                               self.seriennummer, 0)
        return multiframe.make_block(head + name + b"\0")


AUSWAHL_HEAD_SIZE = 8   # ANTWORT_AUSWAHLTEXTE ohne texte[1]: Len, Messwert, Sprache, Offset, Anzahl


@dataclass(frozen=True)
class AuswahltexteAntwort:
    """Antwort 0x08 (``Bus_WriteAuswahltexte``, os2/net/BusOut.cpp:676-770).

    Ein Block enthält bis zu 10 Texte (nullterminiert); Text ``j`` gehört zum Wert ``offset + j``.
    Längere Listen kommen als mehrere Blöcke mit fortlaufendem ``offset``."""
    nr: int
    sprache: int
    offset: int
    texte: tuple[str, ...]

    @classmethod
    def parse(cls, block: bytes) -> AuswahltexteAntwort:
        if len(block) < AUSWAHL_HEAD_SIZE + 2:
            raise ValueError("Auswahltexte-Antwort zu kurz")
        _len, nr, sprache, offset, anzahl = struct.unpack_from("<HHBHB", block)
        payload = block[AUSWAHL_HEAD_SIZE:len(block) - 2]
        texte, pos = [], 0
        for _ in range(anzahl):
            if pos >= len(payload):
                raise ValueError(f"Auswahltexte: {anzahl} Texte angekündigt, weniger enthalten")
            s, pos = _cstr(payload, pos)
            texte.append(s)
        return cls(nr, sprache, offset, tuple(texte))

    def build(self) -> bytes:
        """Wie die Firmware: Kopf + Texte (je mit 0) + 1 Füllbyte (texte[1]) + CRC."""
        body = struct.pack("<HBHB", self.nr, self.sprache, self.offset, len(self.texte))
        body += b"".join(encode_text(t) + b"\0" for t in self.texte) + b"\0"
        return multiframe.make_block(body)


def build_auswahltexte(nr: int, lo: int, texte: list[str]) -> list[bytes]:
    """Aufteilung auf Blöcke wie ``Bus_WriteAuswahltexte``: neuer Block, wenn > 30 Byte oder 10
    Texte erreicht sind (Simulator)."""
    blocks, chunk, start, size = [], [], lo, 11
    for i, t in enumerate(texte):
        chunk.append(t)
        size += len(encode_text(t)) + 1
        if i == len(texte) - 1 or size > 30 or len(chunk) >= 10:
            blocks.append(AuswahltexteAntwort(nr, 0, start, tuple(chunk)).build())
            start, chunk, size = lo + i + 1, [], 11
    return blocks


DETAIL_HEAD_SIZE = 32   # sizeof(ANTWORT_MESSWERTDETAIL) - sizeof(Name[1])


@dataclass(frozen=True)
class DetailAntwort:
    """Antwort 0x30 (Messwert-Detail) bzw. 0x38 (Parameter-Detail / Schreibbestätigung)."""
    nr: int
    sprache: int
    ausgabe: int          # outFlag & OUT_MASK
    einheit: int          # Einheiten-Index (bei Textauswahl 0)
    nachkomma: int
    min: int              # int32 roh
    max: int
    werk: int
    vartyp: int
    code: int
    wert: bytes           # 8 Byte
    name: str
    tip: str

    @property
    def unit(self) -> str:
        return unit_text(self.einheit)

    @classmethod
    def parse(cls, block: bytes) -> DetailAntwort:
        if len(block) < DETAIL_HEAD_SIZE + 2:
            raise ValueError("Detail-Antwort zu kurz")
        (_len, nr, sprache, ausgabe, einheit, nk, mn, mx, werk, vt, code) = \
            struct.unpack_from("<HHBBBBiiiBB", block)
        wert = bytes(block[24:32])
        payload = block[:len(block) - 2]
        name, pos = _cstr(payload, DETAIL_HEAD_SIZE)
        tip, _ = _cstr(payload, pos) if pos < len(payload) else ("", pos)
        return cls(nr, sprache, ausgabe, einheit, nk, mn, mx, werk, vt, code, wert, name, tip)

    def build(self, parameter: bool = True) -> bytes:
        """Wie die Firmware: Messwert-Detail ``size = 33 + 2 + n + t + 1`` (BusOut.cpp:109),
        Parameter-Detail ``size = 33 + 2 + 1 + n + 1 + t + 1`` (BusOut.cpp:333-342) – letzteres
        enthält 2 Füllbytes (0) vor der CRC."""
        name, tip = encode_text(self.name), encode_text(self.tip)
        head = struct.pack("<HBBBBiiiBB2x", self.nr, self.sprache, self.ausgabe, self.einheit,
                           self.nachkomma, self.min, self.max, self.werk, self.vartyp, self.code)
        body = head + bytes(self.wert).ljust(8, b"\0")[:8] + name + b"\0" + tip + b"\0"
        if parameter:
            body += b"\0\0"
        return multiframe.make_block(body)
