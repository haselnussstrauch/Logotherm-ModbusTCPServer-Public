"""FastAPI-Anwendung: REST-API unter /api und statische Oberfläche."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import iterate_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from gateway.config import NUMERIC_VARTYPES
from gateway.devices.names import display_name
from gateway.drivers import DeviceInfo
from gateway.errors import WriteRejected
from gateway.history import check_day
from gateway.registermap import export_csv

STATIC = Path(__file__).parent / "static"
COOKIE = "gw_session"
SESSION_TTL_S = 12 * 3600
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "gateway"
LOCAL_BINDS = {"127.0.0.1", "localhost", "::1"}
MIN_PASSWORD_LEN = 6
#: Windows: erstes Passwort nur vom Gateway-PC selbst (sonst könnte es jeder im Netz zuerst setzen)
LOCAL_SETUP_ONLY = sys.platform == "win32"


def jsonable(o: Any) -> Any:
    """Dataclasses, pydantic-Modelle, Tuples usw. in JSON-fähige Strukturen wandeln."""
    if o is None or isinstance(o, (bool, int, float, str)):
        return o
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return {k: jsonable(v) for k, v in dataclasses.asdict(o).items()}
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    if isinstance(o, dict):
        return {str(k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in o]
    return str(o)


def job_dict(job) -> dict:
    done, total, text = job.progress
    return {"id": job.id, "kind": job.kind, "state": job.state,
            "progress": {"done": done, "total": total, "text": text},
            "result": jsonable(job.result), "error": job.error}


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(body, dict):
        raise ValueError("JSON-Objekt erwartet")
    return body


def _lock_reason(dev, key: int) -> str | None:
    try:
        from gateway import safety
        return safety.param_lock_reason(dev.bus, dev.regler_typ, key)
    except Exception:  # noqa: BLE001
        return None


def create_app(service) -> FastAPI:
    app = FastAPI(title="Logotherm Gateway", docs_url=None, redoc_url=None, openapi_url=None)
    secret = secrets.token_bytes(32)
    sessions: dict[str, float] = {}   # Token -> Ablaufzeit
    fail = {"count": 0, "until": 0.0}

    # --- Sitzung / Auth ---------------------------------------------------------------------
    def password_set() -> bool:
        return bool(service.cfg.web.password_hash)

    def is_local() -> bool:
        from gateway.web.server import effective_bind
        return effective_bind(service.cfg) in LOCAL_BINDS

    def setup_needed() -> bool:
        return not password_set() and not is_local()

    def client_local(request: Request) -> bool:
        host = request.client.host if request.client else ""
        return host in ("127.0.0.1", "::1", "localhost")

    def local_without_password(request: Request) -> bool:
        """Windows: am Gateway-PC selbst (Launcher) geht es ohne Passwort, auch wenn die
        Oberfläche zusätzlich im Netz erreichbar ist. Aus dem Netz erst mit Passwort."""
        return LOCAL_SETUP_ONLY and client_local(request)

    def sign(token: str) -> str:
        return hmac.new(secret, token.encode(), hashlib.sha256).hexdigest()

    def new_session(resp: Response) -> None:
        token = secrets.token_urlsafe(24)
        sessions[token] = time.time() + SESSION_TTL_S
        resp.set_cookie(COOKIE, f"{token}.{sign(token)}", max_age=SESSION_TTL_S, httponly=True,
                        samesite="strict", path="/")

    def logged_in(request: Request) -> bool:
        raw = request.cookies.get(COOKIE, "")
        token, _, sig = raw.partition(".")
        if not token or not hmac.compare_digest(sig, sign(token)):
            return False
        exp = sessions.get(token)
        if exp is None or exp < time.time():
            sessions.pop(token, None)
            return False
        return True

    def authorized(request: Request) -> bool:
        if password_set():
            return logged_in(request)
        return not setup_needed() or local_without_password(request)

    open_api = {"/api/status", "/api/auth/state", "/api/login", "/api/setup-password"}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and \
                request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            return JSONResponse({"detail": "Header X-Requested-With fehlt"}, status_code=403)
        if path.startswith("/api/") and path not in open_api and not authorized(request):
            if setup_needed():
                return JSONResponse({"detail": "Bitte zuerst ein Passwort festlegen"}, status_code=403)
            return JSONResponse({"detail": "Anmeldung erforderlich"}, status_code=401)
        try:
            return await call_next(request)
        except ValueError as exc:   # Validierungsfehler des Dienstes -> 400 mit Klartext
            return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/api/auth/state")
    async def auth_state(request: Request):
        return {"password_set": password_set(), "logged_in": logged_in(request),
                "setup_needed": setup_needed(), "authorized": authorized(request),
                "setup_local_only": LOCAL_SETUP_ONLY and setup_needed() and not client_local(request)}

    @app.post("/api/login")
    async def login(request: Request):
        body = await _body(request)
        if not password_set():
            raise HTTPException(400, "Es ist kein Passwort gesetzt")
        if time.time() < fail["until"]:
            raise HTTPException(429, "Zu viele Fehlversuche, bitte kurz warten")
        if not service.check_password(str(body.get("password", ""))):
            fail["count"] += 1
            if fail["count"] >= 3:
                fail["until"] = time.time() + min(30, 2 ** (fail["count"] - 2))
            raise HTTPException(401, "Passwort falsch")
        fail["count"] = 0
        resp = JSONResponse({"ok": True})
        new_session(resp)
        return resp

    @app.post("/api/logout")
    async def logout(request: Request):
        token = request.cookies.get(COOKIE, "").partition(".")[0]
        sessions.pop(token, None)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.post("/api/setup-password")
    async def setup_password(request: Request):
        """Erster Start (Docker/offener Bind): Passwort festlegen, solange noch keines existiert."""
        body = await _body(request)
        if password_set():
            raise HTTPException(403, "Passwort ist bereits gesetzt")
        if LOCAL_SETUP_ONLY and not client_local(request):
            raise HTTPException(403, "Das erste Passwort bitte am Gateway-PC selbst festlegen "
                                     "(dort http://127.0.0.1:" f"{service.cfg.web.port} öffnen)")
        pw = str(body.get("password", ""))
        if len(pw) < MIN_PASSWORD_LEN:
            raise ValueError(f"Das Passwort braucht mindestens {MIN_PASSWORD_LEN} Zeichen")
        service.set_password(pw)
        resp = JSONResponse({"ok": True})
        new_session(resp)
        return resp

    @app.post("/api/password")
    async def change_password(request: Request):
        body = await _body(request)
        pw = str(body.get("password", ""))
        if len(pw) < MIN_PASSWORD_LEN:
            raise ValueError(f"Das Passwort braucht mindestens {MIN_PASSWORD_LEN} Zeichen")
        service.set_password(pw)
        return {"ok": True}

    # --- Status / Protokoll -----------------------------------------------------------------
    @app.get("/api/status")
    async def status(request: Request):
        st = service.status()
        if (password_set() or setup_needed()) and not authorized(request):
            devs = st.get("devices", [])
            return {"running": st.get("running", True), "version": st.get("version", ""),
                    "online": sum(1 for d in devs if d.get("online")), "devices": len(devs)}
        return jsonable(st)

    @app.get("/api/logs")
    async def logs(n: int = 200):
        from gateway.logging_setup import ring_buffer
        return {"lines": ring_buffer.lines(max(1, min(n, 1000)))}

    # --- Konfiguration ----------------------------------------------------------------------
    def dump_cfg(cfg) -> dict:
        d = cfg.model_dump(mode="json", exclude_none=True)
        d.get("web", {}).pop("password_hash", None)
        return d

    def diff_summary() -> list[str]:
        a, b = dump_cfg(service.cfg), dump_cfg(service.draft)
        out: list[str] = []
        names = {"can": "Schnittstelle CAN", "modbus": "Modbus", "poll": "Abfrage",
                 "safety": "Schreibschutz", "web": "Weboberfläche", "lc1": "LC1", "service": "Dienst"}
        for sec, label in names.items():
            if a.get(sec) != b.get(sec):
                out.append(f"{label} geändert")
        da = {d["id"]: d for d in a.get("devices", [])}
        db = {d["id"]: d for d in b.get("devices", [])}
        for i in sorted(db.keys() - da.keys()):
            out.append(f"Gerät {i} neu")
        for i in sorted(da.keys() - db.keys()):
            out.append(f"Gerät {i} entfernt")
        for i in sorted(db.keys() & da.keys()):
            x, y = da[i], db[i]
            if x == y:
                continue
            parts = []
            for f, label in (("unit_id", "Unit-ID"), ("name", "Name"), ("enabled", "aktiv")):
                if x.get(f) != y.get(f):
                    parts.append(f"{label}: {x.get(f)} -> {y.get(f)}")
            for tbl, label in (("values", "Messwerte"), ("params", "Parameter")):
                def keyed(lst):
                    return {e.get("nr", e.get("k", e.get("e"))): e for e in lst}
                mx, my = keyed(x.get(tbl, [])), keyed(y.get(tbl, []))
                n = sum(1 for k in my if k not in mx or mx[k] != my[k]) + sum(1 for k in mx if k not in my)
                if n:
                    parts.append(f"{n} {label}-Einträge geändert")
            out.append(f"Gerät {i}: " + ("; ".join(parts) or "geändert"))
        return out

    @app.get("/api/config")
    async def get_config():
        changes = diff_summary()
        return {"active": dump_cfg(service.cfg), "draft": dump_cfg(service.draft),
                "dirty": bool(changes), "changes": changes,
                "password_set": password_set(), "config_path": str(service.config_path),
                "windows_local_only": False, "web_remote": not is_local()}

    @app.patch("/api/draft/settings/{section}")
    async def patch_settings(section: str, request: Request):
        body = await _body(request)
        if section == "web":
            body.pop("password_hash", None)
        service.update_draft_settings(section, body)
        return {"ok": True}

    @app.post("/api/draft/apply")
    async def apply():
        warnings = await service.apply_draft()
        return {"ok": True, "warnings": list(warnings or [])}

    @app.post("/api/draft/discard")
    async def discard():
        service.discard_draft()
        return {"ok": True}

    @app.get("/api/registers.csv")
    async def registers_csv():
        return Response(export_csv(service.cfg).encode("utf-8"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="registerliste.csv"'})

    # --- Scan / Jobs ------------------------------------------------------------------------
    @app.post("/api/scan")
    async def scan(request: Request):
        body = await _body(request)
        job = service.start_scan(bool(body.get("can", False)), body.get("lc1_ports") or None,
                                 bool(body.get("full", False)))
        return job_dict(job)

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str):
        job = service.get_job(job_id)
        if job is None:
            raise HTTPException(404, "Auftrag unbekannt")
        return job_dict(job)

    @app.post("/api/can/test")
    async def can_test(request: Request):
        body = await _body(request)
        return job_dict(service.start_can_test(str(body.get("interface", "")), str(body.get("channel", "")),
                                               int(body.get("bitrate", 50000))))

    @app.get("/api/serial-ports")
    async def serial_ports():
        return {"ports": jsonable(service.list_serial_ports())}

    # --- Geräte im Entwurf ------------------------------------------------------------------
    def draft_device(device_id: str):
        try:
            return service.draft.device(device_id)
        except KeyError:
            raise HTTPException(404, f"Gerät {device_id} unbekannt") from None

    @app.post("/api/draft/devices")
    async def add_device(request: Request):
        body = await _body(request)
        fields = {f.name for f in dataclasses.fields(DeviceInfo)}
        try:
            info = DeviceInfo(**{k: v for k, v in body.items() if k in fields})
        except TypeError as e:
            raise ValueError(f"Ungültige Geräteangaben: {e}") from None
        return jsonable(service.add_scanned_device(info))

    @app.patch("/api/draft/devices/{device_id}")
    async def patch_device(device_id: str, request: Request):
        draft_device(device_id)
        body = await _body(request)
        allowed = {k: body[k] for k in ("unit_id", "name", "enabled", "entries") if k in body}
        service.update_draft_device(device_id, **allowed)
        return {"ok": True}

    @app.delete("/api/draft/devices/{device_id}")
    async def delete_device(device_id: str):
        draft_device(device_id)
        service.remove_draft_device(device_id)
        return {"ok": True}

    @app.post("/api/draft/devices/{device_id}/enumerate")
    async def enumerate_values(device_id: str):
        draft_device(device_id)
        return job_dict(service.start_enumerate(device_id))

    @app.post("/api/draft/devices/{device_id}/renumber")
    async def renumber(device_id: str, request: Request):
        draft_device(device_id)
        body = await _body(request)
        if body.get("confirm") is not True:
            raise ValueError("Bestätigung fehlt: Neu nummerieren verschiebt Registeradressen")
        table = body.get("table")
        if table not in ("values", "params"):
            raise ValueError("table muss 'values' oder 'params' sein")
        service.renumber_registers(device_id, table)
        return {"ok": True}

    @app.get("/api/draft/devices/{device_id}/values")
    async def draft_values(device_id: str):
        dev = draft_device(device_id)
        cache = {(v.kind, v.key): v for v in (service.enum_cache(device_id) or [])}
        live = {}
        try:
            live = {(r["kind"], r["key"]): r.get("value") for r in service.device_values(device_id)}
        except Exception:  # noqa: BLE001 - Gerät evtl. noch nicht in aktiver Konfiguration
            pass
        rows: dict[tuple[str, int], dict] = {}
        for kind, entries in (("value", dev.values), ("param", dev.params)):
            for e in entries:
                rows[(kind, e.key)] = {
                    "kind": kind, "key": e.key, "unit": e.unit, "min": e.min, "max": e.max,
                    "name": display_name(e.name, e.nr) if dev.bus == "can" else e.name,
                    "decimals": e.decimals, "value": None, "reg": e.reg, "active": e.active,
                    "dashboard": e.dashboard,
                    "writable": bool(getattr(e, "writable", False)), "in_config": True,
                    "supported": e.vartyp in NUMERIC_VARTYPES,
                    "lock_reason": _lock_reason(dev, e.key) if kind == "param" else None, "tip": ""}
        for k, v in cache.items():
            r = rows.setdefault(k, {"kind": v.kind, "key": v.key, "reg": None, "active": False, "dashboard": False,
                                    "writable": False, "in_config": False})
            r.update({"name": v.name, "unit": v.unit, "min": v.min, "max": v.max, "decimals": v.decimals,
                      "value": v.value, "supported": v.supported, "lock_reason": v.lock_reason,
                      "tip": v.tip})
        for k, val in live.items():
            if k in rows and val is not None:
                rows[k]["value"] = val
        out = sorted(rows.values(), key=lambda r: (r["kind"] != "value",
                                                   r["reg"] if r["reg"] is not None else 1 << 30, r["key"]))
        return {"device": dev.id, "bus": dev.bus, "from_device": bool(cache), "rows": out}

    # --- Laufende Werte / Schreib-Historie --------------------------------------------------
    @app.get("/api/devices/{device_id}/values")
    async def device_values(device_id: str):
        return {"values": jsonable(service.device_values(device_id))}

    @app.get("/api/writes")
    async def writes():
        return {"writes": jsonable(service.write_history())}

    @app.get("/api/dashboard")
    async def dashboard():
        return jsonable(service.dashboard())

    # --- Parameter setzen (gleicher Weg wie Modbus FC 16) --------------------------------------
    @app.get("/api/devices/{device_id}/params")
    async def device_params(device_id: str):
        try:
            return jsonable(service.param_overview(device_id))
        except KeyError as exc:
            raise HTTPException(404, f"Gerät {device_id} nicht in der aktiven Konfiguration") from exc

    @app.post("/api/devices/{device_id}/params/{key}")
    async def set_param(device_id: str, key: str, request: Request):
        body = await _body(request)
        value = body.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(400, "value muss eine Zahl sein")
        try:
            k = int(key, 0)
        except ValueError as exc:
            raise HTTPException(400, f"Ungültige Parameternummer {key!r}") from exc
        try:
            entry = await service.write_param_web(device_id, k, float(value))
        except KeyError as exc:
            raise HTTPException(404, f"Gerät {device_id} nicht in der aktiven Konfiguration") from exc
        except WriteRejected as exc:
            raise HTTPException(409, f"Abgelehnt: {exc.reason}") from exc
        except RuntimeError as exc:      # gesendet, aber keine Bestätigung -> Schreib-Alarm
            raise HTTPException(502, f"Gesendet, aber {exc} – bitte Wert am Regler prüfen") from exc
        return jsonable(entry)

    # --- Aufzeichnung -----------------------------------------------------------------------
    @app.get("/api/history")
    async def history_status():
        return await asyncio.to_thread(service.history_status)

    @app.delete("/api/history/days/{day}")
    async def history_delete(day: str):
        if not await asyncio.to_thread(service.history.delete_day, check_day(day)):
            raise HTTPException(404, f"Keine Aufzeichnung für {day}")
        return {"ok": True}

    @app.get("/api/history/days/{day}.csv")
    async def history_csv(day: str):
        p = service.history.path(check_day(day))
        if not p.exists():
            raise HTTPException(404, f"Keine Aufzeichnung für {day}")
        return StreamingResponse(iterate_in_threadpool(service.history.export_csv(day)),
                                 media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="aufzeichnung-{day}.csv"'})

    @app.get("/api/history/catalog")
    async def history_catalog(t_from: float = Query(..., alias="from"), t_to: float = Query(..., alias="to")):
        metas = await asyncio.to_thread(service.history.catalog, t_from, t_to)
        return {"series": [dataclasses.asdict(m) for m in metas]}

    @app.get("/api/history/series")
    async def history_series(device: str, keys: str, t_from: float = Query(..., alias="from"),
                             t_to: float = Query(..., alias="to"), kind: str = "value", points: int = 800):
        if kind not in ("value", "param"):
            raise HTTPException(400, "kind muss value oder param sein")
        try:
            key_list = [int(k, 0) for k in keys.split(",") if k.strip()][:16]
        except ValueError as exc:
            raise HTTPException(400, "keys: Nummern durch Komma getrennt") from exc
        if t_to - t_from > 366 * 86400:
            raise HTTPException(400, "Zeitraum höchstens ein Jahr")
        res = await asyncio.to_thread(service.history.query, device, kind, key_list, t_from, t_to, points)
        return {"device": device, "kind": kind, "from": t_from, "to": t_to, "bucket_s": res["bucket_s"],
                "interval_s": service.cfg.history.interval_s,
                "series": [{"key": k, "points": pts} for k, pts in res["series"].items()]}

    # --- Seiten -----------------------------------------------------------------------------
    @app.get("/")
    async def index(request: Request):
        page = "index.html"
        if not authorized(request):
            page = "login.html"
        return FileResponse(STATIC / page, headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
