"""Client- und Treibertests gegen den Firmware-Simulator auf dem python-can-virtual-Bus."""

from __future__ import annotations

import asyncio
import itertools
import time
import re
from pathlib import Path

import can
import pytest

from gateway import safety
from gateway.can_lc2 import capture as capture_mod
from gateway.can_lc2 import client as client_mod
from gateway.can_lc2 import driver as driver_mod
from gateway.can_lc2 import idcodec, messages
from gateway.can_lc2.client import CanClient
from gateway.can_lc2.driver import CanDriver
from gateway.can_lc2.selftest import can_selftest
from gateway.can_lc2.simulator import CanSimulator, SimEntry, SimProfile, default_profiles, profile_lc2
from gateway.config import DeviceConfig, GatewayConfig, ParamDef, ValueDef, VarTyp as VT
from gateway.devices.fw_numbers import NUMBERS as N
from gateway.drivers import DeviceTimeout, InterfaceError, ProtocolError
from gateway.errors import SafetyViolation, WriteRejected

OWN = 0xA0
_chan = itertools.count()
TIMEOUT_MS = 60
GAP_MS = 20


def make_cfg(channel: str, devices: list[dict] | None = None, **can_kw) -> GatewayConfig:
    can_cfg = {"enabled": True, "interface": "virtual", "channel": channel,
               "timing": {"request_timeout_ms": TIMEOUT_MS, "min_gap_ms": GAP_MS, "retries": 1}}
    can_cfg.update(can_kw)
    return GatewayConfig.model_validate({"can": can_cfg, "devices": devices or []})


@pytest.fixture
def channel() -> str:
    return f"test-can-{next(_chan)}"


@pytest.fixture
def sim(channel):
    s = CanSimulator(channel).start()
    yield s
    s.stop()


@pytest.fixture
async def drv(channel, sim):
    d = CanDriver(make_cfg(channel), address_check_s=0.1)
    await d.open()
    yield d
    await d.close()


LC2 = DeviceConfig(id="lc2", bus="can", address=0x01, regler_typ=0x0B00, unit_id=1)
MC2 = DeviceConfig(id="mc2", bus="can", address=0x10, regler_typ=0x0A01, unit_id=16)
SONDE = DeviceConfig(id="sonde", bus="can", address=0xE0, regler_typ=0xFFF2, unit_id=200)


def p_tks(**kw) -> ParamDef:
    return ParamDef(nr=N["P_TKS"], vartyp=VT.SHORT, decimals=1, writable=True, **kw)


def requests(sim: CanSimulator) -> list[tuple[float, int, bytes]]:
    with sim.lock:
        return [r for r in sim.rx_log if idcodec.decode(r[1]).sender == OWN]


# ===== Sendestelle =============================================================================
def test_nur_eine_sendestelle():
    """Genau ein ``.send(`` auf einem Bus im Gateway-Code: CanClient._send_frame."""
    root = Path(__file__).resolve().parents[1] / "gateway"
    hits = []
    for f in root.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(r"\b(bus|_bus|self\._bus)\.send\(|bus\.send,|\.send_periodic\(", line):
                hits.append((f.relative_to(root).as_posix(), i, line.strip()))
    can_hits = [h for h in hits if h[0].startswith("can_lc2/")]
    # Simulator ist Geräteseite (Testinfrastruktur), markiert mit "# sim-send"
    gateway_hits = [h for h in can_hits if h[0] != "can_lc2/simulator.py"]
    assert len(gateway_hits) == 1, gateway_hits
    assert gateway_hits[0][0] == "can_lc2/client.py"
    sim_hits = [h for h in can_hits if h[0] == "can_lc2/simulator.py"]
    assert all("# sim-send" in h[2] for h in sim_hits) and len(sim_hits) == 1
    src = (root / "can_lc2" / "client.py").read_text(encoding="utf-8")
    send_def = src[src.index("async def _send_frame"):src.index("# ===== Anfragen")]
    assert "safety.check_can_tx(" in send_def and "bus.send" in send_def
    assert len(re.findall(r"bus\.send[,(]", src)) == 1
    for mod in ("capture.py", "selftest.py", "driver.py", "bus.py"):
        assert ".send(" not in (root / "can_lc2" / mod).read_text(encoding="utf-8"), mod


# ===== Client: Drosselung / Zuordnung ==========================================================
async def test_drosselung_und_nur_eine_offene_anfrage(channel, sim):
    cfg = make_cfg(channel)
    events: list[tuple[float, str]] = []
    cl = CanClient(cfg.can, trace=lambda d, t: events.append((time.perf_counter(), d)))
    await cl.open()
    try:
        async def one(nr):
            return await cl.request(0x01, idcodec.KANAL_ANFRAGE, 0x20, messages.build_anfrage(nr), mode="single",
                                    nr=nr)
        nrs = [N["M_TKI"], N["M_TKS"], N["M_TKRI"], N["M_RV"], N["M_TAGI"]] * 2
        res = await asyncio.gather(*(one(n) for n in nrs))
        assert [int.from_bytes(r[:2], "little") for r in res] == nrs
    finally:
        await cl.close()
    assert len(requests(sim)) == len(nrs)
    # Reihenfolge TX, RX, TX, RX … -> nie zwei offene Anfragen; Abstand Antwort -> nächstes TX >= min_gap
    seq = [(t, d) for t, d in events if d in ("TX", "RX")]
    assert [d for _, d in seq] == ["TX", "RX"] * len(nrs)
    gaps = [seq[i + 1][0] - seq[i][0] for i in range(1, len(seq) - 1, 2)]
    assert min(gaps) >= GAP_MS / 1000 * 0.95, gaps


async def test_fremde_und_broadcast_ignoriert_echo_verworfen(channel):
    sim = CanSimulator(channel, echo=True, broadcast_interval_s=0.02).start()
    other = can.Bus(interface="virtual", channel=channel)
    cl = CanClient(make_cfg(channel).can)
    await cl.open()
    try:
        # fremde Antwort an anderes Ziel mit gleicher Nummer – darf nicht zugeordnet werden
        fake = messages.MesswertAntwort(N["M_TKI"], VT.FLOAT, b"\0\0\0\0\0").build()
        other.send(can.Message(arbitration_id=idcodec.encode(0x0A, 0x01, 0x33, 0x20), data=fake))
        other.send(can.Message(arbitration_id=idcodec.encode(0x0A, 0x01, 0xFF, 0x20), data=fake))
        res = await cl.request(0x01, idcodec.KANAL_ANFRAGE, 0x20, messages.build_anfrage(N["M_TKI"]),
                               mode="single", nr=N["M_TKI"])
        assert messages.MesswertAntwort.parse(res).wert[:4] != b"\0\0\0\0"
        await asyncio.sleep(0.1)
        st = cl.stats()
        assert st["echo"] >= 1 and st["own_address_conflicts"] == 0 and st["ignored"] >= 2
        assert 0x01 in st["heard"]
    finally:
        await cl.close()
        other.shutdown()
        sim.stop()


async def test_retry_bei_timeout(channel, sim):
    cl = CanClient(make_cfg(channel).can)
    await cl.open()
    try:
        sim.drop_next = 1
        res = await cl.request(0x01, idcodec.KANAL_ANFRAGE, 0x20, messages.build_anfrage(N["M_TKI"]),
                               mode="single", nr=N["M_TKI"])
        assert res and cl.stats()["retries"] == 1 and cl.stats()["timeouts"] == 1
        sim.drop_next = 2
        with pytest.raises(DeviceTimeout):
            await cl.request(0x01, idcodec.KANAL_ANFRAGE, 0x20, messages.build_anfrage(N["M_TKI"]),
                             mode="single", nr=N["M_TKI"])
    finally:
        await cl.close()


async def test_unbekannte_nummer_timeout(channel, sim):
    cl = CanClient(make_cfg(channel).can)
    await cl.open()
    try:
        with pytest.raises(DeviceTimeout):
            await cl.request(0x01, idcodec.KANAL_ANFRAGE, 0x20, messages.build_anfrage(0x0BAD), mode="single",
                             nr=0x0BAD, retries=0)
        assert len(requests(sim)) == 1
    finally:
        await cl.close()


async def test_crc_fehler_retry(channel, sim):
    cl = CanClient(make_cfg(channel).can)
    await cl.open()
    try:
        sim.corrupt_next = 1
        blk = await cl.request(0x01, idcodec.KANAL_ANFRAGE, 0x38, messages.build_anfrage(N["P_TKS"]),
                               mode="block", nr=N["P_TKS"])
        assert messages.DetailAntwort.parse(blk).nr == N["P_TKS"]
        assert cl.stats()["crc_errors"] == 1 and cl.stats()["retries"] == 1
    finally:
        await cl.close()


async def test_safety_im_client(channel, sim):
    cl = CanClient(make_cfg(channel).can)
    await cl.open()
    try:
        with pytest.raises(SafetyViolation):     # 0xFFFF bei 0x20 nie
            await cl.request(0x01, idcodec.KANAL_ANFRAGE, 0x20, messages.build_anfrage(0xFFFF), mode="single")
        with pytest.raises(SafetyViolation):     # Broadcast
            await cl.request(0xFF, idcodec.KANAL_ANFRAGE, 0x00, b"", mode="block")
        with pytest.raises(SafetyViolation):     # Fehlerspeicher
            await cl.request(0x01, idcodec.KANAL_ANFRAGE, 0x28, messages.build_anfrage(1), mode="block")
        with pytest.raises(SafetyViolation):     # Aufzählung ohne Freigabe
            await cl.request(0x01, idcodec.KANAL_ANFRAGE, 0x38, messages.build_anfrage(0xFFFF), mode="enum")
        await asyncio.sleep(0.05)
        assert requests(sim) == [] and cl.stats()["tx"] == 0 and cl.stats()["safety_blocked"] == 4
    finally:
        await cl.close()


async def test_listen_only_sendet_nicht(channel, sim):
    cl = CanClient(make_cfg(channel).can)
    await cl.open(listen_only=True)
    try:
        with pytest.raises(InterfaceError):
            await cl.request(0x01, idcodec.KANAL_ANFRAGE, 0x00, b"", mode="block")
        assert requests(sim) == []
    finally:
        await cl.close()


# ===== Treiber: Gateway-Adresse =================================================================
async def test_open_adresse_belegt(channel):
    with CanSimulator(channel, squatter_address=OWN, squatter_interval_s=0.05):
        d = CanDriver(make_cfg(channel), address_check_s=0.3)
        with pytest.raises(InterfaceError, match="belegt"):
            await d.open()
        assert not d.is_open


async def test_adresskonflikt_im_betrieb_sperrt_senden(channel, sim):
    d = CanDriver(make_cfg(channel), address_check_s=0.05)
    await d.open()
    try:
        other = can.Bus(interface="virtual", channel=channel)
        other.send(can.Message(arbitration_id=idcodec.encode(0x0A, OWN, 0xFF, 0x20), data=bytes(8)))
        other.shutdown()
        await asyncio.sleep(0.1)
        with pytest.raises(InterfaceError, match="Gateway-Adresse"):
            await d.read_values(LC2, [ValueDef(nr=N["M_TKI"], vartyp=VT.FLOAT, decimals=1)])
    finally:
        await d.close()


# ===== Scan ====================================================================================
async def test_scan_schnell(drv, sim, monkeypatch):
    monkeypatch.setattr(driver_mod, "FAST_SCAN_RANGES", (range(0x01, 0x04), range(0x10, 0x11), range(0x50, 0x51),
                                                        range(0xE0, 0xE1)))
    seen = []
    found = await drv.scan(lambda d, t, s: seen.append((d, t, s)))
    by = {f.address: f for f in found}
    assert set(by) == {0x01, 0x02, 0x10, 0x50, 0xE0}
    assert by[0x01].regler_typ == 0x0B00 and "LambdaControl 2 Neu" in by[0x01].type_name
    assert by[0x01].version == "2.29" and by[0x01].serial == 0x12345 and by[0x01].name == "LambdaControl 2"
    assert by[0x10].regler_typ == 0x0A01 and by[0x02].regler_typ == 0x0D00
    assert by[0x50].regler_typ == 0x3A00 and by[0xE0].regler_typ == 0xFFF2
    assert seen[-1][0] == seen[-1][1]
    ids = {idcodec.decode(r[1]).empfaenger for r in requests(sim)}
    assert OWN not in ids and 0xFF not in ids


def test_scan_adressen(channel):
    d = CanDriver(make_cfg(channel))
    fast = d.scan_addresses(False)
    assert OWN not in fast and 0 not in fast and 0xFF not in fast
    assert {1, 15, 0x10, 0x1F, 0x20, 0x50, 0x5F, 0xE0, 0xFC} <= set(fast) and 0x30 not in fast
    full = d.scan_addresses(True)
    assert len(full) == 253 and OWN not in full


def test_type_name():
    assert "MultiControl 2" in driver_mod.type_name(0x0A01, 0x10)
    assert "nicht eindeutig" in driver_mod.type_name(0x0A01, 0x55)
    assert "unbekannt" in driver_mod.type_name(0x1234)


# ===== Aufzählung ==============================================================================
async def test_enumerate_lc2(drv, sim):
    infos = await drv.enumerate(LC2)
    by = {(i.kind, i.key): i for i in infos}
    v = by[("value", N["M_TKI"])]
    assert (v.name, v.unit, v.decimals, v.vartyp, v.value, v.min, v.max) == ("Kesseltemp.", "°C", 1, VT.FLOAT,
                                                                           72.4, -50.0, 150.0)
    assert by[("value", N["M_UD"])].vartyp == VT.FLOAT          # FUEHLER -> FLOAT (GetTyp)
    kt = by[("value", N["M_KESSELT"])]
    assert kt.out_type == safety.OUT_SECTOH and kt.value == 12345.5 and kt.unit == "h"
    assert by[("value", N["M_KSTATUS"])].out_textauswahl
    p = by[("param", N["P_TKS"])]
    assert (p.name, p.vartyp, p.decimals, p.min, p.max, p.default, p.value) == ("5.12: Kessel Soll", VT.SHORT, 1,
                                                                              60.0, 120.0, 80.0, 80.0)
    assert p.lock_reason is None and p.supported
    assert by[("param", N["P_KF_TA"])].value == -5.0
    assert "Löschfunktion" in by[("param", N["P_KDELP"])].lock_reason
    kzz = by[("param", N["P_KZZ1"])]
    assert not kzz.supported and kzz.lock_reason
    # ParameterSendenOkay = false -> nicht in der Aufzählung
    for name in ("P_BUSADR", "P_NACHLEG_KG", "P_CODE"):
        assert ("param", N[name]) not in by
    reqs = requests(sim)
    assert len(reqs) == 2 and all(r[2][:2] == b"\xff\xff" for r in reqs)


async def test_enumerate_out_type_sperre(channel):
    prof = SimProfile(key="x", name="X", regler_typ=0x0B00, address=0x05, serial=1, params=[
        SimEntry(N["P_TKS"], "Ok", VT.SHORT, 600, 1200, 800, 1),
        SimEntry(N["P_TAGMIN"], "Std", VT.LONG, 0, 9999, 0, 1, out=safety.OUT_SECTOH),
        SimEntry(N["P_RVLFZ"], "Zyk", VT.CHAR, 0, 3, 0, out=safety.OUT_ZYKLENZEITEN),
        SimEntry(N["P_LVLFZ"], "IP", VT.LONG, 0, 0, 0, out=safety.OUT_IPV4),
        SimEntry(N["P_KF_TA"], "RAM", VT.CHAR, 0, 3, 0, out=safety.OUT_RAMSTRING),
    ])
    dev = DeviceConfig(id="x", bus="can", address=0x05, regler_typ=0x0B00, unit_id=5)
    with CanSimulator(channel, [prof]):
        d = CanDriver(make_cfg(channel), address_check_s=0)
        await d.open()
        try:
            infos = {i.key: i for i in await d.enumerate(dev) if i.kind == "param"}
        finally:
            await d.close()
    assert infos[N["P_TKS"]].lock_reason is None
    assert "OUT_SECTOH" in infos[N["P_TAGMIN"]].lock_reason
    assert "OUT_ZYKLENZEITEN" in infos[N["P_RVLFZ"]].lock_reason
    assert N["P_LVLFZ"] not in infos and N["P_KF_TA"] not in infos   # nicht über den Bus gesendet


async def test_enumerate_powerup(channel):
    with CanSimulator(channel, powerup=True):
        d = CanDriver(make_cfg(channel), address_check_s=0)
        await d.open()
        try:
            infos = await d.enumerate(LC2)
            assert infos and all(i.kind == "param" for i in infos)
            with pytest.raises(DeviceTimeout):
                await d.read_values(LC2, [ValueDef(nr=N["M_TKI"], vartyp=VT.FLOAT, decimals=1)])
        finally:
            await d.close()


# ===== Lesen ===================================================================================
async def test_read_values(drv, sim):
    vals = [ValueDef(nr=N["M_TKI"], vartyp=VT.FLOAT, decimals=1),
            ValueDef(nr=N["M_TAGH"], vartyp=VT.SSHORT, decimals=1),
            ValueDef(nr=N["M_KESSELT"], vartyp=VT.LONG, decimals=1),
            ValueDef(nr=0x0BAD, vartyp=VT.SHORT, decimals=0)]
    res = await drv.read_values(LC2, vals)
    assert res == {N["M_TKI"]: 72.4, N["M_TAGH"]: 245.1, N["M_KESSELT"]: 12345.5, 0x0BAD: None}
    # Ausgabetyp per 0x30 nachgefragt (einmalig), danach nur noch 0x20
    n = len(requests(sim))
    await drv.read_values(LC2, vals[:3])
    assert len(requests(sim)) - n == 3


async def test_read_values_sonde_fuehler(drv):
    res = await drv.read_values(SONDE, [ValueDef(nr=N["M_O2I"], vartyp=VT.FUEHLER, decimals=3)])
    assert res[N["M_O2I"]] == 7.125


async def test_read_values_offline(channel, sim):
    d = CanDriver(make_cfg(channel), address_check_s=0)
    await d.open()
    try:
        dev = DeviceConfig(id="weg", bus="can", address=0x07, unit_id=7)
        vals = [ValueDef(nr=N["M_TKI"] + i, vartyp=VT.FLOAT) for i in range(6)]
        with pytest.raises(DeviceTimeout):
            await d.read_values(dev, vals)
        assert len(requests(sim)) <= 2 * 2 * 3     # Abbruch nach 3 Fehlversuchen (je 0x30 + 0x20)
    finally:
        await d.close()


async def test_read_params(drv):
    res = await drv.read_params(LC2, [p_tks(), ParamDef(nr=N["P_TAAUSLEG"], vartyp=VT.SCHAR),
                                      ParamDef(nr=0x0B4F, vartyp=VT.CHAR)])
    assert res == {N["P_TKS"]: 80.0, N["P_TAAUSLEG"]: -12.0, 0x0B4F: None}


# ===== Schreiben ===============================================================================
def _writes(sim):
    with sim.lock:
        return list(sim.writes)


async def test_write_bestaetigung(drv, sim):
    assert await drv.write_param(LC2, p_tks(), 72.5) == 72.5
    w = _writes(sim)
    assert w == [(0x01, N["P_TKS"], VT.SHORT, (725).to_bytes(2, "little") + bytes(3))]
    assert (await drv.read_params(LC2, [p_tks()]))[N["P_TKS"]] == 72.5
    assert sim.profile(1).entry(N["P_TKS"]).value == 725


async def test_write_signed(drv, sim):
    p = ParamDef(nr=N["P_KF_TA"], vartyp=VT.SCHAR, writable=True)
    assert await drv.write_param(LC2, p, -7) == -7.0
    assert _writes(sim)[0][3][0] == 0xF9


async def test_write_abweichung_firmware_korrigiert(drv, sim):
    sim.override_after_write[N["P_TKS"]] = 800
    actual = await drv.write_param(LC2, p_tks(), 90.0)
    assert actual == 80.0 and not safety.verify_readback(90.0, actual, 1)


async def test_write_keine_bestaetigung_kein_retry(drv, sim):
    sim.suppress_write_confirm = True
    with pytest.raises(DeviceTimeout, match="nicht wiederholt"):
        await drv.write_param(LC2, p_tks(), 70.0)
    assert len(_writes(sim)) == 1
    sets = [r for r in requests(sim) if idcodec.decode(r[1]).kanal == idcodec.KANAL_BEFEHLE]
    assert len(sets) == 1


async def test_write_ausserhalb_geraetebereich(drv, sim):
    with pytest.raises(WriteRejected):
        await drv.write_param(LC2, p_tks(), 130.0)
    with pytest.raises(WriteRejected):
        await drv.write_param(LC2, p_tks(), 6553.6)
    assert _writes(sim) == []


async def test_write_vartyp_abweichung_nicht_gesendet(drv, sim):
    with pytest.raises(ProtocolError, match="VarTyp"):
        await drv.write_param(LC2, ParamDef(nr=N["P_TKS"], vartyp=VT.CHAR, decimals=1, writable=True), 70.0)
    assert _writes(sim) == []


async def test_write_gesperrt_nichts_auf_dem_bus(drv, sim):
    n0 = len(requests(sim))
    for nr in (N["P_KDELP"], N["P_BUSADR"], N["P_KZZ1"], 0x906C):
        with pytest.raises(SafetyViolation):
            await drv.write_param(LC2, ParamDef(nr=nr, vartyp=VT.CHAR, writable=True), 1)
    with pytest.raises(SafetyViolation):     # Sonde: Kalibrierung
        await drv.write_param(SONDE, ParamDef(nr=N["P_CALO2U"], vartyp=VT.SHORT, decimals=1, writable=True), 8.0)
    with pytest.raises(SafetyViolation):     # Messwert
        await drv.write_param(LC2, ParamDef(nr=N["M_TKI"], vartyp=VT.CHAR, writable=True), 1)
    assert len(requests(sim)) == n0 and _writes(sim) == []


async def test_write_out_type_gesperrt_nicht_gesendet(channel):
    prof = SimProfile(key="x", name="X", regler_typ=0x0B00, address=0x05, serial=1, params=[
        SimEntry(N["P_TAGMIN"], "Std", VT.LONG, 0, 999999, 0, 1, out=safety.OUT_SECTOH)])
    dev = DeviceConfig(id="x", bus="can", address=0x05, regler_typ=0x0B00, unit_id=5)
    with CanSimulator(channel, [prof]) as s:
        d = CanDriver(make_cfg(channel), address_check_s=0)
        await d.open()
        try:
            with pytest.raises(SafetyViolation, match="OUT_SECTOH"):
                await d.write_param(dev, ParamDef(nr=N["P_TAGMIN"], vartyp=VT.LONG, decimals=1, writable=True), 1)
        finally:
            await d.close()
        assert s.writes == []


async def test_write_gleicher_wert_nicht_gesendet(drv, sim):
    assert await drv.write_param(LC2, p_tks(), 80.0) == 80.0
    assert _writes(sim) == []


def test_simulator_check_parameter_werk():
    from gateway.can_lc2.simulator import check_parameter
    e = SimEntry(N["P_TKS"], "x", VT.SHORT, 600, 1200, 800, 1, value=1300)
    assert check_parameter(e) and e.value == 800
    e = SimEntry(N["P_KF_TA"], "x", VT.SCHAR, -20, 5, 0, value=-30)
    assert check_parameter(e) and e.value == 0


# ===== Reload / Stats ==========================================================================
async def test_reload_und_stats(drv, channel):
    st = drv.stats()
    assert {"errors", "tx", "rx", "timeouts"} <= set(st) and st["open"]
    new = make_cfg(channel, gateway_address=0xA1)
    drv.reload(new)
    await drv.read_params(LC2, [p_tks()])
    assert drv.client.own_address == 0xA1


# ===== Selbsttest / Mitschnitt =================================================================
async def test_selftest(channel, sim):
    cfg = make_cfg(channel, devices=[{"id": "lc2", "bus": "can", "address": 1, "regler_typ": 0x0B00,
                                      "unit_id": 1}, {"id": "falsch", "bus": "can", "address": 0x10,
                                                      "regler_typ": 0x0B00, "unit_id": 16}])
    sim.broadcast_interval_s = 0.05
    res = await can_selftest(cfg, listen_s=0.3)
    by = {n: (ok, t) for n, ok, t in res}
    assert by["Adapter öffnen"][0] and by["Busverkehr"][0] and by["Gateway-Adresse frei"][0]
    assert by["Identify lc2 (0x01)"][0]
    assert not by["Identify falsch (0x10)"][0] and "≠" in by["Identify falsch (0x10)"][1]


async def test_selftest_kein_adapter():
    cfg = make_cfg("kein-index", interface="gs_usb")
    res = await can_selftest(cfg)
    assert res[0][0] == "Adapter öffnen" and not res[0][1]


async def test_capture(channel, tmp_path):
    with CanSimulator(channel, broadcast_interval_s=0.02):
        cfg = make_cfg(channel)
        out = tmp_path / "cap.log"
        other = CanClient(cfg.can.model_copy(update={"gateway_address": 0xA5}))
        await other.open()
        cap_task = asyncio.create_task(capture_mod.capture(cfg, out, duration_s=0.5))
        await asyncio.sleep(0.1)
        await other.request(0x01, idcodec.KANAL_ANFRAGE, 0x38, messages.build_anfrage(N["P_TKS"]), mode="block",
                            nr=N["P_TKS"])
        stats = await cap_task
        await other.close()
    lines = out.read_text(encoding="ascii").splitlines()
    assert lines and all(re.fullmatch(r"\(\d+\.\d{6}\) \S+ [0-9A-F]{8}#[0-9A-F]*", ln) for ln in lines)
    dec = Path(stats["decoded_path"]).read_text(encoding="utf-8")
    assert "Detail nr=0x0042" in dec and "Kessel Soll" in dec and "sender=0x01" in dec
    assert 0x01 in stats["sources"]
    # mitgeschnittene Datei ist mit python-can lesbar
    msgs = list(can.LogReader(str(out)))
    assert len(msgs) == len(lines)


def test_capture_sendet_nie():
    src = Path(capture_mod.__file__).read_text(encoding="utf-8")
    assert ".send(" not in src and "CanClient" not in src


def test_default_profile_vollstaendig():
    keys = {(p.regler_typ, p.address) for p in default_profiles()}
    assert keys == {(0x0B00, 0x01), (0x0A01, 0x10), (0x0D00, 0x02), (0x3A00, 0x50), (0xFFF2, 0xE0)}
    lc2 = profile_lc2()
    assert all(e.nr & 0xFF < 0x40 for e in lc2.values)
    assert all(0x40 <= e.nr & 0xFF < 0x80 for e in lc2.params)


def test_gs_usb_timing():
    from gateway.can_lc2.bus import gs_usb_timing
    for fclk in (48_000_000, 80_000_000, 40_000_000):
        for br in (50_000, 20_000):
            t = gs_usb_timing(fclk, br)
            nbt = 1 + t["prop_seg"] + t["phase_seg1"] + t["phase_seg2"]
            assert fclk / (t["brp"] * nbt) == br
            assert 80 <= t["sample_point"] <= 87.5
            assert t["phase_seg1"] + t["prop_seg"] <= 16 and t["phase_seg2"] <= 8
    t = gs_usb_timing(48_000_000, 50_000)
    assert t["sample_point"] == 85.0


def test_python_can_gs_usb_kann_50k_nicht():
    """Befund: python-can 4.6.1 rechnet 50k/20k bei 48 MHz nicht – daher eigene Bitzeit."""
    with pytest.raises(ValueError):
        can.BitTiming.from_sample_point(f_clock=48_000_000, bitrate=50_000, sample_point=87.5)


def test_frame_text():
    m = can.Message(arbitration_id=idcodec.encode(0x0B, OWN, 1, 0x20), data=messages.build_anfrage(0x42))
    assert "nr=0x0042" in client_mod.frame_text(m)
