"""Einstiegspunkt: `gateway run | scan | export-csv | selftest | capture | web-access`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from gateway import __version__
from gateway.config import GatewayConfig, load_config
from gateway.logging_setup import setup_logging

log = logging.getLogger("gateway")


def _default_config_path() -> Path:
    return GatewayConfig().service.resolved_data_dir() / "gateway.yaml"


def _install_stop(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows: Strg+C kommt als KeyboardInterrupt
            pass


def _trace_logger(path: Path | None):
    """Mitschnitt aller Rahmen (CAN/LC1) in eine Datei bzw. ins Log."""
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a", encoding="utf-8", buffering=1)
    import datetime as dt

    def trace(direction: str, text: str) -> None:
        fh.write(f"{dt.datetime.now().isoformat(timespec='milliseconds')} {direction:<4} {text}\n")
    return trace


def _start_simulation(args, cfg: GatewayConfig):
    """--simulate: CAN-Simulator (LC2, MC2, LC1 BSB, Zündung, Sonde) im selben Prozess auf dem
    python-can-virtual-Bus. LC1-Simulatoren gibt es über Ports ``sim://<name>``."""
    if not getattr(args, "simulate", False):
        return None
    from gateway.can_lc2.simulator import CanSimulator
    cfg.can.enabled = True
    cfg.can.interface = "virtual"
    cfg.can.channel = "gateway-sim"
    sim = CanSimulator("gateway-sim", broadcast_interval_s=1.0).start()
    log.warning("SIMULATION: CAN-Simulator läuft auf virtual/gateway-sim – keine echte Hardware")
    return sim


def _stop_simulation(sim) -> None:
    if sim is not None:
        sim.stop()


# --- run ----------------------------------------------------------------------------------------
async def _run(cfg: GatewayConfig, path: Path, args: argparse.Namespace) -> None:
    from gateway.service import Service

    sim = _start_simulation(args, cfg)
    svc = Service(cfg, path, dummy=args.dummy)
    svc.trace = _trace_logger(args.trace)
    stop = asyncio.Event()
    _install_stop(stop)
    await svc.start(web=not args.no_web)
    mb = f"{cfg.modbus.bind}:{cfg.modbus.port}" if cfg.modbus.enabled else "aus"
    log.info("Gateway %s läuft – Modbus %s, Web %s:%d%s", __version__, mb,
             cfg.web.bind, cfg.web.port, " (Dummy-Werte)" if args.dummy else "")
    try:
        await stop.wait()
    finally:
        await svc.stop()
        _stop_simulation(sim)
        log.info("Gateway beendet")


def cmd_run(args, cfg, path) -> int:
    try:
        asyncio.run(_run(cfg, path, args))
    except KeyboardInterrupt:
        pass
    return 0


# --- export-csv ---------------------------------------------------------------------------------
def cmd_export_csv(args, cfg, path) -> int:
    from gateway.registermap import export_csv

    text = export_csv(cfg)
    if args.output == "-":
        # Windows-Konsole (cp1252) kann z. B. "₂" nicht darstellen -> immer UTF-8 ausgeben
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.write(text.lstrip("﻿"))
    else:
        Path(args.output).write_text(text, encoding="utf-8", newline="")
        log.info("Registerliste geschrieben: %s", args.output)
    return 0


# --- scan ---------------------------------------------------------------------------------------
async def _scan(cfg: GatewayConfig, path: Path, args) -> int:
    """Geräte suchen (ohne laufenden Dienst!). Optional Werteliste laden und in gateway.yaml speichern."""
    from gateway.service import Service

    sim = _start_simulation(args, cfg)
    svc = Service(cfg, path)
    svc.trace = _trace_logger(args.trace)
    lc1_ports = [p for p in (args.lc1 or "").split(",") if p]
    if not args.can and not lc1_ports:
        args.can = True

    def progress(job):
        done, total, text = job.progress
        print(f"\r  {done}/{total} {text[:60]:<60}", end="", file=sys.stderr, flush=True)

    async def wait(job):
        while job.state == "running":
            progress(job)
            await asyncio.sleep(0.3)
        print(file=sys.stderr)
        if job.state == "error":
            raise RuntimeError(job.error)
        return job.result

    try:
        found = await wait(svc.start_scan(args.can, lc1_ports, full=args.full))
        if not found:
            print("Keine Geräte gefunden.")
            return 1
        print(f"{'Bus':<4} {'Adr/Port':<14} {'Typ':<28} {'Name':<20} {'Version':<10} Serien-Nr.")
        for i in found:
            where = f"{i['address']:#04x}" if i["bus"] == "can" else i["port"]
            print(f"{i['bus']:<4} {where:<14} {i['type_name'][:28]:<28} {(i['name'] or '')[:20]:<20} "
                  f"{(i['version'] or '')[:10]:<10} {i['serial'] or ''}")
        if not args.save:
            print("\n(Mit --save werden die Geräte in die Konfiguration übernommen.)")
            return 0
        for i in found:
            dev = svc.add_scanned_device(i)
            if args.enumerate:
                print(f"Werteliste {dev.id} laden …", file=sys.stderr)
                infos = await wait(svc.start_enumerate(dev.id))
                if args.activate_values:
                    for v in dev.values:
                        info = next((x for x in infos if x.kind == "value" and x.key == v.key), None)
                        v.active = bool(info and info.supported)
                print(f"  {sum(1 for x in infos if x.kind == 'value')} Messwerte, "
                      f"{sum(1 for x in infos if x.kind == 'param')} Parameter")
        warnings = await svc.apply_draft()
        for w in warnings:
            log.warning(w)
        print(f"Konfiguration gespeichert: {path}")
        return 0
    finally:
        await svc.stop()
        _stop_simulation(sim)


def cmd_scan(args, cfg, path) -> int:
    return asyncio.run(_scan(cfg, path, args))


# --- selftest -----------------------------------------------------------------------------------
async def _selftest(cfg: GatewayConfig, args) -> int:
    sim = _start_simulation(args, cfg)
    try:
        return await _selftest_inner(cfg)
    finally:
        _stop_simulation(sim)


async def _selftest_inner(cfg: GatewayConfig) -> int:
    results: list[tuple[str, bool, str]] = []
    if cfg.can.enabled:
        from gateway.can_lc2.selftest import can_selftest
        results += [("CAN: " + n, ok, t) for n, ok, t in await can_selftest(cfg)]
    if any(d.bus == "lc1" for d in cfg.devices):
        from gateway.serial_lc1.selftest import lc1_selftest
        results += [("LC1: " + n, ok, t) for n, ok, t in await lc1_selftest(cfg)]
    if not results:
        print("Nichts zu testen: CAN ist deaktiviert und es sind keine LC1-Geräte konfiguriert.")
        return 1
    for name, ok, text in results:
        print(f"[{'OK ' if ok else 'FEHLER'}] {name}: {text}")
    return 0 if all(ok for _, ok, _ in results) else 1


def cmd_selftest(args, cfg, path) -> int:
    return asyncio.run(_selftest(cfg, args))


# --- capture ------------------------------------------------------------------------------------
def cmd_capture(args, cfg, path) -> int:
    from gateway.can_lc2.capture import capture

    if args.interface:
        cfg.can.interface = args.interface
    if args.channel:
        cfg.can.channel = args.channel
    if args.bitrate:
        cfg.can.bitrate = args.bitrate
    out = Path(args.output or (cfg.service.resolved_data_dir() / "logs" / "can-capture.log"))
    print(f"Passiver CAN-Mitschnitt ({cfg.can.interface}/{cfg.can.channel}, {cfg.can.bitrate} bit/s) "
          f"nach {out} – es wird nichts gesendet. Strg+C beendet.")
    try:
        asyncio.run(capture(cfg, out, duration_s=args.duration))
    except KeyboardInterrupt:
        pass
    return 0


# --- Parser -------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gateway", description="Logotherm Modbus-Gateway")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-c", "--config", type=Path, default=None,
                   help="Pfad zu gateway.yaml (Default: <data_dir>/gateway.yaml)")
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG-Logging")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="Dienst starten")
    r.add_argument("--dummy", action="store_true", help="Dummy-Werte statt echter Geräte (Test)")
    r.add_argument("--no-web", action="store_true", help="ohne Weboberfläche")
    r.add_argument("--trace", type=Path, default=None, help="Mitschnitt aller gesendeten/empfangenen Rahmen in Datei")
    r.add_argument("--simulate", action="store_true", help="CAN-Simulator im Prozess statt Hardware (Generalprobe)")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("scan", help="Geräte suchen (Dienst vorher stoppen – der Adapter ist exklusiv)")
    s.add_argument("--can", action="store_true", help="CAN-Bus durchsuchen (Default, wenn --lc1 fehlt)")
    s.add_argument("--full", action="store_true", help="alle Adressen 0x01..0xFE statt der bekannten Bereiche")
    s.add_argument("--lc1", metavar="PORTS", help="LC1 an diesen Ports suchen, z. B. COM5,COM6")
    s.add_argument("--save", action="store_true", help="gefundene Geräte in gateway.yaml übernehmen")
    s.add_argument("--enumerate", action="store_true", help="mit --save: Werteliste je Gerät laden (CAN ~200 Frames)")
    s.add_argument("--activate-values", action="store_true",
                   help="mit --enumerate: alle unterstützten Messwerte bereitstellen (Parameter nicht)")
    s.add_argument("--trace", type=Path, default=None, help="Mitschnitt in Datei")
    s.add_argument("--simulate", action="store_true", help="CAN-Simulator im Prozess statt Hardware")
    s.set_defaults(func=cmd_scan)

    e = sub.add_parser("export-csv", help="Registerliste als CSV ausgeben")
    e.add_argument("-o", "--output", default="-", help="Zieldatei oder '-' für stdout")
    e.set_defaults(func=cmd_export_csv)

    t = sub.add_parser("selftest", help="Inbetriebnahme-Test: Adapter, Bus, Geräte")
    t.add_argument("--simulate", action="store_true", help="CAN-Simulator im Prozess statt Hardware")
    t.set_defaults(func=cmd_selftest)

    c = sub.add_parser("capture", help="passiver CAN-Mitschnitt (sendet nichts)")
    c.add_argument("-o", "--output", default=None, help="Zieldatei (Default: <data_dir>/logs/can-capture.log)")
    c.add_argument("--duration", type=float, default=None, help="Dauer in s (Default: bis Strg+C)")
    c.add_argument("--interface", default=None)
    c.add_argument("--channel", default=None)
    c.add_argument("--bitrate", type=int, choices=[50000, 20000], default=None)
    c.set_defaults(func=cmd_capture)

    w = sub.add_parser("web-access", help="Weboberfläche im Netz freigeben bzw. nur lokal (Installer)")
    g = w.add_mutually_exclusive_group(required=True)
    g.add_argument("--network", action="store_true", help="web.bind = 0.0.0.0 (andere PCs/Smartphones)")
    g.add_argument("--local", action="store_true", help="web.bind = 127.0.0.1 (nur dieser PC)")
    w.set_defaults(func=cmd_web_access)
    return p


def cmd_web_access(args, cfg: GatewayConfig, path: Path) -> int:
    """Ändert nur ``web.bind`` in gateway.yaml (der Rest bleibt). Wirkt nach Neustart des Dienstes."""
    from gateway.config import save_config
    bind = "0.0.0.0" if args.network else "127.0.0.1"
    if cfg.web.bind == bind:
        print(f"Weboberfläche: web.bind ist bereits {bind}")
        return 0
    cfg.web.bind = bind
    save_config(cfg, path)
    print(f"Weboberfläche: web.bind = {bind} gespeichert in {path}")
    return 0


def cli(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):   # Umlaute/Sonderzeichen auf der Windows-Konsole
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    path = args.config or _default_config_path()
    setup_logging("INFO")
    try:
        cfg = load_config(path)
    except Exception as exc:  # noqa: BLE001 - Konfigfehler verständlich melden
        log.error("Konfiguration %s ungültig: %s", path, exc)
        return 1
    level = "DEBUG" if args.verbose else cfg.service.log_level
    setup_logging(level, cfg.service.resolved_data_dir() / "logs" if args.command == "run" else None)
    from gateway.drivers import InterfaceError

    try:
        return args.func(args, cfg, path)
    except InterfaceError as exc:   # Adapter fehlt/Treiber fehlt: klare Meldung statt Traceback
        log.error("Schnittstelle nicht verfügbar: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(cli())
