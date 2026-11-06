"""gs_usb: USB-Handle wird beim Schließen und bei Fehlern sofort freigegeben.

Nachgebildet ist das Verhalten unter Windows/WinUSB: Ein Gerät darf nur ein offenes Handle haben.
pyusb öffnet beim ersten Control-Transfer und schließt erst bei ``usb.util.dispose_resources``
(oder irgendwann per Garbage Collector). Ohne Freigabe scheiterte das nächste Öffnen im selben
Prozess (Gerätesuche, CAN-Test), bis der Adapter neu eingesteckt wurde.
"""

from types import SimpleNamespace

import pytest

import gateway.can_lc2.bus as busmod
from gateway.config import CanConfig
from gateway.drivers import InterfaceError


class FakeUsbDevice:
    """Physischer Adapter: höchstens ein offenes Handle (wie WinUSB)."""

    def __init__(self):
        self.open_handles = 0
        self.dispose_calls = 0


class FakePyUsbDevice:
    """Ein ``usb.core.Device``-Objekt je Scan; hält höchstens ein eigenes Handle."""

    def __init__(self, phys: FakeUsbDevice):
        self.phys = phys
        self.opened = False


class FakeGsUsb:
    """Ersatz für ``gs_usb.gs_usb.GsUsb`` – ein Objekt je Scan, wie in der Bibliothek."""

    def __init__(self, usb_dev: FakeUsbDevice, *, listen_only_supported=True):
        self.gs_usb = FakePyUsbDevice(usb_dev)
        self.device_flags = 0
        self._lo = listen_only_supported
        self.started = False

    def _ensure_open(self):
        dev = self.gs_usb
        if not dev.opened:
            if dev.phys.open_handles:
                raise RuntimeError("[Errno 13] Access denied (insufficient permissions)")
            dev.phys.open_handles += 1
            dev.opened = True

    @property
    def device_capability(self):
        self._ensure_open()
        from gs_usb.constants import GS_CAN_MODE_LISTEN_ONLY
        return SimpleNamespace(fclk_can=48_000_000, tseg1_min=1, tseg1_max=16, tseg2_min=1,
                               tseg2_max=8, sjw_max=4, brp_min=1, brp_max=1024, brp_inc=1,
                               feature=GS_CAN_MODE_LISTEN_ONLY if self._lo else 0)

    def set_timing(self, *a):
        self._ensure_open()

    def start(self, flags):
        self._ensure_open()
        self.device_flags = flags
        self.started = True

    def stop(self):
        self.started = False


@pytest.fixture
def adapter(monkeypatch):
    usb_dev = FakeUsbDevice()
    state = {"lo": True}
    monkeypatch.setattr(busmod, "ensure_libusb_backend", lambda: None)
    import gs_usb.gs_usb as gsmod
    monkeypatch.setattr(gsmod.GsUsb, "scan",
                        classmethod(lambda cls: [FakeGsUsb(usb_dev, listen_only_supported=state["lo"])]))

    def dispose(dev):
        # Wie pyusb: nur das Handle DIESES Device-Objekts schließen
        dev.phys.dispose_calls += 1
        if dev.opened:
            dev.opened = False
            dev.phys.open_handles -= 1

    import usb.util
    monkeypatch.setattr(usb.util, "dispose_resources", dispose)
    usb_dev.state = state
    return usb_dev


def cfg():
    return CanConfig(enabled=True, interface="gs_usb", channel="0", bitrate=50_000)


def test_shutdown_gibt_handle_frei(adapter):
    bus = busmod.open_can_bus(cfg())
    assert adapter.open_handles == 1
    bus.shutdown()
    assert adapter.open_handles == 0
    assert adapter.dispose_calls == 1


def test_mehrfach_oeffnen_im_selben_prozess(adapter):
    """Suche/Test/Dienst öffnen nacheinander – ohne Neu-Einstecken."""
    for listen_only in (True, False, False, True, False):
        bus = busmod.open_can_bus(cfg(), listen_only=listen_only)
        assert bus.listen_only == listen_only
        bus.shutdown()
    assert adapter.open_handles == 0


def test_fehlgeschlagenes_listen_only_blockiert_nicht(adapter):
    """CAN-Test: Adapter ohne Listen-only -> Fehler, danach muss der Normalmodus gehen."""
    adapter.state["lo"] = False
    with pytest.raises(InterfaceError, match="Listen-only"):
        busmod.open_can_bus(cfg(), listen_only=True)
    assert adapter.open_handles == 0
    bus = busmod.open_can_bus(cfg(), listen_only=False)
    assert not bus.listen_only
    bus.shutdown()


def test_altes_handle_wird_beim_oeffnen_aufgeraeumt(adapter, monkeypatch):
    """Hängt noch ein Handle (Objekt ohne shutdown), wird nach gc.collect erneut versucht."""
    adapter.open_handles = 1          # Altlast aus einem früheren Öffnen
    monkeypatch.setattr(busmod.gc, "collect", lambda: setattr(adapter, "open_handles", 0))
    bus = busmod.open_can_bus(cfg())
    assert adapter.open_handles == 1
    bus.shutdown()


def test_belegter_adapter_meldet_klaren_fehler(adapter, monkeypatch):
    """Anderer Prozess hält den Adapter (z. B. Dienst läuft während `gateway scan`)."""
    adapter.open_handles = 1
    monkeypatch.setattr(busmod.gc, "collect", lambda: 0)
    with pytest.raises(InterfaceError, match="nicht ansprechbar"):
        busmod.open_can_bus(cfg())
    assert adapter.open_handles == 1   # fremdes Handle bleibt unangetastet
