"""Append-only CSV log of ride telemetry.

Why CSV: it opens in anything, greps, and streams. A ride is a few thousand rows
at ~2.5 Hz, so nothing here needs to be clever.

Two deliberate choices:

**It never calls `Mapper.evaluate()`.** `CadenceTracker.value()` is a mutating
getter -- it advances the filter's internal clock -- so a second caller would
steal `dt` from the output loop and silently slow convergence. Derived values
are read from `Status`, which the output loop already maintains.

**It only writes while you are actually riding.** The bridge runs whenever the Pi
is on, which would otherwise mean ~10 MB a day of zeros. Rows are written while
cadence has been non-zero within `idle_grace` seconds, so a log file corresponds
to a ride rather than to an uptime.
"""

from __future__ import annotations

import contextlib
import csv
import time
from datetime import datetime, timezone
from pathlib import Path

FIELDS = [
    "wall_time",        # ISO 8601, for correlating with anything else
    "t",                # seconds since this log started
    "cadence_rpm",
    "power_w",
    "resistance",
    "movement_scale",   # what the left stick was actually multiplied by
    "sprint",
    "gate_open",
]


class RideLogger:
    """One CSV per bridge run. Safe to call unconditionally; never raises."""

    def __init__(self, directory: str | Path, idle_grace: float = 120.0) -> None:
        self.directory = Path(directory)
        self.idle_grace = idle_grace
        self.path: Path | None = None
        self.rows = 0
        self._file = None
        self._writer = None
        self._failed = False
        self._start = time.monotonic()
        self._last_active: float | None = None

    def _open(self) -> None:
        """Created lazily, on the first pedal stroke -- so an idle bridge that
        is never ridden leaves no empty files behind."""
        self.directory.mkdir(parents=True, exist_ok=True)
        # UTC in the filename as well as the rows: a file named for local time
        # whose first row is an hour off is a trap when correlating.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
        path = self.directory / f"ride-{stamp}.csv"
        # The stamp has one-second resolution, so two rides opening in the same
        # second would collide and the second would truncate the first. Rare in
        # practice (idle_grace is 120s), silent and unrecoverable if it happens.
        suffix = 2
        while path.exists():
            path = self.directory / f"ride-{stamp}-{suffix}.csv"
            suffix += 1
        self._file = path.open("w", newline="")
        self._writer = csv.writer(self._file)
        self._writer.writerow(FIELDS)
        self.path = path          # only once the file really exists

    def log(self, sample, status) -> None:
        """Record one telemetry sample. Failures are swallowed: a logging
        problem must never take down the bridge mid-ride."""
        try:
            if self._failed:
                return
            now = time.monotonic()
            if sample.cadence_rpm > 0:
                self._last_active = now
            if self._last_active is None:
                return
            if now - self._last_active > self.idle_grace:
                # Close the file so the NEXT ride opens a fresh one. Without
                # this, one bridge run (which may span weeks of uptime) appends
                # every ride to a single file, and ride_report merges unrelated
                # rides into one power distribution -- describing neither.
                if self._file is not None:
                    self._file.close()
                    self._file = None
                    self._writer = None
                return

            if self._writer is None:
                self._open()

            self._writer.writerow([
                datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                f"{now - self._start:.2f}",
                sample.cadence_rpm,
                sample.power_w,
                sample.resistance,
                f"{status.move:.3f}",
                int(status.sprint),
                int(status.gate),
            ])
            self.rows += 1
            # Flushed per row: a ride ends with a power cut or a Ctrl-C far more
            # often than with a clean shutdown, and 2.5 rows/second is nothing.
            self._file.flush()
        except Exception as exc:                # noqa: BLE001 - logging is optional
            # Latch: retrying an unwritable directory at 2.5 Hz forever, in
            # silence, is worse than losing the log.
            self._failed = True
            print(f"  ride log disabled ({type(exc).__name__}: {exc})", flush=True)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            if self._file is not None:
                self._file.close()
