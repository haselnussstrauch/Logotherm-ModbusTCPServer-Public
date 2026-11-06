"""Wertespeicher: letzte Werte, Zeitstempel, Status je Wert und Gerät, Diagnosezähler.

Threadsicher (Modbus-Server, Poller und Web laufen im selben asyncio-Loop, der Launcher bzw.
Tests evtl. in anderen Threads).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Literal

Kind = Literal["value", "param"]


class ValueStatus(IntEnum):
    OK = 0
    TIMEOUT = 1
    STALE = 2


class DeviceError(IntEnum):
    """Code für 'letzter Fehler' im Gerätestatus-Block."""
    NONE = 0
    TIMEOUT = 1
    PROTOCOL = 2
    WRITE_MISMATCH = 3      # Rückmeldung weicht vom Sollwert ab
    INTERFACE = 4           # Adapter/Port nicht verfügbar


@dataclass
class ValueState:
    value: float | None = None
    status: ValueStatus = ValueStatus.TIMEOUT   # noch nie gelesen = Timeout
    ts: float = 0.0                             # time.monotonic() des letzten OK


@dataclass
class DeviceState:
    online: bool = False
    last_error: DeviceError = DeviceError.NONE
    last_error_text: str = ""
    write_alarm: bool = False
    last_ok: float = 0.0


@dataclass
class Diagnostics:
    heartbeat: int = 0           # zählt je Poll-Zyklus hoch (16 Bit, Überlauf)
    cycle_time_ms: int = 0
    can_error_count: int = 0
    writes_today: int = 0
    started: float = field(default_factory=time.monotonic)


class ValueStore:
    def __init__(self, stale_after_s: float = 60.0, clock=time.monotonic) -> None:
        self._lock = threading.Lock()
        self._values: dict[tuple[str, Kind, int], ValueState] = {}
        self._devices: dict[str, DeviceState] = {}
        self.diag = Diagnostics()
        self.stale_after_s = stale_after_s
        self._clock = clock

    def now(self) -> float:
        """Uhr des Stores (monotonic, in Tests austauschbar)."""
        return self._clock()

    # --- Werte ---
    def set_value(self, device_id: str, kind: Kind, key: int, value: float) -> None:
        with self._lock:
            self._values[(device_id, kind, key)] = ValueState(value, ValueStatus.OK, self._clock())

    def set_timeout(self, device_id: str, kind: Kind, key: int) -> None:
        """Wert nicht gelesen: letzten Wert behalten, Status Timeout."""
        with self._lock:
            st = self._values.setdefault((device_id, kind, key), ValueState())
            st.status = ValueStatus.TIMEOUT

    def get(self, device_id: str, kind: Kind, key: int) -> ValueState:
        with self._lock:
            st = self._values.get((device_id, kind, key))
            if st is None:
                return ValueState()
            status = st.status
            if status == ValueStatus.OK and self._clock() - st.ts > self.stale_after_s:
                status = ValueStatus.STALE
            return ValueState(st.value, status, st.ts)

    # --- Geräte ---
    def device(self, device_id: str) -> DeviceState:
        with self._lock:
            return self._devices.setdefault(device_id, DeviceState())

    def set_online(self, device_id: str) -> None:
        """Abruf erfolgreich. Der Fehlercode beschreibt den letzten Abruf und geht auf 0 zurück;
        ein Schreibfehler bleibt stehen (gehört zum Schreib-Alarm)."""
        with self._lock:
            d = self._devices.setdefault(device_id, DeviceState())
            d.online, d.last_ok = True, self._clock()
            if d.last_error != DeviceError.WRITE_MISMATCH:
                d.last_error, d.last_error_text = DeviceError.NONE, ""

    def set_error(self, device_id: str, err: DeviceError, text: str = "", offline: bool = True) -> None:
        with self._lock:
            d = self._devices.setdefault(device_id, DeviceState())
            d.last_error, d.last_error_text = err, text
            if offline:
                d.online = False
            if err == DeviceError.WRITE_MISMATCH:
                d.write_alarm = True

    def age_s(self, device_id: str) -> int:
        d = self.device(device_id)
        return 0xFFFF if not d.last_ok else min(0xFFFF, int(self._clock() - d.last_ok))
