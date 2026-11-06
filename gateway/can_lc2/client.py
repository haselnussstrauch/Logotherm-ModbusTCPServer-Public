"""asyncio-Client für den Logotherm-CAN-Bus (python-can).

Grundregeln:
- **Genau eine** Methode sendet: ``CanClient._send_frame``. Sie ruft vor jedem Senden
  ``gateway.safety.check_can_tx`` auf. Kein anderer Codepfad ruft ``bus.send`` auf (Test
  ``tests/test_can_client.py::test_nur_eine_sendestelle``).
- Immer nur **eine** offene Anfrage auf dem ganzen Bus (``asyncio.Lock``), Mindestabstand
  ``timing.min_gap_ms`` zwischen dem Ende einer Transaktion und dem nächsten Senden,
  Timeout ``timing.request_timeout_ms`` (je Rahmen, verlängert sich bei Multiframe-Antworten),
  ``timing.retries`` Wiederholungen – **nie** bei ``Parameter setzen``.
- Zuordnung einer Antwort über (Quelle = Ziel der Anfrage, Ziel = Gateway, Kanal 0x0A,
  Subkanal, Nummer). Alles andere wird ignoriert; Quelladressen werden für Scan/Diagnose gesammelt.
- Eigene Echo-Rahmen (InnoMaker/gs_usb liefert gesendete Rahmen zurück) werden verworfen.
  Rahmen mit Sender = Gateway-Adresse, die kein Echo sind, gelten als Adresskonflikt: danach wird
  nicht mehr gesendet (InterfaceError), bis neu geöffnet wird.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Literal

import can

from gateway import safety
from gateway.can_lc2 import idcodec, multiframe
from gateway.can_lc2.bus import open_can_bus
from gateway.config import CanConfig
from gateway.drivers import DeviceTimeout, InterfaceError, ProtocolError, TraceCb
from gateway.errors import SafetyViolation

log = logging.getLogger(__name__)

#: hochauflösende Uhr für Drosselung/Zeitstempel (time.monotonic hat unter Windows ~15,6 ms)
_now = time.perf_counter

#: Echo-Erkennung: gesendete Rahmen werden so lange zum Abgleich vorgehalten
ECHO_WINDOW_S = 2.0
#: Ende einer 0xFFFF-Aufzählung: so lange kein Rahmen mehr vom Gerät
#: TODO(Feld): an echter Anlage kalibrieren (Firmware sendet ohne Endekennung, BusOut.cpp:318-400).
ENUM_IDLE_S = 2.0
#: Obergrenze für eine komplette Aufzählung
ENUM_MAX_S = 120.0
#: so viele Empfangsfehler in Folge (je 0,2 s Pause) -> Adapter gilt als getrennt und wird neu geöffnet
REOPEN_AFTER_FAILS = 10
#: SocketCAN: so oft prüfen, ob die Schnittstelle noch dieselbe ist (Adapter neu angesteckt)
IFINDEX_CHECK_S = 1.0

Mode = Literal["single", "block", "enum"]
FrameHook = Callable[[can.Message, "idcodec.FrameId | None", str], None]


@dataclass
class HeardInfo:
    count: int = 0
    first: float = 0.0
    last: float = 0.0
    services: set[tuple[int, int]] = field(default_factory=set)


@dataclass
class _Pending:
    src: int
    sub: int
    mode: Mode
    nr: int | None
    arb_id: int
    activity: float
    done: asyncio.Event = field(default_factory=asyncio.Event)
    result: bytes | None = None
    blocks: list[bytes] = field(default_factory=list)
    crc_errors: int = 0
    error: Exception | None = None


def frame_text(msg: can.Message) -> str:
    """Rahmen im candump-Stil plus Dekodierung der ID."""
    data = bytes(msg.data)
    head = f"{msg.arbitration_id:08X}#{data.hex().upper()}" if msg.is_extended_id \
        else f"{msg.arbitration_id:03X}#{data.hex().upper()}"
    if msg.is_error_frame:
        return f"{head}  Fehlerrahmen"
    if not msg.is_extended_id:
        return f"{head}  Standard-ID (fremd)"
    try:
        fid = idcodec.decode(msg.arbitration_id)
    except ValueError:
        return head
    extra = ""
    if len(data) >= 2 and fid.kanal in (idcodec.KANAL_ANFRAGE, idcodec.KANAL_BEFEHLE) and fid.subkanal != 0:
        extra = f" nr={int.from_bytes(data[0:2], 'little'):#06x}"
    elif fid.kanal == idcodec.KANAL_ANTWORT and fid.subkanal == 0x20 and len(data) >= 3:
        extra = f" nr={int.from_bytes(data[0:2], 'little'):#06x} vt={data[2]:#04x}"
    return f"{head}  {fid}{extra}"


class CanClient:
    """Siehe Moduldoku. ``bus_factory(can_cfg, listen_only)`` ersetzt ``open_can_bus`` (Tests)."""

    def __init__(self, can_cfg: CanConfig, *, trace: TraceCb | None = None,
                 bus_factory: Callable[[CanConfig, bool], can.BusABC] | None = None) -> None:
        self.cfg = can_cfg
        self.trace = trace
        self._bus_factory = bus_factory or (lambda c, lo: open_can_bus(c, listen_only=lo))
        self._bus: can.BusABC | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._listen_only = False
        self._lock = asyncio.Lock()
        self._pending: _Pending | None = None
        self._reasm = multiframe.Reassembler()
        self._recent_tx: deque[tuple[float, int, bytes]] = deque()
        self._last_end = 0.0           # Ende der letzten Transaktion (TX oder letzter RX-Rahmen)
        self._conflict: str | None = None
        self._iface_error: str | None = None
        self.heard: dict[int, HeardInfo] = {}
        self.hooks: list[FrameHook] = []
        self._stats = {"tx": 0, "rx": 0, "timeouts": 0, "errors": 0, "crc_errors": 0, "echo": 0,
                       "ignored": 0, "retries": 0, "error_frames": 0, "own_address_conflicts": 0,
                       "safety_blocked": 0}

    # ===== Öffnen / Schließen ===================================================================
    @property
    def own_address(self) -> int:
        return self.cfg.gateway_address

    @property
    def is_open(self) -> bool:
        return self._bus is not None and self._running

    @property
    def listen_only(self) -> bool:
        return self._listen_only

    async def open(self, listen_only: bool = False) -> None:
        if self.is_open:
            return
        self._loop = asyncio.get_running_loop()
        try:
            self._bus = await asyncio.to_thread(self._bus_factory, self.cfg, listen_only)
        except InterfaceError:
            raise
        except Exception as exc:  # noqa: BLE001 – jede Adapterstörung als InterfaceError melden
            raise InterfaceError(f"CAN-Adapter ({self.cfg.interface}/{self.cfg.channel}) nicht verfügbar: "
                                 f"{exc}") from exc
        self._listen_only = listen_only
        self._conflict = None
        self._iface_error = None
        self._reasm.reset()
        self._running = True
        self._thread = threading.Thread(target=self._reader, name="can-rx", daemon=True)
        self._thread.start()
        log.info("CAN geöffnet: %s/%s %d bit/s, Gateway-Adresse %#04x%s", self.cfg.interface,
                 self.cfg.channel, self.cfg.bitrate, self.own_address, " (nur hören)" if listen_only else "")

    async def close(self) -> None:
        self._running = False
        th, self._thread = self._thread, None
        if th is not None:
            await asyncio.to_thread(th.join, 2.0)
        bus, self._bus = self._bus, None
        if bus is not None:
            try:
                await asyncio.to_thread(bus.shutdown)
            except Exception as exc:  # noqa: BLE001
                log.warning("CAN schließen: %s", exc)
        if self._pending is not None:
            self._pending.error = InterfaceError("CAN geschlossen")
            self._pending.done.set()

    def bus_state(self) -> str:
        bus = self._bus
        if bus is None:
            return "geschlossen"
        try:
            return str(getattr(bus.state, "name", bus.state))
        except Exception:  # noqa: BLE001
            return "unbekannt"

    @property
    def bus(self) -> can.BusABC | None:
        """Nur für Diagnose (Bitrate/Kanalinfo). NICHT zum Senden verwenden."""
        return self._bus

    def stats(self) -> dict:
        s = dict(self._stats)
        s["bus_state"] = self.bus_state()
        s["heard"] = sorted(self.heard)
        s["own_address_conflict"] = self._conflict
        s["interface_error"] = self._iface_error
        return s

    # ===== Empfang (Thread -> asyncio) ==========================================================
    def _ifindex(self) -> int | None:
        """SocketCAN: aktueller Index der Schnittstelle (neu angesteckt = neuer Index), sonst None."""
        if self.cfg.interface != "socketcan":
            return None
        try:
            return socket.if_nametoindex(str(self.cfg.channel))
        except OSError:
            return 0                      # Schnittstelle gerade nicht vorhanden
        except Exception:  # noqa: BLE001 – Plattform ohne if_nametoindex
            return None

    def _disconnected(self, reason: str) -> None:
        """Adapter getrennt: als geschlossen melden -> der Treiber öffnet beim nächsten Zugriff neu."""
        log.error("CAN-Adapter getrennt (%s) – wird beim nächsten Zugriff neu geöffnet", reason)
        self._iface_error = f"CAN-Adapter getrennt: {reason}"
        self._running = False
        p = self._pending
        if p is not None and self._loop is not None and not self._loop.is_closed():
            def _fail(p=p, err=self._iface_error):
                p.error = InterfaceError(err)
                p.done.set()
            try:
                self._loop.call_soon_threadsafe(_fail)
            except RuntimeError:
                pass

    def _reader(self) -> None:
        fails = 0
        ifindex = self._ifindex()
        next_check = _now() + IFINDEX_CHECK_S
        while self._running:
            bus = self._bus
            if bus is None:
                break
            if ifindex and _now() >= next_check:
                next_check = _now() + IFINDEX_CHECK_S
                cur = self._ifindex()
                if cur is not None and cur != ifindex:
                    # SocketCAN meldet ein Abziehen nur einmal; danach ist der Socket an eine
                    # verschwundene Schnittstelle gebunden und empfängt einfach nichts mehr.
                    self._disconnected(f"Schnittstelle {self.cfg.channel} "
                                       f"{'fehlt' if not cur else 'wurde neu angelegt'}")
                    break
            try:
                msg = bus.recv(0.1)
                fails = 0
            except Exception as exc:  # noqa: BLE001
                fails += 1
                self._stats["errors"] += 1
                self._iface_error = str(exc)
                if fails == 1:
                    log.error("CAN-Empfang gestört: %s", exc)
                if fails >= REOPEN_AFTER_FAILS:
                    self._disconnected(str(exc))
                    break
                time.sleep(0.2)
                continue
            if msg is None:
                continue
            loop = self._loop
            if loop is None or loop.is_closed():
                break
            try:
                loop.call_soon_threadsafe(self._on_frame, msg, _now())
            except RuntimeError:
                break

    def _trace(self, direction: str, text: str) -> None:
        if self.trace is not None:
            try:
                self.trace(direction, text)
            except Exception:  # noqa: BLE001 – Mitschnitt darf den Betrieb nie stören
                log.debug("Trace-Callback fehlgeschlagen", exc_info=True)

    def _is_echo(self, msg: can.Message, now: float) -> bool:
        if getattr(msg, "is_rx", True) is False:
            return True        # gs_usb/socketcan markieren eigene Rahmen
        while self._recent_tx and now - self._recent_tx[0][0] > ECHO_WINDOW_S:
            self._recent_tx.popleft()
        key = (msg.arbitration_id, bytes(msg.data))
        for i, (_, arb, data) in enumerate(self._recent_tx):
            if (arb, data) == key:
                del self._recent_tx[i]
                return True
        return False

    def _on_frame(self, msg: can.Message, now: float) -> None:
        if msg.is_error_frame:
            self._stats["error_frames"] += 1
            self._trace("RX", frame_text(msg))
            self._call_hooks(msg, None, "error")
            return
        fid: idcodec.FrameId | None = None
        if msg.is_extended_id and not msg.is_remote_frame:
            try:
                fid = idcodec.decode(msg.arbitration_id)
            except ValueError:
                fid = None
        if fid is not None and fid.sender == self.own_address:
            if self._is_echo(msg, now):
                self._stats["echo"] += 1
                self._trace("ECHO", frame_text(msg))
                self._call_hooks(msg, fid, "echo")
                return
            self._stats["own_address_conflicts"] += 1
            self._conflict = (f"Ein anderer Teilnehmer sendet mit der Gateway-Adresse "
                              f"{self.own_address:#04x} ({frame_text(msg)})")
            log.error("CAN: %s – Senden gesperrt", self._conflict)
        self._stats["rx"] += 1
        self._trace("RX", frame_text(msg))
        self._call_hooks(msg, fid, "rx")
        if fid is None:
            self._stats["ignored"] += 1
            return
        if fid.sender not in (0x00, self.own_address):
            h = self.heard.setdefault(fid.sender, HeardInfo(first=now))
            h.count += 1
            h.last = now
            h.services.add((fid.kanal, fid.subkanal))
        p = self._pending
        if (p is None or fid.kanal != idcodec.KANAL_ANTWORT or fid.empfaenger != self.own_address
                or fid.sender != p.src or fid.subkanal != p.sub):
            self._stats["ignored"] += 1
            return
        self._handle_answer(p, msg, now)

    def _call_hooks(self, msg: can.Message, fid, kind: str) -> None:
        for hook in list(self.hooks):
            try:
                hook(msg, fid, kind)
            except Exception:  # noqa: BLE001
                log.debug("Frame-Hook fehlgeschlagen", exc_info=True)

    def _handle_answer(self, p: _Pending, msg: can.Message, now: float) -> None:
        data = bytes(msg.data)
        p.activity = now
        self._last_end = now
        if p.mode == "single":
            if len(data) == 8 and (p.nr is None or int.from_bytes(data[0:2], "little") == p.nr):
                p.result = data
                p.done.set()
            else:
                self._stats["ignored"] += 1
            return
        res = self._reasm.feed(msg.arbitration_id, data, now)
        if res.status in ("partial", "ignored"):
            return
        if res.status in ("crc", "overflow"):
            self._stats["crc_errors"] += 1
            p.crc_errors += 1
            if p.mode != "enum":
                p.error = ProtocolError(f"Multiframe von {p.src:#04x}/{p.sub:#04x}: "
                                        f"{'CRC-Fehler' if res.status == 'crc' else 'Überlänge'}")
                p.done.set()
            return
        block = res.block or b""
        if p.mode == "enum":
            p.blocks.append(block)
            return
        if p.nr is not None and (len(block) < 4 or int.from_bytes(block[2:4], "little") != p.nr):
            self._stats["ignored"] += 1      # verspätete Antwort auf eine frühere Anfrage
            return
        p.result = block
        p.done.set()

    # ===== Senden – EINZIGE Stelle mit bus.send =================================================
    async def _send_frame(self, arb_id: int, data: bytes, *, regler_typ: int | None,
                          allow_enumeration: bool = False) -> None:
        """Prüft den Rahmen über ``safety.check_can_tx`` und sendet ihn. Einziger Sendepfad."""
        data = bytes(data)
        try:
            safety.check_can_tx(arb_id, data, is_extended=True, own_address=self.own_address,
                                regler_typ=regler_typ, is_remote_frame=False,
                                allow_enumeration=allow_enumeration)
        except SafetyViolation:
            self._stats["safety_blocked"] += 1
            log.critical("CAN: Sendewunsch durch Sicherheitsprüfung blockiert: %08X#%s",
                         arb_id, data.hex().upper())
            raise
        bus = self._bus
        if bus is None or not self._running:
            raise InterfaceError("CAN-Schnittstelle ist nicht geöffnet")
        if self._listen_only:
            raise InterfaceError("CAN-Schnittstelle ist im Nur-Hören-Modus geöffnet")
        if self._conflict:
            raise InterfaceError(self._conflict)
        # Mindestabstand exakt einhalten (Windows: asyncio-Uhr nur ~15,6 ms genau -> nachprüfen)
        target = self._last_end + self.cfg.timing.min_gap_ms / 1000.0
        while (wait := target - _now()) > 0:
            await asyncio.sleep(wait if wait > 0.002 else 0)
        msg = can.Message(arbitration_id=arb_id, data=bytearray(data), is_extended_id=True,
                          is_remote_frame=False, is_error_frame=False)
        now = _now()
        self._recent_tx.append((now, arb_id, data))
        # vor dem Senden protokollieren, damit TX im Mitschnitt vor der Antwort steht
        self._trace("TX", frame_text(can.Message(arbitration_id=arb_id, data=data, is_extended_id=True)))
        try:
            await asyncio.to_thread(bus.send, msg, 0.5)
        except can.CanError as exc:
            self._stats["errors"] += 1
            raise InterfaceError(f"CAN senden fehlgeschlagen: {exc}") from exc
        self._last_end = _now()
        self._stats["tx"] += 1

    # ===== Anfragen =============================================================================
    async def request(self, dest: int, kanal: int, sub: int, data: bytes, *, mode: Mode,
                      nr: int | None = None, regler_typ: int | None = None,
                      retries: int | None = None, timeout_s: float | None = None,
                      allow_enumeration: bool = False, idle_s: float = ENUM_IDLE_S,
                      max_s: float = ENUM_MAX_S, confirm_sub: int | None = None) -> bytes | list[bytes]:
        """Sendet eine Anfrage und wartet auf die zugeordnete Antwort (Kanal 0x0A, gleicher Subkanal).

        ``mode``: ``single`` (Einzelrahmen, z. B. 0x20), ``block`` (Multiframe, z. B. 0x00/0x30/0x38),
        ``enum`` (0xFFFF-Aufzählung: Liste aller Blöcke bis ``idle_s`` Ruhe).
        Wiederholung nur bei Timeout/CRC-Fehler und nur, wenn ``retries`` > 0.
        ``confirm_sub``: Antwort kommt auf einem anderen Subkanal (Parameter setzen 0x08/0x00 ->
        Bestätigung 0x0A/0x38, BusOut.cpp:931-934).
        """
        if self._iface_error and not self.is_open:
            raise InterfaceError(self._iface_error)
        tries = 1 + (self.cfg.timing.retries if retries is None else max(0, retries))
        timeout = self.cfg.timing.request_timeout_ms / 1000.0 if timeout_s is None else timeout_s
        arb_id = idcodec.encode(kanal, self.own_address, dest, sub)
        ans_sub = sub if confirm_sub is None else confirm_sub
        answer_id = idcodec.encode(idcodec.KANAL_ANTWORT, dest, self.own_address, ans_sub)
        last_exc: Exception | None = None
        async with self._lock:
            for attempt in range(tries):
                if attempt:
                    self._stats["retries"] += 1
                p = _Pending(src=dest, sub=ans_sub, mode=mode, nr=nr, arb_id=answer_id,
                             activity=_now())
                self._reasm.reset(answer_id)          # Reste einer früheren Antwort verwerfen
                self._pending = p
                try:
                    await self._send_frame(arb_id, data, regler_typ=regler_typ,
                                           allow_enumeration=allow_enumeration)
                    p.activity = _now()
                    return await self._wait(p, timeout, idle_s, max_s)
                except (DeviceTimeout, ProtocolError) as exc:
                    last_exc = exc
                    continue
                finally:
                    self._pending = None
                    self._last_end = max(self._last_end, _now())
        assert last_exc is not None
        raise last_exc

    async def _wait(self, p: _Pending, timeout: float, idle_s: float, max_s: float) -> bytes | list[bytes]:
        start = _now()
        while True:
            now = _now()
            if p.mode == "enum":
                limit = p.activity + (idle_s if p.blocks or p.crc_errors else timeout)
                if now - start > max_s:
                    break
            else:
                limit = p.activity + timeout
            remaining = limit - now
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(p.done.wait(), remaining)
            except asyncio.TimeoutError:
                continue          # Aktivität kann die Frist verlängert haben
            if p.error is not None:
                raise p.error
            if p.mode != "enum":
                assert p.result is not None
                return p.result
        if p.mode == "enum" and (p.blocks or p.crc_errors):
            if p.crc_errors:
                log.warning("Aufzählung %#04x/%#04x: %d Block/Blöcke mit CRC-Fehler verworfen",
                            p.src, p.sub, p.crc_errors)
            return list(p.blocks)
        self._stats["timeouts"] += 1
        raise DeviceTimeout(f"keine Antwort von {p.src:#04x} (Subkanal {p.sub:#04x}"
                            f"{'' if p.nr is None else f', Nr {p.nr:#06x}'})")

    async def listen(self, duration_s: float) -> dict:
        """Passiv mithören (sendet nichts). Ergebnis: Rahmenzahl und gehörte Quelladressen."""
        rx0 = self._stats["rx"]
        conflicts0 = self._stats["own_address_conflicts"]
        t0 = _now()
        await asyncio.sleep(duration_s)
        srcs = sorted(a for a, h in self.heard.items() if h.last >= t0)
        return {"frames": self._stats["rx"] - rx0, "sources": srcs,
                "own_address_seen": self._stats["own_address_conflicts"] > conflicts0}
