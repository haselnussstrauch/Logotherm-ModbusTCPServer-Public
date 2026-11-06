"""Aufzeichnung der Werte: eine SQLite-Datei je Kalendertag im Datenordner.

    <data_dir>/history/2026-11-05.sqlite

- Tageweise löschen = Datei löschen; die Aufbewahrung (``history.retention_days``) entfernt alte
  Tagesdateien automatisch. Kein VACUUM, keine wachsende Gesamtdatenbank.
- Tabelle ``series``: eine Zeile je Reihe (Gerät, Art value/param, Nummer, Name, Einheit) mit
  kurzer Kennung ``sid`` – alte Tage bleiben auch nach Konfigurationsänderungen lesbar.
  Tabelle ``samples``: ``(sid, ts, value)``, Zeit in ganzen Unix-Sekunden (Wanduhr), ohne
  Rowid – rund 25 Byte je Messpunkt.
- Datum = lokales Datum des Rechners (so wie der Anwender „Tag“ versteht).
- Alle Zugriffe laufen über eine Sperre; Aufrufer aus asyncio verwenden ``asyncio.to_thread``.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SUFFIXES = ("", "-wal", "-shm", "-journal")
MIN_FREE_BYTES = 200 * 1024 * 1024        # darunter wird nicht mehr aufgezeichnet

_SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    sid INTEGER PRIMARY KEY, dev TEXT NOT NULL, kind TEXT NOT NULL, key INTEGER NOT NULL,
    name TEXT NOT NULL DEFAULT '', unit TEXT NOT NULL DEFAULT '', nr TEXT NOT NULL DEFAULT '',
    dev_name TEXT NOT NULL DEFAULT '', UNIQUE (dev, kind, key));
CREATE TABLE IF NOT EXISTS samples (
    sid INTEGER NOT NULL, ts INTEGER NOT NULL, value REAL NOT NULL, PRIMARY KEY (sid, ts)) WITHOUT ROWID;
"""


@dataclass(frozen=True)
class SeriesMeta:
    dev: str
    kind: str
    key: int
    name: str
    unit: str
    nr: str
    dev_name: str


def day_of(ts: float) -> str:
    return dt.date.fromtimestamp(ts).isoformat()


def check_day(day: str) -> str:
    if not DAY_RE.match(day or ""):
        raise ValueError(f"Ungültiges Datum {day!r} (erwartet JJJJ-MM-TT)")
    dt.date.fromisoformat(day)
    return day


class History:
    def __init__(self, data_dir: Path, clock=time.time) -> None:
        self.dir = Path(data_dir) / "history"
        self._clock = clock
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._conn_day: str | None = None
        self._sids: dict[tuple[str, str, int], int] = {}
        self._meta_written: dict[int, tuple[str, str, str, str]] = {}

    # --- Dateien ---
    def path(self, day: str) -> Path:
        return self.dir / f"{check_day(day)}.sqlite"

    def _files(self, day: str) -> list[Path]:
        base = self.path(day)
        return [base.with_name(base.name + s) for s in SUFFIXES]

    def _close_locked(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
        self._conn, self._conn_day = None, None
        self._sids.clear()
        self._meta_written.clear()

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _writer(self, day: str) -> sqlite3.Connection:
        if self._conn_day != day:
            self._close_locked()
            self.dir.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path(day), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            self._conn, self._conn_day = conn, day
        assert self._conn is not None
        return self._conn

    # --- Schreiben ---
    def record(self, samples: Iterable[tuple[float, str, str, int, float]],
               meta: dict[tuple[str, str, int], SeriesMeta]) -> int:
        """Speichert Messpunkte ``(ts, dev, kind, key, value)``; Name/Einheit aus ``meta``."""
        by_day: dict[str, list[tuple]] = {}
        for s in samples:
            by_day.setdefault(day_of(s[0]), []).append(s)
        n = 0
        with self._lock:
            for day, rows in sorted(by_day.items()):
                conn = self._writer(day)
                with conn:
                    out = []
                    for ts, dev, kind, key, value in rows:
                        sid = self._sid(conn, dev, kind, key, meta.get((dev, kind, key)))
                        out.append((sid, int(round(ts)), value))
                    # gleiche Reihe in derselben Sekunde: der neuere Wert gewinnt
                    conn.executemany("INSERT OR REPLACE INTO samples (sid, ts, value) VALUES (?,?,?)", out)
                n += len(rows)
        return n

    def _sid(self, conn: sqlite3.Connection, dev: str, kind: str, key: int, m: SeriesMeta | None) -> int:
        k = (dev, kind, key)
        sid = self._sids.get(k)
        if sid is None:
            conn.execute("INSERT OR IGNORE INTO series (dev, kind, key) VALUES (?,?,?)", k)
            sid = conn.execute("SELECT sid FROM series WHERE dev = ? AND kind = ? AND key = ?", k).fetchone()[0]
            self._sids[k] = sid
        if m is not None:
            sig = (m.name, m.unit, m.nr, m.dev_name)
            if self._meta_written.get(sid) != sig:
                conn.execute("UPDATE series SET name = ?, unit = ?, nr = ?, dev_name = ? WHERE sid = ?", (*sig, sid))
                self._meta_written[sid] = sig
        return sid

    # --- Verwaltung ---
    def days(self) -> list[dict]:
        """Vorhandene Tage, neuester zuerst: Datum, Größe in Bytes."""
        if not self.dir.is_dir():
            return []
        out = []
        for p in self.dir.glob("*.sqlite"):
            day = p.stem
            if not DAY_RE.match(day):
                continue
            size = sum(f.stat().st_size for f in self._files(day) if f.exists())
            out.append({"date": day, "size": size})
        return sorted(out, key=lambda d: d["date"], reverse=True)

    def delete_day(self, day: str) -> bool:
        check_day(day)
        with self._lock:
            if self._conn_day == day:
                self._close_locked()
            found = False
            for f in self._files(day):
                if f.exists():
                    f.unlink()
                    found = True
        if found:
            log.info("Aufzeichnung %s gelöscht", day)
        return found

    def cleanup(self, retention_days: int, today: dt.date | None = None) -> list[str]:
        """Löscht Tage außerhalb der Aufbewahrung: behalten werden ``retention_days`` Kalendertage
        einschließlich heute."""
        today = today or dt.date.fromtimestamp(self._clock())
        cutoff = (today - dt.timedelta(days=max(1, retention_days) - 1)).isoformat()
        deleted = [d["date"] for d in self.days() if d["date"] < cutoff]
        for day in deleted:
            self.delete_day(day)
        if deleted:
            log.info("Aufzeichnung: %d Tag(e) älter als %d Tage gelöscht (%s … %s)",
                     len(deleted), retention_days, deleted[-1], deleted[0])
        return deleted

    def total_size(self) -> int:
        return sum(d["size"] for d in self.days())

    def disk_free(self) -> int | None:
        p = self.dir if self.dir.is_dir() else self.dir.parent
        try:
            return shutil.disk_usage(p).free
        except OSError:
            return None

    # --- Lesen ---
    def _readers(self, t_from: float, t_to: float) -> Iterator[sqlite3.Connection]:
        d = dt.date.fromtimestamp(t_from)
        last = dt.date.fromtimestamp(max(t_from, t_to - 1e-6))
        while d <= last:
            p = self.path(d.isoformat())
            if p.exists():
                conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, check_same_thread=False)
                try:
                    yield conn
                finally:
                    conn.close()
            d += dt.timedelta(days=1)

    def catalog(self, t_from: float, t_to: float) -> list[SeriesMeta]:
        """Alle Reihen mit Daten im Zeitraum (neueste Beschriftung gewinnt)."""
        found: dict[tuple[str, str, int], SeriesMeta] = {}
        with self._lock:
            self._flush_locked()
        for conn in self._readers(t_from, t_to):
            try:
                for row in conn.execute("SELECT dev, kind, key, name, unit, nr, dev_name FROM series "
                                        "WHERE EXISTS (SELECT 1 FROM samples WHERE samples.sid = series.sid)"):
                    found[(row[0], row[1], row[2])] = SeriesMeta(*row)
            except sqlite3.Error as exc:
                log.warning("Aufzeichnung lesen: %s", exc)
        return sorted(found.values(), key=lambda m: (m.dev, m.kind != "value", m.key))

    def query(self, dev: str, kind: str, keys: list[int], t_from: float, t_to: float,
              max_points: int = 800) -> dict:
        """Zeitreihen im Zeitraum. Mehr als ``max_points`` Punkte werden je Zeitabschnitt zu
        Mittelwert, Minimum und Maximum zusammengefasst. Punkte: ``[t, mittel, min, max]``."""
        if t_to <= t_from:
            raise ValueError("Zeitraum leer")
        max_points = max(10, min(5000, int(max_points)))
        bucket = (t_to - t_from) / max_points
        series: dict[int, list[list[float]]] = {k: [] for k in keys}
        with self._lock:
            self._flush_locked()
        for conn in self._readers(t_from, t_to):
            for key in keys:
                try:
                    row = conn.execute("SELECT sid FROM series WHERE dev = ? AND kind = ? AND key = ?",
                                       (dev, kind, key)).fetchone()
                    if row is None:
                        continue
                    rows = conn.execute(
                        "SELECT CAST((ts - ?) / ? AS INTEGER) AS b, AVG(ts), AVG(value), MIN(value), MAX(value) "
                        "FROM samples WHERE sid = ? AND ts >= ? AND ts < ? "
                        "GROUP BY b ORDER BY b", (t_from, bucket, row[0], t_from, t_to)).fetchall()
                except sqlite3.Error as exc:
                    log.warning("Aufzeichnung lesen: %s", exc)
                    continue
                series[key].extend([r[1], r[2], r[3], r[4]] for r in rows)
        return {"bucket_s": bucket, "series": series}

    def _flush_locked(self) -> None:
        """WAL-Inhalt für Leser sichtbar machen (Leser mit mode=ro sehen ihn ohnehin; hier nur
        ein Checkpoint, damit die Datei kompakt bleibt)."""
        if self._conn is not None:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                pass

    def export_csv(self, day: str) -> Iterator[str]:
        """Tag als CSV (Semikolon, Dezimalkomma für Excel, BOM), eine Zeile je Messpunkt."""
        p = self.path(day)
        if not p.exists():
            raise FileNotFoundError(day)
        with self._lock:
            self._flush_locked()
        yield "﻿Zeit;Gerät;Gerätename;Art;Nr;Name;Einheit;Wert\r\n"
        for conn in self._readers(*_day_range(day)):
            cur = conn.execute(
                "SELECT s.ts, m.dev, m.dev_name, m.kind, m.nr, m.name, m.unit, s.value "
                "FROM samples s JOIN series m ON m.sid = s.sid ORDER BY s.ts, m.dev, m.kind, m.key")
            while chunk := cur.fetchmany(5000):
                out = []
                for ts, devid, dev_name, kind, nr, name, unit, value in chunk:
                    t = dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                    art = "Messwert" if kind == "value" else "Parameter"
                    out.append(";".join([t, devid, _csv(dev_name), art, nr, _csv(name), _csv(unit),
                                         f"{value:.10g}".replace(".", ",")]) + "\r\n")
                yield "".join(out)


def _day_range(day: str) -> tuple[float, float]:
    d = dt.date.fromisoformat(day)
    start = dt.datetime.combine(d, dt.time()).timestamp()
    end = dt.datetime.combine(d + dt.timedelta(days=1), dt.time()).timestamp()
    return start, end


def _csv(s: str) -> str:
    if any(c in s for c in ';"\r\n'):
        return '"' + s.replace('"', '""') + '"'
    return s
