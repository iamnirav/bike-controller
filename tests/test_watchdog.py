"""Tests for systemd watchdog notifications.

No systemd needed: sd_notify is a datagram to an AF_UNIX socket, so a real one
in a temp directory exercises every branch. This module had no coverage at all
and is now on the critical path -- under Type=notify, a watchdog that fails to
notify does not degrade, it gets the unit killed.
"""

import os
import socket
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bike_controller.watchdog import Watchdog        # noqa: E402


class Listener:
    """A real notify socket, so nothing here is faked."""

    def __init__(self, tmp: str) -> None:
        self.path = str(Path(tmp) / "notify.sock")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(self.path)
        self.sock.settimeout(0.2)

    def messages(self) -> list[str]:
        out = []
        while True:
            try:
                out.append(self.sock.recv(256).decode())
            except socket.timeout:
                return out


def with_env(**env):
    saved = {k: os.environ.get(k) for k in env}
    for key, value in env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    return saved


def restore(saved):
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_ready_and_rate_limited_pings():
    with tempfile.TemporaryDirectory() as tmp:
        listener = Listener(tmp)
        saved = with_env(NOTIFY_SOCKET=listener.path, WATCHDOG_USEC="15000000")
        try:
            dog = Watchdog()
            assert dog.enabled and dog.active
            assert abs(dog.interval - 5.0) < 0.01, dog.interval

            dog.ready()
            assert listener.messages() == ["READY=1"]

            dog.ping(now=100.0)
            dog.ping(now=101.0)          # too soon
            dog.ping(now=104.9)          # still too soon
            dog.ping(now=105.1)          # due
            assert listener.messages() == ["WATCHDOG=1", "WATCHDOG=1"]
            dog.close()
        finally:
            restore(saved)


def test_no_notify_socket_is_a_silent_no_op():
    saved = with_env(NOTIFY_SOCKET=None, WATCHDOG_USEC=None)
    try:
        dog = Watchdog()
        assert not dog.enabled and not dog.active
        dog.ready(); dog.ping(); dog.stopping(); dog.close()   # must not raise
    finally:
        restore(saved)


def test_notify_without_watchdog_usec_sends_ready_but_not_pings():
    with tempfile.TemporaryDirectory() as tmp:
        listener = Listener(tmp)
        saved = with_env(NOTIFY_SOCKET=listener.path, WATCHDOG_USEC=None)
        try:
            dog = Watchdog()
            assert dog.enabled and not dog.active
            dog.ready()
            dog.ping(now=100.0)
            assert listener.messages() == ["READY=1"]
            dog.close()
        finally:
            restore(saved)


def test_unconnectable_socket_does_not_claim_to_be_healthy():
    """The regression this test exists for.

    Assigning the socket before connect() left a created-but-unconnected socket
    behind, so `enabled` and `active` both reported True and the banner said the
    unit was supervised while nothing was ever delivered. Under Type=notify that
    gets the unit killed at TimeoutStartSec.
    """
    saved = with_env(NOTIFY_SOCKET="/nonexistent/dir/notify.sock",
                     WATCHDOG_USEC="15000000")
    try:
        dog = Watchdog()
        assert not dog.enabled, "claims to be enabled with no working socket"
        assert not dog.active
        dog.ready(); dog.ping(); dog.close()       # must not raise
    finally:
        restore(saved)


if __name__ == "__main__":
    from _runner import main          # noqa: E402 - script-mode only
    main(globals())
