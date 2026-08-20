"""A small web page for turning the dials, served by the bridge itself.

    http://<pi>.local:8080

Why in-process rather than a separate service: the dials that actually need
tuning -- the movement floor above all -- can only be judged from the saddle,
inside a game. Living in the bridge's own event loop means a slider move
mutates the MappingConfig that Mapper re-reads every frame, so the change
arrives on the next output frame and you feel it while still pedalling. A
separate process could only have written a file and asked for a restart, which
drops the BLE link and destroys the uinput device Remote Play is bound to.

Values are also written back to config.env, so what you tuned on the bike is
what starts next boot.

Stdlib asyncio, no dependency. The HTTP subset here is deliberately tiny: four
routes, one client, a LAN. Connection: close on every reply and no keep-alive,
which removes the entire class of request-framing bugs in exchange for a TCP
handshake per poll that a home network does not notice.

WHY THERE IS NO PASSWORD, AND WHAT STANDS IN FOR ONE
----------------------------------------------------
The page is unauthenticated on the LAN. The allowlist and the range checks
bound what a request can do, but on their own they assume the attacker has to
BE on the LAN -- and that assumption is wrong, because any web page the rider
opens on any device on the LAN can send cross-origin requests to this port. A
`no-cors` POST would have been enough to set the movement floor to full
deflection and disable the freeze guard, and `/api/restart` needs no body at
all. So three cheap checks stand in for the password:

  Host        must name this machine or be a bare IP. Blocks DNS rebinding,
              where the attacker's own domain resolves to the Pi and the
              browser therefore treats the request as same-origin.
  Origin      if present, must match Host. Blocks the ordinary cross-site POST.
  Content-Type  POSTs must be application/json, which a form cannot send and a
              simple (preflight-free) cross-origin request cannot set. The
              preflight it does force is one we never answer.

None of this is authentication. It is the boundary that makes "only something
you deliberately opened" true, so that the allowlist argument holds.

THE RULE THIS FILE IS BUILT AROUND
----------------------------------
The web server must never be able to stop the ride. bridge.py's on_task_done
treats ANY task exception as fatal -- it sets the stop event and exits non-zero
so systemd can restart -- which is right for the output loop and catastrophic
here, because it would mean a malformed request from a phone browser ends a
session mid-firefight. So: every connection is handled inside a blanket
try/except, the accept loop cannot propagate, and a bind failure at startup
logs and leaves the server off rather than raising. A config page is a
convenience; the ride is not.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import ipaddress
import json
import os
import socket
import subprocess
import time
import urllib.parse

from . import dials as dials_module
from .dials import BY_KEY, DIALS, DialError

# A phone sends a few hundred bytes. These caps exist so a confused client (or
# anything else that finds an open port) cannot make us buffer without bound.
MAX_HEADER_BYTES = 8192
MAX_BODY_BYTES = 65536
# Generous for a LAN, short enough that a half-open connection is not held for
# the life of the process.
REQUEST_TIMEOUT = 15.0
# One phone polling at 2 Hz needs one or two at a time. The cap exists so that
# anything else on the LAN cannot hold open sockets -- and accept-loop time on a
# process running at Nice=-10 against the BLE poll loop -- without bound.
MAX_CONNECTIONS = 24

_HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_PATH = os.path.join(os.path.dirname(_HERE), "web", "index.html")


def _local_host_names() -> set[str]:
    """Host header values that mean "this machine".

    A bare IP is always allowed: a rebinding attack needs a NAME it controls,
    so it can never present one of these.
    """
    names = {"localhost"}
    try:
        hostname = socket.gethostname()
    except OSError:                                    # pragma: no cover
        return names
    short = hostname.split(".")[0].lower()
    names.update({hostname.lower(), short, f"{short}.local"})
    return names


def _end_of_headers(buffer: bytes | bytearray) -> bool:
    # LF-only is accepted as well as CRLF. It is not conformant, but a hand-typed
    # `printf 'GET / HTTP/1.1\n\n'` otherwise hangs until the request timeout
    # with no reply, which reads as a broken server rather than a picky one.
    return b"\r\n\r\n" in buffer or b"\n\n" in buffer


def _split_headers(buffer: bytes) -> tuple[bytes, bytes]:
    crlf = buffer.find(b"\r\n\r\n")
    lf = buffer.find(b"\n\n")
    if crlf != -1 and (lf == -1 or crlf <= lf):
        return buffer[:crlf], buffer[crlf + 4:]
    return buffer[:lf], buffer[lf + 2:]


class Request:
    __slots__ = ("method", "path", "query", "body", "headers")

    def __init__(self, method: str, target: str, body: bytes,
                 headers: dict[str, str] | None = None) -> None:
        self.method = method
        self.headers = headers or {}
        parsed = urllib.parse.urlsplit(target)
        self.path = parsed.path
        self.query = urllib.parse.parse_qs(parsed.query)
        self.body = body

    def json(self) -> dict:
        if not self.body:
            return {}
        try:
            payload = json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise DialError(f"body is not valid JSON: {exc}") from None
        if not isinstance(payload, dict):
            raise DialError("body must be a JSON object of key -> value")
        return payload


class ConfigServer:
    """Serves the page and applies dial changes to a live MappingConfig.

    `status` is bridge.py's Status dataclass, read-only here. `config_path` is
    where changes are persisted; None disables persistence (used by tests, and
    by anyone running the bridge straight from the CLI without a config.env).
    """

    def __init__(self, config, status, config_path: str | None,
                 on_change=None, restart_values: dict | None = None) -> None:
        self.config = config
        self.status = status
        self.config_path = config_path
        # What the bridge is ACTUALLY running for the restart-required dials.
        # Reading them back out of config.env is not the same thing and can be
        # wrong in both directions: the deployed config.env has no FRAME_RATE
        # line at all (run-bridge.sh supplies the default), and a bridge started
        # with --frame-rate 120 would be reported as whatever the file said.
        # The page's whole job is to show what is in force.
        self.restart_values = dict(restart_values or {})
        # Called after a live dial changes, so the bridge can log it. The
        # journal is the only record of what was tuned mid-ride.
        self.on_change = on_change or (lambda changes: None)
        self.started_at = time.monotonic()
        self._server: asyncio.AbstractServer | None = None
        self.bound: tuple[str, int] | None = None
        self.allowed_hosts = _local_host_names()
        self._connections = 0
        # Serialises persistence, which happens off the event loop. Without it
        # two overlapping saves can interleave their read-modify-write of
        # config.env and lose one.
        self._save_lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    async def start(self, host: str, port: int) -> bool:
        """Bind and begin serving. Returns False (having said why) on failure.

        Never raises: a port already in use must cost you the config page, not
        the ride.
        """
        try:
            self._server = await asyncio.start_server(
                self._handle, host, port)
        except OSError as exc:
            print(f"  web config could not bind {host}:{port} "
                  f"({type(exc).__name__}: {exc}); continuing without it",
                  flush=True)
            return False
        self.bound = (host, port)
        return True

    async def serve(self) -> None:
        if self._server is None:
            # start() failed. Sleep forever rather than returning: a task that
            # returns is a task done, and on_task_done would have to reason
            # about which kind of done that was.
            while True:
                await asyncio.sleep(3600)
        async with self._server:
            await self._server.serve_forever()

    # -- connection handling -----------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        """One request, one response, one close. Never raises."""
        if self._connections >= MAX_CONNECTIONS:
            # Answer, rather than dropping the socket: a bare RST shows up on
            # the phone as "no connection to the bridge", which is exactly the
            # wrong diagnosis for a bridge that is up and busy.
            with contextlib.suppress(Exception):
                # Consume whatever the client already sent first. Closing a
                # socket that still has unread data in its receive buffer sends
                # an RST instead of a FIN, so the 503 we just wrote never gets
                # read -- which is the very failure this branch is avoiding.
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(reader.read(MAX_HEADER_BYTES), 0.5)
                await self._send(writer, 503, b"too many connections",
                                 "text/plain")
            with contextlib.suppress(Exception):
                writer.close()
            return
        self._connections += 1
        try:
            await asyncio.wait_for(self._serve_one(reader, writer),
                                   REQUEST_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                                   # noqa: BLE001
            # Deliberately swallowed. See the module docstring: this coroutine
            # is spawned per connection by asyncio, and anything escaping here
            # surfaces as an unretrieved task exception at best -- or, if it
            # ever reached the bridge's own handler, as a stopped ride.
            print(f"  web request failed: {type(exc).__name__}: {exc}",
                  flush=True)
        finally:
            self._connections -= 1
            try:
                writer.close()
            except Exception:                                      # noqa: BLE001
                pass

    async def _serve_one(self, reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter) -> None:
        request = await self._read_request(reader)
        if request is None:
            await self._send(writer, 400, b"bad request", "text/plain")
            return
        status, body, content_type = await self._route(request)
        await self._send(writer, status, body, content_type)

    async def _read_request(self, reader: asyncio.StreamReader) -> Request | None:
        # Read the header block by hand rather than with readuntil(): that
        # raises on overflow with the buffered data already lost, and an
        # oversized header should be a clean 400.
        buffer = bytearray()
        while not _end_of_headers(buffer):
            chunk = await reader.read(1024)
            if not chunk:
                return None
            buffer.extend(chunk)
            if len(buffer) > MAX_HEADER_BYTES:
                return None
        head, rest = _split_headers(bytes(buffer))
        try:
            lines = head.replace(b"\r\n", b"\n").decode("latin-1").split("\n")
            method, target, _version = lines[0].split(" ", 2)
        except (ValueError, IndexError):
            return None

        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()

        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            return None

        body = bytearray(rest)
        while len(body) < length:
            chunk = await reader.read(min(4096, length - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        try:
            return Request(method, target, bytes(body[:length]), headers)
        except ValueError:
            # urlsplit raises on targets like `//[`. Any port scanner reaches
            # this, and it should be the same clean 400 an oversized header
            # block gets, not a dropped connection.
            return None

    async def _send(self, writer: asyncio.StreamWriter, status: int,
                    body: bytes, content_type: str) -> None:
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden",
                  404: "Not Found", 405: "Method Not Allowed",
                  415: "Unsupported Media Type",
                  500: "Internal Server Error",
                  503: "Service Unavailable"}.get(status, "OK")
        header = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            # The page is tuned live and reloaded often; a cached copy showing
            # last session's dials would be its own bug report.
            f"Cache-Control: no-store\r\n"
            # Not optional, and not covered by the three checks above. A hostile
            # page can embed this one in an invisible iframe and lure a tap onto
            # a slider track: the POST that follows is made BY THIS PAGE, so its
            # Host, Origin and Content-Type are all genuinely correct and every
            # guard passes. Clickjacking is the one cross-site path that looks
            # identical to a real user, so it has to be refused at the framing
            # layer instead. This page is never legitimately embedded.
            f"X-Frame-Options: DENY\r\n"
            f"Content-Security-Policy: frame-ancestors 'none'; "
            f"default-src 'self'; style-src 'self' 'unsafe-inline'; "
            f"script-src 'self' 'unsafe-inline'\r\n"
            f"Referrer-Policy: no-referrer\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(header + body)
        await writer.drain()

    # -- routes ------------------------------------------------------------

    def _hostname(self, value: str) -> str:
        """Bare hostname from a Host or Origin header value."""
        value = value.strip().lower()
        if "://" in value:
            value = value.split("://", 1)[1]
        # Strip the port, taking care not to cut an IPv6 literal in half.
        if value.startswith("["):
            value = value.partition("]")[0].lstrip("[")
        elif ":" in value:
            value = value.rsplit(":", 1)[0]
        # `pi-2.local.` is the same name as `pi-2.local`, spelled absolutely.
        return value.rstrip(".")

    def _is_local_host(self, value: str) -> bool:
        host = self._hostname(value)
        if not host:
            return False
        try:
            ipaddress.ip_address(host)
            return True          # a bare IP cannot be a rebound attacker name
        except ValueError:
            pass
        return host in self.allowed_hosts

    def _reject_forgery(self, request: Request) -> tuple[int, bytes, str] | None:
        """None if the request may proceed; an error response if it may not.

        See the module docstring. These three checks are what let the page run
        without a password.
        """
        host = request.headers.get("host", "")
        if not self._is_local_host(host):
            return 403, self._json({
                "error": f"unexpected Host {host!r}. Reach this page by the "
                         "Pi's own name or IP."}), "application/json"

        origin = request.headers.get("origin")
        if origin and self._hostname(origin) != self._hostname(host):
            return 403, self._json({
                "error": "cross-origin request refused"}), "application/json"

        if request.method == "POST":
            # A form can only send urlencoded, multipart or text/plain, and a
            # cross-origin fetch cannot set application/json without a
            # preflight we never answer. So this single check removes the whole
            # simple-request path.
            content_type = request.headers.get("content-type", "")
            if content_type.split(";")[0].strip().lower() != "application/json":
                return 415, self._json({
                    "error": "POST requires Content-Type: application/json"
                }), "application/json"
        return None

    async def _route(self, request: Request) -> tuple[int, bytes, str]:
        refusal = self._reject_forgery(request)
        if refusal is not None:
            return refusal

        if request.path in ("/", "/index.html"):
            if request.method != "GET":
                return 405, b"GET only", "text/plain"
            return self._page()
        if request.path == "/api/state":
            if request.method != "GET":
                return 405, self._json({"error": "GET only"}), "application/json"
            return 200, self._json(self.state()), "application/json"
        if request.path == "/api/config":
            if request.method != "POST":
                return 405, self._json({"error": "POST only"}), "application/json"
            return await self._post_config(request)
        if request.path == "/api/restart":
            if request.method != "POST":
                return 405, self._json({"error": "POST only"}), "application/json"
            return self._post_restart()
        return 404, b"not found", "text/plain"

    def _page(self) -> tuple[int, bytes, str]:
        # Exactly one file is ever served and its path is a constant, so there
        # is no request-controlled path to traverse.
        try:
            with open(PAGE_PATH, "rb") as handle:
                return 200, handle.read(), "text/html; charset=utf-8"
        except OSError as exc:
            return 500, f"cannot read {PAGE_PATH}: {exc}".encode(), "text/plain"

    @staticmethod
    def _json(payload) -> bytes:
        return json.dumps(payload).encode("utf-8")

    async def _post_config(self, request: Request) -> tuple[int, bytes, str]:
        try:
            payload = request.json()
        except DialError as exc:
            return 400, self._json({"error": str(exc)}), "application/json"
        if not payload:
            return 400, self._json({"error": "no values given"}), "application/json"

        # Validate EVERYTHING before applying ANYTHING. A partial application
        # would leave the running config in a state the rider never asked for
        # and no restart-free path back to it.
        coerced: dict[str, object] = {}
        for key, raw in payload.items():
            dial = BY_KEY.get(key)
            if dial is None:
                return 400, self._json(
                    {"error": f"{key} is not a configurable dial"}), "application/json"
            try:
                coerced[key] = dials_module.coerce(dial, raw)
            except DialError as exc:
                return 400, self._json({"error": str(exc)}), "application/json"

        # Cross-field rules, checked against what the config WOULD become. The
        # single-field ranges above cannot see that a new MOVEMENT_MAX has just
        # crossed under the standing MOVEMENT_MIN.
        problems = self._would_break(coerced)
        if problems:
            return 400, self._json({"error": "; ".join(problems)}), "application/json"

        # No await between here and the end of this block: on one event loop
        # that is what makes a batch atomic with respect to Mapper.evaluate(),
        # so a frame can never see half of it.
        live_changes = {}
        for key, value in coerced.items():
            dial = BY_KEY[key]
            if dial.live:
                dials_module.apply(self.config, dial, value)
                live_changes[key] = value

        # The write is a different matter: it is an fsync, and on the Pi's SD
        # card that can take tens of milliseconds. Doing it inline cost output
        # frames and BLE poll slots -- the rider feels the latter as lag. The
        # apply above has already happened, so moving only this off the loop
        # costs nothing in atomicity. The lock serialises the read-modify-write
        # so two overlapping saves cannot lose one another's key.
        persisted, warning = await self._persist(coerced)

        if live_changes:
            self.on_change(live_changes)
        result = self.state()
        result["applied"] = sorted(live_changes)
        result["restart_required"] = sorted(
            k for k in coerced if not BY_KEY[k].live)
        result["persisted"] = persisted
        if warning:
            result["warning"] = warning
        return 200, self._json(result), "application/json"

    def _would_break(self, coerced: dict[str, object]) -> list[str]:
        """Consistency check on a copy, so a rejected POST changes nothing."""
        trial = copy.deepcopy(self.config)
        for key, value in coerced.items():
            dial = BY_KEY[key]
            if dial.live:
                dials_module.apply(trial, dial, value)
        return dials_module.check_consistency(trial)

    async def _persist(self, coerced: dict[str, object]) -> tuple[bool, str | None]:
        if self.config_path is None:
            return False, None
        from .configfile import write_values

        try:
            async with self._save_lock:
                await asyncio.to_thread(write_values, self.config_path, coerced)
            return True, None
        except (OSError, ValueError) as exc:
            # The live change already happened and is real. Say the write
            # failed rather than pretending the whole request did -- the rider
            # needs to know the value will not survive a restart, not to be
            # told nothing changed when the stick just got heavier.
            message = (f"applied, but could not write {self.config_path}: "
                       f"{type(exc).__name__}: {exc}")
            print(f"  {message}", flush=True)
            return False, message

    def _post_restart(self) -> tuple[int, bytes, str]:
        if not os.environ.get("INVOCATION_ID"):
            return 400, self._json({
                "error": "not running under systemd; restart it however you "
                         "started it"}), "application/json"
        try:
            # --no-block: systemctl would otherwise wait for a unit whose
            # restart kills this very process, so the reply would never be
            # written and the page would show a failure for a restart that
            # worked.
            subprocess.Popen(
                ["systemctl", "restart", "--no-block", "bike-bridge"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            return 500, self._json(
                {"error": f"{type(exc).__name__}: {exc}"}), "application/json"
        return 200, self._json({"restarting": True}), "application/json"

    # -- state -------------------------------------------------------------

    def _persisted_values(self) -> dict:
        """What config.env currently says, for the dials we cannot read live.

        Without this the restart-required dials have no value at all, and an
        <input type=range> with no value silently parks at the midpoint of its
        own span -- so FRAME_RATE would show ~500 Hz while the bridge ran at
        60, and one nudge of the slider would save that 500. A control that
        invents its own reading is worse than no control.
        """
        if self.config_path is None:
            return {}
        from .configfile import parse

        try:
            with open(self.config_path, "r", encoding="utf-8",
                      errors="surrogateescape") as handle:
                raw = parse(handle.read())
        except (OSError, ValueError):
            # ValueError covers UnicodeDecodeError. config.env is hand-edited,
            # so one stray non-UTF-8 byte -- a degree sign in a comment -- is
            # entirely possible, and it used to take the whole page down: no
            # response at all to /api/state, so the phone showed "no connection
            # to the bridge" while the bridge itself was perfectly healthy.
            return {}
        values = {}
        for dial in DIALS:
            if dial.live or dial.key not in raw:
                continue
            try:
                values[dial.key] = dials_module.coerce(dial, raw[dial.key])
            except DialError:
                # A hand-edited value we would refuse. Report nothing rather
                # than a number the bridge is not running.
                continue
        return values

    def _restart_dial_values(self) -> dict:
        """What a restart-required dial is SET to -- the rider's saved intent.

        config.env first, because that is what the rider last chose and what
        the next restart will use. self.restart_values only fills the gaps: the
        deployed config.env has no FRAME_RATE line at all, so without a
        fallback the control would have no value and park at the midpoint of
        its own range.

        Deliberately NOT the other way round. Letting the running value win
        meant a restart dial snapped back to its startup number the moment the
        rider changed it -- while config.env quietly kept the new value. The
        page said nothing had happened and the next boot disagreed.
        """
        values = dict(self.restart_values)
        values.update(self._persisted_values())
        return values

    def state(self) -> dict:
        saved = self._restart_dial_values()
        entries = []
        for dial in DIALS:
            value = (dials_module.read(self.config, dial) if dial.live
                     else saved.get(dial.key))
            entries.append({
                "key": dial.key,
                "label": dial.label,
                "kind": dial.kind,
                "min": dial.minimum,
                "max": dial.maximum,
                "step": dial.step,
                "unit": dial.unit,
                "live": dial.live,
                "nullable": dial.nullable,
                "disabled_value": dial.disabled_value,
                "help": dial.help,
                "value": value,
                # What the bridge is running RIGHT NOW, for the dials whose
                # saved value only takes effect on a restart. The page needs
                # both numbers to tell "you changed this, restart to apply"
                # from "this is in force" -- reporting one of them as though it
                # were the other is what made this control lie twice.
                "running": (None if dial.live
                            else self.restart_values.get(dial.key)),
            })
        return {"dials": entries, "status": self._status()}

    def _status(self) -> dict:
        status = self.status
        seen = getattr(status, "bike_seen", None)
        return {
            "bike": getattr(status, "bike", "-"),
            "age": (time.monotonic() - seen) if seen else None,
            "controller": getattr(status, "controller", "none"),
            "cadence": round(getattr(status, "cadence", 0.0), 1),
            "cadence_raw": getattr(status, "cadence_raw", 0),
            "power": round(getattr(status, "power", 0.0)),
            "resistance": getattr(status, "resistance", 0),
            "gate": getattr(status, "gate", False),
            "move": round(getattr(status, "move", 1.0), 3),
            "sprint": getattr(status, "sprint", False),
            "frozen": getattr(status, "frozen", False),
            "hz": round(getattr(status, "hz", 0.0), 2),
            "frames": getattr(status, "frames", 0),
            "uptime": round(time.monotonic() - self.started_at),
        }
