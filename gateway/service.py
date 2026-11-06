"""Dienst-Kern: besitzt die Hardware, verbindet Treiber, Poller, Modbus-Server und Weboberfläche.

Die Weboberfläche (gateway/web) arbeitet ausschließlich über diese Klasse. Änderungen werden in
einem Entwurf (`draft`) gesammelt und mit `apply_draft()` gespeichert und ohne Neustart übernommen.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import copy
import datetime as dt
import hashlib
import hmac
import logging
import math
import os
import re
import secrets
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from gateway import __version__, safety
from gateway.config import (
    DeviceConfig, GatewayConfig, ParamDef, ValueDef, assign_all_registers, renumber_registers,
    save_config, suggest_unit_id,
)
from gateway.devices.names import display_name, value_text
from gateway.drivers import BusDriver, DeviceInfo, DeviceTimeout, ValueInfo
from gateway.errors import SafetyViolation, WriteRejected
from gateway.history import MIN_FREE_BYTES, History, SeriesMeta
from gateway.poller import Poller
from gateway.store import DeviceError, ValueStore

log = logging.getLogger(__name__)


# --- Jobs ---------------------------------------------------------------------------------------
@dataclass
class Job:
    kind: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: Literal["running", "done", "error"] = "running"
    progress: tuple[int, int, str] = (0, 0, "")
    result: Any = None
    error: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    def set_progress(self, done: int, total: int, text: str = "") -> None:
        self.progress = (done, total, text)


# --- Passwort -----------------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, salt_hex, h_hex = stored.split("$")
        if algo != "scrypt":
            return False
        h = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
        return hmac.compare_digest(h.hex(), h_hex)
    except (ValueError, TypeError):
        return False


def _device_id_for(info: DeviceInfo) -> str:
    if info.bus == "can":
        return f"can-{info.address:02x}"
    tail = re.sub(r"[^A-Za-z0-9_.-]", "_", (info.port or "x").rsplit("/", 1)[-1])[-30:]
    return f"lc1-{tail}"


class Service:
    def __init__(self, cfg: GatewayConfig, config_path: Path, *, dummy: bool = False) -> None:
        self.cfg = cfg
        self.config_path = Path(config_path)
        self.draft: GatewayConfig = cfg.model_copy(deep=True)
        self.store = ValueStore(stale_after_s=cfg.poll.stale_after_s)
        self.started = time.time()
        self.dummy = dummy
        self.jobs: dict[str, Job] = {}
        self._enum_cache: dict[str, list[ValueInfo]] = {}
        self._history: collections.deque[dict] = collections.deque(maxlen=500)
        self._drivers: dict[str, BusDriver] = {}
        self.guard = safety.WriteGuard(cfg.safety)
        self.poller = Poller(cfg, self.store, self.driver)
        self.modbus = None
        self._web = None
        self._dummy_task: asyncio.Task | None = None
        self._dummy_stop = asyncio.Event()
        self.trace = None   # Mitschnitt-Callback (drivers.TraceCb), z. B. von `gateway run --trace`
        self.history = History(cfg.service.resolved_data_dir())
        self._history_task: asyncio.Task | None = None
        self._history_wake = asyncio.Event()
        self._history_warning = ""

    # ===== Lebenszyklus ==========================================================================
    async def start(self, *, web: bool = True) -> None:
        from gateway.modbus_server import ModbusGateway

        self.modbus = ModbusGateway(self.cfg, self.store, self.write_param,
                                    budget_remaining=lambda: self.guard.remaining_budget)
        await self.modbus.start()
        if self.dummy:
            from gateway.modbus_server import run_dummy_feeder
            self._dummy_task = asyncio.create_task(run_dummy_feeder(self.cfg, self.store, self._dummy_stop))
        else:
            self._create_drivers(self.cfg)
            for bus, drv in self._drivers.items():
                try:
                    await drv.open()
                except Exception as exc:  # noqa: BLE001 - Dienst läuft weiter, Poller versucht erneut
                    log.warning("Schnittstelle %s beim Start nicht verfügbar: %s", bus, exc)
            self.poller.start()
        self._history_task = asyncio.create_task(self._history_loop(), name="history")
        if web:
            from gateway.web.server import serve_web
            from gateway.web.app import create_app
            self._web = await serve_web(create_app(self), self.cfg)
        for w in self.warnings():
            log.warning(w)

    async def stop(self) -> None:
        self._dummy_stop.set()
        if self._dummy_task:
            self._dummy_task.cancel()
        await self.poller.stop()
        if self._history_task:
            self._history_task.cancel()
            with contextlib.suppress(BaseException):
                await self._history_task
        self.history.close()
        if self.modbus:
            await self.modbus.stop()
        for drv in self._drivers.values():
            with contextlib.suppress(Exception):
                await drv.close()
        if self._web:
            server, task = self._web
            server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, 5)

    # ===== Treiber ===============================================================================
    def driver(self, bus: str) -> BusDriver | None:
        return self._drivers.get(bus)

    def _create_drivers(self, cfg: GatewayConfig) -> None:
        if cfg.can.enabled and "can" not in self._drivers:
            from gateway.can_lc2.driver import CanDriver
            self._drivers["can"] = CanDriver(cfg, trace=self.trace)
        if "lc1" not in self._drivers:
            from gateway.serial_lc1.driver import Lc1Driver
            self._drivers["lc1"] = Lc1Driver(cfg, trace=self.trace)

    async def _reconfigure_drivers(self, old: GatewayConfig, new: GatewayConfig) -> None:
        can_changed = old.can.model_dump() != new.can.model_dump()
        if "can" in self._drivers and (can_changed or not new.can.enabled):
            with contextlib.suppress(Exception):
                await self._drivers.pop("can").close()
        for drv in self._drivers.values():
            reload = getattr(drv, "reload", None)
            if reload:
                reload(new)
        if not self.dummy:
            self._create_drivers(new)
            if "can" in self._drivers and not getattr(self._drivers["can"], "is_open", False):
                try:
                    await self._drivers["can"].open()
                except Exception as exc:  # noqa: BLE001
                    log.warning("CAN-Schnittstelle nicht verfügbar: %s", exc)

    # ===== Aufzeichnung ==========================================================================
    async def _history_loop(self) -> None:
        """Speichert im Abstand ``history.interval_s`` alle aktuellen Werte (Status ok) und löscht
        stündlich Tage außerhalb der Aufbewahrung."""
        last: dict[tuple[str, str, int], float] = {}
        next_cleanup = 0.0
        fails = 0
        while True:
            h = self.cfg.history
            if time.time() >= next_cleanup:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self.history.cleanup, h.retention_days)
                next_cleanup = time.time() + 3600
            self._history_wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._history_wake.wait(), timeout=h.interval_s)
            if self._history_wake.is_set():       # Einstellungen geändert -> sofort aufräumen
                next_cleanup = 0.0
                continue
            if not self.cfg.history.enabled or self.poller.state == "paused":
                continue
            try:
                await asyncio.to_thread(self._record_once, last)
                fails = 0
            except Exception as exc:  # noqa: BLE001 - Aufzeichnung darf den Dienst nie stören
                fails += 1
                self._history_warning = f"Aufzeichnung gestört: {exc}"
                if fails in (1, 10, 100):
                    log.error("Aufzeichnung gestört: %s", exc)

    def _record_once(self, last: dict[tuple[str, str, int], float]) -> int:
        free = self.history.disk_free()
        if free is not None and free < MIN_FREE_BYTES:
            self._history_warning = (f"Aufzeichnung angehalten: nur noch {free // (1024 * 1024)} MB frei "
                                     f"im Datenordner")
            return 0
        self._history_warning = ""
        wall, mono = time.time(), self.store.now()
        samples, meta = [], {}
        for d in self.cfg.devices:
            if not d.enabled:
                continue
            for kind, entries in (("value", d.values), ("param", d.params)):
                for e in entries:
                    if not e.active:
                        continue
                    st = self.store.get(d.id, kind, e.key)
                    k = (d.id, kind, e.key)
                    if st.status != 0 or st.value is None or not math.isfinite(st.value) or last.get(k) == st.ts:
                        continue                  # nur frische, gültige Werte
                    last[k] = st.ts
                    samples.append((wall - max(0.0, mono - st.ts), d.id, kind, e.key, float(st.value)))
                    name = display_name(e.name, e.nr) if d.bus == "can" else e.name
                    meta[k] = SeriesMeta(d.id, kind, e.key, name, e.unit, _entry_nr(e), d.name or d.id)
        return self.history.record(samples, meta) if samples else 0

    def history_status(self) -> dict:
        h = self.cfg.history
        days = self.history.days()
        return {"enabled": h.enabled, "interval_s": h.interval_s, "retention_days": h.retention_days,
                "days": days, "total_size": sum(d["size"] for d in days),
                "disk_free": self.history.disk_free(), "warning": self._history_warning,
                "dir": str(self.history.dir)}

    # ===== Schreiben (Modbus FC 16 und Weboberfläche) ==========================================
    async def write_param(self, dev: DeviceConfig, param: ParamDef, value: float, source: str = "Modbus") -> dict:
        """WriteGuard → Treiber → Rückmeldung prüfen. Gleicher Weg für Modbus und Weboberfläche.

        Rückgabe: Eintrag der Schreib-Historie (``ok``, ``readback``, ``text``)."""
        if self.poller.state == "paused":
            raise WriteRejected("value", "Gerätesuche/Konfiguration läuft – später erneut versuchen")
        current = self.store.get(dev.id, "param", param.key).value
        if not self.guard.check(dev, param, value, current):
            return self._log_write(source, dev, param, current, value, current, True,
                                   "unverändert (Totband), nicht gesendet")
        if self.dummy:
            self.guard.commit(dev.id, param.key)
            self.store.set_value(dev.id, "param", param.key, value)
            return self._log_write(source, dev, param, current, value, value, True, "Dummy")
        drv = self.driver(dev.bus)
        if drv is None:
            raise WriteRejected("address", f"Bus {dev.bus} nicht aktiv")
        async with self.poller.exclusive(dev):
            try:
                readback = await drv.write_param(dev, param, value)
            except SafetyViolation as exc:
                log.critical("%s: Schreiben %#06x durch Sicherheitsprüfung blockiert: %s", dev.id, param.key, exc)
                self._log_write(source, dev, param, current, value, None, False, f"blockiert: {exc}")
                raise WriteRejected("address", str(exc)) from exc
            except DeviceTimeout as exc:
                # Rahmen wurde gesendet -> zählt fürs Ratenlimit/Budget; Ergebnis unklar -> Alarm.
                self.guard.commit(dev.id, param.key)
                self.store.set_error(dev.id, DeviceError.WRITE_MISMATCH, "keine Schreibbestätigung", offline=False)
                self._log_write(source, dev, param, current, value, None, False, "keine Bestätigung vom Gerät")
                raise RuntimeError("keine Schreibbestätigung") from exc
            self.guard.commit(dev.id, param.key)
        self.store.set_value(dev.id, "param", param.key, readback)
        ok = safety.verify_readback(value, readback, param.decimals)
        if not ok:
            self.store.set_error(dev.id, DeviceError.WRITE_MISMATCH,
                                 f"{param.name or hex(param.key)}: Soll {value}, Ist {readback}", offline=False)
            log.error("%s: Rückmeldung weicht ab: %#06x Soll %s, Ist %s", dev.id, param.key, value, readback)
        entry = self._log_write(source, dev, param, current, value, readback, ok, "" if ok else "Rückmeldung weicht ab")
        self.store.diag.writes_today = self.guard.writes_today
        return entry

    def _log_write(self, source: str, dev, param, old, new, readback, ok: bool, text: str) -> dict:
        entry = {"ts": dt.datetime.now().isoformat(timespec="seconds"), "device": dev.id, "key": param.key,
                 "name": param.name, "old": old, "new": new, "readback": readback, "ok": ok, "text": text,
                 "source": source}
        self._history.append(entry)
        (log.info if ok else log.warning)("Schreiben (%s) %s %#06x %s: %s -> %s (Ist %s) %s", source,
                                          dev.id, param.key, param.name, old, new, readback, text)
        return entry

    def param_overview(self, device_id: str) -> dict:
        """Aktive Parameter eines Geräts für das Menü „Parameter“: Wert, Bereich und ob/warum
        Schreiben gerade möglich ist (gleiche Regeln wie WriteGuard.check)."""
        d = self.cfg.device(device_id)
        enabled = self.cfg.safety.write_enabled
        rows = []
        for p in d.params:
            if not p.active:
                continue
            st = self.store.get(d.id, "param", p.key)
            lock = safety.param_lock_reason(d.bus, d.regler_typ, p.key, out_type=p.out_type)
            wait = self.guard.wait_s(d.id, p.key)
            if lock:
                why = f"gesperrt: {lock}"
            elif not p.writable:
                why = "nicht als änderbar freigegeben (Werteliste)"
            elif not enabled:
                why = "Schreiben global gesperrt (Schnittstellen → Schreibschutz)"
            elif wait > 0:
                why = f"Wartezeit: erst in {wait:.0f} s wieder (10 min je Parameter)"
            else:
                why = ""
            rows.append({"key": p.key, "nr": _entry_nr(p),
                         "name": display_name(p.name, p.nr) if d.bus == "can" else p.name,
                         "unit": p.unit, "decimals": p.decimals, "min": p.min, "max": p.max,
                         "value": st.value, "status": int(st.status), "writable": p.writable and not lock,
                         "texts": p.texts, "text": value_text(p.texts, st.value),
                         "can_write": not why, "why": why, "wait_s": wait, "dashboard": p.dashboard})
        return {"device": d.id, "name": d.name, "bus": d.bus, "write_enabled": enabled,
                "budget_remaining": self.guard.remaining_budget,
                "budget": self.cfg.safety.daily_write_budget, "params": rows}

    def dashboard(self) -> dict:
        """Startseite: Dienststatus, letzte Aktualisierung, Geräte online und die in der Werteliste
        für das Dashboard markierten Messwerte und Parameter."""
        wall, mono = time.time(), self.store.now()
        groups, devices, last_ok = [], [], 0.0
        for d in self.cfg.devices:
            if not d.enabled:
                continue
            ds = self.store.device(d.id)
            last_ok = max(last_ok, ds.last_ok)
            age = self.store.age_s(d.id)
            devices.append({"id": d.id, "name": d.name or d.id, "online": ds.online,
                            "age_s": None if age >= 0xFFFF else age, "error": ds.last_error_text})
            values = []
            for e in d.values:
                if not (e.active and e.dashboard):
                    continue
                st = self.store.get(d.id, "value", e.key)
                values.append({"key": e.key, "nr": _entry_nr(e), "unit": e.unit, "decimals": e.decimals,
                               "name": display_name(e.name, e.nr) if d.bus == "can" else e.name,
                               "value": st.value, "status": int(st.status), "texts": e.texts,
                               "text": value_text(e.texts, st.value)})
            params = [p for p in self.param_overview(d.id)["params"] if p["dashboard"]]
            if values or params:
                groups.append({"id": d.id, "name": d.name or d.id, "online": ds.online, "values": values,
                               "params": params})
        return {"running": True, "version": __version__, "uptime_s": int(time.time() - self.started),
                "polling": self.poller.state, "warnings": self.warnings(),
                "last_update_age_s": None if not last_ok else max(0.0, mono - last_ok),
                "last_update": None if not last_ok else
                dt.datetime.fromtimestamp(wall - (mono - last_ok)).isoformat(timespec="seconds"),
                "online": sum(1 for x in devices if x["online"]), "total": len(devices), "devices": devices,
                "groups": groups, "write_enabled": self.cfg.safety.write_enabled,
                "budget_remaining": self.guard.remaining_budget, "budget": self.cfg.safety.daily_write_budget}

    async def write_param_web(self, device_id: str, key: int, value: float) -> dict:
        d = self.cfg.device(device_id)
        param = next((p for p in d.params if p.key == key), None)
        if param is None:
            raise WriteRejected("address", f"Parameter {key:#06x} ist nicht konfiguriert")
        return await self.write_param(d, param, value, source="Web")

    def write_history(self) -> list[dict]:
        return list(reversed(self._history))

    # ===== Jobs =================================================================================
    def _start_job(self, kind: str, coro_fn) -> Job:
        job = Job(kind)
        self.jobs[job.id] = job
        # alte Jobs begrenzen
        for old in list(self.jobs)[:-50]:
            self.jobs.pop(old, None)

        async def runner():
            try:
                job.result = await coro_fn(job)
                job.state = "done"
            except Exception as exc:  # noqa: BLE001
                log.warning("Auftrag %s fehlgeschlagen: %s", kind, exc)
                job.error, job.state = str(exc) or type(exc).__name__, "error"

        job.task = asyncio.create_task(runner(), name=f"job-{kind}")
        return job

    def get_job(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def _busy(self) -> bool:
        return any(j.state == "running" and j.kind in ("scan", "enumerate", "can_test") for j in self.jobs.values())

    # ===== Scan =================================================================================
    def start_scan(self, can: bool, lc1_ports: list[str] | None, full: bool = False) -> Job:
        if self._busy():
            raise ValueError("Es läuft bereits eine Suche oder Aufzählung")

        async def run(job: Job) -> list[dict]:
            found: list[DeviceInfo] = []
            async with self.poller.pause():
                if can:
                    drv = await self._can_driver_for(self.draft)
                    found += await drv.scan(job.set_progress, full=full)
                if lc1_ports:
                    from gateway.serial_lc1.driver import Lc1Driver
                    drv = self.driver("lc1") or Lc1Driver(self.cfg, trace=self.trace)
                    found += await drv.scan(job.set_progress, ports=lc1_ports)
            self._last_scan = found
            return [self._info_dict(i) for i in found]

        return self._start_job("scan", run)

    async def _can_driver_for(self, cfg: GatewayConfig) -> BusDriver:
        """CAN-Treiber mit den Einstellungen aus `cfg` (Entwurf) – öffnet bei Bedarf neu."""
        drv = self.driver("can")
        if drv is not None and cfg.can.model_dump() != self.cfg.can.model_dump():
            with contextlib.suppress(Exception):
                await drv.close()
            self._drivers.pop("can", None)
            drv = None
        if drv is None:
            from gateway.can_lc2.driver import CanDriver
            tmp = self.cfg.model_copy(deep=True)
            tmp.can = cfg.can.model_copy(update={"enabled": True})
            drv = CanDriver(tmp, trace=self.trace)
            self._drivers["can"] = drv
        if not getattr(drv, "is_open", False):
            await drv.open()
        return drv

    @staticmethod
    def _info_dict(i: DeviceInfo) -> dict:
        return {"bus": i.bus, "address": i.address, "port": i.port, "regler_typ": i.regler_typ,
                "type_name": i.type_name, "name": i.name, "version": i.version, "serial": i.serial,
                "system_nr": i.system_nr}

    def add_scanned_device(self, info: DeviceInfo | dict) -> DeviceConfig:
        if isinstance(info, dict):
            info = DeviceInfo(**{k: v for k, v in info.items() if k in DeviceInfo.__dataclass_fields__})
        for d in self.draft.devices:
            if (info.bus == "can" and d.bus == "can" and d.address == info.address) or \
                    (info.bus == "lc1" and d.bus == "lc1" and d.port == info.port):
                d.regler_typ, d.serial, d.version = info.regler_typ, info.serial, info.version
                d.name = d.name or info.name
                return d
        dev_id = _device_id_for(info)
        n = 2
        while any(d.id == dev_id for d in self.draft.devices):
            dev_id = f"{_device_id_for(info)}-{n}"
            n += 1
        dev = DeviceConfig(
            id=dev_id, bus=info.bus,
            address=info.address if info.bus == "can" else None,
            port=info.port if info.bus == "lc1" else None,
            regler_typ=info.regler_typ, name=(info.name or info.type_name)[:60], serial=info.serial,
            version=info.version, unit_id=suggest_unit_id(self.draft, info.bus, info.address))
        self.draft.devices.append(dev)
        if info.bus == "can":
            self.draft.can.enabled = True
        return dev

    # ===== Werteliste ===========================================================================
    def start_enumerate(self, device_id: str) -> Job:
        if self._busy():
            raise ValueError("Es läuft bereits eine Suche oder Aufzählung")
        dev = self.draft.device(device_id)

        async def run(job: Job) -> list[ValueInfo]:
            async with self.poller.pause():
                if dev.bus == "can":
                    drv = await self._can_driver_for(self.draft)
                else:
                    from gateway.serial_lc1.driver import Lc1Driver
                    drv = self.driver("lc1") or Lc1Driver(self.draft, trace=self.trace)
                    if hasattr(drv, "reload"):
                        drv.reload(self.draft)
                infos = await drv.enumerate(dev, job.set_progress)
            self._enum_cache[device_id] = infos
            self._merge_enum(dev, infos)
            return infos

        return self._start_job("enumerate", run)

    def enum_cache(self, device_id: str) -> list[ValueInfo] | None:
        return self._enum_cache.get(device_id)

    @staticmethod
    def _merge_enum(dev: DeviceConfig, infos: list[ValueInfo]) -> None:
        values = {v.key: v for v in dev.values}
        params = {p.key: p for p in dev.params}
        for i in infos:
            meta = dict(name=i.name[:80], unit=i.unit, decimals=max(0, min(6, i.decimals)), vartyp=i.vartyp,
                        out_textauswahl=i.out_textauswahl, out_type=i.out_type, min=i.min, max=i.max,
                        texts=dict(i.texts) if i.texts else None)
            if i.kind == "value":
                cur = values.get(i.key)
                if cur is None:
                    keyf = {"nr": i.key} if dev.bus == "can" else {"k": i.key}
                    dev.values.append(ValueDef(**keyf, active=False, **meta))
                else:
                    for k, v in meta.items():
                        setattr(cur, k, v)
                    if not i.supported:
                        cur.active = False
            else:
                cur = params.get(i.key)
                if cur is None:
                    keyf = {"nr": i.key} if dev.bus == "can" else {"e": i.key}
                    dev.params.append(ParamDef(**keyf, active=False, default=i.default, **meta))
                else:
                    for k, v in meta.items():
                        setattr(cur, k, v)
                    cur.default = i.default
                    if not i.supported:
                        cur.active = False
                    if i.lock_reason:
                        cur.writable = False

    def update_draft_device(self, device_id: str, *, unit_id: int | None = None, name: str | None = None,
                            enabled: bool | None = None, entries: list[dict] | None = None) -> None:
        dev = self.draft.device(device_id)
        if unit_id is not None:
            if any(d.unit_id == unit_id and d.id != device_id for d in self.draft.devices) \
                    or unit_id == self.draft.modbus.diag_unit_id:
                raise ValueError(f"Unit-ID {unit_id} ist schon vergeben")
            dev.unit_id = unit_id
        if name is not None:
            dev.name = name[:60]
        if enabled is not None:
            dev.enabled = enabled
        infos = {(i.kind, i.key): i for i in self._enum_cache.get(device_id, [])}
        for e in entries or []:
            kind, key = e["kind"], int(e["key"])
            table = dev.values if kind == "value" else dev.params
            entry = next((x for x in table if x.key == key), None)
            if entry is None:
                raise ValueError(f"{kind} {key:#06x} ist nicht in der Werteliste – erst aufzählen")
            info = infos.get((kind, key))
            if e.get("active") and info is not None and not info.supported:
                raise ValueError(f"{entry.name or hex(key)}: Datentyp wird nicht bereitgestellt")
            if kind == "param" and e.get("writable"):
                out_type = info.out_type if info is not None else getattr(entry, "out_type", None)
                reason = safety.param_lock_reason(dev.bus, dev.regler_typ, key, out_type=out_type)
                if reason:
                    raise ValueError(f"{entry.name or hex(key)} ist gesperrt: {reason}")
            if "active" in e:
                entry.active = bool(e["active"])
            if "dashboard" in e:
                entry.dashboard = bool(e["dashboard"]) and entry.active
            if kind == "param" and "writable" in e:
                entry.writable = bool(e["writable"])

    def remove_draft_device(self, device_id: str) -> None:
        self.draft.devices = [d for d in self.draft.devices if d.id != device_id]

    _SECTIONS = {"can", "modbus", "poll", "safety", "web", "lc1", "history"}

    def update_draft_settings(self, section: str, values: dict) -> None:
        if section not in self._SECTIONS:
            raise ValueError(f"Unbekannter Abschnitt {section}")
        values = {k: v for k, v in values.items() if k != "password_hash"}
        cur = getattr(self.draft, section)
        try:
            new = type(cur).model_validate({**cur.model_dump(), **values})
        except Exception as exc:  # pydantic.ValidationError
            raise ValueError(str(exc)) from exc
        setattr(self.draft, section, new)

    def renumber_registers(self, device_id: str, table: Literal["values", "params"]) -> None:
        dev = self.draft.device(device_id)
        entries = getattr(dev, table)
        kept_active = renumber_registers(entries)
        inactive = [e for e in entries if not e.active]
        for e in inactive:
            e.reg = None
        setattr(dev, table, kept_active + inactive)

    def discard_draft(self) -> None:
        self.draft = self.cfg.model_copy(deep=True)

    async def apply_draft(self) -> list[str]:
        try:
            new = GatewayConfig.model_validate(self.draft.model_dump())
            assign_all_registers(new)
            new = GatewayConfig.model_validate(new.model_dump())
        except Exception as exc:
            raise ValueError(str(exc)) from exc
        save_config(new, self.config_path)
        old = self.cfg
        async with self.poller.pause():
            self.cfg = new
            self.store.stale_after_s = new.poll.stale_after_s
            self.guard.cfg = new.safety
            await self._reconfigure_drivers(old, new)
            self.poller.reload(new)
            if self.modbus:
                self.modbus.reload(new)
        self._history_wake.set()
        self.draft = new.model_copy(deep=True)
        warnings = self.warnings()
        if old.web.model_dump(exclude={"password_hash"}) != new.web.model_dump(exclude={"password_hash"}):
            warnings.append("Weboberfläche: Adresse/Port werden erst nach einem Neustart des Dienstes übernommen")
        log.info("Konfiguration gespeichert und übernommen (%s)", self.config_path)
        return warnings

    # ===== CAN-Test =============================================================================
    def start_can_test(self, interface: str, channel: str, bitrate: int) -> Job:
        if self._busy():
            raise ValueError("Es läuft bereits eine Suche oder Aufzählung")

        async def run(job: Job) -> dict:
            from gateway.can_lc2.selftest import can_selftest
            tmp = self.draft.model_copy(deep=True)
            tmp.can = tmp.can.model_copy(update={"enabled": True, "interface": interface,
                                                 "channel": str(channel), "bitrate": int(bitrate)})
            async with self.poller.pause():
                drv = self._drivers.pop("can", None)   # Adapter freigeben (nur ein Prozess/Handle)
                if drv is not None:
                    with contextlib.suppress(Exception):
                        await drv.close()
                try:
                    job.set_progress(0, 1, "Teste CAN-Schnittstelle …")
                    results = await can_selftest(tmp)
                finally:
                    if self.cfg.can.enabled and not self.dummy:
                        self._create_drivers(self.cfg)
                        with contextlib.suppress(Exception):
                            await self._drivers["can"].open()
            details = [{"name": n, "ok": ok, "text": t} for n, ok, t in results]
            ok = all(d["ok"] for d in details)
            return {"ok": ok, "text": "CAN-Schnittstelle in Ordnung" if ok else "CAN-Test mit Fehlern",
                    "details": details}

        return self._start_job("can_test", run)

    def list_serial_ports(self) -> list[dict]:
        from gateway.serial_lc1.driver import list_serial_ports
        return list_serial_ports()

    # ===== Status ===============================================================================
    def warnings(self) -> list[str]:
        w = []
        if not self.cfg.modbus.allow:
            w.append("Modbus-IP-Allowlist ist leer: JEDER Client im Netz darf zugreifen!")
        if self.cfg.safety.write_enabled:
            w.append("Schreiben über Modbus ist global freigegeben")
        if self.dummy:
            w.append("Dummy-Modus: es werden keine echten Geräte abgefragt")
        if self._history_warning:
            w.append(self._history_warning)
        if self.cfg.web.bind not in ("127.0.0.1", "localhost", "::1") and not self.cfg.web.password_hash:
            w.append("Weboberfläche ist im Netz freigegeben, aber ohne Passwort – aus dem Netz gesperrt, "
                     "bis unter „1 Schnittstellen“ ein Passwort gesetzt ist")
        return w

    def status(self) -> dict:
        diag = self.store.diag
        devices = []
        for d in self.cfg.devices:
            st = self.store.device(d.id)
            devices.append({"id": d.id, "name": d.name, "bus": d.bus, "address": d.address, "port": d.port,
                            "unit_id": d.unit_id, "enabled": d.enabled, "online": st.online,
                            "last_error": st.last_error_text or (st.last_error.name if st.last_error else ""),
                            "write_alarm": st.write_alarm, "age_s": self.store.age_s(d.id)})
        return {"running": True, "version": __version__, "uptime_s": int(time.time() - self.started),
                "write_enabled": self.cfg.safety.write_enabled, "warnings": self.warnings(),
                "polling": "running" if self.dummy else self.poller.state,
                "diag": {"heartbeat": diag.heartbeat, "cycle_time_ms": diag.cycle_time_ms,
                         "can_error_count": diag.can_error_count, "writes_today": self.guard.writes_today,
                         "budget_remaining": self.guard.remaining_budget},
                "devices": devices}

    def device_values(self, device_id: str) -> list[dict]:
        d = self.cfg.device(device_id)
        out = []
        for kind, table in (("value", d.values), ("param", d.params)):
            for e in table:
                if not e.active:
                    continue
                st = self.store.get(d.id, kind, e.key)
                name = display_name(e.name, e.nr) if d.bus == "can" else e.name
                out.append({"kind": kind, "key": e.key, "reg": e.reg, "name": name, "unit": e.unit,
                            "value": st.value, "status": int(st.status), "text": value_text(e.texts, st.value),
                            "writable": bool(getattr(e, "writable", False)) and self.cfg.safety.write_enabled})
        return out

    # ===== Passwort =============================================================================
    def set_password(self, password: str) -> None:
        if len(password) < 8:
            raise ValueError("Passwort muss mindestens 8 Zeichen haben")
        h = hash_password(password)
        self.cfg.web.password_hash = h
        self.draft.web.password_hash = h
        save_config(self.cfg, self.config_path)

    def check_password(self, password: str) -> bool:
        return verify_password(password, self.cfg.web.password_hash)


def _entry_nr(e) -> str:
    from gateway.registermap import entry_nr
    return entry_nr(None, e)
