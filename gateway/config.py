"""Konfigurationsmodell (gateway.yaml).

Zentrale Regeln:
- Jeder bereitgestellte Wert hat eine *feste* Registeradresse (`reg`), die in der YAML steht.
  Abgewählte Werte bleiben als `active: false` stehen und reservieren ihre Adresse weiter.
  Neue Werte werden hinten angehängt. Neu durchnummerieren nur explizit (`renumber_registers`).
- Der LC1-Port steht nur am Gerät; `lc1:` enthält nur Defaults.
- Nur numerische VarTypen werden bereitgestellt.
- Unit-IDs: 1..246, eindeutig; 247 (Default) ist der Diagnose-Block; LC1 ab 101.
"""

from __future__ import annotations

import logging
import os
import sys
from enum import IntEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

log = logging.getLogger(__name__)

# --- Registerbereiche je Unit-ID ------------------------------------------------------------
MAX_ENTRIES = 500            # Werte pro Tabelle (IR bzw. HR) und Gerät
REG_VALUES = 0               # Float32-Werte: reg 0, 2, 4, … 998
REG_STATUS = 1000            # Status je Wert: 1000 + reg//2
REG_INT16 = 2000             # Int16-Spiegel: 2000 + reg//2
REG_U32 = 3000               # exakte 32-Bit-Ganzzahl für LONG/SLONG-Werte: 3000 + reg (2 Register)
REG_DEVICE_STATUS = 9000     # Gerätestatus-Block (nur IR)
UNIT_ID_MIN, UNIT_ID_MAX = 1, 246
LC1_UNIT_ID_START = 101


class VarTyp(IntEnum):
    """VarTyp-Codes laut MesswertNummern.h (LambdaControl_2_1.29)."""
    CHAR = 0x00
    SHORT = 0x01
    LONG = 0x02
    FLOAT = 0x04
    DOUBLE = 0x05
    DATUM = 0x07
    ZEIT = 0x08
    BINARY = 0x09
    ZYKLEN = 0x0A
    URLAUB = 0x0B
    STRING = 0x0F
    SOMMER = 0x10
    FUEHLER = 0x11
    SCHAR = 0x80
    SSHORT = 0x81
    SLONG = 0x82


#: VarTypen, die als Float32 bereitgestellt werden (F1, Punkt 3).
NUMERIC_VARTYPES: frozenset[int] = frozenset({
    VarTyp.CHAR, VarTyp.SHORT, VarTyp.LONG, VarTyp.FLOAT, VarTyp.SOMMER, VarTyp.FUEHLER,
    VarTyp.SCHAR, VarTyp.SSHORT, VarTyp.SLONG,
})


def _default_data_dir() -> Path:
    if env := os.environ.get("GATEWAY_DATA_DIR"):
        return Path(env)
    if sys.platform == "win32":
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "LogothermGateway"
    return Path("/data")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# --- Abschnitte -----------------------------------------------------------------------------
class ServiceConfig(_Model):
    data_dir: str = "auto"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    def resolved_data_dir(self) -> Path:
        return _default_data_dir() if self.data_dir == "auto" else Path(self.data_dir)


class SafetyConfig(_Model):
    # Sperrlisten, Sende-Whitelist und 10-min-Abstand sind fest im Code (safety.py).
    write_enabled: bool = False
    daily_write_budget: int = Field(50, ge=0, le=1000)


class CanTiming(_Model):
    request_timeout_ms: int = Field(250, ge=50, le=5000)
    min_gap_ms: int = Field(20, ge=20, le=1000)   # < 20 ms nicht zulässig (TX-Queue der Geräte)
    retries: int = Field(1, ge=0, le=3)


class CanConfig(_Model):
    enabled: bool = False
    interface: Literal["socketcan", "gs_usb", "pcan", "virtual"] = "gs_usb"
    channel: str = "0"
    # TODO(vor Ort): Laufen ältere LC2-HW1 mit 20 kbit/s?
    bitrate: Literal[50000, 20000] = 50000
    gateway_address: int = Field(0xA0, ge=0x01, le=0xFE)
    timing: CanTiming = CanTiming()
    # TODO(InnoMaker): VID/PID, Firmware (3.3 gesperrt, 3.4.3 bevorzugt); Normal-Modus statt Loopback.


class Lc1Defaults(_Model):
    byte_timeout_s: float = Field(2.0, gt=0, le=10)   # Regler-Timeout zwischen zwei Bytes: 2 s
    line_timeout_s: float = Field(3.0, gt=0, le=30)


class ModbusConfig(_Model):
    bind: str = "0.0.0.0"
    port: int = Field(502, ge=1, le=65535)
    allow: list[str] = []                 # IP/CIDR-Allowlist; leer = alle (Warnung!)
    word_order: Literal["ABCD", "CDAB"] = "ABCD"
    int16_mirror: bool = False
    diag_unit_id: int = Field(247, ge=1, le=247)


class WebConfig(_Model):
    bind: str = "127.0.0.1"
    port: int = Field(8080, ge=1, le=65535)
    password_hash: str | None = None


class PollConfig(_Model):
    interval_s: float = Field(10.0, ge=1, le=3600)
    # Parameter ändern sich selten; 0x38 ist ein Multiframe je Parameter -> seltener lesen.
    param_interval_s: float = Field(60.0, ge=1, le=86400)
    stale_after_s: float = Field(60.0, ge=1, le=86400)


class HistoryConfig(_Model):
    """Aufzeichnung der Werte (gateway/history.py): eine SQLite-Datei je Tag im Datenordner."""
    enabled: bool = True
    interval_s: float = Field(60.0, ge=5, le=3600)       # Abstand der gespeicherten Werte
    retention_days: int = Field(14, ge=1, le=3650)       # ältere Tage werden automatisch gelöscht


# --- Werte und Geräte -------------------------------------------------------------------------
class ValueDef(_Model):
    """Ein Messwert (Input-Register). CAN: `nr`, LC1: `k` (1..35)."""
    nr: int | None = Field(None, ge=0, le=0xFFFF)
    k: int | None = Field(None, ge=1, le=35)
    reg: int | None = Field(None, ge=0, le=2 * (MAX_ENTRIES - 1))
    active: bool = True                   # false = abgewählt, Adresse bleibt reserviert
    dashboard: bool = False               # auf der Startseite (Dashboard) anzeigen
    name: str = ""
    unit: str = ""
    decimals: int = Field(0, ge=0, le=6)
    vartyp: int = VarTyp.FLOAT
    out_textauswahl: bool = False         # Wert ist Index einer Textauswahl
    out_type: int | None = Field(None, ge=0, le=0xFF)   # CAN: Ausgabetyp (OUT_*) aus 0x30/0x38
    texts: dict[int, str] | None = None   # Auswahltexte (Wert -> Text), vom Gerät bzw. fest
    min: float | None = None
    max: float | None = None

    @field_validator("reg")
    @classmethod
    def _even(cls, v: int | None) -> int | None:
        if v is not None and v % 2:
            raise ValueError("reg muss gerade sein (Float32 belegt 2 Register)")
        return v

    @property
    def key(self) -> int:
        """Gerätelokaler Schlüssel: CAN-Nummer bzw. LC1-Index."""
        k = self.nr if self.nr is not None else (self.k if self.k is not None else getattr(self, "e", None))
        assert k is not None
        return k


class ParamDef(ValueDef):
    """Ein Parameter (Holding-Register). CAN: `nr`, LC1: `e` (0..63)."""
    e: int | None = Field(None, ge=0, le=63)
    k: None = None
    writable: bool = False
    deadband: float = Field(0.0, ge=0)
    default: float | None = None


class DeviceConfig(_Model):
    id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,40}$")
    bus: Literal["can", "lc1"]
    address: int | None = Field(None, ge=0x01, le=0xFE)   # nur CAN
    port: str | None = None                                # nur LC1
    # Prüfsumme K36 erst ab LCS9 (LCS9.ARB:2); auto = an der 39. Zeile erkennen
    checksum: Literal["auto", "on", "off"] = "auto"        # nur LC1
    regler_typ: int | None = Field(None, ge=0, le=0xFFFF)
    name: str = ""
    serial: int | None = None
    version: str | None = None
    unit_id: int = Field(ge=UNIT_ID_MIN, le=UNIT_ID_MAX)
    enabled: bool = True
    values: list[ValueDef] = []
    params: list[ParamDef] = []

    @model_validator(mode="after")
    def _check(self) -> DeviceConfig:
        if self.bus == "can":
            if self.address is None:
                raise ValueError(f"{self.id}: CAN-Gerät braucht 'address'")
            if self.port is not None:
                raise ValueError(f"{self.id}: 'port' gibt es nur bei LC1")
            for v in [*self.values, *self.params]:
                if v.nr is None or v.k is not None or getattr(v, "e", None) is not None:
                    raise ValueError(f"{self.id}: CAN-Werte brauchen 'nr' (nicht k/e)")
        else:
            if not self.port:
                raise ValueError(f"{self.id}: LC1-Gerät braucht 'port'")
            if self.address is not None:
                raise ValueError(f"{self.id}: 'address' gibt es nur bei CAN")
            for v in self.values:
                if v.k is None or v.nr is not None:
                    raise ValueError(f"{self.id}: LC1-Messwerte brauchen 'k'")
            for p in self.params:
                if p.e is None or p.nr is not None:
                    raise ValueError(f"{self.id}: LC1-Parameter brauchen 'e'")
        for table, entries in (("values", self.values), ("params", self.params)):
            if len(entries) > MAX_ENTRIES:
                raise ValueError(f"{self.id}.{table}: höchstens {MAX_ENTRIES} Einträge")
            regs = [e.reg for e in entries if e.reg is not None]
            if len(regs) != len(set(regs)):
                raise ValueError(f"{self.id}.{table}: doppelte Registeradresse")
            keys = [e.key for e in entries]
            if len(keys) != len(set(keys)):
                raise ValueError(f"{self.id}.{table}: Wert mehrfach eingetragen")
            for e in entries:
                if e.active and e.vartyp not in NUMERIC_VARTYPES:
                    raise ValueError(
                        f"{self.id}.{table}: {e.key:#06x} hat nicht-numerischen VarTyp "
                        f"{e.vartyp:#04x} und kann nicht bereitgestellt werden")
        return self


class GatewayConfig(_Model):
    version: Literal[1] = 1
    service: ServiceConfig = ServiceConfig()
    safety: SafetyConfig = SafetyConfig()
    can: CanConfig = CanConfig()
    lc1: Lc1Defaults = Lc1Defaults()
    modbus: ModbusConfig = ModbusConfig()
    web: WebConfig = WebConfig()
    poll: PollConfig = PollConfig()
    history: HistoryConfig = HistoryConfig()
    devices: list[DeviceConfig] = []

    @model_validator(mode="after")
    def _check(self) -> GatewayConfig:
        ids = [d.id for d in self.devices]
        if len(ids) != len(set(ids)):
            raise ValueError("Geräte-IDs müssen eindeutig sein")
        seen: dict[int, str] = {}
        for d in self.devices:
            if d.unit_id == self.modbus.diag_unit_id:
                raise ValueError(f"{d.id}: Unit-ID {d.unit_id} ist der Diagnose-Block")
            if d.unit_id in seen:
                raise ValueError(f"Unit-ID {d.unit_id} doppelt: {seen[d.unit_id]} und {d.id}")
            seen[d.unit_id] = d.id
        addrs = [d.address for d in self.devices if d.bus == "can"]
        if len(addrs) != len(set(addrs)):
            raise ValueError("CAN-Adresse doppelt")
        if self.can.gateway_address in addrs:
            raise ValueError("Gateway-Adresse kollidiert mit einem Gerät")
        ports = [d.port for d in self.devices if d.bus == "lc1"]
        if len(ports) != len(set(ports)):
            raise ValueError("LC1-Port doppelt")
        # Schreibfreigabe gesperrter Parameter ist ein Konfigurationsfehler.
        for d in self.devices:
            for p in d.params:
                if p.writable:
                    from gateway import safety  # spät importieren: safety importiert config
                    reason = safety.param_lock_reason(d.bus, d.regler_typ, p.key, out_type=p.out_type)
                    if reason:
                        raise ValueError(f"{d.id}: Parameter {p.key:#06x} ist gesperrt ({reason})")
        return self

    def device(self, device_id: str) -> DeviceConfig:
        for d in self.devices:
            if d.id == device_id:
                return d
        raise KeyError(device_id)

    def device_by_unit(self, unit_id: int) -> DeviceConfig | None:
        return next((d for d in self.devices if d.unit_id == unit_id), None)


# --- Hilfsfunktionen --------------------------------------------------------------------------
def suggest_unit_id(cfg: GatewayConfig, bus: str, address: int | None = None) -> int:
    """Default-Unit-ID (F1, Punkt 4): CAN-Busadresse, wenn 1..246 und frei; LC1 ab 101."""
    used = {d.unit_id for d in cfg.devices} | {cfg.modbus.diag_unit_id}
    if bus == "can" and address is not None and UNIT_ID_MIN <= address <= UNIT_ID_MAX \
            and address not in used:
        return address
    start = LC1_UNIT_ID_START if bus == "lc1" else (address or UNIT_ID_MIN)
    for cand in [*range(start, UNIT_ID_MAX + 1), *range(UNIT_ID_MIN, start)]:
        if cand not in used:
            return cand
    raise ValueError("Keine freie Unit-ID mehr")


def assign_registers(entries: list[ValueDef]) -> None:
    """Vergibt Adressen an aktive Werte ohne Adresse, hinten angehängt.

    Bestehende Adressen (auch von inaktiven = abgewählten Werten) bleiben reserviert. Inaktive
    Einträge ohne Adresse (z. B. aus einer Aufzählung, nie ausgewählt) bekommen keine Adresse.
    """
    used = [e.reg for e in entries if e.reg is not None]
    nxt = (max(used) + 2) if used else 0
    for e in entries:
        if e.reg is None and e.active:
            if nxt > 2 * (MAX_ENTRIES - 1):
                raise ValueError("Registerbereich voll")
            e.reg = nxt
            nxt += 2


def renumber_registers(entries: list[ValueDef]) -> list[ValueDef]:
    """Nur auf ausdrücklichen Knopfdruck: inaktive Einträge entfernen, lückenlos neu vergeben.
    ACHTUNG: verschiebt Registeradressen – die Leittechnik muss angepasst werden."""
    kept = sorted((e for e in entries if e.active), key=lambda e: e.reg if e.reg is not None else 1 << 30)
    for i, e in enumerate(kept):
        e.reg = 2 * i
    return kept


def assign_all_registers(cfg: GatewayConfig) -> None:
    for d in cfg.devices:
        assign_registers(d.values)
        assign_registers(d.params)


def load_config(path: str | Path) -> GatewayConfig:
    """Lädt und validiert gateway.yaml; fehlende Registeradressen werden vergeben."""
    path = Path(path)
    if not path.exists():
        log.warning("Konfiguration %s fehlt, verwende Defaults", path)
        return GatewayConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = GatewayConfig.model_validate(raw)
    assign_all_registers(cfg)
    if not cfg.modbus.allow:
        log.warning("Modbus-IP-Allowlist ist leer: JEDER Client im Netz darf zugreifen!")
    return cfg


# Feld -> Anzahl Hex-Stellen beim Speichern
_HEX_KEYS = {"nr": 4, "regler_typ": 4, "address": 2, "gateway_address": 2}


class _Dumper(yaml.SafeDumper):
    pass


def save_config(cfg: GatewayConfig, path: str | Path) -> None:
    """Schreibt gateway.yaml atomar (tmp + replace). Nummern/Adressen als Hex."""
    path = Path(path)
    data = cfg.model_dump(mode="python", exclude_none=True)

    def hexify(o):
        if isinstance(o, dict):
            return {k: (_Hex(v, _HEX_KEYS[k]) if k in _HEX_KEYS and isinstance(v, int) else hexify(v))
                    for k, v in o.items()}
        if isinstance(o, list):
            return [hexify(x) for x in o]
        if isinstance(o, IntEnum):
            return int(o)
        return o

    text = yaml.dump(hexify(data), Dumper=_Dumper, sort_keys=False, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class _Hex:
    def __init__(self, value: int, digits: int) -> None:
        self.text = f"0x{value:0{digits}X}"


_Dumper.add_representer(_Hex, lambda d, v: d.represent_scalar("tag:yaml.org,2002:int", v.text))
