"""Gemeinsame Ausnahmen (von safety, modbus_server, Treibern genutzt)."""

from __future__ import annotations

from typing import Literal


class SafetyViolation(Exception):
    """Ein Sendeversuch verletzt die fest eingebauten Sicherheitsregeln. Darf nie gesendet werden."""


class WriteRejected(Exception):
    """Ein Schreibwunsch (z. B. via Modbus FC 16) wird abgelehnt.

    kind="address" -> Modbus-Exception 02 (Adresse ungültig/nicht freigegeben/gesperrt/Schreiben aus)
    kind="value"   -> Modbus-Exception 03 (außerhalb Min/Max, Ratenlimit, Tagesbudget)
    """

    def __init__(self, kind: Literal["address", "value"], reason: str) -> None:
        super().__init__(reason)
        self.kind = kind
        self.reason = reason
