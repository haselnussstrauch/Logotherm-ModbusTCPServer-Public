"""Launcher: IP-Adresse des PCs anzeigen/ändern (launcher/netconfig.py)."""

import gc
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "launcher"))
import netconfig as n  # noqa: E402

PS_JSON = json.dumps([
    {"alias": "WLAN", "index": 5, "desc": "Realtek WiFi", "status": "Up", "dhcp": "Enabled",
     "ips": ["192.168.6.109/24"], "gateways": ["192.168.6.1"], "dns": ["192.168.6.1"]},
    {"alias": "Ethernet", "index": 34, "desc": "Realtek GbE", "status": "Disconnected", "dhcp": "Disabled",
     "ips": "10.0.0.5/8", "gateways": None, "dns": []},                 # PS 5.1: Einzelwert statt Liste
    {"alias": "Ethernet 2", "index": 9, "desc": "", "status": "Up", "dhcp": "Enabled",
     "ips": ["169.254.10.3/16"], "gateways": [], "dns": []},
])


def test_parse_und_anzeige():
    ad = n.parse_adapters(PS_JSON)
    assert [a.alias for a in ad] == ["Ethernet 2", "WLAN", "Ethernet"]     # verbundene zuerst
    wlan = ad[1]
    assert wlan.dhcp and wlan.ip == "192.168.6.109" and wlan.mask == "255.255.255.0"
    assert wlan.gateway == "192.168.6.1" and wlan.summary() == "WLAN: 192.168.6.109/24 (DHCP)"
    assert ad[2].summary() == "Ethernet: nicht verbunden (manuell)" and ad[2].ips == ["10.0.0.5/8"]
    assert ad[0].apipa and "kein DHCP-Server" in ad[0].summary()
    assert n.web_urls(ad, 8080) == ["http://192.168.6.109:8080"]
    assert n.parse_adapters(json.dumps({"alias": "X", "index": 1, "status": "Up"}))[0].alias == "X"


def test_parse_kaputt():
    with pytest.raises(n.NetError):
        n.parse_adapters("{kein json")


def test_list_adapters_fehler():
    class R:
        returncode, stdout, stderr = 1, "", "Zugriff verweigert"
    with pytest.raises(n.NetError, match="Zugriff verweigert"):
        n.list_adapters(run=lambda: R())


@pytest.mark.parametrize("ip,mask,gw,dns,ok", [
    ("192.168.6.22", "255.255.255.0", "192.168.6.1", "192.168.6.1, 8.8.8.8", True),
    ("192.168.6.22", "24", "", "", True),
    ("192.168.6.22", "/16", "192.168.1.1", "", True),
    ("192.168.6.300", "24", "", "", False),
    ("192.168.6.22", "255.0.255.0", "", "", False),
    ("192.168.6.22", "31", "", "", False),
    ("192.168.6.0", "24", "", "", False),          # Netzadresse
    ("192.168.6.255", "24", "", "", False),        # Broadcast
    ("192.168.6.22", "24", "192.168.7.1", "", False),  # Gateway außerhalb
    ("192.168.6.22", "24", "192.168.6.22", "", False),
    ("169.254.1.1", "16", "", "", False),
    ("127.0.0.1", "8", "", "", False),
    ("192.168.6.22", "24", "", "1.1.1.1,8.8.8.8,9.9.9.9", False),
    ("192.168.6.22 & calc", "24", "", "", False),
])
def test_validate(ip, mask, gw, dns, ok):
    if ok:
        s = n.validate_static(ip, mask, gw, dns)
        assert s.mask.count(".") == 3
    else:
        with pytest.raises(n.NetError):
            n.validate_static(ip, mask, gw, dns)


def test_befehle_dhcp_und_statisch():
    assert n.command_line(n.build_commands(5, None)) == (
        "netsh interface ipv4 set address name=5 source=dhcp && "
        "netsh interface ipv4 set dnsservers name=5 source=dhcp")
    s = n.validate_static("192.168.6.22", "24", "192.168.6.1", "192.168.6.1,8.8.8.8")
    line = n.command_line(n.build_commands(5, s))
    assert line == ("netsh interface ipv4 set address name=5 source=static address=192.168.6.22 "
                    "mask=255.255.255.0 gateway=192.168.6.1 && "
                    "netsh interface ipv4 set dnsservers name=5 source=static address=192.168.6.1 "
                    "register=primary validate=no && "
                    "netsh interface ipv4 add dnsservers name=5 address=8.8.8.8 index=2 validate=no")
    s = n.validate_static("10.1.2.3", "8")
    line = n.command_line(n.build_commands(7, s))
    assert "gateway=none" in line and "dnsservers name=7 source=static address=none" in line


def test_befehle_nur_geprueft():
    with pytest.raises(n.NetError):
        n.build_commands(0, None)
    with pytest.raises(n.NetError):
        n.command_line([["netsh", "name=x&calc"]])


def test_apply_mit_runner():
    seen = []
    n.apply(5, None, runner=lambda cl: seen.append(cl) or 0)
    assert seen and "source=dhcp" in seen[0]
    with pytest.raises(n.NetError, match="Code 1"):
        n.apply(5, None, runner=lambda cl: 1)


def test_dialog_baut_sich_auf():
    tk = pytest.importorskip("tkinter")
    import launcher
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("kein Display verfügbar")
    try:
        dlg = launcher.IpDialog(root, n.parse_adapters(PS_JSON))
        root.update()
        assert dlg.mode.get() == "dhcp" and str(dlg.entries[0].cget("state")) == "disabled"
        dlg.combo.current(2)                       # Ethernet, manuell
        dlg._load()
        assert dlg.mode.get() == "static" and dlg.vars["ip"].get() == "10.0.0.5"
        assert dlg.vars["mask"].get() == "255.0.0.0" and str(dlg.entries[0].cget("state")) == "normal"
        dlg.win.destroy()
    finally:
        root.destroy()
        del root
        gc.collect()
