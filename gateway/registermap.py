"""Registerbelegung je Unit-ID (aus GatewayConfig berechnet) und CSV-Export.

Belegung je Geräte-Unit-ID:
- Input-Register (FC 04): Messwerte als Float32 auf ``reg``/``reg+1``, Status auf ``1000 + reg//2``,
  optional Int16-Spiegel auf ``2000 + reg//2``, Gerätestatus ab 9000.
  32-Bit-Ganzzahlen (VarTyp LONG/SLONG) zusätzlich exakt als UInt32/Int32 auf ``3000 + reg`` –
  Float32 stellt ganze Zahlen nur bis 2^24 = 16 777 216 exakt dar (Seriennummern, Zähler).
- Holding-Register (FC 03/16): Parameter als Float32, Status, Int16-Spiegel und 32-Bit-Ganzzahl wie
  oben (nur lesbar).
- Diagnose-Unit (``modbus.diag_unit_id``): Input-Register 0..8.

Alle Werte werden bei jedem Lesezugriff frisch aus dem ValueStore gebildet; die Karte selbst
enthält nur die (statische) Struktur und kann per ``rebuild(cfg)`` ersetzt werden (Hot-Reload).
"""

from __future__ import annotations

import csv
import io
import math
import struct
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Literal

from gateway.config import (
    MAX_ENTRIES,
    REG_DEVICE_STATUS,
    REG_INT16,
    REG_STATUS,
    REG_U32,
    REG_VALUES,
    DeviceConfig,
    GatewayConfig,
    ParamDef,
    ValueDef,
    VarTyp,
)
from gateway.devices.names import display_name, nr_text
from gateway.safety import OUT_CNTSECMIN, OUT_MASK, OUT_SECTOH, OUT_SECTOMIN
from gateway.store import ValueStore

Table = Literal["ir", "hr"]
SlotKind = Literal["value", "status", "int16", "u32", "devstatus", "diag"]

NAN_F32 = 0x7FC00000
INT16_NONE = 0x8000
INT16_LIMIT = 32767
U32_NONE = 0xFFFFFFFF          # UInt32: kein Wert
I32_NONE = 0x80000000          # Int32: kein Wert

#: Ausgabetypen, die Sekunden in Stunden/Minuten umrechnen – dort ist der Wert keine Ganzzahl mehr
_TIME_CONVERSIONS = frozenset({OUT_SECTOH, OUT_SECTOMIN, OUT_CNTSECMIN})


# --- Fehler -----------------------------------------------------------------------------------
class RegisterMapError(Exception):
    """Basis der Registerkarten-Fehler."""


class UnknownUnit(RegisterMapError):
    """Unit-ID ist nicht belegt."""


class IllegalAddress(RegisterMapError):
    """Adresse (oder ein Teil des Bereichs) ist nicht belegt -> Modbus-Exception 02."""


# --- Kodierung --------------------------------------------------------------------------------
def encode_float(value: float | None, word_order: str = "ABCD") -> list[int]:
    """Float32 -> zwei 16-Bit-Wörter. Bytes immer Big-Endian; Wortreihenfolge ABCD oder CDAB."""
    if value is None or isinstance(value, float) and math.isnan(value):
        raw = NAN_F32
    else:
        try:
            raw = struct.unpack(">I", struct.pack(">f", value))[0]
        except OverflowError:       # außerhalb des Float32-Bereichs
            raw = NAN_F32
    hi, lo = (raw >> 16) & 0xFFFF, raw & 0xFFFF
    return [hi, lo] if word_order == "ABCD" else [lo, hi]


def decode_float(words: list[int], word_order: str = "ABCD") -> float:
    """Zwei 16-Bit-Wörter -> Float (Umkehrung von encode_float)."""
    hi, lo = (words[0], words[1]) if word_order == "ABCD" else (words[1], words[0])
    return struct.unpack(">f", struct.pack(">I", ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)))[0]


def encode_u32(value: int, word_order: str = "ABCD") -> list[int]:
    value = max(0, min(0xFFFFFFFF, int(value)))
    hi, lo = (value >> 16) & 0xFFFF, value & 0xFFFF
    return [hi, lo] if word_order == "ABCD" else [lo, hi]


def encode_int16(value: float | None, decimals: int) -> int:
    """round(Wert * 10^decimals) als signed int16 (±32767 begrenzt); 0x8000 = kein Wert."""
    if value is None or not math.isfinite(value):
        return INT16_NONE
    scaled = round(value * 10 ** decimals)
    scaled = max(-INT16_LIMIT, min(INT16_LIMIT, scaled))
    return scaled & 0xFFFF


def encode_int32(value: float | None, decimals: int, signed: bool, word_order: str = "ABCD") -> list[int]:
    """round(Wert * 10^decimals) als UInt32 bzw. Int32 (Zweierkomplement), begrenzt.

    Kein Wert: UInt32 0xFFFFFFFF, Int32 0x80000000 (gültig nur mit Status 0)."""
    if value is None or not math.isfinite(value):
        raw = I32_NONE if signed else U32_NONE
    else:
        scaled = round(value * 10 ** decimals)
        if signed:
            raw = max(-0x7FFFFFFF, min(0x7FFFFFFF, scaled)) & 0xFFFFFFFF
        else:
            raw = max(0, min(0xFFFFFFFE, scaled))
    hi, lo = (raw >> 16) & 0xFFFF, raw & 0xFFFF
    return [hi, lo] if word_order == "ABCD" else [lo, hi]


def has_int32(e: ValueDef) -> bool:
    """32-Bit-Ganzzahl ohne Zeitumrechnung -> zusätzlich exakt auf 3000 + reg."""
    return e.vartyp in (VarTyp.LONG, VarTyp.SLONG) and ((e.out_type or 0) & OUT_MASK) not in _TIME_CONVERSIONS


def entry_nr(dev: DeviceConfig | None, e: ValueDef | None) -> str:
    """Nummer für die Registerliste: CAN 0x0103, LC1 K7 bzw. E15."""
    if e is None:
        return ""
    if e.nr is not None:
        return nr_text(e.nr)
    if getattr(e, "e", None) is not None:
        return f"E{e.e}"
    return f"K{e.k}" if e.k is not None else ""


def _u16(value: int) -> int:
    return max(0, min(0xFFFF, int(value)))


# --- Datenstruktur ----------------------------------------------------------------------------
@dataclass(frozen=True)
class Slot:
    """Ein belegter Registerbereich (1 oder 2 Wörter) einer Unit."""
    unit_id: int
    table: Table
    address: int
    width: int
    kind: SlotKind
    name: str
    unit_text: str
    typ: str
    device: DeviceConfig | None
    entry: ValueDef | None            # ValueDef bzw. ParamDef (nur bei kind value/status/int16)
    writable: bool
    read: Callable[[], list[int]]

    @property
    def fc_text(self) -> str:
        if self.table == "ir":
            return "04"
        return "03/16" if self.writable else "03"


@dataclass
class _State:
    cfg: GatewayConfig
    slots: list[Slot]
    words: dict[tuple[int, Table], dict[int, tuple[Slot, int]]]
    units: frozenset[int]


class RegisterMap:
    """Registerkarte. ``store`` darf für reine Struktur-/CSV-Zwecke fehlen (dann nur Metadaten)."""

    def __init__(
        self,
        cfg: GatewayConfig,
        store: ValueStore | None = None,
        budget_remaining: Callable[[], int] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.store = store
        self.budget_remaining = budget_remaining
        self._clock = clock or (getattr(store, "now", None) if store is not None else None) \
            or time.monotonic
        self._state: _State
        self.rebuild(cfg)

    # --- Aufbau ---
    def rebuild(self, cfg: GatewayConfig) -> None:
        """Baut die Karte neu auf (Hot-Reload). Der alte Zustand bleibt bis zum Austausch gültig."""
        slots = self._build_slots(cfg)
        words: dict[tuple[int, Table], dict[int, tuple[Slot, int]]] = {}
        for s in slots:
            tbl = words.setdefault((s.unit_id, s.table), {})
            for off in range(s.width):
                if s.address + off in tbl:
                    raise ValueError(
                        f"Registerkollision Unit {s.unit_id} {s.table} Adresse {s.address + off}")
                tbl[s.address + off] = (s, off)
        units = frozenset({d.unit_id for d in cfg.devices} | {cfg.modbus.diag_unit_id})
        self._state = _State(cfg, slots, words, units)

    @property
    def cfg(self) -> GatewayConfig:
        return self._state.cfg

    @property
    def word_order(self) -> str:
        return self._state.cfg.modbus.word_order

    def _build_slots(self, cfg: GatewayConfig) -> list[Slot]:
        order = cfg.modbus.word_order
        mirror = cfg.modbus.int16_mirror
        store = self.store
        slots: list[Slot] = []

        def add_entries(dev: DeviceConfig, entries: list, table: Table, kind: str) -> None:
            for e in entries:
                if not e.active or e.reg is None:
                    continue
                writable = bool(table == "hr" and getattr(e, "writable", False)
                                and cfg.safety.write_enabled)
                label = display_name(e.name, e.nr) if dev.bus == "can" else (e.name or f"{e.key:#06x}")

                def rd_val(e=e, dev=dev, kind=kind) -> list[int]:
                    v = store.get(dev.id, kind, e.key).value if store else None
                    return encode_float(v, order)

                def rd_status(e=e, dev=dev, kind=kind) -> list[int]:
                    return [int(store.get(dev.id, kind, e.key).status) if store else 1]

                def rd_int(e=e, dev=dev, kind=kind) -> list[int]:
                    v = store.get(dev.id, kind, e.key).value if store else None
                    return [encode_int16(v, e.decimals)]

                slots.append(Slot(dev.unit_id, table, REG_VALUES + e.reg, 2, "value", label, e.unit,
                                  f"Float32 {order}", dev, e, writable, rd_val))
                slots.append(Slot(dev.unit_id, table, REG_STATUS + e.reg // 2, 1, "status",
                                  f"{label} Status", "", "UInt16 (0=ok, 1=Timeout, 2=veraltet)",
                                  dev, e, False, rd_status))
                if has_int32(e):
                    signed = e.vartyp == VarTyp.SLONG

                    def rd_i32(e=e, dev=dev, kind=kind, signed=signed) -> list[int]:
                        v = store.get(dev.id, kind, e.key).value if store else None
                        return encode_int32(v, e.decimals, signed, order)

                    typ = (f"Int32 {order} (0x80000000=kein Wert)" if signed
                           else f"UInt32 {order} (0xFFFFFFFF=kein Wert)")
                    unit_txt = (f"{e.unit} (x10^{e.decimals})" if e.unit else f"x10^{e.decimals}")                         if e.decimals else e.unit
                    slots.append(Slot(dev.unit_id, table, REG_U32 + e.reg, 2, "u32", f"{label} exakt",
                                      unit_txt, typ, dev, e, False, rd_i32))
                if mirror:
                    unit_txt = f"{e.unit} (x10^{e.decimals})" if e.unit else f"x10^{e.decimals}"
                    slots.append(Slot(dev.unit_id, table, REG_INT16 + e.reg // 2, 1, "int16",
                                      f"{label} Int16", unit_txt, "Int16 (0x8000=kein Wert)",
                                      dev, e, False, rd_int))

        for dev in cfg.devices:
            add_entries(dev, dev.values[:MAX_ENTRIES], "ir", "value")
            add_entries(dev, dev.params[:MAX_ENTRIES], "hr", "param")
            slots.extend(self._device_status_slots(dev))
        slots.extend(self._diag_slots(cfg))
        return slots

    def _device_status_slots(self, dev: DeviceConfig) -> list[Slot]:
        store = self.store

        def mk(off: int, name: str, typ: str, fn: Callable[[], int]) -> Slot:
            return Slot(dev.unit_id, "ir", REG_DEVICE_STATUS + off, 1, "devstatus", name, "", typ,
                        dev, None, False, lambda: [_u16(fn())])

        return [
            mk(0, "Gerät online", "UInt16 (0/1)",
               lambda: int(store.device(dev.id).online) if store else 0),
            mk(1, "Letzter Fehlercode", "UInt16 (DeviceError)",
               lambda: int(store.device(dev.id).last_error) if store else 0),
            mk(2, "Schreib-Alarm", "UInt16 (0/1)",
               lambda: int(store.device(dev.id).write_alarm) if store else 0),
            mk(3, "Alter seit letztem OK", "UInt16 (s, max 65535)",
               lambda: store.age_s(dev.id) if store else 0xFFFF),
        ]

    def _diag_slots(self, cfg: GatewayConfig) -> list[Slot]:
        store = self.store
        order = cfg.modbus.word_order
        unit = cfg.modbus.diag_unit_id
        device_ids = [d.id for d in cfg.devices]

        def one(addr: int, name: str, unit_txt: str, fn: Callable[[], int]) -> Slot:
            return Slot(unit, "ir", addr, 1, "diag", name, unit_txt, "UInt16", None, None, False,
                        lambda: [_u16(fn())])

        def two(addr: int, name: str, unit_txt: str, fn: Callable[[], int]) -> Slot:
            return Slot(unit, "ir", addr, 2, "diag", name, unit_txt, f"UInt32 {order}", None, None,
                        False, lambda: encode_u32(fn(), order))

        def budget() -> int:
            if self.budget_remaining is not None:
                return self.budget_remaining()
            used = store.diag.writes_today if store else 0
            return max(0, cfg.safety.daily_write_budget - used)

        def online_count() -> int:
            return sum(1 for i in device_ids if store and store.device(i).online)

        d = (lambda: store.diag) if store else None
        return [
            one(0, "Heartbeat", "", lambda: d().heartbeat if d else 0),
            one(1, "Zykluszeit", "ms", lambda: d().cycle_time_ms if d else 0),
            two(2, "CAN-Fehlerzähler", "", lambda: d().can_error_count if d else 0),
            one(4, "Schreibvorgänge heute", "", lambda: d().writes_today if d else 0),
            one(5, "Verbleibendes Tagesbudget", "", budget),
            two(6, "Uptime", "s", lambda: int(self._clock() - d().started) if d else 0),
            one(8, "Geräte online", "", online_count),
        ]

    # --- Abfragen ---
    def slots(self) -> list[Slot]:
        return list(self._state.slots)

    def units(self) -> frozenset[int]:
        return self._state.units

    def has_unit(self, unit_id: int) -> bool:
        return unit_id in self._state.units

    def lookup(self, unit_id: int, table: Table, address: int) -> tuple[Slot, int] | None:
        """(Slot, Wort-Offset) zu einer Adresse oder None."""
        return self._state.words.get((unit_id, table), {}).get(address)

    def read(self, unit_id: int, table: Table, address: int, count: int) -> list[int]:
        """Liest ``count`` Wörter ab ``address``.

        Raises:
            UnknownUnit: Unit-ID nicht belegt.
            IllegalAddress: mindestens ein Register des Bereichs ist nicht belegt
                (Lücke, Tabelle existiert nicht) oder der Bereich beginnt/endet mitten in einem
                Float32-/u32-Paar.
        """
        st = self._state
        if unit_id not in st.units:
            raise UnknownUnit(unit_id)
        tbl = st.words.get((unit_id, table))
        if not tbl or count < 1:
            raise IllegalAddress(f"{table} {address}+{count}")
        entries: list[tuple[Slot, int]] = []
        for a in range(address, address + count):
            hit = tbl.get(a)
            if hit is None:
                raise IllegalAddress(f"{table} Adresse {a} nicht belegt")
            entries.append(hit)
        first, last = entries[0], entries[-1]
        if first[1] != 0 or last[1] != last[0].width - 1:
            raise IllegalAddress(f"{table} {address}+{count} zerschneidet ein Float/u32-Paar")
        cache: dict[int, list[int]] = {}
        out: list[int] = []
        for slot, off in entries:
            w = cache.get(id(slot))
            if w is None:
                w = cache[id(slot)] = slot.read()
            out.append(w[off])
        return out

    def param_at(self, unit_id: int, address: int) -> tuple[DeviceConfig, ParamDef] | None:
        """Aktiver Parameter, dessen Float-Paar bei ``address`` BEGINNT (sonst None)."""
        hit = self.lookup(unit_id, "hr", address)
        if hit is None:
            return None
        slot, off = hit
        if slot.kind != "value" or off != 0 or slot.device is None or slot.entry is None:
            return None
        return slot.device, slot.entry  # type: ignore[return-value]


# --- CSV --------------------------------------------------------------------------------------
CSV_HEADER = ["Unit-ID", "Register", "FC", "Gerät", "Nr", "Name", "Einheit", "Typ", "Schreibrecht",
              "Hinweis"]
WRITE_HINT = "Schreiben nur per FC 16, genau 2 Register pro Anfrage"


def _csv_rows(cfg: GatewayConfig) -> Iterator[list[str]]:
    rm = RegisterMap(cfg)
    for s in sorted(rm.slots(), key=lambda s: (s.unit_id, s.table != "ir", s.address)):
        device = s.device.id if s.device else "Diagnose"
        hint = WRITE_HINT if s.writable else ""
        if s.kind == "value" and s.entry is not None and has_int32(s.entry) and not s.writable:
            hint = f"ab 16 777 216 gerundet – exakt auf Register {REG_U32 + s.entry.reg}"
        elif s.kind == "value" and s.entry is not None and has_int32(s.entry):
            hint += f"; exakt lesbar auf Register {REG_U32 + s.entry.reg}"
        yield [str(s.unit_id), str(s.address), s.fc_text, device, entry_nr(s.device, s.entry), s.name,
               s.unit_text, s.typ, "ja" if s.writable else "nein", hint]


def export_csv(cfg: GatewayConfig) -> str:
    """Registerliste als CSV (Semikolon, CRLF). Beginnt mit einem BOM (U+FEFF), damit Excel die
    Datei als UTF-8 erkennt; nicht noch einmal mit utf-8-sig kodieren."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(CSV_HEADER)
    w.writerows(_csv_rows(cfg))
    return "﻿" + buf.getvalue()
