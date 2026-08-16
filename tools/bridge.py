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
import dataclasses
import contextlib
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
from bike_controller.sequence import SequenceDetector          # noqa: E402
from bike_controller.mapping import (                                # noqa: E402
    AxisConfig,
    ButtonRule,
    GateConfig,
    Mapper,
    MappingConfig,
    MovementConfig,
)

LEFT_STICK = (e.ABS_X, e.ABS_Y)


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
    axis: float = 0.0
    gate: bool = False
    move: float = 1.0
    sprint: bool = False


@dataclasses.dataclass(frozen=True)
class Wiring:
    """Everything output_loop needs that never changes after startup."""

    axis_code: int | None
    sprint_code: int | None
    gated_axes: frozenset
    gated_buttons: frozenset
    rumble: bool


# Up Up Down Down Left Right Left Right B A. The d-pad is a hat axis, which is
# discrete (-1/0/1) rather than analog, so unlike the sticks it cannot drift.
KONAMI = [
    ("hat_y", -1), ("hat_y", -1), ("hat_y", 1), ("hat_y", 1),
    ("hat_x", -1), ("hat_x", 1), ("hat_x", -1), ("hat_x", 1),
    ("btn", e.BTN_B), ("btn", e.BTN_A),
]


def event_token(event) -> tuple | None:
    """Reduce an evdev event to a comparable token, or None if uninteresting."""
    if event.type == e.EV_KEY and event.value == 1:
        return ("btn", event.code)
    if event.type == e.EV_ABS and event.value != 0:
        if event.code == e.ABS_HAT0Y:
            return ("hat_y", 1 if event.value > 0 else -1)
        if event.code == e.ABS_HAT0X:
            return ("hat_x", 1 if event.value > 0 else -1)
    return None


class Launcher:
    """Runs a command when the rider first touches the controller.

    Triggered by button presses only, never axis movement -- stick drift on a
    resting controller would otherwise fire it unprompted.

    Self-re-arming: it declines to launch while the browser is already up, so
    pressing a button does nothing during a session, but brings Remote Play back
    if it has died. No manual reset needed.
    """

    COOLDOWN = 5.0            # seconds between attempts, so a button mash is one launch

    def __init__(self, command: list[str] | None, rumble=None) -> None:
        self.command = command
        self.rumble = rumble or (lambda name: None)
        self.launches = 0
        self._task: asyncio.Task | None = None
        self._last_attempt = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.command)

    # Debian ships `chromium`; other distros ship `chromium-browser`, and
    # remoteplay.py accepts either. All three checks must agree, or the launcher
    # decides no browser is running and relaunches on every trigger.
    BROWSER_PATTERN = "chromium|chromium-browser"

    @classmethod
    def _browser_running(cls) -> bool:
        return subprocess.run(["pgrep", "-x", cls.BROWSER_PATTERN],
                              capture_output=True).returncode == 0

    async def _run(self) -> None:
        assert self.command is not None
        # Immediate acknowledgement: the launch takes tens of seconds, and
        # without this you cannot tell whether the code registered.
        self.rumble("ack")
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.command, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            returncode = await proc.wait()
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
            return
        self.launches += 1
        print("  controller input with no browser running -> launching Remote Play",
              flush=True)
        self._task = asyncio.create_task(self._run())

FRAME_RATE = 60.0

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
                         poll_interval: float = 0.2) -> None:
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
                    # error path, so bound the wait explicitly. Telemetry
                    # arrives at ~0.87 Hz; 10s is ~9 missed frames.
                    sample = await asyncio.wait_for(stream.__anext__(), timeout=10.0)
                    status.cadence_raw = sample.cadence_rpm
                    status.bike_seen = time.monotonic()
                    mapper.submit(sample.cadence_rpm, sample.power_w)
        except asyncio.CancelledError:
            raise
        except (Exception, StopAsyncIteration) as exc:             # noqa: BLE001
            status.bike = "retrying"
            print(f"  bike link down ({type(exc).__name__}: {exc}); "
                  f"retrying in {backoff:.0f}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 1.5)


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
        await asyncio.sleep(1.0)          # match the real console's ~1 Hz rate


async def feed_from_controller(
    holder: dict, status: Status, path_arg: str | None, grab: bool,
    launcher: "Launcher | None" = None,
    detector: SequenceDetector | None = None,
) -> None:
    """Hold a controller open, re-acquiring it if it is unplugged or absent.

    The controller may not be plugged in when this starts, and USB pads get
    unplugged. `holder["reader"]` is None whenever we have no controller, and
    the output loop treats that as "emit nothing".
    """
    while True:
        reader = None
        try:
            path = path_arg or ControllerReader.find()
            if path is None:
                status.controller = "none"
                await asyncio.sleep(3.0)
                continue

            reader = ControllerReader(path, grab=grab)
            holder["reader"] = reader
            status.controller = reader.device.name
            print(f"  controller acquired: {reader.device.name} at {path}"
                  f" ({'grabbed' if reader.grabbed else 'shared'},"
                  f" haptics {'yes' if reader.rumbler.available else 'no'})", flush=True)

            async for event in reader.device.async_read_loop():
                reader.apply(event)
                if launcher is None:
                    continue
                token = event_token(event)
                if token is None:
                    continue
                if detector is None:
                    if token[0] == "btn":        # any-button mode
                        launcher.trigger()
                elif detector.feed(token):
                    print("  konami code entered", flush=True)
                    launcher.trigger()
            raise ConnectionError("controller event stream ended")
        except asyncio.CancelledError:
            holder["reader"] = None
            if reader is not None:
                reader.close()
            raise
        except Exception as exc:                                   # noqa: BLE001
            # Drop the reader BEFORE backing off. Its axis/button dict still
            # holds the last state, so leaving it in place would replay
            # stick-forward and held buttons for the whole 2s sleep.
            holder["reader"] = None
            if reader is not None:
                reader.close()
            status.controller = "none"
            print(f"  controller lost ({type(exc).__name__}: {exc}); "
                  f"re-acquiring", flush=True)
            await asyncio.sleep(2.0)


async def output_loop(
    pad: VirtualGamepad,
    mapper: Mapper,
    holder: dict,
    wiring: Wiring,
    status: Status,
    stop: asyncio.Event,
) -> None:
    period = 1.0 / FRAME_RATE
    # Haptics are edge-triggered: fire on the transition, not every frame.
    # mapping.py makes both flags hysteretic so hovering at a threshold does
    # not buzz continuously.
    prev_max = False
    prev_sprint = False
    while not stop.is_set():
        out = mapper.evaluate()
        status.gate = out.gate_open
        status.cadence = out.cadence
        status.axis = out.axis
        status.move = out.movement_scale
        status.sprint = out.sprint
        # Take power from the same evaluate() that produced `move`, not from the
        # feed task -- otherwise the two can be a sample apart and the status
        # line implies a relationship between them that isn't real.
        status.power = out.power

        # Everything starts neutral, so anything we skip below stays released
        # (and sticks stay centred, since 0 is centre for a signed axis).
        pad.neutral()

        reader = holder.get("reader")
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

        if wiring.rumble and reader is not None and reader.rumbler.available:
            # Rising edges only. The hysteresis in mapping.py matters more here
            # than it did with paired on/off cues: without it, effort hovering
            # at a threshold would re-fire the SAME cue over and over.
            if out.at_max and not prev_max:
                reader.rumbler.play("max_on")
            if out.sprint and not prev_sprint:
                reader.rumbler.play("sprint_on")
        prev_max, prev_sprint = out.at_max, out.sprint

        pad.sync()
        await asyncio.sleep(period)


async def status_loop(status: Status, stop: asyncio.Event) -> None:
    while not stop.is_set():
        await asyncio.sleep(1.0)
        age = (f"{time.monotonic() - status.bike_seen:4.1f}s"
               if status.bike_seen else "  -- ")
        print(
            f"  bike={status.bike:<10} age={age} "
            f"ctrl={status.controller[:22]:<22} "
            f"cadence={status.cadence:5.1f} (raw {status.cadence_raw:>3}) "
            f"gate={'OPEN' if status.gate else 'shut'} "
            f"pwr={status.power:4.0f} move={status.move:4.2f}"
            f"{' SPRINT' if status.sprint else ''}",
            flush=True,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--address", help="bike BLE address")
    parser.add_argument("--simulate-bike", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=0.2,
                        help="seconds between BLE poll writes (default 0.2). "
                             "Lower means fresher telemetry; see README for the "
                             "measured trade-off.")

    parser.add_argument("--controller", help="evdev path; default is autodetect")
    parser.add_argument("--no-controller", action="store_true")
    parser.add_argument("--no-grab", action="store_true",
                        help="do not take the controller exclusively (debugging only)")
    parser.add_argument("--status", action="store_true", help="print state once a second")

    parser.add_argument("--movement", choices=["none", "power", "cadence"],
                        default="none",
                        help="scale the left stick by effort (default: none)")
    parser.add_argument("--movement-min", type=float, default=0.0,
                        help="effort at which movement starts; 0 lets the GAME's "
                             "deadzone (typically 12-24%%) be the threshold")
    parser.add_argument("--movement-max", type=float, default=100.0,
                        help="effort giving full stick deflection (watts or rpm)")
    parser.add_argument("--movement-floor", type=float, default=0.0,
                        help="minimum scale once above --movement-min (0 = pure scaling)")
    parser.add_argument("--sprint-at", type=float, default=None,
                        help="hold the sprint button at/above this effort")
    parser.add_argument("--sprint-button", default="BTN_THUMBL",
                        help="button held when sprinting (default BTN_THUMBL, "
                             "i.e. left stick click)")

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

    parser.add_argument("--no-rumble", action="store_true",
                        help="disable haptic cues on the physical controller")
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
    gate_groups: list        # resolved names, for display


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
    if movement.enabled and "left_stick" in groups:
        groups.remove("left_stick")
        print("Note: --movement is on, so left_stick is no longer gated "
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
        ),
        wiring=Wiring(
            axis_code=axis_code,
            sprint_code=(getattr(e, args.sprint_button)
                         if args.sprint_at is not None else None),
            gated_axes=frozenset(gated_axes),
            gated_buttons=frozenset(gated_buttons),
            rumble=not args.no_rumble,
        ),
        gate_groups=groups,
    )


def print_banner(args, settings: Settings, launcher: "Launcher",
                 detector, pad: VirtualGamepad) -> None:
    """Startup summary.

    deploy.sh greps journalctl for these exact lines as its post-restart smoke
    check, so this is a verification surface, not decoration. Each feature gets
    its own if/else: a previous version chained them and reported "Movement
    scaling: off" based on whether a LAUNCH COMMAND was configured.
    """
    config, wiring = settings.config, settings.wiring
    print(f"Virtual pad created: {pad.ui.device.path}")

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

    print(f"Cadence axis: {args.axis}")
    print(f"Haptics: {'on' if wiring.rumble else 'off'}")

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
    holder: dict = {"reader": None}

    def rumble(name: str) -> None:
        reader = holder.get("reader")
        if reader is not None and reader.rumbler.available:
            reader.rumbler.play(name)

    launcher = Launcher(
        [args.launch_on_input] if args.launch_on_input else None, rumble=rumble
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

    with VirtualGamepad() as pad:
        print_banner(args, settings, launcher, detector, pad)

        tasks = [asyncio.create_task(
            output_loop(pad, mapper, holder, settings.wiring, status, stop))]
        if args.simulate_bike:
            tasks.append(asyncio.create_task(feed_simulated(mapper, status)))
        else:
            tasks.append(asyncio.create_task(
                feed_from_bike(args.address, mapper, status, args.poll_interval)))
        if not args.no_controller:
            tasks.append(asyncio.create_task(
                feed_from_controller(holder, status, args.controller,
                                     not args.no_grab, launcher, detector)))
        if args.status:
            tasks.append(asyncio.create_task(status_loop(status, stop)))

        for task in tasks:
            task.add_done_callback(on_task_done)

        print("Running. Ctrl-C to stop.\n")
        await stop.wait()

        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

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
