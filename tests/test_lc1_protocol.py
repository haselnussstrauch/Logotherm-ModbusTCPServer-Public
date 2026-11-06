"""Tests für das LC1-Echo-Protokoll (gateway.serial_lc1.protocol) gegen den Simulator."""

from __future__ import annotations

import ast
import math
import time
from pathlib import Path

import pytest

from gateway import safety
from gateway.drivers import DeviceTimeout, ProtocolError
from gateway.errors import SafetyViolation
from gateway.serial_lc1 import protocol as P
from gateway.serial_lc1.protocol import Lc1Protocol, Lc1WriteUnclear, checksum_check, parse_ident, parse_number
from gateway.serial_lc1.simulator import Lc1Simulator, basic_using

BT, LT = 0.3, 0.6      # kurze Timeouts für Tests


def proto(sim, retries=1, trace=None) -> Lc1Protocol:
    return Lc1Protocol(sim, byte_timeout_s=BT, line_timeout_s=LT, retries=retries, trace=trace)


def assert_nothing_forbidden(sim: Lc1Simulator) -> None:
    assert not set(sim.sent) & safety.FORBIDDEN_LC1_BYTES
    assert sim.forbidden_received == []
    assert 51 not in sim.commands and 52 not in sim.commands


# --- Echo-Handshake ------------------------------------------------------------------------------
def test_ident_echo_and_trace():
    sim = Lc1Simulator(version=9.3, system_nr=2, kessel_name="Holzkessel Süd")
    trace: list[tuple[str, str]] = []
    ident = proto(sim, trace=lambda d, t: trace.append((d, t))).ident()
    assert ident.version == pytest.approx(9.3)
    assert ident.system_nr == 2
    assert ident.name == "Holzkessel Süd"
    assert sim.sent == [53]
    assert ("TX", "35 |5|") in trace and ("ECHO", "35 |5|") in trace
    assert any(d == "RX" and "0D 0A" in t for d, t in trace)
    # 32 Byte wie Module1.bas:188 (Echo + CRLF + 2×7 + 15)
    assert len(ident.raw) == 31


def test_work_data_with_checksum():
    sim = Lc1Simulator()
    wd = proto(sim).read_work_data("on")
    assert len(wd.values) == 35
    assert wd.values[2] == pytest.approx(78.4)
    assert wd.values[16] == pytest.approx(-0.25)
    assert (wd.port4, wd.ic8, wd.ic9) == (sim.port4, sim.ic8, sim.ic9)
    assert wd.checksum_ok is True
    assert sim.sent == [50]


def test_read_params_no_echo_and_end_with_cr():
    sim = Lc1Simulator()
    res = proto(sim).read_params([0, 20, 44, 63])
    assert res == {0: sim.params[0], 20: sim.params[20], 44: sim.params[44], 63: sim.params[63]}
    assert sim.sent == [48, 48, 68, 92, 111, 13]


def test_write_param_sequence_and_readback():
    sim = Lc1Simulator()
    p = proto(sim)
    p.write_param(44, 82)
    assert sim.sent == [49, 48 + 44, *b"00082", 13, 11]
    assert sim.ee_writes == [(44, 82)]
    assert p.read_params([44]) == {44: 82}


def test_wrong_echo_raises_without_retry():
    sim = Lc1Simulator()
    sim.wrong_echo_next = True
    with pytest.raises(ProtocolError, match="falsches Echo"):
        proto(sim, retries=0).ident()


def test_wrong_echo_retry_recovers():
    sim = Lc1Simulator()
    sim.wrong_echo_next = True
    p = proto(sim, retries=1)
    t0 = time.monotonic()
    ident = p.ident()
    assert ident.name
    assert p.counters["retries"] == 1
    assert time.monotonic() - t0 >= p.quiet_s * 0.9      # Ruhezeit nach dem Fehler eingehalten


def test_timeout_no_answer():
    sim = Lc1Simulator()
    sim.silent = True
    p = proto(sim, retries=0)
    t0 = time.monotonic()
    with pytest.raises(DeviceTimeout, match="kein Echo"):
        p.read_work_data("auto")
    assert time.monotonic() - t0 == pytest.approx(LT, abs=0.3)
    assert p.counters["timeouts"] == 1


def test_lost_byte_in_write_aborts_without_writing_and_without_retry():
    sim = Lc1Simulator()
    p = proto(sim, retries=3)
    sim.sent.clear()
    # erstes Byte (49) kommt an, das Adressbyte geht verloren -> kein Echo
    orig = sim._on_byte

    def on_byte(b, _n=[0]):
        _n[0] += 1
        if _n[0] == 2:
            return
        orig(b)
    sim._on_byte = on_byte
    with pytest.raises(DeviceTimeout, match="nichts geschrieben"):
        p.write_param(44, 75)
    assert sim.ee_writes == []
    assert sim.sent.count(49) == 1                     # keine Wiederholung beim Schreiben
    assert 13 not in sim.sent                          # Ausführungsbyte nie gesendet


def test_write_without_ack_is_unclear():
    sim = Lc1Simulator()
    sim.no_exec_ack = True
    with pytest.raises(Lc1WriteUnclear):
        proto(sim).write_param(44, 75)
    assert isinstance(Lc1WriteUnclear("x"), DeviceTimeout)
    assert sim.ee_writes == [(44, 75)]


def test_lost_echo_is_detected():
    sim = Lc1Simulator()
    sim.lose_next_tx = True              # erstes Ausgabebyte = Echo '2' geht verloren
    with pytest.raises((DeviceTimeout, ProtocolError)):
        proto(sim, retries=0).read_work_data("on")


@pytest.mark.parametrize("index", [3, 13, 60, 200, 400])
def test_lost_byte_in_work_data_never_gives_wrong_values(index):
    """Ein verlorenes Byte mitten in den Arbeitsdaten führt mit Prüfsumme zu einem Fehler –
    oder (Leerzeichen verloren) zu korrekten Werten, nie zu falschen."""
    sim = Lc1Simulator()
    sim.lose_tx_after = index
    try:
        wd = proto(sim, retries=0).read_work_data("on")
    except (DeviceTimeout, ProtocolError):
        return
    for k, v in sim.values.items():
        assert wd.values[k] == pytest.approx(v, abs=0.006)


# --- Sicherheit -----------------------------------------------------------------------------------
def test_forbidden_bytes_and_commands_never_sent_full_session():
    sim = Lc1Simulator()
    p = proto(sim)
    p.ident()
    p.read_work_data("auto")
    p.read_params(list(range(64)))
    for e in range(64):
        if safety.param_lock_reason("lc1", None, e) is None:
            p.write_param(e, sim.params[e])
    assert_nothing_forbidden(sim)
    assert set(sim.commands) <= {48, 49, 50, 53}


@pytest.mark.parametrize("func", [51, 52, 54, 3, 0x1B])
def test_begin_rejects_forbidden_commands(func):
    sim = Lc1Simulator()
    with pytest.raises(SafetyViolation):
        proto(sim)._begin(func)
    assert sim.sent == []


@pytest.mark.parametrize("b", sorted(safety.FORBIDDEN_LC1_BYTES))
def test_send_byte_rejects_control_chars(b):
    sim = Lc1Simulator()
    with pytest.raises(SafetyViolation):
        proto(sim)._send_byte(b, continuation=False)
    assert sim.sent == []


@pytest.mark.parametrize("e", sorted(safety.LC1_LOCKED))
def test_locked_params_raise_before_sending(e):
    sim = Lc1Simulator()
    with pytest.raises(SafetyViolation, match="gesperrt"):
        proto(sim).write_param(e, 1)
    assert sim.sent == []


@pytest.mark.parametrize("addr,value", [(44, 65536), (44, -1), (64, 1), (-1, 1), (44, 1.5), (True, 1)])
def test_invalid_write_rejected(addr, value):
    sim = Lc1Simulator()
    with pytest.raises(SafetyViolation):
        proto(sim).write_param(addr, value)
    assert sim.sent == []


@pytest.mark.parametrize("addr", [64, -1, 100, 1.0, None])
def test_invalid_read_address_rejected(addr):
    sim = Lc1Simulator()
    with pytest.raises(SafetyViolation):
        proto(sim).read_params([20, addr])
    assert sim.sent == []


def test_continuation_guard_blocks_late_byte():
    """Nach > byte_timeout/2 ohne Antwort darf kein Folgebyte mehr raus (Regler-Timeout droht,
    das Byte würde sonst als Funktionsbyte gedeutet – z. B. 51 für E3!)."""
    sim = Lc1Simulator()
    now = [100.0]
    p = Lc1Protocol(sim, byte_timeout_s=2.0, line_timeout_s=3.0, clock=lambda: now[0])
    p._last_rx = 100.0
    now[0] = 101.2
    with pytest.raises(ProtocolError, match="NICHT gesendet"):
        p._send_byte(48 + 3, continuation=True)
    assert sim.sent == []


def test_simulator_reinterprets_late_byte_as_function_byte():
    """Plausibilitätstest des Simulators: so gefährlich ist ein Timeout mitten im Lesebefehl."""
    sim = Lc1Simulator(byte_timeout_s=0.05)
    sim.write(bytes([48]))
    time.sleep(0.1)
    sim.write(bytes([48 + 3]))            # Adresse E3 nach Timeout -> Funktionsbyte 51
    assert sim.forbidden_received == [51]


def test_only_send_byte_writes_to_port():
    """Statische Prüfung: in protocol.py und driver.py ruft nur _send_byte port.write auf."""
    base = Path(P.__file__).parent
    for fname in ("protocol.py", "driver.py", "selftest.py"):
        tree = ast.parse((base / fname).read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr in ("write", "writelines", "send", "sendall"):
                    assert fname == "protocol.py" and fn.name == "_send_byte", \
                        f"{fname}:{node.lineno} {fn.name} schreibt direkt auf den Port"


def test_only_send_byte_writes_at_runtime():
    import inspect

    sim = Lc1Simulator()
    orig = sim.write

    def checked(data):
        assert inspect.stack()[1].function == "_send_byte"
        assert len(data) == 1
        return orig(data)
    sim.write = checked
    p = proto(sim)
    p.ident()
    p.read_work_data("auto")
    p.read_params([1, 2, 3])
    p.write_param(44, 80)


# --- Parser ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("   12.50 ", 12.5), ("-  3.25", -3.25), ("-     0.25 ", -0.25), ("+ 4", 4.0), ("  -3.25 ", -3.25), ("1,5", 1.5), ("", None),
    ("   ", None), ("abc", None), ("  65535 ", 65535.0), ("x 7.5 y", 7.5), ("nan", None),
])
def test_parse_number(text, expected):
    v = parse_number(text)
    assert (v is None and expected is None) or v == pytest.approx(expected)


@pytest.mark.parametrize("raw,version,system,name", [
    (b"\r\n   9.1    1.0 Holzkessel S\xf5d ", 9.1, 1, "Holzkessel Süd"),            # LCS9 ###.#
    (b"\r\n   1.12    3.00 HDG Navora 25  ", 1.12, 3, "HDG Navora 25"),            # LCS1 ##.##
    (b"\n 9.3 2 Kessel         \r\n", 9.3, 2, "Kessel"),                          # mit CRLF am Ende
    (b"9.3,2,ABCDEFGHIJKLMNO", 9.3, 2, "ABCDEFGHIJKLMNO"),
])
def test_parse_ident_variants(raw, version, system, name):
    ident = parse_ident(raw)
    assert ident.version == pytest.approx(version)
    assert ident.system_nr == system
    assert ident.name == name


def test_parse_ident_too_short():
    with pytest.raises(ProtocolError):
        parse_ident(b"\r\n 9.1 ")


def test_parse_ident_tolerates_missing_numbers():
    ident = parse_ident(b"\r\nKesselname 1234")
    assert ident.name == "Kesselname 1234" and ident.version is None


@pytest.mark.parametrize("ending", [b"\r\n", b"\n", b"\r", b"\n\r"])
def test_line_endings(ending):
    sim = Lc1Simulator()
    sim.line_ending = ending
    p = proto(sim)
    wd = p.read_work_data("on")
    assert wd.checksum_ok and wd.values[3] == pytest.approx(182.6)
    assert p.read_params([44, 45]) == {44: sim.params[44], 45: sim.params[45]}


def test_extra_blank_lines_tolerated():
    sim = Lc1Simulator()
    sim.extra_blank_lines = True
    wd = proto(sim).read_work_data("on")
    assert wd.checksum_ok and wd.values[35] == sim.values[35]


def test_param_line_with_echoed_address_tolerated():
    p = proto(Lc1Simulator())
    assert p._parse_param_line("4  1234", 4) == 1234
    assert p._parse_param_line("  1234 ", 4) == 1234
    with pytest.raises(ProtocolError):
        p._parse_param_line("  12 34 ", 4)
    with pytest.raises(ProtocolError):
        p._parse_param_line(" 70000", 4)
    with pytest.raises(ProtocolError):
        p._parse_param_line(" 12.5", 4)


def test_basic_using_format():
    assert basic_using(78.4, "######.##") == "     78.40 "
    assert basic_using(-0.25, "######.##") == "-     0.25 "
    assert basic_using(123, "#####") == "   123 "


# --- Prüfsumme ------------------------------------------------------------------------------------
@pytest.mark.parametrize("int_mode", ["trunc", "floor"])
def test_checksum_negative_values_both_int_variants(int_mode):
    sim = Lc1Simulator(int_mode=int_mode)
    sim.values.update({16: -2.75, 19: -0.5, 32: -12.25})
    wd = proto(sim).read_work_data("on")
    assert wd.checksum_ok
    assert wd.checksum_variant in (("INT=trunc", "exakt") if int_mode == "trunc" else ("INT=floor",))


def test_checksum_corrupt_raises():
    sim = Lc1Simulator()
    sim.corrupt_checksum = True
    p = proto(sim, retries=0)
    with pytest.raises(ProtocolError, match="Prüfsumme"):
        p.read_work_data("on")
    assert p.counters["checksum_errors"] == 1


def test_checksum_off_ignores_checksum_line():
    sim = Lc1Simulator()
    sim.corrupt_checksum = True
    wd = proto(sim).read_work_data("off")
    assert wd.checksum_ok is None and wd.values[2] == pytest.approx(78.4)


def test_checksum_on_but_missing_times_out():
    sim = Lc1Simulator(checksum=False)
    with pytest.raises(DeviceTimeout):
        proto(sim, retries=0).read_work_data("on")


def test_checksum_auto_detects_both():
    p = proto(Lc1Simulator(checksum=False))
    assert p.read_work_data("auto").checksum_ok is None
    assert p.checksum_seen is False
    p2 = proto(Lc1Simulator(checksum=True))
    assert p2.read_work_data("auto").checksum_ok is True
    assert p2.checksum_seen is True
    # danach ist die Prüfsumme Pflicht
    p2.port.checksum = False
    with pytest.raises(DeviceTimeout):
        p2.read_work_data("auto")


def test_checksum_check_rounding_tolerance():
    # intern 12.996 -> gesendet 13.00, INT = 12
    vals = [13.0, 5.5]
    assert checksum_check(vals, 1, 2, 3, 6 + 12 + 5) == (True, "Rundungstoleranz")
    assert checksum_check(vals, 1, 2, 3, 6 + 13 + 5)[0]
    assert not checksum_check(vals, 1, 2, 3, 6 + 13 + 5 + 2)[0]
    assert not checksum_check(vals, 1, 2, 3, 24.5)[0]
    assert math.isfinite(sum(vals))


def test_work_data_byte_fields_validated():
    sim = Lc1Simulator(checksum=False)
    sim.port4 = 300
    with pytest.raises(ProtocolError, match="Port 4"):
        proto(sim, retries=0).read_work_data("off")
