"""Selbsttest: echte Bitrate einer SocketCAN-Schnittstelle (ip -details -json)."""

import json
from types import SimpleNamespace

from gateway.can_lc2.bus import check_bitrate, socketcan_link
from gateway.config import CanConfig


def _ip(bitrate=50000, sp="0.875", returncode=0):
    out = json.dumps([{"ifname": "can0", "linkinfo": {"info_kind": "can", "info_data": {
        "state": "ERROR-ACTIVE", "restart_ms": 1000,
        "bittiming": {"bitrate": bitrate, "sample_point": sp, "tq": 125, "sjw": 1}}}}])
    return lambda cmd: SimpleNamespace(returncode=returncode, stdout=out)


def test_link_liest_bitrate():
    link = socketcan_link("can0", _ip())
    assert link == {"bitrate": 50000, "sample_point": 0.875, "sjw": 1, "state": "ERROR-ACTIVE", "restart_ms": 1000}


def test_link_ohne_ip_programm():
    def boom(cmd):
        raise FileNotFoundError("ip")
    assert socketcan_link("can0", boom) is None
    assert socketcan_link("can0", _ip(returncode=1)) is None


def test_check_bitrate_ok_und_falsch():
    cfg = CanConfig(enabled=True, interface="socketcan", channel="can0", bitrate=50000)
    ok, text = check_bitrate(object(), cfg, _ip(50000))
    assert ok and "50000 bit/s" in text and "87.5 %" in text
    ok, text = check_bitrate(object(), cfg, _ip(500000))
    assert not ok and "Soll 50000" in text and "bitrate 50000" in text


def test_check_bitrate_unlesbar_ist_kein_fehler():
    cfg = CanConfig(enabled=True, interface="socketcan", channel="can0", bitrate=50000)
    ok, text = check_bitrate(object(), cfg, _ip(returncode=1))
    assert ok and "nicht lesbar" in text
