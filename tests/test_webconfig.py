"""Tests for the config web server.

Runs with pytest, or standalone:  python3 tests/test_webconfig.py

Driven over a real loopback socket rather than by calling the route methods,
because the things most likely to break are the HTTP edges: a truncated
request, a body that never arrives, a header block with no end. Those cannot be
reached through the Python API at all.

The load-bearing test here is test_malformed_request_does_not_kill_the_server.
bridge.py's on_task_done treats any task exception as fatal, so an exception
escaping a request handler would end the ride. That property is asserted, not
assumed.
"""

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bike_controller.mapping import MappingConfig       # noqa: E402
from bike_controller.webconfig import ConfigServer      # noqa: E402


class FakeStatus:
    """Stands in for bridge.py's Status, which needs evdev to import."""

    bike = "connected"
    bike_seen = None
    controller = "Xbox Wireless Controller"
    cadence_raw = 62
    cadence = 61.4
    power = 78.0
    gate = True
    move = 0.73
    sprint = False
    frames = 412
    resistance = 3
    frozen = False
    hz = 2.56


async def _request(port, method, path, body=None, raw=None, headers=None):
    """One HTTP request over a real socket. `raw` bypasses framing entirely.

    Sends a Host and Content-Type the server will accept by default, since
    almost every test is about something else. The anti-forgery guard has its
    own tests below, which override these.
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    if raw is not None:
        writer.write(raw)
    else:
        payload = b"" if body is None else json.dumps(body).encode()
        sent = {"Host": f"127.0.0.1:{port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload))}
        sent.update(headers or {})
        head = (f"{method} {path} HTTP/1.1\r\n"
                + "".join(f"{k}: {v}\r\n" for k, v in sent.items())
                + "\r\n").encode()
        writer.write(head + payload)
    await writer.drain()
    response = await asyncio.wait_for(reader.read(-1), 5.0)
    writer.close()
    if not response:
        return 0, b""        # connection closed with no reply
    head, _, payload = response.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, payload


def serve(coro_factory, config=None, config_path=None, restart_values=None):
    """Start a server on an ephemeral port, run the test body, shut it down.

    Records anything asyncio reports as an unhandled exception. Without this,
    a handler that raised would still leave the server accepting connections,
    so "is it still serving?" alone cannot see the failure that matters -- and
    in the bridge that same escape is a stopped ride.
    """
    config = config or MappingConfig()
    escaped = []

    async def runner():
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: escaped.append(context))
        server = ConfigServer(config, FakeStatus(), config_path,
                              restart_values=restart_values)
        assert await server.start("127.0.0.1", 0), "server failed to bind"
        port = server._server.sockets[0].getsockname()[1]
        task = asyncio.create_task(server.serve())
        try:
            return await coro_factory(port, server)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            # Let any doomed handler task be collected and reported before the
            # loop closes, so `escaped` is complete by the assert below.
            await asyncio.sleep(0)

    result = asyncio.run(runner())
    assert not escaped, (
        "an exception escaped a request handler: "
        f"{escaped[0].get('message')} / {escaped[0].get('exception')!r}. "
        "In the bridge, on_task_done would turn that into a stopped ride.")
    return result


# --- reading state ---------------------------------------------------------

def test_state_reports_the_running_config():
    config = MappingConfig()
    config.movement.floor = 0.42

    async def body(port, _server):
        status, payload = await _request(port, "GET", "/api/state")
        assert status == 200
        state = json.loads(payload)
        by_key = {d["key"]: d for d in state["dials"]}
        assert by_key["MOVEMENT_FLOOR"]["value"] == 0.42
        # Restart-required dials have no live value to report.
        assert by_key["POLL_INTERVAL"]["live"] is False
        assert by_key["POLL_INTERVAL"]["value"] is None
        assert state["status"]["move"] == 0.73
        assert state["status"]["hz"] == 2.56

    serve(body, config)


def test_state_reports_persisted_values_for_restart_dials():
    """They have no live value, and inventing one is how the page lied.

    An <input type=range> with no value parks at the midpoint of its own span,
    so FRAME_RATE showed ~500 Hz while the bridge ran at 60 -- and one nudge
    saved that 500.
    """
    handle = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
    handle.write("FRAME_RATE=60\nPOLL_INTERVAL=0.02\nRUMBLE_PASSTHROUGH=1\n")
    handle.close()

    async def body(port, _server):
        _, payload = await _request(port, "GET", "/api/state")
        by_key = {d["key"]: d for d in json.loads(payload)["dials"]}
        assert by_key["FRAME_RATE"]["value"] == 60
        assert by_key["POLL_INTERVAL"]["value"] == 0.02
        assert by_key["RUMBLE_PASSTHROUGH"]["value"] is True
        # Absent from the file, so genuinely unknown -- not a made-up number.
        assert by_key["RIDE_LOG"]["value"] is None

    try:
        serve(body, None, handle.name)
    finally:
        os.unlink(handle.name)


def test_state_carries_the_disabled_value():
    """The page needs it to tell "freeze guard off" from "freeze guard 0.0s"."""

    async def body(port, _server):
        _, payload = await _request(port, "GET", "/api/state")
        by_key = {d["key"]: d for d in json.loads(payload)["dials"]}
        assert by_key["FROZEN_AFTER"]["disabled_value"] == 0.0
        assert by_key["MOVEMENT_FLOOR"]["disabled_value"] is None
        assert by_key["SPRINT_AT"]["nullable"] is True

    serve(body)


def test_page_is_served():
    async def body(port, _server):
        status, payload = await _request(port, "GET", "/")
        assert status == 200
        assert b"<html" in payload.lower()
        # Self-contained: nothing to fetch from the internet at ride time.
        assert b"http://" not in payload and b"https://" not in payload

    serve(body)


def test_unknown_path_is_404():
    async def body(port, _server):
        status, _ = await _request(port, "GET", "/nope")
        assert status == 404

    serve(body)


def test_wrong_method_is_405():
    async def body(port, _server):
        status, _ = await _request(port, "GET", "/api/config")
        assert status == 405

    serve(body)


# --- applying changes ------------------------------------------------------

def test_post_applies_live_and_is_visible_next_read():
    config = MappingConfig()

    async def body(port, _server):
        status, payload = await _request(
            port, "POST", "/api/config", {"MOVEMENT_FLOOR": 0.35})
        assert status == 200, payload
        result = json.loads(payload)
        assert result["applied"] == ["MOVEMENT_FLOOR"]
        assert result["restart_required"] == []
        # The live config object the Mapper reads every frame.
        assert config.movement.floor == 0.35
        _, payload = await _request(port, "GET", "/api/state")
        by_key = {d["key"]: d for d in json.loads(payload)["dials"]}
        assert by_key["MOVEMENT_FLOOR"]["value"] == 0.35

    serve(body, config)


def test_restart_only_dial_is_reported_not_applied():
    async def body(port, _server):
        status, payload = await _request(
            port, "POST", "/api/config", {"FRAME_RATE": 30})
        assert status == 200
        result = json.loads(payload)
        assert result["applied"] == []
        assert result["restart_required"] == ["FRAME_RATE"]

    serve(body)


def test_out_of_range_is_refused_and_changes_nothing():
    config = MappingConfig()
    config.movement.floor = 0.5

    async def body(port, _server):
        status, payload = await _request(
            port, "POST", "/api/config", {"MOVEMENT_FLOOR": 5.0})
        assert status == 400
        assert "MOVEMENT_FLOOR" in json.loads(payload)["error"]
        assert config.movement.floor == 0.5, "a rejected POST changed the config"

    serve(body, config)


def test_unknown_key_is_refused():
    async def body(port, _server):
        status, payload = await _request(
            port, "POST", "/api/config", {"XBOX_CONSOLE_ID": "hijack"})
        assert status == 400
        assert "not a configurable dial" in json.loads(payload)["error"]

    serve(body)


def test_cross_field_break_is_refused_against_the_standing_config():
    """A new MOVEMENT_MAX can cross under the MIN that is already set.

    No per-dial range check can see that; only a trial application can.
    """
    config = MappingConfig()
    config.movement.min_value = 80.0
    config.movement.max_value = 130.0

    async def body(port, _server):
        status, payload = await _request(
            port, "POST", "/api/config", {"MOVEMENT_MAX": 50.0})
        assert status == 400
        assert "MOVEMENT_MAX" in json.loads(payload)["error"]
        assert config.movement.max_value == 130.0

    serve(body, config)


def test_a_batch_is_all_or_nothing():
    """One bad value in a batch must not leave the others half-applied."""
    config = MappingConfig()
    config.movement.floor = 0.5

    async def body(port, _server):
        status, _ = await _request(
            port, "POST", "/api/config",
            {"MOVEMENT_FLOOR": 0.3, "FROZEN_AFTER": 1.0})
        assert status == 400
        assert config.movement.floor == 0.5

    serve(body, config)


def test_change_is_persisted_to_config_env():
    config = MappingConfig()
    handle = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
    handle.write("# a comment\nMOVEMENT_FLOOR=0.5\nXBOX_CONSOLE_ID=SECRET\n")
    handle.close()

    async def body(port, _server):
        status, payload = await _request(
            port, "POST", "/api/config", {"MOVEMENT_FLOOR": 0.31})
        assert status == 200
        assert json.loads(payload)["persisted"] is True
        text = open(handle.name).read()
        assert "MOVEMENT_FLOOR=0.31" in text
        assert "# a comment" in text
        assert "XBOX_CONSOLE_ID=SECRET" in text

    try:
        serve(body, config, handle.name)
    finally:
        os.unlink(handle.name)


def test_failed_persistence_still_reports_the_live_change():
    """The stick really did get heavier; saying nothing happened would lie."""
    config = MappingConfig()

    async def body(port, _server):
        status, payload = await _request(
            port, "POST", "/api/config", {"MOVEMENT_FLOOR": 0.31})
        assert status == 200
        result = json.loads(payload)
        assert result["persisted"] is False
        assert "warning" in result
        assert config.movement.floor == 0.31

    serve(body, config, "/nonexistent-directory/config.env")


# --- anti-forgery: what stands in for a password ---------------------------

def test_cross_site_post_is_refused():
    """The attack the allowlist alone does not stop.

    Any page on any LAN device can send this. It stays entirely inside the
    allowlist and the ranges, and would set the dead-bike movement scale to
    full deflection with the freeze guard off.
    """
    config = MappingConfig()

    async def body(port, _server):
        status, _ = await _request(
            port, "POST", "/api/config",
            {"MOVEMENT_FLOOR": 0.99, "FROZEN_AFTER": 0},
            headers={"Host": "evil.example", "Origin": "http://evil.example",
                     "Content-Type": "text/plain"})
        assert status == 403
        assert config.movement.floor == 0.5
        assert config.frozen_after == 4.0

    serve(body, config)


def test_dns_rebinding_host_is_refused():
    """The attacker's own name resolving to the Pi. Only Host can see this."""

    async def body(port, _server):
        status, _ = await _request(port, "GET", "/api/state",
                                   headers={"Host": "evil.example"})
        assert status == 403

    serve(body)


def test_cross_origin_with_valid_host_is_refused():
    async def body(port, _server):
        status, _ = await _request(
            port, "POST", "/api/config", {"MOVEMENT_FLOOR": 0.3},
            headers={"Origin": "http://evil.example"})
        assert status == 403

    serve(body)


def test_form_content_type_is_refused():
    """A <form> can only send these, and cannot set application/json."""
    for content_type in ("application/x-www-form-urlencoded", "text/plain",
                         "multipart/form-data", ""):
        async def body(port, _server, ct=content_type):
            status, _ = await _request(
                port, "POST", "/api/config", {"MOVEMENT_FLOOR": 0.3},
                headers={"Content-Type": ct})
            assert status == 415, f"{ct!r} was accepted"

        serve(body)


def test_restart_endpoint_is_behind_the_same_guard():
    """It takes no body, so nothing else would stop a cross-site form."""

    async def body(port, _server):
        status, _ = await _request(
            port, "POST", "/api/restart",
            headers={"Host": "evil.example", "Content-Type": "text/plain"})
        assert status == 403

    serve(body)


def test_same_origin_request_is_allowed():
    """The real page must still work, and so must curl."""

    async def body(port, _server):
        status, _ = await _request(
            port, "POST", "/api/config", {"MOVEMENT_FLOOR": 0.3},
            headers={"Origin": f"http://127.0.0.1:{port}"})
        assert status == 200
        # No Origin at all, by IP -- i.e. curl.
        status, _ = await _request(port, "GET", "/api/state")
        assert status == 200

    serve(body)


def test_localhost_and_hostname_are_accepted():
    import socket as _socket
    short = _socket.gethostname().split(".")[0]

    async def body(port, _server):
        for host in ("localhost", f"localhost:{port}", "127.0.0.1",
                     short, f"{short}.local:8080", "[::1]:8080"):
            status, _ = await _request(port, "GET", "/api/state",
                                       headers={"Host": host})
            assert status == 200, f"{host} was refused"

    serve(body)


def test_page_refuses_to_be_framed():
    """The one cross-site path the Host/Origin/Content-Type checks cannot see.

    Embedded in a hostile iframe, a lured tap on a slider track makes the REAL
    page send the POST -- so Host, Origin and Content-Type are all genuinely
    correct and every guard passes. It has to be refused at the framing layer.
    """
    import http.client

    async def body(port, _server):
        def check():
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/", None, {"Host": f"127.0.0.1:{port}"})
            response = conn.getresponse()
            response.read()
            assert response.getheader("X-Frame-Options") == "DENY"
            csp = response.getheader("Content-Security-Policy") or ""
            assert "frame-ancestors 'none'" in csp, csp
            conn.close()

        await asyncio.to_thread(check)

    serve(body)


def test_a_non_utf8_config_env_does_not_take_the_page_down():
    """config.env is hand-edited; one stray byte used to kill /api/state.

    The bridge stayed healthy while the phone showed "no connection" forever,
    and a POST applied live but silently failed to persist.
    """
    handle = tempfile.NamedTemporaryFile("wb", suffix=".env", delete=False)
    handle.write("# floor \xb0 in latin-1\nMOVEMENT_FLOOR=0.5\nFRAME_RATE=60\n"
                 .encode("latin-1"))
    handle.close()
    config = MappingConfig()

    async def body(port, _server):
        status, payload = await _request(port, "GET", "/api/state")
        assert status == 200, "the page died on a non-UTF-8 config.env"
        by_key = {d["key"]: d for d in json.loads(payload)["dials"]}
        assert by_key["FRAME_RATE"]["value"] == 60

        status, payload = await _request(
            port, "POST", "/api/config", {"MOVEMENT_FLOOR": 0.4})
        assert status == 200
        assert json.loads(payload)["persisted"] is True
        assert config.movement.floor == 0.4
        # The odd byte survived the rewrite.
        assert b"\xb0" in open(handle.name, "rb").read()

    try:
        serve(body, config, handle.name)
    finally:
        os.unlink(handle.name)


def test_restart_dials_report_saved_and_running_separately():
    """A restart dial has two values and needs both.

    `value` is what it is SET to (config.env -- the rider's saved intent, and
    what the next boot will use). `running` is what this process is actually
    using. Collapsing them into one number made this control lie in both
    directions: first claiming the file's value was in force, then snapping the
    rider's own edit back to the startup value while still saving it.
    """
    handle = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
    handle.write("FRAME_RATE=30\n")          # saved, but not what is running
    handle.close()

    async def body(port, _server):
        _, payload = await _request(port, "GET", "/api/state")
        by_key = {d["key"]: d for d in json.loads(payload)["dials"]}
        assert by_key["FRAME_RATE"]["value"] == 30, "saved value was overridden"
        assert by_key["FRAME_RATE"]["running"] == 120, "running value lost"
        # Not in config.env at all, so the saved value falls back to what is
        # running -- rather than nothing, which parks the slider at its midpoint.
        assert by_key["POLL_INTERVAL"]["value"] == 0.02
        assert by_key["POLL_INTERVAL"]["running"] == 0.02

    try:
        serve(body, None, handle.name,
              restart_values={"FRAME_RATE": 120, "POLL_INTERVAL": 0.02})
    finally:
        os.unlink(handle.name)


def test_a_changed_restart_dial_does_not_snap_back():
    """The blocker: the page denied a change it had already written to disk.

    The rider drags POLL_INTERVAL, the thumb returns to its old value within
    500 ms, so they conclude nothing happened -- and config.env now says
    otherwise, widening the fail-safe window on the next boot.
    """
    handle = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
    handle.write("POLL_INTERVAL=0.02\nRIDE_LOG=1\n")
    handle.close()

    async def body(port, _server):
        status, _ = await _request(port, "POST", "/api/config",
                                   {"POLL_INTERVAL": 0.2, "RIDE_LOG": False})
        assert status == 200
        _, payload = await _request(port, "GET", "/api/state")
        by_key = {d["key"]: d for d in json.loads(payload)["dials"]}
        assert by_key["POLL_INTERVAL"]["value"] == 0.2, "the change snapped back"
        assert by_key["RIDE_LOG"]["value"] is False, "the switch flipped back"
        # ...while still reporting honestly what is actually in force.
        assert by_key["POLL_INTERVAL"]["running"] == 0.02
        assert by_key["RIDE_LOG"]["running"] is True

    try:
        serve(body, None, handle.name,
              restart_values={"POLL_INTERVAL": 0.02, "RIDE_LOG": True})
    finally:
        os.unlink(handle.name)


def test_connection_cap_answers_and_then_recovers():
    """The cap had no coverage at all: both it and its decrement could be
    deleted silently. A leaked counter wedges the server permanently."""
    from bike_controller import webconfig as module

    original = module.MAX_CONNECTIONS
    module.MAX_CONNECTIONS = 3
    try:
        async def body(port, server):
            held = []
            for _ in range(module.MAX_CONNECTIONS):
                held.append(await asyncio.open_connection("127.0.0.1", port))
                await asyncio.sleep(0.02)
            assert server._connections == module.MAX_CONNECTIONS
            # One over the cap: an HTTP error, not a dropped socket -- a bare
            # RST reads on the phone as "the bridge is down".
            status, _ = await _request(port, "GET", "/api/state")
            assert status == 503
            for _, writer in held:
                writer.close()
            for _ in range(50):
                await asyncio.sleep(0.02)
                if server._connections == 0:
                    break
            assert server._connections == 0, "the connection counter leaked"
            status, _ = await _request(port, "GET", "/api/state")
            assert status == 200

        serve(body)
    finally:
        module.MAX_CONNECTIONS = original


def test_concurrent_saves_do_not_lose_a_key():
    """Persistence is a read-modify-write on a thread; overlapping POSTs of
    different keys must not clobber one another."""
    handle = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
    handle.write("MOVEMENT_FLOOR=0.5\nMOVEMENT_MAX=75\nFROZEN_AFTER=4\n")
    handle.close()

    async def body(port, _server):
        results = await asyncio.gather(
            _request(port, "POST", "/api/config", {"MOVEMENT_FLOOR": 0.31}),
            _request(port, "POST", "/api/config", {"MOVEMENT_MAX": 90}),
            _request(port, "POST", "/api/config", {"FROZEN_AFTER": 6}),
        )
        assert [r[0] for r in results] == [200, 200, 200]
        from bike_controller.configfile import parse
        values = parse(open(handle.name).read())
        assert values["MOVEMENT_FLOOR"] == "0.31", values
        assert values["MOVEMENT_MAX"] == "90", values
        assert values["FROZEN_AFTER"] == "6", values

    try:
        serve(body, MappingConfig(), handle.name)
    finally:
        os.unlink(handle.name)


def test_malformed_request_target_gets_a_400():
    """urlsplit raises on `//[`; any port scanner reaches it."""

    async def body(port, _server):
        status, _ = await _request(
            port, None, None,
            raw=b"GET //[ HTTP/1.1\r\nHost: localhost\r\n\r\n")
        assert status == 400

    serve(body)


def test_absolute_fqdn_host_is_accepted():
    """`pi-2.local.` is the same name, spelled absolutely."""
    import socket as _socket
    short = _socket.gethostname().split(".")[0]

    async def body(port, _server):
        status, _ = await _request(port, "GET", "/api/state",
                                   headers={"Host": f"{short}.local.:8080"})
        assert status == 200

    serve(body)


# --- HTTP framing a real browser depends on --------------------------------

def test_responses_parse_under_a_strict_http_client():
    """The hand-rolled response framing, checked by something that cares.

    The socket helper above reads to EOF and splits on the first blank line,
    so it would not notice a wrong Content-Length or a missing header.
    """
    import http.client

    async def body(port, _server):
        def check():
            for method, path, payload in (
                ("GET", "/", None),
                ("GET", "/api/state", None),
                ("GET", "/nope", None),
                ("POST", "/api/config", json.dumps({"MOVEMENT_FLOOR": 0.3})),
            ):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request(method, path, payload,
                             {"Content-Type": "application/json"})
                response = conn.getresponse()
                data = response.read()
                declared = response.getheader("Content-Length")
                assert declared is not None, f"{path} sent no Content-Length"
                assert int(declared) == len(data), (
                    f"{path}: Content-Length {declared} != {len(data)} bytes")
                assert response.getheader("Connection") == "close"
                conn.close()

        await asyncio.to_thread(check)

    serve(body)


def test_lf_only_request_gets_an_answer():
    """Not conformant, but hanging for 15s reads as broken rather than picky."""

    async def body(port, _server):
        status, _ = await _request(
            port, None, None,
            raw=b"GET /api/state HTTP/1.1\nHost: localhost\n\n")
        assert status == 200

    serve(body)


def test_a_stalled_request_is_dropped_not_held_forever():
    """A half-open connection must not pin a handler task for the session."""
    from bike_controller import webconfig as module

    original = module.REQUEST_TIMEOUT
    module.REQUEST_TIMEOUT = 0.3            # the real 15s is untestable
    try:
        async def body(port, _server):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            # A header block that never terminates.
            writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n")
            await writer.drain()
            data = await asyncio.wait_for(reader.read(-1), 5.0)
            assert data == b"", "expected the stalled request to be dropped"
            writer.close()
            status, _ = await _request(port, "GET", "/api/state")
            assert status == 200

        serve(body)
    finally:
        module.REQUEST_TIMEOUT = original


# --- the property the ride depends on --------------------------------------

def test_malformed_request_does_not_kill_the_server():
    """bridge.py treats any task exception as fatal, so this must not raise.

    Each of these used to be a plausible way for a phone browser -- or a port
    scanner -- to end a ride.
    """
    host = b"Host: localhost\r\nContent-Type: application/json\r\n"
    garbage = [
        b"\r\n\r\n",                                  # no request line
        b"GET\r\n\r\n",                               # request line with no target
        b"POST /api/config HTTP/1.1\r\n" + host + b"Content-Length: nope\r\n\r\n",
        b"POST /api/config HTTP/1.1\r\n" + host + b"Content-Length: -5\r\n\r\n",
        b"POST /api/config HTTP/1.1\r\n" + host + b"Content-Length: 999999999\r\n\r\n",
        b"\x00\x01\x02\xff\r\n\r\n",                  # not text at all
        b"POST /api/config HTTP/1.1\r\n" + host + b"Content-Length: 4\r\n\r\n{bad",
    ]

    async def body(port, _server):
        for raw in garbage:
            try:
                await _request(port, None, None, raw=raw)
            except (asyncio.IncompleteReadError, ConnectionError):
                pass          # a closed connection is a fine answer; a crash is not
        # Still serving, which is the whole point.
        status, _ = await _request(port, "GET", "/api/state")
        assert status == 200

    serve(body)


def test_client_vanishing_mid_response_does_not_raise():
    """A phone navigating away closes the socket while we are still writing.

    On a LAN the response fits in the socket buffer, so this does not in fact
    reach a closed pipe today -- it is a robustness test, not a test of the
    except clause (see test_a_raising_handler_does_not_kill_the_server for
    that). It stays because a rider locking their screen mid-poll is the most
    common thing that will ever happen to this server.
    """

    async def body(port, _server):
        for _ in range(5):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET / HTTP/1.1\r\nHost: t\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            # Gone before the response is written.
            writer.transport.abort()
            writer.close()
            await asyncio.sleep(0.02)
        status, _ = await _request(port, "GET", "/api/state")
        assert status == 200

    serve(body)


def test_oversized_header_block_is_refused_not_buffered():
    async def body(port, _server):
        raw = (b"GET / HTTP/1.1\r\nHost: localhost\r\nX-Pad: "
               + b"a" * 20000 + b"\r\n\r\n")
        try:
            status, _ = await _request(port, None, None, raw=raw)
            assert status == 400
        except ConnectionError:
            pass
        status, _ = await _request(port, "GET", "/api/state")
        assert status == 200

    serve(body)


def test_a_raising_handler_does_not_kill_the_server():
    """The safety net itself, tested directly.

    Nothing reachable over a socket currently raises inside a handler -- which
    is the point of the validation, but also means the blanket except would go
    untested and could be "cleaned up" by someone who could not find a case for
    it. There is one: bridge.py's on_task_done treats any escaped exception as
    fatal, so a handler that raises would end the ride rather than drop a
    request. This forces that path.
    """

    async def body(port, server):
        original = server._route

        def exploding(request):
            raise RuntimeError("handler blew up")

        server._route = exploding
        try:
            try:
                await _request(port, "GET", "/api/state")
            except (ConnectionError, asyncio.IncompleteReadError):
                pass          # no response is acceptable; a dead server is not
        finally:
            server._route = original
        # Still serving the very next request.
        status, payload = await _request(port, "GET", "/api/state")
        assert status == 200
        assert json.loads(payload)["status"]["move"] == 0.73

    serve(body)


def test_bind_failure_is_survivable():
    """A port already in use costs you the page, not the ride."""

    async def runner():
        first = ConfigServer(MappingConfig(), FakeStatus(), None)
        assert await first.start("127.0.0.1", 0)
        port = first._server.sockets[0].getsockname()[1]
        second = ConfigServer(MappingConfig(), FakeStatus(), None)
        assert await second.start("127.0.0.1", port) is False
        assert second.bound is None
        first._server.close()

    asyncio.run(runner())


if __name__ == "__main__":
    from _runner import main          # noqa: E402 - script-mode only
    main(globals())
