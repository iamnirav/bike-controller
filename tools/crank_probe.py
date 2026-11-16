#!/usr/bin/env python3
"""Find a Bluetooth cadence sensor and watch it, before trusting it to a ride.

    ./.venv/bin/python tools/crank_probe.py --list          # what is out there
    ./.venv/bin/python tools/crank_probe.py                 # nearest sensor
    ./.venv/bin/python tools/crank_probe.py --address AA:BB:CC:DD:EE:FF

Safe to run while the bridge is up, as long as the bridge is not using the
sensor itself (the "Cadence sensor" dial is off): these sensors take one
connection at a time, and the bike is a different device.

WHAT TO LOOK FOR

Every notification prints a line. "turn" marks a new revolution; "same" is the
sensor repeating itself while the crank is still. What matters is how soon after
you START pedalling again the first "turn" line appears -- that is the delay the
bridge will have coming out of the console's pause. The tool cannot see your
legs, so watch the screen and judge it, several times over:

    pedal ~10s -> stop for ~5s -> start again, watching for the first "turn"

Under about a second at a normal cadence is what this whole approach depends
on. The first turn after a rest is marked with how long the rest was. Also note whether the sensor goes completely silent when you stop (fine),
and how long after a long rest it takes to come back (it may have to wake up).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bike_controller.cadence_sensor import CadenceSensor, NoCrankData, scan  # noqa: E402
from bike_controller.csc import CrankTracker                                  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--address", help="sensor address; default is the nearest")
    parser.add_argument("--list", action="store_true", help="scan, print, exit")
    parser.add_argument("--scan-time", type=float, default=8.0)
    args = parser.parse_args()

    address = args.address
    if address is None or args.list:
        print(f"scanning {args.scan_time:.0f}s for cadence/speed sensors "
              f"(turn the crank to wake one)...")
        found = await scan(args.scan_time)
        for f in found:
            print(f"  {f.address}  {f.rssi if f.rssi is not None else '?':>4} dBm  {f.name}")
        if not found:
            print("  none found")
            return 1
        if args.list:
            return 0
        address = found[0].address
        print(f"using the nearest: {found[0].name} ({address})")

    # Killed or hung up (an ssh session dropping), still disconnect: BlueZ
    # keeps a link the process never closed, and a connected sensor stops
    # advertising, so nothing can find it again until someone disconnects it.
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, task.cancel)

    tracker = CrankTracker()
    try:
        async with CadenceSensor(address) as sensor:
            tracker.link_up()
            print(f"connected to {sensor.name}; pedal. Ctrl-C to stop.\n")
            start = time.monotonic()
            was_turning = False
            async for item in sensor.stream(idle_timeout=0.5):
                now = time.monotonic()
                turning = tracker.turning(now)
                if was_turning and not turning:
                    print(f"{now - start:8.2f}s  -- stopped (no turn for "
                          f"{tracker.hold():.1f}s)")
                was_turning = turning
                if item is None:
                    continue
                at, reading = item
                rpm = tracker.rpm(at)
                rate = ("  -- " if rpm is None else f"{rpm:5.1f}")
                previous = tracker.last_turn_at
                new = tracker.submit(reading, at)
                since = ""
                if new and (previous is None or at - previous > 3.0):
                    since = ("   <- first turn" if previous is None
                             else f"   <- first turn after {at - previous:.1f}s")
                print(f"{at - start:8.2f}s  {'turn' if new else 'same'}  "
                      f"revs {reading.revolutions:5d}  t {reading.event_time:5d}  "
                      f"rpm {rate}{since}")
                was_turning = tracker.turning(at)
    except NoCrankData as exc:
        print(f"\n{exc}. If it is a dual-mode sensor, switch it to cadence "
              f"mode (see its instructions) and mount it on the crank.")
        return 1
    except ConnectionError as exc:
        print(f"\n{exc}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except (KeyboardInterrupt, asyncio.CancelledError):
        sys.exit(0)
