"""CAN-Selbsttest für Inbetriebnahme/Feldtest.

Reihenfolge (sicherste Schritte zuerst):
1. Adapter öffnen – zuerst im Listen-only-Modus (gs_usb), damit eine falsche Bitrate den Bus nicht
   mit Fehlerrahmen stört.
2. Bitrate/Bitzeit anzeigen, Bus-Zustand und Fehlerrahmen.
3. Passiv mithören: Verkehr? Welche Quelladressen?
4. Gateway-Adresse frei (niemand sendet mit ihr)?
5. Erst dann im Normalmodus öffnen und Identify an die konfigurierten CAN-Geräte senden
   (über den normalen Client -> ``safety.check_can_tx``).
"""

from __future__ import annotations

import logging

from gateway.can_lc2.bus import check_bitrate
from gateway.can_lc2.client import CanClient
from gateway.can_lc2.driver import CanDriver, type_name
from gateway.config import GatewayConfig
from gateway.drivers import DriverError, InterfaceError

log = logging.getLogger(__name__)

LISTEN_S = 3.0

Result = tuple[str, bool, str]


async def can_selftest(cfg: GatewayConfig, *, listen_s: float = LISTEN_S, bus_factory=None) -> list[Result]:
    results: list[Result] = []
    c = cfg.can
    own = c.gateway_address

    # --- 1. Adapter öffnen (passiv) ---
    passive = CanClient(c, bus_factory=bus_factory)
    mode = "Listen-only"
    try:
        try:
            await passive.open(listen_only=True)
        except InterfaceError as exc:
            if "Listen-only" not in str(exc):
                raise
            mode = "Normalmodus (Adapter kann kein Listen-only; es wird hier nichts gesendet)"
            await passive.open(listen_only=False)
    except InterfaceError as exc:
        results.append(("Adapter öffnen", False, str(exc)))
        return results
    try:
        results.append(("Adapter öffnen", True, f"{c.interface}/{c.channel} geöffnet ({mode})"))
        results.append(("Bitrate", *check_bitrate(passive.bus, c)))

        # --- 3./4. passiv mithören ---
        res = await passive.listen(listen_s)
        st = passive.stats()
        srcs = ", ".join(f"{a:#04x}" for a in res["sources"]) or "keine"
        if res["frames"]:
            results.append(("Busverkehr", True, f"{res['frames']} Rahmen in {listen_s:.0f} s, Quellen: {srcs}"))
        else:
            results.append(("Busverkehr", False,
                            f"kein Rahmen in {listen_s:.0f} s – Verkabelung/Terminierung/Bitrate "
                            f"({c.bitrate} bit/s) prüfen; ältere LC2 (AT90CAN128) laufen mit 20 kbit/s"))
        bus_ok = st["error_frames"] == 0 and st["bus_state"] not in ("ERROR",)
        results.append(("Bus-Zustand", bus_ok,
                        f"Zustand {st['bus_state']}, Fehlerrahmen {st['error_frames']}, "
                        f"Empfangsfehler {st['errors']}"))
        if res["own_address_seen"]:
            results.append(("Gateway-Adresse frei", False,
                            f"{own:#04x} wird von einem anderen Teilnehmer benutzt – can.gateway_address ändern"))
            return results
        results.append(("Gateway-Adresse frei", True, f"{own:#04x}: kein Teilnehmer sendet mit dieser Adresse"))
    finally:
        await passive.close()

    # --- 5. Identify an konfigurierte Geräte ---
    devices = [d for d in cfg.devices if d.bus == "can" and d.enabled and d.address is not None]
    if not devices:
        results.append(("Identify", True, "keine CAN-Geräte konfiguriert – übersprungen"))
        return results
    drv = CanDriver(cfg, bus_factory=bus_factory, address_check_s=0)
    try:
        await drv.open()
        for d in devices:
            try:
                info = await drv.identify(d.address)
            except DriverError as exc:
                results.append((f"Identify {d.id} ({d.address:#04x})", False, str(exc)))
                continue
            ok = d.regler_typ is None or d.regler_typ == info.regler_typ
            text = (f"{type_name(info.regler_typ, d.address)} „{info.name}“ V{info.version} SN {info.serial}")
            if not ok:
                text += f" – Reglertyp {info.regler_typ:#06x} ≠ konfiguriert {d.regler_typ:#06x}"
            results.append((f"Identify {d.id} ({d.address:#04x})", ok, text))
    except InterfaceError as exc:
        results.append(("Identify", False, str(exc)))
    finally:
        await drv.close()
    return results
