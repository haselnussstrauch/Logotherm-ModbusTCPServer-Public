"""Fake-Dienst für die Web-Tests (implementiert die Service-Schnittstelle im Speicher)."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from gateway.config import DeviceConfig, GatewayConfig, ParamDef, ValueDef, suggest_unit_id
from gateway.drivers import DeviceInfo, ValueInfo


class FakeJob:
    def __init__(self, kind: str, result=None, error=None) -> None:
        self.id = f"job{id(self) % 100000}"
        self.kind = kind
        self.state = "error" if error else "done"
        self.progress = (3, 3, "fertig")
        self.result = result
        self.error = error


class FakeService:
    def __init__(self) -> None:
        self.cfg = GatewayConfig()
        self.draft = self.cfg.model_copy(deep=True)
        self.config_path = Path("gateway.yaml")
        self.started = time.time()
        self.jobs: dict[str, FakeJob] = {}
        self.cache: dict[str, list[ValueInfo]] = {}
        self.warnings: list[str] = []

    # --- Aufträge
    def _job(self, kind, result=None, error=None) -> FakeJob:
        j = FakeJob(kind, result, error)
        self.jobs[j.id] = j
        return j

    def start_scan(self, can, lc1_ports, full=False):
        return self._job("scan", [DeviceInfo(bus="can", address=0x11, regler_typ=0x1234, type_name="LC2",
                                             name="Kessel 1", version="1.29", serial=4711)])

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def start_enumerate(self, device_id):
        dev = self.draft.device(device_id)
        infos = [ValueInfo(kind="value", key=0x0010, name="Kesseltemperatur", unit="°C", decimals=1, value=65.5),
                 ValueInfo(kind="value", key=0x0011, name="Text", vartyp=0x0F, supported=False),
                 ValueInfo(kind="param", key=0x0141, name="Sollwert", unit="°C", min=10, max=90, value=60),
                 ValueInfo(kind="param", key=0x0142, name="Gesperrt", lock_reason="Sicherheitsparameter")]
        self.cache[device_id] = infos
        for i in infos:
            if not i.supported:
                continue
            lst = dev.values if i.kind == "value" else dev.params
            if all(e.key != i.key for e in lst):
                cls = ValueDef if i.kind == "value" else ParamDef
                lst.append(cls(nr=i.key, name=i.name, unit=i.unit, decimals=i.decimals, active=False,
                               reg=2 * len(lst)))
        return self._job("enumerate", infos)

    def enum_cache(self, device_id):
        return self.cache.get(device_id)

    def start_can_test(self, interface, channel, bitrate):
        if interface == "kaputt":
            return self._job("can_test", error="Adapter nicht gefunden")
        return self._job("can_test", {"ok": True, "text": f"{interface} ok", "details": ["a", "b"]})

    def list_serial_ports(self):
        return [{"device": "COM5", "description": "USB Serial", "by_id": None, "ftdi": True}]

    # --- Entwurf
    def add_scanned_device(self, info):
        did = f"can_{info.address:02x}" if info.bus == "can" else "lc1_" + str(len(self.draft.devices))
        d = DeviceConfig(id=did, bus=info.bus, address=info.address, port=info.port, name=info.name,
                         regler_typ=info.regler_typ, unit_id=suggest_unit_id(self.draft, info.bus, info.address))
        self.draft.devices.append(d)
        return d

    def update_draft_device(self, device_id, *, unit_id=None, name=None, enabled=None, entries=None):
        dev = self.draft.device(device_id)
        if unit_id is not None:
            if any(o.unit_id == unit_id and o.id != device_id for o in self.draft.devices):
                raise ValueError(f"Unit-ID {unit_id} doppelt")
            dev.unit_id = unit_id
        if name is not None:
            dev.name = name
        if enabled is not None:
            dev.enabled = enabled
        for e in entries or []:
            lst = dev.values if e["kind"] == "value" else dev.params
            ent = next(x for x in lst if x.key == e["key"])
            if e["writable"] and e["key"] == 0x0142:
                raise ValueError("Parameter ist gesperrt")
            ent.active = e["active"]
            if e["kind"] == "param":
                ent.writable = e["writable"]

    def remove_draft_device(self, device_id):
        self.draft.devices = [d for d in self.draft.devices if d.id != device_id]

    def update_draft_settings(self, section, values):
        if section not in ("can", "modbus", "poll", "safety", "web"):
            raise ValueError("unbekannter Abschnitt")
        cur = getattr(self.draft, section).model_dump()
        cur.update(values)
        try:
            setattr(self.draft, section, type(getattr(self.draft, section)).model_validate(cur))
        except Exception as e:  # noqa: BLE001
            raise ValueError(str(e)) from None

    async def apply_draft(self):
        GatewayConfig.model_validate(self.draft.model_dump())
        self.cfg = self.draft.model_copy(deep=True)
        return list(self.warnings)

    def discard_draft(self):
        self.draft = self.cfg.model_copy(deep=True)

    def renumber_registers(self, device_id, table):
        self.draft.device(device_id)
        self.renumbered = (device_id, table)

    # --- Betrieb
    def status(self):
        return {"running": True, "version": "0.1.0", "uptime_s": 5, "write_enabled": False,
                "warnings": self.warnings, "polling": "running",
                "diag": {"heartbeat": 1, "cycle_time_ms": 12, "can_error_count": 0, "writes_today": 0,
                         "budget_remaining": 50},
                "devices": [{"id": d.id, "name": d.name, "bus": d.bus, "address": d.address, "port": d.port,
                             "unit_id": d.unit_id, "online": True, "last_error": "", "write_alarm": False,
                             "age_s": 1} for d in self.cfg.devices]}

    def device_values(self, device_id):
        self.cfg.device(device_id)
        return [{"kind": "value", "key": 0x10, "reg": 0, "name": "Kesseltemperatur", "unit": "°C",
                 "value": 66.0, "status": 0, "writable": False}]

    def write_history(self):
        return [{"ts": "2026-01-01T00:00:00", "device": "can_11", "key": 0x141, "name": "Sollwert",
                 "old": 60, "new": 61, "readback": 61, "ok": True, "text": ""}]

    def set_password(self, password):
        self.cfg.web.password_hash = hashlib.sha256(password.encode()).hexdigest()
        self.draft.web.password_hash = self.cfg.web.password_hash

    def check_password(self, password):
        return hashlib.sha256(password.encode()).hexdigest() == self.cfg.web.password_hash
