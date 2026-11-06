"""Multiframe-Blöcke: CRC16, Zerlegen in Rahmen, Zusammensetzen.

Firmware (Branch LambdaControl_2_1.29):
- CRC: ``crc16``/``GenerateCRC16`` (os2/Kernel/CRC16.cpp:55-159). Tabellengetriebene CRC-CCITT
  (Polynom 0x1021, Startwert 0, ohne Reflexion, ohne Endmaske = "CRC-16/XMODEM"). Die CRC über
  ``len-2`` Bytes wird **Big-Endian** an das Blockende geschrieben; die Prüfung rechnet die CRC über
  den ganzen Block inkl. CRC und erwartet 0 (``ChecksumOkay``, BusMultiBuffer.cpp:47-56).
- Ein Block beginnt mit ``Len`` (u16 LE), das die Gesamtlänge **inklusive** CRC angibt.
- Senden: ``CCAN::AddMsg`` (CAN MCP2515.cpp:385-433): Block in 8-Byte-Rahmen; ist ``len >= 8`` und
  ``len % 8 == 0``, wird die Länge um 1 erhöht -> ein Zusatzrahmen mit DLC 1 (Inhalt: das Byte
  hinter dem Puffer, also undefiniert). Alle Rahmen tragen dieselbe ID.
- Empfang: ``eBusMultiBuffer::BusInsertBuffer`` (BusMultiBuffer.cpp:147-206), Schlüssel ist die
  vollständige ID. Neuer Puffer nur, wenn DLC >= 4 und 8 <= Len <= 256. Überlauf -> verwerfen.
  Fertig, wenn ``pos >= len`` **oder** ein Rahmen mit DLC < 8 kommt; dann CRC-Prüfung (Fehler ->
  verwerfen). ``timeAllive`` = 10 s ab dem letzten Rahmen (Timer 1 s, BusMultiBuffer.cpp:14-40,
  212-223).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

MIN_LEN = 8
MAX_LEN = 256
TIMEOUT_S = 10.0


def _make_table() -> list[int]:
    table = []
    for i in range(256):
        crc = i << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
        table.append(crc & 0xFFFF)
    return table


#: entspricht ``crc_ccitt_table`` in os2/Kernel/CRC16.cpp:55-89 (Test vergleicht Stichproben)
CRC_TABLE: tuple[int, ...] = tuple(_make_table())


def crc16(data: bytes, crc: int = 0) -> int:
    """``_crc16``: ``sum = table[(sum >> 8) ^ byte] ^ (sum << 8)`` (16 Bit)."""
    t = CRC_TABLE
    for b in data:
        crc = (t[((crc >> 8) ^ b) & 0xFF] ^ (crc << 8)) & 0xFFFF
    return crc


def append_crc(buf: bytes) -> bytes:
    """``GenerateCRC16``: ``buf`` enthält bereits alle Bytes ohne die 2 CRC-Bytes."""
    crc = crc16(buf)
    return bytes(buf) + bytes((crc >> 8, crc & 0xFF))


def checksum_ok(block: bytes) -> bool:
    return crc16(block) == 0


def make_block(body: bytes) -> bytes:
    """Block = Len (u16 LE, inkl. CRC) + body + CRC16 (BE)."""
    total = len(body) + 4
    if not MIN_LEN <= total <= 0xFFFF:
        raise ValueError(f"Blocklänge {total} ungültig")
    return append_crc(total.to_bytes(2, "little") + bytes(body))


def split_frames(block: bytes, pad: int = 0x00) -> list[bytes]:
    """Block -> Rahmen-Nutzdaten wie ``CCAN::AddMsg`` mit ``Block=1``.

    Der Zusatzrahmen bei ``len % 8 == 0`` enthält in der Firmware ein undefiniertes Byte;
    hier wird ``pad`` gesendet.
    """
    data = bytes(block)
    n = len(data)
    if n >= 8 and n % 8 == 0:
        data += bytes((pad,))
    return [data[i:i + 8] for i in range(0, len(data), 8)] or [b""]


@dataclass
class _Buf:
    length: int
    data: bytearray
    pos: int = 0
    last: float = field(default_factory=time.monotonic)

    def add(self, chunk: bytes, now: float) -> bool:
        if self.pos + len(chunk) > self.length:
            return False
        self.data[self.pos:self.pos + len(chunk)] = chunk
        self.pos += len(chunk)
        self.last = now
        return True


Status = Literal["ignored", "partial", "ok", "crc", "overflow"]


@dataclass
class FeedResult:
    status: Status
    block: bytes | None = None


class Reassembler:
    """Setzt Blöcke je CAN-ID zusammen (Verhalten wie ``eBusMultiBuffer``)."""

    def __init__(self, timeout_s: float = TIMEOUT_S, clock=time.monotonic) -> None:
        self.timeout_s = timeout_s
        self._clock = clock
        self._bufs: dict[int, _Buf] = {}

    def __len__(self) -> int:
        return len(self._bufs)

    def reset(self, arb_id: int | None = None) -> None:
        if arb_id is None:
            self._bufs.clear()
        else:
            self._bufs.pop(arb_id, None)

    def expire(self, now: float | None = None) -> int:
        now = self._clock() if now is None else now
        old = [k for k, b in self._bufs.items() if now - b.last >= self.timeout_s]
        for k in old:
            del self._bufs[k]
        return len(old)

    def feed(self, arb_id: int, data: bytes, now: float | None = None) -> FeedResult:
        now = self._clock() if now is None else now
        self.expire(now)
        data = bytes(data)
        buf = self._bufs.get(arb_id)
        if buf is None:
            if len(data) < 4:
                return FeedResult("ignored")
            length = int.from_bytes(data[0:2], "little")
            if length > MAX_LEN or length < MIN_LEN:
                return FeedResult("ignored")
            buf = _Buf(length, bytearray(length), last=now)
            self._bufs[arb_id] = buf
            buf.add(data, now)      # erster Rahmen passt immer (DLC <= 8 <= Len)
        elif not buf.add(data, now):
            del self._bufs[arb_id]
            return FeedResult("overflow")
        if buf.pos >= buf.length or len(data) < 8:
            del self._bufs[arb_id]
            block = bytes(buf.data)
            if checksum_ok(block):
                return FeedResult("ok", block)
            return FeedResult("crc")
        return FeedResult("partial")
