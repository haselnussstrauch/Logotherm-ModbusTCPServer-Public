"""Adapter im Betrieb abgezogen/neu angesteckt: Client meldet sich geschlossen, Treiber öffnet neu."""

import asyncio

import can
import pytest

from gateway.can_lc2 import client as client_mod
from gateway.can_lc2.client import CanClient
from gateway.config import CanConfig
from gateway.drivers import InterfaceError


class DyingBus(can.BusABC):
    """recv() liefert erst nichts, nach ``dead=True`` nur noch OSError (wie ENODEV)."""

    def __init__(self):
        self.dead = False
        self.closed = False
        super().__init__(channel="x")

    def _recv_internal(self, timeout):
        if self.dead:
            raise OSError(19, "No such device")
        return None, False

    def send(self, msg, timeout=None):
        if self.dead:
            raise can.CanOperationError("No such device")

    def shutdown(self):
        self.closed = True
        super().shutdown()


def test_client_meldet_sich_nach_dauerstoerung_geschlossen(monkeypatch):
    monkeypatch.setattr(client_mod, "REOPEN_AFTER_FAILS", 3)
    buses = []

    def factory(cfg, listen_only):
        b = DyingBus()
        buses.append(b)
        return b

    async def run():
        c = CanClient(CanConfig(enabled=True, interface="virtual", channel="t"), bus_factory=factory)
        await c.open()
        assert c.is_open
        buses[0].dead = True
        for _ in range(50):
            if not c.is_open:
                break
            await asyncio.sleep(0.1)
        assert not c.is_open
        with pytest.raises(InterfaceError):
            await c.request(0x01, 0x0B, 0x20, b"\x00\x00", mode="single")
        await c.close()
        assert buses[0].closed
        # neu öffnen klappt (neuer Bus)
        await c.open()
        assert c.is_open and len(buses) == 2
        await c.close()

    asyncio.run(run())
