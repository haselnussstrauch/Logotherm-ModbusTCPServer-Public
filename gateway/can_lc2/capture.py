"""Passiver CAN-Mitschnitt – sendet NIE.

Schreibt zwei Dateien:
- ``path``: candump-Logformat (``(sekunden.mikro) kanal ID#DATEN``), mit ``canplayer``/``python-can``
  (``can.LogReader``) wieder abspielbar.
- ``path`` + ``.decoded.txt``: je Rahmen eine dekodierte Zeile (Kanal/Sender/Empfänger/Subkanal/Nr/
  Wert) sowie fertig zusammengesetzte Multiframe-Blöcke (Identify, Detail).

Der Adapter wird – wo möglich – im Listen-only-Modus geöffnet (gs_usb: keine ACK/Fehlerrahmen, also
auch bei falscher Bitrate keine Störung des Busses). Es gibt in diesem Modul keinen Sendeaufruf.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path

import can

from gateway.can_lc2 import idcodec, messages, multiframe, vartyp
from gateway.can_lc2.bus import open_can_bus
from gateway.config import GatewayConfig
from gateway.drivers import InterfaceError

log = logging.getLogger(__name__)


def decode_frame(msg: can.Message, reasm: multiframe.Reassembler | None = None) -> str:
    """Eine Zeile Klartext zu einem Rahmen (für Mitschnitt und Diagnose)."""
    data = bytes(msg.data)
    if msg.is_error_frame:
        return "Fehlerrahmen"
    if not msg.is_extended_id:
        return f"Standard-ID {msg.arbitration_id:03X} (nicht Logotherm)"
    if msg.is_remote_frame:
        return "RTR-Rahmen"
    fid = idcodec.decode(msg.arbitration_id)
    head = (f"kanal={fid.kanal:#04x}({idcodec.KANAL_NAMEN.get(fid.kanal, '?')}) sender={fid.sender:#04x} "
            f"empf={fid.empfaenger:#04x} sub={fid.subkanal:#04x}({fid.name})")
    extra = ""
    if fid.kanal == idcodec.KANAL_ANTWORT and fid.subkanal == 0x20 and len(data) == 8:
        a = messages.MesswertAntwort.parse(data)
        try:
            raw = vartyp.decode_raw(a.vartyp, a.wert)
            extra = f" nr={a.nr:#06x} vt={vartyp.vartyp_name(a.vartyp)} roh={raw:g}" if isinstance(raw, float) \
                else f" nr={a.nr:#06x} vt={vartyp.vartyp_name(a.vartyp)} roh={raw}"
        except ValueError:
            extra = f" nr={a.nr:#06x} vt={a.vartyp:#04x} wert={a.wert.hex()}"
    elif fid.kanal in (idcodec.KANAL_ANFRAGE,) and fid.subkanal in (0x20, 0x28, 0x30, 0x38, 0x08) and len(data) >= 4:
        r = messages.Anfrage.parse(data.ljust(8, b"\0"))
        extra = f" nr={r.nr:#06x} code={r.code:#04x}"
    elif fid.kanal == idcodec.KANAL_BEFEHLE and fid.subkanal == 0x00 and len(data) == 8:
        s = messages.ParameterSetzen.parse(data)
        try:
            extra = f" SETZEN nr={s.nr:#06x} vt={vartyp.vartyp_name(s.vartyp)} roh={vartyp.decode_raw(s.vartyp, s.wert)}"
        except ValueError:
            extra = f" SETZEN nr={s.nr:#06x} vt={s.vartyp:#04x} wert={s.wert.hex()}"
    elif fid.kanal == idcodec.KANAL_ANTWORT and fid.subkanal in idcodec.MULTIFRAME_ANTWORT_SUBS and reasm is not None:
        res = reasm.feed(msg.arbitration_id, data)
        if res.status == "ok" and res.block:
            extra = " BLOCK " + describe_block(fid, res.block)
        elif res.status in ("crc", "overflow"):
            extra = f" Multiframe {res.status.upper()}-FEHLER"
        elif res.status == "partial":
            extra = " (Teil)"
    return head + extra


def describe_block(fid: idcodec.FrameId, block: bytes) -> str:
    try:
        if fid.subkanal == 0x00:
            i = messages.IdentifyAntwort.parse(block)
            return (f"Identify reglertyp={i.regler_typ:#06x} version={i.version_text} sn={i.seriennummer} "
                    f"name={i.name!r}")
        if fid.subkanal in (0x30, 0x38):
            d = messages.DetailAntwort.parse(block)
            val = ""
            if vartyp.is_numeric(d.vartyp):
                raw = vartyp.decode_raw(d.vartyp, d.wert)
                val = f" wert={vartyp.to_value(raw, d.vartyp, d.nachkomma, d.ausgabe):g}{d.unit}"
            return (f"Detail nr={d.nr:#06x} name={d.name!r} vt={vartyp.vartyp_name(d.vartyp)} nk={d.nachkomma} "
                    f"out={d.ausgabe} min={d.min} max={d.max} werk={d.werk}{val}")
    except (ValueError, Exception) as exc:  # noqa: BLE001
        return f"unlesbar ({exc})"
    return f"{len(block)} Byte"


def candump_line(msg: can.Message, channel: str, ts: float) -> str:
    ident = f"{msg.arbitration_id:08X}" if msg.is_extended_id else f"{msg.arbitration_id:03X}"
    if msg.is_remote_frame:
        body = "R"
    else:
        body = bytes(msg.data).hex().upper()
    return f"({ts:.6f}) {channel} {ident}#{body}"


async def capture(cfg: GatewayConfig, path: str | Path, duration_s: float | None = None, *,
                  listen_only: bool = True, stop_event: asyncio.Event | None = None) -> dict:
    """Mitschnitt bis ``duration_s`` abgelaufen, ``stop_event`` gesetzt oder abgebrochen wurde.

    Rückgabe: Statistik ``{"frames", "sources", "errors", "path", "decoded_path"}``.
    ``listen_only``: gs_usb im Listen-only-Modus öffnen (falls der Adapter das nicht kann, wird
    normal geöffnet – gesendet wird trotzdem nichts).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    dec_path = path.with_name(path.name + ".decoded.txt")
    try:
        bus = await asyncio.to_thread(open_can_bus, cfg.can, listen_only=listen_only)
    except InterfaceError as exc:
        if listen_only and "Listen-only" in str(exc):
            log.warning("Mitschnitt: %s – öffne im Normalmodus (es wird nichts gesendet)", exc)
            bus = await asyncio.to_thread(open_can_bus, cfg.can, listen_only=False)
        else:
            raise
    chan = f"can{cfg.can.channel}" if cfg.can.interface == "gs_usb" else str(cfg.can.channel)
    stats = {"frames": 0, "sources": set(), "errors": 0, "error_frames": 0, "path": str(path),
             "decoded_path": str(dec_path)}
    stop = threading.Event()
    reasm = multiframe.Reassembler()

    def reader() -> None:
        with path.open("a", encoding="ascii", buffering=1) as raw_f, \
                dec_path.open("a", encoding="utf-8", buffering=1) as dec_f:
            while not stop.is_set():
                try:
                    msg = bus.recv(0.2)
                except Exception as exc:  # noqa: BLE001
                    stats["errors"] += 1
                    log.error("Mitschnitt: Empfang gestört: %s", exc)
                    time.sleep(0.5)
                    continue
                if msg is None:
                    continue
                ts = time.time()
                stats["frames"] += 1
                if msg.is_error_frame:
                    stats["error_frames"] += 1
                raw_f.write(candump_line(msg, chan, ts) + "\n")
                try:
                    text = decode_frame(msg, reasm)
                    if msg.is_extended_id and not msg.is_error_frame:
                        stats["sources"].add(idcodec.decode(msg.arbitration_id).sender)
                except Exception as exc:  # noqa: BLE001
                    text = f"nicht dekodierbar: {exc}"
                echo = " ECHO" if getattr(msg, "is_rx", True) is False else ""
                dec_f.write(f"{time.strftime('%H:%M:%S', time.localtime(ts))}.{int(ts * 1000) % 1000:03d} "
                            f"{candump_line(msg, chan, ts).split(' ', 2)[2]:<30} {text}{echo}\n")

    th = threading.Thread(target=reader, name="can-capture", daemon=True)
    th.start()
    log.info("CAN-Mitschnitt nach %s (dekodiert: %s)", path, dec_path)
    try:
        if stop_event is not None:
            try:
                await asyncio.wait_for(stop_event.wait(), duration_s)
            except asyncio.TimeoutError:
                pass
        elif duration_s is not None:
            await asyncio.sleep(duration_s)
        else:
            while True:
                await asyncio.sleep(3600)
    finally:
        stop.set()
        await asyncio.to_thread(th.join, 2.0)
        await asyncio.to_thread(bus.shutdown)
    stats["sources"] = sorted(stats["sources"])
    log.info("CAN-Mitschnitt beendet: %d Rahmen, Quellen %s", stats["frames"],
             ", ".join(f"{a:#04x}" for a in stats["sources"]) or "keine")
    return stats
