#!/usr/bin/env python3
"""Characterise -- and try to defeat -- the console's telemetry blackout.

    sudo systemctl stop bike-bridge          # only one BLE client at a time
    ./.venv/bin/python tools/idle_probe.py --poke none     # baseline first
    ./.venv/bin/python tools/idle_probe.py --poke init-tail

WHAT HAPPENS WHEN YOU STOP, in two phases (measured from four ride logs and
probe-output.txt, and confirmed against the console's own beeps):

  Phase 1  FREEZE     ~2.0s   The console keeps reporting your last live
                              cadence and power. Distance stops advancing --
                              that is how you tell this apart from riding.
                              Ends with the console's "stopping" beep.

  Phase 2  BLACKOUT   ~4.5s   cadence 0, power 0, distance 0. Pedalling during
                              this is IGNORED: it neither shortens the window
                              nor extends it. Ends with the "starting" beep.

Neither phase is a BLE dropout and neither is the console powering down.
Notifications keep arriving on schedule throughout, and the distance
accumulator is preserved across the whole thing and resumes where it left off.
The console is still measuring. It stops *reporting*.

The evidence that phase 2 discards pedalling: across four ride logs, blackouts
of 4.1-5.0s show distance jumping 10-43 counts from the sample before to the
sample after, which is a continuously-turning wheel. Genuine rests show +2 or
+3. The freeze phase measured 1.66-2.44s over 34 of those (median 2.00s).

Note for the bridge: during phase 1 it is feeding the game stale full
deflection for two seconds after the rider has stopped. FROZEN_AFTER is 4s, so
the freeze guard never fires on it.

WHAT THIS TOOL ADDS:

1. It records EVERY notification, not just the 0x31 telemetry frame the bridge
   keeps. The console also sends a `01 12 14 ...` frame whose byte 11 is a
   state flag -- 0x02 while live, 0x03 while blacked out. That is a direct read
   of the state the bridge currently has to infer from zeros.

2. It can POKE the console and measure whether that shortens the window. One
   poke per run; compare summaries against a --poke none baseline.
   --poke-on blackout fires inside phase 2 and asks "can this be cut short?".
   --poke-on freeze fires inside phase 1 and asks the better question: "can
   the console be stopped from entering phase 2 at all?".

3. --analyse reads a capture back and reports which bytes of which frames moved
   during a blackout the rider pedalled through. A byte that tracks pedalling
   while the 0x31 frame reads zero would be the whole fix, with nothing written
   to the console at all.

RIDING SCRIPT (do this 5-6 times per run, and keep every run identical):

    pedal steadily ~15s -> stop dead -> WAIT FOR THE STOPPING BEEP -> pedal
    HARD and CONTINUOUSLY until the numbers come back -> 10s more

The wait is the part that is easy to get wrong. The console does not blank the
moment you stop -- phase 1 runs for two seconds first. Resume inside phase 1
and the trial measures nothing, because there was no blackout to pedal
through. Wait for the stopping beep -- or for this tool to print "blackout #N
began", which is the same instant -- and only then pedal.

Pedalling hard through it is what makes the trial readable: the distance delta
across the window then proves the wheel was turning the whole time. A trial
where you sat still is reported as unusable rather than averaged in.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bike_controller import IconBike                      # noqa: E402
from bike_controller.bike import (                        # noqa: E402
    CADENCE_OFFSET, DISTANCE_OFFSET, INIT_SEQUENCE, POWER_OFFSET,
    TELEMETRY_PREFIX, TELEMETRY_SUBTYPE, _b,
)

# The `01 12 14` frame. Byte 11 is the state flag; bytes 7/12/16 carry a
# once-per-second counter that advances while live and freezes while blacked
# out. Identified by diffing riding frames against idle frames in
# probe-output.txt, so treat the names as well-evidenced hypotheses.
STATE_PREFIX = bytes([0x01, 0x12, 0x14])
STATE_OFFSET = 11
STATE_LIVE = 0x02
STATE_BLACKED_OUT = 0x03
COUNTER_OFFSET = 7

# Candidate pokes. All are writes to 0x1534, which is the only characteristic
# the console takes commands on.
POKES: dict[str, list[bytes]] = {
    # Baseline: change nothing, just measure the natural blackout length.
    "none": [],

    # The last packet of the startup handshake, on its own. Cheapest possible
    # nudge and has no physical side effect.
    "init-tail": [_b(0xFE, 0x02, 0x2C, 0x04)],

    # The whole 13-packet handshake. Note it spans 5.2s at the console's
    # required 400ms spacing, which is LONGER than the blackout -- so this
    # tests "does re-init clear it", not "does re-init clear it in time".
    "init-full": list(INIT_SEQUENCE),

    # Packets from qdomyos-zwift's `generic` poll variant that our gx27 cycle
    # never sends. Harmless requests; the question is whether any of them draws
    # a frame that still carries live data during the blackout.
    "poll-generic": [
        _b(0xFE, 0x02, 0x19, 0x03),
        _b(0x00, 0x12, 0x02, 0x04, 0x02, 0x15, 0x07, 0x15, 0x02, 0x00,
           0x0F, 0xBC, 0x90, 0x70, 0x00, 0x00, 0x00, 0x40, 0x00, 0x00),
        _b(0xFF, 0x07, 0x00, 0x00, 0x00, 0x10, 0x00, 0x08, 0x5D),
    ],

    # A real resistance command (qdomyos-zwift forceResistance, level 3). Note
    # the shape: identical to our poll packet POLL_SEQUENCE[4] except byte 9 is
    # 0x01 rather than 0x00 -- 0x01 is "set", 0x00 is "query". This is the only
    # poke here that asserts rider intent at the console, which is why it is
    # worth testing, but IT REALLY DOES CHANGE RESISTANCE TO LEVEL 3 and you
    # will feel it.
    "resistance": [
        _b(0xFF, 0x0D, 0x02, 0x04, 0x02, 0x09, 0x07, 0x09, 0x02, 0x01,
           0x04, 0x14, 0x07, 0x00, 0x32),
    ],
}


class Trial:
    """One blackout, from the first zero sample to the first live one back."""

    def __init__(self, index: int, start: float, distance_before: int) -> None:
        self.index = index
        self.start = start
        self.distance_before = distance_before
        self.end: float | None = None
        self.distance_after: int | None = None
        self.cadence_after: int | None = None
        self.poked_at: float | None = None
        self.poke_phase: str | None = None
        self.freeze_start: float | None = None

    @property
    def duration(self) -> float | None:
        return None if self.end is None else self.end - self.start

    @property
    def distance_delta(self) -> int | None:
        if self.distance_after is None:
            return None
        return self.distance_after - self.distance_before

    def describe(self) -> str:
        dur = "running" if self.duration is None else f"{self.duration:5.2f}s"
        delta = self.distance_delta
        # +2/+3 is a coast-down remainder; anything larger means the wheel was
        # turning throughout, so the trial actually tested what we think.
        if delta is None:
            verdict = ""
        elif delta <= 4:
            verdict = f"dist +{delta:<3d} (rested -- trial not usable)"
        else:
            verdict = f"dist +{delta:<3d} (pedalled through it)"
        freeze = ""
        if self.freeze_start is not None:
            freeze = f"  freeze {self.start - self.freeze_start:.2f}s"
        poke = ""
        if self.poked_at is not None:
            offset = self.poked_at - self.start
            # A freeze-phase poke lands BEFORE the blackout starts, so its
            # offset is negative. Say so rather than printing "+-1.20s".
            where = (f"{offset:.2f}s into the blackout" if offset >= 0
                     else f"{-offset:.2f}s before it, in the freeze")
            after = "" if self.end is None else f", cleared {self.end - self.poked_at:.2f}s later"
            poke = f"  poked {where}{after}"
        return f"  #{self.index:<2d} {dur}  {verdict}{freeze}{poke}"


async def run(args: argparse.Namespace, trials: list[Trial]) -> int:
    address = args.address or await IconBike.discover()
    if address is None:
        print("No Icon bike found. Run tools/scan.py.")
        return 1

    packets = POKES[args.poke]
    out = open(args.out, "w") if args.out else None
    frames: list[tuple[float, bytes]] = []

    def on_raw(t: float, data: bytes) -> None:
        frames.append((t, data))
        if out is not None:
            out.write(json.dumps({"t": round(t, 4), "hex": data.hex()}) + "\n")

    state_flag: int | None = None
    current: Trial | None = None
    last_live_distance = 0
    poke_task: asyncio.Task | None = None
    # Phase 1: cadence still reads live but distance has stopped advancing.
    # Two consecutive unchanged samples, not one -- at ~2.5Hz and ~6 counts/s
    # distance moves every sample while riding, but a single repeat is within
    # the console's rounding and would fire this constantly.
    freeze_start: float | None = None
    unchanged = 0
    last_change: float | None = None
    # A freeze-phase poke fires before the trial it belongs to exists, so park
    # it here and attach it when the blackout actually begins.
    pending_poke: float | None = None
    # Everything reads zero before the first pedal stroke, which is not a
    # blackout -- it is a bike nobody is sitting on. Wait for real data.
    seen_live = False

    print(f"Connecting to {address} ...")
    async with IconBike(address, poll_interval=args.interval, on_raw=on_raw) as bike:
        print(
            f"Connected. poke={args.poke} "
            f"({len(packets)} packet{'s' if len(packets) != 1 else ''}), "
            f"fires {args.poke_after:.1f}s into each {args.poke_on}.\n"
            "Pedal ~15s, stop dead, WAIT for the beep (or for the blackout line\n"
            "below), and only then pedal HARD until the numbers come back.\n"
            "Ctrl-C when you have 5-6 trials.\n"
        )
        started = time.monotonic()

        async def fire_freeze() -> None:
            """Poke inside phase 1, betting the console has not yet decided."""
            nonlocal pending_poke
            await asyncio.sleep(args.poke_after)
            pending_poke = time.monotonic()
            print(f"      poke ({args.poke}) sent during freeze")
            await poke()

        async def poke() -> None:
            for i, packet in enumerate(packets):
                await bike.send(packet)
                # The handshake only works at the console's own 400ms spacing;
                # a burst is ignored. Harmless for the single-packet pokes.
                if i + 1 < len(packets):
                    await asyncio.sleep(0.4)

        async for sample in bike.stream():
            now = time.monotonic()

            # Read the state flag off the most recent `01 12 14` frame.
            for t, data in reversed(frames[-12:]):
                if len(data) == 20 and data[:3] == STATE_PREFIX:
                    state_flag = data[STATE_OFFSET]
                    break

            blacked_out = sample.power_w == 0 and sample.cadence_rpm == 0
            if not blacked_out:
                seen_live = True

            # Track phase 1 so it can be poked, and so each trial can report it.
            if not blacked_out and seen_live:
                if sample.distance_raw == last_live_distance:
                    unchanged += 1
                    if unchanged == 2 and freeze_start is None:
                        # The freeze began when distance last MOVED, not when we
                        # noticed. Samples arrive every ~0.4s (one 0x31 frame per
                        # five-packet poll cycle), not every --interval.
                        freeze_start = last_change if last_change is not None else now
                        print("  --- freeze began (stopping beep due in ~2s) ---")
                        if packets and args.poke_on == "freeze":
                            poke_task = asyncio.create_task(fire_freeze())
                else:
                    unchanged = 0
                    last_change = now
                    if freeze_start is not None:
                        # Pedalling resumed inside phase 1: no blackout follows,
                        # so there is nothing to measure and nothing to cancel.
                        freeze_start = None
                        pending_poke = None
                        if poke_task is not None:
                            poke_task.cancel()
                            poke_task = None

            if blacked_out and seen_live and current is None:
                current = Trial(len(trials) + 1, now, last_live_distance)
                current.freeze_start = freeze_start
                if pending_poke is not None:
                    current.poked_at, current.poke_phase = pending_poke, "freeze"
                freeze_start = None
                pending_poke = None
                unchanged = 0
                trials.append(current)
                print(f"  --- blackout #{current.index} began ---")
                if packets and args.poke_on == "blackout":
                    async def fire(trial: Trial = current) -> None:
                        await asyncio.sleep(args.poke_after)
                        # The blackout may have ended while we waited; poking
                        # after recovery measures nothing and confuses the log.
                        if trial.end is None:
                            trial.poked_at = time.monotonic()
                            trial.poke_phase = "blackout"
                            print(f"      poke ({args.poke}) sent")
                            await poke()
                    poke_task = asyncio.create_task(fire())

            elif not blacked_out:
                if current is not None:
                    current.end = now
                    current.distance_after = sample.distance_raw
                    current.cadence_after = sample.cadence_rpm
                    print(f"  --- blackout #{current.index} ended ---")
                    print(current.describe())
                    if poke_task is not None:
                        poke_task.cancel()
                        poke_task = None
                    current = None
                last_live_distance = sample.distance_raw

            flag = "?" if state_flag is None else f"0x{state_flag:02x}"
            mark = "BLACKOUT" if blacked_out else "        "
            print(
                f"{now - started:7.2f}  cad {sample.cadence_rpm:>3}  "
                f"pw {sample.power_w:>4}W  res {sample.resistance:>2}  "
                f"dist {sample.distance_raw:>5}  state {flag}  {mark}"
            )

    if out is not None:
        out.close()
    return 0


def frame_shape(data: bytes) -> str:
    """A stable key for "frames of this kind", for grouping bytes to compare."""
    if len(data) == 20:
        return f"{data[0]:02x}·{data[1]:02x}·sub{data[5]:02x}"
    return f"{data[:2].hex()} (len {len(data)})"


class Window:
    """One blackout, reconstructed from a capture rather than watched live."""

    def __init__(self, t0: float, t1: float, dist_before: int, dist_after: int) -> None:
        self.t0, self.t1 = t0, t1
        self.dist_before, self.dist_after = dist_before, dist_after

    @property
    def duration(self) -> float:
        return self.t1 - self.t0

    @property
    def distance_delta(self) -> int:
        return self.dist_after - self.dist_before

    @property
    def pedalled_through(self) -> bool:
        """+2/+3 is a coast-down remainder. More means the wheel kept turning."""
        return self.distance_delta > 4


def blackout_windows(frames: list[tuple[float, bytes]]) -> list[Window]:
    """Every blackout that both began and ENDED inside the capture.

    A blackout still running when the capture stopped has no recovery to
    measure and no distance-after to judge it by, so it is not a window.
    """
    live = [(t, d) for t, d in frames
            if len(d) == 20 and d[:4] == TELEMETRY_PREFIX and d[5] == TELEMETRY_SUBTYPE]
    windows: list[Window] = []
    start: float | None = None
    before: int | None = None
    for t, d in live:
        zero = (d[CADENCE_OFFSET] == 0
                and struct.unpack_from("<H", d, POWER_OFFSET)[0] == 0)
        dist = struct.unpack_from("<H", d, DISTANCE_OFFSET)[0]
        if zero:
            if start is None:
                start = t
        else:
            if start is not None and before is not None:
                windows.append(Window(start, t, before, dist))
            start = None
            before = dist
    return windows


def moving_bytes(group: list[bytes]) -> list[str]:
    """Byte positions that took more than one value across these frames."""
    if not group:
        return []
    moving = []
    for i in range(max(len(d) for d in group)):
        values = {d[i] for d in group if len(d) > i}
        if len(values) > 1:
            moving.append(f"{i}({min(values)}-{max(values)})")
    return moving


def read_capture(path: str) -> list[tuple[float, bytes]]:
    frames = []
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if line:
                record = json.loads(line)
                frames.append((record["t"], bytes.fromhex(record["hex"])))
    return frames


def analyse(path: str) -> int:
    """Report which bytes moved during a blackout the rider pedalled through.

    The bridge reads cadence and power out of the 0x31 frame, and the console
    zeroes that frame for the whole of phase 2. But it sends other frames in the
    same poll cycle. If any byte of any of them still tracks the cranks while
    0x31 reads zero, the bridge can read THAT instead and the blackout stops
    mattering -- no writes, no protocol guessing, no fighting the console.
    """
    frames = read_capture(path)
    if not frames:
        print(f"{path}: empty capture")
        return 1

    windows = blackout_windows(frames)
    pedalled = [w for w in windows if w.pedalled_through]
    print(f"{path}: {len(frames)} frames, {len(windows)} blackout(s), "
          f"{len(pedalled)} pedalled through")
    if not pedalled:
        print("\n  No pedalled-through blackout in this capture, so there is nothing"
              "\n  to look for a surviving signal in. Re-run and pedal HARD from the"
              "\n  stopping beep until the numbers return.")
        return 1

    for index, window in enumerate(pedalled, 1):
        print(f"\n=== blackout {index}: {window.duration:.2f}s, "
              f"distance +{window.distance_delta} (wheel was turning) ===")
        inside: dict[str, list[bytes]] = {}
        for t, d in frames:
            # Half-open on purpose: t1 is the LIVE frame that ended the
            # blackout. Include it and the 0x31 frame always reads as "moved"
            # -- it jumps from zero back to real values -- which is the one
            # answer we already know and would drown the ones we do not.
            if window.t0 <= t < window.t1:
                inside.setdefault(frame_shape(d), []).append(d)
        for key in sorted(inside):
            group = inside[key]
            moving = moving_bytes(group)
            head = f"  {key:<20} n={len(group):<4}"
            print(f"{head} {'moved: ' + ', '.join(moving) if moving else 'flat'}")

    print("\nA byte that moves here is a candidate. Read it across the WHOLE capture"
          "\nbefore believing it: a once-per-second tick and a stopwatch also move,"
          "\nand neither tells you anything about the cranks. What you want is a byte"
          "\nthat is flat while resting and moves while pedalling.")
    return 0


def summarise(trials: list[Trial], poke: str, out: str | None) -> None:
    usable = [t for t in trials if t.duration is not None
              and t.distance_delta is not None and t.distance_delta > 4]
    print(f"\n=== {len(trials)} blackout(s), poke={poke} ===")
    for trial in trials:
        print(trial.describe())
    if usable:
        lengths = sorted(t.duration for t in usable)
        mid = lengths[len(lengths) // 2]
        print(f"\n  {len(usable)} usable (pedalled through): "
              f"min {lengths[0]:.2f}s  median {mid:.2f}s  max {lengths[-1]:.2f}s")
        print("  Compare this median against the `--poke none` baseline.")
    else:
        print("\n  No usable trials: every blackout had a coast-down-sized distance"
              "\n  delta, meaning you were resting rather than pedalling through it.")
    if out:
        print(f"\n  Raw frames: {out}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure the console's telemetry blackout and try to shorten it."
    )
    parser.add_argument("--address")
    parser.add_argument(
        "--interval", type=float, default=0.05,
        help="poll spacing, seconds (default 0.05, matching the deployed bridge)",
    )
    parser.add_argument(
        "--poke", choices=sorted(POKES), default="none",
        help="what to write mid-blackout (default none, which is the baseline)",
    )
    parser.add_argument(
        "--poke-on", choices=("blackout", "freeze"), default="blackout",
        help="which phase to poke in: 'blackout' asks whether phase 2 can be cut "
             "short, 'freeze' asks whether the console can be kept out of it",
    )
    parser.add_argument(
        "--poke-after", type=float, default=None,
        help="seconds into the chosen phase before poking. Defaults to 1.0 for "
             "--poke-on blackout and 0.2 for freeze, because the freeze is only "
             "~2.0s long and detecting it has already spent ~0.8s of that",
    )
    parser.add_argument(
        "--out", default=None,
        help="write every raw notification here as JSONL, for offline analysis",
    )
    parser.add_argument(
        "--analyse", metavar="FILE",
        help="read a capture back and report which bytes survive a blackout, "
             "then exit. Does not touch the bike.",
    )
    args = parser.parse_args()
    if args.poke_after is None:
        # Phase 2 is ~4.5s, so 1.0s in leaves plenty to watch. Phase 1 is ~2.0s
        # and two samples have gone before it is even recognised, so the same
        # delay there would land the poke on the beep -- after the console has
        # already decided, which is the one thing this mode exists to beat.
        args.poke_after = 1.0 if args.poke_on == "blackout" else 0.2
    if args.analyse:
        return analyse(args.analyse)
    sys.stdout.reconfigure(line_buffering=True)

    # Owned here, not inside run(), so the summary still prints on Ctrl-C --
    # which is how every run actually ends.
    trials: list[Trial] = []
    try:
        return asyncio.run(run(args, trials))
    except KeyboardInterrupt:
        return 0
    finally:
        summarise(trials, args.poke, args.out)


if __name__ == "__main__":
    sys.exit(main())
