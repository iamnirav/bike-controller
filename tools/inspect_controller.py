#!/usr/bin/env python3
"""List attached controllers and live-dump their events.

    python3 tools/inspect_controller.py            # list what is attached
    python3 tools/inspect_controller.py --watch    # then press every button

Use this to confirm a controller reports standard codes before trusting it in
the bridge. Pads in D-input mode sometimes expose the d-pad as buttons rather
than a hat, or shuffle face buttons; this is how we find out.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evdev import InputDevice, ecodes as e, list_devices          # noqa: E402

from bike_controller.gamepad import DEVICE_NAME                   # noqa: E402

# What a well-behaved X-input pad should report.
EXPECTED_BUTTONS = {
    "BTN_A": e.BTN_A, "BTN_B": e.BTN_B, "BTN_X": e.BTN_X, "BTN_Y": e.BTN_Y,
    "BTN_TL": e.BTN_TL, "BTN_TR": e.BTN_TR,
    "BTN_SELECT": e.BTN_SELECT, "BTN_START": e.BTN_START,
    "BTN_THUMBL": e.BTN_THUMBL, "BTN_THUMBR": e.BTN_THUMBR,
}
EXPECTED_AXES = {
    "ABS_X": e.ABS_X, "ABS_Y": e.ABS_Y,
    "ABS_RX": e.ABS_RX, "ABS_RY": e.ABS_RY,
    "ABS_Z": e.ABS_Z, "ABS_RZ": e.ABS_RZ,
    "ABS_HAT0X": e.ABS_HAT0X, "ABS_HAT0Y": e.ABS_HAT0Y,
}


def gamepads() -> list[InputDevice]:
    found = []
    for path in sorted(list_devices()):
        try:
            device = InputDevice(path)
        except Exception:                                          # noqa: BLE001
            continue
        if device.name == DEVICE_NAME:
            continue                                               # our own virtual pad
        keys = device.capabilities().get(e.EV_KEY, [])
        if e.BTN_A in keys or e.BTN_GAMEPAD in keys or e.BTN_JOYSTICK in keys:
            found.append(device)
    return found


def describe(device: InputDevice) -> None:
    info = device.info
    print(f"\n  {device.path}  {device.name!r}")
    print(f"    bus=0x{info.bustype:04x} vendor={info.vendor:04x} "
          f"product={info.product:04x} version={info.version:04x}")

    caps = device.capabilities()
    keys = set(caps.get(e.EV_KEY, []))
    axes = {code: info for code, info in caps.get(e.EV_ABS, [])}

    missing_b = [n for n, c in EXPECTED_BUTTONS.items() if c not in keys]
    missing_a = [n for n, c in EXPECTED_AXES.items() if c not in axes]

    print(f"    buttons: {len(keys)} reported"
          + (f"  MISSING: {', '.join(missing_b)}" if missing_b else "  (all standard present)"))
    for name, code in EXPECTED_AXES.items():
        if code in axes:
            a = axes[code]
            print(f"      {name:<10} range {a.min}..{a.max}")
    if missing_a:
        print(f"    MISSING AXES: {', '.join(missing_a)}")

    extra = sorted(k for k in keys if k not in EXPECTED_BUTTONS.values())
    if extra:
        names = [e.KEY.get(k, e.BTN.get(k, str(k))) for k in extra[:12]]
        names = [n if isinstance(n, str) else "/".join(n) for n in names]
        print(f"    extra buttons: {', '.join(names)}"
              + (" ..." if len(extra) > 12 else ""))


async def watch(devices: list[InputDevice]) -> None:
    print("\nPress every button and move every stick. Ctrl-C when done.\n")

    async def pump(device: InputDevice) -> None:
        async for event in device.async_read_loop():
            if event.type == e.EV_KEY:
                name = e.BTN.get(event.code, e.KEY.get(event.code, event.code))
                if not isinstance(name, str):
                    name = "/".join(name)
                action = "press " if event.value else "release"
                print(f"  {action} {name}", flush=True)
            elif event.type == e.EV_ABS and event.code in EXPECTED_AXES.values():
                name = e.ABS.get(event.code, event.code)
                print(f"  axis   {name} = {event.value}", flush=True)

    await asyncio.gather(*(pump(d) for d in devices))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="live-dump events")
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    devices = gamepads()
    if not devices:
        print("No controllers found.\n"
              "  - plugged in over USB?\n"
              "  - if Bluetooth, is it paired and connected?\n"
              "  - some 8BitDo modes present as a keyboard rather than a gamepad;\n"
              "    try X-input mode (usually a switch or Start+X at power-on).")
        return 1

    print(f"Found {len(devices)} controller(s):")
    for device in devices:
        describe(device)

    if args.watch:
        try:
            asyncio.run(watch(devices))
        except KeyboardInterrupt:
            print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
