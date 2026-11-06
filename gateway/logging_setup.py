"""Logging: Konsole + rotierende Logdatei in data_dir/logs + Ringpuffer für die Weboberfläche."""

from __future__ import annotations

import collections
import logging
import logging.handlers
import threading
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class RingBufferHandler(logging.Handler):
    """Hält die letzten N formatierten Logzeilen (Statusseite „Protokoll“)."""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self._buf: collections.deque[str] = collections.deque(maxlen=capacity)
        self._lock_buf = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001 - Logging darf nie den Dienst abbrechen
            return
        with self._lock_buf:
            self._buf.append(line)

    def lines(self, n: int | None = None) -> list[str]:
        with self._lock_buf:
            items = list(self._buf)
        return items[-n:] if n else items


ring_buffer = RingBufferHandler()


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level)
    fmt = logging.Formatter(FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    ring_buffer.setFormatter(fmt)
    root.addHandler(ring_buffer)

    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_dir / "gateway.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as exc:
            logging.getLogger(__name__).warning("Logdatei in %s nicht möglich: %s", log_dir, exc)

    # pymodbus ist auf INFO sehr gesprächig
    logging.getLogger("pymodbus").setLevel(max(logging.WARNING, root.level))
