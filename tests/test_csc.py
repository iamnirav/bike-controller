"""Tests for the cadence-sensor decoding and crank bookkeeping.

Runs with pytest, or standalone:  python3 tests/test_csc.py
"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bike_controller.csc import (      # noqa: E402
    CrankReading,
    CrankTracker,
    parse_measurement,
    supports_crank,
)


def crank_frame(revs, ticks):
    return bytes([0x02]) + struct.pack("<HH", revs & 0xFFFF, ticks & 0xFFFF)


def test_parses_crank_only_frame():
    assert parse_measurement(crank_frame(12, 3000)) == CrankReading(12, 3000)


def test_parses_crank_after_wheel_data():
    frame = bytes([0x03]) + struct.pack("<IH", 999, 5) + struct.pack("<HH", 7, 2048)
    assert parse_measurement(frame) == CrankReading(7, 2048)


def test_wheel_only_frame_has_no_crank_reading():
    frame = bytes([0x01]) + struct.pack("<IH", 999, 5)
    assert parse_measurement(frame) is None


def test_truncated_frame_is_refused():
    for bad in (b"", bytes([0x02, 1, 0])):
        try:
            parse_measurement(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_feature_bits():
    assert supports_crank(struct.pack("<H", 0x02))
    assert supports_crank(struct.pack("<H", 0x03))
    assert not supports_crank(struct.pack("<H", 0x01))
    assert not supports_crank(b"")


def pedal(tracker, rpm, seconds, t0=0.0, revs0=0, ticks0=0, notify_hz=1.0):
    """Turn the crank at `rpm`, with the sensor notifying at `notify_hz`.

    Returns (t, revs, ticks) at the end, for chaining.
    """
    period = 60.0 / rpm
    t, revs, ticks = t0, revs0, ticks0
    next_turn = t0 + period
    while t < t0 + seconds:
        t += 1.0 / notify_hz
        while next_turn <= t:
            revs += 1
            ticks = ticks0 + round((next_turn - t0) * 1024)
            next_turn += period
        tracker.submit(CrankReading(revs & 0xFFFF, ticks & 0xFFFF), now=t)
    return t, revs, ticks


def connected():
    tr = CrankTracker()
    tr.link_up()
    return tr


def test_first_reading_is_a_baseline_not_a_turn():
    tr = connected()
    assert not tr.submit(CrankReading(500, 100), now=0.0)
    assert not tr.turning(0.0)


def test_steady_pedalling_measures_cadence():
    tr = connected()
    t, _, _ = pedal(tr, 60, 6)
    assert tr.turning(t)
    assert abs(tr.rpm(t) - 60) < 1, tr.rpm(t)


def test_repeated_count_is_not_a_turn():
    tr = connected()
    tr.submit(CrankReading(10, 1000), now=0.0)
    assert tr.submit(CrankReading(11, 2024), now=1.0)
    assert not tr.submit(CrankReading(11, 2024), now=2.0)
    assert tr.last_turn_at == 1.0


def test_stopping_ends_turning_within_the_hold():
    tr = connected()
    t, revs, ticks = pedal(tr, 60, 6)
    # Sensor keeps notifying the same count.
    for i in range(1, 6):
        tr.submit(CrankReading(revs, ticks), now=t + i)
    assert not tr.turning(t + 3.1)
    assert tr.rpm(t + 3.1) == 0.0


def ride_and_check(rpm, phase, seconds=20.0, step=0.05):
    """Pedal steadily with 1 Hz notifications at `phase`, checking turning()
    every `step` -- between notifications, where a gap would show. Returns
    the times it read as stopped."""
    tr = connected()
    period = 60.0 / rpm
    dropped = []
    next_notify = phase
    t = 0.0
    while t < seconds:
        if t >= next_notify:
            revs = int(next_notify / period)
            ticks = round(revs * period * 1024)
            tr.submit(CrankReading(revs & 0xFFFF, ticks & 0xFFFF), now=next_notify)
            next_notify += 1.0
        # Allow the start: a baseline, then the first turn, then the rate.
        if t > 8.0 and not tr.turning(t):
            dropped.append(round(t, 2))
        t += step
    return dropped


def test_a_steady_rider_is_never_dropped_between_notifications():
    """From a real ride: at about 50 rpm with 1 Hz notifications, turns
    arrived 2.0s apart, and the old hold of 1.5 periods dropped the rider
    between strokes."""
    for rpm in (20, 22, 30, 40, 45, 50, 55, 60, 75, 90, 120):
        for phase in (0.0, 0.3, 0.6, 0.9):
            assert ride_and_check(rpm, phase) == [], (rpm, phase)


def test_a_clock_that_paused_through_a_stop_gives_no_rate():
    """From review, against a Moofit CS8, whose event clock seems to stop
    while the crank is still. A 3s stop then one revolution: the sensor's
    interval leaves the stop out and would read as a confident cadence. Our
    own arrival times say the interval was really ~3.5s."""
    tr = connected()
    tr.submit(CrankReading(10, 10_000), now=0.0)            # baseline
    tr.submit(CrankReading(11, 10_800), now=1.0)
    tr.submit(CrankReading(12, 11_600), now=2.0)            # 0.78s, 77 rpm
    assert tr.rpm(2.0) is not None
    # Stopped ~3s; the clock did not run. One revolution, 0.8s of ticks.
    tr.submit(CrankReading(13, 12_400), now=5.5)
    assert tr.turning(5.5)
    assert tr.rpm(5.5) is None, tr.rpm(5.5)


def test_a_running_clock_still_measures_the_rate_after_a_short_stop():
    """A slow revolution honestly timed by the sensor -- 2.4s, arriving 3s
    after the last -- is a rate we can use."""
    tr = connected()
    tr.submit(CrankReading(10, 0), now=0.0)                 # baseline
    tr.submit(CrankReading(11, 1024), now=1.0)
    tr.submit(CrankReading(12, 2048), now=2.0)
    tr.submit(CrankReading(13, 2048 + round(2.4 * 1024)), now=5.0)
    assert abs(tr.rpm(5.0) - 25.0) < 0.5, tr.rpm(5.0)


def test_first_turn_after_a_rest_is_turning_at_unknown_rate():
    tr = connected()
    t, revs, ticks = pedal(tr, 60, 6)
    # 10s rest, then one revolution.
    t2 = t + 10
    assert tr.submit(CrankReading(revs + 1, ticks + 11 * 1024), now=t2)
    assert tr.turning(t2)
    assert tr.rpm(t2) is None
    # The next one measures it.
    tr.submit(CrankReading(revs + 2, ticks + 12 * 1024), now=t2 + 1)
    assert abs(tr.rpm(t2 + 1) - 60) < 1


def test_event_clock_wrap_after_long_rest_does_not_alias():
    """65s later the 1/1024s clock has wrapped, so the masked interval looks
    like one second. Our own clock must overrule it."""
    tr = connected()
    tr.submit(CrankReading(10, 1000), now=0.0)
    tr.submit(CrankReading(11, 2024), now=1.0)
    tr.submit(CrankReading(12, (2024 + 65 * 1024) & 0xFFFF), now=66.0)
    assert tr.turning(66.0)
    assert tr.rpm(66.0) is None


def test_counter_wrap_is_continuous():
    tr = connected()
    tr.submit(CrankReading(0xFFFF, 1000), now=0.0)
    tr.submit(CrankReading(0, 2024), now=1.0)
    tr.submit(CrankReading(1, 3048), now=2.0)
    assert abs(tr.rpm(2.0) - 60) < 1


def test_implausible_jump_is_rebaselined():
    tr = connected()
    tr.submit(CrankReading(10, 1000), now=0.0)
    assert not tr.submit(CrankReading(500, 2024), now=1.0)
    assert not tr.turning(1.0)


def test_disconnected_tracker_reports_nothing():
    tr = connected()
    t, _, _ = pedal(tr, 60, 5)
    tr.link_down()
    assert not tr.turning(t)
    assert tr.rpm(t) == 0.0


def test_slow_pedalling_holds_between_strokes():
    tr = connected()
    t, _, _ = pedal(tr, 30, 10)       # one turn every 2s
    last = tr.last_turn_at
    assert tr.turning(last + 2.9)


if __name__ == "__main__":
    from _runner import main          # noqa: E402 - script-mode only
    main(globals())
