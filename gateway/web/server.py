"""Uvicorn im bestehenden asyncio-Loop starten und stoppen."""

from __future__ import annotations

import asyncio
import logging
import sys

import uvicorn

from gateway.config import GatewayConfig

log = logging.getLogger(__name__)


def effective_bind(cfg: GatewayConfig) -> str:
    """Adresse der Weboberfläche laut ``web.bind`` (auch unter Windows; 127.0.0.1 = nur lokal).

    Ist sie aus dem Netz erreichbar, gilt: Passwort Pflicht; unter Windows lässt sich das erste
    Passwort nur am Gateway-PC selbst festlegen (gateway/web/app.py)."""
    return cfg.web.bind


async def serve_web(app, cfg: GatewayConfig) -> tuple[uvicorn.Server, asyncio.Task]:
    """Startet die Weboberfläche als Task im laufenden Loop.

    Stoppen: `await stop_web(server, task)` (setzt `server.should_exit = True`).
    Wirft RuntimeError, wenn der Port nicht gebunden werden kann."""
    config = uvicorn.Config(app, host=effective_bind(cfg), port=cfg.web.port, loop="none",
                            log_config=None, access_log=False, lifespan="off")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None   # Signale gehören dem Hauptprogramm

    async def run() -> None:
        try:
            await server.serve()
        except SystemExit as e:   # uvicorn beendet sich bei Bind-Fehlern per sys.exit
            raise RuntimeError(f"Weboberfläche konnte nicht starten (Port {config.port} belegt?)") from e

    task = asyncio.create_task(run(), name="web")
    for _ in range(100):   # bis zu 5 s auf den Start warten
        if server.started or task.done():
            break
        await asyncio.sleep(0.05)
    if task.done():
        task.result()   # löst den Fehler aus
    log.info("Weboberfläche auf http://%s:%d", config.host, config.port)
    return server, task


async def stop_web(server: uvicorn.Server, task: asyncio.Task, timeout: float = 5.0) -> None:
    server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()
    except Exception:  # noqa: BLE001
        log.exception("Fehler beim Beenden der Weboberfläche")
