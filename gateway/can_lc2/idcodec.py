"""29-Bit-CAN-Identifier des Logotherm-Busprotokolls (Empfangsseite/Diagnose).

Firmware: ``CAN_IDENTIFIER`` (os2/Include/dev/CAN.h:80-110) ist ein DWORD
``Kanal<<24 | Sender<<16 | Empfänger<<8 | Kanal2`` (Little-Endian-Union). Der MCP2515-Treiber
(os2/Drivers/Block/CAN MCP2515.cpp, ``CCAN::Encode``) sendet ``DWORD >> 3`` als 29-Bit-ID; Bit 2
des Subkanal-Bytes wird dabei zum RTR-Bit (``MCPTransmit``), Bits 1..0 gehen verloren.

Ergebnis: Kanal Bit 28..21 | Sender 20..13 | Empfänger 12..5 | Bits 4..0 = Subkanal-Byte >> 3.

Dieses Modul ist bewusst unabhängig von ``gateway.safety`` implementiert (dort wird für die
Sendeprüfung selbst dekodiert); ein Test stellt sicher, dass beide übereinstimmen.
"""

from __future__ import annotations

from dataclasses import dataclass

CAN_ID_MAX = (1 << 29) - 1

KANAL_SPEZIAL = 0x00
KANAL_BEFEHLE = 0x08
KANAL_ANTWORT = 0x0A
KANAL_ANFRAGE = 0x0B
KANAL_SPEZIALX = 0x10
BROADCAST = 0xFF

KANAL_NAMEN = {
    KANAL_SPEZIAL: "Spezial",
    KANAL_BEFEHLE: "Befehl",
    KANAL_ANTWORT: "Antwort",
    KANAL_ANFRAGE: "Anfrage",
    KANAL_SPEZIALX: "Bootloader",
}

#: Subkanäle (Kanal2) mit Bedeutung je Kanal (BusIn.cpp: Bus_Anfrage / Bus_Antwort / Befehle)
SUB_NAMEN = {
    (KANAL_ANFRAGE, 0x00): "Identify",
    (KANAL_ANFRAGE, 0x08): "Auswahltexte",
    (KANAL_ANFRAGE, 0x20): "Messwert",
    (KANAL_ANFRAGE, 0x28): "Fehlerspeicher",
    (KANAL_ANFRAGE, 0x30): "Messwert-Detail",
    (KANAL_ANFRAGE, 0x38): "Parameter-Detail",
    (KANAL_ANFRAGE, 0x40): "Parameter-Array",
    (KANAL_ANFRAGE, 0x80): "Menüstruktur",
    (KANAL_ANFRAGE, 0x88): "Aktueller Fehler",
    (KANAL_ANTWORT, 0x00): "Identify",
    (KANAL_ANTWORT, 0x08): "Auswahltexte",
    (KANAL_ANTWORT, 0x20): "Messwert",
    (KANAL_ANTWORT, 0x28): "Fehlerspeicher",
    (KANAL_ANTWORT, 0x30): "Messwert-Detail",
    (KANAL_ANTWORT, 0x38): "Parameter-Detail",
    (KANAL_ANTWORT, 0x40): "Parameter-Array",
    (KANAL_ANTWORT, 0x80): "Menüstruktur",
    (KANAL_ANTWORT, 0x88): "Aktueller Fehler",
    (KANAL_BEFEHLE, 0x00): "Parameter setzen",
    (KANAL_BEFEHLE, 0x08): "Bootloader",
    (KANAL_BEFEHLE, 0x10): "Erw. Parameter setzen",
    (KANAL_BEFEHLE, 0x18): "Aggregatetest",
}

#: Antwort-Subkanäle, die als Multiframe-Block (Len … CRC16) übertragen werden (BusIn.cpp:209-316).
MULTIFRAME_ANTWORT_SUBS = frozenset({0x00, 0x08, 0x28, 0x30, 0x38, 0x40, 0x80, 0x88})


@dataclass(frozen=True)
class FrameId:
    kanal: int
    sender: int
    empfaenger: int
    subkanal: int          # Subkanal-Byte (Vielfaches von 8)

    @property
    def name(self) -> str:
        return SUB_NAMEN.get((self.kanal, self.subkanal), f"Sub {self.subkanal:#04x}")

    def __str__(self) -> str:
        k = KANAL_NAMEN.get(self.kanal, f"{self.kanal:#04x}")
        return (f"{k}({self.kanal:#04x})/{self.name} {self.sender:#04x}->{self.empfaenger:#04x} "
                f"sub={self.subkanal:#04x}")


def decode(arbitration_id: int) -> FrameId:
    """29-Bit-ID -> Felder. ``ValueError`` bei ungültiger ID."""
    if not isinstance(arbitration_id, int) or isinstance(arbitration_id, bool) \
            or not 0 <= arbitration_id <= CAN_ID_MAX:
        raise ValueError(f"ungültige CAN-ID {arbitration_id!r}")
    return FrameId(kanal=arbitration_id >> 21 & 0xFF,
                   sender=arbitration_id >> 13 & 0xFF,
                   empfaenger=arbitration_id >> 5 & 0xFF,
                   subkanal=(arbitration_id & 0x1F) << 3)


def encode(kanal: int, sender: int, empfaenger: int, subkanal: int) -> int:
    """Felder -> 29-Bit-ID. Das Subkanal-Byte muss ein Vielfaches von 8 sein."""
    for v in (kanal, sender, empfaenger, subkanal):
        if not isinstance(v, int) or not 0 <= v <= 0xFF:
            raise ValueError("Feld außerhalb 0..255")
    if subkanal & 0x07:
        raise ValueError("Subkanal-Byte muss ein Vielfaches von 8 sein")
    return kanal << 21 | sender << 13 | empfaenger << 5 | subkanal >> 3
