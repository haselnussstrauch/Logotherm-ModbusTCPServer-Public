"""Tests für gateway.modbus_server (echter pymodbus-Server auf 127.0.0.1)."""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import sys
import types

import pytest


from pymodbus.client import AsyncModbusTcpClient

from gateway.config import GatewayConfig
from gateway.errors import WriteRejected
from gateway.modbus_server import (
    IpAllowlist,
    ModbusGateway,
    make_dummy_write_handler,
    run_dummy_feeder,
)
from gateway.registermap import decode_float, encode_float
from gateway.store import ValueStore

def validate_cfg(raw: dict):
    """GatewayConfig validieren; fehlt gateway.safety noch, wird nur währenddessen ein Stub eingehängt
    (kein Einfluss auf andere Tests, z. B. importorskip)."""
    from importlib.util import find_spec
    if find_spec("gateway.safety") is not None:
        return GatewayConfig.model_validate(raw)
    stub = types.ModuleType("gateway.safety")
    stub.param_lock_reason = lambda *a, **k: None
    sys.modules["gateway.safety"] = stub
    try:
        return GatewayConfig.model_validate(raw)
    finally:
        sys.modules.pop("gateway.safety", None)


UNIT = 16


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_cfg(port: int, **modbus) -> GatewayConfig:
    raw = {
        "modbus": {"bind": "127.0.0.1", "port": port, "allow": ["127.0.0.1"], **modbus},
        "safety": {"write_enabled": True},
        "devices": [{
            "id": "hk1", "bus": "can", "address": 0x10, "unit_id": UNIT,
            "values": [
                {"nr": 1, "reg": 0, "name": "Vorlauf", "decimals": 1},
                {"nr": 2, "reg": 2, "name": "Aus", "active": False},
            ],
            "params": [
                {"nr": 0x100, "reg": 0, "name": "Soll", "decimals": 1, "min": 10, "max": 30},
                {"nr": 0x101, "reg": 2, "name": "Nacht", "decimals": 1, "min": 5, "max": 25},
            ],
        }],
    }
    cfg = validate_cfg(raw)
    for p in cfg.devices[0].params:      # nach der Validierung (safety-Prüfung nicht Gegenstand)
        p.writable = True
    return cfg


@pytest.fixture
async def env():
    port = free_port()
    cfg = make_cfg(port)
    store = ValueStore()
    gw = ModbusGateway(cfg, store, make_dummy_write_handler(store))
    await gw.start()
    client = AsyncModbusTcpClient("127.0.0.1", port=port, timeout=2, retries=1)
    assert await client.connect()
    yield types.SimpleNamespace(gw=gw, store=store, client=client, port=port, cfg=cfg)
    client.close()
    await gw.stop()


def words(value: float, order: str = "ABCD") -> list[int]:
    return encode_float(value, order)


async def test_read_input_and_holding(env):
    env.store.set_value("hk1", "value", 1, 21.5)
    env.store.set_value("hk1", "param", 0x100, 22.0)
    rr = await env.client.read_input_registers(0, count=2, device_id=UNIT)
    assert not rr.isError()
    assert decode_float(rr.registers) == 21.5
    rr = await env.client.read_holding_registers(0, count=2, device_id=UNIT)
    assert decode_float(rr.registers) == 22.0
    # Status
    rr = await env.client.read_input_registers(1000, count=1, device_id=UNIT)
    assert rr.registers == [0]
    # Werte werden bei jeder Anfrage frisch gelesen
    env.store.set_value("hk1", "value", 1, 23.0)
    rr = await env.client.read_input_registers(0, count=2, device_id=UNIT)
    assert decode_float(rr.registers) == 23.0


async def test_diag_unit_heartbeat(env):
    env.store.diag.heartbeat = 7
    rr = await env.client.read_input_registers(0, count=9, device_id=247)
    assert not rr.isError() and rr.registers[0] == 7


async def test_read_gaps_exception_02(env):
    for addr, cnt in ((2, 2), (1, 1), (0, 1), (5000, 1)):
        rr = await env.client.read_input_registers(addr, count=cnt, device_id=UNIT)
        assert rr.isError() and rr.exception_code == 2, (addr, cnt)


async def test_write_fc16_updates_store(env):
    rr = await env.client.write_registers(0, words(21.5), device_id=UNIT)
    assert not rr.isError()
    assert env.store.get("hk1", "param", 0x100).value == 21.5
    rr = await env.client.read_holding_registers(0, count=2, device_id=UNIT)
    assert decode_float(rr.registers) == 21.5


async def test_write_multiple_pairs_rejected(env):
    """Nur genau ein Paar je FC 16 – mehrere Paare -> 02, nichts wird geschrieben."""
    rr = await env.client.write_registers(0, words(20.0) + words(15.0), device_id=UNIT)
    assert rr.isError() and rr.exception_code == 2
    assert env.store.get("hk1", "param", 0x100).value is None
    assert env.store.get("hk1", "param", 0x101).value is None


async def test_write_out_of_range_exception_03(env):
    for v in (9.9, 30.1, float("nan")):
        rr = await env.client.write_registers(0, words(v), device_id=UNIT)
        assert rr.isError() and rr.exception_code == 3, v
    assert env.store.get("hk1", "param", 0x100).value is None


async def test_write_invalid_targets_exception_02(env):
    cases = {
        "status": (1000, words(1.0)),
        "half pair start": (1, words(20.0)),
        "half pair single word": (0, [0x41A0]),
        "unmapped": (500, words(20.0)),
        "diag unit": (0, words(20.0)),
    }
    for name, (addr, data) in cases.items():
        unit = 247 if name == "diag unit" else UNIT
        rr = await env.client.write_registers(addr, data, device_id=unit)
        assert rr.isError() and rr.exception_code == 2, name
    assert env.store.get("hk1", "param", 0x100).value is None


async def test_fc06_illegal_function(env):
    rr = await env.client.write_register(0, 1, device_id=UNIT)
    assert rr.isError() and rr.exception_code == 1


async def test_other_write_function_codes_illegal(env):
    rr = await env.client.write_coil(0, True, device_id=UNIT)
    assert rr.isError() and rr.exception_code == 1
    rr = await env.client.read_coils(0, count=1, device_id=UNIT)
    assert rr.isError() and rr.exception_code == 1
    rr = await env.client.readwrite_registers(read_address=0, read_count=2, address=0,
                                              values=words(20.0), device_id=UNIT)
    assert rr.isError() and rr.exception_code == 1
    assert env.store.get("hk1", "param", 0x100).value is None


async def test_unknown_unit_gateway_exception(env):
    rr = await env.client.read_input_registers(0, count=2, device_id=99)
    assert rr.isError() and rr.exception_code == 0x0B
    rr = await env.client.write_registers(0, words(20.0), device_id=99)
    assert rr.isError() and rr.exception_code == 0x0B


async def test_write_handler_rejection_kinds(env):
    async def reject_addr(dev, par, val):
        raise WriteRejected("address", "gesperrt")

    async def reject_val(dev, par, val):
        raise WriteRejected("value", "Budget")

    env.gw.write_handler = reject_addr
    rr = await env.client.write_registers(0, words(20.0), device_id=UNIT)
    assert rr.isError() and rr.exception_code == 2
    env.gw.write_handler = reject_val
    rr = await env.client.write_registers(0, words(20.0), device_id=UNIT)
    assert rr.isError() and rr.exception_code == 3

    async def boom(dev, par, val):
        raise RuntimeError("kaputt")

    env.gw.write_handler = boom
    rr = await env.client.write_registers(0, words(20.0), device_id=UNIT)
    assert rr.isError() and rr.exception_code == 4


async def test_dummy_handler_respects_writable(env):
    env.cfg.devices[0].params[0].writable = False
    rr = await env.client.write_registers(0, words(20.0), device_id=UNIT)
    assert rr.isError() and rr.exception_code == 2


async def test_cdab_word_order_over_wire():
    port = free_port()
    cfg = make_cfg(port, word_order="CDAB")
    store = ValueStore()
    gw = ModbusGateway(cfg, store, make_dummy_write_handler(store))
    await gw.start()
    client = AsyncModbusTcpClient("127.0.0.1", port=port, timeout=2, retries=1)
    try:
        assert await client.connect()
        store.set_value("hk1", "value", 1, 1.5)
        rr = await client.read_input_registers(0, count=2, device_id=UNIT)
        assert rr.registers == [0x0000, 0x3FC0]
        rr = await client.write_registers(0, [0x0000, 0x41A0], device_id=UNIT)    # 20.0 in CDAB
        assert not rr.isError()
        assert store.get("hk1", "param", 0x100).value == 20.0
    finally:
        client.close()
        await gw.stop()


# --- Allowlist --------------------------------------------------------------------------------
def test_allowlist_matching():
    al = IpAllowlist(["10.0.0.0/8", "192.168.1.5", "fd00::/8"])
    assert al.allows("10.1.2.3") and al.allows("192.168.1.5") and al.allows("fd00::1")
    assert not al.allows("192.168.1.6") and not al.allows("127.0.0.1") and not al.allows("::1")
    assert al.allows("::ffff:10.1.2.3")                 # IPv4-mapped
    assert not al.allows("müll")
    assert IpAllowlist([]).allows("1.2.3.4")
    with pytest.raises(ValueError):
        IpAllowlist(["kein-netz"])


async def test_allowlist_rejects_foreign_ip(caplog):
    port = free_port()
    cfg = make_cfg(port, allow=["10.0.0.0/8"])
    store = ValueStore()
    gw = ModbusGateway(cfg, store, make_dummy_write_handler(store))
    await gw.start()
    client = AsyncModbusTcpClient("127.0.0.1", port=port, timeout=1, retries=0, reconnect_delay=0)
    try:
        with caplog.at_level(logging.WARNING, logger="gateway.modbus_server"):
            try:
                await client.connect()
                rr = await client.read_input_registers(0, count=2, device_id=UNIT)
                failed = rr.isError()
            except Exception:                           # noqa: BLE001 (Verbindung/Timeout)
                failed = True
            await asyncio.sleep(0.1)
        assert failed
        assert any("abgelehnt" in r.getMessage() and "127.0.0.1" in r.getMessage()
                   for r in caplog.records)
        assert not gw._server.active_connections        # abgelehnte Verbindung aufgeräumt
    finally:
        client.close()
        await gw.stop()


async def test_allowlist_reject_log_rate_limited(caplog):
    cfg = make_cfg(free_port(), allow=["10.0.0.0/8"])
    gw = ModbusGateway(cfg, ValueStore(), make_dummy_write_handler(ValueStore()))
    with caplog.at_level(logging.WARNING, logger="gateway.modbus_server"):
        for _ in range(5):
            assert gw.check_peer("1.2.3.4") is False
    assert sum("abgelehnt" in r.getMessage() for r in caplog.records) == 1


async def test_empty_allowlist_warns_on_start(caplog):
    port = free_port()
    cfg = make_cfg(port, allow=[])
    store = ValueStore()
    gw = ModbusGateway(cfg, store, make_dummy_write_handler(store))
    with caplog.at_level(logging.WARNING, logger="gateway.modbus_server"):
        await gw.start()
    try:
        assert any("Allowlist ist leer" in r.getMessage() for r in caplog.records)
        client = AsyncModbusTcpClient("127.0.0.1", port=port, timeout=2, retries=1)
        assert await client.connect()
        rr = await client.read_input_registers(0, count=2, device_id=UNIT)
        assert not rr.isError()
        client.close()
    finally:
        await gw.stop()


# --- Reload -----------------------------------------------------------------------------------
async def test_reload_changes_map_and_allowlist(env):
    rr = await env.client.read_input_registers(2000, count=1, device_id=UNIT)
    assert rr.isError() and rr.exception_code == 2
    new = make_cfg(env.port, int16_mirror=True)
    assert env.gw.reload(new) is None                   # gleicher Port: kein Neustart
    rr = await env.client.read_input_registers(2000, count=1, device_id=UNIT)
    assert not rr.isError() and rr.registers == [0x8000]
    # Allowlist wirkt für neue Verbindungen
    env.gw.reload(make_cfg(env.port, allow=["10.0.0.0/8"]))
    assert not env.gw.allowlist.allows("127.0.0.1")


async def test_reload_port_change_restarts(env):
    new_port = free_port()
    task = env.gw.reload(make_cfg(new_port))
    assert task is not None
    await task
    client = AsyncModbusTcpClient("127.0.0.1", port=new_port, timeout=2, retries=1)
    try:
        assert await client.connect()
        rr = await client.read_input_registers(1000, count=1, device_id=UNIT)
        assert not rr.isError()
    finally:
        client.close()


# --- Dummy-Feeder -----------------------------------------------------------------------------
async def test_dummy_feeder_fills_store():
    cfg = make_cfg(free_port())
    store = ValueStore()
    stop = asyncio.Event()
    task = asyncio.create_task(run_dummy_feeder(cfg, store, stop))
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert store.get("hk1", "value", 1).value is not None
    assert store.get("hk1", "value", 2).value is None                 # inaktiv
    assert store.get("hk1", "param", 0x100).value is not None
    assert store.device("hk1").online
    assert store.diag.heartbeat == 1
    v = store.get("hk1", "value", 1).value
    assert v == round(v, 1)
    assert struct.pack(">f", v)       # darstellbar
