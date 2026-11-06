"""LambdaControl-1-Simulator als Fake-Port (pyserial-kompatibel, in-memory, threadsicher).

Bildet die Kommunikationsroutinen der Firmware nach (``LCS/LCS9/LCS9.ARB:732-813``):
Echo über Routine 996, Lesen ohne Echo über 1004, Ausgabe wie BASIC-52 ``PRINT USING``
(Vorzeichenstelle + Feld + Leerzeichen, Zeilenende CR LF). Jedes vom Host gesendete Byte wird in
``sent`` protokolliert (Tests prüfen damit, dass nie verbotene Bytes/Befehle gesendet werden).

Wie die echte Firmware deutet der Simulator nach Ablauf des Byte-Timeouts das nächste Byte als
neues Funktionsbyte – auch 51/52. Empfängt er 51/52, wird das in ``forbidden_received`` vermerkt.

Manuell nutzbar über ein Nullmodem-Paar (com0com, socat-pty) oder TCP::

    python -m gateway.serial_lc1.simulator --port COM11          # Gateway nutzt COM10
    python -m gateway.serial_lc1.simulator --tcp 7001            # Gateway: port: socket://127.0.0.1:7001

Im Gateway selbst: ``port: sim://lc1-1`` (Simulator im Prozess, siehe ``get_simulator``).
"""

from __future__ import annotations

import argparse
import logging
import math
import socket
import threading
import time
from collections import deque

log = logging.getLogger(__name__)

CR, LF = 13, 10

# HD44780-Rückabbildung für den Kesselnamen
_LCD_ENC = {"ä": 0xE1, "ö": 0xEF, "ü": 0xF5, "°": 0xDF, "ß": 0xE2}


def basic_using(value: float, fmt: str) -> str:
    """BASIC-52 ``PRINT USING(fmt)``: Vorzeichenstelle, rechtsbündiges Feld, Leerzeichen."""
    if "." in fmt:
        ip, dp = fmt.split(".")
        width, dec = len(fmt), len(dp)
        body = f"{abs(value):{width}.{dec}f}"
    else:
        width = len(fmt)
        body = f"{abs(round(value)):{width}d}"
    sign = "-" if value < 0 and float(body) != 0 else " "
    return f"{sign}{body} "


def lcd_encode(text: str) -> bytes:
    return bytes(_LCD_ENC.get(c, ord(c) if ord(c) < 128 else ord("?")) for c in text)


def default_values() -> dict[int, float]:
    """Plausible Arbeitsdaten eines Scheitholzkessels im O2-Regelbetrieb."""
    k = {i: 0.0 for i in range(1, 36)}
    k.update({1: 55.3, 2: 78.4, 3: 182.6, 4: 6.12, 5: 35.0, 6: 0.0, 7: 0, 8: 104.2, 9: 14.3,
              10: 1.41, 11: 89.7, 12: 6.35, 13: 13.9, 14: 6.0, 15: 80.0, 16: -0.25, 17: 60.0,
              18: 140.0, 19: 1.5, 20: 0, 21: 78.0, 22: 5.0, 23: 42.0, 24: 12.0, 25: 95, 26: 0,
              27: 0, 28: 1, 29: 1, 30: 1, 31: 0, 32: 0.0, 33: 1, 34: 8.0, 35: 0})
    return k


def default_params() -> list[int]:
    e = [0] * 64
    from gateway.devices.lc1_tables import PARAMS
    for i, p in PARAMS.items():
        e[i] = p.min_raw if p.min_raw > 0 else 0
    e[0], e[2], e[14], e[15], e[16], e[19], e[20], e[21], e[22] = 12345, 3, 95, 160, 60, 1, 60, 80, 203
    e[29], e[31], e[32], e[33], e[44], e[45], e[49] = 12, 0, 1, 1, 80, 1, 4321
    return e


class Lc1Simulator:
    """Fake-Port mit pyserial-Schnittstelle (``write``, ``read``, ``timeout``, ``in_waiting``, …)."""

    def __init__(self, *, checksum: bool = True, int_mode: str = "trunc", version: float = 9.12,
                 system_nr: int = 1, kessel_name: str = "Holzkessel Süd", ident_format: str = "###.#",
                 byte_timeout_s: float = 2.0, values: dict[int, float] | None = None,
                 params: list[int] | None = None, ee_write_delay_s: float = 0.0) -> None:
        self._cv = threading.Condition()
        self._out: deque[int] = deque()
        self.timeout: float | None = None
        self.write_timeout: float | None = None
        self.is_open = True
        self.dtr = True
        self.rts = True
        self.port = "sim"
        # Inhalt
        self.checksum = checksum
        self.int_mode = int_mode            # "trunc" | "floor" – BASIC-52 INT() bei negativen Werten
        self.version = version
        self.system_nr = system_nr
        self.kessel_name = kessel_name
        self.ident_format = ident_format    # LCS9: "###.#", LCS1 V1.xx: "##.##"
        self.byte_timeout_s = byte_timeout_s
        self.values = values if values is not None else default_values()
        self.params = params if params is not None else default_params()
        self.port4, self.ic8, self.ic9 = 0x1F, 0x4D, 0x20
        self.ee_write_delay_s = ee_write_delay_s
        # Protokolle
        self.sent: list[int] = []                   # alles, was der Host gesendet hat
        self.forbidden_received: list[int] = []     # 51/52 als Funktionsbyte angekommen
        self.ee_writes: list[tuple[int, int]] = []  # (Adresse, Wert) tatsächlich programmiert
        self.commands: list[int] = []
        # Fehlerinjektion
        self.silent = False             # gar keine Reaktion
        self.wrong_echo_next = False    # nächstes Echo verfälschen
        self.lose_next_rx = False       # nächstes Host-Byte geht verloren
        self.lose_next_tx = False       # nächstes Ausgabebyte geht verloren
        self.lose_tx_after: int | None = None   # nach so vielen Ausgabebytes eines verlieren
        self.no_exec_ack = False        # nach CR kein CHR(11)
        self.corrupt_checksum = False
        self.extra_blank_lines = False  # zusätzliche Leerzeilen in Antworten
        self.line_ending = b"\r\n"
        # Zustand
        self._state = "idle"
        self._last = 0.0
        self._addr = 0
        self._digits: list[int] = []

    # ----- pyserial-Schnittstelle ------------------------------------------------------------
    @property
    def in_waiting(self) -> int:
        with self._cv:
            return len(self._out)

    def reset_input_buffer(self) -> None:
        with self._cv:
            self._out.clear()

    def reset_output_buffer(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        with self._cv:
            self.is_open = False
            self._cv.notify_all()

    def write(self, data: bytes) -> int:
        if not self.is_open:
            raise OSError("Simulator-Port geschlossen")
        for b in bytes(data):
            with self._cv:
                self.sent.append(b)
                self._on_byte(b)
                self._cv.notify_all()
        return len(data)

    def read(self, size: int = 1) -> bytes:
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        with self._cv:
            while not self._out:
                if not self.is_open:
                    return b""
                if deadline is None:
                    self._cv.wait(0.1)
                    continue
                rest = deadline - time.monotonic()
                if rest <= 0:
                    return b""
                self._cv.wait(rest)
            n = min(size, len(self._out))
            return bytes(self._out.popleft() for _ in range(n))

    # ----- Ausgabe -------------------------------------------------------------------------
    def _emit(self, data: bytes) -> None:
        for b in data:
            if self.lose_next_tx:
                self.lose_next_tx = False
                continue
            if self.lose_tx_after is not None:
                if self.lose_tx_after == 0:
                    self.lose_tx_after = None
                    continue
                self.lose_tx_after -= 1
            self._out.append(b)

    def _println(self, text: str = "") -> None:
        if self.extra_blank_lines:
            self._emit(self.line_ending)
        self._emit(text.encode("latin-1") + self.line_ending)

    def _echo(self, b: int) -> None:
        if self.wrong_echo_next:
            self.wrong_echo_next = False
            b = (b + 1) & 0xFF
        self._emit(bytes([b]))

    # ----- Zustandsautomat (Firmware 906-1014) ------------------------------------------------
    def _on_byte(self, b: int) -> None:
        if self.silent:
            return
        if self.lose_next_rx:
            self.lose_next_rx = False
            return
        now = time.monotonic()
        if self._state != "idle" and now - self._last > self.byte_timeout_s:
            log.debug("Simulator: Byte-Timeout im Zustand %s -> idle", self._state)
            self._state = "idle"
        self._last = now
        st = self._state
        if st == "idle":
            self._echo(b)                                   # 906 -> 996 echot jedes Byte
            if not 48 <= b <= 53:
                return                                      # 908 KOM-Störung
            self.commands.append(b)
            if b == 48:
                self._state = "read_addr"
            elif b == 49:
                self._state = "write_addr"
            elif b == 50:
                self._work_data()
            elif b in (51, 52):
                self.forbidden_received.append(b)           # 962/970 – im Test nie erlaubt
            elif b == 53:
                self._ident()
        elif st == "read_addr":                             # 912/922: ohne Echo
            if 48 <= b <= 111:
                self._println(basic_using(self.params[b - 48], "#####"))
            else:
                self._state = "idle"
        elif st == "write_addr":                            # 924: mit Echo, ohne Prüfung
            self._echo(b)
            self._addr = b - 48
            self._digits = []
            self._state = "write_digits"
        elif st == "write_digits":                          # 928-930: mit Echo, ohne Prüfung
            self._echo(b)
            self._digits.append(b - 48)
            if len(self._digits) == 5:
                self._state = "write_exec"
        elif st == "write_exec":                            # 932: ohne Echo, muss 13 sein
            if b != CR:
                self._state = "idle"
                return
            d = self._digits
            dw = d[0] * 10000 + d[1] * 1000 + d[2] * 100 + d[3] * 10 + d[4]
            if self.ee_write_delay_s:
                time.sleep(self.ee_write_delay_s)
            if 0 <= self._addr <= 63:
                self.params[self._addr] = dw & 0xFFFF
            self.ee_writes.append((self._addr, dw))
            if not self.no_exec_ack:
                self._emit(bytes([11]))                     # 938 P.CHR(11)
            self._state = "write_cont"
        elif st == "write_cont":                            # 940: mit Echo
            self._echo(b)
            self._state = "write_addr" if b == CR else "idle"

    def _int(self, v: float) -> int:
        return math.trunc(v) if self.int_mode == "trunc" else math.floor(v)

    def _work_data(self) -> None:
        self._println()                                     # P.U.(######.##) ohne Ausdruck
        fmt = "######.##"
        for i in range(1, 36):
            self._println(basic_using(self.values.get(i, 0.0), fmt))
        for v in (self.port4, self.ic8, self.ic9):
            self._println(basic_using(v, fmt))
        if self.checksum:
            cs = self.port4 + self.ic8 + self.ic9 + sum(self._int(self.values.get(i, 0.0)) for i in range(1, 36))
            if self.corrupt_checksum:
                cs += 7
            self._println(basic_using(cs, fmt))

    def _ident(self) -> None:
        self._println()                                     # P.U.(###.#) ohne Ausdruck
        name = (self.kessel_name + " " * 15)[:15]
        out = basic_using(self.version, self.ident_format) + basic_using(self.system_nr, self.ident_format)
        self._emit(out.encode("latin-1") + lcd_encode(name))   # kein CRLF am Ende (LCS9.ARB:793)

    # ----- Hilfen für Tests ------------------------------------------------------------------
    def host_sent_function_bytes(self) -> list[int]:
        return list(self.commands)


# --- Registry für sim://-Ports -----------------------------------------------------------------
_REGISTRY: dict[str, Lc1Simulator] = {}
_REG_LOCK = threading.Lock()


def get_simulator(url: str, **kwargs) -> Lc1Simulator:
    """Liefert den Simulator zu ``sim://name`` (legt ihn bei Bedarf an)."""
    with _REG_LOCK:
        sim = _REGISTRY.get(url)
        if sim is None:
            sim = _REGISTRY[url] = Lc1Simulator(**kwargs)
        sim.is_open = True
        return sim


def register_simulator(url: str, sim: Lc1Simulator) -> None:
    with _REG_LOCK:
        _REGISTRY[url] = sim


def clear_simulators() -> None:
    with _REG_LOCK:
        _REGISTRY.clear()


# --- Brücke auf echten Port / TCP --------------------------------------------------------------
def _pump(sim: Lc1Simulator, rx, tx, stop: threading.Event) -> None:
    """rx(): Bytes vom Host lesen, tx(data): Bytes zum Host schreiben."""
    sim.timeout = 0.05

    def out_loop():
        while not stop.is_set():
            d = sim.read(64)
            if d:
                try:
                    tx(d)
                except OSError:
                    stop.set()

    t = threading.Thread(target=out_loop, daemon=True)
    t.start()
    while not stop.is_set():
        try:
            d = rx()
        except OSError:
            break
        if d is None:
            break
        if d:
            sim.write(d)
    stop.set()
    t.join(1)


def serve_serial(port: str, sim: Lc1Simulator) -> None:
    import serial  # pyserial

    ser = serial.serial_for_url(port, baudrate=9600, bytesize=8, parity="N", stopbits=1, timeout=0.05)
    print(f"LC1-Simulator an {port} (9600 8N1). Strg+C beendet.")
    stop = threading.Event()
    try:
        _pump(sim, lambda: ser.read(64), ser.write, stop)
    finally:
        ser.close()


def serve_tcp(tcp_port: int, sim: Lc1Simulator, host: str = "127.0.0.1") -> None:
    srv = socket.create_server((host, tcp_port))
    print(f"LC1-Simulator auf socket://{host}:{tcp_port}. Strg+C beendet.")
    while True:
        conn, addr = srv.accept()
        print(f"Verbindung von {addr}")
        conn.settimeout(0.05)
        stop = threading.Event()

        def rx():
            try:
                d = conn.recv(64)
                return d if d else None
            except TimeoutError:
                return b""

        _pump(sim, rx, conn.sendall, stop)
        conn.close()
        print("Verbindung beendet")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LambdaControl-1-Simulator (RS232)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--port", help="serieller Port (z. B. COM11 eines com0com-Paars, /dev/pts/3)")
    g.add_argument("--tcp", type=int, help="TCP-Port für socket://127.0.0.1:<port>")
    ap.add_argument("--no-checksum", action="store_true", help="ältere Firmware ohne Prüfsumme K36")
    ap.add_argument("--int-mode", choices=["trunc", "floor"], default="trunc")
    ap.add_argument("--name", default="Holzkessel Süd")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO)
    sim = Lc1Simulator(checksum=not a.no_checksum, int_mode=a.int_mode, kessel_name=a.name)
    try:
        if a.port:
            serve_serial(a.port, sim)
        else:
            serve_tcp(a.tcp, sim)
    except KeyboardInterrupt:
        pass
    if sim.forbidden_received:
        print(f"WARNUNG: verbotene Funktionsbytes empfangen: {sim.forbidden_received}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
