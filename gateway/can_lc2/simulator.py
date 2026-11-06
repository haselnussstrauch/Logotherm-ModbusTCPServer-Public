"""CAN-Simulator: antwortet wie die Logotherm-Firmware (Branch LambdaControl_2_1.29).

Nachgebildet (os2/net/BusIn.cpp, BusOut.cpp, Messwerte.cpp):
- Identify 0x0B/0x00 -> 0x0A/0x00 (Multiframe, ``Bus_WriteIdentify``).
- Messwert 0x0B/0x20 -> 0x0A/0x20 (Einzelrahmen, VarTyp über ``GetTyp``); nur Messwerte-Tabelle.
- Messwert-Detail 0x0B/0x30, Parameter-Detail 0x0B/0x38 (einzeln und Nr=0xFFFF als Folge von
  Multiframe-Blöcken; 0x38 lässt bei 0xFFFF aus, was ``ParameterSendenOkay`` verbietet).
- Parameter setzen 0x08/0x00: VarTyp muss exakt stimmen (sonst stillschweigend verworfen),
  ``CheckParameter`` (außerhalb Min/Max -> Werkseinstellung), Bestätigung 0x0A/0x38 an den Absender.
- Unbekannte Nummer -> keine Antwort. RS_POWERUP -> 0x20/0x30 werden ignoriert.
- Unaufgeforderte 0x0A/0x20 an 0xFF (``SendMesswert``), optional Echo der empfangenen Rahmen
  (wie InnoMaker/gs_usb) und ein "Hausbesetzer", der mit einer fremden Adresse sendet.

Die Profile enthalten eine Auswahl echter Einträge aus ``App/<Gerät>/Messwert Tabelle.cpp``
(Typ, Min/Max, Werk, Nachkomma, Ausgabetyp, Einheit). Texte aus den String-Tabellen
(``App/*/String Table.c[pp]``, für LC2 Neu die gleichnamigen IDS aus ``App/LambdaControl 2``),
sonst der Enum-Name.

Start: ``python -m gateway.can_lc2.simulator --channel sim0`` (virtual-Bus nur im selben Prozess;
prozessübergreifend z. B. ``--interface socketcan --channel vcan0`` oder ``udp_multicast``).
Der Simulator ist Testinfrastruktur und sendet selbst Antworten (Kanal 0x0A) – er nutzt bewusst
NICHT den Sendepfad des Gateways.
"""

from __future__ import annotations

import argparse
import copy
import logging
import struct
import threading
import time
from dataclasses import dataclass, field

import can

from gateway.can_lc2 import idcodec, messages, multiframe, vartyp
from gateway.config import VarTyp as VT
from gateway.devices.fw_numbers import NUMBERS as N
from gateway.safety import (
    OUT_DECIMAL, OUT_HEXADECIMAL, OUT_IPV4, OUT_MINTOTIME, OUT_ONOFF, OUT_RAMSTRING,
    OUT_SECTOH, OUT_SERVICECODE, OUT_TEXTAUSWAHL, OUT_YESNO, OUT_ZYKLEN, OUT_ZYKLENZEITEN, OUT_CNTSECMIN,
)

log = logging.getLogger(__name__)

SC_NONE, SC_FACH, SC_HERS, SC_WERK, SC_INTE = 0x00, 0x10, 0x20, 0x40, 0x80   # GUI/Menu.h:47-51
# Einheiten-Indizes (EINHEIT-Enum)
EH = {name: i for i, name in enumerate(
    "NONE CELSIUS KELVIN PERCENT KW KWH MWH SEKUNDEN MINUTEN STUNDEN LITER M3 LITER_MIN UPM M3_H MV "
    "PASCAL IMPL OHM KG PPM MG_M3 MG_MJ FREI V A W".split())}


@dataclass
class SimEntry:
    nr: int
    name: str
    vartyp: int
    min: int
    max: int
    werk: int = 0
    nachkomma: int = 0
    out: int = OUT_DECIMAL         # vollständiger outFlag (inkl. OUT_RAM/OUT_FLASHPTR)
    einheit: int = 0
    tip: str = ""
    code: int = SC_NONE
    kat: int = 0                   # Anzeige-Kategorie/-Nummer (GetParameterNummer)
    pnr: int = 0
    value: int | float = 0         # aktueller Rohwert (bzw. 8 Byte für ZYKLEN)


@dataclass
class SimProfile:
    key: str
    name: str
    regler_typ: int
    address: int
    serial: int
    major: int = 2
    minor: int = 29
    ident_version: int = 1
    values: list[SimEntry] = field(default_factory=list)
    params: list[SimEntry] = field(default_factory=list)
    broadcast: list[int] = field(default_factory=list)   # Nummern, die unaufgefordert gesendet werden

    def entry(self, nr: int) -> SimEntry | None:
        return next((e for e in [*self.values, *self.params] if e.nr == nr), None)


def _e(name: str, *a, **kw) -> SimEntry:
    return SimEntry(N[name] if isinstance(name, str) else name, *a, **kw)


def _ag(agg: int, name: str) -> int:
    return N[name] | agg << 8


# ---------------------------------------------------------------------------------------------
# Profile (Quelle jeweils App/<Gerät>/Messwert Tabelle.cpp, Zeilennummern Branch LambdaControl_2_1.29)
# ---------------------------------------------------------------------------------------------
def profile_lc2() -> SimProfile:
    """LambdaControl 2 Neu, Reglertyp 0x0B00 (Werk), Adresse 1. Tabelle Z. 1520-2196."""
    return SimProfile(
        key="lc2", name="LambdaControl 2", regler_typ=0x0B00, address=0x01, serial=0x00012345,
        values=[
            _e("M_KSTATUS", "Kessel", VT.CHAR, 0, 10, out=OUT_TEXTAUSWAHL, value=3),
            _e("M_TKS", "Kessel Soll", VT.FLOAT, 1, 150, nachkomma=1, einheit=EH["CELSIUS"], value=80.0),
            _e("M_TKI", "Kesseltemp.", VT.FLOAT, -50, 150, nachkomma=1, einheit=EH["CELSIUS"], value=72.4),
            _e("M_TKRI", "Rücklauftemp.", VT.FLOAT, -50, 150, nachkomma=1, einheit=EH["CELSIUS"], value=61.2),
            _e("M_RV", "Rücklaufventil", VT.FLOAT, 0, 110, nachkomma=1, einheit=EH["PERCENT"], value=35.5),
            _e("M_KESSELT", "Kessel t.", VT.LONG, 0, 9999999, nachkomma=1, out=OUT_SECTOH,
               einheit=EH["STUNDEN"], tip="h total", value=12_345 * 3600 + 1800),
            _e("M_TAGI", "Abgastemp.", VT.FLOAT, -20, 500, nachkomma=1, einheit=EH["CELSIUS"], value=182.6),
            _e("M_TAGH", "Abgas max.", VT.SSHORT, -300, 7000, nachkomma=1, einheit=EH["CELSIUS"], value=2451),
            _e("M_O2I", "O₂", VT.FLOAT, 0, 21, nachkomma=1, einheit=EH["PERCENT"], value=7.25),
            _e("M_LAMBDA", "Lambda (λ)", VT.FLOAT, 0, 10, nachkomma=2, value=1.52),
            _e("M_UD", "IDS_UNTERDRUCK", VT.FUEHLER, -99, 999, nachkomma=1, einheit=EH["V"], code=SC_INTE,
               value=1.75),
            _e("M_STBCNT", "Taste ▲ drücken", VT.SHORT, 0, 99, out=OUT_CNTSECMIN, einheit=EH["MINUTEN"], value=125),
        ],
        params=[
            _e("P_TKS", "Kessel Soll", VT.SHORT, 600, 1200, 800, 1, einheit=EH["CELSIUS"], kat=5, pnr=12,
               value=800),
            _e("P_TAGMIN", "Abgas min.", VT.SHORT, 90, 300, 140, einheit=EH["CELSIUS"], kat=5, pnr=13, value=140),
            _e("P_RVLFZ", "Laufzeit Y1", VT.SHORT, 90, 240, 150, einheit=EH["SEKUNDEN"], kat=5, pnr=15, value=150),
            _e("P_LVLFZ", "Laufzeit Y2", VT.SHORT, 90, 240, 150, einheit=EH["SEKUNDEN"], kat=5, pnr=16, value=150),
            _e("P_KF_TA", "Fr. Außentemp.", VT.SCHAR, -20, 5, 0, einheit=EH["CELSIUS"], kat=5, pnr=18,
               tip="Fr. erweitert < Wert", value=-5),
            _e("P_TAAUSLEG", "TA-Auslegung", VT.SCHAR, -30, 10, -12, einheit=EH["CELSIUS"], kat=5, pnr=7,
               tip="Siehe Wärmebedarfsber.", value=-12),
            _e("P_NACHLEGAB", "Nachlegemenge ab", VT.CHAR, 0, 100, 50, einheit=EH["PERCENT"], kat=5, pnr=9,
               value=50),
            _e("P_ANZEIGE", "Anzeigebild", VT.CHAR, 0, 4, 4, out=OUT_TEXTAUSWAHL, kat=5, pnr=10, value=4),
            _e("P_ABGESVON", "HK abgesenkt von", VT.SHORT, 0, 1440, 22 * 60, out=OUT_MINTOTIME, kat=5, pnr=6,
               tip="Heizkreise abgesenkt", value=22 * 60),
            _e("P_KDELP", "Perioden löschen", VT.CHAR, 0, 1, 0, out=OUT_YESNO, value=0),
            _e("P_KZZ1", "IDS_ZUENDZEIT", VT.ZYKLEN, 0, 0, 0, out=OUT_ZYKLENZEITEN, value=bytes(8)),
            # ParameterSendenOkay = false -> nicht in der 0xFFFF-Aufzählung
            _e("P_NACHLEG_KG", "Nachlegemenge", VT.SHORT, 0, 9999, 0, einheit=EH["KG"], value=0),
            _e("P_BUSADR", "LC2-Busnummer", VT.CHAR, 1, 15, 1, kat=10, pnr=11, code=SC_FACH, value=1),
            _e("P_CODE", "Code", VT.SHORT, 0, 999, 0, out=OUT_SERVICECODE, value=0),
        ],
        broadcast=[N["M_TKI"]],
    )


def profile_mc2() -> SimProfile:
    """MultiControl 2, Reglertyp 0x0A01, Adresse 0x10 (BUS_BASIS 16). Tabelle Z. 2046-2809."""
    return SimProfile(
        key="mc2", name="MultiControl 2", regler_typ=0x0A01, address=0x10, serial=0x00020001,
        values=[
            _e(_ag(0, "M_ANFGES"), "Anf.", VT.FLOAT, 1, 150, nachkomma=1, einheit=EH["CELSIUS"], code=SC_INTE,
               value=65.0),
            _e("M_KOLLEKT", "Solarkollektor", VT.FLOAT, -50, 290, nachkomma=1, einheit=EH["CELSIUS"], value=48.3),
            _e("M_SKREIS", "Aktiv", VT.CHAR, 0, 5, out=OUT_TEXTAUSWAHL, value=1),
            _e(_ag(0, "M_WWO"), "Warmwasser", VT.FLOAT, -50, 290, nachkomma=1, einheit=EH["CELSIUS"], value=52.1),
            _e(_ag(0, "M_WWPU"), "WW-Ladepumpe", VT.CHAR, 0, 1, out=OUT_ONOFF, value=1),
            _e("M_HKFREIGABE", "HK-Freigabe", VT.CHAR, -50, 150, einheit=EH["PERCENT"], value=100),
            _e(_ag(0, "M_OEL_P"), "Periode", VT.LONG, 0, 9999999, nachkomma=1, out=OUT_SECTOH,
               einheit=EH["STUNDEN"], value=360 * 3600),
        ],
        params=[
            _e(_ag(0, "P_WWAUS"), "WW max.", VT.CHAR, 9, 90, 55, einheit=EH["CELSIUS"], value=55),
            _e(_ag(0, "P_WWEIN"), "WW min.", VT.CHAR, 8, 89, 45, einheit=EH["CELSIUS"], value=45),
            _e(_ag(0, "P_WWDIFF"), "Differenz", VT.CHAR, 2, 20, 5, einheit=EH["KELVIN"], value=5),
            _e(_ag(0, "P_WWSPERRVON"), "Sperrzeit von", VT.SHORT, 0, 1440, 22 * 60, out=OUT_MINTOTIME,
               value=22 * 60),
            _e("P_SKMAX", "Kollektor max.", VT.CHAR, 90, 250, 120, einheit=EH["CELSIUS"], value=120),
            _e("P_ANZEIGE", "Anzeigebild", VT.CHAR, 0, 3, 3, out=OUT_TEXTAUSWAHL, value=3),
            _e(_ag(0, "P_OELDELP"), "Periode löschen", VT.CHAR, 0, 1, 0, out=OUT_YESNO, value=0),
            _e("P_PFILTERZ1", "1.", VT.ZYKLEN, 0, 3, 0, out=OUT_ZYKLENZEITEN, einheit=EH["CELSIUS"],
               value=bytes(8)),
        ],
    )


def profile_lc1bsb() -> SimProfile:
    """LambdaControl 1 BSB, Reglertyp 0x0D00, Adresse 2. Tabelle Z. 915-1359."""
    return SimProfile(
        key="lc1bsb", name="LambdaControl 1 BSB", regler_typ=0x0D00, address=0x02, serial=0x00030007,
        values=[
            _e("M_KSTATUS", "Kessel", VT.CHAR, 0, 10, out=OUT_TEXTAUSWAHL, value=2),
            _e("M_TKS", "Kessel Soll", VT.FLOAT, 1, 150, nachkomma=1, einheit=EH["CELSIUS"], value=78.0),
            _e("M_TKI", "Kesseltemp.", VT.FLOAT, -50, 150, nachkomma=1, einheit=EH["CELSIUS"], value=69.9),
            _e("M_TKRI", "Rücklauftemp.", VT.FLOAT, -50, 150, nachkomma=1, einheit=EH["CELSIUS"], value=58.0),
            _e("M_KESSELT", "Kessel t.", VT.LONG, 0, 9999999, nachkomma=1, out=OUT_SECTOH,
               einheit=EH["STUNDEN"], tip="h total", value=4321 * 3600),
            _e("M_KESSELP", "Kessel P.", VT.LONG, 0, 9999999, nachkomma=1, out=OUT_SECTOH,
               einheit=EH["STUNDEN"], tip="h Periode", value=21 * 3600 + 360),
            _e("M_BEREITS", "Übertemp. total", VT.FLOAT, 0, 100, nachkomma=1, einheit=EH["PERCENT"], value=1.5),
        ],
        params=[
            _e("P_TKS", "Kessel Soll", VT.SHORT, 600, 1200, 800, 1, einheit=EH["CELSIUS"], kat=5, pnr=12, value=780),
            _e("P_TAGMIN", "Abgas min.", VT.SHORT, 90, 300, 140, einheit=EH["CELSIUS"], kat=5, pnr=13, value=140),
            _e("P_FEUCHTE", "Feuchte", VT.CHAR, 0, 2, 1, out=OUT_TEXTAUSWAHL, kat=5, pnr=2, value=1),
            _e("P_TAAUSLEG", "TA-Auslegung", VT.SCHAR, -30, 10, -12, einheit=EH["CELSIUS"], kat=5, pnr=7,
               tip="Siehe Wärmebedarfsber.", value=-14),
            _e("P_KF_TA", "Fr. Außentemp.", VT.SCHAR, -20, 5, 0, einheit=EH["CELSIUS"], kat=5, pnr=18, value=0),
            _e("P_RVLFZ", "Laufzeit Y1", VT.SHORT, 90, 240, 150, einheit=EH["SEKUNDEN"], kat=5, pnr=15, value=150),
            _e("P_KDELP", "Perioden löschen", VT.CHAR, 0, 1, 0, out=OUT_YESNO, value=0),
        ],
    )


def profile_zuendung() -> SimProfile:
    """Zündung, Reglertyp 0x3A00, Adresse 0x50 (_BUS_OFFSET). Tabelle App/Zündung Z. 226-321."""
    return SimProfile(
        key="zuendung", name="Zündung", regler_typ=0x3A00, address=0x50, serial=0x00040003, major=1, minor=7,
        values=[
            _e("M_ZZUEND", "Zündung", VT.CHAR, 0, 1, out=OUT_ONOFF, value=0),
            _e("M_ZGEBL", "Zündgebläse", VT.CHAR, 0, 1, out=OUT_ONOFF, value=0),
            _e("M_ZTHEIZUNG", "Heizung", VT.LONG, 0, 9999999, nachkomma=2, out=OUT_SECTOH,
               einheit=EH["STUNDEN"], value=95 * 3600),
            _e("M_ZTGEBLAESE", "Zündgebläse", VT.LONG, 0, 9999999, nachkomma=2, out=OUT_SECTOH,
               einheit=EH["STUNDEN"], value=40 * 3600 + 900),
            _e("M_REGV", "Software", VT.SHORT, 0, 0, nachkomma=2, value=107),
            _e("M_CANTX", "Tx", VT.SHORT, 0, 9999, code=SC_WERK, value=15),
        ],
        params=[
            # TODO: Werk = ZT_GETRENNT (Enum-Wert nicht nachgeschlagen) -> 0
            _e("P_ZGEBLAESE", "Zündgebläse", VT.CHAR, 0, 1, 0, out=OUT_TEXTAUSWAHL, code=SC_FACH, value=0),
            _e("P_ZDELHEIZ", "Heizung löschen", VT.CHAR, 0, 1, 0, out=OUT_YESNO, code=SC_FACH, value=0),
            _e("P_ZDELGEBL", "Gebläse löschen", VT.CHAR, 0, 1, 0, out=OUT_YESNO, code=SC_FACH, value=0),
        ],
    )


def profile_sonde() -> SimProfile:
    """O2-Sonde NGK, Reglertyp 0xFFF2, Adresse 0xE0 (_BUS_OFFSET). App/Sauerstoffsonde Z. 160-221."""
    return SimProfile(
        key="sonde", name="Sonde NGK", regler_typ=0xFFF2, address=0xE0, serial=0x00050011, major=1, minor=3,
        values=[
            _e("M_O2I", "O₂", VT.FUEHLER, -999, 99999, nachkomma=3, einheit=EH["PERCENT"], value=7.125),
            _e("M_U0", "UPOWER", VT.FUEHLER, -999, 99999, nachkomma=3, einheit=EH["V"], value=12.04),
            _e("M_U5", "Rpvs", VT.FLOAT, -999, 99999, nachkomma=3, einheit=EH["OHM"], value=88.5),
            _e("M_PWM1", "Heizung", VT.FLOAT, 0, 99999, nachkomma=1, einheit=EH["PERCENT"], value=42.0),
            _e("M_O2U", "Signal", VT.FLOAT, -9999, 99999, nachkomma=2, einheit=EH["MV"], value=31.5),
            _e("M_U8", "S-Reg 0", VT.CHAR, 0, 255, out=OUT_HEXADECIMAL, value=0x5A),
        ],
        params=[
            _e("P_O2PIDT", "T", VT.CHAR, 1, 30, 1, einheit=EH["SEKUNDEN"], code=SC_WERK, value=1),
            _e("P_O2PIDTN", "Tn", VT.SHORT, 1, 3000, 150, einheit=EH["SEKUNDEN"], code=SC_WERK, value=150),
            _e("P_O2PIDTV", "Tv", VT.SHORT, 0, 600, 60, einheit=EH["SEKUNDEN"], code=SC_WERK, value=60),
            _e("P_O2PIDKPR", "Kpr", VT.CHAR, 1, 250, 30, 1, code=SC_WERK, value=30),
            _e("P_CALO2U", "O₂ -12mV", VT.SHORT, 1, 150, 77, 1, einheit=EH["PERCENT"], code=SC_WERK, value=77),
            _e("P_RLABG_U", "Abgl. RL   6.81", VT.CHAR, 0, 1, 0, out=OUT_YESNO, code=SC_WERK, value=0),
        ],
    )


PROFILES = {"lc2": profile_lc2, "mc2": profile_mc2, "lc1bsb": profile_lc1bsb, "zuendung": profile_zuendung,
            "sonde": profile_sonde}


def default_profiles() -> list[SimProfile]:
    return [f() for f in PROFILES.values()]


# ---------------------------------------------------------------------------------------------
# Firmware-Logik
# ---------------------------------------------------------------------------------------------
def parameter_nummer(e: SimEntry) -> int:
    """``GetParameterNummer`` (Messwerte.cpp:533-570)."""
    nr = (e.kat << 8) | e.pnr
    if nr:
        return nr
    m = e.nr
    if (m & 0x00F0) < 0x0040 or (m & 0x00F0) >= 0x0080 or m == N["P_CODE"]:
        return 0
    if (m & 0xF000) == 0xF000:
        nr = 0x1000 | (m & 0x0F00)
    elif (m & 0xF000) == 0 and (m & 0x0F00) != 0:
        nr = 0x1F00 + (m & 0x0F00)
    else:
        nr = ((m & 0xF000) >> 4) + 0x0100
    return nr | ((m & 0xFF) - 0x40) & 0xFF


def parameter_name(e: SimEntry) -> str:
    """``GetParameterName(..., MitNr=1)`` – "<Kat>.<Nr>: Name". TODO: Format von write_val_to_string."""
    nr = parameter_nummer(e)
    return f"{nr >> 8}.{nr & 0xFF:02d}: {e.name}" if nr else e.name


def parameter_senden_okay(e: SimEntry) -> bool:
    """``ParameterSendenOkay`` (BusOut.cpp:278-294)."""
    return not (e.nr in (N["P_CODE"], N["P_BUSADR"], N["P_NACHLEG_KG"], N["P_NACHLEGEM"], N["P_DATE"], N["P_TIME"])
                or e.out in (OUT_RAMSTRING, OUT_IPV4)
                or (e.nr & ~0x0F00) == N["P_WRSDBUS"])


def _wert_bytes(e: SimEntry, length: int) -> bytes:
    if isinstance(e.value, (bytes, bytearray)):
        return bytes(e.value)[:length].ljust(length, b"\0")
    if vartyp.is_float(e.vartyp):
        return struct.pack("<f", float(e.value)).ljust(length, b"\0")
    return vartyp.encode_raw(e.vartyp, int(e.value), length)


def _limit32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


def check_parameter(e: SimEntry) -> bool:
    """``CheckParameter`` (Messwerte.cpp:360-431): außerhalb Min/Max -> Werkseinstellung."""
    if e.nr & 0x80:
        return False
    if (e.out in (OUT_SECTOH, OUT_ZYKLEN, OUT_ZYKLENZEITEN)
            or e.nr in (N["P_CODE"], N["P_NACHLEG_KG"], N["P_NACHLEGEM"], N["P_KTYP"])):
        return False
    if not vartyp.is_numeric(e.vartyp):
        return False
    var = int(e.value) if vartyp.is_float(e.vartyp) else int(e.value)   # (DWORD)float -> abgeschnitten
    if (e.vartyp & 0x80) or vartyp.is_float(e.vartyp):
        mn, mx = _limit32(e.min), _limit32(e.max)
        if var < mn or var > mx:
            werk = min(max(e.werk, mn), mx)
            if var != werk:
                e.value = float(werk) if vartyp.is_float(e.vartyp) else werk
                return True
    else:
        mn, mx = e.min & 0xFFFFFFFF, e.max & 0xFFFFFFFF
        uvar = var & 0xFFFFFFFF
        if uvar < mn or uvar > mx:
            werk = e.werk & 0xFFFF       # (unsigned)pParameter->Werk – int ist 16 Bit
            if e.nr not in (N["P_KTYP"], N["P_SYSTEM"]):
                werk = min(max(werk, mn), mx)
            if uvar != werk:
                e.value = werk
                return True
    return False


class CanSimulator:
    """Simuliert eines oder mehrere Geräte an einem python-can-Bus (Standard: ``virtual``)."""

    def __init__(self, channel: str, profiles: list[SimProfile] | None = None, *, interface: str = "virtual",
                 powerup: bool = False, echo: bool = False, broadcast_interval_s: float | None = None,
                 response_delay_s: float = 0.0, squatter_address: int | None = None,
                 squatter_interval_s: float = 0.5, enum_gap_s: float = 0.0, **bus_kwargs) -> None:
        self.channel = channel
        self.interface = interface
        self.profiles = profiles if profiles is not None else default_profiles()
        self.powerup = powerup
        self.echo = echo
        self.broadcast_interval_s = broadcast_interval_s
        self.response_delay_s = response_delay_s
        self.squatter_address = squatter_address
        self.squatter_interval_s = squatter_interval_s
        self.enum_gap_s = enum_gap_s
        self._bus_kwargs = bus_kwargs
        self._bus: can.BusABC | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self.lock = threading.Lock()
        #: empfangene Rahmen (Zeit, ID, Daten) – für Tests
        self.rx_log: list[tuple[float, int, bytes]] = []
        #: Anfragen ohne Antwort verwerfen (Tests: Retry/Timeout)
        self.drop_next = 0
        #: nächste Multiframe-Antwort(en) mit falscher CRC senden
        self.corrupt_next = 0
        #: Schreib-Bestätigung unterdrücken
        self.suppress_write_confirm = False
        #: Nummer -> Wert, den die "Firmware" nach dem Schreiben stattdessen einträgt
        self.override_after_write: dict[int, int | float] = {}
        self.writes: list[tuple[int, int, int, bytes]] = []    # (Adresse, Nr, VarTyp, Wert)

    # --- Lebenszyklus ---
    def start(self) -> CanSimulator:
        self._bus = can.Bus(interface=self.interface, channel=self.channel, receive_own_messages=False,
                            **self._bus_kwargs)
        self._running = True
        self._thread = threading.Thread(target=self._run, name=f"can-sim-{self.channel}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(2.0)
        if self._bus:
            self._bus.shutdown()
        self._bus = None

    def __enter__(self) -> CanSimulator:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def profile(self, address: int) -> SimProfile | None:
        return next((p for p in self.profiles if p.address == address), None)

    # --- Senden (Simulator-intern, Geräteseite) ---
    def _tx(self, arb_id: int, data: bytes) -> None:
        assert self._bus is not None
        self._bus.send(can.Message(arbitration_id=arb_id, data=data, is_extended_id=True))   # sim-send

    def _tx_block(self, arb_id: int, block: bytes) -> None:
        if self.corrupt_next > 0:
            self.corrupt_next -= 1
            block = block[:-1] + bytes(((block[-1] ^ 0xFF),))
        for chunk in multiframe.split_frames(block, pad=0xA5):
            self._tx(arb_id, chunk)

    # --- Hauptschleife ---
    def _run(self) -> None:
        next_bc = time.monotonic() + (self.broadcast_interval_s or 0)
        next_sq = time.monotonic() + self.squatter_interval_s
        while self._running:
            bus = self._bus
            if bus is None:
                break
            msg = bus.recv(0.02)
            now = time.monotonic()
            if self.broadcast_interval_s and now >= next_bc:
                next_bc = now + self.broadcast_interval_s
                self._broadcast()
            if self.squatter_address is not None and now >= next_sq:
                next_sq = now + self.squatter_interval_s
                self._tx(idcodec.encode(idcodec.KANAL_ANTWORT, self.squatter_address, 0xFF, 0x20),
                         bytes(8))
            if msg is None or not msg.is_extended_id or msg.is_error_frame:
                continue
            with self.lock:
                self.rx_log.append((now, msg.arbitration_id, bytes(msg.data)))
            if self.echo:
                self._tx(msg.arbitration_id, bytes(msg.data))
            try:
                self._handle(msg)
            except Exception:  # noqa: BLE001
                log.exception("Simulator: Fehler bei %08X", msg.arbitration_id)

    def _broadcast(self) -> None:
        for p in self.profiles:
            for nr in p.broadcast:
                e = p.entry(nr)
                if e is not None:
                    data = messages.MesswertAntwort(e.nr, vartyp.bus_typ(e.vartyp),
                                                    _wert_bytes(e, 5)).build()
                    self._tx(idcodec.encode(idcodec.KANAL_ANTWORT, p.address, idcodec.BROADCAST, 0x20), data)

    def _handle(self, msg: can.Message) -> None:
        fid = idcodec.decode(msg.arbitration_id)
        data = bytes(msg.data)
        # Hardwarefilter: eigene Adresse und 0xFF werden angenommen (BusIn.cpp:85-88)
        targets = [p for p in self.profiles if fid.empfaenger in (p.address, idcodec.BROADCAST)]
        for p in targets:
            if fid.kanal == idcodec.KANAL_ANFRAGE:
                if self.drop_next > 0:
                    self.drop_next -= 1
                    continue
                if self.response_delay_s:
                    time.sleep(self.response_delay_s)
                self._anfrage(p, fid, data)
            elif fid.kanal == idcodec.KANAL_BEFEHLE and fid.empfaenger == p.address and fid.subkanal == 0x00:
                self._parameter_setzen(p, fid, data)

    def _answer_id(self, p: SimProfile, sub: int, dest: int) -> int:
        return idcodec.encode(idcodec.KANAL_ANTWORT, p.address, dest, sub)

    def _anfrage(self, p: SimProfile, fid: idcodec.FrameId, data: bytes) -> None:
        sub, src = fid.subkanal, fid.sender
        if sub == 0x00:
            ident = messages.IdentifyAntwort(prg_crc=0xBEEF, major=p.major, minor=p.minor,
                                             regler_typ=p.regler_typ, seriennummer=p.serial,
                                             version=p.ident_version, datum=0, name=p.name)
            self._tx_block(self._answer_id(p, 0x00, src), ident.build())
            return
        if len(data) < 8:
            return
        req = messages.Anfrage.parse(data)
        if sub == 0x20:
            if self.powerup:
                return
            for e in p.values:
                if req.nr in (0xFFFF, e.nr):
                    ans = messages.MesswertAntwort(e.nr, vartyp.bus_typ(e.vartyp), _wert_bytes(e, 5))
                    self._tx(self._answer_id(p, 0x20, src), ans.build())
                    if req.nr != 0xFFFF:
                        return
        elif sub == 0x30:
            if self.powerup:
                return
            for e in p.values:
                if req.nr in (0xFFFF, e.nr):
                    self._tx_block(self._answer_id(p, 0x30, src), self._detail(e, parameter=False))
                    if req.nr != 0xFFFF:
                        return
                    if self.enum_gap_s:
                        time.sleep(self.enum_gap_s)
        elif sub == 0x38:
            if req.nr == 0xFFFF:
                for e in p.params:
                    if parameter_senden_okay(e):
                        self._tx_block(self._answer_id(p, 0x38, src), self._detail(e, parameter=True))
                        if self.enum_gap_s:
                            time.sleep(self.enum_gap_s)
            else:
                e = next((x for x in p.params if x.nr == req.nr), None)
                if e is not None:
                    self._tx_block(self._answer_id(p, 0x38, src), self._detail(e, parameter=True))
        # andere Subkanäle (0x08 Auswahltexte, 0x28 Fehlerspeicher, …): bewusst nicht simuliert

    def _detail(self, e: SimEntry, parameter: bool) -> bytes:
        einheit = 0 if (e.out & 0x3F) == OUT_TEXTAUSWAHL else e.einheit
        det = messages.DetailAntwort(
            nr=e.nr, sprache=0, ausgabe=e.out & 0x3F, einheit=einheit, nachkomma=e.nachkomma,
            min=_limit32(e.min), max=_limit32(e.max), werk=e.werk if parameter else 0,
            vartyp=e.vartyp if parameter else vartyp.bus_typ(e.vartyp), code=e.code,
            wert=_wert_bytes(e, 8) if e.nr != N["M_NAME"] else bytes(8),
            name=parameter_name(e) if parameter else e.name, tip=e.tip)
        return det.build(parameter=parameter)

    def _parameter_setzen(self, p: SimProfile, fid: idcodec.FrameId, data: bytes) -> None:
        """``Bus_Parameter_setzen`` (BusOut.cpp:889-950)."""
        if len(data) != 8:
            return
        par = messages.ParameterSetzen.parse(data)
        if par.nr >= N["P_SPEZI"] and par.nr in (N["P_ZYKLENWT"], N["P_ERWBUS"]):
            return
        e = next((x for x in p.params if x.nr == par.nr), None)
        if e is None or e.vartyp != par.vartyp:
            return          # unbekannt bzw. falscher VarTyp: stillschweigend verworfen
        size = vartyp.size_of(par.vartyp)
        if size > 5:
            return
        with self.lock:
            self.writes.append((p.address, par.nr, par.vartyp, par.wert))
        if vartyp.is_numeric(e.vartyp):
            e.value = vartyp.decode_raw(e.vartyp, par.wert[:size].ljust(5, b"\0"))
        check_parameter(e)
        if par.nr in self.override_after_write:
            e.value = self.override_after_write[par.nr]
        if self.suppress_write_confirm:
            return
        self._tx_block(self._answer_id(p, 0x38, fid.sender), self._detail(e, parameter=True))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m gateway.can_lc2.simulator",
                                 description="Logotherm-CAN-Simulator (antwortet wie die Firmware)")
    ap.add_argument("--channel", required=True, help="Kanal (virtual: Name; socketcan: vcan0 …)")
    ap.add_argument("--interface", default="virtual", help="python-can-Interface (Default: virtual)")
    ap.add_argument("--profiles", default=",".join(PROFILES),
                    help=f"Kommagetrennt aus: {', '.join(PROFILES)}")
    ap.add_argument("--powerup", action="store_true", help="Regler im Hochlauf (0x20/0x30 ignorieren)")
    ap.add_argument("--echo", action="store_true", help="empfangene Rahmen zurücksenden (wie InnoMaker)")
    ap.add_argument("--broadcast", type=float, default=5.0, help="Intervall unaufgeforderter 0x0A/0x20 [s], 0=aus")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    profiles = [PROFILES[k.strip()]() for k in args.profiles.split(",") if k.strip()]
    sim = CanSimulator(args.channel, profiles, interface=args.interface, powerup=args.powerup, echo=args.echo,
                       broadcast_interval_s=args.broadcast or None)
    sim.start()
    for p in profiles:
        log.info("Simuliere %s (Reglertyp %#06x) an Adresse %#04x", p.name, p.regler_typ, p.address)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop()
    return 0


def clone_profiles(profiles: list[SimProfile]) -> list[SimProfile]:
    return copy.deepcopy(profiles)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
