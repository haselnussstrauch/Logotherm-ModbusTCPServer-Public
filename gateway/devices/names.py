"""Anzeigenamen für CAN-Messwerte/-Parameter, wenn das Gerät keinen Text liefert.

Manche Nummern haben in der Firmware keinen Anzeigetext; 0x30/0x38 liefert dann einen leeren
Namen und die Registerliste zeigte nur die Nummer (z. B. „0x060d“). Der Ersatzname nimmt das
Symbol aus der Firmware-Nummerntabelle (``MesswertNummern.h``), z. B. „M_O2OFFSET (0x060D)“.
"""

from __future__ import annotations

import re

from gateway.devices.fw_numbers import NAMES

_HEX_ONLY = re.compile(r"^\s*0x[0-9a-fA-F]{1,4}\s*$")


#: Feste Texte der Firmware-Ausgabeklasse eIO_OnOff (os2/GUI/GUI Parameterausgabe.cpp:131-133)
OUT_ONOFF = 1
OUT_YESNO = 2
FIXED_TEXTS: dict[int, dict[int, str]] = {OUT_ONOFF: {0: "AUS", 1: "EIN"}, OUT_YESNO: {0: "NEIN", 1: "JA"}}


def fixed_texts(out_type: int | None) -> dict[int, str] | None:
    return dict(FIXED_TEXTS[out_type & 0x3F]) if out_type is not None and (out_type & 0x3F) in FIXED_TEXTS else None


def value_text(texts: dict[int, str] | None, value: float | None) -> str | None:
    """Text zum Wert, wenn es eine Auswahl gibt und der Wert eine passende Ganzzahl ist."""
    if not texts or value is None:
        return None
    try:
        iv = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return texts.get(iv) if iv == value else None


def texts_hint(texts: dict[int, str] | None, limit: int = 12) -> str:
    """Kurzform für die Registerliste: ``0=AUS; 1=EIN``."""
    if not texts:
        return ""
    items = sorted(texts.items())
    s = "; ".join(f"{k}={v}" for k, v in items[:limit])
    return s + (f"; … ({len(items)} Texte)" if len(items) > limit else "")


def nr_text(nr: int) -> str:
    """Nummer wie in gateway.yaml: 0x060D."""
    return f"0x{nr:04X}"


def is_placeholder(name: str | None) -> bool:
    """Leer oder nur eine Hex-Nummer (früherer Ersatzname)."""
    return not name or not name.strip() or bool(_HEX_ONLY.match(name))


def fallback_name(nr: int) -> str:
    syms = NAMES.get(nr)
    return f"{syms[0]} ({nr_text(nr)})" if syms else nr_text(nr)


def display_name(name: str | None, nr: int | None) -> str:
    """Gerätename, sonst Firmware-Symbol mit Nummer."""
    if nr is None:
        return (name or "").strip()
    return name.strip() if not is_placeholder(name) else fallback_name(nr)  # type: ignore[union-attr]
