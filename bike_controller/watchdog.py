"""systemd watchdog notifications.

`Restart=on-failure` only catches a process that *exits*. It cannot see one that
is alive and doing nothing -- and that is a real failure mode here: if the output
loop stops, the uinput device holds its last values, so the stick stays deflected
and the gate stays open while systemd reports the unit perfectly healthy.

Pinging from inside the output loop changes the health question from "does the
process exist?" to "is it still emitting frames?", which is the property that
actually matters.

No dependency: sd_notify is a datagram to the socket named by $NOTIFY_SOCKET.
Outside systemd the environment variable is absent and every method is a no-op,
so running the bridge by hand needs no special casing.
"""

from __future__ import annotations

import contextlib
import os
import socket
import time


class Watchdog:
    def __init__(self) -> None:
        self._address = os.environ.get("NOTIFY_SOCKET")
        self._socket = None
        self._last_ping = 0.0

        # systemd states the timeout in microseconds. Ping at a third of it, so
        # a single late frame never trips the watchdog -- only a stalled loop.
        usec = int(os.environ.get("WATCHDOG_USEC", "0") or 0)
        self.interval = (usec / 1e6) / 3.0 if usec else 0.0

        if self._address:
            # A leading '@' means the Linux abstract namespace, spelled NUL.
            path = "\0" + self._address[1:] if self._address[0] == "@" else self._address
            with contextlib.suppress(OSError):
                self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                self._socket.connect(path)

    @property
    def enabled(self) -> bool:
        return self._socket is not None

    @property
    def active(self) -> bool:
        """True when systemd is actually watching (WatchdogSec is set)."""
        return self.enabled and self.interval > 0

    def _send(self, message: str) -> None:
        if self._socket is None:
            return
        with contextlib.suppress(OSError):
            self._socket.send(message.encode())

    def ready(self) -> None:
        """Tell systemd startup is complete. Required by Type=notify."""
        self._send("READY=1")

    def ping(self, now: float | None = None) -> None:
        """Rate-limited keepalive. Safe to call every frame."""
        if not self.active:
            return
        now = time.monotonic() if now is None else now
        if now - self._last_ping < self.interval:
            return
        self._last_ping = now
        self._send("WATCHDOG=1")

    def stopping(self) -> None:
        self._send("STOPPING=1")

    def close(self) -> None:
        with contextlib.suppress(OSError):
            if self._socket is not None:
                self._socket.close()
