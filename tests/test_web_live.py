"""Web-Tests gegen einen echten Uvicorn (serve_web) mit urllib - laufen auch ohne httpx."""

import asyncio
import json
import socket
import urllib.error
import urllib.request

from gateway.web.app import create_app
from gateway.web.server import effective_bind, serve_web, stop_web
from test_web_fake import FakeService


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def req(base, method, path, body=None, headers=None, cookie=None):
    h = {"X-Requested-With": "gateway", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    if cookie:
        h["Cookie"] = cookie
    r = urllib.request.Request(base + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def jreq(*a, **k):
    st, raw, hd = req(*a, **k)
    return st, json.loads(raw), hd


def test_effective_bind_folgt_konfiguration():
    """Seit 0.1.6 auch unter Windows: web.bind gilt (Freigabe im Netz per Installer-Aufgabe)."""
    svc = FakeService()
    assert effective_bind(svc.cfg) == "127.0.0.1"
    svc.cfg.web.bind = "0.0.0.0"
    assert effective_bind(svc.cfg) == "0.0.0.0"


def test_live_server_flow():
    async def main():
        svc = FakeService()
        svc.cfg.web.port = free_port()
        server, task = await serve_web(create_app(svc), svc.cfg)
        base = f"http://127.0.0.1:{svc.cfg.web.port}"
        call = lambda *a, **k: asyncio.to_thread(jreq, base, *a, **k)  # noqa: E731
        try:
            assert (await call("GET", "/api/status"))[1]["running"]
            st, raw, hd = await asyncio.to_thread(req, base, "GET", "/")
            assert st == 200 and b"Logotherm" in raw
            assert (await asyncio.to_thread(req, base, "GET", "/static/app.js"))[0] == 200
            # CSRF
            st, _, _ = await asyncio.to_thread(req, base, "POST", "/api/draft/discard", None, {"X-Requested-With": "x"})
            assert st == 403
            # Scan -> übernehmen -> Aufzählung -> Werte -> speichern
            job = (await call("POST", "/api/scan", {"can": True}))[1]
            info = job["result"][0]
            assert (await call("POST", "/api/draft/devices", info))[0] == 200
            await call("POST", "/api/draft/devices/can_11/enumerate")
            rows = (await call("GET", "/api/draft/devices/can_11/values"))[1]["rows"]
            assert any(r["kind"] == "param" and r["lock_reason"] for r in rows)
            st, body, _ = await call("PATCH", "/api/draft/devices/can_11", {"unit_id": 0})
            assert st == 400
            await call("PATCH", "/api/draft/devices/can_11", {"unit_id": 17})
            st, body, _ = await call("PATCH", "/api/draft/settings/modbus", {"port": 0})
            assert st == 400 and body["detail"]
            assert (await call("POST", "/api/draft/apply"))[1]["ok"]
            st, raw, hd = await asyncio.to_thread(req, base, "GET", "/api/registers.csv")
            assert st == 200 and "registerliste.csv" in hd["Content-Disposition"]
            # Passwort
            assert (await call("POST", "/api/password", {"password": "geheim123"}))[0] == 200
            assert (await call("GET", "/api/config"))[0] == 401
            assert set((await call("GET", "/api/status"))[1]) == {"running", "version", "online", "devices"}
            st, _, hd = await call("POST", "/api/login", {"password": "geheim123"})
            assert st == 200
            cookie = hd["Set-Cookie"].split(";")[0]
            assert (await call("GET", "/api/config", cookie=cookie))[0] == 200
        finally:
            await stop_web(server, task)
        assert task.done()

    asyncio.run(main())
