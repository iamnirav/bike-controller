#!/usr/bin/env python3
"""Connect to an Icon/iFit console, poll it for telemetry, and find the data bytes.

    python3 tools/probe_bike.py --address <addr from scan.py>

The Icon protocol is REQUEST/RESPONSE, not streaming. Three things must happen
in order or the console stays silent:

  1. Subscribe to notifications on 0x1535.
  2. Send a 13-packet init handshake on 0x1534 (write WITH response -- the
     characteristic does not support write-without-response). This identifies
     the console; the replies carry model, firmware and serial as ASCII.
  3. Then poll forever: write a short "noOp" request every 200ms, cycling
     through a fixed sequence. Each request draws one reply, and the replies are
     where telemetry lives. Stop polling and the console stops talking.

The exact poll sequence differs per console generation, so this tries each known
variant in turn and keeps whichever one produces telemetry frames.

All of it is transcribed from qdomyos-zwift's proformbike.cpp (btinit + update).
"""

import argparse
import asyncio
import contextlib
import signal
import struct
import sys
import time
from collections import defaultdict

from bleak import BleakClient, BleakScanner

IFIT_WRITE = "00001534-1412-efde-1523-785feabcd123"
IFIT_NOTIFY = "00001535-1412-efde-1523-785feabcd123"
DFU_SERVICE = "00001530-1212-efde-1523-785feabcd123"
FTMS_SERVICE = "00001826-0000-1000-8000-00805f9b34fb"
FTMS_INDOOR_BIKE_DATA = "00002ad2-0000-1000-8000-00805f9b34fb"

# A telemetry payload frame always starts with these four bytes.
TELEMETRY_PREFIX = bytes([0x00, 0x12, 0x01, 0x04])
TELEMETRY_SUBTYPE = 0x31


def _b(*head: int) -> bytes:
    """A 4-byte command, or a 20-byte frame zero-padded to length."""
    return bytes(head) if len(head) == 4 else bytes(head).ljust(20, b"\x00")


IFIT_INIT_SEQUENCE = [
    _b(0xFE, 0x02, 0x08, 0x02),
    _b(0xFF, 0x08, 0x02, 0x04, 0x02, 0x04, 0x02, 0x04, 0x81, 0x87),
    _b(0xFE, 0x02, 0x08, 0x02),
    _b(0xFF, 0x08, 0x02, 0x04, 0x02, 0x04, 0x07, 0x04, 0x80, 0x8B),
    _b(0xFE, 0x02, 0x08, 0x02),
    _b(0xFF, 0x08, 0x02, 0x04, 0x02, 0x04, 0x07, 0x04, 0x88, 0x93),
    _b(0xFE, 0x02, 0x0A, 0x02),
    _b(0xFF, 0x0A, 0x02, 0x04, 0x02, 0x06, 0x02, 0x06, 0x82, 0x00, 0x00, 0x8A),
    _b(0xFE, 0x02, 0x0A, 0x02),
    _b(0xFF, 0x0A, 0x02, 0x04, 0x02, 0x06, 0x02, 0x06, 0x84, 0x00, 0x00, 0x8C),
    _b(0xFE, 0x02, 0x08, 0x02),
    _b(0xFF, 0x08, 0x02, 0x04, 0x02, 0x04, 0x02, 0x04, 0x95, 0x9B),
    _b(0xFE, 0x02, 0x2C, 0x04),
]

# Per-generation poll cycles, sent one packet per 200ms tick, looping forever.
POLL_VARIANTS: dict[str, list[bytes]] = {
    # nordictrack_gx_2_7 / nordictrack_vr21 / proform_cycle_trainer_300_ci.
    # The VR21 is Icon's other recumbent, so this is the best first guess.
    "gx27": [
        _b(0xFE, 0x02, 0x17, 0x03),
        _b(0x00, 0x12, 0x02, 0x04, 0x02, 0x13, 0x07, 0x13, 0x02, 0x00,
           0x0D, 0x3C, 0x9E, 0x31, 0x00, 0x00, 0x40, 0x40, 0x00, 0x80),
        _b(0xFF, 0x05, 0x00, 0x00, 0x00, 0x85, 0xB9),
        _b(0xFE, 0x02, 0x0D, 0x02),
        _b(0xFF, 0x0D, 0x02, 0x04, 0x02, 0x09, 0x07, 0x09, 0x02, 0x00,
           0x03, 0x80, 0x00, 0x40, 0xD5),
    ],
    "generic": [
        _b(0xFE, 0x02, 0x19, 0x03),
        _b(0x00, 0x12, 0x02, 0x04, 0x02, 0x15, 0x07, 0x15, 0x02, 0x00,
           0x0F, 0xBC, 0x90, 0x70, 0x00, 0x00, 0x00, 0x40, 0x00, 0x00),
        _b(0xFF, 0x07, 0x00, 0x00, 0x00, 0x10, 0x00, 0x08, 0x5D),
        _b(0xFE, 0x02, 0x17, 0x03),
        _b(0x00, 0x12, 0x02, 0x04, 0x02, 0x13, 0x07, 0x13, 0x02, 0x00,
           0x0D, 0x3C, 0x9C, 0x31, 0x00, 0x00, 0x40, 0x40, 0x00, 0x80),
        _b(0xFF, 0x05, 0x00, 0x80, 0x01, 0x00, 0xA9),
        _b(0xFE, 0x02, 0x0D, 0x02),
    ],
    "gx45pro": [
        _b(0xFE, 0x02, 0x17, 0x03),
        _b(0x00, 0x12, 0x02, 0x04, 0x02, 0x13, 0x07, 0x13, 0x02, 0x00,
           0x0D, 0x3C, 0x9C, 0x31, 0x00, 0x00, 0x40, 0x40, 0x00, 0x80),
        _b(0xFF, 0x05, 0x00, 0x80, 0x01, 0x00, 0xA9),
        _b(0xFE, 0x02, 0x0D, 0x02),
        _b(0xFE, 0x02, 0x19, 0x03),
        _b(0x00, 0x12, 0x02, 0x04, 0x02, 0x15, 0x07, 0x15, 0x02, 0x00,
           0x0F, 0xBC, 0x90, 0x70, 0x00, 0x00, 0x00, 0x40, 0x00, 0x00),
        _b(0xFF, 0x07, 0x00, 0x00, 0x00, 0x10, 0x00, 0x08, 0x5D),
    ],
}


class ByteTable:
    """Per-frame-shape record of which byte positions actually move."""

    def __init__(self) -> None:
        self.frames: dict[tuple[str, int, int, int], dict] = defaultdict(
            lambda: {"count": 0, "min": None, "max": None, "last": None}
        )

    def add(self, source: str, data: bytes) -> None:
        if not data:
            return
        # Sub-type on byte 5. Every iFit frame starts 0x00 and is 20 bytes, so
        # keying on (length, first byte) alone lumps live telemetry (0x31),
        # all-0xff filler (0x17) and the ASCII handshake replies into ONE
        # bucket -- min/max then span 0..255 on nearly every byte and the
        # MOVING line becomes noise. Sub-typing is still generic discovery, not
        # an assumption about what any field means.
        subtype = data[5] if len(data) >= 6 else -1
        slot = self.frames[(source, len(data), data[0], subtype)]
        slot["count"] += 1
        slot["last"] = list(data)
        if slot["min"] is None:
            slot["min"], slot["max"] = list(data), list(data)
        else:
            for i, byte in enumerate(data):
                slot["min"][i] = min(slot["min"][i], byte)
                slot["max"][i] = max(slot["max"][i], byte)

    def render(self) -> str:
        if not self.frames:
            return "  (no data was ever received)"
        out = []
        for (source, length, first, subtype), slot in sorted(self.frames.items()):
            tag = f"  subtype=0x{subtype:02x}" if subtype >= 0 else ""
            out.append(f"\n  {source}  first=0x{first:02x}{tag}  "
                       f"len={length}  n={slot['count']}")
            out.append("    idx  " + " ".join(f"{i:>3}" for i in range(length)))
            out.append("    last " + " ".join(f"{b:>3}" for b in slot["last"]))
            out.append("    min  " + " ".join(f"{b:>3}" for b in slot["min"]))
            out.append("    max  " + " ".join(f"{b:>3}" for b in slot["max"]))
            moving = [
                f"{i}({slot['min'][i]}-{slot['max'][i]})"
                for i in range(length)
                if slot["max"][i] != slot["min"][i]
            ]
            out.append("    MOVING: " + (", ".join(moving) or "(nothing moved)"))
        return "\n".join(out)


def ascii_of(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


def decode_telemetry(data: bytes) -> dict:
    """qdomyos-zwift's field offsets for the GX/VR family. A hypothesis, not truth.

    Byte 5 must be checked: 0x17 frames share the 00 12 01 04 prefix but are
    all-0xff filler, and decoding them yields a confident-looking
    cadence=255 power=65535 that then poisons the summary.
    """
    if len(data) != 20 or data[:4] != TELEMETRY_PREFIX:
        return {}
    if data[5] != TELEMETRY_SUBTYPE:
        return {}
    return {
        "cadence?": data[18],
        "power_w?": struct.unpack_from("<H", data, 12)[0],
        "resistance?": data[11],
    }


def decode_ftms(data: bytes) -> dict:
    if len(data) < 4:
        return {}
    flags = struct.unpack_from("<H", data, 0)[0]
    out, pos = {}, 2
    with contextlib.suppress(struct.error):
        if not flags & 1:
            out["speed_kph"] = struct.unpack_from("<H", data, pos)[0] * 0.01
            pos += 2
        if flags & (1 << 2):
            out["cadence_rpm"] = struct.unpack_from("<H", data, pos)[0] * 0.5
    return out


async def pick_device(args):
    if args.address:
        print(f"Connecting directly to {args.address} ...")
        return args.address
    print(f"Scanning for a name starting with {args.name!r} ...")
    device = await BleakScanner.find_device_by_filter(
        lambda d, adv: (adv.local_name or d.name or "").upper().startswith(args.name.upper()),
        timeout=20.0,
    )
    if device is None:
        print(f"No device matching {args.name!r}. Run tools/scan.py first.")
        return None
    print(f"Found {device.name} [{device.address}]")
    return device


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", help="BLE address (macOS: a UUID) from scan.py")
    parser.add_argument("--name", default="I_")
    parser.add_argument("--seconds", type=float, default=300.0)
    parser.add_argument(
        "--variant",
        default="auto",
        choices=["auto", *POLL_VARIANTS],
        help="poll sequence to use; 'auto' tries each until telemetry appears",
    )
    parser.add_argument("--try-seconds", type=float, default=15.0,
                        help="how long to give each variant in auto mode")
    parser.add_argument("--interval", type=float, default=0.2, help="poll tick")
    parser.add_argument("--quiet", action="store_true",
                        help="only print telemetry frames, not every reply")
    args = parser.parse_args()

    sys.stdout.reconfigure(line_buffering=True)

    target = await pick_device(args)
    if target is None:
        return 1

    table = ByteTable()
    counters = {"notify": 0, "telemetry": 0}
    strings: set[str] = set()
    latest: dict = {}
    start = time.monotonic()

    stop_event = asyncio.Event()
    with contextlib.suppress(NotImplementedError):
        asyncio.get_running_loop().add_signal_handler(signal.SIGINT, stop_event.set)

    def print_summary() -> None:
        # Ctrl-C kills `tee` first, so writing only to stdout loses the summary to
        # a broken pipe. Always persist it to a file as well.
        with contextlib.suppress(Exception):
            with open("probe-summary.txt", "w") as fh:
                fh.write(table.render())
                fh.write("\n\nASCII: " + ", ".join(sorted(strings)))
                fh.write(f"\n\ntotals: {counters['notify']} notifications, "
                         f"{counters['telemetry']} telemetry frames\n")
        print("\n\n=== WHICH BYTES MOVED ===")
        print(table.render())
        if strings:
            print("\n=== ASCII SEEN IN REPLIES (model / firmware / serial) ===")
            for s in sorted(strings):
                print(f"  {s}")
        if latest:
            print("\n=== LAST DECODED ===")
            for key, value in latest.items():
                print(f"  {key} = {value}")
        print(f"\ntotals: {counters['notify']} notifications, "
              f"{counters['telemetry']} telemetry frames")

    async with BleakClient(target, timeout=30.0) as client:
        print(f"\nConnected. MTU={client.mtu_size}\n")

        chars = {}
        print("=== GATT TREE ===")
        for service in client.services:
            print(f"\n  service {service.uuid}  ({service.description})")
            for char in service.characteristics:
                chars[char.uuid.lower()] = char
                print(f"    char {char.uuid}  [{','.join(char.properties)}]")
        print()

        def handler(_char, raw: bytearray):
            data = bytes(raw)
            counters["notify"] += 1
            is_telemetry = (len(data) == 20 and data[:4] == TELEMETRY_PREFIX
                            and data[5] == TELEMETRY_SUBTYPE)
            table.add("telemetry" if is_telemetry else f"reply-0x{data[0]:02x}", data)

            text = ascii_of(data)
            # Long printable runs are model/firmware/serial strings worth keeping.
            for chunk in text.replace(".", " ").split():
                if len(chunk) >= 6:
                    strings.add(chunk)

            if is_telemetry:
                counters["telemetry"] += 1
                decoded = decode_telemetry(data)
                latest.update(decoded)
                fields = "  ".join(f"{k}={v}" for k, v in decoded.items())
                print(f"[{time.monotonic() - start:7.2f}s] TELEMETRY {data.hex(' ')}"
                      f"\n            -> {fields}", flush=True)
            elif not args.quiet:
                print(f"[{time.monotonic() - start:7.2f}s]           {data.hex(' ')}"
                      f"   |{text}|", flush=True)

        print("=== SUBSCRIBING ===")
        for service in client.services:
            if service.uuid.lower() == DFU_SERVICE:
                continue
            for char in service.characteristics:
                if {"notify", "indicate"} & set(char.properties):
                    with contextlib.suppress(Exception):
                        await client.start_notify(char, handler)
                        print(f"  {char.uuid}")
        print()

        write_char = chars.get(IFIT_WRITE)
        if write_char is None:
            print("No iFit write characteristic. Cannot wake this console.")
            print_summary()
            return 1

        print("=== HANDSHAKE (13 packets, write-with-response) ===")
        for i, packet in enumerate(IFIT_INIT_SEQUENCE, 1):
            try:
                await client.write_gatt_char(write_char, packet, response=True)
                status = "ok"
            except Exception as exc:                      # noqa: BLE001
                status = f"FAILED {type(exc).__name__}: {exc}"
            print(f"  init {i:>2}/13 [{status}]")
            await asyncio.sleep(0.4)
        print()

        print("=" * 72)
        print("PEDAL NOW -- easy, then hard, then STOP, then change resistance.")
        print("Ctrl-C when done.")
        print("=" * 72 + "\n")

        # --- the poll loop: this is what actually makes telemetry flow ---------
        order = [args.variant] if args.variant != "auto" else list(POLL_VARIANTS)
        chosen = None

        async def poll_with(name: str, deadline: float | None) -> int:
            """Cycle one variant's request sequence until deadline. Returns frames seen."""
            sequence = POLL_VARIANTS[name]
            before = counters["telemetry"]
            index = 0
            while not stop_event.is_set():
                if deadline is not None and time.monotonic() > deadline:
                    break
                try:
                    await client.write_gatt_char(
                        write_char, sequence[index % len(sequence)], response=True
                    )
                except Exception as exc:                  # noqa: BLE001
                    print(f"  poll write failed: {exc}", flush=True)
                    break
                index += 1
                await asyncio.sleep(args.interval)
            return counters["telemetry"] - before

        for name in order:
            if stop_event.is_set():
                break
            print(f">>> Trying poll variant {name!r} "
                  f"({len(POLL_VARIANTS[name])} packets, {args.interval*1000:.0f}ms apart)\n")
            got = await poll_with(name, time.monotonic() + args.try_seconds)
            print(f"\n>>> variant {name!r}: {got} telemetry frames\n")
            if got > 0:
                chosen = name
                break

        if chosen is None and not stop_event.is_set():
            print("!! No variant produced telemetry. Staying on the last one so you\n"
                  "   can watch the raw replies; Ctrl-C when you have seen enough.\n")
            chosen = order[-1]

        if not stop_event.is_set():
            print(f">>> Locked on variant {chosen!r}. Keep pedaling; vary your effort.\n")
            heartbeat = asyncio.create_task(poll_with(chosen, None))
            deadline = time.monotonic() + args.seconds
            while not stop_event.is_set() and time.monotonic() < deadline:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=10.0)
                if not stop_event.is_set():
                    print(f"  ... {time.monotonic() - start:5.0f}s | "
                          f"{counters['notify']} replies | "
                          f"{counters['telemetry']} telemetry", flush=True)
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

        # Print BEFORE disconnecting -- a hung disconnect must not eat the results.
        print_summary()
        print(f"\nwinning poll variant: {chosen}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
