"""Tests der Launcher-Statuslogik (ohne Tk-Fenster) gegen einen lokalen http.server."""

import gc
import importlib.util
import sys
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "launcher" / "launcher.py"
_spec = importlib.util.spec_from_file_location("logotherm_launcher", _PATH)
launcher = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = launcher
_spec.loader.exec_module(launcher)


def _serve(payload=None, code=200, raw: bytes | None = None):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = raw if raw is not None else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def serve():
    servers = []

    def make(*a, **kw):
        s = _serve(*a, **kw)
        servers.append(s)
        return s.server_address[1]

    yield make
    for s in servers:
        s.shutdown()
        s.server_close()


def test_status_running_voll(serve):
    port = serve({"running": True, "version": "0.1.0", "warnings": ["Allowlist leer"],
                  "devices": [{"id": "a", "online": True}, {"id": "b", "online": False},
                              {"id": "c", "online": True}]})
    r = launcher.fetch_status(port)
    assert r.state == launcher.RUNNING
    assert r.version == "0.1.0"
    assert (r.devices_online, r.devices_total) == (2, 3)
    assert r.devices_text == "2 von 3 online"
    assert r.warnings == ["Allowlist leer"]
    assert r.headline == "Dienst läuft"


def test_status_reduziert_bei_passwortschutz(serve):
    # Genau das Format, das gateway/web/app.py ohne Anmeldung liefert.
    port = serve({"running": True, "version": "0.1.0", "online": 4, "devices": 6})
    r = launcher.fetch_status(port)
    assert r.state == launcher.RUNNING
    assert (r.devices_online, r.devices_total) == (4, 6)
    assert r.devices_text == "4 von 6 online"


def test_status_reduziert_nur_online(serve):
    port = serve({"running": True, "version": "0.1.0", "devices_online": 4})
    r = launcher.fetch_status(port)
    assert r.state == launcher.RUNNING
    assert r.devices_online == 4 and r.devices_total is None
    assert r.devices_text == "4 online"


def test_status_401_ist_laufend(serve):
    r = launcher.fetch_status(serve({"detail": "nope"}, code=401))
    assert r.state == launcher.RUNNING
    assert r.devices_text == "–"


def test_status_kein_json(serve):
    assert launcher.fetch_status(serve(raw=b"<html>")).state == launcher.UNREACHABLE


def test_status_server_fehler(serve):
    assert launcher.fetch_status(serve({}, code=500)).state == launcher.UNREACHABLE


def test_status_nicht_erreichbar():
    # Port, auf dem sicher niemand lauscht: kurz binden, schließen, abfragen.
    srv = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = srv.server_address[1]
    srv.server_close()
    r = launcher.fetch_status(port, timeout=1)
    assert r.state == launcher.UNREACHABLE
    assert r.headline == "Dienst nicht erreichbar"
    assert r.detail


def test_running_false():
    assert launcher.parse_status({"running": False}).state == launcher.UNREACHABLE


def test_parse_status_unsinn():
    assert launcher.parse_status([1, 2]).state == launcher.UNREACHABLE


@pytest.mark.parametrize("text,expected", [
    ("web:\n  bind: 127.0.0.1\n  port: 9090\n", 9090),
    ("modbus:\n  port: 502\nweb:\n  port: 8181  # Kommentar\n", 8181),
    ("modbus:\n  port: 502\n", 8080),
    ("web:\n  port: 99999\n", 8080),
    ("{{{ kaputt", 8080),
])
def test_read_web_port(tmp_path, text, expected):
    p = tmp_path / "gateway.yaml"
    p.write_text(text, encoding="utf-8")
    assert launcher.read_web_port(p) == expected


def test_read_web_port_textfallback():
    # Ohne PyYAML (Launcher-Exe) greift die einfache Textsuche.
    assert launcher._port_from_text("modbus:\n  port: 502\nweb:\n  bind: x\n  port: 9191\n") == 9191
    assert launcher._port_from_text("modbus:\n  port: 502\n") is None


def test_read_web_port_datei_fehlt(tmp_path):
    assert launcher.read_web_port(tmp_path / "gibt-es-nicht.yaml") == 8080


def test_data_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GATEWAY_DATA_DIR", str(tmp_path))
    assert launcher.config_path() == tmp_path / "gateway.yaml"
    assert launcher.log_path() == tmp_path / "logs" / "gateway.log"


def test_fenster_baut_sich_auf():
    tk = pytest.importorskip("tkinter")
    try:
        win = launcher.LauncherWindow(check_updates=False)
    except tk.TclError:
        pytest.skip("kein Display verfügbar")
    try:
        win.root.update()
        assert win.headline.get()
    finally:
        win._on_close()
        # Tk-Variablen hier im Hauptthread freigeben: Hält der Status-Thread die letzte Referenz,
        # laufen die __del__ in seinem Thread („main thread is not in main loop“).
        win._poller.join(timeout=10)
        del win
        gc.collect()


def test_driver_script_pfad(tmp_path):
    exe = tmp_path / "app" / "launcher" / "LogothermGateway.exe"
    assert launcher.driver_script(exe) == (tmp_path / "app" / "driver" / "install-winusb.cmd").resolve()


def test_install_driver_ohne_skript(tmp_path):
    assert launcher.install_driver(tmp_path / "fehlt.cmd") is False
