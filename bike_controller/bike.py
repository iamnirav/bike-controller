"""Live telemetry from a NordicTrack G/GX LE (Icon Health & Fitness) console.

Field offsets and the poll sequence were confirmed empirically against console
"54801-VV" firmware 22017.0908 on 2026-08-13; see README.md for the evidence.

Usage:

    async with IconBike(address) as bike:
        async for state in bike.stream():
            print(state.cadence_rpm)
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
import time
from dataclasses import dataclass, field

from bleak import BleakClient, BleakScanner

IFIT_WRITE = "00001534-1412-efde-1523-785feabcd123"
IFIT_NOTIFY = "00001535-1412-efde-1523-785feabcd123"

# A telemetry payload frame. Byte 5 distinguishes sub-types; 0x31 is the one
# carrying live cadence/power/resistance. 0x17 frames are all-0xff filler.
TELEMETRY_PREFIX = bytes([0x00, 0x12, 0x01, 0x04])
TELEMETRY_SUBTYPE = 0x31

CADENCE_OFFSET = 18
POWER_OFFSET = 12
RESISTANCE_OFFSET = 11
DISTANCE_OFFSET = 14


def _b(*head: int) -> bytes:
    return bytes(head) if len(head) == 4 else bytes(head).ljust(20, b"\x00")


INIT_SEQUENCE = [
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

# The "gx27" cycle (nordictrack_gx_2_7 / vr21 / cycle_trainer_300_ci), which is
# the one this console answers. Sent one packet per tick, looping forever --
# the console only speaks when spoken to.
POLL_SEQUENCE = [
    _b(0xFE, 0x02, 0x17, 0x03),
    _b(0x00, 0x12, 0x02, 0x04, 0x02, 0x13, 0x07, 0x13, 0x02, 0x00,
       0x0D, 0x3C, 0x9E, 0x31, 0x00, 0x00, 0x40, 0x40, 0x00, 0x80),
    _b(0xFF, 0x05, 0x00, 0x00, 0x00, 0x85, 0xB9),
    _b(0xFE, 0x02, 0x0D, 0x02),
    _b(0xFF, 0x0D, 0x02, 0x04, 0x02, 0x09, 0x07, 0x09, 0x02, 0x00,
       0x03, 0x80, 0x00, 0x40, 0xD5),
]


@dataclass
class BikeState:
    """One telemetry sample. `age` lets callers distrust stale data."""

    cadence_rpm: int = 0
    power_w: int = 0
    resistance: int = 0
    distance_raw: int = 0
    updated_at: float = field(default_factory=time.monotonic)

    @property
    def age(self) -> float:
        return time.monotonic() - self.updated_at


class IconBike:
    """Polls an Icon console and keeps `state` current.

    The console is request/response: it sends nothing unless polled, so the
    poll loop is not an optimisation, it is the protocol.
    """

    def __init__(self, address: str, poll_interval: float = 0.2) -> None:
        self.address = address
        self.poll_interval = poll_interval
        self.state = BikeState()
        self.frames_received = 0
        self._client: BleakClient | None = None
        self._poll_task: asyncio.Task | None = None
        # None is a sentinel meaning "the link died"; stream() turns it into an
        # exception so callers can reconnect. Without it, a dead link leaves
        # stream() waiting on a queue nothing will ever fill again.
        self._updates: asyncio.Queue[BikeState | None] = asyncio.Queue(maxsize=64)
        self._failure: BaseException | None = None

    @staticmethod
    async def discover(timeout: float = 20.0) -> str | None:
        """Find an Icon bike by its advertised name prefix."""
        device = await BleakScanner.find_device_by_filter(
            lambda d, adv: (adv.local_name or d.name or "").upper().startswith(("I_EB", "I_SB")),
            timeout=timeout,
        )
        return None if device is None else device.address

    def _fail(self, exc: BaseException) -> None:
        """Record a link failure and wake any waiting stream()."""
        if self._failure is None:
            self._failure = exc
        # A full queue is exactly when a waiter most needs waking, so drop the
        # oldest sample to make room rather than dropping the sentinel -- but
        # only when full, so a queue with space keeps every sample.
        try:
            self._updates.put_nowait(None)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._updates.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._updates.put_nowait(None)

    def _on_disconnect(self, _client) -> None:
        self._fail(ConnectionError("BLE disconnected"))

    def _on_notify(self, _char, raw: bytearray) -> None:
        data = bytes(raw)
        if (
            len(data) != 20
            or data[:4] != TELEMETRY_PREFIX
            or data[5] != TELEMETRY_SUBTYPE
        ):
            return
        self.frames_received += 1
        self.state = BikeState(
            cadence_rpm=data[CADENCE_OFFSET],
            power_w=struct.unpack_from("<H", data, POWER_OFFSET)[0],
            resistance=data[RESISTANCE_OFFSET],
            distance_raw=struct.unpack_from("<H", data, DISTANCE_OFFSET)[0],
        )
        with contextlib.suppress(asyncio.QueueFull):
            self._updates.put_nowait(self.state)

    async def _poll_forever(self) -> None:
        assert self._client is not None
        index = 0
        while True:
            packet = POLL_SEQUENCE[index % len(POLL_SEQUENCE)]
            try:
                # 0x1534 has no write-without-response property; unacknowledged
                # writes are silently dropped by CoreBluetooth.
                await self._client.write_gatt_char(IFIT_WRITE, packet, response=True)
            except Exception as exc:                       # noqa: BLE001
                # Tell stream() rather than returning silently -- a silent
                # return strands the consumer on an empty queue forever.
                self._fail(exc)
                return
            index += 1
            await asyncio.sleep(self.poll_interval)

    async def __aenter__(self) -> "IconBike":
        self._client = BleakClient(
            self.address, timeout=30.0, disconnected_callback=self._on_disconnect
        )
        await self._client.__aenter__()
        try:
            await self._client.start_notify(IFIT_NOTIFY, self._on_notify)

            # The handshake spans 5.2s of wall time and can fail on a marginal
            # link. __aexit__ does not run unless __aenter__ returns, so clean up
            # here or we strand a connected client -- and these consoles accept
            # exactly one connection, so an orphan locks out our own retry.
            for packet in INIT_SEQUENCE:
                await self._client.write_gatt_char(IFIT_WRITE, packet, response=True)
                await asyncio.sleep(0.4)

            self._poll_task = asyncio.create_task(self._poll_forever())
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.__aexit__(*exc)

    async def stream(self):
        """Yield each telemetry sample as it arrives (~1 Hz).

        Raises ConnectionError when the link dies, so callers can reconnect.
        """
        while True:
            item = await self._updates.get()
            if item is None:
                raise ConnectionError(f"bike link lost: {self._failure}")
            yield item
