"""Selbsttest der LC1-Anbindung: Port öffnen, Echo (Befehl 53), Arbeitsdaten (50), Prüfsumme.

Sendet ausschließlich Lesebefehle (53, 50). Für den Feldtest: ``gateway selftest``.
"""

from __future__ import annotations

from gateway.config import GatewayConfig
from gateway.drivers import DriverError, TraceCb
from gateway.errors import SafetyViolation
from gateway.serial_lc1.driver import Lc1Driver, PortFactory


async def lc1_selftest(cfg: GatewayConfig, *, port_factory: PortFactory | None = None,
                       trace: TraceCb | None = None) -> list[tuple[str, bool, str]]:
    devices = [d for d in cfg.devices if d.bus == "lc1" and d.enabled]
    if not devices:
        return [("Geräte", True, "keine aktiven LC1-Geräte konfiguriert")]
    drv = Lc1Driver(cfg, trace=trace, port_factory=port_factory)
    results: list[tuple[str, bool, str]] = []
    try:
        for d in devices:
            pre = f"{d.id} ({d.port})"
            # 1. Port öffnen
            try:
                await drv._call(d.port, lambda p: None)
                results.append((f"{pre} Port", True, "geöffnet (9600 8N1, kein Handshake)"))
            except DriverError as exc:
                results.append((f"{pre} Port", False, str(exc)))
                continue
            # 2. Echo / Identifikation
            try:
                ident = await drv.ident(d)
                results.append((f"{pre} Echo/Ident", True,
                                f"Version {ident.version_text or '?'}, System {ident.system_nr}, "
                                f"Kessel '{ident.name}'"))
            except (DriverError, SafetyViolation) as exc:
                results.append((f"{pre} Echo/Ident", False,
                                f"{exc} – Kabel (Nullmodem/1:1?), Port, Regler eingeschaltet?"))
                continue
            # 3. Arbeitsdaten + 4. Prüfsumme
            try:
                wd = await drv.read_work_data(d)
            except (DriverError, SafetyViolation) as exc:
                results.append((f"{pre} Arbeitsdaten", False, str(exc)))
                continue
            n = sum(1 for v in wd.values.values() if v is not None)
            results.append((f"{pre} Arbeitsdaten", n == 35,
                            f"{n}/35 Werte, TK Ist {wd.values.get(2)}, Störung {wd.values.get(7)}, "
                            f"Status {wd.values.get(28)}; Port4={wd.port4} IC8={wd.ic8} IC9={wd.ic9}"))
            if wd.checksum_ok is None:
                ok = d.checksum != "on"
                results.append((f"{pre} Prüfsumme", ok,
                                "keine Prüfsumme K36 gesendet (Firmware vor LCS9?)" +
                                ("" if ok else " – checksum: on konfiguriert!")))
            else:
                results.append((f"{pre} Prüfsumme", bool(wd.checksum_ok),
                                f"K36={wd.checksum:g} ({wd.checksum_variant})"))
    finally:
        await drv.close()
    return results
