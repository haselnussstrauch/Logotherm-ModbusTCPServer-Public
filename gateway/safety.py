"""Zentrale, fest eingebaute Sicherheitsregeln (NICHT konfigurierbar).

Dieses Modul ist die letzte Instanz vor jedem Sendevorgang:

- ``check_can_tx``      – Whitelist für CAN-Frames (LC2-Protokoll, 29-Bit-IDs).
- ``param_lock_reason`` – dauerhaft gesperrte Parameter je Bus/Reglertyp.
- ``check_lc1_*``       – Sendeprüfung für die serielle LC1-Schnittstelle.
- ``WriteGuard``        – Schreib-Guards für Modbus-Schreibwünsche (Freigabe, Min/Max,
                          Totband, Ratenlimit 10 min je Parameter, Tagesbudget).
- ``verify_readback``   – Prüfung der Rückmeldung nach dem Schreiben.

Grundlage ist die Regler-Firmware (Stand LambdaControl 2 V1.29)
(os2/net/BusIn.cpp, os2/net/BusOut.cpp, os2/Include/dev/CAN.h,
os2/Drivers/Block/CAN MCP2515.cpp, Include/Common/BusProtokoll.h).

Unabhängig vom späteren Codec in ``gateway/can_lc2/``: die ID wird hier bewusst selbst
dekodiert, damit ein Fehler im Codec nicht auch die Prüfung aushebelt.

Import-Regel: safety importiert ``gateway.config``; config importiert safety erst innerhalb
seines Validators (kein Import-Zyklus).
"""

from __future__ import annotations

import datetime as _dt
import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

from gateway import config
from gateway.devices import fw_numbers, lc1_tables
from gateway.errors import SafetyViolation, WriteRejected

# =============================================================================================
# CAN-Identifier (29 Bit, extended)
# =============================================================================================
# Firmware-intern (CAN.h, CAN_IDENTIFIER): DWORD = Kanal<<24 | Sender<<16 | Empfänger<<8 | Kanal2.
# MCP2515-Treiber (CAN MCP2515.cpp, CCAN::Encode): 29-Bit-ID = DWORD >> 3, Bit 2 des DWORD
# (= Bit 2 des Subkanal-Bytes) erzeugt ein RTR-Frame (CCAN::MCPTransmit).
# Ergebnis: Kanal Bit 28..21 | Sender 20..13 | Empfänger 12..5 | Bits 4..0 = Subkanal-Byte >> 3.

CAN_ID_MAX = (1 << 29) - 1

KANAL_SPEZIAL = 0x00     # Uhrzeit/DCF (Broadcast von 0 an 0)
KANAL_BEFEHLE = 0x08     # Befehle (Parameter setzen, Bootloader, Aggregatetest, …)
KANAL_ANTWORT = 0x0A     # Antworten / Prozesswerte – NIEMALS senden
KANAL_ANFRAGE = 0x0B     # Anfragen
KANAL_SPEZIALX = 0x10    # Bootloader / Update

BROADCAST = 0xFF


@dataclass(frozen=True)
class CanId:
    kanal: int
    sender: int
    empfaenger: int
    subkanal: int        # Subkanal-Byte (Vielfaches von 8)


def decode_can_id(arbitration_id: int) -> CanId:
    """Zerlegt eine 29-Bit-ID in Kanal, Sender, Empfänger und Subkanal-Byte."""
    if not isinstance(arbitration_id, int) or isinstance(arbitration_id, bool) \
            or not 0 <= arbitration_id <= CAN_ID_MAX:
        raise SafetyViolation(f"ungültige CAN-ID {arbitration_id!r}")
    return CanId(kanal=(arbitration_id >> 21) & 0xFF,
                 sender=(arbitration_id >> 13) & 0xFF,
                 empfaenger=(arbitration_id >> 5) & 0xFF,
                 subkanal=(arbitration_id & 0x1F) << 3)


def encode_can_id(kanal: int, sender: int, empfaenger: int, subkanal: int) -> int:
    """Gegenstück zu ``decode_can_id`` (für Tests/Diagnose). Subkanal muss Vielfaches von 8 sein."""
    for v in (kanal, sender, empfaenger, subkanal):
        if not 0 <= v <= 0xFF:
            raise ValueError("Feld außerhalb 0..255")
    if subkanal & 0x07:
        raise ValueError("Subkanal-Byte muss ein Vielfaches von 8 sein (Bits 2..0 gehen verloren)")
    return kanal << 21 | sender << 13 | empfaenger << 5 | subkanal >> 3


# Erlaubte Dienste (Kanal, Subkanal) – alles andere ist verboten.
SVC_IDENTIFY = (KANAL_ANFRAGE, 0x00)
SVC_MESSWERT = (KANAL_ANFRAGE, 0x20)
SVC_MESSWERT_DETAIL = (KANAL_ANFRAGE, 0x30)
SVC_PARAMETER_DETAIL = (KANAL_ANFRAGE, 0x38)
SVC_PARAMETER_SETZEN = (KANAL_BEFEHLE, 0x00)

ALLOWED_CAN_SERVICES: dict[tuple[int, int], str] = {
    SVC_IDENTIFY: "Identify",
    SVC_MESSWERT: "Messwert",
    SVC_MESSWERT_DETAIL: "Messwert-Detail",
    SVC_PARAMETER_DETAIL: "Parameter-Detail",
    SVC_PARAMETER_SETZEN: "Parameter schreiben",
}

# Begründungen für ausdrücklich verbotene Kanäle/Dienste (nur für bessere Fehlermeldungen;
# verboten ist ohnehin alles, was nicht in ALLOWED_CAN_SERVICES steht).
_FORBIDDEN_KANAL: dict[int, str] = {
    KANAL_ANTWORT: "Kanal 0x0A (Antworten) – andere Geräte übernehmen 0x0A/0x20 als Prozesswert "
                   "(z. B. Zündbefehl an das Zündmodul)",
    KANAL_SPEZIALX: "Kanal 0x10 (Bootloader/Update)",
    KANAL_SPEZIAL: "Kanal 0x00 (Uhrzeit/DCF)",
}
_FORBIDDEN_SERVICE: dict[tuple[int, int], str] = {
    (KANAL_BEFEHLE, 0x08): "Befehl 0x08 (Bootloader starten, \"xxBootZ\\\")",
    (KANAL_BEFEHLE, 0x10): "Befehl 0x10 (erweiterte Parameter/Schaltzyklen setzen)",
    (KANAL_BEFEHLE, 0x18): "Befehl 0x18 (Aggregatetest – steuert Ausgänge)",
    (KANAL_ANFRAGE, 0x28): "Anfrage 0x28 (Fehlerspeicher – \"CLR\" löscht ihn)",
}

#: Maximale VarTyp-Größe je numerischem Typ (get_sizeofTyp, os2/net/Messwerte.cpp).
_VARTYP_SIZE: dict[int, int] = {
    config.VarTyp.CHAR: 1, config.VarTyp.SCHAR: 1,
    config.VarTyp.SHORT: 2, config.VarTyp.SSHORT: 2, config.VarTyp.SOMMER: 2,
    config.VarTyp.LONG: 4, config.VarTyp.SLONG: 4, config.VarTyp.FLOAT: 4,
    config.VarTyp.FUEHLER: 4,
}

#: Code-Byte (Serviceebene) in Anfragen; die Firmware sendet selbst immer 0xFF (SendMesswert).
ANFRAGE_CODE = 0xFF


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def is_parameter_number(nr: int) -> bool:
    """Parameter = Low-Byte 0x40..0x7F (PARAMETER_MASK gesetzt, TESTFUNKT_MASK nicht)."""
    return 0x40 <= (nr & 0xFF) <= 0x7F


def _check_anfrage(name: str, data: bytes, allow_enumeration: bool = False) -> None:
    """ANFRAGE_MESSWERT {Nummer u16 LE, Sprache u8, Code u8, frei[4]} – DLC 8."""
    if len(data) != 8:
        raise SafetyViolation(f"{name}: DLC {len(data)} statt 8")
    nr = int.from_bytes(data[0:2], "little")
    if nr == 0xFFFF and allow_enumeration:
        pass  # Aufzählung aller Einträge: nur auf Knopfdruck (Werteliste), nie zyklisch
    elif nr in (0x0000, 0xFFFF):
        # 0xFFFF = "alle" -> Antwortflut (TX-Queue der Geräte), 0x0000 = Tabellenende.
        raise SafetyViolation(f"{name}: Nummer {nr:#06x} nicht erlaubt (nur Einzelabfrage)")
    if data[3] != ANFRAGE_CODE:
        raise SafetyViolation(f"{name}: Code {data[3]:#04x} statt {ANFRAGE_CODE:#04x}")
    if any(data[4:8]):
        raise SafetyViolation(f"{name}: freie Bytes müssen 0 sein")


def _check_parameter_setzen(data: bytes, regler_typ: int | None) -> None:
    """BUS_PARAMETERSETZEN {Nummer u16 LE, VarTyp u8, Wert[5]} – DLC 8."""
    if len(data) != 8:
        raise SafetyViolation(f"Parameter schreiben: DLC {len(data)} statt 8")
    nr = int.from_bytes(data[0:2], "little")
    vartyp = data[2]
    if not is_parameter_number(nr):
        raise SafetyViolation(f"Parameter schreiben: {nr:#06x} ist kein Parameter")
    reason = param_lock_reason("can", regler_typ, nr)
    if reason:
        raise SafetyViolation(f"Parameter schreiben: {nr:#06x} gesperrt ({reason})")
    if vartyp == config.VarTyp.ZYKLEN:
        raise SafetyViolation("Parameter schreiben: VT_ZYKLEN ist verboten (8 Byte -> RAM-Überlauf)")
    if vartyp not in config.NUMERIC_VARTYPES or vartyp not in _VARTYP_SIZE:
        raise SafetyViolation(f"Parameter schreiben: VarTyp {vartyp:#04x} nicht numerisch")
    size = _VARTYP_SIZE[vartyp]
    if any(data[3 + size:8]):
        raise SafetyViolation("Parameter schreiben: ungenutzte Wert-Bytes müssen 0 sein")


def check_can_tx(arbitration_id: int, data: bytes, *, is_extended: bool, own_address: int,
                 regler_typ: int | None = None, is_remote_frame: bool = False,
                 allow_enumeration: bool = False) -> None:
    """Prüft einen CAN-Sendewunsch. Wirft ``SafetyViolation``, wenn er nicht erlaubt ist.

    ``allow_enumeration=True`` erlaubt Nr=0xFFFF (alle Einträge) – ausschließlich bei 0x30/0x38
    und nur vom expliziten Aufzählungs-Pfad (Knopfdruck in der Oberfläche) zu setzen.
    """
    if is_extended is not True:
        raise SafetyViolation("nur Extended-Frames (29 Bit) erlaubt")
    if is_remote_frame is not False:
        raise SafetyViolation("Remote-Frames (RTR) sind verboten")
    if not isinstance(data, (bytes, bytearray)):
        raise SafetyViolation("Daten müssen bytes sein")
    data = bytes(data)
    if len(data) > 8:
        raise SafetyViolation(f"DLC {len(data)} > 8")
    if not _is_int(own_address) or not 0x01 <= own_address <= 0xFE:
        raise SafetyViolation(f"ungültige eigene Adresse {own_address!r}")
    cid = decode_can_id(arbitration_id)

    if cid.sender != own_address:
        raise SafetyViolation(f"Sender {cid.sender:#04x} ≠ eigene Adresse {own_address:#04x}")
    if cid.empfaenger in (0x00, BROADCAST) or not 0x01 <= cid.empfaenger <= 0xFE:
        raise SafetyViolation(f"Empfänger {cid.empfaenger:#04x} ist kein konkretes Ziel (Broadcast)")
    if cid.empfaenger == own_address:
        raise SafetyViolation("Empfänger = eigene Adresse")

    svc = (cid.kanal, cid.subkanal)
    if svc not in ALLOWED_CAN_SERVICES:
        why = _FORBIDDEN_KANAL.get(cid.kanal) or _FORBIDDEN_SERVICE.get(svc) \
            or f"Kanal {cid.kanal:#04x}/Subkanal {cid.subkanal:#04x} nicht freigegeben"
        raise SafetyViolation(f"verboten: {why}")

    if svc == SVC_IDENTIFY:
        if len(data) != 0:
            raise SafetyViolation(f"Identify: DLC {len(data)} statt 0")
    elif svc in (SVC_MESSWERT_DETAIL, SVC_PARAMETER_DETAIL):
        _check_anfrage(ALLOWED_CAN_SERVICES[svc], data, allow_enumeration)
    elif svc == SVC_MESSWERT:
        _check_anfrage(ALLOWED_CAN_SERVICES[svc], data)
    elif svc == SVC_PARAMETER_SETZEN:
        _check_parameter_setzen(data, regler_typ)
    else:  # pragma: no cover – Whitelist und Zweige müssen übereinstimmen
        raise SafetyViolation("interner Fehler: Dienst ohne Prüfung")


# =============================================================================================
# Gesperrte Parameter
# =============================================================================================
AGG_MASK = 0x0F00
P_SPEZI = fw_numbers.NUMBERS["P_SPEZI"]    # 0xFF00

REGLER_TYP_DATABOX = 0xFFF0
REGLER_TYP_SONDE = 0xFFF2

# (Muster, Grund) – Namen werden aus fw_numbers.NUMBERS ermittelt, nicht abgetippt.
_CAN_COMMON_PATTERNS: list[tuple[str, str]] = [
    (r"P_BUSADR", "Busadresse"),
    (r"P_SYSTEM", "Systemeinstellung"),
    (r"P_KTYP", "Kesseltyp"),
    (r"P_BASIS", "Basisdaten laden"),
    (r"P_DATE", "Datum"),
    (r"P_TIME", "Uhrzeit"),
    (r"P_CODE", "Servicecode"),
    (r"P_KZZ1", "Zündzeiten (Schaltzyklen)"),
    (r"P_HKZYK\d+", "Heizkreis-Schaltzyklen"),
    (r"P_DEL\w*", "Löschfunktion"),
    (r"P_KDEL\w*", "Löschfunktion Kessel"),
    (r"P_ZDEL\w*", "Löschfunktion Zündung"),
    # Zusätzlich (konservativ): alle übrigen Löschfunktionen wie P_OELDELP, P_SOLDELT, …
    (r"P_\w+DEL\w*", "Löschfunktion"),
    # F4: Was die Firmware nicht über den Bus herausgibt (ParameterSendenOkay, BusOut.cpp:278-294)
    # oder nicht prüft (CheckParameter, Messwerte.cpp:360-377), wird nicht schreibbar.
    (r"P_WRSDBUS", "Busadresse WRS-D (wird von der Firmware nicht über den Bus gesendet)"),
    (r"P_NACHLEG_KG", "keine Bereichsprüfung in der Firmware (CheckParameter)"),
    (r"P_NACHLEGEM", "keine Bereichsprüfung in der Firmware (CheckParameter)"),
]
_SONDE_PATTERNS: list[tuple[str, str]] = [
    (r"P_CAL\w*", "Sonde: Kalibrierung"),
    (r"P_RLABG_\w*", "Sonde: RL-Abgleich"),
    (r"P_PWM2", "Sonde: PWM-Ausgang 2"),
    (r"P_PWM3", "Sonde: PWM-Ausgang 3"),
]
_DATABOX_PATTERNS: list[tuple[str, str]] = [
    (r"P_CANBUS", "Databox: CAN-Bus-Einstellung"),
    (r"P_IPADDR", "Databox: IP-Adresse"),
    (r"P_DHCP", "Databox: DHCP"),
]


@dataclass
class _LockTable:
    exact: dict[int, str]
    all_agg: dict[int, str]    # Schlüssel: Nummer mit Aggregat-Bits 0

    def lookup(self, nr: int) -> str | None:
        if nr in self.exact:
            return self.exact[nr]
        if nr < 0xF000:
            return self.all_agg.get(nr & ~AGG_MASK)
        return None


def _add_number(tbl: _LockTable, value: int, reason: str) -> None:
    """Aggregat-Regel: Kategorie < 0xF000 und Aggregat-Bits 0 -> für alle Aggregate sperren."""
    if value < 0xF000 and not value & AGG_MASK:
        tbl.all_agg.setdefault(value, reason)
    else:
        tbl.exact.setdefault(value, reason)


def _build(patterns: list[tuple[str, str]]) -> _LockTable:
    tbl = _LockTable({}, {})
    for pattern, reason in patterns:
        rx = re.compile(pattern)
        hits = [(n, v) for n, v in fw_numbers.NUMBERS.items() if rx.fullmatch(n)]
        if not hits:
            raise RuntimeError(f"Sperrmuster {pattern!r} trifft keinen Firmware-Namen")
        for name, value in hits:
            _add_number(tbl, value, f"{name}: {reason}")
    return tbl


def _header_p_names(nr: int) -> list[str]:
    return [n for n in fw_numbers.NAMES.get(nr, []) if n.startswith("P_")]


def _build_common() -> _LockTable:
    tbl = _build(_CAN_COMMON_PATTERNS)
    for nr in sorted(fw_numbers.PSEUDO_POINTER_PARAMS):
        reason = "Pseudo-Zeiger (void*)1 in Gerätetabelle – Schreiben beschädigt RAM"
        names = _header_p_names(nr)
        label = "/".join(names)
        if not label and nr < 0xF000 and (base_names := _header_p_names(nr & ~AGG_MASK)):
            label = f"AG{((nr & AGG_MASK) >> 8) + 1}|{'/'.join(base_names)}"
        tbl.exact.setdefault(nr, f"{label or f'{nr:#06x}'}: {reason}")
        if nr >= 0xF000 or (names and nr & AGG_MASK):
            continue      # Header-Wert hat bereits Aggregat-Bits -> nur exakt
        base = nr & ~AGG_MASK
        if _header_p_names(base):
            # z. B. AG2|P_HKZYK1 -> P_HKZYK1 in allen Aggregaten sperren (im Zweifel sperren)
            tbl.all_agg.setdefault(base, f"{'/'.join(_header_p_names(base))}: {reason}")
    return tbl


_CAN_COMMON = _build_common()
_CAN_SONDE = _build(_SONDE_PATTERNS)
_CAN_DATABOX = _build(_DATABOX_PATTERNS)

#: LC1 (RS232): gesperrte E-Indizes (0..63) mit Grund.
LC1_LOCKED: dict[int, str] = {
    0: "E(0) Reglernummer/Fabrikmodus",
    1: "E(1) gesperrt",
    2: "E(2) gesperrt",
    **{i: f"E({i}) Kalibrierung/TKmax" for i in range(4, 15)},
    29: "E(29) Kesseltyp",
    **{i: f"E({i}) Zähler" for i in range(48, 52)},
    **{i: f"E({i}) gesperrt" for i in range(54, 59)},
    60: "E(60) gesperrt",
    63: "E(63) gesperrt",
    # Zusätzlich nach Auswertung der LC1-Firmware (LCS9.ARB, LCS1.ARB) – „im Zweifel sperren“:
    3: "E(3) Ebene Werk",
    30: "E(30) Reserve",
    31: "E(31) Sprache",
    32: "E(32) Systemanwendung (ändert die Hydraulik)",
    33: "E(33) Systemanwendung (ändert die Hydraulik)",
    46: "E(46) gesperrt (Firmware-intern)",
    47: "E(47) gesperrt (Firmware-intern)",
    52: "E(52) Störungshistorie",
    53: "E(53) Reparaturhistorie",
    59: "E(59) Störungsflag (setzt/löscht die Firmware, LCS9.ARB:550-557)",
    61: "E(61) Zähler (LCS1 V1.xx, LCS1.ARB:719-720)",
    62: "E(62) Zähler (LCS1 V1.xx, LCS1.ARB:719-720)",
}
LC1_E_MAX = 63
#: F8: Nur Parameter der Ebenen 0 (Betreiber), 1 (Installateur) und 2 (Kesselhersteller) aus
#: LCS1.par sind schreibbar. Ebenen 3..5 und "/" (Reserve) sowie E-Indizes ohne Tabelleneintrag
#: sind gesperrt.
LC1_WRITABLE_LEVELS: frozenset[str] = frozenset({"0", "1", "2"})


# --- Sperren nach Ausgabetyp (OUTPUT_FLAG, Include/Common/MesswertNummern.h:1121-1156) ----------
OUT_DECIMAL = 0
OUT_ONOFF = 1
OUT_YESNO = 2
OUT_SECTOH = 3
OUT_SECTOMIN = 4
OUT_CNTSECMIN = 5
OUT_DATE = 6
OUT_TIME = 7
OUT_TEXTAUSWAHL = 8
OUT_SERVICECODE = 9
OUT_URLAUBVON = 10
OUT_URLAUBBIS = 11
OUT_HEXADECIMAL = 12
OUT_SECTOTIME = 13
OUT_MINTOTIME = 14
OUT_MINVONBIS = 15
OUT_GRAPH = 16
OUT_ZYKLEN = 17
OUT_IPV4 = 18
OUT_TAGMONAT = 19
OUT_ZYKLENZEITEN = 20
OUT_MASK = 0x3F
OUT_RAM = 0x40
OUT_FLASHPTR = 0x80
OUT_RAMSTRING = OUT_RAM | OUT_TEXTAUSWAHL       # 0x48
OUT_FLASHSTRING = OUT_FLASHPTR | OUT_TEXTAUSWAHL  # 0x88

#: Regel: "Was die Firmware nicht über den Bus herausgibt oder nicht prüft, wird nicht schreibbar."
#: Schlüssel = vollständiger outFlag-Wert (wie in der Gerätetabelle) bzw. Wert & OUT_MASK (Bus).
_OUT_TYPE_LOCKS: dict[int, str] = {
    # CheckParameter (os2/net/Messwerte.cpp:369-377): keine Bereichsprüfung für diese Ausgabetypen
    OUT_SECTOH: "Ausgabetyp OUT_SECTOH: keine Bereichsprüfung in der Firmware (CheckParameter)",
    OUT_ZYKLEN: "Ausgabetyp OUT_ZYKLEN: keine Bereichsprüfung in der Firmware (CheckParameter)",
    OUT_ZYKLENZEITEN: "Ausgabetyp OUT_ZYKLENZEITEN: keine Bereichsprüfung in der Firmware (CheckParameter)",
    # ParameterSendenOkay (os2/net/BusOut.cpp:278-294): wird nicht über den Bus gesendet
    OUT_RAMSTRING: "Ausgabetyp OUT_RAMSTRING: wird von der Firmware nicht über den Bus gesendet",
    OUT_IPV4: "Ausgabetyp OUT_IPV4: wird von der Firmware nicht über den Bus gesendet",
    # Zusätzlich (im Zweifel sperren): Servicecode (P_CODE steht ebenfalls auf beiden Ausnahmelisten)
    OUT_SERVICECODE: "Ausgabetyp OUT_SERVICECODE: Servicecode (P_CODE-Ausnahme in CheckParameter)",
    # Zusätzlich: Datum/Zeit/Urlaub – nicht numerisch, eigene Prüfung in der Firmware
    OUT_DATE: "Ausgabetyp OUT_DATE: Datum, nicht numerisch",
    OUT_TIME: "Ausgabetyp OUT_TIME: Uhrzeit, nicht numerisch",
    OUT_URLAUBVON: "Ausgabetyp OUT_URLAUBVON: Urlaub, nicht numerisch",
    OUT_URLAUBBIS: "Ausgabetyp OUT_URLAUBBIS: Urlaub, nicht numerisch",
    # Zusätzlich: Anzeige-Umrechnung (Sekunden -> Minuten/Zeit), im Gateway nicht verifiziert
    OUT_SECTOMIN: "Ausgabetyp OUT_SECTOMIN: Anzeige-Umrechnung s->min, im Gateway nicht verifiziert",
    OUT_CNTSECMIN: "Ausgabetyp OUT_CNTSECMIN: Anzeige-Umrechnung s->min, im Gateway nicht verifiziert",
    OUT_SECTOTIME: "Ausgabetyp OUT_SECTOTIME: Anzeige-Umrechnung s->Zeit, im Gateway nicht verifiziert",
    OUT_MINVONBIS: "Ausgabetyp OUT_MINVONBIS: in der Firmware als TODO markiert",
    OUT_TAGMONAT: "Ausgabetyp OUT_TAGMONAT: Kodierung nicht verifiziert",
}
#: Ausgabetypen, die ohne Bedenken geschrieben werden dürfen (alles andere: gesperrt).
_OUT_TYPE_OK: frozenset[int] = frozenset({
    OUT_DECIMAL, OUT_ONOFF, OUT_YESNO, OUT_TEXTAUSWAHL, OUT_HEXADECIMAL, OUT_MINTOTIME, OUT_GRAPH,
})


def out_type_lock_reason(out_type: int) -> str | None:
    """Sperrgrund für einen Ausgabetyp (outFlag) oder ``None``. Unbekannte Typen sind gesperrt."""
    if not _is_int(out_type) or not 0 <= out_type <= 0xFF:
        return f"ungültiger Ausgabetyp {out_type!r}"
    if reason := _OUT_TYPE_LOCKS.get(out_type):
        return reason
    base = out_type & OUT_MASK
    if reason := _OUT_TYPE_LOCKS.get(base):
        return reason
    if base not in _OUT_TYPE_OK:
        return f"unbekannter Ausgabetyp {out_type:#04x} (im Zweifel gesperrt)"
    return None


def param_lock_reason(bus: str, regler_typ: int | None, key: int, out_type: int | None = None) -> str | None:
    """Grund, falls ein Parameter dauerhaft gesperrt ist, sonst ``None``.

    CAN: ``key`` = Messwertnummer (u16); LC1: ``key`` = E-Index (0..63).
    Unbekannter oder fehlender Reglertyp: Sonden- und Databox-Sperren gelten zusätzlich.
    ``out_type`` (nur CAN): Ausgabetyp (OUT_*) aus dem 0x30/0x38-Detail bzw. der Gerätetabelle;
    gesperrt sind u. a. OUT_SECTOH/OUT_ZYKLEN/OUT_ZYKLENZEITEN (keine Bereichsprüfung) und
    OUT_RAMSTRING/OUT_IPV4 (nicht über den Bus) – siehe ``out_type_lock_reason``.
    """
    reason = _param_lock_reason(bus, regler_typ, key)
    if reason is None and out_type is not None and bus == "can":
        reason = out_type_lock_reason(out_type)
    return reason


def _param_lock_reason(bus: str, regler_typ: int | None, key: int) -> str | None:
    if not _is_int(key):
        return f"ungültiger Schlüssel {key!r}"
    if bus == "lc1":
        if not 0 <= key <= LC1_E_MAX:
            return f"E-Index {key} außerhalb 0..{LC1_E_MAX}"
        if reason := LC1_LOCKED.get(key):
            return reason
        par = lc1_tables.PARAMS.get(key)
        if par is None:
            return f"E({key}) nicht in der Parametertabelle (im Zweifel gesperrt)"
        if par.level not in LC1_WRITABLE_LEVELS:
            name = lc1_tables.LEVEL_TEXT.get(par.level, par.level)
            return f"E({key}) Ebene {par.level} ({name}): nur Ebenen 0..2 sind schreibbar"
        return None
    if bus != "can":
        return f"unbekannter Bus {bus!r}"
    if not 0 <= key <= 0xFFFF:
        return f"Nummer {key!r} außerhalb 0..0xFFFF"
    low = key & 0xFF
    if low < 0x40:
        return f"{key:#06x} ist ein Messwert (M_*), kein Parameter"
    if low >= 0x80:
        return f"{key:#06x} ist eine Testfunktion (T_*), kein Parameter"
    if key >= P_SPEZI:
        return f"{key:#06x}: Spezial-Menüpunkt (≥ P_SPEZI)"
    if reason := _CAN_COMMON.lookup(key):
        return reason
    known = regler_typ is not None and regler_typ in fw_numbers.REGLER_TYPEN
    if regler_typ == REGLER_TYP_SONDE or not known:
        if reason := _CAN_SONDE.lookup(key):
            return reason if known else f"{reason} (Reglertyp unbekannt)"
    if regler_typ == REGLER_TYP_DATABOX or not known:
        if reason := _CAN_DATABOX.lookup(key):
            return reason if known else f"{reason} (Reglertyp unbekannt)"
    return None


# =============================================================================================
# LC1 (RS232)
# =============================================================================================
#: BASIC-Steuerzeichen, die den LC1-Interpreter unterbrechen/steuern (Ctrl-C, XON, XOFF, …).
FORBIDDEN_LC1_BYTES: frozenset[int] = frozenset({0x03, 0x11, 0x13, 0x17, 0x1B})

LC1_CMD_PARAM_LESEN = 48        # '0'
LC1_CMD_PARAM_SCHREIBEN = 49    # '1'
LC1_CMD_ARBEITSDATEN = 50       # '2'
LC1_CMD_STOP = 51               # '3' – hält die Regelung an: VERBOTEN
LC1_CMD_AUSGAENGE = 52          # '4' – steuert Ausgänge: VERBOTEN
LC1_CMD_IDENT = 53              # '5'

ALLOWED_LC1_COMMANDS: dict[int, str] = {
    LC1_CMD_IDENT: "Ident",
    LC1_CMD_ARBEITSDATEN: "Arbeitsdaten",
    LC1_CMD_PARAM_LESEN: "Parameter lesen",
    LC1_CMD_PARAM_SCHREIBEN: "Parameter schreiben",
}


def check_lc1_byte(b: int) -> None:
    if not _is_int(b) or not 0 <= b <= 0xFF:
        raise SafetyViolation(f"LC1: ungültiges Byte {b!r}")
    if b in FORBIDDEN_LC1_BYTES:
        raise SafetyViolation(f"LC1: Steuerzeichen {b:#04x} darf nie gesendet werden")


def check_lc1_command(func: int) -> None:
    """Funktionsbyte = ASCII 48+n; erlaubt nur Ident, Arbeitsdaten, Parameter lesen/schreiben."""
    if not _is_int(func):
        raise SafetyViolation(f"LC1: ungültiges Funktionsbyte {func!r}")
    if func == LC1_CMD_STOP:
        raise SafetyViolation("LC1: Funktion 51 (Regelung anhalten) ist verboten")
    if func == LC1_CMD_AUSGAENGE:
        raise SafetyViolation("LC1: Funktion 52 (Ausgänge steuern) ist verboten")
    if func not in ALLOWED_LC1_COMMANDS:
        raise SafetyViolation(f"LC1: Funktion {func} nicht freigegeben")


def check_lc1_param_write(address: int, value: int) -> None:
    if not _is_int(address) or not 0 <= address <= LC1_E_MAX:
        raise SafetyViolation(f"LC1: Parameteradresse {address!r} außerhalb 0..{LC1_E_MAX}")
    if not _is_int(value) or not 0 <= value <= 0xFFFF:
        raise SafetyViolation(f"LC1: Wert {value!r} außerhalb 0..65535")
    if reason := param_lock_reason("lc1", None, address):
        raise SafetyViolation(f"LC1: E({address}) gesperrt ({reason})")


def check_lc1_tx(data: bytes) -> None:
    """Prüft eine komplette Bytefolge auf verbotene Steuerzeichen."""
    if not isinstance(data, (bytes, bytearray)):
        raise SafetyViolation("LC1: Daten müssen bytes sein")
    for b in data:
        check_lc1_byte(b)


# =============================================================================================
# Schreib-Guards (Modbus -> Gerät)
# =============================================================================================
#: Mindestabstand zwischen zwei Schreibvorgängen desselben Parameters (fest, 10 min).
MIN_WRITE_INTERVAL_S = 600.0


def _resolution(decimals: int) -> float:
    return 0.5 * 10.0 ** (-decimals)


class WriteGuard:
    """Prüft Schreibwünsche und verbucht gesendete Schreibvorgänge (threadsicher).

    ``cfg`` darf zur Laufzeit ausgetauscht werden (Hot-Reload); Zähler bleiben erhalten.
    ``clock`` liefert Sekunden seit Epoche (für Ratenlimit und lokale Mitternacht).
    """

    def __init__(self, cfg: config.SafetyConfig, clock: Callable[[], float] = time.time) -> None:
        self._lock = threading.Lock()
        self._cfg = cfg
        self._clock = clock
        self._last_write: dict[tuple[str, int], float] = {}
        self._day: _dt.date = self._today(clock())
        self._writes_today = 0

    # --- Konfiguration ---
    @property
    def cfg(self) -> config.SafetyConfig:
        return self._cfg

    @cfg.setter
    def cfg(self, cfg: config.SafetyConfig) -> None:
        with self._lock:
            self._cfg = cfg

    # --- Zähler ---
    @staticmethod
    def _today(ts: float) -> _dt.date:
        return _dt.datetime.fromtimestamp(ts).date()   # lokale Zeit

    def _rollover(self, now: float) -> None:
        day = self._today(now)
        if day != self._day:
            self._day = day
            self._writes_today = 0

    @property
    def writes_today(self) -> int:
        with self._lock:
            self._rollover(self._clock())
            return self._writes_today

    @property
    def remaining_budget(self) -> int:
        with self._lock:
            self._rollover(self._clock())
            return max(0, self._cfg.daily_write_budget - self._writes_today)

    # --- Prüfen / Verbuchen ---
    def check(self, device: config.DeviceConfig, param: config.ParamDef, new_value: float,
              current_value: float | None) -> bool:
        """True = senden; False = innerhalb Totband (nicht senden, für den Client trotzdem OK).

        Wirft ``WriteRejected`` (kind "address" bzw. "value").
        """
        with self._lock:
            cfg = self._cfg
            if not cfg.write_enabled:
                raise WriteRejected("address", "Schreiben global deaktiviert")
            if not param.writable:
                raise WriteRejected("address", f"Parameter {param.key:#06x} nicht freigegeben")
            if not param.active:
                raise WriteRejected("address", f"Parameter {param.key:#06x} ist abgewählt")
            if reason := param_lock_reason(device.bus, device.regler_typ, param.key):
                raise WriteRejected("address", f"Parameter {param.key:#06x} gesperrt ({reason})")

            if isinstance(new_value, bool) or not isinstance(new_value, (int, float)):
                raise WriteRejected("value", f"Wert {new_value!r} ist keine Zahl")
            value = float(new_value)
            if not math.isfinite(value):
                raise WriteRejected("value", "Wert ist NaN/unendlich")
            if param.min is not None and value < param.min:
                raise WriteRejected("value", f"Wert {value} < Minimum {param.min}")
            if param.max is not None and value > param.max:
                raise WriteRejected("value", f"Wert {value} > Maximum {param.max}")
            if device.bus == "lc1":
                raw = round(value * 10 ** param.decimals)
                if not 0 <= raw <= 0xFFFF:
                    raise WriteRejected("value", f"LC1-Rohwert {raw} außerhalb 0..65535")

            if current_value is not None and math.isfinite(current_value):
                band = max(param.deadband, _resolution(param.decimals))
                if abs(value - current_value) <= band:
                    return False

            now = self._clock()
            last = self._last_write.get((device.id, param.key))
            if last is not None:
                elapsed = now - last
                # Uhr rückwärts gestellt (elapsed < 0) -> ebenfalls sperren (im Zweifel sperren)
                if elapsed < 0 or elapsed < MIN_WRITE_INTERVAL_S:
                    wait = MIN_WRITE_INTERVAL_S - max(elapsed, 0.0)
                    raise WriteRejected("value", f"Ratenlimit: nächster Schreibvorgang in {wait:.0f} s")
            self._rollover(now)
            if self._writes_today >= cfg.daily_write_budget:
                raise WriteRejected("value", f"Tagesbudget ({cfg.daily_write_budget}) erschöpft")
            return True

    def wait_s(self, device_id: str, key: int) -> float:
        """Sekunden bis zum nächsten erlaubten Schreibvorgang dieses Parameters (0 = jetzt)."""
        with self._lock:
            last = self._last_write.get((device_id, key))
            if last is None:
                return 0.0
            elapsed = self._clock() - last
            if elapsed < 0:
                return MIN_WRITE_INTERVAL_S
            return max(0.0, MIN_WRITE_INTERVAL_S - elapsed)

    def commit(self, device_id: str, key: int) -> None:
        """Verbucht einen tatsächlich gesendeten Schreibvorgang (Ratenlimit + Tagesbudget)."""
        with self._lock:
            now = self._clock()
            self._rollover(now)
            self._last_write[(device_id, key)] = now
            self._writes_today += 1


def verify_readback(expected: float, actual: float, decimals: int) -> bool:
    """Rückmeldung prüfen: Abweichung höchstens eine halbe letzte Stelle."""
    try:
        e, a = float(expected), float(actual)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(e) and math.isfinite(a)):
        return False
    tol = _resolution(decimals)
    # kleiner Zuschlag für Float32-Darstellungsfehler
    return abs(e - a) <= tol * (1 + 1e-6) + 1e-9 * max(1.0, abs(e))
