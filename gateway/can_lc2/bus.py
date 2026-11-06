"""CAN-Adapter öffnen (python-can 4.6.1) – mit klaren Fehlermeldungen.

Befunde python-can 4.6.1 / gs_usb 0.3.1 / pyusb 1.3.1 (im venv geprüft):
- ``can.interfaces.gs_usb.GsUsbBus`` berechnet die Bitzeit mit
  ``can.BitTiming.from_sample_point(..., sample_point=87.5)``. Diese Funktion erzeugt nur
  "strikte" Timings (BRP <= 32, 8..25 TQ) – bei 48 MHz Adaptertakt (InnoMaker/candleLight) ist
  das Minimum 60 kbit/s. **50 kbit/s und 20 kbit/s scheitern dort mit "No suitable bit timings
  found."** Deshalb wird hier eine eigene Unterklasse verwendet, die die Bitzeit selbst aus den
  Grenzen des Adapters (``device_capability``: BRP bis 1024) berechnet.
- ``GsUsb.start()`` startet standardmäßig im Normalmodus (``GS_CAN_MODE_NORMAL |
  GS_CAN_MODE_HW_TIMESTAMP``), also ohne Loopback/Listen-only. Hier wird der Modus explizit
  gesetzt; Listen-only nur auf Wunsch und nur, wenn der Adapter es kann.
- Gesendete Rahmen kommen als Echo zurück (``is_rx=False``); der Client verwirft sie.
- ``channel`` muss eine **Zahl** (Geräteindex) sein – python-can vergleicht ``len(devs) <= index``.
- Windows: pyusb sucht ``libusb-1.0.dll`` per ``ctypes.util.find_library`` (PATH). Das Paket
  ``libusb-package`` wird von pyusb NICHT automatisch benutzt; ist es installiert, wird es hier
  verwendet. Zusätzlich: Umgebungsvariable ``LOGOTHERM_LIBUSB_DLL`` bzw. ``libusb-1.0.dll`` neben
  ``python.exe``/im Arbeitsverzeichnis. Der Adapter braucht den WinUSB-Treiber (z. B. per Zadig).
"""

from __future__ import annotations

import gc
import logging
import os
import sys
from pathlib import Path

import can

from gateway.config import CanConfig
from gateway.drivers import InterfaceError

log = logging.getLogger(__name__)

#: Abtastpunkt der Firmware (MCP2515: 1+8+8 von 20 TQ = 85 %, CAN MCP2515.cpp:36-80); Ausweichwert 87,5 %
SAMPLE_POINTS = (85.0, 87.5, 80.0)

# Standard-gs_usb-Kennung (Linux-Treiber), wird u. a. von InnoMaker USB2CAN verwendet
GS_USB_VID_PID = (0x1D50, 0x606F)


# =============================================================================================
# libusb (Windows)
# =============================================================================================
def _libusb_candidates() -> list[Path]:
    out: list[Path] = []
    if env := os.environ.get("LOGOTHERM_LIBUSB_DLL"):
        out.append(Path(env))
    for base in (Path(sys.executable).parent, Path(sys.prefix), Path.cwd(),
                 Path(__file__).resolve().parents[2]):
        out.append(base / "libusb-1.0.dll")
        out.append(base / "libusb" / "libusb-1.0.dll")
    return out


def ensure_libusb_backend():
    """Liefert das pyusb-libusb1-Backend oder wirft ``InterfaceError`` mit Hinweis."""
    try:
        import usb.backend.libusb1 as libusb1
    except ImportError as exc:  # pragma: no cover – pyusb ist Abhängigkeit von python-can[gs-usb]
        raise InterfaceError("pyusb fehlt (pip-Paket 'pyusb')") from exc
    backend = libusb1.get_backend()
    if backend is not None:
        return backend
    try:
        import libusb_package  # type: ignore[import-not-found]
        backend = libusb1.get_backend(find_library=libusb_package.find_library)
        if backend is not None:
            return backend
    except ImportError:
        pass
    for cand in _libusb_candidates():
        if cand.is_file():
            backend = libusb1.get_backend(find_library=lambda _name, p=str(cand): p)
            if backend is not None:
                log.info("libusb geladen: %s", cand)
                return backend
    raise InterfaceError(
        "libusb-1.0 nicht gefunden. Windows: libusb-1.0.dll (64 Bit) neben python.exe legen, "
        "LOGOTHERM_LIBUSB_DLL setzen oder das Paket 'libusb-package' installieren; den Adapter mit "
        "Zadig auf den WinUSB-Treiber umstellen. Linux: Paket libusb-1.0-0 installieren.")


# =============================================================================================
# Bitzeit
# =============================================================================================
def gs_usb_timing(fclk: int, bitrate: int, *, tseg1_min=1, tseg1_max=16, tseg2_min=1, tseg2_max=8,
                  sjw_max=4, brp_min=1, brp_max=1024, brp_inc=1) -> dict:
    """Bitzeit für gs_usb: exakte Bitrate, Abtastpunkt möglichst 85 % (wie die Firmware).

    Rückgabe: ``prop_seg, phase_seg1, phase_seg2, sjw, brp, sample_point`` (tseg1 = prop + phase1).
    """
    best = None
    for sp in SAMPLE_POINTS:
        for brp in range(max(1, brp_min), brp_max + 1, max(1, brp_inc)):
            if fclk % (bitrate * brp):
                continue
            nbt = fclk // (bitrate * brp)
            if nbt < 8 or nbt > 1 + tseg1_max + tseg2_max:
                continue
            tseg1 = round(sp / 100 * nbt) - 1
            tseg2 = nbt - 1 - tseg1
            if not (tseg1_min <= tseg1 <= tseg1_max and tseg2_min <= tseg2 <= tseg2_max):
                continue
            real_sp = 100.0 * (1 + tseg1) / nbt
            cand = (abs(real_sp - sp), -nbt, brp, tseg1, tseg2, real_sp)
            if best is None or cand < best:
                best = cand
        if best is not None and best[0] < 0.01:
            break
    if best is None:
        raise InterfaceError(f"keine Bitzeit für {bitrate} bit/s bei Adaptertakt {fclk} Hz")
    _, _, brp, tseg1, tseg2, real_sp = best
    sjw = max(1, min(2, tseg2, sjw_max))     # Firmware: SJW-Register 1 -> 2 TQ
    return {"prop_seg": 1, "phase_seg1": tseg1 - 1, "phase_seg2": tseg2, "sjw": sjw, "brp": brp,
            "sample_point": real_sp}


# =============================================================================================
# gs_usb
# =============================================================================================
def _make_gs_usb_bus(index: int, bitrate: int, listen_only: bool) -> can.BusABC:
    from can.interfaces.gs_usb import GsUsbBus
    from gs_usb.constants import GS_CAN_MODE_HW_TIMESTAMP, GS_CAN_MODE_LISTEN_ONLY, GS_CAN_MODE_NORMAL
    from gs_usb.gs_usb import GsUsb

    class LogothermGsUsbBus(GsUsbBus):
        """GsUsbBus mit eigener Bitzeit-Berechnung (siehe Moduldoku)."""

        def __init__(self) -> None:          # noqa: D401 – bewusst ohne super().__init__ von GsUsbBus
            self._is_shutdown = True        # bis zum erfolgreichen Start (kein __del__-Warnhinweis)
            ensure_libusb_backend()
            try:
                devs = GsUsb.scan()
            except Exception as exc:  # noqa: BLE001 – z. B. usb.core.NoBackendError
                raise InterfaceError(f"USB-Suche fehlgeschlagen: {exc}") from exc
            if not devs:
                raise InterfaceError(
                    "kein gs_usb-Adapter gefunden (InnoMaker USB2CAN: VID:PID 1D50:606F). Adapter "
                    "eingesteckt? Windows: WinUSB-Treiber per Zadig installiert?")
            if index >= len(devs):
                raise InterfaceError(f"gs_usb-Adapter Nr. {index} nicht vorhanden ({len(devs)} gefunden)")
            dev = devs[index]
            self.gs_usb = dev
            self._index = index
            self.channel_info = f"gs_usb:{index}"
            self._can_protocol = can.CanProtocol.CAN_20
            try:
                self._start(dev, bitrate, listen_only)
            except BaseException:
                # USB-Handle sofort freigeben, sonst blockiert es den nächsten Öffnungsversuch
                _release_usb(dev)
                raise
            can.BusABC.__init__(self, channel=index)

        def _start(self, dev, bitrate: int, listen_only: bool) -> None:
            try:
                cap = dev.device_capability
            except Exception as exc:  # noqa: BLE001 – USBError: Zugriff verweigert, Treiber fehlt
                raise InterfaceError(f"gs_usb-Adapter nicht ansprechbar (Treiber/Zugriffsrechte?): {exc}") from exc
            t = gs_usb_timing(cap.fclk_can, bitrate, tseg1_min=cap.tseg1_min, tseg1_max=cap.tseg1_max,
                              tseg2_min=cap.tseg2_min, tseg2_max=cap.tseg2_max, sjw_max=cap.sjw_max,
                              brp_min=cap.brp_min, brp_max=cap.brp_max, brp_inc=cap.brp_inc)
            self.timing = t
            self.fclk = cap.fclk_can
            dev.set_timing(t["prop_seg"], t["phase_seg1"], t["phase_seg2"], t["sjw"], t["brp"])
            flags = GS_CAN_MODE_NORMAL | GS_CAN_MODE_HW_TIMESTAMP
            if listen_only:
                if not cap.feature & GS_CAN_MODE_LISTEN_ONLY:
                    raise InterfaceError("Adapter unterstützt keinen Listen-only-Modus")
                flags |= GS_CAN_MODE_LISTEN_ONLY
            dev.start(flags)
            # Sicherstellen, dass weder Loopback noch (ungewollt) Listen-only aktiv ist
            from gs_usb.constants import GS_CAN_MODE_LOOP_BACK
            if dev.device_flags & GS_CAN_MODE_LOOP_BACK:
                dev.stop()
                raise InterfaceError("Adapter meldet Loopback-Modus – abgebrochen")
            self.listen_only = bool(dev.device_flags & GS_CAN_MODE_LISTEN_ONLY)
            self._bitrate = bitrate
            log.info("gs_usb %s: Takt %d Hz, BRP %d, TSEG1 %d, TSEG2 %d, SJW %d, Abtastpunkt %.1f %%%s",
                     dev, cap.fclk_can, t["brp"], t["prop_seg"] + t["phase_seg1"], t["phase_seg2"],
                     t["sjw"], t["sample_point"], ", nur hören" if self.listen_only else "")

        def shutdown(self) -> None:
            if self._is_shutdown:
                return
            can.BusABC.shutdown(self)
            try:
                self.gs_usb.stop()
            except Exception:  # noqa: BLE001
                pass
            _release_usb(self.gs_usb)
            self._is_shutdown = True

    try:
        return LogothermGsUsbBus()
    except InterfaceError as exc:
        # Ein noch nicht aufgeräumtes Handle aus einem früheren Öffnen (Suche/Test im selben
        # Prozess) kann WinUSB blockieren: einmal aufräumen lassen und erneut versuchen.
        if "nicht ansprechbar" not in str(exc):
            raise
        log.info("gs_usb: Adapter belegt (%s) – gebe alte Handles frei und versuche erneut", exc)
        gc.collect()
        return LogothermGsUsbBus()


def _release_usb(dev) -> None:
    """USB-Handle und Interface des Adapters sofort freigeben.

    ``GsUsb.stop()`` setzt nur den CAN-Modus zurück. pyusb schließt das Handle erst, wenn das
    Objekt vom Garbage Collector aufgeräumt wird – unbestimmt spät. Unter Windows (WinUSB, nur ein
    Handle je Gerät) scheitert dann das nächste Öffnen im selben Prozess, z. B. nach Gerätesuche
    oder CAN-Test, bis der Adapter neu eingesteckt wird.
    """
    usb_dev = getattr(dev, "gs_usb", None)
    if usb_dev is None:
        return
    try:
        import usb.util
        usb.util.dispose_resources(usb_dev)
    except Exception as exc:  # noqa: BLE001
        log.debug("gs_usb: Freigeben fehlgeschlagen: %s", exc)


# =============================================================================================
# Öffentlich
# =============================================================================================
def open_can_bus(cfg: CanConfig, *, listen_only: bool = False) -> can.BusABC:
    """Öffnet den Adapter laut Konfiguration. ``InterfaceError`` mit Klartext bei Problemen.

    ``listen_only``: gs_usb – Adapter im Listen-only-Modus (sendet auch keine ACK/Fehlerrahmen);
    pcan – ``state=PASSIVE``; socketcan – nicht aus dem Programm einstellbar (``ip link set can0
    type can listen-only on``), es wird trotzdem nichts gesendet; virtual – ohne Bedeutung.
    """
    iface = cfg.interface
    try:
        if iface == "gs_usb":
            try:
                index = int(cfg.channel, 0)
            except ValueError as exc:
                raise InterfaceError(f"gs_usb: Kanal muss die Adapternummer sein (z. B. \"0\"), nicht "
                                     f"{cfg.channel!r}") from exc
            return _make_gs_usb_bus(index, cfg.bitrate, listen_only)
        if iface == "socketcan":
            bus = can.Bus(interface="socketcan", channel=cfg.channel, receive_own_messages=False)
            log.info("socketcan %s: Bitrate wird vom System gesetzt (ip link set %s type can bitrate %d)",
                     cfg.channel, cfg.channel, cfg.bitrate)
            return bus
        if iface == "pcan":
            kwargs = {}
            if listen_only:
                kwargs["state"] = can.BusState.PASSIVE
            return can.Bus(interface="pcan", channel=cfg.channel, bitrate=cfg.bitrate, **kwargs)
        if iface == "virtual":
            return can.Bus(interface="virtual", channel=cfg.channel, receive_own_messages=False)
    except InterfaceError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise InterfaceError(f"CAN-Adapter {iface}/{cfg.channel} nicht verfügbar: {exc}") from exc
    raise InterfaceError(f"unbekannte CAN-Schnittstelle {iface!r}")


def describe_bus(bus: can.BusABC | None, cfg: CanConfig) -> str:
    """Kurztext zur Bitzeit für Selbsttest/Diagnose."""
    if bus is None:
        return "nicht geöffnet"
    t = getattr(bus, "timing", None)
    if t:
        return (f"{cfg.bitrate} bit/s, Takt {getattr(bus, 'fclk', 0)} Hz, BRP {t['brp']}, "
                f"TSEG1 {t['prop_seg'] + t['phase_seg1']}, TSEG2 {t['phase_seg2']}, SJW {t['sjw']}, "
                f"Abtastpunkt {t['sample_point']:.1f} %")
    if cfg.interface == "socketcan":
        return f"socketcan {cfg.channel}: Bitrate vom Betriebssystem (Soll {cfg.bitrate} bit/s)"
    return f"{cfg.interface}/{cfg.channel}, {cfg.bitrate} bit/s"
