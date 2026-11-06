"""LambdaControl 1 (RS232): Echo-Protokoll und Befehle 53/50/48/49.

Grundlage ist die Regler-Firmware (MCS BASIC-52), ``LCS/LCS9/LCS9.ARB:732-813`` und gleichlautend
``LCS1.ARB:922-992``:

- Der Regler prüft die Schnittstelle nur aus der Hauptschleife (``LCS9.ARB:811`` GOSUB 906, etwa
  1 x je Sekunde) -> das Echo des Funktionsbytes kann über 1 s dauern (wir warten line_timeout_s).
- Das 8051-UART hat EIN Empfangsbyte (SBUF). Deshalb sendet der Host das nächste Byte erst nach der
  Antwort (Echo bzw. Zeile).
- Zwei Empfangsroutinen: ``996`` liest ein Byte und echot es, ``1004`` liest ohne Echo.
  Timeout je Byte ``D4(0)=2`` s (``LCS9.ARB:734, 798``).

  ======  ==================================================================================
  53      Funktionsbyte (Echo). Antwort: CRLF, Programmversion (USING ###.# bzw. ##.##), Systemnr.
          E(32), 15 Zeichen Kesselname – OHNE abschließendes CRLF (``LCS9.ARB:792-793``).
  50      Funktionsbyte (Echo). Antwort: CRLF, 35 Zeilen K(1..35), Port 4, IC8, IC9 (alle USING
          ######.##), ab LCS9 zusätzlich Prüfsumme K(36) (``LCS9.ARB:2, 766-770``).
  48      Funktionsbyte (Echo). Je Adresse: Byte 48+Adr OHNE Echo -> Zeile ``#####``. Ende: ein Byte
          < 48 oder > 111 (wir senden CR) ohne Echo (``LCS9.ARB:739-744``).
  49      Funktionsbyte (Echo), 48+Adr (Echo), 5 Ziffern MSB zuerst (je Echo), CR (ohne Echo) ->
          Regler programmiert das EEPROM und sendet CHR(11); danach Byte mit Echo: 13 = weiteres
          Wort, sonst Ende (wir senden 11) (``LCS9.ARB:747-757``).
  51/52   NIE senden (Regelung anhalten / Ausgänge steuern) – safety.check_lc1_command.
  ======  ==================================================================================

Gefahr, die dieses Modul abfängt: Läuft der 2-s-Timeout des Reglers mitten in einem Befehl ab,
deutet er das nächste Byte als neues Funktionsbyte. Die Adressbytes 48..53 (E0..E5) und die
Ziffern '0'..'5' sind gleichzeitig Funktionsbytes (z. B. 51 = Regelung anhalten!). Deshalb:

- Jedes Folgebyte wird nur gesendet, wenn seit dem letzten empfangenen Byte höchstens
  ``continuation_max_s`` (Default byte_timeout_s/2) vergangen ist, sonst Abbruch ohne Senden.
- Nach jedem Fehler bleibt die Leitung ``quiet_s`` still (Regler läuft sicher in seinen Timeout).
- Lesebefehle werden wiederholt, Schreiben nie.

Alle Bytes zum Regler laufen durch ``Lc1Protocol._send_byte`` (einzige Stelle mit ``port.write``).
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Literal, Protocol

from gateway import safety
from gateway.drivers import DeviceTimeout, ProtocolError, TraceCb
from gateway.errors import SafetyViolation

log = logging.getLogger(__name__)

CR = 13
LF = 10
EXEC_OK = 11            # CHR(11): Regler meldet "EEPROM programmiert" bzw. Host: "Ende"

CMD_PARAM_LESEN = safety.LC1_CMD_PARAM_LESEN          # 48
CMD_PARAM_SCHREIBEN = safety.LC1_CMD_PARAM_SCHREIBEN  # 49
CMD_ARBEITSDATEN = safety.LC1_CMD_ARBEITSDATEN        # 50
CMD_IDENT = safety.LC1_CMD_IDENT                      # 53

N_K = 35                        # K(1..35)
N_WORK_LINES = N_K + 3          # + Port 4, IC8, IC9 (ohne Prüfsumme)
IDENT_NAME_LEN = 15             # LCS9.ARB:793 FOR DI=0 TO 14
IDENT_IDLE_S = 0.4              # Ident hat kein Endezeichen: Ende = Pause
AUTO_CHECKSUM_WAIT_S = 1.0      # checksum=auto: so lange auf die 39. Zeile warten
MAX_LINE_LEN = 64

ChecksumMode = Literal["auto", "on", "off"]

# HD44780-Zeichensatz (Kesselname aus dem Text-EPROM, LCS9.ARB:792 CBY(...))
_LCD_CHARS = {0xE1: "ä", 0xEF: "ö", 0xF5: "ü", 0xDF: "°", 0xE2: "ß", 0xE4: "µ"}
_SIGN_GAP_RX = re.compile(r"^([-+])\s+")
_NUM_RX = re.compile(r"[-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?")


class PortLike(Protocol):
    """pyserial-kompatibles Port-Objekt (``serial.Serial`` oder der Simulator)."""
    timeout: float | None

    def write(self, data: bytes) -> int | None: ...
    def read(self, size: int = 1) -> bytes: ...
    def reset_input_buffer(self) -> None: ...
    def close(self) -> None: ...


def decode_lcd(raw: bytes) -> str:
    """Bytes aus dem Regler-Text-EPROM lesbar machen (ASCII + HD44780-Umlaute)."""
    out = []
    for b in raw:
        if b in _LCD_CHARS:
            out.append(_LCD_CHARS[b])
        elif 0x20 <= b < 0x7F:
            out.append(chr(b))
        elif b in (0, 0xFF):
            out.append(" ")
        else:
            out.append("?")
    return "".join(out)


def fmt_bytes(data: bytes) -> str:
    """Hex + ASCII für den Mitschnitt."""
    hx = " ".join(f"{b:02X}" for b in data)
    asc = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in data)
    return f"{hx} |{asc}|"


def parse_number(text: str) -> float | None:
    """Toleranter Zahlenparser: Leerzeichen, Vorzeichen, Komma statt Punkt."""
    t = text.strip().replace(",", ".")
    if not t:
        return None
    # BASIC-52 PRINT USING kann das Vorzeichen vor die Feldauffüllung setzen ("-     0.25").
    t = _SIGN_GAP_RX.sub(lambda m: m.group(1), t)
    try:
        v = float(t)
    except ValueError:
        m = _NUM_RX.search(t)
        if not m:
            return None
        try:
            v = float(m.group(0).replace(",", "."))
        except ValueError:
            return None
    return v if math.isfinite(v) else None


# --- Ergebnisobjekte ---------------------------------------------------------------------------
@dataclass
class Lc1Ident:
    version: float | None
    system_nr: int | None
    name: str
    raw: bytes = b""
    text: str = ""

    @property
    def version_text(self) -> str:
        return "" if self.version is None else f"{self.version:g}"


@dataclass
class Lc1WorkData:
    values: dict[int, float | None]               # K-Index -> Wert
    port4: int | None = None
    ic8: int | None = None
    ic9: int | None = None
    checksum: float | None = None                 # K(36) wie empfangen
    checksum_ok: bool | None = None               # None = keine Prüfsumme vorhanden
    checksum_variant: str = ""                    # "INT=trunc", "INT=floor", "Rundungstoleranz", …
    lines: list[str] = field(default_factory=list)


def parse_ident(data: bytes) -> Lc1Ident:
    """Antwort auf 53 ohne das Echo. Format je Firmware verschieden, daher tolerant:
    Kesselname = die letzten 15 Zeichen (wie Module1.bas:191 / LCS17.BAS:183), davor zwei Zahlen
    (Programmversion, Systemnummer)."""
    body = data.lstrip(b"\r\n")
    text = decode_lcd(body)
    if len(body) < IDENT_NAME_LEN:
        raise ProtocolError(f"Ident zu kurz ({len(body)} Byte): {fmt_bytes(data)}")
    # Abschließendes CR/LF (falls eine Firmware es sendet) gehört nicht zum Namen.
    stripped = body.rstrip(b"\r\n")
    name_raw = stripped[-IDENT_NAME_LEN:]
    head = decode_lcd(stripped[:-IDENT_NAME_LEN]).replace("\r", " ").replace("\n", " ")
    nums = [m.group(0) for m in _NUM_RX.finditer(head)]
    version = parse_number(nums[0]) if nums else None
    system = parse_number(nums[1]) if len(nums) > 1 else None
    if len(nums) != 2:
        log.warning("LC1-Ident: %d statt 2 Zahlen vor dem Namen: %r", len(nums), head)
    return Lc1Ident(version=version, system_nr=None if system is None else int(round(system)),
                    name=decode_lcd(name_raw).strip(), raw=bytes(data), text=text)


def checksum_check(values: list[float], port4: int, ic8: int, ic9: int,
                   checksum: float) -> tuple[bool, str]:
    """Prüfsumme K36 = Port4 + IC8 + IC9 + Σ INT(K1..35) (LCS9.ARB:768-769).

    Offen in den Quellen: INT() bei negativen Werten (BASIC-52: abschneiden oder abrunden?) und
    dass INT auf den internen Wert wirkt, gesendet aber auf 2 Stellen gerundet wird. Darum werden
    beide Varianten akzeptiert und zusätzlich eine Rundungstoleranz von ±0,005 je Wert.
    """
    base = port4 + ic8 + ic9
    target = round(checksum)
    if abs(checksum - target) > 0.01:
        return False, f"Prüfsumme {checksum} nicht ganzzahlig"
    s_trunc = base + sum(math.trunc(v) for v in values)
    s_floor = base + sum(math.floor(v) for v in values)
    if target == s_trunc:
        return True, "INT=trunc" if s_trunc != s_floor else "exakt"
    if target == s_floor:
        return True, "INT=floor"
    lo = base + sum(math.floor(v - 0.005) for v in values)
    hi = base + sum(math.trunc(v + 0.005) for v in values)
    if lo <= target <= hi:
        return True, "Rundungstoleranz"
    return False, f"Prüfsumme {target} ≠ {s_trunc} (trunc) / {s_floor} (floor)"


# --- Protokoll ---------------------------------------------------------------------------------
class Lc1Protocol:
    """Ein Regler an einem Port. Nicht threadsicher – der Treiber hält je Port eine Sperre."""

    def __init__(self, port: PortLike, *, byte_timeout_s: float = 2.0, line_timeout_s: float = 3.0,
                 retries: int = 1, trace: TraceCb | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 name: str = "") -> None:
        self.port = port
        self.name = name
        self.trace = trace
        self.retries = retries
        self._clock = clock
        self._sleep = sleep
        self.set_timeouts(byte_timeout_s, line_timeout_s)
        self._last_rx = 0.0
        self._quiet_until = 0.0
        self._pushback: bytearray = bytearray()
        self.checksum_seen: bool | None = None     # auto: wurde schon eine Prüfsumme empfangen?
        self.counters = {"tx": 0, "rx": 0, "errors": 0, "timeouts": 0, "checksum_errors": 0,
                         "retries": 0, "commands": 0}
        self._lock = threading.Lock()              # Schutz gegen versehentliche Parallelnutzung

    def set_timeouts(self, byte_timeout_s: float, line_timeout_s: float) -> None:
        self.byte_timeout_s = float(byte_timeout_s)
        self.line_timeout_s = float(line_timeout_s)
        # Folgebyte nur, wenn der Regler-Timeout (2 s) sicher noch nicht abgelaufen ist.
        self.continuation_max_s = self.byte_timeout_s * 0.5
        # Nach Fehlern: Regler sicher in seinen Timeout laufen lassen (auch ein verspätet
        # verarbeitetes Funktionsbyte, Hauptschleife ca. 1 s).
        self.quiet_s = self.byte_timeout_s + self.line_timeout_s + 0.5

    # ----- Mitschnitt ------------------------------------------------------------------------
    def _trace(self, direction: str, text: str) -> None:
        if self.trace is not None:
            try:
                self.trace(direction, f"{self.name} {text}".strip())
            except Exception:  # noqa: BLE001 - Mitschnitt darf das Protokoll nie stören
                log.debug("Trace-Callback fehlgeschlagen", exc_info=True)

    # ----- Senden: EINZIGE Stelle mit port.write ----------------------------------------------
    def _send_byte(self, b: int, *, continuation: bool) -> None:
        """Sendet genau ein Byte nach Sicherheitsprüfung.

        ``continuation=True``: Folgebyte innerhalb eines Befehls – nur, wenn seit dem letzten
        Empfang höchstens ``continuation_max_s`` vergangen ist (sonst könnte der Regler das Byte
        als neues Funktionsbyte deuten)."""
        safety.check_lc1_byte(b)
        if continuation:
            gap = self._clock() - self._last_rx
            if gap > self.continuation_max_s:
                raise ProtocolError(
                    f"LC1: {gap:.2f} s seit der letzten Antwort (> {self.continuation_max_s:.2f} s) – "
                    f"Folgebyte {b:#04x} wird NICHT gesendet (Regler-Timeout droht)")
        self._trace("TX", fmt_bytes(bytes([b])))
        self.port.write(bytes([b]))
        self.counters["tx"] += 1

    # ----- Empfangen ---------------------------------------------------------------------------
    def _read_byte(self, timeout: float) -> int | None:
        if self._pushback:
            return self._pushback.pop(0)
        self.port.timeout = max(0.0, timeout)
        data = self.port.read(1)
        if not data:
            return None
        self._last_rx = self._clock()
        self.counters["rx"] += 1
        return data[0]

    def _expect_echo(self, b: int, timeout: float, what: str) -> None:
        got = self._read_byte(timeout)
        if got is None:
            raise DeviceTimeout(f"LC1: kein Echo auf {what} ({b:#04x}) innerhalb {timeout:.1f} s")
        self._trace("ECHO", fmt_bytes(bytes([got])))
        if got != b:
            raise ProtocolError(f"LC1: falsches Echo auf {what}: {got:#04x} statt {b:#04x}")

    def _read_line(self, timeout: float, what: str) -> str:
        """Eine Zeile bis LF/CR (CRLF, LFCR, CR, LF werden akzeptiert)."""
        deadline = self._clock() + timeout
        buf = bytearray()
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                b = None
            else:
                b = self._read_byte(min(remaining, max(self.byte_timeout_s, 0.05)))
            if b is None:
                if self._clock() < deadline:
                    continue
                if buf:
                    self._trace("RX", fmt_bytes(bytes(buf)) + " (unvollständig)")
                raise DeviceTimeout(f"LC1: keine (vollständige) Zeile für {what} innerhalb {timeout:.1f} s")
            if b in (CR, LF):
                # Gegenstück (LF nach CR bzw. CR nach LF) mitnehmen, falls es direkt folgt.
                tail = bytes([b])
                nxt = self._read_byte(0.05)
                if nxt is not None:
                    if nxt in (CR, LF) and nxt != b:
                        tail += bytes([nxt])
                    else:
                        self._pushback.append(nxt)
                self._trace("RX", fmt_bytes(bytes(buf) + tail))
                return buf.decode("latin-1")
            buf.append(b)
            if len(buf) > MAX_LINE_LEN:
                self._trace("RX", fmt_bytes(bytes(buf)) + " (zu lang)")
                raise ProtocolError(f"LC1: Zeile für {what} länger als {MAX_LINE_LEN} Zeichen")

    def _read_until_idle(self, idle_s: float, total_s: float, max_len: int = 128) -> bytes:
        deadline = self._clock() + total_s
        buf = bytearray()
        while len(buf) < max_len:
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            b = self._read_byte(min(remaining, idle_s if buf else remaining))
            if b is None:
                if buf:
                    break
                continue
            buf.append(b)
        if buf:
            self._trace("RX", fmt_bytes(bytes(buf)))
        return bytes(buf)

    # ----- Befehlsrahmen ---------------------------------------------------------------------
    def _drain(self) -> bytes:
        """Eingang leeren; liefert verworfene Bytes (sollte leer sein)."""
        self._pushback.clear()
        try:
            self.port.reset_input_buffer()
        except Exception:  # noqa: BLE001 - z. B. socket:// ohne Puffer
            pass
        junk = bytearray()
        while True:
            self.port.timeout = 0
            d = self.port.read(64)
            if not d:
                break
            junk += d
            if len(junk) > 4096:
                break
        return bytes(junk)

    def _begin(self, func: int) -> None:
        """Wartet die Ruhezeit ab, leert den Eingang, sendet das Funktionsbyte und prüft das Echo."""
        safety.check_lc1_command(func)
        wait = self._quiet_until - self._clock()
        if wait > 0:
            self._sleep(wait)
        junk = self._drain()
        if junk:
            # Regler sendet unaufgefordert / Rest eines alten Befehls -> Ruhezeit und erneut leeren.
            self._trace("RX", fmt_bytes(junk) + " (verworfen)")
            log.warning("LC1 %s: %d unerwartete Bytes verworfen, warte %.1f s", self.name, len(junk), self.quiet_s)
            self._sleep(self.quiet_s)
            junk = self._drain()
            if junk:
                raise ProtocolError(f"LC1: Leitung nicht ruhig ({len(junk)} Byte empfangen)")
        self.counters["commands"] += 1
        self._send_byte(func, continuation=False)
        self._expect_echo(func, self.line_timeout_s, f"Funktionsbyte {func}")

    def _failed(self, exc: Exception) -> None:
        self.counters["errors"] += 1
        if isinstance(exc, DeviceTimeout):
            self.counters["timeouts"] += 1
        self._quiet_until = self._clock() + self.quiet_s

    def _with_retry(self, what: str, fn):
        attempts = 1 + max(0, self.retries)
        for i in range(attempts):
            try:
                return fn()
            except SafetyViolation:
                raise
            except (DeviceTimeout, ProtocolError) as exc:
                self._failed(exc)
                if i + 1 >= attempts:
                    raise
                self.counters["retries"] += 1
                log.info("LC1 %s: %s fehlgeschlagen (%s), Wiederholung", self.name, what, exc)

    # ----- Befehl 53: Identifikation ----------------------------------------------------------
    def ident(self) -> Lc1Ident:
        with self._lock:
            return self._with_retry("Ident", self._ident_once)

    def _ident_once(self) -> Lc1Ident:
        self._begin(CMD_IDENT)
        data = self._read_until_idle(IDENT_IDLE_S, self.line_timeout_s)
        if not data:
            raise DeviceTimeout("LC1: Echo auf 53, aber keine Identifikation")
        return parse_ident(data)

    # ----- Befehl 50: Arbeitsdaten ----------------------------------------------------------
    def read_work_data(self, checksum: ChecksumMode = "auto") -> Lc1WorkData:
        with self._lock:
            return self._with_retry("Arbeitsdaten", lambda: self._work_once(checksum))

    def _numeric_line(self, timeout: float, what: str) -> str:
        """Nächste nicht leere Zeile (leere Zeilen werden übersprungen)."""
        deadline = self._clock() + timeout
        while True:
            line = self._read_line(max(0.01, deadline - self._clock()), what)
            if line.strip():
                return line

    def _work_once(self, mode: ChecksumMode) -> Lc1WorkData:
        self._begin(CMD_ARBEITSDATEN)
        lines: list[str] = []
        for i in range(N_WORK_LINES):
            what = f"K({i + 1})" if i < N_K else ("Port 4", "IC8", "IC9")[i - N_K]
            lines.append(self._numeric_line(self.line_timeout_s, what))
        cs_line: str | None = None
        if mode == "on" or (mode == "auto" and self.checksum_seen):
            cs_line = self._numeric_line(self.line_timeout_s, "Prüfsumme K(36)")
        elif mode == "auto":
            try:
                cs_line = self._numeric_line(AUTO_CHECKSUM_WAIT_S, "Prüfsumme K(36)")
            except DeviceTimeout:
                cs_line = None
            if cs_line is not None and self.checksum_seen is None:
                log.info("LC1 %s: Prüfsumme K36 erkannt (Firmware ab LCS9)", self.name)
            if cs_line is None and self.checksum_seen is None:
                log.info("LC1 %s: keine Prüfsumme K36 (ältere Firmware?)", self.name)
            self.checksum_seen = cs_line is not None
        else:  # off: eine eventuell folgende Prüfsumme abwarten und verwerfen
            extra = self._read_until_idle(0.2, 0.5)
            if extra.strip():
                log.debug("LC1 %s: checksum=off, %d Byte nach IC9 verworfen", self.name, len(extra))
        return self._parse_work(lines, cs_line)

    def _parse_work(self, lines: list[str], cs_line: str | None) -> Lc1WorkData:
        nums = [parse_number(x) for x in lines]
        values = {k: nums[k - 1] for k in range(1, N_K + 1)}
        bad = [k for k, v in values.items() if v is None]

        def byte_val(v: float | None, what: str) -> int:
            if v is None or not 0 <= round(v) <= 255 or abs(v - round(v)) > 0.01:
                raise ProtocolError(f"LC1: {what} ungültig: {v!r}")
            return int(round(v))

        wd = Lc1WorkData(values=values, lines=list(lines) + ([cs_line] if cs_line is not None else []))
        wd.port4 = byte_val(nums[N_K], "Port 4")
        wd.ic8 = byte_val(nums[N_K + 1], "IC8")
        wd.ic9 = byte_val(nums[N_K + 2], "IC9")
        if cs_line is not None:
            cs = parse_number(cs_line)
            if cs is None or bad:
                self.counters["checksum_errors"] += 1
                raise ProtocolError(f"LC1: Prüfsumme nicht prüfbar (K36={cs_line!r}, unlesbar: {bad})")
            ok, variant = checksum_check([v for v in values.values() if v is not None],
                                         wd.port4, wd.ic8, wd.ic9, cs)
            wd.checksum, wd.checksum_ok, wd.checksum_variant = cs, ok, variant
            if not ok:
                self.counters["checksum_errors"] += 1
                raise ProtocolError(f"LC1: Prüfsummenfehler Arbeitsdaten: {variant}")
            if variant in ("INT=floor", "Rundungstoleranz"):
                log.info("LC1 %s: Prüfsumme passt nur mit %s", self.name, variant)
        elif bad:
            log.warning("LC1 %s: Messwerte unlesbar: %s", self.name, bad)
        return wd

    # ----- Befehl 48: Parameter lesen ---------------------------------------------------------
    def read_params(self, addresses: list[int]) -> dict[int, int]:
        addrs = list(dict.fromkeys(addresses))      # Reihenfolge behalten, doppelte entfernen
        for a in addrs:
            if isinstance(a, bool) or not isinstance(a, int) or not 0 <= a <= safety.LC1_E_MAX:
                raise SafetyViolation(f"LC1: Parameteradresse {a!r} außerhalb 0..{safety.LC1_E_MAX}")
        result: dict[int, int] = {}
        if not addrs:
            return result
        with self._lock:
            def once():
                todo = [a for a in addrs if a not in result]
                if todo:
                    self._params_once(todo, result)
                return result
            return self._with_retry("Parameter lesen", once)

    def _params_once(self, addrs: list[int], result: dict[int, int]) -> None:
        self._begin(CMD_PARAM_LESEN)
        for a in addrs:
            b = 48 + a
            self._send_byte(b, continuation=True)            # ohne Echo (LCS9.ARB:739, 744)
            line = self._read_line(self.line_timeout_s, f"E({a})")
            result[a] = self._parse_param_line(line, a)
        # Ende: Byte < 48 ohne Echo (LCS9.ARB:744). CR ist auch als Funktionsbyte harmlos
        # (LCS9.ARB:908 -> KOM-Störung, Abbruch), daher ohne Zeitprüfung.
        self._send_byte(CR, continuation=False)

    def _parse_param_line(self, line: str, a: int) -> int:
        tokens = line.split()
        if len(tokens) == 2 and tokens[0] == chr(48 + a):
            log.info("LC1 %s: Adressbyte E(%d) wurde geechot – toleriert", self.name, a)
            tokens = tokens[1:]
        v = parse_number(tokens[0]) if len(tokens) == 1 else None
        if v is None or abs(v - round(v)) > 1e-6 or not 0 <= round(v) <= 0xFFFF:
            raise ProtocolError(f"LC1: ungültige Antwort für E({a}): {line!r}")
        return int(round(v))

    # ----- Befehl 49: Parameter schreiben -----------------------------------------------------
    def write_param(self, address: int, raw: int) -> None:
        """Schreibt EIN Wort. Keine Wiederholung. Wirft ``SafetyViolation`` vor jedem Senden,
        ``Lc1WriteUnclear`` (DeviceTimeout), wenn nach dem CR keine Bestätigung kam."""
        safety.check_lc1_param_write(address, raw)
        digits = f"{raw:05d}"
        if len(digits) != 5 or not digits.isdigit():      # pragma: no cover - durch safety gedeckt
            raise SafetyViolation(f"LC1: Wert {raw!r} nicht als 5 Ziffern darstellbar")
        with self._lock:
            exec_sent = False
            try:
                self._begin(CMD_PARAM_SCHREIBEN)
                ab = 48 + address
                self._send_byte(ab, continuation=True)
                self._expect_echo(ab, self.byte_timeout_s, f"Adresse E({address})")
                for d in digits.encode("ascii"):
                    self._send_byte(d, continuation=True)
                    self._expect_echo(d, self.byte_timeout_s, f"Ziffer {chr(d)}")
                # Ab hier programmiert der Regler das EEPROM (LCS9.ARB:932-938).
                self._send_byte(CR, continuation=True)
                exec_sent = True
                got = self._read_byte(self.line_timeout_s + self.byte_timeout_s)
                if got is None:
                    raise Lc1WriteUnclear(f"LC1: keine Bestätigung (CHR 11) nach Schreiben E({address})")
                self._trace("RX", fmt_bytes(bytes([got])))
                if got != EXEC_OK:
                    raise Lc1WriteUnclear(f"LC1: Bestätigung {got:#04x} statt 0x0B nach Schreiben E({address})")
                # Ende senden (mit Echo, LCS9.ARB:940-944). Fehlt das Echo, ist der Wert trotzdem
                # geschrieben; der Treiber liest ihn zurück.
                try:
                    self._send_byte(EXEC_OK, continuation=True)
                    self._expect_echo(EXEC_OK, self.byte_timeout_s, "Ende (11)")
                except (DeviceTimeout, ProtocolError) as exc:
                    log.warning("LC1 %s: Ende nach Schreiben E(%d) nicht bestätigt: %s", self.name, address, exc)
                    self._failed(exc)
            except SafetyViolation:
                raise
            except Lc1WriteUnclear as exc:
                self._failed(exc)
                raise
            except (DeviceTimeout, ProtocolError) as exc:
                self._failed(exc)
                if exec_sent:     # pragma: no cover - nach dem CR gibt es nur Lc1WriteUnclear
                    raise Lc1WriteUnclear(str(exc)) from exc
                # Vor dem CR abgebrochen: der Regler schreibt nichts (LCS9.ARB:932 verlangt CR).
                raise type(exc)(f"{exc} – Schreiben abgebrochen, nichts geschrieben") from exc


class Lc1WriteUnclear(DeviceTimeout):
    """Schreibvorgang wurde bis zum Ausführungsbyte gesendet, Bestätigung fehlt/falsch.
    Ergebnis unklar -> zählt für Ratenlimit/Budget, Alarm (service behandelt DeviceTimeout so)."""
