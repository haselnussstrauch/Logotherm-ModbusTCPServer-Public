"""Anzeigenamen für CAN-Messwerte/-Parameter, wenn das Gerät keinen Text liefert.

Manche Nummern haben in der Firmware keinen Anzeigetext; 0x30/0x38 liefert dann einen leeren
Namen und die Registerliste zeigte nur die Nummer (z. B. „0x060d“). Der Ersatzname nimmt das
Symbol aus der Firmware-Nummerntabelle (``MesswertNummern.h``), z. B. „M_O2OFFSET (0x060D)“.
"""

from __future__ import annotations

import re

from gateway.devices.fw_numbers import NAMES

_HEX_ONLY = re.compile(r"^\s*0x[0-9a-fA-F]{1,4}\s*$")


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
