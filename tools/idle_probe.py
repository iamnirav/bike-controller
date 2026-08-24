#!/usr/bin/env python3
"""Characterise -- and try to defeat -- the console's telemetry blackout.

    sudo systemctl stop bike-bridge          # only one BLE client at a time
    ./.venv/bin/python tools/idle_probe.py --poke none     # baseline first
    ./.venv/bin/python tools/idle_probe.py --poke init-tail

WHAT THE BLACKOUT IS (established from ride logs and probe-output.txt, before
this tool existed -- see README "The idle blackout"):

Stop pedalling and within one sample the console reports cadence 0, power 0 AND
distance 0. It is not a BLE dropout: notifications keep arriving on schedule,
they just carry zeros. It is not the console powering down either -- the
distance accumulator is preserved and comes back where it left off. The console
simply stops *reporting* for a fixed window of roughly 4.5 seconds, and
pedalling during that window does not shorten it.

The evidence that pedalling is ignored rather than unmeasured: across four ride
logs, blackouts of 4.1-5.0s show distance jumping 10-43 counts from the sample
before to the sample after, which is a continuously-pedalling amount. Genuine
rests show +2 or +3.

WHAT THIS TOOL ADDS:

1. It records EVERY notification, not just the 0x31 telemetry frame the bridge
   keeps. The console also sends a `01 12 14 ...` frame whose byte 11 is a
   state flag -- 0x02 while live, 0x03 while blacked out. That is a direct read
   of the state the bridge currently has to infer from zeros.

2. It can POKE the console mid-blackout and measure whether the blackout ends
   sooner than the baseline. One poke per run; compare summaries.

RIDING SCRIPT (do this 5-6 times per run, and keep every run identical):

    pedal steadily ~15s -> stop dead -> count 2 -> pedal HARD and CONTINUOUSLY
    until the numbers come back -> keep pedalling 10s

Stopping dead and then pedalling hard is what makes the trial readable: the
distance delta across the blackout then proves you were pedalling through it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bike_controller import IconBike                      # noqa: E402
from bike_controller.bike import INIT_SEQUENCE, _b        # noqa: E402

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
        poke = ""
        if self.poked_at is not None:
            after = "" if self.end is None else f", cleared {self.end - self.poked_at:.2f}s later"
            poke = f"  poked at +{self.poked_at - self.start:.2f}s{after}"
        return f"  #{self.index:<2d} {dur}  {verdict}{poke}"


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
    # Everything reads zero before the first pedal stroke, which is not a
    # blackout -- it is a bike nobody is sitting on. Wait for real data.
    seen_live = False

    print(f"Connecting to {address} ...")
    async with IconBike(address, poll_interval=args.interval, on_raw=on_raw) as bike:
        print(
            f"Connected. poke={args.poke} "
            f"({len(packets)} packet{'s' if len(packets) != 1 else ''}), "
            f"fires {args.poke_after:.1f}s into each blackout.\n"
            "Pedal ~15s, stop dead, count 2, then pedal HARD until it comes back.\n"
            "Ctrl-C when you have 5-6 trials.\n"
        )
        started = time.monotonic()

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

            if blacked_out and seen_live and current is None:
                current = Trial(len(trials) + 1, now, last_live_distance)
                trials.append(current)
                print(f"  --- blackout #{current.index} began ---")
                if packets:
                    async def fire(trial: Trial = current) -> None:
                        await asyncio.sleep(args.poke_after)
                        # The blackout may have ended while we waited; poking
                        # after recovery measures nothing and confuses the log.
                        if trial.end is None:
                            trial.poked_at = time.monotonic()
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
        "--poke-after", type=float, default=1.0,
        help="seconds into the blackout before poking (default 1.0)",
    )
    parser.add_argument(
        "--out", default=None,
        help="write every raw notification here as JSONL, for offline analysis",
    )
    args = parser.parse_args()
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
