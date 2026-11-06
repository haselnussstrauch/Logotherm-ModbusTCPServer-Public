"""BusDriver für LambdaControl 1 über RS232 (ein COM-Port je Regler, Regler ist reiner Slave).

- Je LC1-Gerät (``DeviceConfig.port``) ein eigener Port mit eigener Sperre; blockierende
  pyserial-Aufrufe laufen in ``asyncio.to_thread``.
- Port wird bei Bedarf geöffnet und nach Fehlern (Abstecken) neu geöffnet.
- Port-Strings: ``COM5``, ``/dev/serial/by-id/...``, ``socket://host:port`` (pyserial-URL) und
  ``sim://name`` (Simulator im Prozess). Tests übergeben ``port_factory``.
- Skalierung Parameter: Rohwert (0..65535) = Wert × 10**decimals (decimals aus der ParamDef, wie
  im WriteGuard; Abweichungen zur hinterlegten Tabelle werden gewarnt).
"""

from __future__ import annotations

import asyncio
import glob
import logging
import math
import os
import sys
import threading
from dataclasses import dataclass, field
from typing import Callable, Literal

from gateway import safety
from gateway.config import DeviceConfig, GatewayConfig, ParamDef, ValueDef, VarTyp
from gateway.devices import lc1_tables
from gateway.drivers import (DeviceInfo, DeviceTimeout, InterfaceError, ProgressCb, ProtocolError,
                             TraceCb, ValueInfo)
from gateway.errors import SafetyViolation
from gateway.serial_lc1.protocol import Lc1Ident, Lc1Protocol, Lc1WorkData, Lc1WriteUnclear, PortLike

log = logging.getLogger(__name__)

BAUDRATE = 9600
TYPE_NAME = "LambdaControl 1 (RS232)"
FTDI_VID, FTDI_PID = 0x0403, 0x6001
#: Modemleitungen nach dem Öffnen. TODO(vor Ort): Die DOS-Software setzt nach dem Öffnen
#: ``OUT 1018,2`` (MCR: RTS an, DTR aus, D-Lcs-alt/LCS17.BAS:792/829) – falls kein Echo kommt,
#: DTR/RTS-Varianten prüfen (Kabel/Pegelwandler).
DTR_ON = True
RTS_ON = True

PortFactory = Callable[[str], PortLike]


def default_port_factory(port: str) -> PortLike:
    """Öffnet einen echten Port (9600 8N1, kein Handshake, DTR an) oder einen Simulator."""
    if port.startswith("sim://"):
        from gateway.serial_lc1.simulator import get_simulator
        return get_simulator(port)
    try:
        import serial
    except ImportError as exc:  # pragma: no cover
        raise InterfaceError("pyserial fehlt") from exc
    try:
        ser = serial.serial_for_url(port, do_not_open=True, baudrate=BAUDRATE, bytesize=serial.EIGHTBITS,
                                    parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                                    xonxoff=False, rtscts=False, dsrdtr=False, timeout=0.1,
                                    write_timeout=2.0)
        ser.dtr = DTR_ON
        ser.rts = RTS_ON
        ser.open()
    except (serial.SerialException, OSError, ValueError) as exc:
        raise InterfaceError(f"LC1-Port {port} nicht verfügbar: {exc}") from exc
    return ser


def list_serial_ports() -> list[dict]:
    """Serielle Ports für die Oberfläche. FTDI (0403:6001, z. B. DIGITUS DA-70156) zuerst.
    Unter Linux zusätzlich der stabile Pfad unter /dev/serial/by-id."""
    try:
        from serial.tools import list_ports
    except ImportError:  # pragma: no cover
        return []
    by_id: dict[str, str] = {}
    if sys.platform.startswith("linux"):
        for link in glob.glob("/dev/serial/by-id/*"):
            try:
                by_id[os.path.realpath(link)] = link
            except OSError:
                pass
    out = []
    for p in list_ports.comports():
        ftdi = p.vid == FTDI_VID and p.pid == FTDI_PID
        out.append({
            "device": p.device, "description": p.description or "", "hwid": p.hwid or "",
            "vid": p.vid, "pid": p.pid, "serial_number": p.serial_number,
            "manufacturer": p.manufacturer, "ftdi": ftdi,
            "by_id": by_id.get(os.path.realpath(p.device)) if by_id else None,
        })
    out.sort(key=lambda d: (not d["ftdi"], d["device"]))
    return out


@dataclass
class _PortState:
    name: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    port: PortLike | None = None
    proto: Lc1Protocol | None = None
    last_error: str = ""
    ident: Lc1Ident | None = None


class Lc1Driver:
    bus: Literal["lc1"] = "lc1"

    def __init__(self, cfg: GatewayConfig, trace: TraceCb | None = None, *,
                 port_factory: PortFactory | None = None) -> None:
        self.cfg = cfg
        self.trace = trace
        self._factory = port_factory or default_port_factory
        self._ports: dict[str, _PortState] = {}
        self._ports_lock = threading.Lock()
        self._warned_decimals: set[tuple[str, int]] = set()
        self._closed_counters = {"tx": 0, "rx": 0, "errors": 0, "timeouts": 0, "checksum_errors": 0,
                                 "retries": 0, "commands": 0}

    # ----- Lebenszyklus ---------------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        """Ports werden je Zugriff bei Bedarf geöffnet; der Treiber selbst ist immer bereit."""
        return True

    async def open(self) -> None:
        for d in self._lc1_devices():
            try:
                await asyncio.to_thread(self._run, d.port, lambda proto: None)
            except InterfaceError as exc:
                log.warning("LC1 %s: %s", d.id, exc)

    async def close(self) -> None:
        await asyncio.to_thread(self._close_all)

    def _close_all(self) -> None:
        with self._ports_lock:
            states = list(self._ports.values())
        for st in states:
            with st.lock:
                self._close_port(st)

    def reload(self, cfg: GatewayConfig) -> None:
        self.cfg = cfg
        wanted = {d.port for d in self._lc1_devices()}
        with self._ports_lock:
            states = list(self._ports.items())
        for name, st in states:
            if name not in wanted:
                with st.lock:
                    self._close_port(st)
                with self._ports_lock:
                    self._ports.pop(name, None)
            elif st.proto is not None:
                st.proto.set_timeouts(cfg.lc1.byte_timeout_s, cfg.lc1.line_timeout_s)

    def _lc1_devices(self) -> list[DeviceConfig]:
        return [d for d in self.cfg.devices if d.bus == "lc1" and d.port]

    # ----- Port-Verwaltung (im Worker-Thread) ---------------------------------------------------
    def _state(self, port: str) -> _PortState:
        with self._ports_lock:
            st = self._ports.get(port)
            if st is None:
                st = self._ports[port] = _PortState(port)
            return st

    def _close_port(self, st: _PortState) -> None:
        if st.proto is not None:
            for k, v in st.proto.counters.items():
                self._closed_counters[k] = self._closed_counters.get(k, 0) + v
        if st.port is not None:
            try:
                st.port.close()
            except Exception:  # noqa: BLE001
                pass
        st.port, st.proto = None, None

    def _ensure(self, st: _PortState) -> Lc1Protocol:
        if st.proto is None:
            try:
                st.port = self._factory(st.name)
            except InterfaceError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise InterfaceError(f"LC1-Port {st.name} nicht verfügbar: {exc}") from exc
            lc1 = self.cfg.lc1
            st.proto = Lc1Protocol(st.port, byte_timeout_s=lc1.byte_timeout_s,
                                   line_timeout_s=lc1.line_timeout_s, trace=self.trace, name=st.name)
            log.info("LC1-Port %s geöffnet", st.name)
        return st.proto

    def _run(self, port: str | None, fn):
        if not port:
            raise InterfaceError("LC1-Gerät ohne Port")
        st = self._state(port)
        with st.lock:
            proto = self._ensure(st)
            try:
                return fn(proto)
            except (SafetyViolation, DeviceTimeout, ProtocolError):
                raise
            except InterfaceError:
                self._close_port(st)
                raise
            except Exception as exc:  # noqa: BLE001 - SerialException/OSError: Adapter abgesteckt?
                st.last_error = str(exc)
                self._close_port(st)
                raise InterfaceError(f"LC1-Port {port}: {exc}") from exc

    async def _call(self, port: str | None, fn):
        return await asyncio.to_thread(self._run, port, fn)

    # ----- Scan -----------------------------------------------------------------------------
    async def scan(self, progress: ProgressCb | None = None, ports: list[str] | None = None,
                   **_ignored) -> list[DeviceInfo]:
        """Befehl 53 an jedem Port. Default: konfigurierte LC1-Ports (nie automatisch alle
        COM-Ports – fremde Geräte bekämen sonst Bytes)."""
        if ports is None:
            ports = [d.port for d in self._lc1_devices()]
        ports = list(dict.fromkeys(p for p in ports if p))
        found: list[DeviceInfo] = []
        configured = {d.port for d in self._lc1_devices()}
        for i, port in enumerate(ports):
            if progress:
                progress(i, len(ports), f"LC1 {port}")
            try:
                ident = await self._call(port, lambda proto: proto.ident())
            except (DeviceTimeout, ProtocolError, InterfaceError) as exc:
                log.info("LC1-Scan %s: kein Regler (%s)", port, exc)
                continue
            finally:
                if port not in configured:
                    await asyncio.to_thread(self._forget, port)
            if port in configured:
                self._state(port).ident = ident
            found.append(DeviceInfo(
                bus="lc1", port=port, type_name=TYPE_NAME, name=ident.name,
                version=ident.version_text or None, system_nr=ident.system_nr,
                raw={"ident": ident.text, "ident_hex": ident.raw.hex(" ")}))
        if progress:
            progress(len(ports), len(ports), "fertig")
        return found

    def _forget(self, port: str) -> None:
        with self._ports_lock:
            st = self._ports.pop(port, None)
        if st is not None:
            with st.lock:
                self._close_port(st)

    # ----- Aufzählung ------------------------------------------------------------------------
    async def enumerate(self, device: DeviceConfig, progress: ProgressCb | None = None) -> list[ValueInfo]:
        """Hinterlegte Tabellen (lc1_tables) mit aktuellen Werten (ein 50 und ein 48 für 0..63)."""
        if progress:
            progress(0, 2, "Arbeitsdaten")
        wd: Lc1WorkData | None = None
        raw: dict[int, int] = {}
        errors: list[Exception] = []
        try:
            wd = await self._call(device.port, lambda p: p.read_work_data(device.checksum))
        except (DeviceTimeout, ProtocolError, InterfaceError) as exc:
            errors.append(exc)
            log.warning("LC1 %s: Arbeitsdaten bei Aufzählung nicht lesbar: %s", device.id, exc)
        if progress:
            progress(1, 2, "Parameter")
        try:
            raw = await self._call(device.port, lambda p: p.read_params(list(range(64))))
        except (DeviceTimeout, ProtocolError, InterfaceError) as exc:
            errors.append(exc)
            log.warning("LC1 %s: Parameter bei Aufzählung nicht lesbar: %s", device.id, exc)
        if len(errors) == 2:
            raise errors[-1]
        if progress:
            progress(2, 2, "fertig")
        infos: list[ValueInfo] = []
        for k, v in lc1_tables.VALUES.items():
            tip = v.tip
            if v.texts:
                tip = (tip + "; " if tip else "") + ", ".join(f"{c}={t}" for c, t in v.texts.items())
            infos.append(ValueInfo(kind="value", key=k, name=v.name, unit=v.unit, decimals=v.decimals,
                                   vartyp=VarTyp.FLOAT, value=wd.values.get(k) if wd else None, tip=tip))
        for e, p in lc1_tables.PARAMS.items():
            tip = "; ".join(x for x in (p.note, p.source) if x)
            infos.append(ValueInfo(
                kind="param", key=e, name=p.text, unit=p.unit, decimals=p.decimals, vartyp=VarTyp.FLOAT,
                min=p.min, max=p.max, value=p.scale(raw[e]) if e in raw else None, tip=tip,
                level=lc1_tables.LEVEL_TEXT.get(p.level, p.level),
                lock_reason=safety.param_lock_reason("lc1", None, e)))
        return infos

    # ----- Lesen ---------------------------------------------------------------------------
    async def read_values(self, device: DeviceConfig, values: list[ValueDef]) -> dict[int, float | None]:
        wd: Lc1WorkData = await self._call(device.port, lambda p: p.read_work_data(device.checksum))
        return {v.key: wd.values.get(v.key) for v in values}

    async def read_params(self, device: DeviceConfig, params: list[ParamDef]) -> dict[int, float | None]:
        keys = [p.key for p in params]
        raw: dict[int, int] = await self._call(device.port, lambda proto: proto.read_params(keys))
        out: dict[int, float | None] = {}
        for p in params:
            r = raw.get(p.key)
            out[p.key] = None if r is None else r / 10 ** self._decimals(device, p)
        return out

    async def read_work_data(self, device: DeviceConfig) -> Lc1WorkData:
        """Vollständige Arbeitsdaten inkl. Port 4/IC8/IC9 und Prüfsumme (Diagnose)."""
        return await self._call(device.port, lambda p: p.read_work_data(device.checksum))

    async def ident(self, device: DeviceConfig) -> Lc1Ident:
        return await self._call(device.port, lambda p: p.ident())

    def _decimals(self, device: DeviceConfig, p: ParamDef) -> int:
        tbl = lc1_tables.PARAMS.get(p.key)
        if tbl is not None and tbl.decimals != p.decimals and (device.id, p.key) not in self._warned_decimals:
            self._warned_decimals.add((device.id, p.key))
            log.warning("LC1 %s: E(%d) decimals=%d in der Konfiguration, Tabelle sagt %d (%s) – "
                        "verwende Konfiguration", device.id, p.key, p.decimals, tbl.decimals, tbl.source)
        return p.decimals

    # ----- Schreiben ------------------------------------------------------------------------
    async def write_param(self, device: DeviceConfig, param: ParamDef, value: float) -> float:
        """Befehl 49, danach Befehl 48 zurücklesen -> Istwert. Nie wiederholt."""
        e = param.key
        dec = self._decimals(device, param)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SafetyViolation(f"LC1: Wert {value!r} ist keine endliche Zahl")
        raw = round(float(value) * 10 ** dec)
        safety.check_lc1_param_write(e, raw)                 # vor jedem Senden
        tbl = lc1_tables.PARAMS.get(e)
        # Die Firmware prüft den Bereich NICHT (LCS9.ARB:924-938) -> hier zusätzlich nach LCS1.par.
        if tbl is None or not tbl.min_raw <= raw <= tbl.max_raw:
            rng = f"{tbl.min_raw}..{tbl.max_raw}" if tbl else "?"
            raise SafetyViolation(f"LC1: E({e}) Rohwert {raw} außerhalb {rng} laut LCS1.par")
        await self._call(device.port, lambda p: p.write_param(e, raw))
        try:
            back = await self._call(device.port, lambda p: p.read_params([e]))
        except (DeviceTimeout, ProtocolError, InterfaceError) as exc:
            # Regler hat CHR(11) gesendet -> Wert ist geschrieben, nur die Rückmeldung fehlt.
            raise Lc1WriteUnclear(f"LC1: E({e}) geschrieben, Rücklesen fehlgeschlagen: {exc}") from exc
        actual = back[e] / 10 ** dec
        if back[e] != raw:
            log.error("LC1 %s: E(%d) geschrieben %d, zurückgelesen %d", device.id, e, raw, back[e])
        return actual

    # ----- Diagnose -------------------------------------------------------------------------
    def stats(self) -> dict:
        total = dict(self._closed_counters)
        with self._ports_lock:
            states = list(self._ports.values())
        for st in states:
            if st.proto is not None:
                for k, v in st.proto.counters.items():
                    total[k] = total.get(k, 0) + v
        total["ports"] = {st.name: {"open": st.proto is not None, "last_error": st.last_error,
                                    **(st.proto.counters if st.proto else {})} for st in states}
        return total
