"""Install progress reporting.

`Progress.emit(pct, msg, detail)` writes one JSON line per event ({"event":"progress",...}) to a stream
(the wizard reads them and draws the bar) and appends a human line to the install log. With no stream it is
silent, so library code can always call it.

Every event also carries `elapsed`: the seconds since the current step (`msg`) started. A heartbeat thread re-sends
the last event once a second, so the screen keeps counting during long steps (an AUR build can take minutes) and the
user can always see that the install is alive.
"""
from __future__ import annotations

import json
import os
import threading
import time

LOG_PATH = "/var/log/tapkich-os-install.log"


class Progress:
    def __init__(self, stream=None, log_path: str | None = LOG_PATH, clock=time.monotonic):
        self.stream = stream
        self.pct = 0
        self.msg = ""
        self.detail = ""
        self.log_path = log_path
        self._clock = clock
        self._since = clock()
        self._lock = threading.Lock()
        self._beat: threading.Thread | None = None
        self._stop = threading.Event()
        self._log = None
        if log_path:
            try:
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                self._log = open(log_path, "a", buffering=1)
            except OSError:
                self._log = None

    def emit(self, pct: float, msg: str, detail: str = ""):
        with self._lock:
            if msg != self.msg:
                self._since = self._clock()                       # a new step: its timer starts at 0
            self.pct = max(self.pct, min(100, int(pct)))          # the bar never goes backwards
            self.msg, self.detail = msg, detail
            self._write()
        self.log(f"[{self.pct:3d}%] {msg} {detail}".rstrip())

    def update(self, detail: str):
        """New sub-text for the current step (e.g. the latest build output line), same percentage. Not logged."""
        with self._lock:
            self.detail = detail
            self._write()

    def elapsed(self) -> int:
        return int(self._clock() - self._since)

    def _write(self):
        if self.stream:
            self.stream.write(json.dumps({"event": "progress", "pct": self.pct, "msg": self.msg, "detail": self.detail,
                                          "elapsed": self.elapsed()}) + "\n")
            self.stream.flush()

    # ---- heartbeat
    def start_heartbeat(self, interval: float = 1.0):
        if not self.stream or self._beat:
            return
        self._stop.clear()

        def beat():
            while not self._stop.wait(interval):
                with self._lock:
                    if self.msg:
                        try:
                            self._write()
                        except (OSError, ValueError):             # the reader went away: stop quietly
                            return
        self._beat = threading.Thread(target=beat, name="progress-heartbeat", daemon=True)
        self._beat.start()

    def stop_heartbeat(self):
        if self._beat:
            self._stop.set()
            self._beat.join(timeout=2)
            self._beat = None

    def log(self, line: str):
        if self._log:
            self._log.write(f"{time.strftime('%H:%M:%S')} {line}\n")

    def close(self):
        self.stop_heartbeat()
        if self._log:
            self._log.close()
            self._log = None


NULL = Progress(None, None)


class Span:
    """Map a sub-task's 0..1 fraction onto a slice [lo, hi] of the overall bar."""

    def __init__(self, progress: Progress, lo: float, hi: float):
        self.p, self.lo, self.hi = progress, lo, hi

    def at(self, fraction: float, msg: str, detail: str = ""):
        fraction = max(0.0, min(1.0, fraction))
        self.p.emit(self.lo + (self.hi - self.lo) * fraction, msg, detail)
