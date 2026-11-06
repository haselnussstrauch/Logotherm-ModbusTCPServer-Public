"""Modbus-TCP-Server (pymodbus 3.16.2), liest live aus dem ValueStore über die RegisterMap.

pymodbus-Anbindung (aus dem installierten Quellcode von 3.16.2):
- Der Server ``pymodbus.server.ModbusTcpServer`` ruft pro Request nur ``context.async_getValues``,
  ``context.async_setValues`` und (Broadcast) ``context.device_ids`` auf (``requesthandler.py``).
  Die klassischen Datastore-Klassen (ModbusDeviceContext, Sequential-/SparseDataBlock) sind in
  3.16 deprecated und speichern Werte statisch, deshalb ersetzt ``_GatewayServer`` das Context-Objekt
  durch ``_GatewayContext`` (Duck-Typing, Instanzattribut – kein globaler Monkeypatch).
- IP-Allowlist: ``callback_new_connection()`` ist der offizielle Hook des Listeners; er liefert
  einen eigenen ``ServerRequestHandler`` (``_GuardedHandler``), der in ``connection_made`` die
  Peer-Adresse prüft und die Verbindung sofort wieder schließt.
- Unbekannte Unit-ID: Exception 0x0B (Gateway Target Device Failed to Respond). Das ist die
  Standard-Antwort eines Modbus-Gateways und lässt dem Client sofort erkennen, dass die Unit
  nicht existiert (statt Timeout + Wiederholungen); ``ignore_missing_devices`` würde stattdessen
  schweigen.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import time
from collections.abc import Awaitable, Callable, Sequence

from pymodbus.constants import ExcCodes
from pymodbus.server import ModbusTcpServer
from pymodbus.server.requesthandler import ServerRequestHandler
from pymodbus.simulator import DataType, SimData, SimDevice

from gateway.config import DeviceConfig, GatewayConfig, ParamDef
from gateway.errors import WriteRejected
from gateway.registermap import (
    IllegalAddress,
    RegisterMap,
    decode_float,
)
from gateway.store import Kind, ValueStore

log = logging.getLogger(__name__)

WriteHandler = Callable[[DeviceConfig, ParamDef, float], Awaitable[None]]

_FC_TABLE = {3: "hr", 4: "ir"}
_LOG_INTERVAL_S = 60.0       # Rate-Limit für "Verbindung abgelehnt" je Quell-IP


# --- Allowlist --------------------------------------------------------------------------------
class IpAllowlist:
    """IP-/CIDR-Allowlist (IPv4 und IPv6). Leer = alle erlaubt."""

    def __init__(self, entries: Sequence[str]) -> None:
        self.networks = [ipaddress.ip_network(e.strip(), strict=False) for e in entries if e.strip()]

    def allows(self, ip: str) -> bool:
        if not self.networks:
            return True
        try:
            addr = ipaddress.ip_address(ip.split("%", 1)[0])      # Zone-ID (fe80::1%eth0) abschneiden
        except ValueError:
            return False
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        return any(addr.version == n.version and addr in n for n in self.networks)


# --- pymodbus-Anbindung -----------------------------------------------------------------------
class _GuardedHandler(ServerRequestHandler):
    """Verbindungs-Handler, der Peers außerhalb der Allowlist sofort trennt."""

    def connection_made(self, transport) -> None:
        peer = transport.get_extra_info("peername")
        ip = peer[0] if peer else ""
        if not self.server.gateway.check_peer(ip):
            self.transport = transport
            self.close()           # schließt Transport und meldet sich beim Listener ab
            return
        super().connection_made(transport)


class _GatewayServer(ModbusTcpServer):
    """ModbusTcpServer mit eigenem Context und Allowlist-Handler."""

    def __init__(self, gateway: ModbusGateway, address: tuple[str, int]) -> None:
        # Platzhalter-Device nur, damit der Konstruktor einen gültigen Context bekommt.
        stub = SimDevice(id=0, simdata=SimData(0, count=1, datatype=DataType.REGISTERS))
        super().__init__(stub, address=address)
        self.gateway = gateway
        self.context = _GatewayContext(gateway)     # type: ignore[assignment]

    def callback_new_connection(self):
        return _GuardedHandler(self, self.trace_packet, self.trace_pdu, self.trace_connect)


class _GatewayContext:
    """Datastore-Ersatz: jede Anfrage wird frisch aus RegisterMap/ValueStore beantwortet."""

    def __init__(self, gateway: ModbusGateway) -> None:
        self.gw = gateway

    def device_ids(self) -> list[int]:
        return sorted(self.gw.map.units())

    async def async_getValues(self, device_id: int, func_code: int, address: int, count: int = 1):
        rmap = self.gw.map
        if not rmap.has_unit(device_id):
            return ExcCodes.GATEWAY_NO_RESPONSE
        table = _FC_TABLE.get(func_code)
        if table is None:
            return ExcCodes.ILLEGAL_FUNCTION
        try:
            return rmap.read(device_id, table, address, count)       # type: ignore[arg-type]
        except IllegalAddress:
            return ExcCodes.ILLEGAL_ADDRESS

    async def async_setValues(self, device_id: int, func_code: int, address: int,
                              values: Sequence[int]):
        if not self.gw.map.has_unit(device_id):
            return ExcCodes.GATEWAY_NO_RESPONSE
        if func_code != 16:                          # FC 05/06/15/22/23: nicht erlaubt
            return ExcCodes.ILLEGAL_FUNCTION
        return await self.gw.write_registers(device_id, address, list(values))


# --- Gateway ----------------------------------------------------------------------------------
class ModbusGateway:
    def __init__(
        self,
        cfg: GatewayConfig,
        store: ValueStore,
        write_handler: WriteHandler,
        budget_remaining: Callable[[], int] | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.write_handler = write_handler
        self.map = RegisterMap(cfg, store, budget_remaining)
        self.allowlist = IpAllowlist(cfg.modbus.allow)
        self._server: _GatewayServer | None = None
        self._write_lock = asyncio.Lock()
        self._last_reject_log: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}
        self._restart_task: asyncio.Task | None = None

    # --- Lebenszyklus ---
    @property
    def running(self) -> bool:
        return self._server is not None

    async def start(self) -> None:
        m = self.cfg.modbus
        if not m.enabled:
            log.info("Modbus-TCP-Server ist deaktiviert (modbus.enabled = false)")
            return
        if not self.allowlist.networks:
            log.warning("Modbus-IP-Allowlist ist leer: JEDER Client im Netz darf zugreifen!")
        server = _GatewayServer(self, (m.bind, m.port))
        try:
            await server.serve_forever(background=True)
        except RuntimeError as exc:
            raise OSError(f"Modbus-Server konnte {m.bind}:{m.port} nicht öffnen") from exc
        self._server = server
        log.info("Modbus-TCP-Server lauscht auf %s:%d", m.bind, m.port)

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            await server.shutdown()
            await asyncio.sleep(0)
            log.info("Modbus-TCP-Server gestoppt")

    def reload(self, cfg: GatewayConfig) -> asyncio.Task | None:
        """Übernimmt eine neue Konfiguration (Registerkarte, Allowlist, Schreibrechte).

        Ändern sich Bind-Adresse oder Port, wird der Listener im Hintergrund neu gestartet
        (die zurückgegebene Task kann abgewartet werden); sonst bleiben Verbindungen bestehen.
        """
        old = self.cfg.modbus
        self.map.rebuild(cfg)
        self.allowlist = IpAllowlist(cfg.modbus.allow)
        self.cfg = cfg
        if cfg.modbus.enabled and not self.allowlist.networks:
            log.warning("Modbus-IP-Allowlist ist leer: JEDER Client im Netz darf zugreifen!")
        log.info("Modbus-Konfiguration neu geladen")
        new = cfg.modbus
        if new.enabled != old.enabled or (
                self._server is not None and (old.bind, old.port) != (new.bind, new.port)):
            self._restart_task = asyncio.ensure_future(self._restart())
            return self._restart_task
        return None

    async def _restart(self) -> None:
        if self._server is not None:
            log.info("Modbus-Einstellungen geändert, Listener wird neu gestartet")
        await self.stop()
        await self.start()

    # --- Allowlist ---
    def check_peer(self, ip: str) -> bool:
        if self.allowlist.allows(ip):
            return True
        now = time.monotonic()
        last = self._last_reject_log.get(ip)
        if last is None or now - last >= _LOG_INTERVAL_S:
            n = self._suppressed.pop(ip, 0)
            extra = f" ({n} weitere unterdrückt)" if n else ""
            log.warning("Modbus-Verbindung von %s abgelehnt (nicht in Allowlist)%s", ip, extra)
            self._last_reject_log[ip] = now
            if len(self._last_reject_log) > 1024:          # Speicher begrenzen
                self._last_reject_log = {ip: now}
                self._suppressed.clear()
        else:
            self._suppressed[ip] = self._suppressed.get(ip, 0) + 1
        return False

    # --- Schreiben (FC 16) ---
    async def write_registers(self, unit_id: int, address: int, words: list[int]):
        """FC 16: genau ein Float-Paar (2 Register) je Anfrage. Rückgabe None oder ExcCodes.

        Mehrere Parameter in einer Anfrage sind bewusst verboten: Der Handler (safety.WriteGuard)
        kann einen späteren Parameter ablehnen, nachdem ein früherer schon gesendet wurde – ein
        Teilerfolg wäre für die Leittechnik nicht erkennbar.
        """
        if len(words) != 2:
            return ExcCodes.ILLEGAL_ADDRESS                       # halbes Paar / mehrere Paare
        order = self.map.word_order
        plan: list[tuple[DeviceConfig, ParamDef, float]] = []
        for i in range(0, len(words), 2):
            target = self.map.param_at(unit_id, address + i)
            if target is None:
                return ExcCodes.ILLEGAL_ADDRESS                   # Status/Int16/unbelegt/Paarmitte
            dev, param = target
            value = decode_float(words[i:i + 2], order)
            if not math.isfinite(value):
                return ExcCodes.ILLEGAL_VALUE
            if (param.min is not None and value < param.min) or \
                    (param.max is not None and value > param.max):
                return ExcCodes.ILLEGAL_VALUE
            plan.append((dev, param, value))
        async with self._write_lock:
            for n, (dev, param, value) in enumerate(plan):
                try:
                    await self.write_handler(dev, param, value)
                except WriteRejected as exc:
                    log.warning("Schreiben %s/%#06x=%s abgelehnt (%s): %s",
                                dev.id, param.key, value, exc.kind, exc.reason)
                    return ExcCodes.ILLEGAL_ADDRESS if exc.kind == "address" \
                        else ExcCodes.ILLEGAL_VALUE
                except Exception:                                  # noqa: BLE001
                    log.exception("Schreiben %s/%#06x fehlgeschlagen", dev.id, param.key)
                    return ExcCodes.DEVICE_FAILURE
                log.info("Modbus-Schreiben %s/%#06x = %s", dev.id, param.key, value)
        return None


# --- Dummy-Modus (Meilenstein 1) ----------------------------------------------------------------
def _dummy_range(e) -> tuple[float, float]:
    lo = e.min if e.min is not None else 0.0
    hi = e.max if e.max is not None else lo + 100.0
    return lo, hi


def _dummy_value(e, phase: float, t: float) -> float:
    lo, hi = _dummy_range(e)
    mid, amp = (lo + hi) / 2, (hi - lo) / 2 * 0.8
    return round(mid + amp * math.sin(t / 10.0 + phase), e.decimals)


async def run_dummy_feeder(cfg: GatewayConfig, store: ValueStore, stop_event: asyncio.Event) -> None:
    """Schreibt jede Sekunde Sinuswerte für alle aktiven Werte in den Store.

    Parameter bekommen nur einen Startwert (Mitte des Bereichs), damit per Modbus geschriebene
    Sollwerte nicht sofort überschrieben werden.
    """
    t0 = time.monotonic()
    while not stop_event.is_set():
        t = time.monotonic() - t0
        for d in cfg.devices:
            for i, e in enumerate(d.values):
                if e.active:
                    store.set_value(d.id, "value", e.key, _dummy_value(e, i, t))
            for i, p in enumerate(d.params):
                if p.active and store.get(d.id, "param", p.key).value is None:
                    lo, hi = _dummy_range(p)
                    start = p.default if p.default is not None else (lo + hi) / 2
                    store.set_value(d.id, "param", p.key, round(start, p.decimals))
            store.set_online(d.id)
        store.diag.heartbeat = (store.diag.heartbeat + 1) & 0xFFFF
        store.diag.cycle_time_ms = 1000
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


def make_dummy_write_handler(store: ValueStore) -> WriteHandler:
    """Dummy-Handler: Min/Max-Prüfung, dann Wert direkt als Ist-Wert in den Store.

    (Der echte Handler mit safety kommt später.)
    """
    async def handler(device: DeviceConfig, param: ParamDef, value: float) -> None:
        if not param.writable:
            raise WriteRejected("address", f"{param.key:#06x} ist nicht beschreibbar")
        if not math.isfinite(value) or (param.min is not None and value < param.min) \
                or (param.max is not None and value > param.max):
            raise WriteRejected("value", f"{value} außerhalb {param.min}..{param.max}")
        kind: Kind = "param"
        store.set_value(device.id, kind, param.key, value)

    return handler
