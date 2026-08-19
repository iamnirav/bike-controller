#!/usr/bin/env python3
"""Merge bike telemetry with a real controller into one virtual gamepad.

    sudo ./.venv/bin/python tools/bridge.py --address <bike> --status

Test modes, so each half can be exercised without the other:

    --simulate-bike     sweep cadence 0..95 rpm instead of connecting over BLE
    --no-controller     emit bike-driven input only

Semantics: the gate suppresses a chosen SUBSET of the real controller's inputs
(default: the left stick, i.e. movement). Everything else passes through
unconditionally, so menus and buttons still work while you are stopped.

Bike-driven output (cadence axis, threshold buttons) is the bike's own
contribution and always passes -- it is already zero when you are not
pedalling, so gating it too would be redundant.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import math
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evdev import ecodes as e                                        # noqa: E402

from bike_controller.gamepad import (                                # noqa: E402
    ControllerReader,
    VirtualGamepad,
)
from bike_controller.ridelog import RideLogger                 # noqa: E402
from bike_controller.sequence import (                          # noqa: E402
    EventCodes,
    SequenceDetector,
    Tokenizer,
)
from bike_controller.watchdog import Watchdog                  # noqa: E402
from bike_controller.mapping import (                                # noqa: E402
    AxisConfig,
    ButtonRule,
    GateConfig,
    Mapper,
    MappingConfig,
    MovementConfig,
    stale_after_for,
)

LEFT_STICK = (e.ABS_X, e.ABS_Y)

# evdev's numbers, handed to the (evdev-free) tokenizer so it stays testable
# without a Linux gamepad stack. A stick-direction bug shipped unnoticed
# precisely because that code used to live here, where nothing could test it.
EVENT_CODES = EventCodes(
    ev_key=e.EV_KEY, ev_abs=e.EV_ABS, ev_syn=e.EV_SYN,
    abs_x=e.ABS_X, abs_y=e.ABS_Y,
    abs_hat_x=e.ABS_HAT0X, abs_hat_y=e.ABS_HAT0Y,
)


@dataclasses.dataclass
class Status:
    """Shared state, written by the feed tasks and read by status_loop.

    Typed rather than a dict of bare strings: the previous version accumulated a
    `resistance` key that was written and never read, and every reader repeated
    a default that duplicated the real one.
    """

    bike: str = "-"
    bike_seen: float | None = None      # monotonic time of the last telemetry frame
    controller: str = "none"
    cadence_raw: int = 0
    cadence: float = 0.0
    power: float = 0.0
    gate: bool = False
    move: float = 1.0
    sprint: bool = False
    game_rumble_seen: bool = False
    frames: int = 0                     # telemetry frames since start
    resistance: int = 0                 # raw byte, not the console's displayed level
    frozen: bool = False                # console repeating one reading
    # Set by whichever feed is running; consumed by output_loop, which logs it
    # alongside the mapping derived from that same sample.
    pending_sample: object = None


class ControllerHolder:
    """The controller currently held, or None while we have none.

    Written by feed_from_controller, read by output_loop and the launcher. A
    typed cell rather than a dict so the "do we have a controller, and can it
    buzz?" test lives in one place instead of being re-derived at each call.
    """

    def __init__(self) -> None:
        self.reader: ControllerReader | None = None
        self._rumble_task: asyncio.Task | None = None

    def rumble(self, name: str) -> None:
        if self.can_rumble:
            self.reader.rumbler.play(name)

    def rumble_raw(self, strong: int, weak: int) -> None:
        """Forward the game's own rumble, at its own magnitudes.

        Off the event loop: this is an ioctl plus a write to a wireless USB pad,
        and doing it inline made the same loop that drives BLE polling wait on
        the controller. Measured cost during a firefight: telemetry dropped from
        ~0.50s per sample to ~0.73s, which the rider feels as lag.

        If a write is still in flight the new value is dropped rather than
        queued -- the pad has one pair of motors and only the latest matters.
        """
        if not self.can_rumble:
            return
        if self._rumble_task is not None and not self._rumble_task.done():
            return
        reader = self.reader
        self._rumble_task = asyncio.create_task(
            asyncio.to_thread(reader.rumbler.passthrough, strong, weak)
        )
        # to_thread can fail on its own (loop shutting down, executor
        # exhausted). Nothing else retrieves this task's exception.
        self._rumble_task.add_done_callback(self._rumble_done)

    @staticmethod
    def _rumble_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            print(f"  rumble forward failed: {type(exc).__name__}: {exc}",
                  flush=True)

    async def release(self) -> None:
        """Drop the controller, waiting for any in-flight rumble write first.

        Closing the device while a worker thread is inside upload_effect() or
        write() means it operates on a closing fd -- and if that fd number is
        recycled by the next open(), the worker writes a 24-byte binary
        input_event into whatever took it. A ride CSV, for instance.
        """
        reader, self.reader = self.reader, None
        task = self._rumble_task
        self._rumble_task = None        # or the guard in rumble_raw stays true
                                        # forever and the NEW pad never buzzes
        if task is not None and not task.done():
            # shield so the write completes rather than being cancelled
            # mid-ioctl -- cancelling is how you get the corruption this method
            # exists to prevent.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(task), 2.0)
            if not task.done():
                # The pad is wedged. Closing now would put us back in exactly
                # the use-after-close this method prevents, so deliberately
                # leak the fd instead: one leaked fd per wedged controller is
                # far cheaper than a 24-byte input_event landing in whatever
                # recycles that number. Say so -- silence here is what would
                # make the corruption undiagnosable.
                print("  rumble write still in flight after 2s; leaving the "
                      "controller fd open rather than closing it under a live "
                      "write", flush=True)
                return
        if reader is not None:
            reader.close()

    @property
    def can_rumble(self) -> bool:
        return self.reader is not None and self.reader.rumbler.available


@dataclasses.dataclass(frozen=True)
class Wiring:
    """Everything output_loop needs that never changes after startup."""

    axis_code: int | None
    sprint_code: int | None
    gated_axes: frozenset[int]
    gated_buttons: frozenset[int]
    rumble: bool
    frame_rate: float


# Up Up Down Down Left Right Left Right B A. The d-pad is a hat axis, which is
# discrete (-1/0/1) rather than analog, so unlike the sticks it cannot drift.
KONAMI = [
    ("hat_y", -1), ("hat_y", -1), ("hat_y", 1), ("hat_y", 1),
    ("hat_x", -1), ("hat_x", 1), ("hat_x", -1), ("hat_x", 1),
    ("btn", e.BTN_B), ("btn", e.BTN_A),
]


class Launcher:
    """Runs a command when the rider first touches the controller.

    Triggered by button presses only, never axis movement -- stick drift on a
    resting controller would otherwise fire it unprompted.

    Self-re-arming: it declines to launch while the browser is already up, so
    pressing a button does nothing during a session, but brings Remote Play back
    if it has died. No manual reset needed.
    """

    COOLDOWN = 5.0            # seconds between attempts, so a button mash is one launch
    # Worst legitimate case is ~30s to attach + the launcher's own --timeout.
    # The bridge's liveness must not depend on a subprocess choosing to exit --
    # that layering is what wedged the launcher in the first place.
    # Must exceed remoteplay.py's worst case, or the rider gets a failure buzz
    # for a launch that was still going: 3 attempts x (2s kill + 30s attach +
    # 75s drive + 25s stream check) + 2s ~= 400s. Keep in step with --attempts
    # and --attempt-timeout in tools/start-remoteplay.sh.
    MAX_RUNTIME = 450.0

    def __init__(self, command: list[str] | None, rumble=None) -> None:
        self.command = command
        self.rumble = rumble or (lambda name: None)
        self.launches = 0
        self._task: asyncio.Task | None = None
        self._last_attempt = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.command)

    # Debian ships `chromium`, other distros `chromium-browser`, and
    # remoteplay.py accepts either -- so all the checks must agree or the
    # launcher decides no browser is running and relaunches on every trigger.
    #
    # No -x: pgrep matches /proc/<pid>/comm, which the kernel caps at 15 chars
    # (TASK_COMM_LEN), so "chromium-browser" appears as "chromium-browse" and an
    # exact match could never hit it. A substring match on "chromium" covers both.
    BROWSER_PATTERN = "chromium"

    @classmethod
    def _browser_running(cls) -> bool:
        return subprocess.run(["pgrep", cls.BROWSER_PATTERN],
                              capture_output=True).returncode == 0

    async def _run(self) -> None:
        assert self.command is not None
        # The ack already fired in trigger(), on recognition.
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.command, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                returncode = await asyncio.wait_for(proc.wait(), self.MAX_RUNTIME)
            except asyncio.TimeoutError:
                print(f"  launcher exceeded {self.MAX_RUNTIME:.0f}s; killing it",
                      flush=True)
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
                returncode = -1
        except asyncio.CancelledError:
            raise
        except Exception as exc:                               # noqa: BLE001
            # A missing or non-executable script would otherwise raise into a
            # task nothing awaits: no log, no buzz, and the rider on the bike
            # has no way to tell the difference from a slow launch.
            print(f"  launcher could not start: {type(exc).__name__}: {exc}",
                  flush=True)
            returncode = -1

        if returncode == 0:
            print("  remote play connected", flush=True)
            self.rumble("ok")
        else:
            print(f"  remote play FAILED (exit {returncode})", flush=True)
            # Three short pulses -- unmistakably different from the long success
            # buzz, and readable without looking at anything.
            for _ in range(3):
                self.rumble("ack")
                await asyncio.sleep(0.22)

    def trigger(self) -> None:
        if not self.command:
            return
        now = time.monotonic()
        if now - self._last_attempt < self.COOLDOWN:
            return
        if self._task is not None and not self._task.done():
            return
        self._last_attempt = now

        # Cheap process check, gated by the cooldown so a held button does not
        # spawn a pgrep per frame.
        if self._browser_running():
            print("  konami code entered, but a browser is already running -- "
                  "nothing to launch", flush=True)
            return
        self.launches += 1
        print("  controller input with no browser running -> launching Remote Play",
              flush=True)
        self._task = asyncio.create_task(self._run())

# Output frames per second. Higher means lower controller latency; it also means
# more event-loop wakeups competing with BLE polling, which on a Pi busy
# software-decoding a video stream costs telemetry rate -- and telemetry rate is
# what sets how long after you start pedalling the game starts moving.
DEFAULT_FRAME_RATE = 60.0

AXIS_CHOICES = {
    "none": None,
    "right_trigger": e.ABS_RZ,
    "left_trigger": e.ABS_Z,
    "right_stick_y": e.ABS_RY,
    "left_stick_y": e.ABS_Y,
}

# Which inputs the gate suppresses. Gating only the left stick means menus,
# face buttons and the camera keep working while you are stopped, and only
# MOVEMENT costs you effort -- which plays far better than killing everything.
GATE_TARGETS: dict[str, tuple[set[int], set[int]]] = {
    # name: (axis codes, button codes)
    "left_stick": ({e.ABS_X, e.ABS_Y}, set()),
    "right_stick": ({e.ABS_RX, e.ABS_RY}, set()),
    "triggers": ({e.ABS_Z, e.ABS_RZ}, set()),
    "dpad": ({e.ABS_HAT0X, e.ABS_HAT0Y}, set()),
    "face_buttons": (set(), {e.BTN_A, e.BTN_B, e.BTN_X, e.BTN_Y}),
    "shoulders": (set(), {e.BTN_TL, e.BTN_TR}),
}

# Derived, not hand-written: adding a group above must not silently stop "all"
# meaning all. The extras are buttons no named group covers.
GATE_TARGETS["all"] = (
    {code for axes, _ in GATE_TARGETS.values() for code in axes},
    {code for _, buttons in GATE_TARGETS.values() for code in buttons}
    | {e.BTN_SELECT, e.BTN_START, e.BTN_THUMBL, e.BTN_THUMBR},
)


def resolve_gate_targets(names: list[str]) -> tuple[set[int], set[int]]:
    axes: set[int] = set()
    buttons: set[int] = set()
    for name in names:
        group_axes, group_buttons = GATE_TARGETS[name]
        axes |= group_axes
        buttons |= group_buttons
    return axes, buttons


def parse_button(spec: str) -> ButtonRule:
    """--button 80:BTN_TR  ->  hold BTN_TR while cadence >= 80."""
    rpm, _, name = spec.partition(":")
    if not name:
        raise argparse.ArgumentTypeError(f"expected RPM:BUTTON, got {spec!r}")
    if not hasattr(e, name):
        raise argparse.ArgumentTypeError(f"unknown button {name!r} (try BTN_A, BTN_TR)")
    return ButtonRule(name=name, min_rpm=float(rpm))


async def feed_from_bike(address: str | None, mapper: Mapper, status: Status,
                         poll_interval: float) -> None:
    """Keep a bike link alive, reconnecting indefinitely.

    This must never give up. At boot the console is asleep and unreachable, and
    the link also drops when it sleeps mid-session; in both cases the bridge has
    to reconnect by itself, because the whole point is that this runs unattended.
    """
    from bike_controller.bike import IconBike

    backoff = 3.0
    while True:
        try:
            target = address or await IconBike.discover()
            if target is None:
                status.bike = "searching"
                await asyncio.sleep(backoff)
                continue

            status.bike = "connecting"
            async with IconBike(target, poll_interval=poll_interval) as bike:
                status.bike = "connected"
                backoff = 3.0
                stream = bike.stream()
                while True:
                    # A console that stays connected but stops replying is
                    # invisible to both the disconnect callback and the write
                    # error path, so bound the wait explicitly.
                    #
                    # Deliberately much longer than the mapper's staleness
                    # window: the mapper fails safe in ~1.6s, which is what
                    # protects the rider. This only decides when to spend a
                    # reconnect, which costs a BLE round trip, so it can afford
                    # to be patient. ~26 missed frames at the deployed rate.
                    sample = await asyncio.wait_for(stream.__anext__(), timeout=10.0)
                    status.cadence_raw = sample.cadence_rpm
                    status.resistance = sample.resistance
                    status.bike_seen = time.monotonic()
                    status.frames += 1
                    mapper.submit(sample.cadence_rpm, sample.power_w,
                                  distance=sample.distance_raw)
                    # Handed to the output loop rather than logged here, so the
                    # row's derived values match its raw ones. Calling
                    # mapper.evaluate() here would advance the filter's clock and
                    # steal dt from the loop that actually drives the pad.
                    status.pending_sample = sample
        except asyncio.CancelledError:
            raise
        except (Exception, StopAsyncIteration) as exc:             # noqa: BLE001
            status.bike = "retrying"
            print(f"  bike link down ({type(exc).__name__}: {exc}); "
                  f"retrying in {backoff:.0f}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 1.5)


class _SimSample:
    """Minimal stand-in for BikeState, so simulation can feed the ride log."""

    __slots__ = ("cadence_rpm", "power_w", "resistance", "distance_raw")

    def __init__(self, cadence_rpm: int, power_w: int) -> None:
        self.cadence_rpm, self.power_w = cadence_rpm, power_w
        self.resistance = self.distance_raw = 0


async def feed_simulated(mapper: Mapper, status: Status) -> None:
    """A slow cadence sweep, so gate/axis behaviour is visible in a browser."""
    status.bike = "simulated"
    start = time.monotonic()
    while True:
        phase = (time.monotonic() - start) / 20.0
        cadence = max(0.0, 47.5 * (1 - math.cos(phase * 2 * math.pi)))
        # Mirror the console's own estimate at zero resistance, which we
        # measured as watts = 2 * (cadence - 25).
        power = max(0.0, 2.0 * (cadence - 25.0))
        status.cadence_raw = round(cadence)
        mapper.submit(cadence, power)
        # Handed over the same way the real feed does, so --ride-log is
        # exercised by the smoke run rather than only ever on a real ride.
        status.pending_sample = _SimSample(round(cadence), round(power))
        # Mirror the deployed telemetry rate (~2.56 Hz), not the original
        # ~0.87 Hz -- otherwise the simulator runs closer to the staleness
        # window than the real thing ever does.
        await asyncio.sleep(0.4)


async def feed_from_controller(
    holder: ControllerHolder, status: Status, path_arg: str | None, grab: bool,
    launcher: "Launcher | None" = None,
    detector: SequenceDetector | None = None,
) -> None:
    """Hold a controller open, re-acquiring it if it is unplugged or absent.

    The controller may not be plugged in when this starts, and USB pads get
    unplugged. `holder.reader` is None whenever we have no controller, and
    the output loop treats that as "emit nothing".
    """
    while True:
        # Rebuilt per acquisition: a pad unplugged with the stick deflected
        # would otherwise leave the edge state latched, swallowing the first
        # push on its replacement.
        tokenizer = Tokenizer(EVENT_CODES)
        reader = None
        try:
            path = path_arg or ControllerReader.find()
            if path is None:
                status.controller = "none"
                await asyncio.sleep(3.0)
                continue

            reader = ControllerReader(path, grab=grab)
            holder.reader = reader
            status.controller = reader.device.name
            print(f"  controller acquired: {reader.device.name} at {path}"
                  f" ({'grabbed' if reader.grabbed else 'shared'},"
                  f" haptics {'yes' if reader.rumbler.available else 'no'})", flush=True)

            async for event in reader.device.async_read_loop():
                reader.apply(event)
                if launcher is None:
                    continue
                token = tokenizer.token(event)
                if token is None:
                    continue
                if detector is None:
                    if token[0] == "btn":        # any-button mode
                        launcher.trigger()
                    continue
                was = detector.index
                if detector.feed(token):
                    # Ack RECOGNITION, here, where it happens. The buzz answers
                    # "did it hear me?" -- the only question a rider can ask
                    # from the saddle -- and must fire even when trigger()
                    # declines because a browser is already running, which is
                    # the common case.
                    print("  konami code entered", flush=True)
                    holder.rumble("ack")
                    launcher.trigger()
                elif was >= 1 and detector.index < was:
                    # From step 1, not 3: the failure that actually happens is
                    # a spurious perpendicular token on the FIRST push, which a
                    # threshold of 3 could never report.
                    print(f"  konami progress lost at step {was}/"
                          f"{len(detector.sequence)} (got {token}, "
                          f"expected {detector.sequence[was]})", flush=True)
            raise ConnectionError("controller event stream ended")
        except asyncio.CancelledError:
            await holder.release()
            raise
        except Exception as exc:                                   # noqa: BLE001
            # Drop the reader BEFORE backing off. Its axis/button dict still
            # holds the last state, so leaving it in place would replay
            # stick-forward and held buttons for the whole 2s sleep.
            await holder.release()
            status.controller = "none"
            print(f"  controller lost ({type(exc).__name__}: {exc}); "
                  f"re-acquiring", flush=True)
            await asyncio.sleep(2.0)


async def output_loop(
    pad: VirtualGamepad,
    mapper: Mapper,
    holder: ControllerHolder,
    wiring: Wiring,
    status: Status,
    stop: asyncio.Event,
    watchdog: Watchdog | None = None,
    ride_log: RideLogger | None = None,
) -> None:
    period = 1.0 / wiring.frame_rate
    # Haptics are edge-triggered: fire on the transition, not every frame.
    # mapping.py makes both flags hysteretic so hovering at a threshold does
    # not buzz continuously.
    prev_max = False
    prev_sprint = False
    prev_degraded = False
    last_rumble = 0.0
    while not stop.is_set():
        now = time.monotonic()
        out = mapper.evaluate()
        status.gate = out.gate_open
        status.cadence = out.cadence
        status.move = out.movement_scale
        status.frozen = mapper.is_frozen(now)
        status.sprint = out.sprint
        # Take power from the same evaluate() that produced `move`, not from the
        # feed task -- otherwise the two can be a sample apart and the status
        # line implies a relationship between them that isn't real.
        status.power = out.power

        # Everything starts neutral, so anything we skip below stays released
        # (and sticks stay centred, since 0 is centre for a signed axis).
        pad.neutral()

        reader = holder.reader
        if reader is not None:
            buttons, axes = reader.snapshot()
            blocked = not out.gate_open
            for code, value in buttons.items():
                if blocked and code in wiring.gated_buttons:
                    continue
                pad.set_button(code, bool(value))
            for code, value in axes.items():
                if blocked and code in wiring.gated_axes:
                    continue
                if code in LEFT_STICK:
                    # Uniform scalar on both components: magnitude scales,
                    # direction is preserved exactly.
                    value = int(value * out.movement_scale)
                pad.set_axis(code, value)

        # Bike-driven output always applies.
        if wiring.axis_code is not None and mapper.config.axis.enabled:
            pad.set_axis_normalised(wiring.axis_code, out.axis)
        for name in out.buttons:
            pad.set_button(getattr(e, name), True)
        if out.sprint and wiring.sprint_code is not None:
            pad.set_button(wiring.sprint_code, True)

        if wiring.rumble:
            # Rising edges only. The hysteresis in mapping.py matters more here
            # than it did with paired on/off cues: without it, effort hovering
            # at a threshold would re-fire the SAME cue over and over.
            #
            # Via the holder, not the `reader` local captured above: that local
            # can go stale, and mixing it with a live can_rumble check is the
            # same invisible invariant snapshot() was added to remove.
            if out.at_max and not prev_max:
                holder.rumble("max_on")
            if out.sprint and not prev_sprint:
                holder.rumble("sprint_on")
            # Rising edge only: one buzz when the bike goes away, not a stream.
            if out.degraded and not prev_degraded:
                holder.rumble("fault")
        prev_max, prev_sprint, prev_degraded = out.at_max, out.sprint, out.degraded

        # Serviced every frame: an unanswered FF upload leaves the BROWSER
        # blocked in its ioctl, so this is not optional once the pad advertises
        # force feedback.
        rumbles = pad.poll_rumble()
        # Poll every frame -- an unanswered upload blocks the browser -- but
        # forward at most 20/s. A game can emit effects far faster than a pad
        # can render them, and each forward costs a USB round trip.
        if rumbles:
            if not status.game_rumble_seen:
                status.game_rumble_seen = True
                print("  game rumble received -- passthrough is working end to end",
                      flush=True)
        if rumbles and now - last_rumble >= 0.05:
            last_rumble = now
            # Say so the first time. Whether Remote Play's web client forwards
            # game rumble to the Gamepad API at all is the one link in this
            # chain that cannot be tested without a game running, so make it
            # observable instead of a matter of feel.
            if not status.game_rumble_seen:
                status.game_rumble_seen = True
                print("  game rumble received -- passthrough is working end to end",
                      flush=True)
            # Coalesce: several effects can arrive in one drain, but the pad has
            # one pair of motors and only the strongest is audible.
            holder.rumble_raw(max(s for s, _ in rumbles),
                              max(w for _, w in rumbles))

        # Logged HERE, not from the bike feed: this is the only place that has
        # a telemetry sample and the mapping derived FROM it at the same instant.
        # Logging at submit time recorded each row's movement_scale one sample
        # behind its own cadence and power, which quietly corrupts any analysis.
        if ride_log is not None and status.pending_sample is not None:
            ride_log.log(status.pending_sample, status)
            status.pending_sample = None

        pad.sync()
        # Pinged from HERE, not from a timer: the point is to attest that frames
        # are still being emitted. A ping from anywhere else would keep systemd
        # happy while the pad sat latched at its last values.
        if watchdog is not None:
            watchdog.ping()
        await asyncio.sleep(period)


async def status_loop(status: Status, stop: asyncio.Event) -> None:
    # Telemetry rate measured over the last interval. `age` only ever hinted at
    # this, and the difference between the bridge's rate and pure polling is
    # exactly the lag a rider feels.
    last_frames, last_at = status.frames, time.monotonic()
    while not stop.is_set():
        await asyncio.sleep(1.0)
        now = time.monotonic()
        hz = (status.frames - last_frames) / max(1e-6, now - last_at)
        last_frames, last_at = status.frames, now
        age = (f"{time.monotonic() - status.bike_seen:4.1f}s"
               if status.bike_seen else "  -- ")
        print(
            f"  bike={status.bike:<10} age={age} "
            f"ctrl={status.controller[:22]:<22} "
            f"cadence={status.cadence:5.1f} (raw {status.cadence_raw:>3}) "
            f"gate={'OPEN' if status.gate else 'shut'}"
            f"{' FROZEN' if status.frozen else ''} "
            f"{hz:4.1f}Hz "
            f"pwr={status.power:4.0f} res={status.resistance:2d} "
            f"move={status.move:4.2f}"
            f"{' SPRINT' if status.sprint else ''}",
            flush=True,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--address", help="bike BLE address")
    parser.add_argument("--simulate-bike", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=0.05,
                        help="seconds between BLE poll writes (default 0.05, the "
                             "measured knee). The staleness window is derived "
                             "from this, so raising it also relaxes the fail-safe.")

    parser.add_argument("--controller", help="evdev path; default is autodetect")
    parser.add_argument("--no-controller", action="store_true")
    parser.add_argument("--no-grab", action="store_true",
                        help="do not take the controller exclusively (debugging only)")
    parser.add_argument("--status", action="store_true", help="print state once a second")
    parser.add_argument("--ride-log", metavar="DIR", default=None,
                        help="append ride telemetry to a CSV in DIR (one file "
                             "per run, written only while you are riding)")

    parser.add_argument("--movement", choices=["none", "power", "cadence"],
                        default="none",
                        help="scale the left stick by effort (default: none)")
    parser.add_argument("--movement-min", type=float, default=0.0,
                        help="effort at which movement starts; 0 lets the GAME's "
                             "deadzone (typically 12-24%%) be the threshold")
    parser.add_argument("--movement-max", type=float, default=100.0,
                        help="effort giving full stick deflection (watts or rpm)")
    parser.add_argument("--movement-floor", type=float, default=0.5,
                        help="baseline multiplier you always have, at any effort "
                             "including none (default 0.5). 0 = strict "
                             "pedal-or-nothing.")
    parser.add_argument("--sprint-at", type=float, default=None,
                        help="hold the sprint button at/above this effort")
    parser.add_argument("--sprint-button", default="BTN_THUMBL",
                        help="button held when sprinting (default BTN_THUMBL, "
                             "i.e. left stick click)")

    parser.add_argument("--frozen-after", type=float, default=4.0,
                        help="seconds of bit-identical telemetry before the "
                             "console counts as frozen and movement is zeroed. "
                             "0 disables. (default 4)")
    parser.add_argument("--no-gate", action="store_true")
    parser.add_argument(
        "--gate-inputs", default="left_stick",
        help="comma-separated groups the gate suppresses: "
             + ", ".join(GATE_TARGETS) + " (default: left_stick)",
    )
    parser.add_argument("--gate-open", type=float, default=40.0)
    parser.add_argument("--gate-close", type=float, default=25.0)
    parser.add_argument("--gate-grace", type=float, default=1.5)

    parser.add_argument("--axis", choices=sorted(AXIS_CHOICES), default="none",
                        help="axis driven by cadence (default: none)")
    parser.add_argument("--axis-min", type=float, default=30.0)
    parser.add_argument("--axis-max", type=float, default=90.0)
    parser.add_argument("--button", action="append", type=parse_button, default=[],
                        help="RPM:BUTTON, repeatable (e.g. --button 80:BTN_TR)")

    parser.add_argument("--frame-rate", type=float, default=DEFAULT_FRAME_RATE,
                        help="virtual pad output frames per second "
                             f"(default {DEFAULT_FRAME_RATE:.0f}). Lower frees "
                             "the event loop for BLE polling.")
    parser.add_argument("--no-rumble", action="store_true",
                        help="disable haptic cues on the physical controller")
    parser.add_argument("--rumble-passthrough", action="store_true",
                        help="advertise force feedback on the virtual pad and "
                             "forward the game's rumble to your controller")
    parser.add_argument("--launch-on-input", metavar="CMD", default=None,
                        help="run CMD on the launch trigger, when no browser is "
                             "running (e.g. tools/start-remoteplay.sh)")
    parser.add_argument("--launch-trigger", choices=["konami", "any"],
                        default="konami",
                        help="konami: up up down down left right left right B A; "
                             "any: any button press (default: konami)")
    return parser


@dataclasses.dataclass(frozen=True)
class Settings:
    config: MappingConfig
    wiring: Wiring
    gate_groups: tuple[str, ...]     # resolved names, for display
    notes: tuple[str, ...] = ()      # emitted by print_banner


def build_settings(args, parser: argparse.ArgumentParser) -> Settings:
    """Validate arguments and assemble the runtime configuration."""
    if args.simulate_bike and args.address:
        parser.error("--simulate-bike and --address are mutually exclusive")
    if args.gate_open < args.gate_close:
        parser.error(f"--gate-open ({args.gate_open}) must be >= --gate-close "
                     f"({args.gate_close}); inverted thresholds make the gate chatter")
    if args.axis_max <= args.axis_min:
        parser.error(f"--axis-max ({args.axis_max}) must exceed "
                     f"--axis-min ({args.axis_min})")
    if args.movement_max <= args.movement_min:
        parser.error(f"--movement-max ({args.movement_max}) must exceed "
                     f"--movement-min ({args.movement_min})")
    if not 0.0 <= args.movement_floor < 1.0:
        parser.error("--movement-floor must be in [0.0, 1.0)")
    if args.frozen_after < 0 or 0 < args.frozen_after < 2.5:
        parser.error(
            f"--frozen-after ({args.frozen_after}) must be 0 (disabled) or at "
            "least 2.5. The console legitimately holds its last reading for "
            "about 2s at the end of every pedalling stretch, so anything "
            "shorter zeroes movement every time you stop.")
    if not 5.0 <= args.frame_rate <= 1000.0:
        parser.error(
            f"--frame-rate ({args.frame_rate}) must be between 5 and 1000. "
            "Zero divides by zero; negative spins the loop at 100% CPU (and it "
            "runs at Nice=-10); below the telemetry rate, ride-log rows are "
            "silently dropped.")
    if not 0.0 < args.poll_interval <= 0.5:
        # The staleness window is derived from this, so an unbounded value buys
        # seconds of full-deflection movement from a dead bike. The model behind
        # stale_after_for is only calibrated over 0.02-0.2 anyway.
        parser.error(f"--poll-interval ({args.poll_interval}) must be in (0, 0.5]")
    if not hasattr(e, args.sprint_button):
        parser.error(f"unknown --sprint-button {args.sprint_button!r}")

    movement = MovementConfig(
        enabled=args.movement != "none",
        source=args.movement,
        min_value=args.movement_min,
        max_value=args.movement_max,
        floor=args.movement_floor,
        sprint_at=args.sprint_at,
    )

    groups = [n.strip() for n in args.gate_inputs.split(",") if n.strip()]
    # Scaling supersedes gating for the left stick -- applying both would zero
    # it below the gate threshold AND scale it above, which is just the gate
    # with extra steps. Done on the parsed list, before resolving: an earlier
    # version edited the display string afterwards, so the note printed while
    # the stick stayed gated.
    notes: list[str] = []
    if movement.enabled and "left_stick" in groups:
        groups.remove("left_stick")
        notes.append("Note: --movement is on, so left_stick is no longer gated "
                     "(scaling handles it).")

    try:
        gated_axes, gated_buttons = resolve_gate_targets(groups)
    except KeyError as exc:
        parser.error(f"unknown --gate-inputs group {exc}; "
                     f"choose from {', '.join(GATE_TARGETS)}")

    axis_code = AXIS_CHOICES[args.axis]
    return Settings(
        config=MappingConfig(
            gate=GateConfig(
                enabled=not args.no_gate,
                open_rpm=args.gate_open,
                close_rpm=args.gate_close,
                grace_seconds=args.gate_grace,
            ),
            axis=AxisConfig(
                enabled=axis_code is not None,
                min_rpm=args.axis_min,
                max_rpm=args.axis_max,
            ),
            movement=movement,
            buttons=args.button,
            # Derived, never hand-set: a fixed window silently stops matching
            # the telemetry rate the moment --poll-interval changes.
            stale_after=stale_after_for(args.poll_interval),
            frozen_after=args.frozen_after,
        ),
        wiring=Wiring(
            axis_code=axis_code,
            sprint_code=(getattr(e, args.sprint_button)
                         if args.sprint_at is not None else None),
            gated_axes=frozenset(gated_axes),
            gated_buttons=frozenset(gated_buttons),
            rumble=not args.no_rumble,
            frame_rate=args.frame_rate,
        ),
        gate_groups=tuple(groups),
        notes=tuple(notes),
    )


def print_banner(args, settings: Settings, launcher: "Launcher",
                 detector, pad: VirtualGamepad, mapper: Mapper,
                 watchdog: Watchdog) -> None:
    """Startup summary.

    deploy.sh greps journalctl for these exact lines as its post-restart smoke
    check, so this is a verification surface, not decoration. Each feature gets
    its own if/else: a previous version chained them and reported "Movement
    scaling: off" based on whether a LAUNCH COMMAND was configured.
    """
    config, wiring = settings.config, settings.wiring
    for note in settings.notes:
        print(note)
    print(f"Virtual pad created: {pad.path}")
    print(f"Rumble passthrough: "
          f"{'on' if pad.has_force_feedback else 'off'}")

    if config.gate.enabled and (wiring.gated_axes or wiring.gated_buttons):
        print(f"Gating: {','.join(settings.gate_groups)} "
              f"(open >{config.gate.open_rpm:.0f} rpm, "
              f"close <{config.gate.close_rpm:.0f} rpm, "
              f"grace {config.gate.grace_seconds:.1f}s)")
    else:
        print("Gating: none")

    movement = config.movement
    if movement.enabled:
        floor = f", floor {movement.floor:.2f}" if movement.floor else ""
        print(f"Movement: left stick scaled by {movement.source} "
              f"{movement.min_value:.0f}..{movement.max_value:.0f}{floor}")
    else:
        print("Movement: off")

    if movement.sprint_at is not None:
        print(f"Sprint: {args.sprint_button} at/above {movement.sprint_at:.0f} "
              f"{movement.source} (releases below "
              f"{movement.sprint_at * movement.sprint_release_ratio:.0f})")
    else:
        print("Sprint: off")

    # stale_after governs the gate as well as movement, so it is not part of
    # the movement branch. Read from the tracker: the config value can be None,
    # and duplicating CadenceTracker's default here would let the banner lie.
    print(f"Fail-safe: input zeroes after "
          f"{mapper.tracker.stale_after:.2f}s without telemetry")
    print(f"Freeze guard: "
          f"{f'{args.frozen_after:.1f}s' if args.frozen_after > 0 else 'off'}")
    print(f"Ride log: {args.ride_log or 'off'}")
    print(f"Watchdog: {'supervised' if watchdog.active else 'not supervised'}")
    print(f"Cadence axis: {args.axis}")
    print(f"Haptics: {'on' if wiring.rumble else 'off'}")
    print(f"Frame rate: {wiring.frame_rate:.0f} Hz")

    if launcher.enabled:
        trigger = ("Konami code (up up down down left right left right B A)"
                   if detector else "any button press")
        print(f"Launch trigger: {trigger}")
        print(f"  runs: {args.launch_on_input} (only when no browser is running)")
    else:
        print("Launch trigger: none")


async def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    settings = build_settings(args, parser)
    mapper = Mapper(settings.config)
    status = Status()
    watchdog = Watchdog()
    ride_log = RideLogger(args.ride_log) if args.ride_log else None
    holder = ControllerHolder()
    launcher = Launcher(
        [args.launch_on_input] if args.launch_on_input else None,
        rumble=holder.rumble,
    )
    detector = SequenceDetector(KONAMI) if args.launch_trigger == "konami" else None

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # systemctl stop/restart and shutdown send SIGTERM; without this the
        # whole graceful-shutdown path is dead code in production.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    crashed: list[BaseException] = []

    def on_task_done(task: asyncio.Task) -> None:
        """Never let a task die quietly.

        The tasks list holds a strong reference for the life of the process, so
        asyncio's "exception was never retrieved" warning never fires. A dead
        output_loop would leave the uinput device latched at its last values --
        a stuck-open gate walking a game forever from a stationary bike.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            crashed.append(exc)
            print(f"  FATAL: {type(exc).__name__}: {exc}", flush=True)
            stop.set()

    with VirtualGamepad(force_feedback=args.rumble_passthrough) as pad:
        print_banner(args, settings, launcher, detector, pad, mapper, watchdog)

        tasks = [asyncio.create_task(
            output_loop(pad, mapper, holder, settings.wiring, status, stop,
                        watchdog, ride_log))]
        if args.simulate_bike:
            tasks.append(asyncio.create_task(feed_simulated(mapper, status)))
        else:
            tasks.append(asyncio.create_task(
                feed_from_bike(args.address, mapper, status,
                               args.poll_interval)))
        if not args.no_controller:
            tasks.append(asyncio.create_task(
                feed_from_controller(holder, status, args.controller,
                                     not args.no_grab, launcher, detector)))
        if args.status:
            tasks.append(asyncio.create_task(status_loop(status, stop)))

        for task in tasks:
            task.add_done_callback(on_task_done)

        watchdog.ready()

        print("Running. Ctrl-C to stop.\n")
        await stop.wait()
        watchdog.stopping()

        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    watchdog.close()
    if ride_log is not None:
        ride_log.close()
        if ride_log.path is not None:
            print(f"Ride log: {ride_log.rows} rows -> {ride_log.path}")

    if crashed:
        # Exit non-zero so systemd's Restart=on-failure can actually help.
        print(f"\nExiting after {len(crashed)} task failure(s).")
        return 1
    print("\nStopped; controller released and virtual pad removed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(1)
