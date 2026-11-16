"""A Bluetooth crank cadence sensor: the radio half.

Decoding and bookkeeping live in csc.py, which has no BLE dependency and is
where the tests are. This is only connect, subscribe and hand readings on.

Usage:

    found = await CadenceSensor.discover()
    async with CadenceSensor(found.address) as sensor:
        async for at, reading in sensor.stream(idle_timeout=1.0):
            ...

Most of these sensors accept ONE Bluetooth connection at a time. A phone app or
Zwift holding the sensor locks the bridge out, and vice versa.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner

from bike_controller.csc import (
    CSC_FEATURE,
    CSC_MEASUREMENT,
    CSC_SERVICE,
    CrankReading,
    parse_measurement,
    supports_crank,
)


class NoCrankData(Exception):
    """The sensor is a speed sensor, or a dual-mode one set to speed."""


@dataclass(frozen=True)
class Found:
    address: str
    name: str
    rssi: int | None
    # What the scan saw, for connecting. Handing BleakClient a bare address
    # makes it scan AGAIN, for up to its whole timeout, before connecting --
    # most of the ~40s connects measured on the Pi.
    device: object = None


async def scan(timeout: float = 8.0) -> list[Found]:
    """Every device advertising the cycling speed/cadence service, nearest first."""
    seen = await BleakScanner.discover(timeout=timeout, return_adv=True,
                                       service_uuids=[CSC_SERVICE])
    found = []
    for device, adv in seen.values():
        # The service filter is a hint to the OS, not a guarantee on every
        # backend, so check the advertisement as well.
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        if CSC_SERVICE not in uuids:
            continue
        found.append(Found(device.address,
                           adv.local_name or device.name or "?",
                           adv.rssi, device))
    found.sort(key=lambda f: -(f.rssi if f.rssi is not None else -999))
    return found


DBUS_TIMEOUT = 5.0


async def release_stranded(also: str | None = None) -> list[str]:
    """Disconnect cycling sensors BlueZ holds that nobody is using.

    A connected sensor stops advertising, so a scan cannot find it -- and BlueZ
    keeps an LE link up after the process that opened it has gone, if that
    process died without disconnecting (a killed probe did exactly this, and
    the bridge then searched for 17 minutes). Call it only when we hold no
    sensor ourselves: anything connected then is treated as an orphan. That
    includes one another process on this machine is using (crank_probe.py run
    while the bridge searches): on a Pi that exists to run the bridge, the
    bridge wins.

    Matches the CSC service, which BlueZ lists once services were resolved, or
    `also` (a pinned address). Linux only; elsewhere returns [] untouched.
    Returns the addresses released.
    """
    try:
        from dbus_fast import BusType, Message, MessageType
        from dbus_fast.aio import MessageBus
    except ImportError:
        return []
    released = []
    bus = None
    try:
        # Bounded: a wedged bluetoothd must not hang the sensor task forever.
        bus = await asyncio.wait_for(
            MessageBus(bus_type=BusType.SYSTEM).connect(), DBUS_TIMEOUT)
        reply = await asyncio.wait_for(bus.call(Message(
            destination="org.bluez", path="/",
            interface="org.freedesktop.DBus.ObjectManager",
            member="GetManagedObjects")), DBUS_TIMEOUT)
        if reply.message_type == MessageType.ERROR:
            return []
        for path, ifaces in reply.body[0].items():
            dev = ifaces.get("org.bluez.Device1")
            if not dev or not dev.get("Connected") or not dev["Connected"].value:
                continue
            address = dev["Address"].value if "Address" in dev else ""
            uuids = [u.lower() for u in dev["UUIDs"].value] if "UUIDs" in dev else []
            if CSC_SERVICE not in uuids and not (
                    also and address.upper() == also.upper()):
                continue
            await asyncio.wait_for(bus.call(Message(
                destination="org.bluez", path=path,
                interface="org.bluez.Device1", member="Disconnect")),
                DBUS_TIMEOUT)
            released.append(address)
    except Exception:                                       # noqa: BLE001
        # A cleanup, never a reason to stop looking for the sensor.
        pass
    finally:
        if bus is not None:
            bus.disconnect()
    return released


async def find(address: str, timeout: float = 8.0) -> Found | None:
    """A pinned sensor, found by address, so the connect needs no scan."""
    device = await BleakScanner.find_device_by_address(address, timeout=timeout)
    if device is None:
        return None
    return Found(device.address, device.name or "?", None, device)


class CadenceSensor:
    """One connected sensor, yielding (arrival_time, CrankReading)."""

    # Wheel-only frames in a row before giving up on a sensor. A dual-mode
    # sensor in speed mode sends nothing else, and should be named as such
    # rather than sat on forever looking connected.
    WHEEL_ONLY_LIMIT = 5

    def __init__(self, address: str, device=None) -> None:
        """`device` is the BLEDevice a scan returned, when there is one: it
        lets the connect skip a second scan. A bare address still works."""
        self.address = address
        self.name = address
        self._device = device
        self._client: BleakClient | None = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._failure: BaseException | None = None
        self._wheel_only = 0

    def _put(self, item) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(item)

    def _fail(self, exc: BaseException) -> None:
        if self._failure is None:
            self._failure = exc
        self._put(None)

    def _on_disconnect(self, _client) -> None:
        self._fail(ConnectionError("sensor disconnected"))

    def _on_notify(self, _char, raw: bytearray) -> None:
        at = time.monotonic()
        try:
            reading = parse_measurement(bytes(raw))
        except ValueError:
            return                      # one bad frame is not a dead sensor
        if reading is None:
            self._wheel_only += 1
            if self._wheel_only >= self.WHEEL_ONLY_LIMIT:
                self._fail(NoCrankData(
                    f"{self.name} sends wheel data only -- a speed sensor, or "
                    f"a dual-mode one in speed mode"))
            return
        self._wheel_only = 0
        self._put((at, reading))

    async def __aenter__(self) -> "CadenceSensor":
        # 40s, not bleak's usual 20, for a pinned address: then there is no
        # scan result to hand over, and bleak's own scan comes out of this.
        self._client = BleakClient(self._device or self.address, timeout=40.0,
                                   disconnected_callback=self._on_disconnect)
        await self._client.__aenter__()
        try:
            self.name = self._client.name or self.address
            # Optional on some sensors; a missing Feature is not a reason to
            # refuse one that does send crank data. The wheel-only check in
            # _on_notify catches the rest.
            feature = None
            with contextlib.suppress(Exception):
                feature = bytes(await self._client.read_gatt_char(CSC_FEATURE))
            if feature is not None and not supports_crank(feature):
                raise NoCrankData(f"{self.name} does not report crank data "
                                  f"(a speed sensor, or set to speed mode)")
            await self._client.start_notify(CSC_MEASUREMENT, self._on_notify)
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.__aexit__(*exc)

    async def stream(self, idle_timeout: float | None = None):
        """Yield (arrival_time, CrankReading) as they arrive.

        With idle_timeout, also yields None whenever that long passes with
        nothing -- not a failure: a sensor on a still crank may say nothing at
        all, and the caller still needs to wake up to check its settings.

        Raises ConnectionError on disconnect, NoCrankData for a speed sensor.
        """
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), idle_timeout)
            except asyncio.TimeoutError:
                yield None
                continue
            if item is None:
                if isinstance(self._failure, NoCrankData):
                    raise self._failure
                raise ConnectionError(f"sensor link lost: {self._failure}")
            yield item
