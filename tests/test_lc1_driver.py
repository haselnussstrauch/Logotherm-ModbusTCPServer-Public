"""Tests für den LC1-Treiber (gateway.serial_lc1.driver) gegen den Simulator."""

from __future__ import annotations

import pytest

from gateway import safety
from gateway.config import DeviceConfig, GatewayConfig, Lc1Defaults, ParamDef, ValueDef
from gateway.devices import lc1_tables
from gateway.drivers import DeviceTimeout, InterfaceError, ProtocolError
from gateway.errors import SafetyViolation
from gateway.serial_lc1 import simulator as simmod
from gateway.serial_lc1.driver import Lc1Driver, list_serial_ports
from gateway.serial_lc1.protocol import Lc1WriteUnclear
from gateway.serial_lc1.selftest import lc1_selftest
from gateway.serial_lc1.simulator import Lc1Simulator


def make_cfg(port: str = "sim://t1", checksum: str = "auto", **dev) -> GatewayConfig:
    d = DeviceConfig(id="lc1-a", bus="lc1", port=port, checksum=checksum, unit_id=101,
                     values=[ValueDef(k=2), ValueDef(k=7), ValueDef(k=28)],
                     params=[ParamDef(e=44, writable=True), ParamDef(e=20, decimals=1, writable=True),
                             ParamDef(e=15)], **dev)
    return GatewayConfig(lc1=Lc1Defaults(byte_timeout_s=0.3, line_timeout_s=0.6), devices=[d])


class Factory:
    """Port-Factory für Tests: liefert Simulatoren, zählt Öffnungen, kann Fehler werfen."""

    def __init__(self, **sims: Lc1Simulator) -> None:
        self.sims = sims
        self.opened: list[str] = []
        self.fail: set[str] = set()

    def __call__(self, port: str):
        self.opened.append(port)
        if port in self.fail or port not in self.sims:
            raise OSError(f"{port} gibt es nicht")
        sim = self.sims[port]
        sim.is_open = True
        return sim


@pytest.fixture
def sim():
    return Lc1Simulator()


@pytest.fixture
def setup(sim):
    cfg = make_cfg()
    fac = Factory(**{"sim://t1": sim})
    return cfg, Lc1Driver(cfg, port_factory=fac), sim, fac


def dev(cfg):
    return cfg.devices[0]


def param(cfg, e):
    return next(p for p in dev(cfg).params if p.e == e)


async def test_read_values(setup):
    cfg, drv, sim, _ = setup
    res = await drv.read_values(dev(cfg), dev(cfg).values)
    assert res == {2: pytest.approx(78.4), 7: 0, 28: 1}
    assert sim.commands == [50]


async def test_read_params_scaling(setup):
    cfg, drv, sim, _ = setup
    res = await drv.read_params(dev(cfg), dev(cfg).params)
    assert res[44] == sim.params[44]
    assert res[20] == pytest.approx(sim.params[20] / 10)       # O2 ×10
    assert sim.commands == [48]


async def test_write_param_and_readback(setup):
    cfg, drv, sim, _ = setup
    actual = await drv.write_param(dev(cfg), param(cfg, 20), 7.5)
    assert actual == pytest.approx(7.5)
    assert sim.ee_writes == [(20, 75)]
    assert sim.commands == [49, 48]
    assert not set(sim.sent) & safety.FORBIDDEN_LC1_BYTES


@pytest.mark.parametrize("e", sorted(safety.LC1_LOCKED))
async def test_write_locked_param_never_sends(setup, e):
    cfg, drv, sim, fac = setup
    p = ParamDef(e=e)              # writable=False reicht nicht als Schutz – Treiber prüft selbst
    with pytest.raises(SafetyViolation):
        await drv.write_param(dev(cfg), p, 1)
    assert sim.sent == []


@pytest.mark.parametrize("value", [65536, 70000.0, -1, float("nan"), float("inf")])
async def test_write_value_out_of_range(setup, value):
    cfg, drv, sim, _ = setup
    with pytest.raises(SafetyViolation):
        await drv.write_param(dev(cfg), ParamDef(e=30), value)
    assert sim.sent == []


async def test_write_outside_lcs1par_range_rejected(setup):
    cfg, drv, sim, _ = setup
    with pytest.raises(SafetyViolation, match="LCS1.par"):
        await drv.write_param(dev(cfg), param(cfg, 44), 95)       # E44: 70..85
    assert sim.sent == []


async def test_write_non_number_rejected(setup):
    cfg, drv, sim, _ = setup
    with pytest.raises(SafetyViolation):
        await drv.write_param(dev(cfg), param(cfg, 44), "80")    # type: ignore[arg-type]
    assert sim.sent == []


async def test_read_address_64_rejected(setup):
    cfg, drv, sim, _ = setup
    st = drv._state("sim://t1")
    proto = drv._ensure(st)
    with pytest.raises(SafetyViolation):
        proto.read_params([64])
    with pytest.raises(ValueError):
        ParamDef(e=64)
    assert sim.sent == []


async def test_write_unclear_is_device_timeout(setup):
    cfg, drv, sim, _ = setup
    sim.no_exec_ack = True
    with pytest.raises(DeviceTimeout) as ei:
        await drv.write_param(dev(cfg), param(cfg, 44), 75)
    assert isinstance(ei.value, Lc1WriteUnclear)
    assert sim.commands == [49]           # kein Rücklesen, keine Wiederholung


async def test_enumerate_tables_with_values(setup):
    cfg, drv, sim, _ = setup
    progress = []
    infos = await drv.enumerate(dev(cfg), lambda a, b, t: progress.append((a, b)))
    vals = [i for i in infos if i.kind == "value"]
    pars = [i for i in infos if i.kind == "param"]
    assert [v.key for v in vals] == list(range(1, 36))
    assert [p.key for p in pars] == list(range(64))
    assert vals[1].value == pytest.approx(78.4) and vals[1].unit == "°C"
    by = {p.key: p for p in pars}
    assert by[20].value == pytest.approx(sim.params[20] / 10) and by[20].decimals == 1
    assert by[20].min == pytest.approx(2.0) and by[20].max == pytest.approx(12.0)
    for e in range(64):
        assert by[e].lock_reason == safety.param_lock_reason("lc1", None, e)
    assert by[0].lock_reason and by[44].lock_reason is None
    assert "Störung" in vals[6].name and "STB" in vals[6].tip
    assert sim.commands == [50, 48]
    assert progress[-1] == (2, 2)


async def test_enumerate_without_device_raises(setup):
    cfg, drv, sim, _ = setup
    sim.silent = True
    drv._ensure(drv._state("sim://t1")).retries = 0
    with pytest.raises(DeviceTimeout):
        await drv.enumerate(dev(cfg))


async def test_scan_ports(sim):
    cfg = make_cfg()
    other = Lc1Simulator(kessel_name="Zweiter Kessel")
    fac = Factory(**{"sim://t1": sim, "sim://t2": other})
    drv = Lc1Driver(cfg, port_factory=fac)
    found = await drv.scan(None, ports=["sim://t1", "sim://t2", "COM99"])
    assert [(f.port, f.name) for f in found] == [("sim://t1", "Holzkessel Süd"), ("sim://t2", "Zweiter Kessel")]
    assert found[0].type_name == "LambdaControl 1 (RS232)" and found[0].system_nr == 1
    assert "sim://t2" not in drv._ports              # nicht konfigurierte Ports werden geschlossen
    assert sim.sent == [53] and other.sent == [53]


async def test_scan_default_only_configured_ports(sim):
    cfg = make_cfg()
    fac = Factory(**{"sim://t1": sim})
    found = await Lc1Driver(cfg, port_factory=fac).scan()
    assert len(found) == 1 and fac.opened == ["sim://t1"]


async def test_interface_error_and_reopen(setup):
    cfg, drv, sim, fac = setup
    fac.fail.add("sim://t1")
    with pytest.raises(InterfaceError):
        await drv.read_values(dev(cfg), dev(cfg).values)
    fac.fail.clear()
    assert (await drv.read_values(dev(cfg), dev(cfg).values))[2] == pytest.approx(78.4)


async def test_unplugged_adapter_closes_and_reopens(setup):
    cfg, drv, sim, fac = setup
    await drv.read_values(dev(cfg), dev(cfg).values)
    orig = sim.write

    def broken(data):
        raise OSError("Gerät entfernt")
    sim.write = broken
    with pytest.raises(InterfaceError):
        await drv.read_values(dev(cfg), dev(cfg).values)
    assert drv._ports["sim://t1"].proto is None
    sim.write = orig
    assert (await drv.read_values(dev(cfg), dev(cfg).values))[28] == 1
    assert fac.opened.count("sim://t1") == 2


async def test_protocol_error_propagates(setup):
    cfg, drv, sim, _ = setup
    sim.corrupt_checksum = True
    with pytest.raises(ProtocolError):
        await drv.read_values(dev(cfg), dev(cfg).values)


async def test_reload_closes_removed_ports(setup):
    cfg, drv, sim, _ = setup
    await drv.read_values(dev(cfg), dev(cfg).values)
    new = GatewayConfig(lc1=Lc1Defaults(byte_timeout_s=0.4, line_timeout_s=0.8), devices=[])
    drv.reload(new)
    assert drv._ports == {}
    s = drv.stats()
    assert s["tx"] >= 1 and s["rx"] > 0 and "errors" in s and "timeouts" in s


async def test_default_factory_sim_url():
    simmod.clear_simulators()
    cfg = make_cfg(port="sim://default-1")
    drv = Lc1Driver(cfg)
    res = await drv.read_values(dev(cfg), dev(cfg).values)
    assert res[2] == pytest.approx(78.4)
    await drv.close()
    simmod.clear_simulators()


async def test_default_factory_missing_port():
    cfg = make_cfg(port="COM_GIBT_ES_NICHT_99")
    with pytest.raises(InterfaceError):
        await Lc1Driver(cfg).read_values(dev(cfg), dev(cfg).values)


def test_list_serial_ports_shape():
    ports = list_serial_ports()
    assert isinstance(ports, list)
    for p in ports:
        assert {"device", "description", "by_id", "ftdi"} <= set(p)


async def test_selftest_ok(sim):
    cfg = make_cfg()
    res = await lc1_selftest(cfg, port_factory=Factory(**{"sim://t1": sim}))
    assert [ok for _, ok, _ in res] == [True, True, True, True]
    assert "Holzkessel" in res[1][2] and "K36=" in res[3][2]
    assert set(sim.commands) == {53, 50}


async def test_selftest_without_checksum_and_failure():
    cfg = make_cfg()
    res = await lc1_selftest(cfg, port_factory=Factory(**{"sim://t1": Lc1Simulator(checksum=False)}))
    assert res[-1][1] is True and "keine Prüfsumme" in res[-1][2]
    res = await lc1_selftest(cfg, port_factory=Factory())
    assert res[0][1] is False and len(res) == 1


async def test_selftest_no_devices():
    res = await lc1_selftest(GatewayConfig())
    assert res and res[0][1] is True


def test_tables_complete():
    assert sorted(lc1_tables.VALUES) == list(range(1, 36))
    assert sorted(lc1_tables.PARAMS) == list(range(64))
    for p in lc1_tables.PARAMS.values():
        assert 0 <= p.min_raw <= p.max_raw <= 0xFFFF, p
        assert p.text and p.level and p.source.startswith("LCS/LCS9/LCS1.par:")
    for v in lc1_tables.VALUES.values():
        assert v.name and v.source.startswith("LCS/LCS9/LCS9.ARB:")
    assert lc1_tables.PARAMS[20].decimals == 1 and lc1_tables.PARAMS[22].decimals == 1
    assert lc1_tables.PARAMS[44].text == "Kesseltemperatur Sollwert"
    assert lc1_tables.PARAMS[20].scale(60) == pytest.approx(6.0)
    assert lc1_tables.PARAMS[20].to_raw(6.0) == 60
    assert lc1_tables.VALUES[7].texts[4].startswith("STB")
    assert lc1_tables.VALUES[28].texts[3] == "Bereitschaft"
    assert set(lc1_tables.STOERUNG_TEXTE) == set(range(12))
