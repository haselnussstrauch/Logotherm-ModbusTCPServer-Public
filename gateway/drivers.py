"""Gemeinsame Treiber-Schnittstelle für CAN (can_lc2) und RS232 (serial_lc1).

Der Dienst (service.py) besitzt die Hardware allein. Ein Treiber serialisiert intern alle
Zugriffe auf seinen Bus (CAN: immer nur eine offene Anfrage). Poller, Scan, Aufzählung und
Schreiben laufen alle über diese Methoden; der Poller pausiert während Scan/Aufzählung.

Fehlerkonvention:
- `DeviceTimeout`  – Gerät hat nicht (rechtzeitig) geantwortet (CAN: kein Fehlerframe!).
- `ProtocolError`  – Antwort kam, war aber ungültig (CRC, Echo falsch, Format).
- `InterfaceError` – Adapter/Port nicht verfügbar.
- `gateway.errors.SafetyViolation` – darf nie auftreten; Treiber prüfen jeden Sendewunsch über
  `gateway.safety` und werfen dann, statt zu senden.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal, Protocol

from gateway.config import DeviceConfig, ParamDef, ValueDef


class DriverError(Exception):
    pass


class DeviceTimeout(DriverError):
    pass


class ProtocolError(DriverError):
    pass


class InterfaceError(DriverError):
    pass


@dataclass
class DeviceInfo:
    """Ergebnis eines Scans (CAN: Identify, LC1: Befehl 53)."""
    bus: Literal["can", "lc1"]
    address: int | None = None        # CAN-Busadresse
    port: str | None = None           # LC1-Port
    regler_typ: int | None = None     # CAN: _REGLER_TYP aus Identify
    type_name: str = ""               # lesbar, z. B. "LambdaControl 2 Neu" / "LambdaControl 1 (RS232)"
    name: str = ""                    # Kessel-/Gerätename
    version: str | None = None
    serial: int | None = None
    system_nr: int | None = None      # LC1: Systemnummer
    raw: dict = field(default_factory=dict)   # Rohdaten für Diagnose


@dataclass
class ValueInfo:
    """Ein Eintrag der Werteliste (CAN: aus 0x30/0x38, LC1: fest hinterlegt)."""
    kind: Literal["value", "param"]
    key: int                          # CAN: Nummer; LC1: K-Index (value) bzw. E-Index (param)
    name: str
    unit: str = ""
    decimals: int = 0
    vartyp: int = 0x04
    out_type: int | None = None       # CAN: Ausgabetyp (OUT_*) aus dem Detail
    out_textauswahl: bool = False
    min: float | None = None
    max: float | None = None
    default: float | None = None      # Werkseinstellung
    value: float | None = None        # aktueller Wert zum Zeitpunkt der Aufzählung
    tip: str = ""
    level: str = ""                   # LC1: Ebene aus LCS1.par
    supported: bool = True            # numerisch bereitstellbar (config.NUMERIC_VARTYPES)
    lock_reason: str | None = None    # gesetzt = darf nie schreibbar werden (safety)


ProgressCb = Callable[[int, int, str], None]   # (erledigt, gesamt, Text)


class BusDriver(Protocol):
    bus: Literal["can", "lc1"]

    async def open(self) -> None: ...
    async def close(self) -> None: ...

    async def scan(self, progress: ProgressCb | None = None) -> list[DeviceInfo]:
        """CAN: Identify an 0x01..0xFE (ohne eigene Adresse), gedrosselt + passives Mithören.
        LC1: Befehl 53 an allen übergebenen/erkannten Ports."""
        ...

    async def enumerate(self, device: DeviceConfig, progress: ProgressCb | None = None) -> list[ValueInfo]:
        """Werteliste eines Geräts. CAN: 0x30/0x38 mit Nr=0xFFFF (nur auf Knopfdruck!).
        LC1: hinterlegte Tabellen + aktuelle Werte."""
        ...

    async def read_values(self, device: DeviceConfig, values: list[ValueDef]) -> dict[int, float | None]:
        """Messwerte lesen (CAN: 0x20 je Nummer; LC1: ein Befehl 50 für alle K).
        Ergebnis key -> skalierter Wert; None = Timeout für diesen Wert.
        Wirft DeviceTimeout, wenn das Gerät gar nicht antwortet."""
        ...

    async def read_params(self, device: DeviceConfig, params: list[ParamDef]) -> dict[int, float | None]:
        """Parameter-Istwerte lesen (CAN: 0x38 je Nummer; LC1: Befehl 48)."""
        ...

    async def write_param(self, device: DeviceConfig, param: ParamDef, value: float) -> float:
        """Parameter schreiben und die Rückmeldung (Istwert) zurückgeben.
        CAN: 0x08/0x00, Bestätigung 0x0A/0x38. LC1: Befehl 49, danach Befehl 48 zurücklesen.
        Der Aufrufer (service) hat WriteGuard schon geprüft; der Treiber prüft safety erneut."""
        ...

    def stats(self) -> dict:
        """Zähler für Diagnose, mindestens {'errors': int, 'tx': int, 'rx': int, 'timeouts': int}."""
        ...


#: Ruft der Treiber für jeden gesendeten/empfangenen Rahmen auf, wenn der Mitschnitt aktiv ist.
TraceCb = Callable[[str, str], None]   # (Richtung "TX"/"RX"/"ECHO", Text)

AsyncFn = Callable[[], Awaitable[None]]
