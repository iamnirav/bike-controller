#!/usr/bin/env python3
"""Scan for BLE devices and flag anything that looks like an iFit/Icon machine.

Run this standing next to the bike, with the console awake (pedal a few turns)
and with the iFit app FORCE-QUIT on every phone in the room -- these consoles
only accept one connection at a time.

    python3 tools/scan.py

Works on macOS and Linux. On macOS the "address" is a system-assigned UUID
rather than a real MAC; that's normal and still usable for connecting.
"""

import asyncio
import sys

from bleak import BleakScanner

# Icon Health & Fitness (NordicTrack / ProForm / FreeMotion) proprietary service.
IFIT_SERVICE = "00001533-1412-efde-1523-785feabcd123"

# Standard fitness services worth knowing about, in preference order.
STANDARD_SERVICES = {
    "00001826-0000-1000-8000-00805f9b34fb": "FTMS (Fitness Machine Service) -- best case",
    "00001816-0000-1000-8000-00805f9b34fb": "CSC (Cycling Speed & Cadence)",
    "00001818-0000-1000-8000-00805f9b34fb": "CPS (Cycling Power)",
    "0000180d-0000-1000-8000-00805f9b34fb": "HRS (Heart Rate)",
}

# Advertised-name prefixes qdomyos-zwift matches for Icon bikes.
IFIT_NAME_HINTS = ("I_EB", "I_SB", "I_RB", "I_VE", "I_EL", "I_TL", "I_RW", "I_FS", "I_IT")


def classify(name: str, uuids: list[str]) -> list[str]:
    """Return human-readable reasons this device is interesting, if any."""
    reasons = []
    upper = (name or "").upper()
    lower_uuids = {u.lower() for u in uuids}

    if upper.startswith(IFIT_NAME_HINTS) or "_IFIT_" in upper:
        reasons.append("name matches an Icon/iFit machine")
    if IFIT_SERVICE in lower_uuids:
        reasons.append("advertises the iFit proprietary service 0x1533")
    for uuid, label in STANDARD_SERVICES.items():
        if uuid in lower_uuids:
            reasons.append(f"advertises {label}")
    return reasons


async def main() -> int:
    # Python block-buffers stdout when piped (e.g. through `tee`), which makes a
    # working tool look hung. Every tool here sets this.
    sys.stdout.reconfigure(line_buffering=True)

    seconds = 15.0
    print(f"Scanning for {seconds:.0f}s -- pedal the bike so the console stays awake...\n")

    # return_adv=True gives us the advertisement data, which carries service UUIDs.
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)

    interesting, other = [], []
    for address, (device, adv) in found.items():
        name = adv.local_name or device.name or ""
        reasons = classify(name, adv.service_uuids)
        (interesting if reasons else other).append((name, address, adv, reasons))

    if interesting:
        print("=== LIKELY THE BIKE ===\n")
        for name, address, adv, reasons in interesting:
            print(f"  {name or '(no name)'}   [{address}]   rssi={adv.rssi}")
            for reason in reasons:
                print(f"      - {reason}")
            for uuid in adv.service_uuids:
                print(f"      service: {uuid}")
            print()
        print("Next:  python3 tools/probe_bike.py --address <address above>\n")
    else:
        print("No obvious fitness machine found.\n")

    print(f"=== EVERYTHING ELSE ({len(other)} devices) ===\n")
    for name, address, adv, _ in sorted(other, key=lambda r: -r[2].rssi):
        print(f"  {name or '(no name)':<28} [{address}]  rssi={adv.rssi}")

    if not interesting:
        print(
            "\nNothing matched. Things to try, in order:\n"
            "  1. Make sure the console is awake (pedal) and no phone is connected to it.\n"
            "  2. Look through the list above for an unnamed device whose RSSI is very\n"
            "     strong (> -50) and rises when you move closer to the console.\n"
            "  3. Put the console into its Bluetooth pairing mode if it has one, then rescan.\n"
            "  4. If still nothing, the console may only advertise while the iFit app is\n"
            "     actively trying to pair -- start pairing in the app, then rescan.\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
