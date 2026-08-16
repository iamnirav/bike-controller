#!/usr/bin/env python3
"""Live cadence readout. Proves end-to-end bike reading works.

    python3 tools/live.py --address <addr>
"""

import argparse
import asyncio
import os
import sys
import time

# Make the repo root importable no matter where this is run from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bike_controller import IconBike           # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--address")
    parser.add_argument(
        "--interval", type=float, default=0.2,
        help="seconds between poll writes (default 0.2). Try 0.1 and 0.05 to "
             "raise the telemetry rate; watch the Hz readout and frame count.",
    )
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    address = args.address or await IconBike.discover()
    if address is None:
        print("No Icon bike found. Run tools/scan.py.")
        return 1

    print(f"Connecting to {address} ...")
    async with IconBike(address, poll_interval=args.interval) as bike:
        print(f"Connected (poll interval {args.interval*1000:.0f}ms). Pedal!\n")
        first = None
        async for state in bike.stream():
            now = time.monotonic()
            if first is None:
                first = now
            elapsed = now - first
            hz = bike.frames_received / elapsed if elapsed > 0.5 else float("nan")
            bar = "#" * min(50, state.cadence_rpm // 2)
            print(
                f"  cadence {state.cadence_rpm:>3} rpm  "
                f"power {state.power_w:>4} W  "
                f"res {state.resistance:>2}  "
                f"{hz:4.2f} Hz  |{bar:<50}|"
            )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nStopped.")
