"""Poller: liest zyklisch die ausgewählten Werte aller Geräte in den Wertespeicher.

- Je Bus-Gruppe eine Schleife: alle CAN-Geräte teilen sich eine Gruppe (ein Bus, eine offene
  Anfrage), jedes LC1-Gerät hat seine eigene Gruppe (eigener COM-Port).
- `pause()` hält alle Gruppen an (Scan, Aufzählung, CAN-Test, Konfigwechsel) und wartet, bis
  laufende Zugriffe beendet sind. `exclusive(device)` sperrt nur die Gruppe eines Geräts
  (Schreibvorgang).
- Nicht erreichbare Schnittstellen werden periodisch neu geöffnet.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

from gateway.config import DeviceConfig, GatewayConfig
from gateway.drivers import BusDriver, DeviceTimeout, DriverError, InterfaceError, ProtocolError
from gateway.errors import SafetyViolation
from gateway.store import DeviceError, ValueStore

log = logging.getLogger(__name__)

REOPEN_INTERVAL_S = 10.0


def group_key(device: DeviceConfig) -> str:
    return "can" if device.bus == "can" else f"lc1:{device.id}"


class Poller:
    def __init__(self, cfg: GatewayConfig, store: ValueStore,
                 drivers: Callable[[str], BusDriver | None]) -> None:
        self.cfg = cfg
        self.store = store
        self._drivers = drivers               # bus -> Treiber (oder None, wenn deaktiviert)
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._running = asyncio.Event()
        self._running.set()
        self._pause_depth = 0
        self._last_params: dict[str, float] = {}
        self._cycle_ms: dict[str, int] = {}
        self._open_failed: dict[str, float] = {}
        self.state = "stopped"

    # --- Steuerung ---
    def lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def start(self) -> None:
        self._sync_tasks()
        self.state = "running"

    async def stop(self) -> None:
        for t in self._tasks.values():
            t.cancel()
        for t in self._tasks.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()
        self.state = "stopped"

    def reload(self, cfg: GatewayConfig) -> None:
        self.cfg = cfg
        self._last_params.clear()
        if self.state != "stopped":
            self._sync_tasks()

    def _groups(self) -> dict[str, list[DeviceConfig]]:
        groups: dict[str, list[DeviceConfig]] = {}
        for d in self.cfg.devices:
            if d.enabled and (d.bus != "can" or self.cfg.can.enabled):
                groups.setdefault(group_key(d), []).append(d)
        return groups

    def _sync_tasks(self) -> None:
        wanted = set(self._groups())
        for key in list(self._tasks):
            if key not in wanted or self._tasks[key].done():
                self._tasks.pop(key).cancel()
        for key in wanted - set(self._tasks):
            self._tasks[key] = asyncio.create_task(self._loop(key), name=f"poll-{key}")

    @contextlib.asynccontextmanager
    async def pause(self):
        """Hält das Polling an, bis der Block verlassen wird (verschachtelbar)."""
        self._pause_depth += 1
        self._running.clear()
        self.state = "paused"
        locks = [self.lock(k) for k in sorted(set(self._groups()) | set(self._locks))]
        acquired: list[asyncio.Lock] = []
        try:
            for lk in locks:
                await lk.acquire()
                acquired.append(lk)
            yield
        finally:
            for lk in acquired:
                lk.release()
            self._pause_depth -= 1
            if self._pause_depth == 0:
                self._running.set()
                self.state = "running" if self._tasks else "stopped"

    @contextlib.asynccontextmanager
    async def exclusive(self, device: DeviceConfig):
        async with self.lock(group_key(device)):
            yield

    # --- Schleife ---
    async def _loop(self, key: str) -> None:
        while True:
            await self._running.wait()
            t0 = time.monotonic()
            try:
                await self._cycle(key)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - Poller darf nie sterben
                log.exception("Fehler im Poll-Zyklus %s", key)
            dt = time.monotonic() - t0
            self._cycle_ms[key] = int(dt * 1000)
            diag = self.store.diag
            diag.heartbeat = (diag.heartbeat + 1) & 0xFFFF
            diag.cycle_time_ms = min(0xFFFF, max(self._cycle_ms.values(), default=0))
            can = self._drivers("can")
            if can is not None:
                with contextlib.suppress(Exception):
                    diag.can_error_count = int(can.stats().get("errors", 0))
            await asyncio.sleep(max(0.5, self.cfg.poll.interval_s - dt))

    async def _ensure_open(self, driver: BusDriver, key: str, devices: list[DeviceConfig]) -> bool:
        if getattr(driver, "is_open", True):
            return True
        last = self._open_failed.get(key, 0.0)
        if time.monotonic() - last < REOPEN_INTERVAL_S:
            return False
        try:
            await driver.open()
            self._open_failed.pop(key, None)
            log.info("Schnittstelle %s geöffnet", key)
            return True
        except Exception as exc:  # noqa: BLE001
            self._open_failed[key] = time.monotonic()
            for d in devices:
                self.store.set_error(d.id, DeviceError.INTERFACE, str(exc))
            log.warning("Schnittstelle %s nicht verfügbar: %s", key, exc)
            return False

    async def _cycle(self, key: str) -> None:
        devices = self._groups().get(key, [])
        if not devices:
            return
        driver = self._drivers(devices[0].bus)
        if driver is None:
            return
        if not await self._ensure_open(driver, key, devices):
            return
        for dev in devices:
            await self._running.wait()
            async with self.lock(key):
                await self.poll_device(driver, dev)

    async def poll_device(self, driver: BusDriver, dev: DeviceConfig) -> None:
        values = [v for v in dev.values if v.active]
        params = [p for p in dev.params if p.active]
        now = time.monotonic()
        due_params = params and now - self._last_params.get(dev.id, -1e9) >= self.cfg.poll.param_interval_s
        try:
            if values:
                self._store_results(dev.id, "value", values, await driver.read_values(dev, values))
            if due_params:
                self._store_results(dev.id, "param", params, await driver.read_params(dev, params))
                self._last_params[dev.id] = now
            if not values and not due_params and not params:
                return
            self.store.set_online(dev.id)
        except DeviceTimeout as exc:
            self._mark_all_timeout(dev)
            self.store.set_error(dev.id, DeviceError.TIMEOUT, str(exc) or "keine Antwort")
        except ProtocolError as exc:
            self.store.set_error(dev.id, DeviceError.PROTOCOL, str(exc))
            log.warning("%s: Protokollfehler: %s", dev.id, exc)
        except InterfaceError as exc:
            self.store.set_error(dev.id, DeviceError.INTERFACE, str(exc))
            log.warning("%s: Schnittstelle: %s", dev.id, exc)
            with contextlib.suppress(Exception):
                await driver.close()
        except SafetyViolation:
            log.critical("%s: Sicherheitsprüfung hat einen Lesezugriff blockiert", dev.id, exc_info=True)
            self.store.set_error(dev.id, DeviceError.PROTOCOL, "Sicherheitsprüfung")
        except DriverError as exc:
            self.store.set_error(dev.id, DeviceError.PROTOCOL, str(exc))

    def _store_results(self, device_id: str, kind, entries, results: dict) -> None:
        for e in entries:
            v = results.get(e.key)
            if v is None:
                self.store.set_timeout(device_id, kind, e.key)
            else:
                self.store.set_value(device_id, kind, e.key, float(v))

    def _mark_all_timeout(self, dev: DeviceConfig) -> None:
        for v in dev.values:
            if v.active:
                self.store.set_timeout(dev.id, "value", v.key)
        for p in dev.params:
            if p.active:
                self.store.set_timeout(dev.id, "param", p.key)
