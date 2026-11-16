"""IP-Adresse des Gateway-PCs anzeigen und ändern (Windows, für den Launcher).

- Lesen: PowerShell (Get-NetAdapter -Physical, Get-NetIPInterface/-Address, Get-NetRoute,
  Get-DnsClientServerAddress) als JSON – keine Administratorrechte nötig.
- Ändern: ``netsh interface ipv4 …`` mit Administratorrechten (UAC-Abfrage). Das Interface wird
  über seinen **Index** angesprochen, alle Werte sind geprüfte IPv4-Adressen – es gibt also keine
  frei eingegebenen Zeichenketten in der Befehlszeile.

Nur Standardbibliothek; die Logik ohne tkinter ist testbar (tests/test_netconfig.py).
"""

from __future__ import annotations

import ipaddress
import json
import subprocess
import sys
from dataclasses import dataclass, field

PS_TIMEOUT_S = 20
APPLY_TIMEOUT_S = 60

_PS_LIST = r"""
$r = @()
foreach ($a in Get-NetAdapter -Physical -ErrorAction SilentlyContinue) {
  $i = Get-NetIPInterface -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue
  if (-not $i) { continue }
  $ip = @(Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
          ForEach-Object { "$($_.IPAddress)/$($_.PrefixLength)" })
  $gw = @(Get-NetRoute -InterfaceIndex $a.ifIndex -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
          ForEach-Object { $_.NextHop })
  $dns = @((Get-DnsClientServerAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue).ServerAddresses)
  $r += [pscustomobject]@{ alias = $a.Name; index = $a.ifIndex; desc = $a.InterfaceDescription;
                           status = "$($a.Status)"; dhcp = "$($i.Dhcp)"; ips = $ip; gateways = $gw; dns = $dns }
}
ConvertTo-Json -InputObject @($r) -Depth 3 -Compress
"""


class NetError(Exception):
    """Lesen/Ändern fehlgeschlagen oder ungültige Eingabe (Text für den Benutzer)."""


@dataclass
class Adapter:
    alias: str
    index: int
    desc: str = ""
    status: str = ""
    dhcp: bool = False
    ips: list[str] = field(default_factory=list)        # "192.168.6.22/24" (ohne 169.254.x.x)
    gateways: list[str] = field(default_factory=list)
    dns: list[str] = field(default_factory=list)
    apipa: bool = False                                  # nur 169.254.x.x: DHCP-Server nicht erreichbar

    @property
    def connected(self) -> bool:
        return self.status.lower() == "up"

    @property
    def ip(self) -> str:
        return self.ips[0].split("/")[0] if self.ips else ""

    @property
    def mask(self) -> str:
        if not self.ips:
            return ""
        return str(ipaddress.IPv4Network(self.ips[0], strict=False).netmask)

    @property
    def gateway(self) -> str:
        return next((g for g in self.gateways if g and g != "0.0.0.0"), "")

    def summary(self) -> str:
        mode = "DHCP" if self.dhcp else "manuell"
        if not self.connected:
            return f"{self.alias}: nicht verbunden ({mode})"
        if not self.ips:
            hint = " – kein DHCP-Server erreichbar?" if self.dhcp else ""
            return f"{self.alias}: keine IP-Adresse ({mode}){hint}"
        return f"{self.alias}: {', '.join(self.ips)} ({mode})"


def _as_list(v) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, list) else [v]


def parse_adapters(text: str) -> list[Adapter]:
    """JSON aus ``_PS_LIST`` -> Adapter (verbundene zuerst)."""
    try:
        data = json.loads(text or "[]")
    except json.JSONDecodeError as exc:
        raise NetError(f"Netzwerkdaten nicht lesbar: {exc}") from exc
    out = []
    for d in _as_list(data):
        if not isinstance(d, dict) or "index" not in d:
            continue
        ips = [str(x) for x in _as_list(d.get("ips")) if x]
        real = [x for x in ips if not x.startswith("169.254.")]
        out.append(Adapter(
            alias=str(d.get("alias") or ""), index=int(d["index"]), desc=str(d.get("desc") or ""),
            status=str(d.get("status") or ""), dhcp=str(d.get("dhcp", "")).lower() == "enabled",
            ips=real, gateways=[str(x) for x in _as_list(d.get("gateways")) if x],
            dns=[str(x) for x in _as_list(d.get("dns")) if x], apipa=bool(ips) and not real))
    out.sort(key=lambda a: (not a.connected, a.alias.lower()))
    return out


def list_adapters(run=None) -> list[Adapter]:
    if sys.platform != "win32" and run is None:
        return []
    run = run or (lambda: subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PS_LIST],
        capture_output=True, text=True, timeout=PS_TIMEOUT_S,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
    try:
        res = run()
    except (OSError, subprocess.SubprocessError) as exc:
        raise NetError(f"Netzwerkadapter konnten nicht gelesen werden: {exc}") from exc
    if res.returncode != 0:
        raise NetError(f"Netzwerkadapter konnten nicht gelesen werden: {(res.stderr or '').strip()[:200]}")
    return parse_adapters(res.stdout)


# --- Eingabe prüfen -------------------------------------------------------------------------
@dataclass
class StaticConfig:
    ip: str
    mask: str
    gateway: str = ""          # leer = kein Standardgateway
    dns: list[str] = field(default_factory=list)


def _ipv4(text: str, what: str) -> ipaddress.IPv4Address:
    try:
        return ipaddress.IPv4Address(text.strip())
    except ValueError:
        raise NetError(f"{what}: „{text.strip()}“ ist keine gültige IPv4-Adresse") from None


def validate_static(ip: str, mask: str, gateway: str = "", dns: str = "") -> StaticConfig:
    """Prüft die Eingaben. Maske als 255.255.255.0 oder Präfix (24 bzw. /24)."""
    addr = _ipv4(ip, "IP-Adresse")
    m = mask.strip().lstrip("/")
    try:
        net = ipaddress.IPv4Network(f"{addr}/{m}", strict=False)
    except ValueError:
        raise NetError(f"Subnetzmaske: „{mask.strip()}“ ist ungültig (z. B. 255.255.255.0 oder 24)") from None
    if net.prefixlen < 8 or net.prefixlen > 30:
        raise NetError("Subnetzmaske: erlaubt sind /8 bis /30 (z. B. 255.255.255.0)")
    if addr in (net.network_address, net.broadcast_address):
        raise NetError(f"IP-Adresse {addr} ist die Netz- bzw. Broadcast-Adresse von {net}")
    if addr.is_loopback or addr.is_multicast or addr.is_unspecified or addr.is_link_local:
        raise NetError(f"IP-Adresse {addr} ist hier nicht erlaubt")
    gw = ""
    if gateway.strip():
        g = _ipv4(gateway, "Gateway")
        if g not in net:
            raise NetError(f"Gateway {g} liegt nicht im Netz {net} der IP-Adresse")
        if g == addr:
            raise NetError("Gateway und IP-Adresse dürfen nicht gleich sein")
        gw = str(g)
    servers = [str(_ipv4(x, "DNS-Server")) for x in dns.replace(";", ",").replace(" ", ",").split(",") if x.strip()]
    if len(servers) > 2:
        raise NetError("Höchstens zwei DNS-Server")
    return StaticConfig(ip=str(addr), mask=str(net.netmask), gateway=gw, dns=servers)


# --- Befehle ----------------------------------------------------------------------------------
def build_commands(index: int, static: StaticConfig | None) -> list[list[str]]:
    """netsh-Befehle; ``static=None`` = automatisch (DHCP)."""
    if not isinstance(index, int) or index <= 0:
        raise NetError("ungültiger Netzwerkadapter")
    base = ["netsh", "interface", "ipv4"]
    name = f"name={index}"
    if static is None:
        return [base + ["set", "address", name, "source=dhcp"],
                base + ["set", "dnsservers", name, "source=dhcp"]]
    cmds = [base + ["set", "address", name, "source=static", f"address={static.ip}", f"mask={static.mask}",
                    f"gateway={static.gateway or 'none'}"]]
    if static.dns:
        cmds.append(base + ["set", "dnsservers", name, "source=static", f"address={static.dns[0]}",
                            "register=primary", "validate=no"])
        for i, d in enumerate(static.dns[1:], start=2):
            cmds.append(base + ["add", "dnsservers", name, f"address={d}", f"index={i}", "validate=no"])
    else:
        cmds.append(base + ["set", "dnsservers", name, "source=static", "address=none"])
    return cmds


def command_line(cmds: list[list[str]]) -> str:
    """Eine cmd.exe-Zeile; bricht beim ersten Fehler ab (``&&``). Nur [A-Za-z0-9.=] in den Teilen."""
    for c in cmds:
        for part in c:
            if not all(ch.isalnum() or ch in ".=" for ch in part):
                raise NetError(f"unerwartetes Zeichen in {part!r}")
    return " && ".join(" ".join(c) for c in cmds)


def _is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _run_elevated(params: str, timeout_s: float) -> int:
    """cmd.exe /c <params> per UAC („runas“), wartet auf das Ende; Rückgabe: Exit-Code."""
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                    ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                    ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                    ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

    SEE_MASK_NOCLOSEPROCESS = 0x40
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = "cmd.exe"
    info.lpParameters = "/c " + params
    info.nShow = 0                                       # SW_HIDE
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        raise NetError("Administratorrechte wurden nicht erteilt – nichts geändert")
    k32 = ctypes.windll.kernel32
    try:
        if k32.WaitForSingleObject(info.hProcess, int(timeout_s * 1000)) != 0:
            raise NetError("Zeitüberschreitung beim Ändern der IP-Adresse")
        code = wintypes.DWORD()
        k32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        k32.CloseHandle(info.hProcess)


def apply(index: int, static: StaticConfig | None, runner=None) -> None:
    """Setzt DHCP bzw. die feste Adresse. ``runner(cmdline) -> exitcode`` für Tests."""
    line = command_line(build_commands(index, static))
    if runner is None:
        if sys.platform != "win32":
            raise NetError("nur unter Windows möglich")
        if _is_admin():
            def runner(cl: str) -> int:
                return subprocess.run(["cmd.exe", "/c", cl], capture_output=True, timeout=APPLY_TIMEOUT_S,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode
        else:
            def runner(cl: str) -> int:
                return _run_elevated(cl, APPLY_TIMEOUT_S)
    code = runner(line)
    if code != 0:
        raise NetError(f"netsh meldet Fehler (Code {code}). Ist die Adresse im Netz schon vergeben "
                       "oder der Adapter deaktiviert?")


def web_urls(adapters: list[Adapter], port: int) -> list[str]:
    """Adressen der Weboberfläche für andere Geräte im Netz."""
    return [f"http://{a.ip}:{port}" for a in adapters if a.connected and a.ip]
