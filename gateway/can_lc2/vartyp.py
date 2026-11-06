"""VarTyp-Kodierung des Feldes ``Wert[5]`` / ``Wert[8]`` und Skalierung.

Firmware-Grundlagen (Branch LambdaControl_2_1.29):
- Größen: ``get_sizeofTyp`` (os2/net/Messwerte.cpp:643-681). Signed-Bit 0x80 und Busy-Bit 0x40
  werden für die Größe ausgeblendet. AVR: ``double`` = ``float`` (4 Byte).
- Bus-Typ: ``eMesswerte::GetTyp`` (Messwerte.cpp:604-618) macht aus VT_FUEHLER/VT_DOUBLE ein
  VT_FLOAT (0x20- und 0x30-Antworten). 0x38 (Parameter-Detail) sendet den Tabellentyp unverändert
  (BusOut.cpp:358), und 0x08/0x00 (Parameter setzen) verlangt genau diesen Typ (BusOut.cpp:913).
- Byte-Reihenfolge: Little-Endian (AVR, ``memcpy`` der Variable).
- Skalierung (Kommentar Messwert Tabelle.cpp:1432-1435, ``GetAnzeigeWert`` Messwerte.cpp:173-239):
  Integer-Typen: Anzeigewert = Rohwert / 10^Nachkomma, Min/Max/Werk ebenfalls in Rohwert-Einheiten.
  VT_FLOAT/VT_FUEHLER: Wert ist der echte Wert, Min/Max/Werk sind der echte Wert (keine Skalierung).
  Ausgabetypen OUT_SECTOH/OUT_SECTOMIN/OUT_CNTSECMIN rechnen Sekunden in Stunden/Minuten um.
- Min/Max im Detail sind ``int32``; bei vorzeichenlosen Typen vergleicht ``CheckParameter``
  vorzeichenlos (Messwerte.cpp:406-428) -> hier ebenfalls als ``uint32`` gelesen.
"""

from __future__ import annotations

import math
import struct
from decimal import ROUND_HALF_UP, Decimal

from gateway.config import NUMERIC_VARTYPES, VarTyp
from gateway.safety import (
    OUT_CNTSECMIN, OUT_DATE, OUT_IPV4, OUT_MASK, OUT_SECTOH, OUT_SECTOMIN, OUT_SECTOTIME, OUT_TIME,
    OUT_URLAUBBIS, OUT_URLAUBVON, OUT_ZYKLEN, OUT_ZYKLENZEITEN,
)

VT_SIGNED = 0x80
VT_BUSY = 0x40

_FLOAT_TYPES = frozenset({VarTyp.FLOAT, VarTyp.FUEHLER, VarTyp.DOUBLE})

#: Wertebereich der Integer-Typen (Rohwert)
INT_RANGE: dict[int, tuple[int, int]] = {
    VarTyp.CHAR: (0, 0xFF),
    VarTyp.SCHAR: (-0x80, 0x7F),
    VarTyp.SHORT: (0, 0xFFFF),
    VarTyp.SOMMER: (0, 0xFFFF),
    VarTyp.SSHORT: (-0x8000, 0x7FFF),
    VarTyp.LONG: (0, 0xFFFFFFFF),
    VarTyp.BINARY: (0, 0xFFFFFFFF),
    VarTyp.SLONG: (-0x80000000, 0x7FFFFFFF),
}
_INT_FMT: dict[int, str] = {
    VarTyp.CHAR: "<B", VarTyp.SCHAR: "<b", VarTyp.SHORT: "<H", VarTyp.SOMMER: "<H",
    VarTyp.SSHORT: "<h", VarTyp.LONG: "<I", VarTyp.BINARY: "<I", VarTyp.SLONG: "<i",
}

#: Ausgabetypen, deren Wert nicht als Zahl bereitgestellt wird
NON_NUMERIC_OUT = frozenset({OUT_DATE, OUT_TIME, OUT_URLAUBVON, OUT_URLAUBBIS, OUT_ZYKLEN,
                             OUT_ZYKLENZEITEN, OUT_IPV4})

VARTYP_NAMEN = {int(v): v.name for v in VarTyp}


def vartyp_name(vt: int) -> str:
    return VARTYP_NAMEN.get(vt, f"{vt:#04x}")


def size_of(vt: int) -> int:
    """Byte-Größe wie ``get_sizeofTyp`` (Messwerte.cpp:643-681, AVR: double = 4 Byte)."""
    base = vt & ~(VT_SIGNED | VT_BUSY) & 0xFF
    # Ergebnis des switch mit Durchfallen: ZYKLEN 1+3+1+1+1+1, DATUM 1+1+1+1+1, …; default 1 (CHAR)
    return _SIZES.get(base, 1)


_SIZES: dict[int, int] = {
    VarTyp.DOUBLE: 4,
    VarTyp.ZYKLEN: 8,
    VarTyp.DATUM: 5,
    VarTyp.BINARY: 4, VarTyp.LONG: 4, VarTyp.FUEHLER: 4, VarTyp.FLOAT: 4, VarTyp.URLAUB: 4,
    VarTyp.ZEIT: 3,
    VarTyp.SOMMER: 2, VarTyp.SHORT: 2,
}


def bus_typ(vt: int) -> int:
    """``eMesswerte::GetTyp``: VT_FUEHLER/VT_DOUBLE werden bei 0x20/0x30 als VT_FLOAT gesendet."""
    return VarTyp.FLOAT if vt in (VarTyp.FUEHLER, VarTyp.DOUBLE) else vt


def is_float(vt: int) -> bool:
    return vt in _FLOAT_TYPES


def is_numeric(vt: int) -> bool:
    return vt in NUMERIC_VARTYPES


def is_unsigned_int(vt: int) -> bool:
    return vt in INT_RANGE and INT_RANGE[vt][0] == 0


def same_vartyp(a: int, b: int) -> bool:
    """Gleicher Typ im Sinne der Busübertragung (FUEHLER ≙ FLOAT)."""
    return bus_typ(a) == bus_typ(b)


# --- Rohwert <-> Bytes ------------------------------------------------------------------------
def f32_clean(x: float) -> float:
    """float32 -> kürzeste Dezimaldarstellung, die denselben float32 ergibt (72.4 statt 72.40000153)."""
    if not math.isfinite(x):
        return x
    target = struct.pack("<f", x)
    for prec in range(1, 10):
        cand = float(f"{x:.{prec}g}")
        if struct.pack("<f", cand) == target:
            return cand
    return x


def decode_raw(vt: int, wert: bytes) -> int | float:
    """Bytes (LE) -> Rohwert. ``ValueError`` bei nicht numerischem Typ oder zu kurzem Feld.
    FLOAT wird auf die kürzeste float32-genaue Dezimalzahl gebracht (``f32_clean``)."""
    if is_float(vt):
        if len(wert) < 4:
            raise ValueError("Wert-Feld zu kurz für FLOAT")
        return f32_clean(struct.unpack_from("<f", wert)[0])
    fmt = _INT_FMT.get(vt)
    if fmt is None:
        raise ValueError(f"VarTyp {vartyp_name(vt)} ist nicht numerisch")
    if len(wert) < struct.calcsize(fmt):
        raise ValueError(f"Wert-Feld zu kurz für {vartyp_name(vt)}")
    return struct.unpack_from(fmt, wert)[0]


def encode_raw(vt: int, raw: int | float, length: int = 5) -> bytes:
    """Rohwert -> ``Wert[length]`` (LE, mit Nullen aufgefüllt). Prüft den Wertebereich."""
    if is_float(vt):
        f = float(raw)
        if not math.isfinite(f):
            raise ValueError("FLOAT-Wert ist NaN/unendlich")
        try:
            data = struct.pack("<f", f)
        except OverflowError as exc:
            raise ValueError(f"FLOAT-Wert {f} nicht als float32 darstellbar") from exc
        if not math.isfinite(struct.unpack("<f", data)[0]):
            raise ValueError(f"FLOAT-Wert {f} nicht als float32 darstellbar")
    else:
        fmt = _INT_FMT.get(vt)
        if fmt is None:
            raise ValueError(f"VarTyp {vartyp_name(vt)} ist nicht numerisch")
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValueError(f"Rohwert {raw!r} ist keine ganze Zahl")
        lo, hi = INT_RANGE[vt]
        if not lo <= raw <= hi:
            raise ValueError(f"Rohwert {raw} außerhalb {lo}..{hi} ({vartyp_name(vt)})")
        data = struct.pack(fmt, raw)
    if len(data) > length:
        raise ValueError("Wert-Feld zu klein")
    return data.ljust(length, b"\0")


def limit_raw(vt: int, limit: int) -> int:
    """int32 aus dem Detail (Min/Max/Werk) typgerecht deuten (unsigned -> uint32)."""
    return limit & 0xFFFFFFFF if is_unsigned_int(vt) else limit


# --- Skalierung -------------------------------------------------------------------------------
def _base_out(out_type: int | None) -> int | None:
    return None if out_type is None else out_type & OUT_MASK


def effective_decimals(decimals: int, out_type: int | None) -> int:
    """Nachkommastellen des bereitgestellten Werts (OUT_SECTOTIME: Sekunden ohne Nachkomma)."""
    return 0 if _base_out(out_type) == OUT_SECTOTIME else decimals


def to_value(raw: int | float, vt: int, decimals: int, out_type: int | None = None) -> float:
    """Rohwert -> bereitgestellter Wert (wie die Anzeige der Firmware, ``GetAnzeigeWert``)."""
    o = _base_out(out_type)
    if o == OUT_SECTOH:
        return float(raw) / 3600.0
    if o == OUT_SECTOMIN:
        return float(raw) / 60.0
    if o == OUT_CNTSECMIN:
        # var += 59; Anzeige = (var / 60) * 10^nk, abgeschnitten (int32)
        mul = 10 ** decimals
        return math.trunc((float(raw) + 59.0) / 60.0 * mul) / mul
    if o == OUT_SECTOTIME:
        return float(raw)       # TODO(Feld): Anzeigeformat „Fr 15:30“ – hier Sekunden
    if is_float(vt):
        return float(raw)
    return float(raw) / 10 ** decimals


def limit_to_value(limit: int, vt: int, decimals: int) -> float:
    """Min/Max/Werk aus Detail -> Werteinheit (FLOAT: unskaliert, Integer: / 10^Nachkomma)."""
    lim = limit_raw(vt, limit)
    if is_float(vt):
        return float(lim)
    return lim / 10 ** decimals


def value_to_raw(value: float, vt: int, decimals: int, out_type: int | None = None) -> int | float:
    """Wert -> Rohwert für ``Parameter setzen``. Rundung kaufmännisch (halbe Stelle weg von 0).

    ``ValueError``, wenn der Ausgabetyp eine Umrechnung braucht, die nicht verifiziert ist, oder
    der Rohwert nicht in den VarTyp passt.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Wert {value!r} ist keine endliche Zahl")
    o = _base_out(out_type)
    if o in (OUT_SECTOH, OUT_SECTOMIN, OUT_CNTSECMIN, OUT_SECTOTIME):
        raise ValueError("Ausgabetyp mit Zeit-Umrechnung ist nicht schreibbar")
    if is_float(vt):
        f = float(value)
        encode_raw(vt, f)      # Bereich float32 prüfen
        return f
    if vt not in INT_RANGE:
        raise ValueError(f"VarTyp {vartyp_name(vt)} ist nicht numerisch")
    scaled = (Decimal(repr(float(value))) * (Decimal(10) ** decimals))
    raw = int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    lo, hi = INT_RANGE[vt]
    if not lo <= raw <= hi:
        raise ValueError(f"Rohwert {raw} außerhalb {lo}..{hi} ({vartyp_name(vt)})")
    return raw
