"""Bluetooth cadence sensors: the pure half.

The console pauses for ~5s every time you stop pedalling, and throws away
anything you pedal during it (see tools/idle_probe.py and README.md). Nothing it
sends tracks the cranks in that window, and nothing we can send it shortens it.
So the answer is a second witness the console does not control: an ordinary
crank-arm cadence sensor speaking the standard Bluetooth Cycling Speed and
Cadence profile (CSC, service 0x1816) -- Wahoo, Garmin, Magene, Moofit and most
others.

This module is the decoding and the bookkeeping. It has no BLE dependency, like
mapping.py, so it unit-tests on a laptop. The radio half is cadence_sensor.py.

WHAT A CSC SENSOR ACTUALLY SENDS
--------------------------------
Not a cadence. A Measurement notification (0x2A5B) carries a cumulative crank
revolution count (uint16) and the time of the most recent revolution in 1/1024s
units (uint16, wraps every 64s). Cadence is derived from two of them:

    rpm = 60 * revolutions_since / seconds_since

Sensors typically notify about once a second whether or not anything changed,
repeating the same count while the crank is still, and many go silent entirely
once idle. So "a notification arrived" means nothing; "the count went up" is the
whole signal.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

CSC_SERVICE = "00001816-0000-1000-8000-00805f9b34fb"
CSC_MEASUREMENT = "00002a5b-0000-1000-8000-00805f9b34fb"
CSC_FEATURE = "00002a5c-0000-1000-8000-00805f9b34fb"

WHEEL_DATA_PRESENT = 0x01
CRANK_DATA_PRESENT = 0x02

# CSC Feature bit 1: crank revolution data supported. A sensor that clears it
# is a SPEED sensor (wheel hub), useless on a stationary bike. Dual-mode units
# such as the Magene S3+ report whichever mode they are currently in.
FEATURE_CRANK = 0x02

EVENT_TICKS_PER_SECOND = 1024


@dataclass(frozen=True)
class CrankReading:
    revolutions: int        # cumulative, uint16, wraps
    event_time: int         # time of the latest revolution, 1/1024 s, uint16


def parse_measurement(data: bytes) -> CrankReading | None:
    """Decode a CSC Measurement. None when it carries no crank data.

    Raises ValueError on a malformed frame rather than guessing at it.
    """
    if not data:
        raise ValueError("empty CSC measurement")
    flags = data[0]
    offset = 1
    if flags & WHEEL_DATA_PRESENT:
        # uint32 cumulative wheel revolutions + uint16 last wheel event time.
        offset += 6
    if not flags & CRANK_DATA_PRESENT:
        return None
    if len(data) < offset + 4:
        raise ValueError(f"truncated CSC measurement ({len(data)} bytes, "
                         f"flags {flags:#04x})")
    revolutions, event_time = struct.unpack_from("<HH", data, offset)
    return CrankReading(revolutions, event_time)


def supports_crank(feature: bytes) -> bool:
    """Whether a CSC Feature value says the sensor reports crank data."""
    if len(feature) < 2:
        return False
    return bool(struct.unpack_from("<H", feature, 0)[0] & FEATURE_CRANK)


class CrankTracker:
    """When the crank last turned, and how fast it was turning.

    Times are the bridge's own monotonic clock, stamped when each notification
    ARRIVED. The sensor's event_time is used only for the interval between two
    revolutions, which is what it is accurate for; its absolute value is a
    free-running counter in the sensor with no relation to our clock.
    """

    # One revolution in this long or longer is not a cadence, it is a rest
    # ending: 2.5s per turn is 24 rpm. The first turn after stopping spans the
    # whole stop, and reporting "6 rpm" for it would be worse than "unknown".
    MAX_PERIOD = 2.5
    # How long "turning" survives without a new revolution. A revolution only
    # REACHES us at the sensor's next notification, about once a second (a
    # Moofit CS8 measured 0.99s), so two turns can arrive up to one period PLUS
    # one notification interval apart. Holding only 1.5 periods dropped a real
    # 50 rpm rider for half a second between strokes: turns arrived 2.0s apart
    # against a 1.5s hold. Clamped so a stopped rider is not believed for long.
    NOTIFY_INTERVAL = 1.0
    HOLD_SLACK = 0.25
    MIN_HOLD = 1.5
    MAX_HOLD = 4.5
    # Before the rate is known (the first turn after a rest), assume nothing
    # slower than 40 rpm: 1.5s per turn.
    UNKNOWN_PERIOD = 1.5
    # A count that jumps by more than this between two notifications is not
    # pedalling, it is a sensor that reset or a link that was down. At 160 rpm
    # with 1 Hz notifications the honest jump is under 3.
    MAX_JUMP = 20

    def __init__(self) -> None:
        self.connected = False
        self._last: CrankReading | None = None
        self._turned_at: float | None = None
        self._period: float | None = None
        self._reported_at: float | None = None
        self._gap: float | None = None

    def link_up(self) -> None:
        self.reset()
        self.connected = True

    def link_down(self) -> None:
        self.reset()
        self.connected = False

    def reset(self) -> None:
        """Forget everything. A reconnected sensor's count is not continuous
        with the old one, and a turn seen before the outage proves nothing now."""
        self._last = None
        self._turned_at = None
        self._period = None
        self._reported_at = None
        self._gap = None

    def submit(self, reading: CrankReading, now: float) -> bool:
        """Feed one decoded measurement. Returns True if it was a new turn."""
        last = self._last
        self._last = reading
        self._reported_at = now
        if last is None:
            # A baseline, not a turn: the count says how many revolutions
            # ever, not that one just happened.
            return False
        revs = (reading.revolutions - last.revolutions) & 0xFFFF
        if revs == 0:
            return False                    # the ~1 Hz repeat while still
        if revs > self.MAX_JUMP:
            return False                    # re-baselined above
        ticks = (reading.event_time - last.event_time) & 0xFFFF
        seconds = ticks / EVENT_TICKS_PER_SECOND
        # Check the sensor's interval against our own clock. Each turn reaches
        # us up to one report after it happened, so two turns that ARRIVED
        # `gap` apart happened within a report interval of `gap` apart. An
        # interval outside that is not this one: the event clock wrapped
        # during a long rest (every 64s), or it paused while the crank was
        # still -- a Moofit CS8's does, and would otherwise report a confident
        # cadence that leaves the stop out.
        gap = None if self._turned_at is None else now - self._turned_at
        slack = self.NOTIFY_INTERVAL + self.HOLD_SLACK
        period = seconds / revs
        if (gap is not None and abs(seconds - gap) <= slack
                and 0 < period <= self.MAX_PERIOD):
            self._period = period
        else:
            # Proven turning, rate not yet known. The next revolution fixes it.
            self._period = None
        # Turns arriving this far apart are slow pedalling, not a rest ending:
        # what the hold must cover when there is no period to go on.
        self._gap = (gap if gap is not None
                     and gap <= self.MAX_PERIOD + self.NOTIFY_INTERVAL else None)
        self._turned_at = now
        return True

    @property
    def last_turn_at(self) -> float | None:
        return self._turned_at

    @property
    def last_report_at(self) -> float | None:
        """When the sensor last said anything, turn or not."""
        return self._reported_at

    @property
    def revolutions(self) -> int | None:
        """The sensor's raw cumulative count, for the ride log."""
        return None if self._last is None else self._last.revolutions

    def hold(self) -> float:
        if self._period is not None:
            period = self._period
        elif self._gap is not None:
            # Too slow for a rate (under 24 rpm): the next turn can arrive a
            # whole report later than this gap.
            period = max(self.UNKNOWN_PERIOD, self._gap)
        else:
            period = self.UNKNOWN_PERIOD
        return min(self.MAX_HOLD, max(self.MIN_HOLD, period
                                      + self.NOTIFY_INTERVAL + self.HOLD_SLACK))

    def turning(self, now: float) -> bool:
        return (self.connected and self._turned_at is not None
                and now - self._turned_at <= self.hold())

    def rpm(self, now: float) -> float | None:
        """Cadence while turning, 0.0 when stopped, None when turning at a rate
        not yet measured (the first revolution after a rest)."""
        if not self.turning(now):
            return 0.0
        if self._period is None:
            return None
        return 60.0 / self._period
