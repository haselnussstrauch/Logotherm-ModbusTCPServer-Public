"""Tests für Poller und Service (Schreibpfad, Entwurf, Hot-Reload) mit einem Fake-Treiber."""

from __future__ import annotations

import asyncio

import pytest

from gateway.config import GatewayConfig, load_config
from gateway.drivers import DeviceInfo, DeviceTimeout, ValueInfo
from gateway.errors import WriteRejected
from gateway.poller import Poller
from gateway.service import Service, hash_password, verify_password
from gateway.store import DeviceError, ValueStatus, ValueStore


class FakeDriver:
    bus = "can"

    def __init__(self):
        self.is_open = True
        self.values = {0x0003: 61.5, 0x0002: 70.0}
        self.params = {0x0141: 60.0}
        self.offline = False
        self.writes: list[tuple[int, float]] = []
        self.readback_offset = 0.0
        self.write_timeout = False

    async def open(self):
        self.is_open = True

    async def close(self):
        self.is_open = False

    async def scan(self, progress=None, **kw):
        if progress:
            progress(1, 1, "fertig")
        return [DeviceInfo(bus="can", address=0x01, regler_typ=0x0B00, type_name="LambdaControl 2 Neu",
                           name="Kessel", version="1.29", serial=4711)]

    async def enumerate(self, device, progress=None):
        return [
            ValueInfo(kind="value", key=0x0003, name="TK Ist", unit="°C", decimals=1, vartyp=0x01),
            ValueInfo(kind="value", key=0x0010, name="Datum", vartyp=0x07, supported=False),
            ValueInfo(kind="param", key=0x0141, name="TK Soll", unit="°C", vartyp=0x01, min=50, max=90,
                      default=75),
            ValueInfo(kind="param", key=0xFE42, name="Busadresse", vartyp=0x00, lock_reason="Busadresse"),
        ]

    async def read_values(self, device, values):
        if self.offline:
            raise DeviceTimeout("keine Antwort")
        return {v.key: self.values.get(v.key) for v in values}

    async def read_params(self, device, params):
        if self.offline:
            raise DeviceTimeout("keine Antwort")
        return {p.key: self.params.get(p.key) for p in params}

    async def write_param(self, device, param, value):
        if self.write_timeout:
            raise DeviceTimeout("keine Bestätigung")
        self.writes.append((param.key, value))
        self.params[param.key] = value + self.readback_offset
        return self.params[param.key]

    def stats(self):
        return {"errors": 3, "tx": 0, "rx": 0, "timeouts": 0}


def make_cfg(write=True) -> GatewayConfig:
    return GatewayConfig.model_validate({
        "safety": {"write_enabled": write},
        "can": {"enabled": True},
        "poll": {"interval_s": 1, "param_interval_s": 1},
        "modbus": {"allow": ["127.0.0.1"]},
        "devices": [{
            "id": "can-01", "bus": "can", "address": 1, "regler_typ": 0x0B00, "unit_id": 1,
            "values": [{"nr": 0x0003, "reg": 0, "decimals": 1, "vartyp": 1},
                       {"nr": 0x0002, "reg": 2, "decimals": 1, "vartyp": 1, "active": False}],
            "params": [{"nr": 0x0141, "reg": 0, "vartyp": 1, "min": 50, "max": 90, "writable": write}],
        }],
    })


@pytest.fixture
def svc(tmp_path):
    s = Service(make_cfg(), tmp_path / "gateway.yaml")
    drv = FakeDriver()
    s._drivers["can"] = drv
    s.fake = drv
    return s


async def test_poller_reads_values_and_params(svc):
    dev = svc.cfg.devices[0]
    await svc.poller.poll_device(svc.fake, dev)
    assert svc.store.get("can-01", "value", 0x0003).value == 61.5
    assert svc.store.get("can-01", "param", 0x0141).value == 60.0
    assert svc.store.get("can-01", "value", 0x0002).status == ValueStatus.TIMEOUT  # inaktiv, nie gelesen
    assert svc.store.device("can-01").online


async def test_poller_timeout_marks_offline(svc):
    dev = svc.cfg.devices[0]
    await svc.poller.poll_device(svc.fake, dev)
    svc.fake.offline = True
    svc.poller._last_params.clear()
    await svc.poller.poll_device(svc.fake, dev)
    st = svc.store.device("can-01")
    assert not st.online and st.last_error == DeviceError.TIMEOUT
    v = svc.store.get("can-01", "value", 0x0003)
    assert v.status == ValueStatus.TIMEOUT and v.value == 61.5        # letzter Wert bleibt


async def test_poller_loop_runs_and_pauses(svc):
    svc.poller.start()
    await asyncio.sleep(0.1)
    assert svc.store.diag.heartbeat >= 1
    assert svc.store.diag.can_error_count == 3
    async with svc.poller.pause():
        assert svc.poller.state == "paused"
        hb = svc.store.diag.heartbeat
        await asyncio.sleep(0.2)
        assert svc.store.diag.heartbeat == hb
    assert svc.poller.state == "running"
    await svc.poller.stop()


async def test_write_path_ok(svc):
    dev, p = svc.cfg.devices[0], svc.cfg.devices[0].params[0]
    await svc.write_param(dev, p, 65.0)
    assert svc.fake.writes == [(0x0141, 65.0)]
    assert svc.store.get("can-01", "param", 0x0141).value == 65.0
    assert svc.guard.writes_today == 1
    h = svc.write_history()[0]
    assert h["ok"] and h["new"] == 65.0 and h["readback"] == 65.0


async def test_write_rate_limit_and_deadband(svc):
    dev, p = svc.cfg.devices[0], svc.cfg.devices[0].params[0]
    await svc.write_param(dev, p, 65.0)
    await svc.write_param(dev, p, 65.0)          # Totband: nicht gesendet, aber ok
    assert len(svc.fake.writes) == 1
    with pytest.raises(WriteRejected) as e:
        await svc.write_param(dev, p, 70.0)      # 10-min-Sperre
    assert e.value.kind == "value"


async def test_write_out_of_range_never_sent(svc):
    dev, p = svc.cfg.devices[0], svc.cfg.devices[0].params[0]
    with pytest.raises(WriteRejected):
        await svc.write_param(dev, p, 95.0)
    assert svc.fake.writes == []


async def test_write_disabled_globally(tmp_path):
    s = Service(make_cfg(write=False), tmp_path / "g.yaml")
    s._drivers["can"] = FakeDriver()
    dev = s.cfg.devices[0]
    with pytest.raises(WriteRejected) as e:
        await s.write_param(dev, dev.params[0], 60.0)
    assert e.value.kind == "address"


async def test_write_readback_mismatch_sets_alarm(svc):
    svc.fake.readback_offset = 5.0
    dev, p = svc.cfg.devices[0], svc.cfg.devices[0].params[0]
    await svc.write_param(dev, p, 65.0)
    st = svc.store.device("can-01")
    assert st.write_alarm and st.last_error == DeviceError.WRITE_MISMATCH
    assert not svc.write_history()[0]["ok"]


async def test_write_timeout_counts_and_alarms(svc):
    svc.fake.write_timeout = True
    dev, p = svc.cfg.devices[0], svc.cfg.devices[0].params[0]
    with pytest.raises(RuntimeError):
        await svc.write_param(dev, p, 65.0)
    assert svc.guard.writes_today == 1          # gesendet -> verbucht
    assert svc.store.device("can-01").write_alarm


async def test_write_rejected_while_paused(svc):
    svc.poller.start()
    dev, p = svc.cfg.devices[0], svc.cfg.devices[0].params[0]
    async with svc.poller.pause():
        with pytest.raises(WriteRejected):
            await svc.write_param(dev, p, 65.0)
    await svc.poller.stop()


async def wait_job(job):
    while job.state == "running":
        await asyncio.sleep(0.01)
    assert job.state == "done", job.error
    return job.result


async def test_scan_add_enumerate_apply(tmp_path):
    s = Service(GatewayConfig(), tmp_path / "gateway.yaml")
    drv = FakeDriver()

    async def fake_can_driver(cfg):
        return drv
    s._can_driver_for = fake_can_driver
    found = await wait_job(s.start_scan(can=True, lc1_ports=None))
    assert found[0]["address"] == 1
    dev = s.add_scanned_device(found[0])
    assert dev.id == "can-01" and dev.unit_id == 1 and s.draft.can.enabled
    infos = await wait_job(s.start_enumerate(dev.id))
    assert len(infos) == 4
    d = s.draft.device("can-01")
    assert all(not v.active for v in d.values) and all(p.reg is None for p in d.params)
    with pytest.raises(ValueError, match="nicht bereitgestellt"):
        s.update_draft_device("can-01", entries=[{"kind": "value", "key": 0x0010, "active": True}])
    with pytest.raises(ValueError, match="gesperrt"):
        s.update_draft_device("can-01", entries=[{"kind": "param", "key": 0xFE42, "active": True, "writable": True}])
    s.update_draft_device("can-01", entries=[{"kind": "value", "key": 0x0003, "active": True},
                                             {"kind": "param", "key": 0x0141, "active": True}])
    s.cfg.can.interface = "virtual"
    s._reconfigure_drivers = _noop_reconf
    warnings = await s.apply_draft()
    assert any("Allowlist" in w for w in warnings)
    cfg = load_config(tmp_path / "gateway.yaml")
    d = cfg.device("can-01")
    assert next(v for v in d.values if v.key == 0x0003).reg == 0
    assert next(v for v in d.values if v.key == 0x0010).reg is None
    assert next(p for p in d.params if p.key == 0x0141).default == 75


async def _noop_reconf(old, new):
    return None


async def test_scan_rejected_while_busy(svc):
    async def slow(cfg):
        await asyncio.sleep(0.2)
        return svc.fake
    svc._can_driver_for = slow
    svc.start_scan(can=True, lc1_ports=None)
    with pytest.raises(ValueError):
        svc.start_scan(can=True, lc1_ports=None)
    await asyncio.sleep(0.3)


def test_update_settings_validation(svc):
    svc.update_draft_settings("modbus", {"port": 1502})
    assert svc.draft.modbus.port == 1502 and svc.cfg.modbus.port == 502
    with pytest.raises(ValueError):
        svc.update_draft_settings("can", {"min_gap_ms": 1, "bitrate": 12345})
    with pytest.raises(ValueError):
        svc.update_draft_settings("devices", {})
    svc.discard_draft()
    assert svc.draft.modbus.port == 502


def test_unit_id_collision(svc):
    svc.draft.devices.append(svc.draft.devices[0].model_copy(update={"id": "x", "address": 2, "unit_id": 2}))
    with pytest.raises(ValueError):
        svc.update_draft_device("x", unit_id=1)


def test_password(svc):
    h = hash_password("geheim123")
    assert verify_password("geheim123", h) and not verify_password("falsch", h) and not verify_password("x", None)
    with pytest.raises(ValueError):
        svc.set_password("kurz")
    svc.set_password("geheim123")
    assert svc.check_password("geheim123") and (svc.config_path).exists()


def test_status_shape(svc):
    st = svc.status()
    assert st["running"] and st["devices"][0]["id"] == "can-01" and "budget_remaining" in st["diag"]


def test_poller_groups():
    cfg = GatewayConfig.model_validate({"can": {"enabled": False}, "devices": [
        {"id": "a", "bus": "can", "address": 1, "unit_id": 1},
        {"id": "b", "bus": "lc1", "port": "COM5", "unit_id": 101},
        {"id": "c", "bus": "lc1", "port": "COM6", "unit_id": 102, "enabled": False}]})
    p = Poller(cfg, ValueStore(), lambda bus: None)
    assert set(p._groups()) == {"lc1:b"}


def test_cli_interface_error_ohne_traceback(monkeypatch, tmp_path, caplog):
    from gateway import main as gmain
    from gateway.drivers import InterfaceError

    def boom(args, cfg, path):
        raise InterfaceError("kein Adapter")

    monkeypatch.setenv("GATEWAY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(gmain, "cmd_capture", boom)
    parser_build = gmain.build_parser

    def patched():
        p = parser_build()
        p._subparsers._group_actions[0].choices["capture"].set_defaults(func=boom)
        return p

    monkeypatch.setattr(gmain, "build_parser", patched)
    assert gmain.cli(["capture", "--duration", "1"]) == 2
